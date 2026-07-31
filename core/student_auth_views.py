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

from core.models import Course, Student
from core.report_views import _build_student_plan_payload
from core.services.rbac import ROLE_STUDENT, get_user_scope
from core.services.recommender import eligible_next_term_courses, recommend_next_courses
from core.services.student_otp import OTPError, issue_otp, provision_student_user, verify_otp
from core.services.student_sections import get_student_term_baseline, student_gender
from core.services.student_unlock import build_unlock_report
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

    # The builders are shared with the advisor screens and can raise on incomplete
    # curriculum data; a student's only page must degrade, never 500.
    try:
        plan, _err = _build_student_plan_payload(student_id)
    except Exception:  # noqa: BLE001
        logger.exception("student plan build failed for %s", student_id)
        plan = None

    # Only the configured term is shown. There is deliberately NO fallback to an
    # older term: TermSection carries no year/term, so its meetings are always the
    # latest import — a past term would be rendered with today's times.
    try:
        rows = get_student_term_baseline(student_id, str(year), str(term))
    except Exception:  # noqa: BLE001
        logger.exception("student timetable load failed for %s", student_id)
        rows = []
    # Defence in depth: the institution segregates sections by gender, so never render
    # a section of the other cohort even if a mapping row exists. Mirrors the ORM rule
    # in services.student_sections.gender_section_filter: own-gender sections PLUS any
    # ungendered one; a blank/unknown gender keeps everything (never hide all).
    gender = student_gender(student_id)
    if gender:
        rows = [
            r
            for r in rows
            if not str(r.get("section") or "").upper().startswith(("M", "F"))
            or str(r.get("section") or "").upper().startswith(gender)
        ]
    timetable, unscheduled = _weekly_timetable(rows)

    try:
        rec_codes = recommend_next_courses(student_id, year, term)
    except Exception:  # noqa: BLE001
        logger.exception("student recommendations failed for %s", student_id)
        rec_codes = []
    # What the student may actually register in THIS term (same rule as the
    # recommendation list, without the credit-load trim) — not the whole-plan count.
    try:
        eligible_now = len(eligible_next_term_courses(student_id, year, term))
    except Exception:  # noqa: BLE001
        logger.exception("student eligibility count failed for %s", student_id)
        eligible_now = None
    names = dict(
        Course.objects.filter(course_code__in=rec_codes).values_list("course_code", "description")
    )
    recommendations = [{"code": c, "name": names.get(c, "")} for c in rec_codes]

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
            "plan": plan,
            "timetable": timetable,
            "unscheduled": unscheduled,
            "recommendations": recommendations,
            "eligible_now": eligible_now,
        },
    )
