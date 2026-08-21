"""Student authentication: Uni ID -> email OTP -> lazy-provisioned session.
Separate from the advisor password login. No self-registration, no passwords.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from core.models import Course, Student, StudentTermSection
from core.services.advisor_presentations import graduation_presentation_from_tool_results
from core.services.rbac import ROLE_STUDENT, get_user_scope
from core.services.recommender import recommend_next_courses
from core.services.student_graduation import RECOMMENDED_CURRENT_TERM, build_graduation_report
from core.services.student_helpers import normalize_code
from core.services.student_home_cards import build_student_home_cards, progress_buckets
from core.services.student_identity import normalize_student_id, student_email
from core.services.student_otp import (
    OTPError,
    issue_otp,
    mark_student_authentication,
    provision_student_user,
    verify_otp,
)
from core.services.student_sections import (
    arabic_term_section_course_names,
    get_student_term_baseline,
    prefer_arabic_course_names_in_payload,
    prefer_arabic_timetable_course_names,
    section_gender,
    student_gender,
)
from core.services.student_unlock import build_unlock_report
from core.services.timetable_provenance import baseline_sections
from core.services.timetable_snapshots import Snapshot, forecast_rows
from core.services.timetable_snapshots import select as select_snapshot
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)
_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _request_prefers_arabic(request: HttpRequest) -> bool:
    return str(getattr(request, "LANGUAGE_CODE", "") or "").lower().startswith("ar")


def _client_ip(request: HttpRequest) -> str:
    # Behind a trusted proxy, the right-most XFF entry is the one the proxy appended
    # (a client cannot forge it). Otherwise trust only the direct peer (REMOTE_ADDR).
    if getattr(settings, "IP_FROM_XFF", False):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[-1].strip()[:64]
    return (request.META.get("REMOTE_ADDR", "") or "")[:64]


#: Where to send the student after they sign in, when something sent them here on
#: the way to somewhere else. Held in the SESSION rather than round-tripped through
#: the two login forms: the OTP step is a separate POST that carries none of the
#: first step's fields, so a hidden input would have to be threaded through both
#: templates — and a value the browser sends back is a value the browser can
#: change, which is the whole open-redirect surface. The session cannot be edited
#: by its owner.
_NEXT_SESSION_KEY = "post_login_next"


#: How long a remembered destination stays usable. A login is a few minutes of
#: work; anything older belongs to a session somebody walked away from.
_NEXT_MAX_AGE_SECONDS = 600


def _remember_next(request: HttpRequest) -> None:
    """Record a validated `?next=`, and forget any earlier one.

    `login_required` already appends `?next=` to every protected student route, so
    this parameter has been arriving and being discarded since those routes
    existed. Honouring it needs the check Django's own `LoginView` does — neither
    login view here is a `LoginView`, so none of that protection is inherited.

    Clearing on a request that carries NO `next` is the other half, and it is not
    tidiness: a student who starts a redirect-carrying login on a shared lab
    machine and walks away leaves the destination in a session that outlives them,
    and the next person to sign in on that browser would be sent somewhere they
    never asked to go.
    """
    from django.utils.http import url_has_allowed_host_and_scheme

    candidate = str(request.GET.get("next") or "").strip()
    if not candidate:
        # Only a fresh GET clears. The Uni-ID POST is the second step of the SAME
        # login and carries none of the first step's query string, so clearing on
        # any request without `next` would drop the destination halfway through
        # the flow it was recorded for.
        if request.method == "GET":
            request.session.pop(_NEXT_SESSION_KEY, None)
        return
    # Same host, same scheme, and a path rather than a bare authority. The leading
    # single slash is checked explicitly because `//evil.example` is a
    # protocol-relative URL that some parsers treat as a path.
    if not candidate.startswith("/") or candidate.startswith("//"):
        request.session.pop(_NEXT_SESSION_KEY, None)
        return
    if not url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        request.session.pop(_NEXT_SESSION_KEY, None)
        return
    request.session[_NEXT_SESSION_KEY] = {
        "url": candidate[:500],
        "at": timezone.now().isoformat(),
    }


def _post_login_redirect(request: HttpRequest):
    """Where sign-in lands. `student_home` unless something asked for elsewhere."""
    destination = _fresh_next_destination(request, consume=True)
    return redirect(destination) if destination else redirect("student_home")


def _fresh_next_destination(request: HttpRequest, *, consume: bool = False) -> str:
    """Return the saved destination only while it belongs to this login attempt."""

    stored = (
        request.session.pop(_NEXT_SESSION_KEY, None)
        if consume
        else request.session.get(_NEXT_SESSION_KEY)
    )
    if not isinstance(stored, dict):
        return ""
    try:
        asked_at = datetime.fromisoformat(str(stored.get("at") or ""))
        age = timezone.now() - asked_at
    except (TypeError, ValueError):
        return ""
    if age < timedelta(0) or age > timedelta(seconds=_NEXT_MAX_AGE_SECONDS):
        return ""
    destination = str(stored.get("url") or "")
    return destination[:500] if destination else ""


def _ip_throttled(request: HttpRequest, bucket: str, limit: int, window: int) -> bool:
    key = f"student_otp:{bucket}:{_client_ip(request)}"
    n = cache.get(key, 0)
    if n >= limit:
        return True
    cache.set(key, n + 1, window)
    return False


@never_cache
@require_http_methods(["GET", "POST"])
def student_login_view(request: HttpRequest) -> HttpResponse:
    # Recorded before the already-authenticated shortcut, so a signed-in student
    # following a link that needs them somewhere specific still gets there.
    _remember_next(request)

    if request.user.is_authenticated:
        scope = get_user_scope(request.user)
        if scope.get("role") == ROLE_STUDENT:
            return _post_login_redirect(request)
        # Staff never follow a student's `next`: the destination was chosen for a
        # student session and may not be theirs to see.
        request.session.pop(_NEXT_SESSION_KEY, None)
        return redirect("dashboard")

    if request.method == "GET":
        return render(request, "core/student_login.html", {"step": "id"})

    raw = str(request.POST.get("student_id", "")).strip()
    try:
        student_id = normalize_student_id(raw)
        email = student_email(student_id)
    except ValueError:
        return render(
            request,
            "core/student_login.html",
            {
                "step": "id",
                "error": "يرجى إدخال رقم جامعي صحيح.",
            },
        )

    if _ip_throttled(request, "send", limit=8, window=900):
        return render(
            request,
            "core/student_login.html",
            {
                "step": "id",
                "error": "تم تجاوز العدد المسموح به من المحاولات. حاول مرة أخرى لاحقًا.",
            },
        )

    exists = Student.objects.filter(student_id=student_id).exists()

    if exists:
        try:
            issue_otp(student_id, _client_ip(request))
        except OTPError:
            # Rate-limited or send-failed: swallow silently so the response is
            # identical to the unknown-id / success paths (no enumeration branch).
            pass
    # Advance to OTP step regardless (enumeration-resistant). Only a real id got a code.
    request.session["otp_student_id"] = student_id if exists else 0
    return render(
        request,
        "core/student_login.html",
        {
            "step": "otp",
            "student_id": str(student_id),
            "email": email,
            "sent": True,  # template renders the bilingual "code sent" notice
        },
    )


@never_cache
@require_POST
def student_otp_verify_view(request: HttpRequest) -> HttpResponse:
    student_id = int(request.session.get("otp_student_id", 0) or 0)
    code = str(request.POST.get("code", "")).strip()

    if _ip_throttled(request, "verify", limit=15, window=900):
        return render(
            request,
            "core/student_login.html",
            {
                "step": "id",
                "error": "تم تجاوز العدد المسموح به من المحاولات. حاول مرة أخرى لاحقًا.",
            },
        )

    if student_id and verify_otp(student_id, code):
        try:
            user = provision_student_user(student_id)
        except OTPError:
            return render(
                request,
                "core/student_login.html",
                {
                    "step": "id",
                    "error": "تعذّر تسجيل الدخول باستخدام هذا الحساب.",
                },
            )
        login(request, user, backend=_MODEL_BACKEND)
        mark_student_authentication(request)
        request.session.pop("otp_student_id", None)
        return _post_login_redirect(request)

    return render(
        request,
        "core/student_login.html",
        {
            "step": "otp",
            "student_id": str(student_id or ""),
            "email": student_email(student_id) if student_id else "",
            "error": "رمز التحقق غير صحيح أو انتهت صلاحيته.",
        },
    )


# The stored day labels are 3-letter upper codes (SUN, MON, ...); normalise on the
# first three characters so full names ("Sunday") sort into the same week order.
_DAY_ORDER = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


def _weekly_timetable(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group the student's registered meetings into week-ordered days. Rows with no
    time are returned separately (unscheduled/unmapped sections)."""
    by_day: dict[str, list[dict]] = {}
    unscheduled: list[dict] = []
    for r in rows:
        day = str(r.get("day") or "").strip()
        if day and str(r.get("start_time") or "").strip():
            by_day.setdefault(day[:3].upper(), []).append(r)
        else:
            unscheduled.append(r)

    def block(code: str) -> dict:
        return {
            "code": code,
            "meetings": sorted(by_day[code], key=lambda m: str(m.get("start_time") or "")),
        }

    ordered = [block(c) for c in _DAY_ORDER if c in by_day]
    # any unrecognised label still shows, after the known week
    ordered += [block(c) for c in sorted(by_day) if c not in _DAY_ORDER]
    return ordered, unscheduled


