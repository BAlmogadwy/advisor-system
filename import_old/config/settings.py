PORTAL_LOGIN_URL = "https://eas.taibahu.edu.sa/TaibahReg/teachers_login.jsp"
STUDENT_PLAN_URL = "https://eas.taibahu.edu.sa/TaibahReg/studentStudyPlanEnquiryEng.do?ex=preEx"
STUDENT_TIMETABLE_URL = "https://eas.taibahu.edu.sa/TaibahReg/studentSchedualEnquiry.do?ex=preEx"

ADMIN_USERNAME = "322071"
ADMIN_PASSWORD = "Bass1409"

from pathlib import Path as _Path
DB_PATH = str(_Path(__file__).resolve().parents[2] / "db.sqlite3")
STUDENT_LIST_FILE = "data/students_list.csv"
