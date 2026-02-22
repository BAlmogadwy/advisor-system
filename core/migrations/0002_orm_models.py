import sqlite3
from pathlib import Path

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _import_legacy_data(apps, schema_editor):
    """Import data from the legacy advisor.db into the new ORM tables."""
    db_path_str = getattr(settings, "DB_PATH", "")
    if not db_path_str:
        return
    db_path = Path(db_path_str)
    if not db_path.exists():
        return

    Student = apps.get_model("core", "Student")
    if Student.objects.exists():
        return  # already imported

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    def _table_exists(table_name):
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None

    # --- students ---
    if _table_exists("students"):
        rows = conn.execute("SELECT * FROM students").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(Student(
                student_id=r["student_id"],
                registration_no=str(r["registration_no"] or "") if "registration_no" in keys else "",
                name=str(r["name"] or "") if "name" in keys else "",
                nationality=str(r["nationality"] or "") if "nationality" in keys else "",
                status=str(r["status"] or "") if "status" in keys else "",
                gpa=float(r["gpa"]) if "gpa" in keys and r["gpa"] is not None else None,
                total_registered_credits=int(r["total_registered_credits"]) if "total_registered_credits" in keys and r["total_registered_credits"] is not None else 0,
                total_earned_credits=int(r["total_earned_credits"]) if "total_earned_credits" in keys and r["total_earned_credits"] is not None else 0,
                program=str(r["program"]) if "program" in keys and r["program"] is not None else None,
                section=str(r["section"] or "") if "section" in keys else "",
                advisor_id=str(r["advisor_id"] or "") if "advisor_id" in keys else "",
            ))
        Student.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- courses ---
    Course = apps.get_model("core", "Course")
    if _table_exists("courses"):
        rows = conn.execute("SELECT * FROM courses").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(Course(
                course_id=r["course_id"],
                course_code=str(r["course_code"] or ""),
                department=str(r["department"] or "") if "department" in keys else "",
                description=str(r["description"] or "") if "description" in keys else "",
                credit_hours=int(r["credit_hours"]) if "credit_hours" in keys and r["credit_hours"] is not None else 0,
            ))
        Course.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- academic_advisors ---
    AcademicAdvisor = apps.get_model("core", "AcademicAdvisor")
    if _table_exists("academic_advisors"):
        rows = conn.execute("SELECT * FROM academic_advisors").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(AcademicAdvisor(
                advisor_id=str(r["advisor_id"]),
                full_name=str(r["full_name"] or ""),
                email=str(r["email"] or ""),
                department=str(r["department"] or ""),
                created_at=str(r["created_at"] or "") if "created_at" in keys else "",
            ))
        AcademicAdvisor.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- programme_requirements ---
    ProgrammeRequirement = apps.get_model("core", "ProgrammeRequirement")
    if _table_exists("programme_requirements"):
        rows = conn.execute("SELECT * FROM programme_requirements").fetchall()
        objs = []
        for r in rows:
            objs.append(ProgrammeRequirement(
                program=str(r["program"] or ""),
                course_code=str(r["course_code"] or ""),
                type=str(r["type"] or "") if "type" in r.keys() else "",
                programme_term=int(r["programme_term"]) if r["programme_term"] is not None else None,
                credit_hours=int(r["credit_hours"]) if r["credit_hours"] is not None else None,
            ))
        ProgrammeRequirement.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- prerequisites ---
    Prerequisite = apps.get_model("core", "Prerequisite")
    if _table_exists("prerequisites"):
        rows = conn.execute("SELECT * FROM prerequisites").fetchall()
        objs = []
        for r in rows:
            objs.append(Prerequisite(
                id=r["id"],
                program=str(r["program"] or ""),
                course_code=str(r["course_code"] or ""),
                prerequisite_course_code=str(r["prerequisite_course_code"] or ""),
            ))
        Prerequisite.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- student_courses ---
    StudentCourse = apps.get_model("core", "StudentCourse")
    if _table_exists("student_courses"):
        rows = conn.execute("SELECT * FROM student_courses").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(StudentCourse(
                id=r["id"],
                student_id=r["student_id"],
                course_id=r["course_id"],
                programme_term=int(r["programme_term"]) if "programme_term" in keys and r["programme_term"] is not None else None,
                status=str(r["status"] or "") if "status" in keys else "",
                grade=str(r["grade"] or "") if "grade" in keys else "",
                mark=float(r["mark"]) if "mark" in keys and r["mark"] is not None else None,
                actual_term=str(r["actual_term"] or "") if "actual_term" in keys else "",
            ))
        StudentCourse.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- term_sections ---
    TermSection = apps.get_model("core", "TermSection")
    if _table_exists("term_sections"):
        rows = conn.execute("SELECT * FROM term_sections").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(TermSection(
                id=r["id"],
                source_tag=str(r["source_tag"] or "other") if "source_tag" in keys else "other",
                course_name=str(r["course_name"] or "") if "course_name" in keys else "",
                available_capacity=int(r["available_capacity"]) if "available_capacity" in keys and r["available_capacity"] is not None else None,
                registered_count=int(r["registered_count"]) if "registered_count" in keys and r["registered_count"] is not None else None,
                course_code=str(r["course_code"] or ""),
                course_number=str(r["course_number"] or ""),
                course_key=str(r["course_key"] or "") if "course_key" in keys else "",
                section=str(r["section"] or ""),
                source_file=str(r["source_file"] or "") if "source_file" in keys else "",
                created_at=str(r["created_at"] or "") if "created_at" in keys else "",
                updated_at=str(r["updated_at"] or "") if "updated_at" in keys else "",
            ))
        TermSection.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- term_section_meetings ---
    TermSectionMeeting = apps.get_model("core", "TermSectionMeeting")
    if _table_exists("term_section_meetings"):
        rows = conn.execute("SELECT * FROM term_section_meetings").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(TermSectionMeeting(
                id=r["id"],
                term_section_id=r["term_section_id"],
                day=str(r["day"] or ""),
                start_time=str(r["start_time"] or ""),
                end_time=str(r["end_time"] or ""),
                building=str(r["building"] or "") if "building" in keys else "",
                floor_wing=str(r["floor_wing"] or "") if "floor_wing" in keys else "",
                room=str(r["room"] or "") if "room" in keys else "",
                instructor=str(r["instructor"] or "") if "instructor" in keys else "",
                created_at=str(r["created_at"] or "") if "created_at" in keys else "",
                updated_at=str(r["updated_at"] or "") if "updated_at" in keys else "",
            ))
        TermSectionMeeting.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    # --- student_term_sections ---
    StudentTermSection = apps.get_model("core", "StudentTermSection")
    if _table_exists("student_term_sections"):
        rows = conn.execute("SELECT * FROM student_term_sections").fetchall()
        objs = []
        for r in rows:
            keys = r.keys()
            objs.append(StudentTermSection(
                id=r["id"],
                student_id=r["student_id"],
                academic_year=str(r["academic_year"] or ""),
                term=str(r["term"] or ""),
                term_section_id=r["term_section_id"],
                source=str(r["source"] or "manual") if "source" in keys else "manual",
                created_at=str(r["created_at"] or "") if "created_at" in keys else "",
                updated_at=str(r["updated_at"] or "") if "updated_at" in keys else "",
            ))
        StudentTermSection.objects.bulk_create(objs, batch_size=1000, ignore_conflicts=True)

    conn.close()


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("core", "0001_core_scope_and_audit"),
    ]

    operations = [
        # --- New tables (academic data from advisor.db) ---
        migrations.CreateModel(
            name="AcademicAdvisor",
            fields=[
                ("advisor_id", models.TextField(primary_key=True, serialize=False)),
                ("full_name", models.TextField()),
                ("email", models.TextField(unique=True)),
                ("department", models.TextField()),
                ("created_at", models.TextField(blank=True, default="")),
            ],
            options={"db_table": "academic_advisors"},
        ),
        migrations.CreateModel(
            name="Course",
            fields=[
                ("course_id", models.AutoField(primary_key=True, serialize=False)),
                ("course_code", models.TextField(unique=True)),
                ("department", models.TextField(blank=True, default="")),
                ("description", models.TextField(blank=True, default="")),
                ("credit_hours", models.IntegerField(default=0, null=True)),
            ],
            options={"db_table": "courses"},
        ),
        migrations.CreateModel(
            name="Student",
            fields=[
                ("student_id", models.IntegerField(primary_key=True, serialize=False)),
                ("registration_no", models.TextField(blank=True, default="")),
                ("name", models.TextField(blank=True, default="")),
                ("nationality", models.TextField(blank=True, default="")),
                ("status", models.TextField(blank=True, default="")),
                ("gpa", models.FloatField(blank=True, null=True)),
                ("total_registered_credits", models.IntegerField(default=0, null=True)),
                ("total_earned_credits", models.IntegerField(default=0, null=True)),
                ("program", models.TextField(blank=True, null=True)),
                ("section", models.TextField(blank=True, default="")),
                ("advisor_id", models.TextField(blank=True, default="")),
            ],
            options={
                "db_table": "students",
                "indexes": [
                    models.Index(fields=["program"], name="idx_students_program"),
                    models.Index(fields=["advisor_id"], name="idx_students_advisor_id"),
                    models.Index(fields=["section"], name="idx_students_section"),
                ],
            },
        ),
        migrations.CreateModel(
            name="Prerequisite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("program", models.TextField()),
                ("course_code", models.TextField()),
                ("prerequisite_course_code", models.TextField()),
            ],
            options={
                "db_table": "prerequisites",
                "indexes": [
                    models.Index(fields=["program", "course_code"], name="idx_prereq_program_code"),
                    models.Index(fields=["prerequisite_course_code", "program"], name="idx_prereq_prereq_program"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProgrammeRequirement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("program", models.TextField()),
                ("course_code", models.TextField()),
                ("type", models.TextField(blank=True, default="")),
                ("programme_term", models.IntegerField(blank=True, null=True)),
                ("credit_hours", models.IntegerField(blank=True, null=True)),
            ],
            options={
                "db_table": "programme_requirements",
                "indexes": [models.Index(fields=["program"], name="idx_pr_program")],
                "constraints": [models.UniqueConstraint(fields=("program", "course_code"), name="uq_programme_requirements_program_code")],
            },
        ),
        migrations.CreateModel(
            name="StudentCourse",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("programme_term", models.IntegerField(blank=True, null=True)),
                ("status", models.TextField(blank=True, default="")),
                ("grade", models.TextField(blank=True, default="")),
                ("mark", models.FloatField(blank=True, null=True)),
                ("actual_term", models.TextField(blank=True, default="")),
                ("course", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="student_courses", to="core.course")),
                ("student", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="student_courses", to="core.student")),
            ],
            options={"db_table": "student_courses"},
        ),
        migrations.CreateModel(
            name="TermSection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_tag", models.TextField(default="other")),
                ("course_name", models.TextField(blank=True, default="")),
                ("available_capacity", models.IntegerField(blank=True, null=True)),
                ("registered_count", models.IntegerField(blank=True, null=True)),
                ("course_code", models.TextField()),
                ("course_number", models.TextField()),
                ("course_key", models.TextField()),
                ("section", models.TextField()),
                ("source_file", models.TextField(blank=True, default="")),
                ("created_at", models.TextField(blank=True, default="")),
                ("updated_at", models.TextField(blank=True, default="")),
            ],
            options={
                "db_table": "term_sections",
                "indexes": [models.Index(fields=["course_key"], name="idx_ts_course_key")],
                "constraints": [models.UniqueConstraint(fields=("course_key", "section"), name="ux_term_sections_unique")],
            },
        ),
        migrations.CreateModel(
            name="TermSectionMeeting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", models.TextField()),
                ("start_time", models.TextField()),
                ("end_time", models.TextField()),
                ("building", models.TextField(blank=True, default="")),
                ("floor_wing", models.TextField(blank=True, default="")),
                ("room", models.TextField(blank=True, default="")),
                ("instructor", models.TextField(blank=True, default="")),
                ("created_at", models.TextField(blank=True, default="")),
                ("updated_at", models.TextField(blank=True, default="")),
                ("term_section", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="meetings", to="core.termsection")),
            ],
            options={
                "db_table": "term_section_meetings",
                "constraints": [models.UniqueConstraint(fields=("term_section", "day", "start_time", "end_time", "room", "instructor"), name="ux_term_section_meetings_unique")],
            },
        ),
        migrations.CreateModel(
            name="StudentTermSection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("student_id", models.IntegerField()),
                ("academic_year", models.TextField()),
                ("term", models.TextField()),
                ("source", models.TextField(default="manual")),
                ("created_at", models.TextField(blank=True, default="")),
                ("updated_at", models.TextField(blank=True, default="")),
                ("term_section", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="student_sections", to="core.termsection")),
            ],
            options={
                "db_table": "student_term_sections",
                "indexes": [models.Index(fields=["student_id"], name="ix_sts_student")],
                "constraints": [models.UniqueConstraint(fields=("student_id", "term_section"), name="ux_student_term_sections_unique")],
            },
        ),
        # --- Existing tables: adopt into ORM without touching schema ---
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="UserScope",
                    fields=[
                        ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, serialize=False, to=settings.AUTH_USER_MODEL)),
                        ("advisor_id", models.TextField(blank=True, default="")),
                        ("departments", models.TextField(blank=True, default="")),
                        ("updated_at", models.TextField(blank=True, default="")),
                    ],
                    options={"db_table": "core_user_scope"},
                ),
                migrations.CreateModel(
                    name="AuditLog",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("ts_utc", models.TextField()),
                        ("actor_username", models.TextField(blank=True, default="")),
                        ("actor_role", models.TextField(blank=True, default="")),
                        ("action", models.TextField()),
                        ("endpoint", models.TextField(blank=True, default="")),
                        ("method", models.TextField(blank=True, default="")),
                        ("status", models.TextField(blank=True, default="")),
                        ("details_json", models.TextField(blank=True, default="{}")),
                        ("error_text", models.TextField(blank=True, default="")),
                        ("prev_hash", models.TextField(blank=True, default="")),
                        ("entry_hash", models.TextField(blank=True, default="")),
                    ],
                    options={
                        "db_table": "core_audit_log",
                        "indexes": [
                            models.Index(fields=["action"], name="idx_audit_action"),
                            models.Index(fields=["actor_username"], name="idx_audit_actor"),
                            models.Index(fields=["ts_utc"], name="idx_audit_ts"),
                        ],
                    },
                ),
            ],
            database_operations=[],
        ),
        # --- Data migration ---
        migrations.RunPython(_import_legacy_data, migrations.RunPython.noop),
    ]
