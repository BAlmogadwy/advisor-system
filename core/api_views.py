import json

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from core.authz import throttle
from core.services.course_classifier import classify_courses
from core.services.policy import require_student_scope
from core.services.recommender import recommend_next_courses
from core.services.student_parser import parse_study_plan, parse_timetable


def _parse_int(value: str | None, field: str) -> tuple[int | None, JsonResponse | None]:
    if value is None:
        return None, JsonResponse(
            {"error": f"Missing required query parameter: {field}"}, status=400
        )
    try:
        return int(value), None
    except ValueError:
        return None, JsonResponse({"error": f"Invalid integer for {field}: {value}"}, status=400)


@login_required(login_url="login")
@require_GET
@throttle(max_calls=20, window_seconds=60)
def recommend_view(request: HttpRequest, student_id: int) -> JsonResponse:
    scope_err = require_student_scope(request, student_id)
    if scope_err:
        return scope_err

    year, err = _parse_int(request.GET.get("year"), "year")
    if err:
        return err

    semester, err = _parse_int(request.GET.get("semester"), "semester")
    if err:
        return err

    if year is None or semester is None:
        return JsonResponse({"error": "Invalid parameters"}, status=400)

    recommendations = recommend_next_courses(
        student_id=student_id,
        current_academic_year=year,
        current_semester=semester,
    )

    return JsonResponse(
        {
            "student_id": student_id,
            "current_academic_year": year,
            "current_semester": semester,
            "recommendations": recommendations,
            "count": len(recommendations),
        }
    )


@login_required(login_url="login")
@require_POST
@throttle(max_calls=15, window_seconds=60)
def classify_view(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    study_plan = payload.get("study_plan")
    timetable = payload.get("timetable")

    if not isinstance(study_plan, list):
        return JsonResponse({"error": "study_plan must be a list"}, status=400)
    if not isinstance(timetable, list):
        return JsonResponse({"error": "timetable must be a list"}, status=400)

    result = classify_courses(study_plan, set(str(x) for x in timetable))
    return JsonResponse(result)


@login_required(login_url="login")
@require_POST
@throttle(max_calls=10, window_seconds=60)
def parse_and_classify_view(request: HttpRequest) -> JsonResponse:
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    study_html = payload.get("study_plan_html")
    timetable_html = payload.get("timetable_html")

    if not isinstance(study_html, str) or not isinstance(timetable_html, str):
        return JsonResponse(
            {"error": "study_plan_html and timetable_html must both be strings"},
            status=400,
        )

    study_plan = parse_study_plan(study_html)
    timetable = parse_timetable(timetable_html, verbose=False)
    result = classify_courses(study_plan, timetable)
    return JsonResponse(
        {
            "study_plan_count": len(study_plan),
            "timetable_count": len(timetable),
            "classification": result,
        }
    )