@never_cache
@login_required
def student_courses_view(request: HttpRequest) -> HttpResponse:
    """ "What can I take, what is locked, and why" — all from the session identity."""
    scope = get_user_scope(request.user)
    if scope.get("role") != ROLE_STUDENT:
        return redirect("dashboard")
    student_id = scope.get("student_id")
    if student_id is None:
        return render(
            request,
            "core/student_home.html",
            {**get_sidebar_context(request), "unlinked": True},
            status=409,
        )

    defaults = load_defaults()
    year, term = int(defaults["academic_year"]), int(defaults["term"])
    try:
        report = build_unlock_report(
            student_id,
            year,
            term,
            prefer_arabic_names=_request_prefers_arabic(request),
        )
    except Exception:  # noqa: BLE001 — the page must degrade, never 500
        logger.exception("unlock report failed for %s", student_id)
        report = None

    return render(
        request,
        "core/student_courses.html",
        {
            **get_sidebar_context(request),
            "student": Student.objects.filter(student_id=student_id).first(),
            "student_id": student_id,
            "academic_year": year,
            "term": term,
            "report": report or None,
            "progress": progress_buckets(report) if report else None,
            "recommended_next_term": [
                course
                for course in (report or {}).get("open_courses", [])
                if course.get("fits_this_term")
            ],
            "open_other_terms": [
                course
                for course in (report or {}).get("open_courses", [])
                if not course.get("fits_this_term")
            ],
        },
    )


