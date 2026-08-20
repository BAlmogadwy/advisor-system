from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNER_JS = ROOT / "static/js/page-student-planner.js"
GLOBAL_CSS = ROOT / "static/css/global.css"


def test_requested_course_controls_have_a_responsive_layout_wrapper():
    source = PLANNER_JS.read_text(encoding="utf-8")

    assert "sp-requested-controls" in source
    assert "sp-requested-remove" in source
    assert "courseName.dir = 'auto'" in source


def test_requested_course_controls_stack_and_keep_touch_targets_on_phones():
    source = GLOBAL_CSS.read_text(encoding="utf-8")

    assert ".sp-requested-controls {" in source
    assert "flex-wrap: wrap" in source
    assert "min-height: 44px" in source
    assert "html body .btn.sp-requested-remove" in source
    assert (
        ".sp-requested-controls { display: grid; grid-template-columns: minmax(0, 1fr); }" in source
    )
    assert ".sp-requested-remove { width: 100%; justify-content: center; }" in source
