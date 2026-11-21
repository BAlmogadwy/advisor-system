"""Every export, download or copy-out control looks the same: `btn btn-export`.

Before this, the app's export controls wore five different styles. The Batch,
Debug, Eligibility, Student-plan and Advisor-portfolio links were `.fb-dd` filter
chips, so they read as toggles, their icon was faded to 40%, and XLSX was tinted
teal (the chip's own "selected" cue) while CSV beside it stayed grey. Others were
`btn-outline-primary`, `btn-outline-secondary`, `btn-outline-success` or
`btn-neutral`, chosen screen by screen.

The first pass converted only the exports, and the owner's next screenshot still
showed a mixed row: "Copy IDs" stayed a grey chip beside the new CSV button. The
copy-out controls take data OUT of a screen just as an export does, and the
portfolio's "Copy selected IDs" was already `btn-export`, so they are in scope.

The guard is structural: it finds the controls by what they SAY (CSV, XLSX,
Excel, Export, Download, Copy and the Arabic تصدير / تنزيل / إكسل / نسخ) and
requires the class on each one. A few export controls are deliberately something else; they are
listed with the reason, and the list is checked both ways so it cannot rot.
"""

from __future__ import annotations

import pathlib
import re

from django.conf import settings

ROOT = pathlib.Path(settings.BASE_DIR)

_CONTROL = re.compile(r"<(a|button)\b([^>]*)>(.*?)</\1>", re.S)
_EXPORT_WORDS = re.compile(r"\b(?:CSV|XLSX|Excel|Export|Download|Copy)\b|تصدير|تنزيل|إكسل|نسخ")

#: Export-worded controls that are deliberately NOT `btn-export`, and why.
EXEMPT = {
    # The exam screens draw their own buttons: `main.exam-builder-page` restyles
    # every `.btn` (white face, navy edge), so `btn-export` would change nothing
    # there, and their export buttons already match each other. The three
    # dialog/page Downloads are also each screen's single main action.
    ("core/templates/core/exam_timetable.html", "exportXlsx"),
    ("core/templates/core/partials/exam_roster_drawer.html", "examRosterDrawerDownload"),
    ("core/templates/core/exam_rosters.html", "examRostersExport"),
    ("core/templates/core/partials/exam_student_export.html", "examStudentExportDownload"),
    ("core/templates/core/partials/exam_department_export.html", "downloadExamDepartments"),
    # Starts a scrape job that produces a CSV; it is the panel's Run, not a download.
    ("core/templates/core/partials/dashboard/_panel_scrape.html", "oracleStudentsBtn"),
    # IMPORTS a CSV mapping.
    ("core/templates/core/partials/dashboard/_panel_advisoradmin.html", "advAssignCsvBtn"),
    # One of the Timetable Workspace toolbar's split buttons, which share one
    # neutral look (pinned in test_design_system_buttons).
    ("core/templates/core/timetable_workspace.html", "twExport"),
    # Not exports at all: the two answers to a stale save in the teaching-times
    # dialog. Their Arabic says «نسخة» ("version": "load their version", "save
    # mine instead"), which contains «نسخ» ("copy"). Nothing leaves the screen.
    ("core/templates/core/instructor_management.html", "imRulesLoadTheirs"),
    ("core/templates/core/instructor_management.html", "imRulesSaveMine"),
}

#: The MRI page is frozen by the owner (2026-10-23): never restyled from here.
_SKIPPED_FILES = ("timetable_workspace_mri.html", "page-timetable-mri.js")


def _key(rel: str, attrs: str, label: str) -> tuple[str, str]:
    """(file, a stable identifier) — never a line number."""
    for probe in (r'(?<![\w-])id="([^"]+)"', r'onclick="([^"(]+)', r'href="(/[^"?{]+)'):
        found = re.search(probe, attrs)
        if found:
            return rel, found.group(1)
    # The Exports table's Copy buttons are told apart by the link they copy.
    found = re.search(r'data-link-id="([^"]+)"', attrs)
    if found:
        return rel, f"copy:{found.group(1)}"
    for cls in ("advisor-grad-export",):
        if cls in attrs:
            return rel, cls
    return rel, label[:40]