@never_cache
@login_required
def student_plan_map_view(request: HttpRequest) -> HttpResponse:
    """Full-width view of the same prerequisite graph used by the unlock report."""
    scope = get_user_scope(request.user)
    if scope.get("role") != ROLE_STUDENT:
        return redirect("dashboard")
    student_id = scope.get("student_id")
    if student_id is None:
        return render(
            request,
            "core/student_home.html",
            {**get_sidebar_context(request), "unlinked": True},
            status=409,
        )

    defaults = load_defaults()
    year, term = int(defaults["academic_year"]), int(defaults["term"])
    try:
        report = build_unlock_report(
            student_id,
            year,
            term,
            prefer_arabic_names=_request_prefers_arabic(request),
        )
    except Exception:  # noqa: BLE001 — the page must degrade, never 500
        logger.exception("student plan map failed for %s", student_id)
        report = None

    return render(
        request,
        "core/student_plan_map.html",
        {
            **get_sidebar_context(request),
            "student": Student.objects.filter(student_id=student_id).first(),
            "student_id": student_id,
            "academic_year": year,
            "term": term,
            "report": report or None,
        },
    )


@never_cache
@login_required
def student_graduation_view(request: HttpRequest) -> HttpResponse:
    """How far from graduating — plan progress, and how long, honestly split."""
    scope = get_user_scope(request.user)
    if scope.get("role") != ROLE_STUDENT:
        return redirect("dashboard")
    student_id = scope.get("student_id")
    if student_id is None:
        return render(
            request,
            "core/student_home.html",
            {**get_sidebar_context(request), "unlinked": True},
            status=409,
        )

    defaults = load_defaults()
    year = int(defaults.get("currentYear") or defaults["academic_year"])
    configured_term = defaults.get("currentTerm")
    term = int(configured_term if configured_term is not None else defaults["term"])
    try:
        grad = build_graduation_report(
            student_id,
            year,
            term,
            planning_baseline_kind=RECOMMENDED_CURRENT_TERM,
        )
        if grad and _request_prefers_arabic(request):
            grad = prefer_arabic_course_names_in_payload(grad)
    except Exception:  # noqa: BLE001 — degrade, never 500
        logger.exception("graduation report failed for %s", student_id)
        grad = None

    graduation_presentation = {}
    if grad:
        graduation_presentation = graduation_presentation_from_tool_results(
            [
                {
                    **grad,
                    "tool": "graduation_progress",
                    "ok": True,
                    "scenario_academic_year": year,
                    "scenario_term": term,
                }
            ]
        )

    return render(
        request,
        "core/student_graduation.html",
        {
            **get_sidebar_context(request),
            "student": Student.objects.filter(student_id=student_id).first(),
            "student_id": student_id,
            "academic_year": year,
            "term": term,
            "grad": grad or None,
            "graduation_presentation": graduation_presentation,
        },
    )


