"""Every top filter bar shows its fields whole the moment its page loads.

Section Planning's scope bar was redesigned so each field is as wide as what
it holds (its value, or its placeholder when empty), in ch so a wider font
widens it too, instead of being capped at 80px or squeezed by its row. The
other screens' top bars now share that as `.fb-fit` (static/css/global.css,
"Filter bar that fits its fields"). This loads each screen once per language
and width, touches nothing (no field is typed into, no button pressed; a
field is only focused to see its ring) and checks the top bar only:

- every value and placeholder shows whole: its text fits the part of the
  field the user can see, which is the field's own box cut by its capsule;
- each field sits inside its capsule; each label is on one line, whole, and
  clear of its field;
- nothing in the bar reaches past the viewport, so the bar never makes the
  page scroll sideways;
- the bar's buttons wrap as one group, never one left alone on a line;
- in Arabic the bar reads from the right and a select's arrow sits at its
  inline end (the left), where its padding is, not over its text;
- a focused field has a ring of at least 3:1 against what is behind it, in
  light and in dark.

At 390 and 1440px, English and Arabic, in a wide font (Verdana, DejaVu Sans)
as a CI machine without the design's fonts renders them. 1px is rounding: a
field sized in ch can measure a fraction over its box. Nothing leaves the
machine (tests/browser_isolation.py).
"""

from __future__ import annotations

import os

# Playwright's synchronous API runs through a greenlet; fixture creation is
# synchronous ORM work while Django serves the page on its own thread.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "1")

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client

from tests.test_exam_rosters_browser import WIDE_FONT

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

WIDTHS = (390, 1440)

# Each screen's top bar, reached the way a user reaches it (a dashboard panel
# by its tab, i.e. its URL hash).
SCREENS = {
    "advisor portfolio": ("/advisor-portfolio/", "main .filter-bar"),
    "instructor management": ("/instructor-management/", "main .filter-bar"),
    "planner": ("/planner/", "main .filter-bar"),
    "timetable workspace": ("/timetable-workspace/", "#twGenBar"),
    "dashboard: student": ("/#student", "#student > .filter-bar"),
    "dashboard: batch": ("/#batch", "#batch > .filter-bar"),
    "dashboard: prerequisites": ("/#prereq", "#prereq > .filter-bar"),
    "dashboard: program plan": ("/#plan", "#plan .field-row"),
    "dashboard: scrape": ("/#scrape", "#scrape > .filter-bar"),
    "dashboard: debug": ("/#debug", "#debug > .filter-bar"),
    "dashboard: conflict matrix": ("/#conflictmatrix", "#conflictmatrix > .filter-bar"),
    "dashboard: eligibility": ("/#eligibility", "#eligibility > .filter-bar"),
    "dashboard: high priority": ("/#highpriority", "#highpriority > .filter-bar"),
    "dashboard: advisor admin": ("/#advisoradmin", "#advisoradmin .advisor-block .field-row"),
}
# A phone folds the planner's session settings behind their toggle: on first
# load there is no bar to see.
FOLDED = {("planner", 390)}

