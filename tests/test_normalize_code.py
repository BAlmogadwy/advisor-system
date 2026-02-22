from core.services.student_helpers import normalize_code


def test_normalize_code_variants() -> None:
    assert normalize_code("CS 289") == "CS289"
    assert normalize_code("cs289") == "CS289"
    assert normalize_code("CS\u00A0289") == "CS289"
    assert normalize_code(None) == ""
