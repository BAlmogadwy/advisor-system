"""The design system must decide what a button looks like — not the browser.

`html body .btn` shapes every button in the app (font, radius, padding,
`border: none !important`) and never gave one a background. A `.btn` carrying no
variant therefore had nothing of its own to render and fell through to
`ButtonFace`, a USER-AGENT system colour: `rgb(240,240,240)` with black text in a
light UA, `rgb(107,107,107)` with white text in a dark one. Flat grey slabs chosen
by the browser and the operating system, ignoring the theme and reading as
disabled.

AN EXPLICIT CLASS, NOT A FALLTHROUGH

The first attempt put the surface on the base rule. `html body .btn` (0,1,2)
outranks `.btn-link` (0,1,0), so link-buttons lost their colour in dark mode; and
buttons carrying an inline background gained a grey hairline inside their own
fill. `.btn-neutral` cannot interact with Bootstrap variants, link and close
controls, or the screen-specific button systems.

The scope that justified the global approach was also wrong. `\\bbtn\\b` matches
INSIDE `btn-circle` — `\\b` fires at the hyphen — so "130 controls across 30
templates" was about 5x too many. Tokenising the class attribute gives 19 template
sites; three of those own a surface inline and were never affected, leaving 16,
plus four buttons the adviser screen builds in JavaScript. Twenty.
"""

from __future__ import annotations

import os
import pathlib
import re

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from django.urls import reverse

from core.models import Student
from core.services.rbac import ensure_role_groups

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

STUDENT = 7003001
ROOT = pathlib.Path(settings.BASE_DIR)

#: Chromium's `ButtonFace` in its light and dark UA themes — the exact values the
#: defect produced.
USER_AGENT_BUTTON_FACES = {"rgb(240, 240, 240)", "rgb(107, 107, 107)"}

#: WCAG 1.4.11 requires 3:1 for a non-text component boundary. Asserted at 3.25,
#: because sitting on the threshold is fragile against alpha rounding, slightly
#: different rendered backdrops, device-pixel anti-aliasing and any future token
#: change. The chosen alphas measure 3.4–4.5, so the assertion has headroom and it
#: is the THRESHOLD that is pinned here, never the alpha.
MIN_BOUNDARY = 3.25
#: WCAG 1.4.3 for normal text.
MIN_TEXT = 4.5


def _class_attributes(path: pathlib.Path):
    """Every `class="…"` in a file, TOKENISED. Never a regex over the attribute:
    `\\bbtn\\b` matches inside `btn-circle`, which is how the original scope came
    out five times too large."""
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"""class=(["'])(.*?)\1""", text):
        yield match, text, match.group(2).split()


#: Every control that must carry `btn-neutral`, by file and by a STABLE identifier
#: — never by line number, which moves whenever anything above it is edited.
NEUTRAL_TEMPLATE_SITES = {
    ("core/templates/core/advisor_inbox_case.html", "assign_to_me"),
    ("core/templates/core/advisor_inbox_case.html", "set_status"),
    ("core/templates/core/advisor_inbox_case.html", "add_note"),
    ("core/templates/core/db_admin.html", "elecRefreshBtn"),
    ("core/templates/core/instructor_management.html", "imHideInstructorModal"),
    ("core/templates/core/partials/dashboard/_panel_conflict_matrix.html", "cmSavePreset"),
    ("core/templates/core/partials/dashboard/_panel_conflict_matrix.html", "cmLoadPreset"),
    ("core/templates/core/partials/dashboard/_panel_conflict_matrix.html", "cmXlsx"),
    ("core/templates/core/section_planning.html", "spDeptFilterClear"),
    ("core/templates/core/student_advisor.html", "example:graduate-ar"),
    ("core/templates/core/student_advisor.html", "example:register-ar"),
    ("core/templates/core/student_advisor.html", "example:withdraw-ar"),
    ("core/templates/core/student_advisor.html", "example:graduate-en"),
    ("core/templates/core/student_advisor.html", "example:register-en"),
    ("core/templates/core/student_advisor.html", "example:withdraw-en"),
    ("core/templates/core/student_graduation.html", "link:student_advisor"),
}