# What is wrong with the bar at `sel`, as a list of short findings.
FIT = r"""(sel) => {
  const bar = document.querySelector(sel);
  if (!bar) return ['no bar'];
  const R = el => el.getBoundingClientRect();
  const shown = el => { const r = R(el), s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const name = el => el.id || el.getAttribute('name') || el.getAttribute('for') || el.textContent.trim().slice(0, 20);
  const dir = document.documentElement.dir;
  const bad = [];
  if (!shown(bar)) return ['bar not shown'];

  // Text width in an element's own font (or its placeholder's).
  const ruler = document.createElement('span');
  ruler.style.cssText = 'position:absolute;visibility:hidden;white-space:pre;left:-9999px;top:0';
  document.body.append(ruler);
  const textWidth = (el, text, pseudo) => { const s = getComputedStyle(el, pseudo || null);
    for (const p of ['fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing', 'textTransform', 'fontVariantNumeric'])
      ruler.style[p] = s[p];
    ruler.textContent = text; return ruler.getBoundingClientRect().width; };

  // The bar never reaches past the viewport (a scroller inside it may).
  for (const el of [bar, ...bar.querySelectorAll('*')]) {
    if (!shown(el) || ['absolute', 'fixed'].includes(getComputedStyle(el).position)) continue;
    let scrolls = false;
    for (let a = el.parentElement; a && a !== bar && el !== bar; a = a.parentElement)
      if (['auto', 'scroll'].includes(getComputedStyle(a).overflowX)) { scrolls = true; break; }
    const r = R(el);
    if (!scrolls && (r.left < -0.5 || r.right > innerWidth + 0.5)) { bad.push(`past the viewport: ${name(el)}`); break; }
  }

  const fields = [...bar.querySelectorAll('input, select')]
    .filter(el => shown(el) && !['checkbox', 'radio', 'hidden', 'file'].includes(el.type));
  if (!fields.length) bad.push('no fields');
  for (const el of fields) {
    const s = getComputedStyle(el);
    // What the user sees of the field's text: its content box, cut by every
    // clipping box up to the bar (the capsule clips).
    const r = R(el);
    let left = r.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft);
    let right = r.right - parseFloat(s.borderRightWidth) - parseFloat(s.paddingRight);
    for (let a = el.parentElement; a && a !== bar.parentElement; a = a.parentElement)
      if (getComputedStyle(a).overflowX !== 'visible') { const ar = R(a); left = Math.max(left, ar.left); right = Math.min(right, ar.right); }
    const seen = right - left;
    const texts = el.tagName === 'SELECT' ? [[(el.selectedOptions[0] || {}).text || '', null]]
                                          : [[el.value, null], [el.placeholder, '::placeholder']];
    for (const [text, pseudo] of texts) {
      if (!text) continue;
      const need = textWidth(el, text, pseudo);
      if (need > seen + 1) bad.push(`clipped: ${name(el)} "${text}" needs ${Math.ceil(need)}px, shows ${Math.floor(seen)}px`);
    }
    const capsule = el.closest('.fb-search');
    if (capsule) { const c = R(capsule);
      if (r.left < c.left - 0.5 || r.right > c.right + 0.5) bad.push(`outside its capsule: ${name(el)}`); }
    if (el.tagName === 'SELECT' && dir === 'rtl' && s.backgroundImage !== 'none' && parseFloat(s.paddingLeft) > parseFloat(s.paddingRight)
        && /100%|right/.test(s.backgroundPositionX)) bad.push(`arrow over the text: ${name(el)} at ${s.backgroundPositionX}`);
  }

  // An element's own words (not a field's, not an icon's) and how many lines they take.
  const words = el => { const own = [];
    const walk = n => { for (const c of n.childNodes) {
      if (c.nodeType === 3 && c.textContent.trim()) own.push(c);
      else if (c.nodeType === 1 && !c.matches('input, select, textarea, option, svg')) walk(c); } };
    walk(el);
    return own.flatMap(n => { const range = document.createRange(); range.selectNodeContents(n);
      return [...range.getClientRects()].filter(x => x.width > 0.5); }); };
  const lineCount = rects => { const middles = rects.map(x => (x.top + x.bottom) / 2).sort((a, b) => a - b);
    return middles.filter((m, i) => i === 0 || m - middles[i - 1] > 4).length; };

  // Labels: one line, whole, clear of every field in their capsule or group.
  const labels = [...bar.querySelectorAll('label, .fb-lbl, .form-label')]
    .filter(el => shown(el) && !el.querySelector('input[type=checkbox], input[type=radio]') && el.textContent.trim());
  for (const el of labels) {
    const rects = words(el);
    const text = el.textContent.trim().replace(/\s+/g, ' ');
    if (lineCount(rects) > 1) bad.push(`label wraps: "${text}"`);
    if (!el.querySelector('input, select') && el.scrollWidth > el.clientWidth + 1) bad.push(`label cut: "${text}"`);
    for (const field of (el.closest('.fb-search') || el.parentElement).querySelectorAll('input, select')) {
      if (!shown(field)) continue; const f = R(field);
      if (rects.some(x => Math.min(x.right, f.right) - Math.max(x.left, f.left) > 2 && Math.min(x.bottom, f.bottom) - Math.max(x.top, f.top) > 2))
        bad.push(`label over its field: "${text}"`);
    }
  }

  // The bar's own buttons (or its button group) never leave one alone on a line.
  const buttons = [...bar.querySelectorAll('button, a.fb-dd, a.btn')]
    .filter(el => shown(el) && (el.parentElement === bar || el.parentElement.matches('.fb-actions')));
  const lines = [];
  for (const el of buttons) { const r = R(el), m = (r.top + r.bottom) / 2;
    const line = lines.find(l => Math.abs(l.m - m) < 8); if (line) line.n++; else lines.push({ m, n: 1 }); }
  if (lines.length > 1 && lines.some(l => l.n === 1)) bad.push('a button alone on its line');
  for (const el of buttons)
    if (lineCount(words(el)) > 1 || el.scrollWidth > el.clientWidth + 1) bad.push(`button text broken: "${el.textContent.trim()}"`);

  // Arabic reads from the right: the bar's first item starts at its right.
  if (dir === 'rtl') {
    const items = [...bar.children].filter(el => shown(el) && !el.classList.contains('fb-spacer'));
    if (items.length > 1 && Math.abs(R(items[0]).top - R(items[1]).top) < 8 && R(items[0]).left < R(items[1]).left)
      bad.push('not mirrored');
  }
  ruler.remove();
  return bad;
}"""

