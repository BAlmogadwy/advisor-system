from django.contrib.auth.decorators import login_required
from django.urls import path

from .advisor_views import (
    advisor_upsert_view,
    advisors_list_view,
    assign_students_advisors_view,
    ensure_students_advisor_column_view,
    seed_advisors_view,
    students_by_advisor_view,
)
from .api_views import classify_view, parse_and_classify_view, recommend_view
from .audit_views import audit_explorer_api, audit_explorer_page, audit_export_csv_view
from .auth_views import login_view, logout_view
from .db_admin_views import (
    db_admin_page,
    db_backup_snapshot_view,
    db_delete_external_courses_view,
    db_delete_program_catalog_view,
    db_delete_students_view,
    db_import_legacy_exact_view,
    db_import_oracle_plan_view,
    db_import_program_plan_view,
    db_import_term_sections_view,
    db_integrity_report_view,
    db_list_external_courses_view,
    db_preview_delete_program_catalog_view,
    db_preview_delete_students_view,
    db_preview_oracle_plan_view,
    db_preview_term_sections_view,
    db_programme_capacities_view,
    db_update_programme_capacities_view,
    elective_catalogue_import_view,
    elective_catalogue_list_view,
    elective_mapping_list_view,
    elective_mapping_set_view,
    elective_placeholders_view,
)
from .exam_views import (
    exam_timetable_build_view,
    exam_timetable_delete_view,
    exam_timetable_detail_view,
    exam_timetable_export_view,
    exam_timetable_filters_view,
    exam_timetable_list_view,
    exam_timetable_page,
    exam_timetable_preview_courses_view,
)
from .planner_job_views import (
    planner_job_cancel,
    planner_job_poll,
    planner_job_result,
    planner_job_submit,
)
from .planner_views import (
    planner_build_view,
    planner_context_view,
    planner_page,
    planner_save_student_sections_view,
    planner_sections_catalog_view,
)
from .portfolio_views import advisor_portfolio_page
from .profile_views import (
    profile_change_password_view,
    profile_change_username_view,
    profile_me_view,
    profile_page,
)
from .report_views import (
    conflict_matrix_view,
    course_eligibility_view,
    export_aggregate_csv_view,
    export_aggregate_xlsx_view,
    export_conflict_matrix_xlsx_view,
    export_course_eligibility_csv_view,
    export_missing_high_priority_xlsx_view,
    export_prerequisites_xlsx_view,
    export_recommendation_debug_csv_view,
    export_recommendation_debug_xlsx_view,
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
from .scrape_views import (
    oracle_students_csv_view,
    scrape_start_view,
    scrape_status_view,
    scrape_stop_view,
)
from .section_plan_views import (
    section_plan_courses_view,
    section_plan_export_view,
    section_plan_generate_view,
    section_plan_page,
    section_plan_save_capacity_view,
    section_plan_save_overrides_bulk_view,
)
from .sections_import_views import (
    sections_import_insert_view,
    sections_import_page,
    sections_import_preview_view,
)
from .settings_views import defaults_settings_view
from .timetable_workspace_views import (
    timetable_workspace_page,
    timetable_workspace_split_page,
    tw_board_capacity_view,
    tw_board_conflicts_view,
    tw_board_create_view,
    tw_board_detail_view,
    tw_board_summary_view,
    tw_board_unplaced_view,
    tw_boards_list_view,
    tw_generate_workspace_view,
    tw_optimise_v2_view,
    tw_placement_create_planned_view,
    tw_placement_create_view,
    tw_placement_lock_view,
    tw_placement_move_view,
    tw_placement_remove_view,
    tw_scenario_budget_view,
    tw_scenario_create_view,
    tw_scenario_detail_view,
    tw_scenario_export_view,
    tw_scenario_publish_view,
    tw_scenario_slots_update_view,
    tw_scenarios_list_view,
    tw_slot_template_create_view,
    tw_slot_templates_list_view,
)
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
    path("recommend/<int:student_id>/", recommend_view, name="recommend"),
    path("classify/", classify_view, name="classify"),
    path("parse-and-classify/", parse_and_classify_view, name="parse_and_classify"),
    path("report/summary/", report_summary_view, name="report_summary"),
    path("report/student-plan/", student_plan_view, name="student_plan_view"),
    path("export/student-plan.csv", export_student_plan_csv_view, name="export_student_plan_csv"),
    path("report/prerequisites/", prerequisites_view, name="prerequisites_view"),
    path(
        "export/prerequisites.xlsx",
        export_prerequisites_xlsx_view,
        name="export_prerequisites_xlsx",
    ),
    path("report/program-plan/", program_plan_view, name="program_plan_view"),
    path(
        "report/recommendation-debug/", recommendation_debug_view, name="recommendation_debug_view"
    ),
    path("report/conflict-matrix/", conflict_matrix_view, name="conflict_matrix_view"),
    path(
        "export/conflict-matrix.xlsx",
        export_conflict_matrix_xlsx_view,
        name="export_conflict_matrix_xlsx",
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
        "export/recommendation-debug.xlsx",
        export_recommendation_debug_xlsx_view,
        name="export_recommendation_debug_xlsx",
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
    path("export/aggregate.xlsx", export_aggregate_xlsx_view, name="export_aggregate_xlsx"),
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
    path("ops/advisors/seed/", seed_advisors_view, name="seed_advisors"),
    # Elective catalogue & mapping
    path("ops/electives/catalogue/", elective_catalogue_list_view, name="elective_catalogue_list"),
    path(
        "ops/electives/catalogue/import/",
        elective_catalogue_import_view,
        name="elective_catalogue_import",
    ),
    path("ops/electives/mapping/", elective_mapping_list_view, name="elective_mapping_list"),
    path("ops/electives/mapping/set/", elective_mapping_set_view, name="elective_mapping_set"),
    path("ops/electives/placeholders/", elective_placeholders_view, name="elective_placeholders"),
    path("ops/scrape/start/", scrape_start_view, name="scrape_start"),
    path("ops/scrape/status/", scrape_status_view, name="scrape_status"),
    path("ops/scrape/stop/", scrape_stop_view, name="scrape_stop"),
    path("ops/scrape/oracle-students-csv/", oracle_students_csv_view, name="oracle_students_csv"),
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
    path("section-planning/", section_plan_page, name="section_plan_page"),
    path(
        "ops/section-planning/generate/",
        section_plan_generate_view,
        name="section_plan_generate",
    ),
    path(
        "ops/section-planning/export/",
        section_plan_export_view,
        name="section_plan_export",
    ),
    path(
        "ops/section-planning/courses/",
        section_plan_courses_view,
        name="section_plan_courses",
    ),
    path(
        "ops/section-planning/save-capacity/",
        section_plan_save_capacity_view,
        name="section_plan_save_capacity",
    ),
    path(
        "ops/section-planning/save-overrides-bulk/",
        section_plan_save_overrides_bulk_view,
        name="section_plan_save_overrides_bulk",
    ),
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
        "ops/db/preview-oracle-plan/",
        db_preview_oracle_plan_view,
        name="db_preview_oracle_plan",
    ),
    path(
        "ops/db/import-oracle-plan/",
        db_import_oracle_plan_view,
        name="db_import_oracle_plan",
    ),
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
    path(
        "ops/db/external-courses/", db_list_external_courses_view, name="db_list_external_courses"
    ),
    path(
        "ops/db/delete-external-courses/",
        db_delete_external_courses_view,
        name="db_delete_external_courses",
    ),
    path(
        "ops/db/programme-capacities/",
        db_programme_capacities_view,
        name="db_programme_capacities",
    ),
    path(
        "ops/db/update-programme-capacities/",
        db_update_programme_capacities_view,
        name="db_update_programme_capacities",
    ),
    path("ops/settings/defaults/", defaults_settings_view, name="settings_defaults"),
    path("ops/users/list/", users_list_view, name="users_list"),
    path("user-management/", user_management_page, name="user_management_page"),
    path("ops/users/create/", users_create_view, name="users_create"),
    path("ops/users/update-role/", users_update_role_view, name="users_update_role"),
    path("ops/users/set-password/", users_set_password_view, name="users_set_password"),
    path("ops/users/set-active/", users_set_active_view, name="users_set_active"),
    path("ops/users/delete/", users_delete_view, name="users_delete"),
    path("profile/", profile_page, name="profile_page"),
    path("ops/profile/me/", profile_me_view, name="profile_me"),
    path(
        "ops/profile/change-username/", profile_change_username_view, name="profile_change_username"
    ),
    path(
        "ops/profile/change-password/", profile_change_password_view, name="profile_change_password"
    ),
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
        "ops/exam-timetable/<int:run_id>/export.xlsx",
        login_required(exam_timetable_export_view),
        name="exam_timetable_export",
    ),
    path(
        "ops/exam-timetable/<int:run_id>/delete/",
        login_required(exam_timetable_delete_view),
        name="exam_timetable_delete",
    ),
    path(
        "ops/exam-timetable/<int:run_id>/",
        login_required(exam_timetable_detail_view),
        name="exam_timetable_detail",
    ),
    # ── Timetable Workspace ──
    path("timetable-workspace/", timetable_workspace_page, name="timetable_workspace_page"),
    path(
        "timetable-workspace/split/",
        timetable_workspace_split_page,
        name="timetable_workspace_split_page",
    ),
    path("ops/tw/generate-workspace/", tw_generate_workspace_view, name="tw_generate_workspace"),
    path(
        "ops/tw/scenarios/<int:scenario_id>/budget/",
        tw_scenario_budget_view,
        name="tw_scenario_budget",
    ),
    path(
        "ops/tw/scenarios/<int:scenario_id>/export.xlsx",
        tw_scenario_export_view,
        name="tw_scenario_export",
    ),
    path("ops/tw/scenarios/", tw_scenarios_list_view, name="tw_scenarios_list"),
    path("ops/tw/scenarios/create/", tw_scenario_create_view, name="tw_scenario_create"),
    path("ops/tw/scenarios/<int:scenario_id>/", tw_scenario_detail_view, name="tw_scenario_detail"),
    path(
        "ops/tw/scenarios/<int:scenario_id>/slots/update/",
        tw_scenario_slots_update_view,
        name="tw_scenario_slots_update",
    ),
    path(
        "ops/tw/scenarios/<int:scenario_id>/publish/",
        tw_scenario_publish_view,
        name="tw_scenario_publish",
    ),
    path("ops/tw/boards/", tw_boards_list_view, name="tw_boards_list"),
    path("ops/tw/boards/create/", tw_board_create_view, name="tw_board_create"),
    path("ops/tw/boards/<int:board_id>/", tw_board_detail_view, name="tw_board_detail"),
    path("ops/tw/boards/<int:board_id>/summary/", tw_board_summary_view, name="tw_board_summary"),
    path(
        "ops/tw/boards/<int:board_id>/conflicts/",
        tw_board_conflicts_view,
        name="tw_board_conflicts",
    ),
    path(
        "ops/tw/boards/<int:board_id>/capacity/",
        tw_board_capacity_view,
        name="tw_board_capacity",
    ),
    path(
        "ops/tw/boards/<int:board_id>/unplaced/",
        tw_board_unplaced_view,
        name="tw_board_unplaced",
    ),
    path("ops/tw/placements/create/", tw_placement_create_view, name="tw_placement_create"),
    path(
        "ops/tw/placements/create-planned/",
        tw_placement_create_planned_view,
        name="tw_placement_create_planned",
    ),
    path(
        "ops/tw/placements/<int:placement_id>/move/",
        tw_placement_move_view,
        name="tw_placement_move",
    ),
    path(
        "ops/tw/placements/<int:placement_id>/remove/",
        tw_placement_remove_view,
        name="tw_placement_remove",
    ),
    path(
        "ops/tw/placements/<int:placement_id>/lock/",
        tw_placement_lock_view,
        name="tw_placement_lock",
    ),
    path("ops/tw/slot-templates/", tw_slot_templates_list_view, name="tw_slot_templates_list"),
    path(
        "ops/tw/slot-templates/create/",
        tw_slot_template_create_view,
        name="tw_slot_template_create",
    ),
    path(
        "ops/tw/scenarios/<int:scenario_id>/optimise-v2/",
        tw_optimise_v2_view,
        name="tw_optimise_v2",
    ),
    # Dev role switch — guarded inside the view itself (requires DEBUG + env var)
    path("ops/dev/switch-role/", dev_role_switch_view, name="dev_role_switch"),
    # PR7 async planner endpoints (flag-gated inside the views)
    path("planner-jobs/", planner_job_submit, name="planner_job_submit"),
    path("planner-jobs/<uuid:job_id>/", planner_job_poll, name="planner_job_poll"),
    path(
        "planner-jobs/<uuid:job_id>/result/",
        planner_job_result,
        name="planner_job_result",
    ),
    path(
        "planner-jobs/<uuid:job_id>/cancel/",
        planner_job_cancel,
        name="planner_job_cancel",
    ),
]
