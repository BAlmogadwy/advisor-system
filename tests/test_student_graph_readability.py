from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAPH_JS = ROOT / "static" / "js" / "prereq-graph.js"
STUDENT_GRAPH_JS = ROOT / "static" / "js" / "page-student-graph.js"
STUDENT_GRADUATION_JS = ROOT / "static" / "js" / "page-student-graduation.js"
GLOBAL_CSS = ROOT / "static" / "css" / "global.css"


def test_desktop_graph_keeps_course_codes_at_a_readable_scale() -> None:
    renderer = GRAPH_JS.read_text(encoding="utf-8")
    styles = GLOBAL_CSS.read_text(encoding="utf-8")

    # The active layout owns its viewBox.  Reusing the much wider chain width
    # for the default term layout was what shrank 13px labels to ~6px on screen.
    assert "naturalContentW(row, inferred)" in renderer
    assert 'class="prereq-svg w-100"' not in renderer
    assert "min-width:${svgW}px" in renderer

    assert "const nH = 42" in renderer
    assert "Math.max(92, maxChars * 9 + 26)" in renderer
    assert 'font-size="13"' in renderer
    assert ".pg-edge { fill:none;stroke:var(--teal);stroke-width:2;stroke-opacity:0.4" in styles


def test_narrow_screen_still_uses_the_accessible_term_list() -> None:
    student_renderer = STUDENT_GRAPH_JS.read_text(encoding="utf-8")

    assert "const NARROW = '(max-width: 768px)'" in student_renderer
    assert "if (narrow) drawList(); else drawSvg();" in student_renderer
    assert "host.setAttribute('role', 'region')" in student_renderer
    assert 'role="listitem"' in student_renderer


def test_graduation_map_focuses_on_the_remaining_prerequisite_path() -> None:
    renderer = STUDENT_GRADUATION_JS.read_text(encoding="utf-8")
    styles = GLOBAL_CSS.read_text(encoding="utf-8")

    assert "function focusRemainingPath(source)" in renderer
    assert "statuses[code] !== 'passed'" in renderer
    assert "statuses[edge.prerequisite_course_code] === 'passed'" in renderer
    assert "const graph = focusRemainingPath(sourceGraph)" in renderer
    assert ".student-grad-map-details { order: 1; }" in styles
    assert ".student-grad-timeline { order: 2; }" in styles