#: The three that style themselves inline. They were never `ButtonFace`, and the
#: neutral ring would draw a hairline inside a fill they already own.
INLINE_SURFACE_SITES = {
    ("core/templates/core/section_planning.html", "spAdvSaveDb"),
    ("core/templates/core/timetable_workspace.html", "twMapElectives"),
    ("core/templates/core/timetable_workspace.html", "twFullscreen"),
}

#: The four the adviser screen builds at runtime, by the class the constructor uses.
NEUTRAL_JS_CONTROLS = {"sa-fb-btn", "sa-fb-reason", "sa-retry", "sa-escalate-btn"}

#: Arabic question text is the only stable handle the example chips have, so they
#: are keyed by intent rather than by the sentence.
_EXAMPLE_KEYS = {
    "وش باقي": "example:graduate-ar",
    "ما المقررات": "example:register-ar",
    "كم مرة": "example:withdraw-ar",
    "How many credits": "example:graduate-en",
    "Which courses": "example:register-en",
    "How many times": "example:withdraw-en",
}


def _identify(path: pathlib.Path, tag: str) -> tuple[str, str]:
    """(file, stable identifier) for one control."""
    rel = path.relative_to(ROOT).as_posix()
    for probe in (r'id="([^"]+)"', r'data-ai-action="([^"]+)"', r'onclick="([^"(]+)'):
        found = re.search(probe, tag)
        if found:
            return rel, found.group(1)
    example = re.search(r'data-sa-example="([^"]+)"', tag)
    if example:
        for prefix, key in _EXAMPLE_KEYS.items():
            if example.group(1).startswith(prefix):
                return rel, key
    href = re.search(r"""href="\{%\s*url\s*'([^']+)'""", tag)
    if href:
        return rel, f"link:{href.group(1)}"
    return rel, tag[:60]


#: Every colour function a computed `box-shadow` can contain, with its alpha.
#: Chromium serialises this project's `color-mix` results as `color(srgb r g b / a)`
#: and everything else as `rgb()`/`rgba()`, so both forms have to be read.
_SHADOW_COLOUR = re.compile(r"(?:rgba?|color|hsla?)\(([^)]*)\)")


def _layers(box_shadow: str) -> list[dict]:
    """One entry per box-shadow layer: its colour alpha, spread, and insetness."""
    if not box_shadow or box_shadow == "none":
        return []
    out = []
    for layer in re.split(r",(?![^(]*\))", box_shadow):
        colour = _SHADOW_COLOUR.search(layer)
        alpha = 1.0
        if colour:
            numbers = [float(n) for n in re.findall(r"[\d.]+", colour.group(1))]
            # rgba(r, g, b, a) and color(srgb r g b / a) both put alpha fourth.
            alpha = numbers[3] if len(numbers) >= 4 else 1.0
        lengths = [float(n) for n in re.findall(r"(-?[\d.]+)px", layer)]
        out.append(
            {
                "alpha": alpha,
                "spread": lengths[3] if len(lengths) >= 4 else 0.0,
                "inset": "inset" in layer,
                "raw": layer.strip(),
            }
        )
    return out


def _ring_alpha(box_shadow: str) -> float:
    """The alpha of the OUTER ring — the widest-spread outset layer.

    Not the strongest alpha anywhere in the shadow: both focus levels contain an
    opaque 2px gap layer, so a `max()` over every layer reads 1.0 for both and
    cannot tell them apart. What the contract distinguishes is the ring itself.
    """
    outset = [layer for layer in _layers(box_shadow) if not layer["inset"]]
    if not outset:
        return 0.0
    return max(outset, key=lambda layer: layer["spread"])["alpha"]


def _has_boundary(box_shadow: str) -> bool:
    """An inset ring of at least 1px is still present."""
    return any(layer["inset"] and layer["spread"] >= 1 for layer in _layers(box_shadow))


def _stylesheet() -> str:
    return "".join(
        (ROOT / "static/css" / name).read_text(encoding="utf-8")
        for name in ("global.css", "bootstrap-compat.css")
    )


class ButtonInventoryTests:
    """Structural, no browser: which controls carry the class."""