def _export_controls():
    """Yield ((file, identifier), class tokens) for every export control."""
    for base in ("core/templates", "static/js"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.suffix not in {".html", ".js"} or not path.is_file():
                continue
            if path.name in _SKIPPED_FILES or ".min." in path.name:
                continue
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for match in _CONTROL.finditer(text):
                attrs, body = match.group(2), match.group(3)
                if len(body) > 4000:
                    continue  # a runaway match across unrelated markup
                classes = re.search(r"""class=(["'])(.*?)\1""", attrs)
                tokens = classes.group(2).split() if classes else []
                if "btn" not in tokens and "fb-dd" not in tokens:
                    continue
                label = re.sub(r"<svg.*?</svg>", "", body, flags=re.S)
                label = " ".join(re.sub(r"<[^>]+>", " ", label).split())
                key = _key(rel, attrs, label)
                if _EXPORT_WORDS.search(label):
                    yield key, tokens


def test_every_export_control_wears_btn_export():
    wrong = sorted(
        f"{file} #{ident}: {' '.join(tokens)}"
        for (file, ident), tokens in _export_controls()
        if (file, ident) not in EXEMPT and not ("btn" in tokens and "btn-export" in tokens)
    )
    assert not wrong, "export controls not on `btn btn-export`:\n  " + "\n  ".join(wrong)


def test_an_export_control_carries_no_competing_style():
    """`btn-export` beside `fb-dd`, a colour utility or another variant would let
    the old look win back on specificity or `!important`."""
    rivals = re.compile(
        r"^(?:fb-dd|text-\w+|btn-(?:outline-\w+|primary|secondary|success|neutral))$"
    )
    mixed = sorted(
        f"{file} #{ident}: {' '.join(tokens)}"
        for (file, ident), tokens in _export_controls()
        if "btn-export" in tokens and any(rivals.match(t) for t in tokens)
    )
    assert not mixed, "export controls mixing in another style:\n  " + "\n  ".join(mixed)


def test_every_exemption_still_points_at_a_real_control():
    """A renamed or deleted control must leave the list, not linger in it as a
    silent pass."""
    found = {key for key, _ in _export_controls()}
    stale = sorted(EXEMPT - found)
    assert not stale, f"listed but no longer found: {stale}"


def test_the_scan_still_sees_the_controls_it_was_written_for():
    """The scan must not quietly go blind: these are the chip-styled links that
    started this, plus one of each other kind it has to catch."""
    found = {key for key, _ in _export_controls()}
    for key in (
        ("core/templates/core/partials/dashboard/_panel_batch.html", "batchCsv"),
        ("core/templates/core/partials/dashboard/_panel_batch.html", "batchXlsx"),
        ("core/templates/core/partials/dashboard/_panel_debug.html", "dbgXlsx"),
        ("core/templates/core/advisor_portfolio.html", "apCsvLink"),
        ("core/templates/core/user_management.html", "exportCSV"),
        ("core/templates/core/section_planning.html", "spExportRosters"),
        ("core/templates/core/partials/dashboard/_panel_exports.html", "exHighPriorityXlsx"),
        # The copy-out controls the owner's second screenshot showed.
        ("core/templates/core/partials/dashboard/_panel_eligibility.html", "elCopyIds"),
        ("core/templates/core/advisor_portfolio.html", "copyFilteredIds"),
        ("core/templates/core/advisor_portfolio.html", "copyHighRiskIds"),
        ("core/templates/core/partials/dashboard/_panel_student.html", "copyStudentCsv"),
        ("core/templates/core/partials/dashboard/_panel_exports.html", "copy:exStudentCsv"),
        ("core/templates/core/partials/dashboard/_panel_exports.html", "copy:exHighPriorityXlsx"),
    ):
        assert key in found, key
