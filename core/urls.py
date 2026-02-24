from django.contrib.auth.decorators import login_required
from django.urls import path

from .advisor_views import (
    advisor_upsert_view,
    advisors_list_view,
    assign_students_advisors_view,
    ensure_students_advisor_column_view,
    students_by_advisor_view,
)
from .api_views import classify_view, parse_and_classify_view, recommend_view
from .audit_views import audit_explorer_api, audit_explorer_page, audit_export_csv_view
from .auth_views import login_view, logout_view
from .db_admin_views import (
    db_admin_page,
    db_backup_snapshot_view,
    db_delete_program_catalog_view,
    db_delete_students_view,
    db_import_legacy_exact_view,
    db_import_program_plan_view,
    db_import_term_sections_view,
    db_integrity_report_view,
    db_preview_delete_program_catalog_view,
    db_preview_delete_students_view,
    db_preview_term_sections_view,
)
from .exam_views import (
    exam_timetable_build_view,
    exam_timetable_detail_view,
    exam_timetable_filters_view,
    exam_timetable_list_view,
    exam_timetable_page,
    exam_timetable_preview_courses_view,
)
from .planner_views import (
    planner_build_view,
    planner_context_view,
    planner_page,
    planner_save_student_sections_view,
    planner_sections_catalog_view,
)
from .portfolio_views import advisor_portfolio_page
from .report_views import (
    course_eligibility_view,
    export_aggregate_csv_view,
    export_course_eligibility_csv_view,
    export_missing_high_priority_xlsx_view,
    export_recommendation_debug_csv_view,
    export_student_csv_view,
    export_student_plan_csv_view,
    export_students_by_advisor_csv_view,
    missing_high_priority_view,
    prerequisites_view,
    program_plan_view,
    recommendation_debug_view,
    report_summary_view,
    student_plan_view,
)
from .scrape_views import scrape_start_view, scrape_status_view, scrape_stop_view
from .sections_import_views import (
    sections_import_insert_view,
    sections_import_page,
    sections_import_preview_view,
)
from .settings_views import defaults_settings_view
from .user_admin_views import (
    user_management_page,
    users_create_view,
    users_delete_view,
    users_list_view,
    users_set_active_view,
    users_set_password_view,
    users_update_role_view,
)
from .views import dashboard, dev_role_switch_view, health

