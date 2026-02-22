# student_advisor_system/database/db_utils.py

import sqlite3
from config.settings import DB_PATH

def get_connection():
    return sqlite3.connect(DB_PATH)


def insert_student(student_data):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO students (
            student_id, registration_no, name, nationality, status,
            gpa, total_registered_credits, total_earned_credits,
            program, section                           -- ✅ NEW
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        student_data["student_id"],
        student_data["registration_no"],
        student_data["name"],
        student_data["nationality"],
        student_data["status"],
        student_data["gpa"],
        student_data["total_registered_credits"],
        student_data["total_earned_credits"],
        student_data["program"],
        student_data["section"]                      # ✅ NEW
    ))
    conn.commit()
    conn.close()



def insert_course(course_data):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO courses (course_code, department, description, credit_hours)
        VALUES (?, ?, ?, ?)
    """, (
        course_data["course_code"],
        course_data["department"],
        course_data["description"],
        course_data["credit_hours"]
    ))
    conn.commit()
    conn.close()

def get_course_id(course_code):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT course_id FROM courses WHERE course_code = ?", (course_code,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def insert_student_course(student_id, course_code, course_data):
    course_id = get_course_id(course_code)
    if course_id is None:
        raise Exception(f"Course {course_code} not found in courses table!")

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO student_courses (student_id, course_id, programme_term, status, grade, mark, actual_term)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        student_id,
        course_id,
        course_data["programme_term"],
        course_data["status"],
        course_data["grade"],
        course_data["mark"],
        course_data["actual_term"]
    ))
    conn.commit()
    conn.close()

def load_prerequisites_as_dict():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c1.course_code, c2.course_code
        FROM prerequisites p
        JOIN courses c1 ON p.course_id = c1.course_id
        JOIN courses c2 ON p.prerequisite_course_id = c2.course_id
    """)
    rows = cursor.fetchall()
    conn.close()

    prerequisites = {}
    for course, prereq in rows:
        prerequisites.setdefault(course, []).append(prereq)
    return prerequisites

def get_all_students():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT student_id FROM students")
    students = [row[0] for row in cursor.fetchall()]
    conn.close()
    return students

def get_student_passed_courses(student_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.course_code FROM student_courses sc
        JOIN courses c ON sc.course_id = c.course_id
        WHERE sc.student_id = ? AND sc.status = 'passed'
    """, (student_id,))
    courses = {row[0] for row in cursor.fetchall()}
    conn.close()
    return courses

def get_student_not_taken_courses(student_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.course_code FROM student_courses sc
        JOIN courses c ON sc.course_id = c.course_id
        WHERE sc.student_id = ? AND sc.status = 'not_taken'
    """, (student_id,))
    courses = [row[0] for row in cursor.fetchall()]
    conn.close()
    return courses

def get_course_programme_term(course_code):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sc.programme_term FROM student_courses sc
        JOIN courses c ON sc.course_id = c.course_id
        WHERE c.course_code = ? LIMIT 1
    """, (course_code,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def insert_programme_requirement(programme_data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO programme_requirements (program, course_code, type, programme_term, credit_hours)
        VALUES (?, ?, ?, ?, ?)
    """, (
        programme_data["program"],                 # ✅ NEW
        programme_data["course_code"],
        programme_data["type"],
        programme_data["programme_term"],
        programme_data["credit_hours"]
    ))
    conn.commit()
    conn.close()

def insert_prerequisite(prerequisite_data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO prerequisites (program, course_code, prerequisite_course_code)
        VALUES (?, ?, ?)
    """, (
        prerequisite_data["program"],              # ✅ NEW
        prerequisite_data["course_code"],
        prerequisite_data["prerequisite_course_code"]
    ))
    conn.commit()
    conn.close()
