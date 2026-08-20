from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "core" / "templates" / "core"
JS_DIR = ROOT / "static" / "js"

STUDENT_COPY_FILES = [
    *sorted(TEMPLATE_DIR.glob("student*.html")),
    TEMPLATE_DIR / "profile.html",
    TEMPLATE_DIR / "partials" / "sidebar.html",
    *sorted(JS_DIR.glob("page-student-*.js")),
    JS_DIR / "student-timetable.js",
    JS_DIR / "prereq-graph.js",
]


def _student_copy() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in STUDENT_COPY_FILES)


def test_student_portal_uses_one_timetable_glossary() -> None:
    copy = _student_copy()

    for required in (
        "الجدول المسجّل فعليًا",
        "الجدول المتوقع",
        "الجدول المقترح",
    ):
        assert required in copy

    for misleading in (
        "تسجيلك الحقيقي",
        "جدولك الحالي",
        "شُعبي الحالية",
        "خيارات الجدول",
        "مخطط الجدول",
        "طريقة بناء الجدول",
        "أي شعبة مناسبة",
    ):
        assert misleading not in copy


def test_default_student_copy_is_formal_not_colloquial_or_literal() -> None:
    copy = _student_copy()

    for superseded in (
        "كيف أقدر أساعدك اليوم؟",
        "وش المواد",
        "أبغى جدول",
        "ما راح",
        "ابنِ الخيارات",
        "دفع المرشحات",
        "محطة بناء الجدول",
        "توأم الجدول الرسومي",
        "الحد الأدنى الإضافي",
    ):
        assert superseded not in copy
