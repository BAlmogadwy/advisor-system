"""Student authentication: Uni ID -> email OTP -> lazy-provisioned session.
Separate from the advisor password login. No self-registration, no passwords.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from core.models import Course, Student, StudentTermSection
from core.services.advisor_presentations import graduation_presentation_from_tool_results
from core.services.rbac import ROLE_STUDENT, get_user_scope
from core.services.recommender import recommend_next_courses
from core.services.student_graduation import build_graduation_report
from core.services.student_helpers import normalize_code
from core.services.student_home_cards import build_student_home_cards, progress_buckets
from core.services.student_otp import OTPError, issue_otp, provision_student_user, verify_otp
from core.services.student_sections import get_student_term_baseline, section_gender, student_gender
from core.services.student_unlock import build_unlock_report
from core.services.timetable_provenance import baseline_sections
from core.settings_views import load_defaults
from core.sidebar_context import get_sidebar_context

logger = logging.getLogger(__name__)
_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _client_ip(request: HttpRequest) -> str:
    # Behind a trusted proxy, the right-most XFF entry is the one the proxy appended
    # (a client cannot forge it). Otherwise trust only the direct peer (REMOTE_ADDR).
    if getattr(settings, "IP_FROM_XFF", False):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[-1].strip()[:64]
    return (request.META.get("REMOTE_ADDR", "") or "")[:64]


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
    if request.user.is_authenticated:
        scope = get_user_scope(request.user)
        return redirect("student_home" if scope.get("role") == ROLE_STUDENT else "dashboard")

    if request.method == "GET":
        return render(
            request,
            "core/student_login.html",
            {
                "step": "id",
                "no_otp_mode": getattr(settings, "STUDENT_LOGIN_NO_OTP", False),
            },
        )

    raw = str(request.POST.get("student_id", "")).strip()
    if not raw.isdigit():
        return render(
            request,
            "core/student_login.html",
            {"step": "id", "error": "أدخل رقمًا جامعيًا صحيحًا · Enter a valid University ID"},
        )

    if _ip_throttled(request, "send", limit=8, window=900):
        return render(
            request,
            "core/student_login.html",
            {"step": "id", "error": "محاولات كثيرة، حاول لاحقًا · Too many attempts, try later"},
        )

    student_id = int(raw)
    exists = Student.objects.filter(student_id=student_id).exists()

    # TESTING BYPASS: sign in from the Uni ID alone, no code. Double-guarded by
    # DEBUG + STUDENT_LOGIN_NO_OTP (settings resolves it to False whenever DEBUG is
    # off), so it cannot be switched on in production by env alone.
    if getattr(settings, "STUDENT_LOGIN_NO_OTP", False) and exists:
        logger.warning("STUDENT_LOGIN_NO_OTP: signing in %s WITHOUT a code (testing)", student_id)
        try:
            user = provision_student_user(student_id)
        except OTPError:
            return render(
                request,
                "core/student_login.html",
                {
                    "step": "id",
                    "error": "تعذّر تسجيل الدخول لهذا الحساب · Login unavailable for this account",
                },
            )
        login(request, user, backend=_MODEL_BACKEND)
        return redirect("student_home")

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
            "student_id": raw,
            "email": f"{raw}@{settings.STUDENT_EMAIL_DOMAIN}",
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
            {"step": "id", "error": "محاولات كثيرة، حاول لاحقًا · Too many attempts, try later"},
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
                    "error": "تعذّر تسجيل الدخول لهذا الحساب · Login unavailable for this account",
                },
            )
        login(request, user, backend=_MODEL_BACKEND)
        request.session.pop("otp_student_id", None)
        return redirect("student_home")

    return render(
        request,
        "core/student_login.html",
        {
            "step": "otp",
            "student_id": str(student_id or ""),
            "email": f"{student_id}@{settings.STUDENT_EMAIL_DOMAIN}" if student_id else "",
            "error": "رمز غير صحيح أو منتهي · Invalid or expired code",
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
        report = build_unlock_report(student_id, year, term)
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
        report = build_unlock_report(student_id, year, term)
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
    year, term = int(defaults["academic_year"]), int(defaults["term"])
    try:
        grad = build_graduation_report(student_id, year, term)
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
    try:
        configured_rows = get_student_term_baseline(student_id, str(year), str(term))
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
    rows = list(configured_rows)
    tt_year, tt_term, tt_fallback = year, term, False
    if not rows:
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
                    rows = visible_to_student(
                        get_student_term_baseline(student_id, str(tt_year), str(tt_term))
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("student fallback timetable failed for %s", student_id)
                    rows = []
    timetable, unscheduled = _weekly_timetable(rows)
    timetable_meetings = [
        {
            "course_code": meeting.get("course_code") or "",
            "course_name": meeting.get("course_name") or "",
            "section": meeting.get("section") or "",
            "day": day["code"],
            "start_time": meeting.get("start_time") or "",
            "end_time": meeting.get("end_time") or "",
            "room": meeting.get("room") or "",
            "instructor": meeting.get("instructor") or "",
            "source": "current",
        }
        for day in timetable
        for meeting in day["meetings"]
    ]

    current_codes = list(
        dict.fromkeys(
            normalize_code(row.get("course_code") or "")
            for row in baseline_sections(configured_rows)
            if normalize_code(row.get("course_code") or "")
        )
    )
    current_code_set = set(current_codes)

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
    rec_codes = [code for code in all_rec_codes if code not in current_code_set]
    recommendations_already_current = [code for code in all_rec_codes if code in current_code_set]
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
            current_term_rows=configured_rows,
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
    recommendations = [
        {
            "code": code,
            "name": course_info.get(code, {}).get("name", ""),
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
            "timetable": timetable,
            "timetable_meetings": timetable_meetings,
            "unscheduled": unscheduled,
            "recommendations": recommendations,
            "recommendations_already_current": recommendations_already_current,
            "home_cards": home_cards,
        },
    )