urlpatterns = [
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("", login_required(dashboard, login_url="login"), name="dashboard"),
    path("health/", health, name="health"),
    path("ops/dev/switch-role/", dev_role_switch_view, name="dev_role_switch"),
    path("recommend/<int:student_id>/", recommend_view, name="recommend"),
    path("classify/", classify_view, name="classify"),
    path("parse-and-classify/", parse_and_classify_view, name="parse_and_classify"),
    path("report/summary/", report_summary_view, name="report_summary"),
    path("report/student-plan/", student_plan_view, name="student_plan_view"),
    path("export/student-plan.csv", export_student_plan_csv_view, name="export_student_plan_csv"),
    path("report/prerequisites/", prerequisites_view, name="prerequisites_view"),
    path("report/program-plan/", program_plan_view, name="program_plan_view"),
    path(
        "report/recommendation-debug/", recommendation_debug_view, name="recommendation_debug_view"
    ),
    path("report/course-eligibility/", course_eligibility_view, name="course_eligibility_view"),
    path(
        "report/missing-high-priority/",
        missing_high_priority_view,
        name="missing_high_priority_view",
    ),
    path(
        "export/recommendation-debug.csv",
        export_recommendation_debug_csv_view,
        name="export_recommendation_debug_csv",
    ),
    path(
        "export/course-eligibility.csv",
        export_course_eligibility_csv_view,
        name="export_course_eligibility_csv",
    ),
    path(
        "export/missing-high-priority.xlsx",
        export_missing_high_priority_xlsx_view,
        name="export_missing_high_priority_xlsx",
    ),
    path(
        "export/students-by-advisor.csv",
        export_students_by_advisor_csv_view,
        name="export_students_by_advisor_csv",
    ),
    path("export/student.csv", export_student_csv_view, name="export_student_csv"),
    path("export/aggregate.csv", export_aggregate_csv_view, name="export_aggregate_csv"),
    path("report/advisors/", advisors_list_view, name="advisors_list"),
    path("report/students-by-advisor/", students_by_advisor_view, name="students_by_advisor"),
    path("ops/advisors/upsert/", advisor_upsert_view, name="advisor_upsert"),
    path(
        "ops/advisors/ensure-students-column/",
        ensure_students_advisor_column_view,
        name="ensure_students_advisor_column",
    ),
    path(
        "ops/advisors/assign-students/",
        assign_students_advisors_view,
        name="assign_students_advisors",
    ),
    path("ops/scrape/start/", scrape_start_view, name="scrape_start"),
    path("ops/scrape/status/", scrape_status_view, name="scrape_status"),
    path("ops/scrape/stop/", scrape_stop_view, name="scrape_stop"),
    path("db-admin/", db_admin_page, name="db_admin_page"),
    path("ops/sections-import/", sections_import_page, name="sections_import_page"),
    path(
        "ops/sections-import/preview/", sections_import_preview_view, name="sections_import_preview"
    ),
    path("ops/sections-import/insert/", sections_import_insert_view, name="sections_import_insert"),
    path("planner/", planner_page, name="planner_page"),
    path("advisor-portfolio/", advisor_portfolio_page, name="advisor_portfolio_page"),
    path("ops/planner/context/", planner_context_view, name="planner_context"),
    path(
        "ops/planner/save-student-sections/",
        planner_save_student_sections_view,
        name="planner_save_student_sections",
    ),
    path(
        "ops/planner/sections-catalog/",
        planner_sections_catalog_view,
        name="planner_sections_catalog",
    ),
    path("ops/planner/build/", planner_build_view, name="planner_build"),
    path("audit-explorer/", audit_explorer_page, name="audit_explorer_page"),
    path("ops/audit/explorer/", audit_explorer_api, name="audit_explorer_api"),
    path("ops/audit/export.csv", audit_export_csv_view, name="audit_export_csv"),
    path(
        "ops/db/preview-delete-students/",
        db_preview_delete_students_view,
        name="db_preview_delete_students",
    ),
    path("ops/db/delete-students/", db_delete_students_view, name="db_delete_students"),
    path(
        "ops/db/preview-delete-program-catalog/",
        db_preview_delete_program_catalog_view,
        name="db_preview_delete_program_catalog",
    ),
    path(
        "ops/db/delete-program-catalog/",
        db_delete_program_catalog_view,
        name="db_delete_program_catalog",
    ),
    path("ops/db/import-program-plan/", db_import_program_plan_view, name="db_import_program_plan"),
    path("ops/db/import-legacy-exact/", db_import_legacy_exact_view, name="db_import_legacy_exact"),
    path(
        "ops/db/preview-term-sections/",
        db_preview_term_sections_view,
        name="db_preview_term_sections",
    ),
    path(
        "ops/db/import-term-sections/", db_import_term_sections_view, name="db_import_term_sections"
    ),
    path("ops/db/backup-snapshot/", db_backup_snapshot_view, name="db_backup_snapshot"),
    path("ops/db/integrity-report/", db_integrity_report_view, name="db_integrity_report"),
    path("ops/settings/defaults/", defaults_settings_view, name="settings_defaults"),
    path("ops/users/list/", users_list_view, name="users_list"),
    path("user-management/", user_management_page, name="user_management_page"),
    path("ops/users/create/", users_create_view, name="users_create"),
    path("ops/users/update-role/", users_update_role_view, name="users_update_role"),
    path("ops/users/set-password/", users_set_password_view, name="users_set_password"),
    path("ops/users/set-active/", users_set_active_view, name="users_set_active"),
    path("ops/users/delete/", users_delete_view, name="users_delete"),
    path("exam-timetable/", login_required(exam_timetable_page), name="exam_timetable_page"),
    path(
        "ops/exam-timetable/filters/",
        login_required(exam_timetable_filters_view),
        name="exam_timetable_filters",
    ),
    path(
        "ops/exam-timetable/preview-courses/",
        login_required(exam_timetable_preview_courses_view),
        name="exam_timetable_preview_courses",
    ),
    path(
        "ops/exam-timetable/build/",
        login_required(exam_timetable_build_view),
        name="exam_timetable_build",
    ),
    path(
        "ops/exam-timetable/list/",
        login_required(exam_timetable_list_view),
        name="exam_timetable_list",
    ),
    path(
        "ops/exam-timetable/<int:run_id>/",
        login_required(exam_timetable_detail_view),
        name="exam_timetable_detail",
    ),
]