@pytest.mark.parametrize("_", [0])
def test_every_affected_control_carries_btn_neutral(_):
    """The guard against the class being added to a screen and forgotten on the
    next one.

    A control is AFFECTED when it carries the exact token `btn`, none of its other
    `btn-*` tokens has a CSS rule anywhere, and it has no inline background of its
    own. That last clause matters: three sites style themselves inline, so they
    never fell through to `ButtonFace` and the neutral ring would draw a hairline
    inside a fill they already own.
    """
    css = _stylesheet()

    def has_rule(cls: str) -> bool:
        return re.search(r"\." + re.escape(cls) + r"(?![\w-])", css) is not None

    missing, covered, inline = [], [], []
    for root in ("core/templates", "static/js"):
        for path in sorted((ROOT / root).rglob("*")):
            if path.suffix not in {".html", ".js"} or not path.is_file():
                continue
            for match, text, classes in _class_attributes(path):
                if "btn" not in classes:
                    continue
                variants = [
                    c
                    for c in classes
                    if c.startswith("btn-") and c not in ("btn-sm", "btn-neutral")
                ]
                if [c for c in variants if has_rule(c)]:
                    continue  # a real variant: it brings its own surface
                tag = text[text.rfind("<", 0, match.start()) : text.find(">", match.end()) + 1]
                where = f"{path.relative_to(ROOT).as_posix()}:{text[: match.start()].count(chr(10)) + 1}"
                entry = (_identify(path, tag), where)
                if re.search(r"""style=["'][^"']*background""", tag):
                    inline.append(entry)
                elif "btn-neutral" in classes:
                    covered.append(entry)
                else:
                    missing.append(where)

    assert not missing, f"controls with no surface and no btn-neutral: {missing}"
    # THE EXACT SITES, not the count. A count stays at 16 while one correct control
    # loses the class and an unrelated one gains it, so each is pinned by file and
    # by a stable identifier — an id, a data attribute, or the href it points at.
    assert {c for c, _ in covered} == set(NEUTRAL_TEMPLATE_SITES), (
        f"the set of neutral controls changed.\n"
        f"  unexpected: {sorted({c for c, _ in covered} - set(NEUTRAL_TEMPLATE_SITES))}\n"
        f"  missing:    {sorted(set(NEUTRAL_TEMPLATE_SITES) - {c for c, _ in covered})}"
    )
    assert {c for c, _ in inline} == set(INLINE_SURFACE_SITES), (
        f"the set of inline-surface controls changed.\n"
        f"  unexpected: {sorted({c for c, _ in inline} - set(INLINE_SURFACE_SITES))}\n"
        f"  missing:    {sorted(set(INLINE_SURFACE_SITES) - {c for c, _ in inline})}"
    )


def test_the_javascript_built_adviser_controls_carry_it_too():
    """Four of the affected controls exist only at runtime, so no template scan can
    see them — and they are the rating, escalate and retry buttons the screen
    review was about."""
    js = (ROOT / "static/js/page-student-advisor.js").read_text(encoding="utf-8")
    built_neutral = set(re.findall(r"""el\(\s*['"]button['"]\s*,\s*['"]([^'"]*)['"]""", js))
    carrying = {
        cls
        for line in built_neutral
        if "btn-neutral" in line.split()
        for cls in line.split()
        if cls.startswith("sa-")
    }
    assert carrying == NEUTRAL_JS_CONTROLS, (
        f"the set of generated neutral controls changed: {carrying} != {NEUTRAL_JS_CONTROLS}"
    )
    for name in sorted(NEUTRAL_JS_CONTROLS):
        built = re.findall(
            r"""el\(\s*['"]button['"]\s*,\s*['"]([^'"]*""" + re.escape(name) + r"""[^'"]*)['"]""",
            js,
        )
        assert built, f"{name} is no longer built with el('button', …); the guard is blind"
        for classes in built:
            assert "btn-neutral" in classes.split(), f"{name}: {classes!r}"


