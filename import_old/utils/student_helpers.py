
import sqlite3
from config.settings import DB_PATH

def normalize_code(code):
    return str(code).replace(" ", "").upper()

def get_student_program(student_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT program FROM students WHERE student_id = ?", (student_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def get_all_programs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT program FROM students WHERE program IS NOT NULL")
    programs = [row[0] for row in cursor.fetchall()]
    conn.close()
    return programs

def course_exists_in_program(course_code, program):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1 FROM programme_requirements
        WHERE course_code = ? AND program = ?
        LIMIT 1
    """, (course_code, program))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def get_prerequisites(course_code, program):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT prerequisite_course_code FROM prerequisites
        WHERE course_code = ? AND program = ?
    """, (course_code, program))
    rows = cursor.fetchall()
    conn.close()

    prereqs = []
    for row in rows:
        codes = row[0].split(",")
        for code in codes:
            prereqs.append(normalize_code(code))
    return prereqs

def get_student_passed_and_studying(student_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.course_code, sc.status
        FROM student_courses sc
        JOIN courses c ON sc.course_id = c.course_id
        WHERE sc.student_id = ?
    """, (student_id,))
    records = cursor.fetchall()
    conn.close()

    passed = set()
    studying = set()
    for code, status in records:
        code = normalize_code(code)
        if status == 'passed':
            passed.add(code)
        elif status == 'studying':
            studying.add(code)
    return passed, studying

def get_all_filtered_students(section=None, program=None, join_year_prefixes=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = "SELECT student_id FROM students WHERE 1=1"
    params = []

    if section:
        query += " AND section = ?"
        params.append(section)
    if program:
        query += " AND program = ?"
        params.append(program)
    if join_year_prefixes:
        if isinstance(join_year_prefixes, str):
            join_year_prefixes = [join_year_prefixes]
        clauses = []
        for prefix in join_year_prefixes:
            clauses.append("CAST(student_id AS TEXT) LIKE ?")
            params.append(f"{prefix}%")
        query += f" AND ({' OR '.join(clauses)})"

    cursor.execute(query, tuple(params))
    students = [row[0] for row in cursor.fetchall()]
    conn.close()
    return students

def count_eligible_students(course_code, section=None, program=None, verbose=False, join_year_prefixes=None):
    course_code = normalize_code(course_code)
    programs_to_check = [program] if program else get_all_programs()
    total_eligible = []
    total_students = 0

    for prog in programs_to_check:
        if not course_exists_in_program(course_code, prog):
            if verbose:
                print(f"⚠️ Course {course_code} not found in {prog} plan. Skipping.")
            continue

        students = get_all_filtered_students(section, prog, join_year_prefixes)
        eligible = []

        for student_id in students:
            passed, studying = get_student_passed_and_studying(student_id)
            if course_code in passed or course_code in studying:
                continue
            prereqs = get_prerequisites(course_code, prog)
            if all(p in passed or p in studying for p in prereqs):
                eligible.append(student_id)

        total_students += len(students)
        total_eligible.extend(eligible)

        if verbose:
            print(f"\n📘 Program: {prog} → {len(eligible)} of {len(students)} eligible")
            if eligible:
                print("Eligible student IDs:", ", ".join(str(sid) for sid in eligible))

    if verbose:
        print(f"\n✅ Total eligible: {len(total_eligible)} out of {total_students} students for {course_code}")
    return total_eligible