# Each field's focus ring: its contrast against what is behind the capsule
# (or the field, outside a capsule). An outline or a solid box-shadow ring.
RING = r"""(sel) => {
  const bar = document.querySelector(sel);
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgba = v => { cx.clearRect(0, 0, 1, 1); cx.fillStyle = 'rgba(0,0,0,0)'; cx.fillStyle = v; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3])).concat(1);
  const behind = el => { const chain = []; for (let e = el; e; e = e.parentElement) chain.push(rgba(getComputedStyle(e).backgroundColor));
    return chain.reverse().reduce((under, c) => c[3] > 0 ? over(c, under) : under, [255, 255, 255, 1]); };
  const lum = c => c.slice(0, 3).map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; })
    .reduce((s, x, i) => s + x * [0.2126, 0.7152, 0.0722][i], 0);
  const contrast = (fg, bg) => { const a = lum(over(fg, bg)), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
  const out = [];
  for (const el of bar.querySelectorAll('input, select')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || el.disabled || ['checkbox', 'radio', 'hidden', 'file'].includes(el.type)) continue;
    el.focus({ preventScroll: true });
    const holder = el.closest('.fb-search') || el;
    const bg = behind(holder.parentElement);
    let best = 0;
    for (const s of [getComputedStyle(holder), getComputedStyle(el)]) {
      if (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 1.5) best = Math.max(best, contrast(rgba(s.outlineColor), bg));
      for (const part of s.boxShadow === 'none' ? [] : s.boxShadow.split(/,(?![^(]*\))/)) {
        if (/inset/.test(part)) continue;
        const colour = (part.match(/(rgba?|color|oklch|oklab|lab|lch|hsla?)\([^)]*\)/) || [''])[0];
        const [, , blur = 0, spread = 0] = (part.replace(colour, '').match(/-?[\d.]+px/g) || []).map(parseFloat);
        if (spread >= 1.5 && blur <= 1) best = Math.max(best, contrast(rgba(colour), bg));
      }
    }
    el.blur();
    out.push([el.id || el.name, +best.toFixed(2)]);
  }
  return out;
}"""


class FilterBarFitBrowserTests(StaticLiveServerTestCase):
    maxDiff = None

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

    def setUp(self) -> None:
        user = get_user_model().objects.create_superuser(
            username="filter-bar-fit", email="filter-bar-fit@example.test", password="unused"
        )
        client = Client()
        client.force_login(user)
        self.session = client.cookies[settings.SESSION_COOKIE_NAME].value

    def _load(self, path: str, language: str, width: int):
        """The page as it first loads: nothing typed, nothing pressed."""
        context = self.browser.new_context(
            locale="ar" if language == "ar" else "en-US",
            extra_http_headers={"Accept-Language": language},
            viewport={"width": width, "height": 900},
        )
        self.addCleanup(context.close)
        context.add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": self.session,
                    "url": self.live_server_url,
                },
                {
                    "name": settings.LANGUAGE_COOKIE_NAME,
                    "value": language,
                    "url": self.live_server_url,
                },
            ]
        )
        context.add_init_script("try { localStorage.setItem('theme', 'light'); } catch (e) {}")
        context.add_init_script(WIDE_FONT)
        page = context.new_page()
        sent: list[str] = []
        page.on(
            "request", lambda r: sent.append(f"{r.method} {r.url}") if r.method != "GET" else None
        )
        page.goto(f"{self.live_server_url}{path}")
        page.add_style_tag(
            content="*, *::before, *::after { transition: none !important; animation: none !important; }"
        )
        self.assertEqual(page.evaluate("document.documentElement.lang"), language)
        return page, sent

    def test_every_top_bar_fits_what_it_shows_on_first_load(self) -> None:
        # Every screen is checked before failing, so one run names every misfit.
        problems: dict[str, list] = {}
        for screen, (path, bar) in SCREENS.items():
            for language in ("en", "ar"):
                for width in WIDTHS:
                    where = f"{screen} {language} {width}px"
                    page, sent = self._load(path, language, width)
                    if (screen, width) in FOLDED:
                        if page.locator(bar).is_visible():
                            problems[where] = ["expected folded behind its toggle"]
                        page.context.close()
                        continue
                    found = page.evaluate(FIT, bar)
                    if width == 1440:
                        for theme in ("light", "dark"):
                            page.evaluate(
                                f"document.documentElement.setAttribute('data-theme', '{theme}')"
                            )
                            found += [
                                f"{theme}: focus ring {ratio}:1 on {field}"
                                for field, ratio in page.evaluate(RING, bar)
                                if ratio < 3
                            ]
                    # Loading the bar sent nothing that could change data.
                    found += [f"sent {request}" for request in sent]
                    if found:
                        problems[where] = found
                    page.context.close()
        self.assertEqual(problems, {})