def test_no_btn_neutral_reached_a_control_that_owns_a_surface():
    """`.btn-link`, `.btn-close` and the filled variants bring their own
    background. The neutral class must not be on them: that is the interaction the
    explicit-class design exists to make impossible."""
    css = _stylesheet()

    def has_rule(cls: str) -> bool:
        return re.search(r"\." + re.escape(cls) + r"(?![\w-])", css) is not None

    wrong = []
    for root in ("core/templates", "static/js"):
        for path in sorted((ROOT / root).rglob("*")):
            if path.suffix not in {".html", ".js"} or not path.is_file():
                continue
            for match, text, classes in _class_attributes(path):
                if "btn-neutral" not in classes:
                    continue
                clashing = [
                    c
                    for c in classes
                    if c.startswith("btn-") and c not in ("btn-sm", "btn-neutral") and has_rule(c)
                ]
                if clashing:
                    wrong.append(
                        f"{path.relative_to(ROOT)}:"
                        f"{text[: match.start()].count(chr(10)) + 1} {clashing}"
                    )
    assert not wrong, f"btn-neutral on controls that already have a surface: {wrong}"


def test_the_base_button_rule_still_declares_no_surface():
    """The global fallthrough must stay removed. Re-adding a background to
    `html body .btn` is the rejected design, and it is one edit away."""
    css = (ROOT / "static/css/global.css").read_text(encoding="utf-8")
    base = re.search(r"html body \.btn \{(.*?)\n\}", css, re.S)
    assert base, "the base rule moved; this guard is blind"
    body = base.group(1)
    assert "background" not in body, (
        "the base .btn rule declares a background again — that is the design that "
        "outranked .btn-link and hairlined inline-styled buttons"
    )


# ── the browser acceptance matrix ────────────────────────────────

#: The four surfaces a neutral button actually sits on. Opaque palette colours
#: cannot serve all four — `.va-bubble` IS `var(--surface)`, so a button painted
#: `var(--surface)` is 1.00:1 on the screen this was written for.
SURFACES = {
    "card": "var(--card)",
    "va-bubble": "var(--surface)",
    "modal footer": "var(--surface)",
    "glass toolbar": "var(--glass-bg)",
}

PROBE = """
({theme, surfaces, shapes}) => {
  document.documentElement.setAttribute('data-theme', theme);
  const parse = (s) => { const n = (s.match(/[\\d.]+/g) || [0,0,0,1]).map(Number);
                         return [n[0]||0, n[1]||0, n[2]||0, n.length > 3 ? n[3] : 1]; };
  const over = (fg, bg) => fg.slice(0,3).map((c,i) => c*fg[3] + bg[i]*(1-fg[3]));
  const lum = (c) => { const [r,g,b] = c.map(v => { v/=255;
      return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); });
      return 0.2126*r + 0.7152*g + 0.0722*b; };
  const ratio = (a,b) => { const l = [lum(a), lum(b)].sort((x,y) => y-x);
                           return +((l[0]+0.05)/(l[1]+0.05)).toFixed(2); };

  const host = document.querySelector('.sa-main') || document.body;
  const made = [];
  const opaque = (expr) => {
    const probe = document.createElement('div');
    probe.style.backgroundColor = expr;
    host.appendChild(probe); made.push(probe);
    const c = parse(getComputedStyle(probe).backgroundColor);
    // A translucent surface token composites over the page background.
    const page = parse(getComputedStyle(document.body).backgroundColor);
    return c[3] === 1 ? c.slice(0,3) : over(c, page[3] === 1 ? page.slice(0,3) : [255,255,255]);
  };

  const out = {theme, screenLoaded: !!document.querySelector('.sa-main .va-chat'), surfaces: {}};
  for (const [label, expr] of Object.entries(surfaces)) {
    const backdrop = opaque(expr);
    const panel = document.createElement('div');
    panel.style.backgroundColor = 'rgb(' + backdrop.map(Math.round).join(',') + ')';
    host.appendChild(panel); made.push(panel);

    out.surfaces[label] = {};
    for (const cls of shapes) {
      const b = document.createElement('button');
      b.className = cls; b.textContent = 'probe';
      panel.appendChild(b);
      const cs = getComputedStyle(b);
      const fill = over(parse(cs.backgroundColor), backdrop);
      const text = over(parse(cs.color), fill);
      const ringColour = (cs.boxShadow.match(/(rgba?\\([^)]*\\))[^,]*inset/) || [])[1];
      out.surfaces[label][cls] = {
        background: cs.backgroundColor,
        boundary: ringColour ? ratio(over(parse(ringColour), fill), backdrop) : null,
        textOnFill: ratio(text, fill),
        fillVsBackdrop: ratio(fill, backdrop),
      };
      b.remove();
    }
  }
  made.forEach((n) => n.remove());
  return out;
}
"""


class ButtonSurfaceTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def _page(self):
        from core.services import student_otp

        ensure_role_groups()
        Student.objects.get_or_create(
            student_id=STUDENT, defaults={"name": "S", "program": "AI", "section": "M"}
        )
        client = Client()
        client.force_login(student_otp.provision_student_user(STUDENT))
        context = self.browser.new_context()
        context.add_cookies(
            [
                {
                    "name": "sessionid",
                    "value": client.cookies["sessionid"].value,
                    "url": self.live_server_url,
                }
            ]
        )
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.live_server_url}{reverse('student_advisor')}")
        page.wait_for_load_state("networkidle")
        return page

    def _probe(self, page, theme, shapes):
        seen = page.evaluate(PROBE, {"theme": theme, "surfaces": SURFACES, "shapes": shapes})
        # Seven of eight tests in an earlier version of this file passed on the
        # LOGIN page. A redirect to login was invisible to all of them.
        assert seen["screenLoaded"], "the adviser screen did not render; this asserts nothing"
        return seen

    def _focus_state(self, page, *, via: str) -> dict:
        """Focus a fresh probe by one modality and read what it got."""
        page.evaluate(
            """() => {
              const probe = document.createElement('button');
              probe.className = 'btn btn-neutral';
              probe.textContent = 'focus probe';
              document.body.insertBefore(probe, document.body.firstChild);
            }"""
        )
        if via == "keyboard":
            reached = False
            for _ in range(10):
                page.keyboard.press("Tab")
                if page.evaluate("() => document.activeElement.textContent === 'focus probe'"):
                    reached = True
                    break
            assert reached, "the probe was never reached by keyboard; this asserts nothing"
        else:
            page.locator("button.btn-neutral", has_text="focus probe").click()
        page.wait_for_timeout(350)  # `.btn` transitions box-shadow over .20s
        state = page.evaluate(
            """() => {
              const el = document.activeElement;
              return {isProbe: el.textContent === 'focus probe',
                      focusVisible: el.matches(':focus-visible'),
                      shadow: getComputedStyle(el).boxShadow};
            }"""
        )
        assert state["isProbe"], f"the {via} did not leave focus on the probe"
        return state

    # ── the matrix: four surfaces x two themes ──────────────────
    def test_the_neutral_button_holds_its_contrast_on_every_surface(self):
        """The property an opaque token cannot have. `.va-bubble` is
        `var(--surface)`, so a `var(--surface)` button is invisible on the screen
        this was written for; a translucent tint composites over whatever is
        behind it, so its contrast is a property of the tint."""
        page = self._page()
        for theme in ("light", "dark"):
            seen = self._probe(page, theme, ["btn btn-neutral"])
            for surface, shapes in seen["surfaces"].items():
                m = shapes["btn btn-neutral"]
                assert m["background"] not in USER_AGENT_BUTTON_FACES, (theme, surface, m)
                assert m["fillVsBackdrop"] > 1.1, f"{theme}/{surface}: invisible fill {m}"
                assert m["boundary"] is not None, f"{theme}/{surface}: no boundary {m}"
                assert m["boundary"] >= MIN_BOUNDARY, (
                    f"{theme}/{surface}: boundary {m['boundary']} < {MIN_BOUNDARY} (WCAG 1.4.11)"
                )
                assert m["textOnFill"] >= MIN_TEXT, f"{theme}/{surface}: text {m}"

    def test_every_control_type_gets_the_same_neutral_treatment(self):
        """A fix that reached the probe's class shape and missed the adviser
        screen's own controls would otherwise pass — those are `btn btn-neutral
        btn-sm sa-fb-btn` and friends, and `.sa-fb-btn` declares no background."""
        shapes = [
            "btn btn-neutral",
            "btn btn-neutral btn-sm",
            "btn btn-neutral btn-sm sa-fb-btn",
            "btn btn-neutral btn-sm sa-fb-reason",
            "btn btn-neutral btn-sm sa-retry",
            "btn btn-neutral btn-sm sa-escalate-btn",
            "btn btn-neutral btn-secondary",
            "btn btn-neutral btn-outline btn-sm",
        ]
        page = self._page()
        for theme in ("light", "dark"):
            seen = self._probe(page, theme, shapes)
            for surface, measured in seen["surfaces"].items():
                fills = {m["background"] for m in measured.values()}
                assert len(fills) == 1, f"{theme}/{surface}: {len(fills)} different fills {fills}"
                for cls, m in measured.items():
                    assert m["boundary"] >= MIN_BOUNDARY, (theme, surface, cls, m)

    def test_the_variants_that_own_a_surface_are_untouched(self):
        """`.btn-link` and `.btn-close` are deliberately surface-less; the filled
        variants bring their own. Under the rejected base-rule design the link
        variant lost its colour in dark mode."""
        page = self._page()
        for theme in ("light", "dark"):
            seen = self._probe(
                page, theme, ["btn btn-primary", "btn btn-danger", "btn btn-link", "btn-close"]
            )
            for surface, measured in seen["surfaces"].items():
                assert measured["btn btn-link"]["background"] == "rgba(0, 0, 0, 0)", (
                    theme,
                    surface,
                    measured["btn btn-link"],
                )
                assert (
                    measured["btn-close"]["background"] != measured["btn btn-primary"]["background"]
                )
                for filled in ("btn btn-primary", "btn btn-danger"):
                    assert measured[filled]["background"] not in USER_AGENT_BUTTON_FACES

    def test_a_real_adviser_control_is_neutral_inside_its_own_bubble(self):
        """The measurement on the actual screen rather than on a probe: the
        feedback buttons are appended into `.va-bubble`, which is the surface that
        made the first attempt invisible."""
        page = self._page()
        measured = page.evaluate(
            """() => {
              const bubble = document.createElement('div');
              bubble.className = 'va-bubble';
              (document.querySelector('.sa-main') || document.body).appendChild(bubble);
              const b = document.createElement('button');
              b.className = 'btn btn-neutral btn-sm sa-fb-btn';
              b.textContent = 'Yes';
              bubble.appendChild(b);
              const cs = getComputedStyle(b);
              const out = {
                bubble: getComputedStyle(bubble).backgroundColor,
                button: cs.backgroundColor,
                shadow: cs.boxShadow,
              };
              bubble.remove();
              return out;
            }"""
        )
        assert measured["button"] != measured["bubble"], (
            f"the control is the same colour as its bubble: {measured}"
        )
        assert "inset" in measured["shadow"], measured

    def test_the_generated_controls_still_carry_the_class_once_rendered(self):
        """The structural guard reads the CONSTRUCTOR. That is not the same claim:
        `el('button', 'btn btn-neutral …')` can be perfectly correct while a later
        `className =`, `classList.remove` or `setAttribute('class', …)` strips it
        before the student ever sees the control.

        So the four are rendered through the real code path — a stored turn, loaded
        by the real endpoint — and read off the live DOM."""
        from unittest import mock

        from core.models import AdvisorConversation, RateLimitBucket

        conversation = AdvisorConversation.objects.create(student_id=STUDENT)
        with mock.patch(
            "core.services.virtual_advisor.answer_virtual_advisor",
            return_value={
                "ok": True,
                "answer": "الحد الأقصى خمسة انسحابات.",
                "model": "stub",
                "citations": [],
                "cited_policy_ids": [],
                "agent": {"loop_used": True, "policy_grounding": "retrieved"},
            },
        ):
            from django.test import Client

            from core.services import student_otp

            client = Client()
            client.force_login(student_otp.provision_student_user(STUDENT))
            response = client.post(
                reverse("advisor_conversation_send", args=[conversation.id]),
                data='{"message": "كم مرة؟"}',
                content_type="application/json",
            )
        assert response.status_code == 201, response.content
        RateLimitBucket.objects.all().delete()

        page = self._page()
        page.goto(f"{self.live_server_url}{reverse('student_advisor')}?c={conversation.id}")
        page.wait_for_selector(".sa-feedback .sa-fb-btn")

        rendered = page.evaluate(
            """() => {
              const seen = {};
              for (const name of ['sa-fb-btn', 'sa-fb-reason', 'sa-retry', 'sa-escalate-btn']) {
                const el = document.querySelector('.' + name);
                seen[name] = el ? {classes: Array.from(el.classList),
                                   shadow: getComputedStyle(el).boxShadow} : null;
              }
              return seen;
            }"""
        )
        # `sa-fb-reason` and `sa-retry` only exist after a negative rating or a
        # failed turn, so absence is expected; presence without the class is not.
        for name, state in rendered.items():
            if state is None:
                continue
            assert "btn-neutral" in state["classes"], (
                f"{name} lost btn-neutral between its constructor and the DOM: {state}"
            )
            assert _has_boundary(state["shadow"]), f"{name} rendered with no boundary: {state}"
        assert rendered["sa-fb-btn"] is not None, "no rating button rendered; this asserts nothing"
        assert rendered["sa-escalate-btn"] is not None, "no escalate button rendered"

    # ── interaction ─────────────────────────────────────────────
    def test_focus_is_visible_to_the_mouse_and_stronger_to_the_keyboard(self):
        """Two levels. The old ring was 50%-alpha teal at 1.9–2.1:1; dropping
        `:focus` entirely then left `page-student-planner.js`'s deliberate focus
        moves with no indicator at all."""
        # ONE PAGE PER MODALITY. The two measurements interfere in both orders:
        # clicking moves the sequential-focus starting point onto the probe so a
        # later Tab goes past it, and Chromium keeps `:focus-visible` on after a
        # click when the previous interaction was keyboard. A fresh page for each
        # is the only way to measure what a real user of that modality gets.
        keyboard = self._focus_state(self._page(), via="keyboard")
        assert keyboard["focusVisible"], "the probe never took keyboard focus"

        mouse = self._focus_state(self._page(), via="mouse")
        assert not mouse["focusVisible"], "a mouse click set :focus-visible"
        assert mouse["shadow"] != "none", "a programmatic/mouse focus shows nothing"

        # Comparing the whole box-shadow STRING is not the contract, and a mutant
        # that gave `:focus-visible` the same 65% teal as `:focus` survived it. What
        # the contract says is that the keyboard ring is the stronger of the two, so
        # measure the ring's own alpha: `var(--teal)` is opaque, the mouse ring's
        # `color-mix(… 65%, transparent)` is not.
        # THE BOUNDARY SURVIVES FOCUS. box-shadow is not additive, so any rule that
        # sets it replaces the whole list — and `html body .btn:focus` carries
        # `!important` at the SAME specificity as the neutral override, so before
        # the neutral rules were moved after it the focused control rendered two
        # layers instead of three and lost the only boundary it has.
        for label, state in (("keyboard", keyboard), ("mouse", mouse)):
            assert _has_boundary(state["shadow"]), (
                f"{label} focus erased the inset boundary: {state['shadow']!r}"
            )

        mouse_max, keyboard_max = (
            _ring_alpha(mouse["shadow"]),
            _ring_alpha(keyboard["shadow"]),
        )
        assert keyboard_max > mouse_max, (
            f"the keyboard ring is not stronger than the mouse ring: "
            f"{keyboard_max} vs {mouse_max} — {keyboard['shadow']!r} vs {mouse['shadow']!r}"
        )
        assert keyboard_max == 1, f"the keyboard ring is not fully opaque: {keyboard['shadow']!r}"

    def test_a_control_disabled_BY_CLASS_does_not_look_focused(self):
        """The `disabled` ATTRIBUTE is not the case worth testing: HTML makes such
        an element unfocusable, so `.focus()` is a no-op and no rule can fire. An
        earlier version of this test used it and a mutant deleting the guard
        survived — the test was asserting something that cannot happen.

        `class="btn disabled"` IS focusable. That is why the stylesheet carries both
        selectors, and it is the only one a test can prove."""
        page = self._page()
        measured = page.evaluate(
            """() => {
              const b = document.createElement('button');
              b.className = 'btn btn-neutral disabled';
              b.textContent = 'off';
              document.body.insertBefore(b, document.body.firstChild);
              const resting = getComputedStyle(b).boxShadow;
              b.focus();
              const focusable = document.activeElement === b;
              const focused = getComputedStyle(b).boxShadow;
              const opacity = getComputedStyle(b).opacity;
              b.remove();
              return {resting, focused, opacity, focusable};
            }"""
        )
        assert measured["focusable"], (
            "a .disabled control could not take focus, so this proves nothing"
        )
        # Three named properties, because "does not look focused" is three claims.
        # It stays visibly DISABLED …
        assert float(measured["opacity"]) < 1, f"not visibly disabled: {measured}"
        # … it keeps its BOUNDARY, so it is still recognisably a control …
        assert _has_boundary(measured["focused"]), (
            f"a disabled control lost its boundary when focused: {measured}"
        )
        # … and it does not put on the keyboard-focus presentation.
        assert _ring_alpha(measured["focused"]) == 0.0, (
            f"a disabled control gained an outer focus ring: {measured}"
        )
        assert measured["focused"] == measured["resting"], (
            f"focusing a disabled control changed how it looks: {measured}"
        )

    def test_the_primary_button_still_shows_a_ring(self):
        """The focus gap is `--surface`, not `--card`, so the ring survives on a
        saturated fill instead of being swallowed by it."""
        page = self._page()
        measured = page.evaluate(
            """() => {
              const b = document.createElement('button');
              b.className = 'btn btn-primary';
              b.textContent = 'send';
              document.body.insertBefore(b, document.body.firstChild);
              const resting = getComputedStyle(b).boxShadow;
              b.focus();
              const focused = getComputedStyle(b).boxShadow;
              b.remove();
              return {resting, focused};
            }"""
        )
        assert measured["focused"] != measured["resting"], measured

    def test_the_second_surface_is_a_second_surface(self):
        """`--surface-2` aliased to `--surface` is the same surface-on-surface
        defect as the button had. `.sa-escalate.is-prominent` — the panel whose
        whole job is to stand out when the adviser abstains — sits inside a
        `.va-bubble` that IS `var(--surface)`, so the collapse takes it to 1.00:1
        and leaves a 1.19:1 border as the only cue."""
        page = self._page()
        measured = page.evaluate(
            """() => {
              const bubble = document.createElement('div');
              bubble.className = 'va-bubble';
              (document.querySelector('.sa-main') || document.body).appendChild(bubble);
              const read = (cls) => {
                const el = document.createElement('div');
                el.className = cls;
                bubble.appendChild(el);
                const bg = getComputedStyle(el).backgroundColor;
                el.remove();
                return bg;
              };
              const out = {
                bubble: getComputedStyle(bubble).backgroundColor,
                prominent: read('sa-escalate is-prominent'),
                caseCard: read('sa-case'),
              };
              bubble.remove();
              return out;
            }"""
        )
        assert measured["prominent"] != measured["bubble"], (
            f"the prominent panel is the same colour as its bubble: {measured}"
        )
        assert measured["caseCard"] != measured["bubble"], measured

    # ── navigation ──────────────────────────────────────────────
    def test_the_active_conversation_is_distinguishable_without_font_weight(self):
        """A grey 1.19:1 border left `font-weight: 600` as the only reliable cue.
        The app's own selected state — `.nav-a.active`, 250px away in the same
        viewport — uses a teal wash and a teal inset edge."""
        page = self._page()
        measured = page.evaluate(
            """() => {
              const host = document.querySelector('.sa-conv-list') || document.body;
              const make = (cls) => {
                const b = document.createElement('button');
                b.className = cls; b.textContent = 'conversation';
                host.appendChild(b); return b;
              };
              const plain = make('sa-conv');
              const active = make('sa-conv is-active');
              const read = (el) => { const cs = getComputedStyle(el);
                return {background: cs.backgroundImage + '|' + cs.backgroundColor,
                        shadow: cs.boxShadow, colour: cs.color}; };
              const out = {plain: read(plain), active: read(active)};
              plain.remove(); active.remove();
              return out;
            }"""
        )
        differences = sum(
            1
            for key in ("background", "shadow", "colour")
            if measured["active"][key] != measured["plain"][key]
        )
        assert differences >= 2, (
            f"the selected conversation differs on {differences} cue(s) besides weight: {measured}"
        )
