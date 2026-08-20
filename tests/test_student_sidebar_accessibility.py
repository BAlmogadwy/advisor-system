"""Static accessibility contracts for the shared responsive navigation."""

from pathlib import Path

SIDEBAR_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "templates"
    / "core"
    / "partials"
    / "sidebar.html"
)


def test_mobile_sidebar_behaves_like_an_accessible_modal_drawer():
    source = SIDEBAR_TEMPLATE.read_text(encoding="utf-8")

    assert "sidebar.setAttribute('role', 'dialog')" in source
    assert "sidebar.setAttribute('aria-modal', 'true')" in source
    assert "sidebar.setAttribute('aria-hidden', 'true')" in source
    assert "element.inert = true" in source
    assert "element.inert = wasInert" in source
    assert "previouslyFocused.focus()" in source
    assert "e.key === 'Escape'" in source