@never_cache
@login_required
def student_advisor_view(request: HttpRequest) -> HttpResponse:
    """Student-facing AI advisor. The chat endpoint forces the session identity, so
    this page never sends (or accepts) a student id."""
    scope = get_user_scope(request.user)
    if scope.get("role") != ROLE_STUDENT:
        return redirect("virtual_advisor_page")
    student_id = scope.get("student_id")
    if student_id is None:
        return render(
            request,
            "core/student_home.html",
            {**get_sidebar_context(request), "unlinked": True},
            status=409,
        )
    return render(
        request,
        "core/student_advisor.html",
        {
            **get_sidebar_context(request),
            "student": Student.objects.filter(student_id=student_id).first(),
            "student_id": student_id,
        },
    )


@never_cache
@login_required
def student_home_view(request: HttpRequest) -> HttpResponse:
    scope = get_user_scope(request.user)
    if scope.get("role") != ROLE_STUDENT:
        return redirect("dashboard")

    # Identity comes ONLY from the session scope — never from the request.
    student_id = scope.get("student_id")
    if student_id is None:
        # Account in the STUDENT group but never bound to a student record. Render a
        # terminal page — redirecting to the login view would bounce back here forever.
        return render(
            request,
            "core/student_home.html",
            {**get_sidebar_context(request), "unlinked": True},
            status=409,
        )

    student = Student.objects.filter(student_id=student_id).first()
    defaults = load_defaults()
    year, term = int(defaults["academic_year"]), int(defaults["term"])

    # Show the configured term. If nothing is registered there, fall back to the
    # one published timetable in the database — but ONLY when the whole database
    # holds exactly one (year, term). TermSection carries no year/term of its own,
    # so with two generations loaded its meetings could belong to either and a past
    # term would be drawn with another term's times; in that case show nothing
    # rather than something plausible and wrong. The label always names the term.
    #
    # ``Snapshot.ANY`` because this screen is the one surface that deliberately
    # presents both snapshots at once. Every other reader picks a single class; here
    # the rows are partitioned below and each class gets its OWN card with its own
    # heading, so nothing is ever merged into one grid that could not say which
    # meeting was registered and which was only planned.
    try:
        configured_rows = get_student_term_baseline(
            student_id, str(year), str(term), snapshot=Snapshot.ANY
        )
    except Exception:  # noqa: BLE001
        logger.exception("student timetable load failed for %s", student_id)
        configured_rows = []

    # One cohort rule, applied before these rows feed ANY home-screen fact. The
    # timetable, hours card, plan state and recommendation exclusion must all see
    # the same configured-term evidence.
    gender = student_gender(student_id)

    def visible_to_student(candidate_rows: list[dict]) -> list[dict]:
        if not gender:
            return candidate_rows
        return [
            row
            for row in candidate_rows
            if section_gender(str(row.get("section") or "")) in ("", gender)
        ]

    configured_rows = visible_to_student(configured_rows)
    if _request_prefers_arabic(request):
        configured_rows = prefer_arabic_timetable_course_names(configured_rows)

    # Split by PROVENANCE CLASS, not by "is it the expected prefix or not". The
    # old two-way split promoted the staff planner's own mappings — sources
    # `planner` and `auto_from_studying`, written by two staff-only endpoints in
    # core/planner_views.py — into the registered half, and this screen then titled
    # them «جدولي الأسبوعي» with no disclaimer.
    #
    # The forecast card shows the department's LATEST forecast, not always the
    # imported one. A planner save writes WORKING rows and, since each writer now
    # replaces only its own class, the imported plan survives underneath it — so
    # picking EXPECTED unconditionally would show a student the seating their
    # department has already moved them out of, for ever, with no writer that ever
    # removes it. `forecast_rows` applies the same precedence the advisor's
    # EFFECTIVE resolution uses, which is also what keeps chat and this screen
    # naming the same plan.
    registered_configured_rows = select_snapshot(configured_rows, Snapshot.REGISTERED)
    expected_rows = forecast_rows(configured_rows)

    tt_year, tt_term, tt_fallback = year, term, False
    # These two are the CONFIGURED term's evidence and must stay bound to it. The
    # fallback below re-reads another term for the panels only: the hours card and
    # the recommendation filter are labelled with the configured term, so feeding
    # them 1447/2's registrations under a 1448/1 label is a false statement about
    # what the student is carrying now.
    panel_registered_rows = registered_configured_rows
    panel_expected_rows = expected_rows
    if not registered_configured_rows and not expected_rows:
        # Nothing at all in the configured term. Fall back to the one published
        # timetable in the database — but ONLY when the whole database holds exactly
        # one (year, term). TermSection carries no year/term of its own, so with two
        # generations loaded its meetings could belong to either and a past term
        # would be drawn with another term's times; in that case show nothing rather
        # than something plausible and wrong. The label always names the term.
        published = list(
            StudentTermSection.objects.filter(term_section__scenario__isnull=True)
            .values_list("academic_year", "term")
            .distinct()[:2]
        )
        if len(published) == 1:
            published_year, published_term = published[0]
            mine = StudentTermSection.objects.filter(
                student_id=student_id,
                academic_year=str(published_year),
                term=str(published_term),
                term_section__scenario__isnull=True,
            ).exists()
            is_other_term = (str(published_year), str(published_term)) != (
                str(year),
                str(term),
            )
            if mine and is_other_term:
                tt_year, tt_term, tt_fallback = published_year, published_term, True
                try:
                    fallback_rows = visible_to_student(
                        get_student_term_baseline(
                            student_id,
                            str(tt_year),
                            str(tt_term),
                            snapshot=Snapshot.ANY,
                        )
                    )
                    if _request_prefers_arabic(request):
                        fallback_rows = prefer_arabic_timetable_course_names(fallback_rows)
                except Exception:  # noqa: BLE001
                    logger.exception("student fallback timetable failed for %s", student_id)
                else:
                    panel_registered_rows = select_snapshot(fallback_rows, Snapshot.REGISTERED)
                    panel_expected_rows = forecast_rows(fallback_rows)

    def _panel(kind: str, panel_rows: list[dict]) -> dict:
        """One card: its own grid, its own agenda table, its own provenance.

        Each meeting carries the class of the rows it was built from. The previous
        payload stamped every meeting with the kind of the WHOLE snapshot, so in a
        term holding both, a genuinely registered lecture and a merely planned one
        were both labelled "mixed" and the grid could not tell them apart. A panel
        is built from one class, so its label is a fact about every cell in it.
        """
        panel_timetable, panel_unscheduled = _weekly_timetable(panel_rows)
        return {
            "kind": kind,
            "dom_id": f"studentHomeTimetable-{kind}",
            "data_id": f"studentHomeTimetableData-{kind}",
            "academic_year": tt_year,
            "term": tt_term,
            "is_fallback": tt_fallback,
            "timetable": panel_timetable,
            "unscheduled": panel_unscheduled,
            "meetings": [
                {
                    "course_code": meeting.get("course_code") or "",
                    "course_name": meeting.get("course_name") or "",
                    "section": meeting.get("section") or "",
                    "day": day["code"],
                    "start_time": meeting.get("start_time") or "",
                    "end_time": meeting.get("end_time") or "",
                    "room": meeting.get("room") or "",
                    "instructor": meeting.get("instructor") or "",
                    "source": "planned" if kind == "expected" else "current",
                }
                for day in panel_timetable
                for meeting in day["meetings"]
            ],
        }

    # Registered first: it is what is true. The expected plan follows as the
    # forecast it is compared against. Built from the PANEL rows, which are the
    # configured term's unless the fallback replaced them.
    timetable_panels = []
    if panel_registered_rows:
        timetable_panels.append(_panel("registered", panel_registered_rows))
    if panel_expected_rows:
        timetable_panels.append(_panel("expected", panel_expected_rows))

    registered_codes = list(
        dict.fromkeys(
            normalize_code(row.get("course_code") or "")
            for row in baseline_sections(registered_configured_rows)
            if normalize_code(row.get("course_code") or "")
        )
    )
    registered_code_set = set(registered_codes)
    expected_code_set = {
        normalize_code(row.get("course_code") or "")
        for row in baseline_sections(expected_rows)
        if normalize_code(row.get("course_code") or "")
    }
    timetable_code_set = registered_code_set | expected_code_set

    # What the plan said and the registrar did not record. Only meaningful when BOTH
    # snapshots exist: with no registration on file, every planned course would list
    # here and read as an accusation rather than a difference.
    expected_not_registered = (
        sorted(expected_code_set - registered_code_set)
        if registered_code_set and expected_code_set
        else []
    )
    registered_not_expected = (
        sorted(registered_code_set - expected_code_set)
        if registered_code_set and expected_code_set
        else []
    )

    try:
        all_rec_codes = list(
            dict.fromkeys(
                normalize_code(code)
                for code in recommend_next_courses(student_id, year, term)
                if normalize_code(code)
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception("student recommendations failed for %s", student_id)
        all_rec_codes = []
    rec_codes = [code for code in all_rec_codes if code not in timetable_code_set]
    recommendations_already_current = [
        code for code in all_rec_codes if code in registered_code_set
    ]
    recommendations_already_expected = [code for code in all_rec_codes if code in expected_code_set]
    # ONE service for every card on this screen. `eligible_now` used to be computed
    # here — a second implementation of "what can this student take", rendered as
    # «متاحة للتسجيل هذا الفصل». Prerequisite data does not establish that a course
    # is offered this term, that a section is published, or that a seat exists; and
    # two implementations of one card eventually disagree, with the one on screen
    # being the one nobody tested.
    try:
        home_cards = build_student_home_cards(
            student_id=student_id,
            academic_year=year,
            term=term,
            # An imported next-term plan is useful timetable evidence, but it is
            # not proof that the student registered those hours or is studying
            # those courses. Academic-summary cards use registrar evidence only.
            current_term_rows=registered_configured_rows,
            prefer_arabic_names=_request_prefers_arabic(request),
        )
    except Exception:  # noqa: BLE001 — one card block degrades, the page does not
        logger.exception("student home cards failed for %s", student_id)
        home_cards = None

    course_info = {
        normalize_code(code): {"name": name or "", "credits": credits}
        for code, name, credits in Course.objects.filter(course_code__in=rec_codes).values_list(
            "course_code", "description", "credit_hours"
        )
    }
    arabic_recommendation_names = (
        arabic_term_section_course_names(rec_codes) if _request_prefers_arabic(request) else {}
    )
    recommendations = [
        {
            "code": code,
            "name": arabic_recommendation_names.get(code)
            or course_info.get(code, {}).get("name", ""),
            "credits": course_info.get(code, {}).get("credits"),
        }
        for code in rec_codes
    ]

    # base.html includes the shared sidebar partial, which hides its staff navigation
    # for a STUDENT role (see partials/sidebar.html) — the student gets the app shell.
    return render(
        request,
        "core/student_home.html",
        {
            **get_sidebar_context(request),
            "student": student,
            "student_id": student_id,
            "academic_year": year,
            "term": term,
            "timetable_year": tt_year,
            "timetable_term": tt_term,
            "timetable_is_fallback": tt_fallback,
            # One entry per snapshot the student actually has, already separated.
            # There is deliberately no merged `timetable`/`timetable_meetings` pair
            # any more: a single list could only be rendered under a single heading,
            # and that heading would have to lie about one of the two snapshots.
            "timetable_panels": timetable_panels,
            "has_registered_timetable": bool(registered_configured_rows),
            "has_expected_timetable": bool(expected_rows),
            "expected_not_registered": expected_not_registered,
            "registered_not_expected": registered_not_expected,
            "recommendations": recommendations,
            "recommendations_already_current": recommendations_already_current,
            "recommendations_already_expected": recommendations_already_expected,
            "home_cards": home_cards,
        },
    )
