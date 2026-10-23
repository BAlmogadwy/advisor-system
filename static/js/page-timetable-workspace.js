/* ══════════════════════════════════════════════════════════════════
   Timetable Workspace — Client-Side Logic
   ══════════════════════════════════════════════════════════════════ */

/* The template's value, not the identifier in quotes.
 *
 * `'LANGUAGE_CODE' === 'ar'` is false for everyone, which pinned all 145 uses
 * of IS_AR — and the whole 27-entry `T` dictionary below — to the English arm.
 * This screen has never rendered in Arabic. Both sibling files
 * (page-timetable-workspace-split.js, page-exam-timetable.js) already read the
 * bare reference; only this one quoted it. `typeof` for the same reason as
 * page-advisor-portfolio.js: a missing template block must degrade to English,
 * not throw at the top level and take the page's JavaScript with it.
 */
const IS_AR = (typeof LANGUAGE_CODE === 'string' ? LANGUAGE_CODE : 'en') === 'ar';
const T = {
  draft: IS_AR ? 'مسودة' : 'Draft',
  published: IS_AR ? 'منشور' : 'Published',
  selectScenario: IS_AR ? 'اختر سيناريو' : 'Select Scenario',
  createScenario: IS_AR ? 'إنشاء سيناريو' : 'Create Scenario',
  scenarioName: IS_AR ? 'اسم السيناريو' : 'Scenario name',
  createBoard: IS_AR ? 'إنشاء لوحة' : 'Create Board',
  boardLabel: IS_AR ? 'اسم اللوحة' : 'Board label',
  placed: IS_AR ? 'موضوعة' : 'Placed',
  publishConfirm: IS_AR ? 'هل تريد نشر هذا السيناريو؟' : 'Publish this scenario?',
  published_ok: IS_AR ? 'تم النشر' : 'Published successfully',
  lockHelp: IS_AR ? 'يثبّت أيام وأوقات هذه الشعبة؛ ويمكن تغيير القاعة.' : "Keeps this section's days and times; its room can still change.",
  publish: IS_AR ? 'نشر' : 'Publish',
  checking: IS_AR ? 'جارٍ التحقق…' : 'Checking…',
};

const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value || 'djCsrfToken';
const $ = id => document.getElementById(id);

const DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU'];
const DAY_LABELS = IS_AR
  ? ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس']
  : ['Sun', 'Mon', 'Tue', 'Wed', 'Thu'];

/* ── State ── */
const TW = {
  scenarioId: null,
  scenario: null,
  boardId: null,
  boards: [],
  placements: [],
  conflicts: {},
  conflictsBoardId: null,
  unplacedSections: [],
  undoStack: [],
  redoStack: [],
  selectedPlacementId: null,
  capacityData: null,
  generatedPlan: null,       // section plan from generate
  sectionBudget: null,       // budget per course
  studentSummary: null,      // student classification summary
  planLens: {
    plans: [],
    courses: {},
    sections: {},
    loaded: false,
  },
  // The tables the user opened while the filter folds them ({group}|{kind}):
  // page memory for one board and one text, never stored (design record
  // §7.2 item 6, 6c). `foldScope` is the board and text it holds for.
  foldOpen: new Set(),
  foldScope: '',
  // Every successful write bumps this. Scenario-wide reads (quality, publish
  // readiness) are cached against it, so switching boards never recomputes them.
  dataVersion: 0,
  qualityVersion: -1,
  readiness: null,
  readinessVersion: -1,
};

/* ── Keyboard (design record §7.3, 5a) ──
   The board and the required-sections list are each ONE tab stop
   (UXControls.roving): arrows move over the cards (and the empty cells), ↑/↓
   walk the sections stacked at one time, Enter selects (Selection shows it),
   Escape clears the selection, M moves the card's meeting (5e) and R changes
   its room (5c; this map is the only listener that reads M or R). The keys
   line under each shows while
   the keyboard is there; what a key did is said in #twBoardStatus, outside
   both panes (a collapsed inspector would silence its own status line).
   A table the filter folds is one stop, its fold toggle (6c): Enter (the
   button's own) opens it, and ↓ goes on into the table. */
const boardNav = window.UXControls?.roving?.($('twGrid'), {
  stop: '.tw-card, .tw-fold-toggle',
  hint: $('twGridKeys'),
  initial: () => (TW.selectedPlacementId ? `placement:${TW.selectedPlacementId}` : ''),
  activate: stop => { if (stop.matches('.tw-card')) selectCardFromKeyboard(stop); },
  keys: { Escape: () => clearSelectionFromKeyboard(), M: card => runCardKey('move', card), R: card => runCardKey('room', card) },
});
const listNav = window.UXControls?.roving?.($('twSectionList'), {
  hint: $('twListKeys'),
  activate: cell => activateListRow(cell),
});

function announceBoard(text) {
  const status = $('twBoardStatus');
  if (!status) return;
  status.textContent = '';
  status.textContent = text;
}

/* Enter on a card: selected, as a click does; the keyboard stays on the card. */
function selectCardFromKeyboard(card) {
  card.click();
  const p = TW.placements.find(row => String(row.id) === card.dataset.placementId);
  if (!p) return;
  const what = `\u2066${p.course_code} ${p.section}\u2069`;
  announceBoard(IS_AR ? `حُدِّدت الشعبة ${what}؛ تفاصيلها في تبويب «التحديد».` : `${what} selected; its details are in Selection.`);
}

/* Escape on the board: the selection is cleared when there is one; otherwise
   the key is not the board's (it passes through). */
function clearSelectionFromKeyboard() {
  if (!TW.selectedPlacementId) return false;
  TW.selectedPlacementId = null;
  markSelection();
  redrawSelection();
  announceBoard(IS_AR ? 'أُلغي التحديد.' : 'Selection cleared.');
  return true;
}

/* Enter on a list row (or a click on its "Place…"): a complete course says
   so; an English row opens its weekly pattern; a row of the unplaced list
   says how to place it (keyboard placement needs a section budget); any
   other row opens Place… (5f) through the seam, the row its origin. */
function activateListRow(cell) {
  if (cell.getAttribute('aria-disabled') === 'true') {
    announceBoard(PLACE_WORDS.allPlaced(isolatedText(cell.dataset.course || '')));
    return;
  }
  if (typeof cell._twEnglish === 'function') {
    if (!workspaceCanEdit()) {
      announceBoard(TW.scenario?.status === 'published'
        ? (IS_AR ? 'هذا السيناريو منشور ولا يمكن تعديله.' : 'This scenario is published and read-only.')
        : (IS_AR ? 'انتظر انتهاء العملية الحالية.' : 'Wait for the current operation to finish.'));
      return;
    }
    // Back on the list after the chooser, unless the user went elsewhere.
    Promise.resolve(cell._twEnglish()).finally(() => {
      const active = document.activeElement;
      if (!active || active === document.body || $('twSectionList').contains(active)) listNav?.restore();
    });
    return;
  }
  if (String(cell.dataset.focusKey || '').startsWith('ts:') || !cell._twPlace) {
    announceBoard(PLACE_WORDS.needsBudget);
    return;
  }
  TW.keyActions.place(cell._twPlace, { origin: cell.dataset.focusKey });
}

/* ── Card actions: one gate, one seam (design record §7.3.2, 5c, 5e) ──
   Move (M on a card, or Selection's Move…) and Change room (R, or
   Selection's button) go through TW.keyActions[kind](placement, {origin})
   after one gate, whose words are the same wherever it refuses; Place… (a
   list row, or a drop of a course that meets more than once a week) goes
   through TW.keyActions.place(request, {origin}) after the gate's parts
   that apply to a section not placed yet (5f).
   `origin` is a focus key, never a node: the page may draw the card or the
   button again while a dialog is open, and the keyboard goes back to
   whatever answers to that key when it closes. */
const REFUSAL = {
  published: IS_AR ? 'هذا السيناريو منشور ولا يمكن تعديله.' : 'This scenario is published and read-only.',
  loading: IS_AR ? 'اللوحة ما زالت قيد التحميل.' : 'The board is still loading.',
  job: IS_AR ? 'مهمة المخطِّط قيد التشغيل؛ يمكن التعديل بعد انتهائها.' : 'A planner job is running; edits wait until it ends.',
  busy: IS_AR ? 'انتظر انتهاء العملية الحالية.' : 'Wait for the current operation to finish.',
  external: IS_AR ? 'يملك سيناريو آخر وقت هذه الشعبة وقاعتها؛ عدّلها هناك.' : "Another scenario owns this section's time and room; change it there.",
  locked: IS_AR ? 'وقت هذه الشعبة مقفل. افتح قفل الوقت لنقلها؛ ويمكن تغيير قاعتها.' : "This section's time is locked. Unlock its time to move it; its room can still change.",
  online: IS_AR ? 'هذه الشعبة عن بُعد؛ لا تحتاج إلى قاعة.' : 'This section is online; it needs no room.',
  noRoom: IS_AR ? 'هذه الشعبة لا تحتاج إلى قاعة.' : 'This section needs no room.',
};
// Not this section's action at all: Selection draws no button for it.
const NOT_APPLICABLE = new Set(['online', 'noRoom']);

/* '' or {code, text}; the first rule that applies wins. A write in flight or
   a board refresh never refuses: opening a dialog only reads, and its commit
   waits for them on its own (busy, through data-tw-mutation). A time lock
   holds Move only: a lock freezes days and times, never the room. */
function cardActionRefusal(kind, p) {
  let code = '';
  if (TW.scenario?.status === 'published') code = 'published';
  else if (!TW.boardId || TW.loadedBoardId !== TW.boardId) code = 'loading';
  else if (ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status)) code = 'job';
  else if (p?.shared_with_other_scenario) code = 'external';
  else if (kind === 'move' && p?.is_locked) code = 'locked';
  else if (kind === 'room' && p?.is_online) code = 'online';
  else if (kind === 'room' && p?.needs_room === false) code = 'noRoom';
  return code ? { code, text: REFUSAL[code] } : '';
}

/* Any dialog: a template one, the shared `dlg` ones or the English chooser.
   Keys and dialog openers wait for it to close. */
function anyDialogOpen() {
  return Boolean(TW.englishPicker || document.querySelector('dialog[open], .dlg-backdrop'));
}

/* A letter key on a board stop. False leaves the key alone: a dialog is
   open, the stop is an empty cell, or the action is not built yet. A refusal
   is a toast — no dialog is open to say it in. */
function runCardKey(kind, card) {
  if (anyDialogOpen()) return false;
  const p = TW.placements.find(row => String(row.id) === card.dataset.placementId);
  const action = TW.keyActions?.[kind];
  if (!p || typeof action !== 'function') return false;
  const refusal = cardActionRefusal(kind, p);
  if (refusal) {
    notify.warning(refusal.text);
    return true;
  }
  action(p, { origin: `placement:${p.id}` });
  return true;
}
TW.keyActions = {
  move: (p, options) => openMoveDialog(p, options),
  room: (p, options) => openRoomDialog(p, options),
  place: (request, options) => openPlaceDialog(request, options),
};

/* The element an origin key names now, else its region's own stop (or the
   inspector body, which takes the keyboard). */
function resolveOrigin(origin) {
  const key = String(origin || '');
  if (key.startsWith('placement:')) return boardNav?.element(key) || boardNav?.element() || null;
  if (key.startsWith('place:') || key.startsWith('ts:')) return listNav?.element(key) || listNav?.element() || null;
  const sel = /^sel:([^:]+):(.+)$/.exec(key);
  if (!sel) return null;
  const body = $('twRightBody');
  return [...body.querySelectorAll('[data-focus-key][data-placement-id]')]
    .find(element => element.dataset.focusKey === sel[1] && element.dataset.placementId === sel[2]) || body;
}

/* Why the page cannot write now, in the gate's words (a key of REFUSAL), or ''. */
function writeHold() {
  if (TW.scenario?.status === 'published') return 'published';
  if (ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status)) return 'job';
  return workspaceCanEdit() ? '' : 'busy';
}

/* A dialog's commit (contract C): {data}, or {code, status} for every
   failure (status 0: no answer was read), with nothing toasted (a toast
   behind a modal is never seen). The keyboard goes
   to the chosen row first, since the commit button turns off as the write
   starts. twFetch's own busy guard would toast, so the hold is checked here
   and answered as a code like any refusal. */
async function dialogWrite(dialog, url, body) {
  const hold = writeHold();
  if (hold) return { code: hold };
  (dialog.querySelector('input[type="radio"]:checked') || dialog.querySelector('.ux-choice-list'))?.focus({ preventScroll: true });
  let code = '', failed = 0;
  const data = await twFetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), _quiet: true,
    _onError: (answer, status) => {
      code = answer?.error?.code || (answer ? String(status) : 'NETWORK');
      failed = Number(status) || 0;
      return true;
    },
  });
  return data ? { data } : { code: code || 'NETWORK', status: failed };
}

/* What a dialog's work came to: in its alert line while it is open. Once the
   user has left it (Escape while its write was out, D17a), in the alert line
   of the helper dialog open now, if any, since a toast behind a modal is
   never seen (D17); else a toast. A helper dialog says what it is about
   (`data-tw-about`, as `placement:<id>`), and the same dialog may be open on
   another meeting by then: in a dialog about anything but `about.key`, the
   words lead with `about.name` (the same form in Arabic), so they never read
   as that dialog's own. The line takes the tone, so a saved change never
   reads as a refusal. Clearing is only ever about the dialog itself. */
function dialogSay(dialog, text, tone = 'warning', about = null) {
  const open = sayingDialog(dialog, text);
  const line = open?.querySelector('[role="alert"]');
  if (line) {
    line.textContent = text && about && open.dataset.twAbout !== about.key ? `${about.name}: ${text}` : text;
    line.hidden = !text;
    line.dataset.tone = tone;
    return;
  }
  if (text) notify[tone](text);
}
/* The helper dialog whose alert line would carry `text` (dialogSay's
   choice), or null when it would be a toast. */
function sayingDialog(dialog, text) {
  const shown = element => Boolean(element && window.UXControls?.dialog?.isOpen(element));
  const open = shown(dialog) ? dialog : text ? [...document.querySelectorAll('dialog[open]')].find(shown) : null;
  return open?.querySelector('[role="alert"]') ? open : null;
}

/* Preview reads (contract E): one request in flight per key, which a second
   open joins; none is aborted (the server's preview lane frees its token
   only when the view returns). An answer is kept for the data version it was
   read at, until a board answer arrives (selectBoard drops the settled
   ones); `fresh` (Retry, Check again) skips a kept answer but still joins a
   read in flight. A failure is not kept: the next open asks again. Each
   read answers {data} or {code, status}. */
const PREVIEW = new Map();
function previewRead(key, url, { fresh = false } = {}) {
  const memo = `${key}|${TW.dataVersion}`;
  const known = PREVIEW.get(memo);
  if (known && (!known.settled || !fresh)) return known.promise;
  const entry = { settled: false, promise: null };
  let failure = null;
  entry.promise = twFetch(url, {
    _quiet: true,
    _onError: (body, status) => {
      failure = { code: body?.error?.code || body?.code || (status ? String(status) : 'NETWORK'), status };
      return true;
    },
  }).then(data => {
    entry.settled = true;
    if (data) return { data };
    if (PREVIEW.get(memo) === entry) PREVIEW.delete(memo);
    return failure || { code: 'NETWORK', status: 0 };
  });
  PREVIEW.set(memo, entry);
  return entry.promise;
}
function dropSettledPreviews() {
  PREVIEW.forEach((entry, memo) => { if (entry.settled) PREVIEW.delete(memo); });
}

// Expose workspace state for the PR8 async-job UI adapter (reads
// TW.scenarioId at submit time so the Run-in-background card targets
// whatever scenario the registrar currently has active).
window.TW = TW;

/* One owner for operations that can replace timetable rows. Read-only board
 * inspection and export remain available while the owner finishes. State is
 * derived afresh when controls are rendered, so a newly rendered Lock button
 * cannot escape the same guard and published scenarios never become editable. */
const TW_ACTIVITY = { owner: null, writes: 0, adapterPending: false, refresh: false, loading: 0, boardLoading: 0 };
let scenarioListRevision = 0;
const ACTIVE_PLANNER_STATUSES = new Set(['queued', 'running', 'unknown']);
function workspaceBusy() {
  return Boolean(TW_ACTIVITY.owner || TW_ACTIVITY.writes || TW_ACTIVITY.adapterPending
    || TW_ACTIVITY.refresh || TW_ACTIVITY.loading || TW_ACTIVITY.boardLoading || ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status));
}
function workspaceCanEdit() {
  return Boolean(TW.scenarioId && TW.scenario?.status !== 'published' && !workspaceBusy());
}
function syncWorkspaceControls() {
  const busy = workspaceBusy();
  TW.busy = busy;
  const editable = workspaceCanEdit();
  if ($('twCopyScenario')) $('twCopyScenario').disabled = !TW.scenarioId || !TW.scenario || busy;
  ['twGenerate', 'twGenerateRun', 'twEmptyGenerate', 'twMapElectives', 'twNewScenario', 'twScenario',
    'twYear', 'twTerm'].forEach(id => {
    if ($(id)) $(id).disabled = busy;
  });
  ['twNewBoard', 'twSlotEdit', 'twOptimise', 'twOptimiseMenu'].forEach(id => {
    if ($(id)) $(id).disabled = !editable;
  });
  if ($('twUndo')) $('twUndo').disabled = !editable || !TW.undoStack.length;
  if ($('twRedo')) $('twRedo').disabled = !editable || !TW.redoStack.length;
  // `data-tw-hold`: a control that is off for its own reason as well (a
  // save in flight, a section another scenario owns) stays off.
  document.querySelectorAll('.tw-opt-item, [data-tw-mutation]').forEach(el => { el.disabled = !editable || el.hasAttribute('data-tw-hold'); });
  document.querySelectorAll('[data-tw-draggable]').forEach(el => { el.draggable = editable && el.dataset.twMovable !== 'false' && !el.classList.contains('locked'); });
  const workspaceEl = document.querySelector('.tw-workspace');
  if (workspaceEl) workspaceEl.dataset.busy = String(busy);
  updateOpenLinks();
  renderPublishState();
  window.dispatchEvent(new CustomEvent('tw:planner-busy', { detail: { busy } }));
  // Selection's actions and an open dialog's commit follow the same state.
  syncSelectionActions();
  syncMoveDialog();
  syncRoomDialog();
  syncPlaceDialog();
}

/* The other views of this timetable are real links, so they open in a new tab
   on a middle click, and each carries the scenario (and board) being looked at. */
function workspaceViewHref(path, withBoard = true) {
  const params = new URLSearchParams();
  if (TW.scenarioId) params.set('scenario', String(TW.scenarioId));
  if (withBoard && TW.boardId) params.set('board', String(TW.boardId));
  const query = params.toString();
  return path + (query ? `?${query}` : '');
}
function updateOpenLinks() {
  if ($('twMriPage')) $('twMriPage').href = workspaceViewHref('/timetable-workspace/mri/');
  if ($('twGraphPage')) $('twGraphPage').href = workspaceViewHref('/timetable-workspace/graph/', false);
  if ($('twSplitPage')) $('twSplitPage').href = workspaceViewHref('/timetable-workspace/split/');
}
function beginWorkspaceOperation(kind) {
  if (workspaceBusy()) {
    notify.warning(IS_AR ? 'انتظر انتهاء العملية الحالية.' : 'Wait for the current operation to finish.');
    return null;
  }
  const token = { kind, scenarioId: TW.scenarioId };
  TW_ACTIVITY.owner = token;
  syncWorkspaceControls();
  return token;
}
function finishWorkspaceOperation(token) {
  if (TW_ACTIVITY.owner === token) TW_ACTIVITY.owner = null;
  syncWorkspaceControls();
}
function announcePlannerJob(job, scenarioId, jobId) {
  window.dispatchEvent(new CustomEvent('tw:planner-job', {
    detail: { job, scenarioId, jobId: jobId || job?.job_id, source: 'workspace' },
  }));
}
window.addEventListener('tw:planner-request', event => {
  TW_ACTIVITY.adapterPending = Boolean(event.detail?.pending);
  syncWorkspaceControls();
});
window.addEventListener('tw:planner-job', async event => {
  const { job, scenarioId, source } = event.detail || {};
  if (String(scenarioId) !== String(TW.scenarioId)) return;
  const previous = TW.plannerJob;
  if (previous && job && previous.job_id === job.job_id
      && !ACTIVE_PLANNER_STATUSES.has(previous.status) && ACTIVE_PLANNER_STATUSES.has(job.status)) return;
  // A late poll from an older job must never replace a newly submitted job.
  if (previous && job && previous.job_id !== job.job_id
      && ACTIVE_PLANNER_STATUSES.has(previous.status)) return;
  TW.plannerJob = job || null;
  if (source !== 'workspace' && job && !ACTIVE_PLANNER_STATUSES.has(job.status)
      && previous && ACTIVE_PLANNER_STATUSES.has(previous.status) && !TW_ACTIVITY.owner) {
    TW_ACTIVITY.refresh = true;
    syncWorkspaceControls();
    TW.undoStack = []; TW.redoStack = [];
    try { await onScenarioChange(); } finally { TW_ACTIVITY.refresh = false; }
  }
  syncWorkspaceControls();
});

/* ── API ── */
async function twFetch(url, opts = {}) {
  // `_quiet`: a background read whose failure only means "unknown" — no toast.
  // `_changed(body)`: a write the server answers "nothing changed" leaves the
  // data version, so every answer read before it stays current.
  // `_onError(body, status)`: a caller that can say more than a toast takes
  // the failure instead (true = handled: no toast). It hears of every
  // failure the server or the network gives, since a dialog keeps its
  // messages inside itself (a toast behind a modal is never seen): `body` is
  // null when a refusal is not JSON (a proxy's error page); status 0 means
  // no answer could be read — the request failed, or a success came back
  // that is not JSON (an expired session's login page).
  // It does not hear the local busy guard below, which toasts and returns
  // null before any request is made. Today's callers (publish, drop, the slot
  // editor) use no dialog or the page's `dlg` ones, which sit under the
  // toasts and leave them announced, so that toast is seen even when the
  // slot editor reopens on it. A template <dialog> opened modal
  // (UXControls.dialog) is in the top layer, over every toast: it keeps its
  // commit off while the workspace is busy (data-tw-mutation, which
  // syncWorkspaceControls turns off) and checks workspaceCanEdit() before it
  // writes, or it gets null with nothing said inside it.
  // It is asked at most once. A handler that throws has not handled the
  // failure: the slip is logged, and the toast follows as if there were no
  // handler (the server's message; none for a quiet request).
  const { _workspaceToken, _onError, _quiet, _changed, ...requestOptions } = opts;
  let asked = false;
  const handled = (body, status) => {
    if (asked || typeof _onError !== 'function') return false;
    asked = true;
    try {
      return Boolean(_onError(body, status));
    } catch (slip) {
      console.error('[twFetch] the _onError handler threw', slip);
      return false;
    }
  };
  const writes = !['GET', 'HEAD'].includes((opts.method || 'GET').toUpperCase());
  if (writes && workspaceBusy() && (!TW_ACTIVITY.owner || _workspaceToken !== TW_ACTIVITY.owner)) {
    notify.warning(IS_AR ? 'الجدول قيد التحديث؛ انتظر انتهاء العملية.' : 'The timetable is being updated; wait for the operation to finish.');
    return null;
  }
  if (writes) { TW_ACTIVITY.writes++; syncWorkspaceControls(); }
  // Wrap safeFetch with better error extraction for our {ok, error: {code, message}} format
  try {
    let response = await fetch(url, _injectCsrf(requestOptions));
    let busyBody = null;
    // A heavy preview (slot candidates) refused because the server's preview
    // lane is full is asked again after Retry-After; a read is safe to repeat.
    for (let attempt = 0; !writes && response.status === 503 && attempt < 6; attempt++) {
      try { busyBody = await response.json(); } catch { busyBody = null; }
      if (busyBody?.code !== 'SERVER_BUSY' && busyBody?.error?.code !== 'SERVER_BUSY') break;
      const seconds = Number(response.headers?.get?.('Retry-After')) || 2;
      await new Promise(resolve => setTimeout(resolve, Math.min(30, Math.max(1, seconds)) * 1000));
      busyBody = null;
      response = await fetch(url, _injectCsrf(requestOptions));
    }
    if (!response.ok) {
      // The retry loop above may have read the body already (a body is read
      // once). One that is not JSON reads as null.
      let d = busyBody;
      if (!d) { try { d = await response.json(); } catch { d = null; } }
      const e = d?.error;
      // A planner job holding the scenario is announced before the caller
      // takes the refusal: a dialog that says "busy" in its own words, or a
      // quiet request, still leaves the page showing the job and holding edits.
      if (e?.code === 'SCENARIO_BUSY' && e.details?.active_job) {
        announcePlannerJob(e.details.active_job, e.details.scenario_id, e.details.active_job.job_id);
      }
      // Publish lists the server's blockers; a dialog keeps the refusal inside itself.
      if (handled(d, response.status)) return null;
      if (_quiet) return null;
      let msg = (e && typeof e === 'object') ? (e.message || e.code || JSON.stringify(e)) : (e || d?.message || `${response.status}`);
      const seedLabel = runSeedText(e?.details || d);
      if (seedLabel) msg += ` · ${seedLabel}`;
      notify.error(msg);
      return null;
    }
    const body = await response.json();
    if (writes && (typeof _changed !== 'function' || _changed(body))) TW.dataVersion++;
    return body;
  } catch (err) {
    // No answer could be read: fetch rejected (offline, the connection
    // dropped), or a 2xx came back whose body is not JSON. fetch follows a
    // redirect, so a session that expired mid-request lands here with the
    // login page's HTML under a 200. Either way there is no refusal to hand
    // over, and _onError(null, 0) is the intended answer.
    if (handled(null, 0)) return null;
    if (!_quiet) notify.error(err.message || String(err));
    return null;
  } finally {
    if (writes) { TW_ACTIVITY.writes--; syncWorkspaceControls(); }
  }
}

const _sleep = ms => new Promise(r => setTimeout(r, ms));

/* Poll an async planner job until it reaches a terminal status. Uses a raw
   fetch (not twFetch) so transient poll blips don't spam error toasts during a
   long solve. Calls onTick(job, elapsedMs) each poll for progress UI. */
/* Losing contact does not prove a job failed. Keep its write guard in place
   until a later poll confirms its terminal status; the card keeps monitoring. */
async function cancelPlannerJob(jobId) {
  try {
    await fetch(`/planner-jobs/${jobId}/cancel/`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': CSRF },
    });
  } catch { /* best-effort */ }
}

async function pollPlannerJob(jobId, onTick, { intervalMs = 2000, maxMs = 1800000, maxConsecutiveErrors = 8, scenarioId = TW.scenarioId } = {}) {
  const start = Date.now();
  let consecutiveErrors = 0;
  while (Date.now() - start < maxMs) {
    let job = null;
    try {
      const r = await fetch(`/planner-jobs/${jobId}/`, { credentials: 'same-origin' });
      if (r.ok) job = await r.json();
    } catch { /* transient — tolerate below */ }
    if (!job) {
      if (++consecutiveErrors >= maxConsecutiveErrors) {
        const unknown = { job_id: jobId, status: 'unknown', error_message: 'Connection lost; planner status is unconfirmed.' };
        announcePlannerJob(unknown, scenarioId, jobId);
        return unknown;
      }
    } else {
      consecutiveErrors = 0;
      announcePlannerJob(job, scenarioId, jobId);
      onTick && onTick(job, Date.now() - start);
      if (job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled') {
        return job;
      }
    }
    // Sleep AFTER checking, so a job that is already finished (the sync-dispatch
    // path under tests, or a fast "current" run) returns without paying a full
    // interval of latency.
    await _sleep(intervalMs);
  }
  const unknown = { job_id: jobId, status: 'unknown', error_message: 'Monitoring timed out; planner status is unconfirmed.' };
  announcePlannerJob(unknown, scenarioId, jobId);
  return unknown;
}

/* Single optimise request. BOTH modes run off the request thread as a planner
   job and are polled.

   "current" used to run synchronously on the assumption it was fast. It is not:
   measured at ~7 minutes on a 400-student scenario, against gunicorn's
   `--timeout 120`. The worker is SIGKILLed long before it returns — and because
   optimise_current_timetable persists INSIDE the run, the kill lands after the
   board is written but before run_v2_optimisation_guarded can evaluate its
   regression gate and roll back. So the sync path could leave a persisted,
   ungated board with the registrar seeing only a dead request. This is the main
   Optimise button, so that was the most-used path in the workspace.

   Returns the same {ok, optimisation} envelope the sync path returns, so the
   results dialog is identical either way. */
async function runOptimiseRequest(mode, payload, btn, modeLabel, token) {
  const scenarioId = token.scenarioId;
  const url = `/ops/tw/scenarios/${scenarioId}/optimise-v2/`;
  const post = b => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
  const sub = await twFetch(url, { ...post({ ...payload, async_run: true }), _workspaceToken: token });
  if (sub === null) return null;  // HTTP error — twFetch already toasted
  if (sub.job_id) {
    announcePlannerJob({ job_id: sub.job_id, status: sub.status || 'queued' }, scenarioId, sub.job_id);
    const job = await pollPlannerJob(sub.job_id, (j, ms) => {
      const secs = Math.round((ms || 0) / 1000);
      const detail = j.last_stage_seen ? ` (${j.last_stage_seen})` : (secs ? ` (${secs}s)` : '');
      btn.textContent = `${modeLabel}${detail}`;
    }, { scenarioId });
    if (job.status === 'succeeded') {
      const res = await twFetch(`/planner-jobs/${sub.job_id}/result/`);
      return (res && res.result)
        ? { ok: true, optimisation: res.result }
        : { ok: false, error: { code: 'NO_RESULT', message: 'job finished but result was missing' } };
    }
    const code = job.status === 'cancelled' ? 'CANCELLED' : 'FAILED';
    const message = [job.error_message || job.status, runSeedText({ seed: job.seed ?? sub.seed })].filter(Boolean).join(' · ');
    if (job.status === 'unknown') return { ok: false, error: { code: 'STATUS_UNCONFIRMED', message } };
    return { ok: false, error: { code, message, details: { seed: job.seed } } };
  }
  // No job_id: the PR7 flag is off, so the view IGNORED async_run and ran the
  // pipeline synchronously in THIS request — `sub` already is the completed
  // {ok, optimisation} envelope. Re-POSTing here (as an earlier cut did) would
  // run the whole ~7-minute optimisation a SECOND time and discard the first
  // result, in precisely the state settings.py documents as the runtime revert
  // lever. The server does the work either way; the only question is whether we
  // throw it away.
  return sub;
}

/* Timetable outcome helpers — pure renderers, also used by the split workspace. */
function runSeedText(result) {
  const seed = result?.seed;
  return Number.isInteger(seed) && seed >= 0 && seed <= 2147483647
    ? `${IS_AR ? 'بذرة التشغيل' : 'Run seed'}: ${seed}` : '';
}

function renderRunSeed(result) {
  const label = runSeedText(result);
  return label ? `<div class="tw-run-seed" style="margin-bottom:12px"><b>${label}</b><div style="font-size:11px;color:var(--t3)">${IS_AR ? 'احتفظ بهذه البذرة لإعادة استخدام نقطة بدء البحث. قد تختلف النتائج بسبب حدود الوقت.' : 'Keep this seed to reuse the search starting point. Time limits may still change the outcome.'}</div></div>` : '';
}

function instructorScheduleOf(result) {
  return result.instructor_schedule_metric || result.instructor_schedule || {};
}

function instructorScheduleImproved(result) {
  const { before, after } = instructorScheduleOf(result);
  return !result.safety_blocked && before && after && (
    (Number.isFinite(before.working_days) && Number.isFinite(after.working_days)
      && after.working_days < before.working_days)
    || (Number.isFinite(before.idle_minutes) && Number.isFinite(after.idle_minutes)
      && after.idle_minutes < before.idle_minutes)
  );
}

function secondaryRecoveryImproved(result) {
  const recovery = result.secondary_recovery;
  return !result.safety_blocked && recovery?.applied && (
    recovery.soft_unresolved_after < recovery.soft_unresolved_before
    || recovery.room_shortages_after < recovery.room_shortages_before
  );
}

function renderSecondaryRecoveryOutcome(result) {
  if (!secondaryRecoveryImproved(result)) return '';
  const recovery = result.secondary_recovery;
  const number = value => Number.isFinite(value) ? value.toLocaleString() : '—';
  return `<div style="padding:10px 12px;margin-bottom:12px;border:1px solid var(--line);border-radius:8px">
    <b>${IS_AR ? 'تحسين المقاعد والقاعات' : 'Seat and room recovery'}</b>
    <div>${IS_AR ? 'مقاعد المقررات المرنة غير المعينة' : 'Soft-course unresolved seats'}: ${number(recovery.soft_unresolved_before)} → ${number(recovery.soft_unresolved_after)}</div>
    <div>${IS_AR ? 'الاجتماعات دون قاعة مناسبة' : 'Meetings without a suitable room'}: ${number(recovery.room_shortages_before)} → ${number(recovery.room_shortages_after)}</div>
    <div style="font-size:11px;color:var(--t3)">${IS_AR ? 'يشمل نقص القاعات القاعات الأصغر من السعة المطلوبة. تمت حماية أيام وفراغات كل مدرس.' : 'Room shortages include undersized rooms. Each instructor’s days and gaps were protected.'}</div>
  </div>`;
}

/* One teacher per section: who teaches what, beside the days and idle — a
 * section left without a teacher must never make those numbers look better
 * unseen. Present only with the rule on. */
function teacherCoverageOf(result) {
  return instructorScheduleOf(result).coverage?.after || result.section_teachers?.coverage || null;
}

function teacherCoverageText(coverage) {
  const number = value => Number.isFinite(value) ? value.toLocaleString() : '—';
  const assigned = number(coverage.assigned);
  const needing = number((coverage.assigned || 0) + (coverage.tbd || 0));
  return IS_AR
    ? `شعب لها مدرس: ${assigned} / ${needing} · لم يُحدَّد بعد: ${number(coverage.tbd)} (D10: ${number(coverage.tbd_d10_floor)})`
    : `Sections with a teacher: ${assigned} / ${needing} · to be decided: ${number(coverage.tbd)} (D10: ${number(coverage.tbd_d10_floor)})`;
}

function renderInstructorOutcome(result) {
  const metric = instructorScheduleOf(result);
  const after = metric.after || result.quality_score?.instructor_schedule;
  const before = metric.before || result.baseline_instructor_schedule;
  const priority = result.optimisation_policy === 'instructor_first';
  const coverage = teacherCoverageOf(result);
  if (!after && !priority && !coverage) return '';
  const number = value => Number.isFinite(value) ? value.toLocaleString() : '—';
  const row = (label, key) => `<div>${label}: <b>${before ? `${number(before[key])} → ` : ''}${number(after?.[key])}</b></div>`;
  return `<div style="padding:10px 12px;margin-bottom:12px;border:1px solid var(--line);border-radius:8px">
    <div style="font-weight:700;color:var(--teal)">${IS_AR ? 'جدول المدرسين' : 'Instructor schedule'}</div>
    ${row(IS_AR ? 'مجموع أيام العمل' : 'Total working days', 'working_days')}
    ${row(IS_AR ? 'دقائق الانتظار' : 'Idle minutes', 'idle_minutes')}
    ${coverage ? `<div>${esc(teacherCoverageText(coverage))}</div>` : ''}
    ${priority ? `<div style="font-size:11px;color:var(--t3);margin-top:5px">${IS_AR ? 'الأولوية لتقليل أيام المدرسين ثم الانتظار، مع حماية التسجيل والقيود الأساسية. قد تتغير فراغات الطلاب.' : 'Priority: fewer instructor days, then less idle time, with hard enrolment and operational constraints protected. Student gaps may change.'}</div>` : ''}
  </div>`;
}

function schedulerSeatingText(result) {
  const seating = result.student_seating;
  if (!seating || !seating.solution_found) {
    return IS_AR ? 'تعيين الطلاب الفعلي لم يُؤكد؛ المقاعد الناقصة والتعارضات غير معروفة.'
      : 'Actual student seating not confirmed; missing seats and clashes are unknown.';
  }
  const number = value => Number.isFinite(value) ? value.toLocaleString() : (IS_AR ? 'غير معروف' : 'unknown');
  const proof = seating.proven_optimal
    ? (IS_AR ? 'الحل الأمثل مثبت' : 'optimum proven')
    : (IS_AR ? 'حل ممكن؛ المثالية غير مثبتة' : 'feasible solution; optimality not proven');
  return (IS_AR ? 'تعيين الطلاب الفعلي: ' : 'Actual student seating: ')
    + `${proof}; ${IS_AR ? 'الطلاب المعينون بالكامل' : 'fully seated students'} ${number(seating.students_fully_seated)}/${number(seating.students)}; `
    + `${IS_AR ? 'المقاعد الناقصة' : 'missing seats'} ${number(seating.unseated_demands)}; `
    + `${IS_AR ? 'التعارضات' : 'clashes'} ${number(seating.total_clashes)}.`;
}

function schedulerOutcomeText(result) {
  const bits = [];
  if (runSeedText(result)) bits.push(runSeedText(result));
  if (result.instructor_days != null) bits.push((IS_AR ? 'مجموع أيام المدرسين: ' : 'Total instructor working days: ') + result.instructor_days);
  if (result.instructor_idle_minutes != null) bits.push((IS_AR ? 'دقائق الانتظار: ' : 'Instructor idle minutes: ') + result.instructor_idle_minutes);
  if (result.unroomed != null) bits.push((IS_AR ? 'بلا قاعة: ' : 'Unroomed: ') + result.unroomed
    + (result.unroomed_floor != null ? (IS_AR ? ' (الحد الأدنى: ' : ' (floor: ') + result.unroomed_floor + ')' : ''));
  if (result.sections_on_the_same_hour_percent != null) bits.push((IS_AR ? 'نفس التوقيت طوال الأسبوع: ' : 'Same hour all week: ') + result.sections_on_the_same_hour_percent + '%');
  bits.push(schedulerSeatingText(result));
  if (result.expected_clashes != null) bits.push((IS_AR ? 'تقدير التعارضات (تقريبي): ' : 'Estimated overlaps (proxy): ') + result.expected_clashes);
  return bits.join(' | ');
}
/* End timetable outcome helpers. */

/* What the new engine actually achieved, said out loud.
 *
 * The scheduler returns a full report — instructor days against their proven
 * floor, unroomed meetings against theirs, how many sections keep the same hour
 * all week — and a `notes` list for anything it had to give up. None of it was
 * reaching the screen: a build that quietly dropped a rule looked identical to
 * one that met it. Notes are shown as warnings for exactly that reason.
 */
async function showSchedulerOutcome(jobId) {
  // Deliberately NOT twFetch: that helper toasts an error on any non-2xx, and a
  // hiccup fetching this summary would then show a red failure for a build that
  // succeeded. This is a nice-to-have on top of a finished job; if it cannot be
  // fetched, say nothing.
  let r = null;
  try {
    const res = await fetch(`/planner-jobs/${jobId}/result/`, { credentials: 'same-origin' });
    if (res.ok) r = (await res.json()).result;
  } catch { /* the build succeeded; the summary is decoration */ }
  if (!r) {
    $('twGenStatus').textContent = schedulerSeatingText({});
    return;
  }

  $('twGenStatus').textContent = schedulerOutcomeText(r);
  // `warnings` only, never `notes`. Notes are a mixed channel — every successful
  // two-pass run appends one ("19 working days held; idle 3255 -> 1635") — so
  // showing them as warnings would put an amber triangle on every good build and
  // teach the reader to ignore the one that matters.
  (r.warnings || []).forEach(warning => notify.warning(warning));
}

/* ── Init ──
   Menus open, close, place themselves and take the keyboard through the design
   system's ux-controls.js; this script only answers their items. */
(function init() {
  // New ▾ / empty state: generating is a dialog with its own year and term, so
  // preparing next term never clears the scenario that is open.
  $('twGenerate').addEventListener('click', openGenerateDialog);
  $('twEmptyGenerate')?.addEventListener('click', openGenerateDialog);
  wireGenerateDialog();
  $('twNewScenario').addEventListener('click', createScenarioPrompt);

  // ⋯ scenario actions
  $('twCopyScenario')?.addEventListener('click', copyScenarioPrompt);
  $('twSlotEdit').addEventListener('click', openSlotEditor);

  // Scenario picker and its scope
  $('twScenario').addEventListener('change', onScenarioChange);
  ['twYear', 'twTerm'].forEach(id => $(id).addEventListener('change', loadScenarios));

  // Optimise: the main button improves the current timetable; Full rebuild
  // replaces every unlocked placement, so it asks first.
  $('twOptimise').addEventListener('click', () => runOptimiseV2('current'));
  document.querySelectorAll('.tw-opt-item').forEach(item => {
    item.addEventListener('click', () => runOptimiseMode(item.dataset.mode));
  });
  // Export: the main button follows the active filter / programme lens.
  $('twExport').addEventListener('click', () => exportScenario('per-plan'));
  document.querySelectorAll('.tw-export-item').forEach(item => {
    item.addEventListener('click', () => exportScenario(item.dataset.mode));
  });
  $('twPublish').addEventListener('click', publishScenario);

  // Board bar
  $('twNewBoard').addEventListener('click', createBoardPrompt);
  // The strip changes width without a render (a window resize, a longer
  // filter count beside it): keep the chosen board in view.
  if (window.ResizeObserver) {
    const strip = $('twBoardTabs');
    new ResizeObserver(() => {
      const current = strip.querySelector('[aria-current="true"]');
      if (current) revealInStrip(strip, current);
    }).observe(strip);
  }
  // The filter is a view over the board (§7.2 item 6, 6c): it folds and dims in
  // place (applyGridView, which also writes the count), never redraws it, so
  // a drag or a focused card survives typing.
  $('twSearch')?.addEventListener('input', debounce(() => {
    applyGridView();
    renderBudgetSidebar();
  }, 120));
  // A fold the user opens or shuts stays so for this board and text, and the
  // count's words say how many tables are still folded.
  $('twGrid').addEventListener('ux-disclosure', event => {
    const key = event.target.closest?.('.tw-fold-toggle')?.dataset.foldKey;
    if (!key) return;
    if (event.detail?.open) TW.foldOpen.add(key); else TW.foldOpen.delete(key);
    updateSearchStatus();
  });
  $('twSectionSearch').addEventListener('input', debounce(renderBudgetSidebar, 200));

  // Inspector tabs
  document.querySelectorAll('.tw-right .tw-panel-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tw-right .tw-panel-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      renderRightPanel(tab.dataset.tab);
    });
  });

  // Prime the selector on first paint using the default year/term so it is
  // never empty when the page loads.
  loadScenarios().then(restoreWorkspaceContext);
})();

async function runOptimiseMode(mode) {
  if (mode === 'full') {
    const ok = await dlg.confirm({
      title: IS_AR ? 'إعادة بناء كاملة؟' : 'Rebuild the whole scenario?',
      body: IS_AR
        ? 'تُستبدل كل الشعب غير المقفلة في كل لوحات هذا السيناريو بنتيجة جديدة (7 استراتيجيات وبحث محلي). الشعب المقفلة تبقى في أوقاتها. قد يستغرق ذلك عدة دقائق.'
        : 'Every unlocked placement on every board of this scenario is replaced by a new result (7 strategies plus local search). Locked sections keep their days and times. This can take several minutes.',
      kind: 'warning',
      confirmText: IS_AR ? 'إعادة البناء' : 'Rebuild',
    });
    if (!ok) return;
  }
  runOptimiseV2(mode);
}

/* ── Generate dialog ── */
function generateDialogValues() {
  const programmes = [...document.querySelectorAll('input[name="twGenProgramme"]:checked')]
    .map(box => box.value.trim().toUpperCase()).filter(Boolean);
  return {
    year: ($('twGenYear')?.value || '').trim(),
    term: ($('twGenTerm')?.value || '').trim(),
    programs: programmes,
    section: (document.querySelector('input[name="twGenSection"]:checked')?.value || '').toUpperCase(),
    engine: document.querySelector('input[name="twGenEngine"]:checked')?.value || 'classic',
  };
}
function generateDialogError(message) {
  const error = $('twGenError');
  if (!error) return;
  error.textContent = message || '';
  error.hidden = !message;
}
/* The new engine builds one section (M or F) at a time: "Both" is not offered
   with it, rather than refused after the click. */
function syncGenerateDialog() {
  const engine = document.querySelector('input[name="twGenEngine"]:checked')?.value || 'classic';
  const both = document.querySelector('input[name="twGenSection"][value=""]');
  if (!both) return;
  both.disabled = engine === 'scheduler';
  if (both.disabled && both.checked) {
    const male = document.querySelector('input[name="twGenSection"][value="M"]');
    if (male) male.checked = true;
  }
}
function openGenerateDialog() {
  if (workspaceBusy()) {
    notify.warning(IS_AR ? 'انتظر انتهاء العملية الحالية.' : 'Wait for the current operation to finish.');
    return;
  }
  const dialog = $('twGenerateDialog');
  if (!dialog) return;
  $('twGenYear').value = $('twYear').value.trim();
  $('twGenTerm').value = $('twTerm').value.trim();
  // Start from what is open: its programmes and its section.
  const programs = (TW.scenario?.programs || []).map(code => String(code).trim().toUpperCase());
  if (programs.length) {
    document.querySelectorAll('input[name="twGenProgramme"]').forEach(box => {
      box.checked = programs.includes(box.value.trim().toUpperCase());
    });
  }
  const gender = String(TW.scenario?.gender || '').trim().toUpperCase();
  const genderRadio = document.querySelector(`input[name="twGenSection"][value="${gender === 'M' || gender === 'F' ? gender : ''}"]`);
  if (genderRadio) genderRadio.checked = true;
  syncGenerateDialog();
  generateDialogError('');
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
}
function closeGenerateDialog() {
  const dialog = $('twGenerateDialog');
  if (!dialog) return;
  if (typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
}
function wireGenerateDialog() {
  const dialog = $('twGenerateDialog');
  if (!dialog) return;
  dialog.querySelectorAll('[data-dialog-close]').forEach(button => button.addEventListener('click', closeGenerateDialog));
  dialog.querySelectorAll('input[name="twGenEngine"]').forEach(radio => radio.addEventListener('change', syncGenerateDialog));
  $('twGenerateForm')?.addEventListener('submit', event => {
    event.preventDefault();
    const request = generateDialogValues();
    if (!request.year || !request.term) {
      generateDialogError(IS_AR ? 'أدخل السنة والفصل.' : 'Enter the year and the term.');
      return;
    }
    if (!request.programs.length) {
      generateDialogError(IS_AR ? 'اختر برنامجاً واحداً على الأقل.' : 'Choose at least one programme.');
      return;
    }
    if (request.engine === 'scheduler' && !request.section) {
      generateDialogError(IS_AR ? 'المحرك الجديد يبني شعبة واحدة: اختر M أو F.' : 'The new engine builds one section at a time: choose M or F.');
      return;
    }
    closeGenerateDialog();
    runGenerate(request.engine, request);
  });
}

/* MRI/Graph/Split handoffs carry the saved scenario and board in the URL. */
async function restoreWorkspaceContext() {
  const params = new URLSearchParams(window.location.search);
  const scenarioId = params.get('scenario');
  if (!scenarioId || TW.scenarioId || workspaceBusy()) return;
  const select = $('twScenario');
  if (![...select.options].some(option => option.value === scenarioId)) return;
  select.value = scenarioId;
  await onScenarioChange();
  const boardId = params.get('board');
  const board = TW.boards.find(item => String(item.id) === boardId);
  if (board && String(TW.scenarioId) === scenarioId) await selectBoard(board.id);
}

/* ── Generate Workspace ── */
async function runGenerate(engine = 'classic', request = {}) {
  if ($('twGenerate').disabled) return;
  const year = String(request.year || '').trim();
  const term = String(request.term || '').trim();
  const program = (request.programs || []).join(',').toUpperCase();
  const section = String(request.section || '').trim().toUpperCase() || null;

  if (!year || !term || !program) {
    notify.error(IS_AR ? 'أدخل السنة والفصل والبرنامج' : 'Enter year, term, and program');
    return;
  }

  const token = beginWorkspaceOperation('generate');
  if (!token) return;
  try {
    $('twGenStatus').textContent = engine === 'scheduler'
      ? (IS_AR ? 'جاري التوليد بالمحرك الجديد...' : 'Generating with the new engine...')
      : (IS_AR ? 'جاري التوليد...' : 'Generating...');

    const data = await twFetch('/ops/tw/generate-workspace/', {
      _workspaceToken: token,
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        year: parseInt(year),
        semester: parseInt(term),
        program: program,
        section: section,
        // The one portfolio this generator has; the server reads it only when
        // it places sections itself, which a deferred build does not.
        strategy: 'compact',
        engine: engine,     // 'classic' = the original placer, 'scheduler' = the new engine
        defer_build: true,  // place sections via an async job (won't block the request)
      }),
    });

    if (!data) { $('twGenStatus').textContent = ''; return; }

    // Store generated data
    TW.generatedPlan = data.section_plan || [];
    TW.sectionBudget = data.section_budget || [];
    TW.studentSummary = data.student_summary || {};

    // Auto-set scenario
    TW.scenarioId = data.scenario.id;
    TW.scenario = data.scenario;
    TW.boards = data.boards || [];
    token.scenarioId = data.scenario.id;
    TW.boardId = null;
    TW.loadedBoardId = null;
    TW.selectedPlacementId = null;
    TW.undoStack = []; TW.redoStack = [];
    TW.quality = null; TW.qualityVersion = -1;
    // The new scenario's readiness is unknown: never show the previous one's.
    TW.readiness = null; TW.readinessVersion = -1;
    TW.plannerJob = null;
    if (data.build_job_id) announcePlannerJob({ job_id: data.build_job_id, status: 'queued' }, token.scenarioId, data.build_job_id);

    const ss = TW.studentSummary;
    $('twGenStatus').textContent = IS_AR
      ? `${ss.classified || 0} طالب | خطة: ${ss.on_plan || 0} | متقاطع: ${ss.cross_term || 0} | ${TW.boards.length} لوحات`
      : `${ss.classified || 0} students | On-plan: ${ss.on_plan || 0} | Cross-term: ${ss.cross_term || 0} | ${TW.boards.length} boards`;

    updateValidationBadge();
    $('twExport').disabled = false;
    $('twExportMenu').disabled = false;
    syncWorkspaceControls();

    // The new draft belongs to the year and term it was generated for: show
    // that scope, and the saved scenarios of THAT term beside it.
    const scopeChanged = $('twYear').value.trim() !== year || $('twTerm').value.trim() !== term;
    $('twYear').value = year;
    $('twTerm').value = term;
    const sel = $('twScenario');
    if (scopeChanged) await refreshScenarioOptions(data.scenario.id);
    if (![...(sel.options || [])].some(item => String(item.value) === String(data.scenario.id))) {
      const option = document.createElement('option');
      option.value = data.scenario.id; option.textContent = data.scenario.name;
      sel.appendChild(option);
    }
    sel.value = data.scenario.id;
    window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: TW.scenarioId, job: TW.plannerJob } }));

    // Render boards and sidebar
    renderBoardTabs();
    renderBudgetSidebar();

    // Auto-select first board
    if (TW.boards.length > 0) {
      await selectBoard(TW.boards[0].id);
    }

    // Deferred build: the heavy section placement (+ optimise + compaction) runs
    // as an async planner job so the request never blocks on a long multi-program
    // build. Poll it and refresh the board when it lands. (No build_job_id → the
    // build already ran synchronously, e.g. async planner disabled.)
    if (data.build_job_id) {
      const building = IS_AR ? 'جاري بناء الجدول ومقارنة المرشحين...' : 'Building timetable and comparing candidates...';
      $('twGenStatus').textContent = building;
      const job = await pollPlannerJob(data.build_job_id, (j, ms) => {
        const secs = Math.round((ms || 0) / 1000);
        const detail = ` (${secs}s)${j.last_stage_seen ? ` · ${j.last_stage_seen}` : ''}`;
        $('twGenStatus').textContent = building + detail;
      }, { scenarioId: token.scenarioId });
      if (job.status === 'succeeded') {
        if (String(TW.scenarioId) !== String(token.scenarioId)) return;
        await onScenarioChange();
        $('twGenStatus').textContent = '';
        notify.success(IS_AR ? 'تم التوليد والبناء' : 'Generated + built');
        if (engine === 'scheduler') await showSchedulerOutcome(data.build_job_id);
        else {
          const report = await twFetch(`/planner-jobs/${data.build_job_id}/result/`);
          if (report?.result) {
            $('twGenStatus').innerHTML = renderRunSeed(report.result) + renderInstructorOutcome(report.result);
          }
        }
      } else {
        const uncertain = job.status === 'unknown';
        $('twGenStatus').textContent = uncertain
          ? [IS_AR ? 'تعذر تأكيد حالة البناء؛ تستمر متابعة المخطط.' : 'Build status unconfirmed; the planner card continues monitoring.', runSeedText({ seed: job.seed ?? data.seed })].filter(Boolean).join(' · ')
          : (IS_AR ? 'لم يكتمل البناء: ' : 'Build did not complete: ') + [job.error_message || job.status, runSeedText({ seed: job.seed ?? data.seed })].filter(Boolean).join(' · ');
        if (uncertain) notify.warning($('twGenStatus').textContent);
        else { notify.error($('twGenStatus').textContent); await onScenarioChange(); }
      }
      return;
    }

    if (data.build_summary) {
      if (engine === 'scheduler') {
        $('twGenStatus').textContent = schedulerOutcomeText(data.build_summary);
        (data.build_summary.warnings || []).forEach(warning => notify.warning(warning));
      } else {
        $('twGenStatus').innerHTML = renderRunSeed(data.build_summary) + renderInstructorOutcome(data.build_summary);
      }
      notify.success(IS_AR ? 'تم التوليد والبناء' : 'Generated + built');
    } else {
      $('twGenStatus').textContent = IS_AR ? 'أُنشئ السيناريو؛ لم يبدأ البناء. استخدم إعادة البناء الكاملة.' : 'Scenario created; build did not start. Use Full rebuild.';
      notify.warning($('twGenStatus').textContent);
    }
    await refreshWorkspaceQuality();
  } catch (error) {
    notify.error(error.message || String(error));
  } finally {
    finishWorkspaceOperation(token);
  }
}

/* debounce() now in shared-utils.js */

/* ── Scenarios ── */
function persistWorkspaceScenario(scenarioId, boardId = null) {
  const url = new URL(window.location.href);
  if (scenarioId) url.searchParams.set('scenario', String(scenarioId));
  else url.searchParams.delete('scenario');
  if (scenarioId && boardId) url.searchParams.set('board', String(boardId));
  else url.searchParams.delete('board');
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
}
async function loadScenarios() {
  if (workspaceBusy()) return;
  const revision = ++scenarioListRevision;
  const year = $('twYear').value.trim();
  const term = $('twTerm').value;
  if (!year || !term) return;

  const data = await twFetch(`/ops/tw/scenarios/?year=${year}&term=${term}`);
  if (!data || revision !== scenarioListRevision || year !== $('twYear').value.trim() || term !== $('twTerm').value || workspaceBusy()) return;

  const sel = $('twScenario');
  sel.innerHTML = `<option value="">${T.selectScenario}</option>`;
  (data.scenarios || []).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.status === 'published' ? `${s.name} · ${T.published}` : s.name;
    sel.appendChild(opt);
  });

  TW.scenarioId = null;
  TW.scenario = null;
  TW.boardId = null;
  TW.loadedBoardId = null;
  TW.plannerJob = null;
  resetWorkspace();
  window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: null, job: null } }));
}

/* Re-read the scenario list for the current scope without closing what is open
   (loadScenarios resets the workspace). */
async function refreshScenarioOptions(selectedId) {
  const year = $('twYear').value.trim();
  const term = $('twTerm').value.trim();
  const data = await twFetch(`/ops/tw/scenarios/?year=${encodeURIComponent(year)}&term=${encodeURIComponent(term)}`, { _quiet: true });
  if (!data || year !== $('twYear').value.trim() || term !== $('twTerm').value.trim()) return;
  const sel = $('twScenario');
  sel.innerHTML = `<option value="">${T.selectScenario}</option>`;
  (data.scenarios || []).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.status === 'published' ? `${s.name} · ${T.published}` : s.name;
    sel.appendChild(opt);
  });
  if (selectedId != null) sel.value = String(selectedId);
}

let scenarioLoadRevision = 0;
async function onScenarioChange() {
  scenarioListRevision++;
  const sid = $('twScenario').value;
  const revision = ++scenarioLoadRevision;
  TW_ACTIVITY.loading = revision;
  syncWorkspaceControls();
  try {
    if (!sid) {
      persistWorkspaceScenario(null);
      TW.scenarioId = null; TW.scenario = null; TW.boardId = null; TW.plannerJob = null; resetWorkspace();
      window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: null, job: null } }));
      return;
    }
    const sameScenario = String(TW.scenarioId) === String(sid);
    const preferredBoard = sameScenario ? TW.boardId : null;

    resetPlanLens();
    const data = await twFetch(`/ops/tw/scenarios/${sid}/`);
    if (revision !== scenarioLoadRevision || sid !== $('twScenario').value) return;
    if (!data) {
      $('twScenario').value = TW.scenarioId || '';
      window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: TW.scenarioId, job: TW.plannerJob || null } }));
      return;
    }

    TW.scenarioId = data.scenario.id;
    TW.scenario = data.scenario;
    TW.boardId = null;
    TW.loadedBoardId = null;
    TW.selectedPlacementId = null;
    TW.placements = []; TW.conflicts = {}; TW.conflictsBoardId = null; TW.capacityData = null; TW.quality = null;
    if (!sameScenario) {
      TW.undoStack = []; TW.redoStack = []; TW.plannerJob = null;
      TW.sectionBudget = null; TW.unplacedSections = [];
    }
    if (data.active_job) TW.plannerJob = data.active_job;
    else if (!TW_ACTIVITY.owner && ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status)) TW.plannerJob = null;
    window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: TW.scenarioId, job: TW.plannerJob || null } }));
    if (data.active_job) announcePlannerJob(data.active_job, TW.scenarioId, data.active_job.job_id);
    const search = $('twSearch');
    if (search) search.value = '';
    updateSearchStatus();
    TW.readiness = null; TW.readinessVersion = -1; TW.qualityVersion = -1;

    updateValidationBadge();
    $('twExport').disabled = false;
    $('twExportMenu').disabled = false;
    syncWorkspaceControls();

    await loadPlanLens();
    if (revision !== scenarioLoadRevision) return;
    const boardsLoaded = await loadBoards(preferredBoard);
    if (revision === scenarioLoadRevision) persistWorkspaceScenario(TW.scenarioId, TW.loadedBoardId);
    syncWorkspaceControls();
    return boardsLoaded;
  } finally {
    if (TW_ACTIVITY.loading === revision) TW_ACTIVITY.loading = 0;
    syncWorkspaceControls();
  }
}

let copyScenarioPromptOpen = false;
function copyScenarioSuggestedName(name) {
  let suggestion = '';
  for (const character of `${name} ${IS_AR ? '(نسخة)' : '(copy)'}`) {
    // Keep whole code points within the input's UTF-16 maxlength.
    if (suggestion.length + character.length > 200) break;
    suggestion += character;
  }
  return suggestion;
}
function clearCopyWorkspaceBoards() {
  TW.boardId = null; TW.loadedBoardId = null;
  resetWorkspace();
  $('twGrid').innerHTML = '';
  $('twRightBody').innerHTML = '';
}
async function copyScenarioPrompt() {
  if (!TW.scenarioId || !TW.scenario || workspaceBusy() || copyScenarioPromptOpen) return;
  const sourceId = TW.scenarioId;
  const defaultName = copyScenarioSuggestedName(TW.scenario.name);
  const sourceBoardIds = new Set(TW.boards.map(board => String(board.id)));
  copyScenarioPromptOpen = true;
  let token = null;
  let copied = null;
  try {
    const name = await dlg.confirm({
      title: IS_AR ? 'نسخ السيناريو' : 'Copy scenario',
      body: IS_AR ? 'إنشاء مسودة مستقلة تحفظ الجدول والقاعات والأقفال والطلاب والإعدادات. يبقى السيناريو الأصلي كما هو.' : 'Create an independent draft with the timetable, rooms, locks, students and settings. The original scenario stays unchanged.',
      inputLabel: IS_AR ? 'اسم النسخة (اختياري)' : 'Copy name (optional)',
      defaultValue: defaultName,
      maxLength: 200,
      confirmText: IS_AR ? 'إنشاء نسخة' : 'Create copy',
      cancelText: IS_AR ? 'إلغاء' : 'Cancel',
    });
    if (name === false || name == null || TW.scenarioId !== sourceId) return;
    token = beginWorkspaceOperation('copy-scenario');
    if (!token) return;
    scenarioListRevision++;
    const data = await twFetch(`/ops/tw/scenarios/${sourceId}/copy/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(typeof name === 'string' && name.trim() && name.trim() !== defaultName ? { name: name.trim() } : {}),
      _workspaceToken: token,
    });
    if (!data?.scenario) return;
    copied = data.scenario;
    clearCopyWorkspaceBoards();
    const option = document.createElement('option');
    option.value = copied.id; option.textContent = `${copied.name} (${copied.status})`;
    $('twScenario').appendChild(option);
    $('twScenario').value = String(copied.id);
    const loaded = await onScenarioChange();
    const expectedIds = Array.isArray(data.boards) ? data.boards.map(board => String(board.id)).sort() : null;
    const actualIds = TW.boards.map(board => String(board.id)).sort();
    if (!loaded || String(TW.scenarioId) !== String(copied.id) || !expectedIds
        || expectedIds.some(id => sourceBoardIds.has(id)) || JSON.stringify(actualIds) !== JSON.stringify(expectedIds)) throw new Error('Copy load failed');
    notify.success(IS_AR ? 'تم إنشاء النسخة وفتحها' : 'Copy created and opened');
  } catch (error) {
    if (copied) {
      TW.scenarioId = null; TW.scenario = null; TW.plannerJob = null;
      $('twScenario').value = '';
      persistWorkspaceScenario(null);
      clearCopyWorkspaceBoards();
      window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: null, job: null } }));
      notify.warning(IS_AR ? `حُفظت النسخة «${copied.name}» (رقم ${copied.id}). تعذر فتحها؛ اخترها من قائمة السيناريوهات.` : `Copy “${copied.name}” (ID ${copied.id}) was saved but could not be opened. Select it from the scenario list.`);
    }
    else notify.error(error.message || (IS_AR ? 'تعذر نسخ السيناريو' : 'Could not copy scenario'));
  } finally {
    copyScenarioPromptOpen = false;
    if (token) finishWorkspaceOperation(token);
  }
}

async function createScenarioPrompt() {
  const name = await dlg.confirm({
    title: T.createScenario,
    inputLabel: T.scenarioName,
    inputPlaceholder: 'Draft A',
    confirmText: IS_AR ? 'إنشاء' : 'Create',
  });
  if (!name || name === true) return;

  const year = $('twYear').value;
  const term = $('twTerm').value;
  if (!year || !term) { notify.error('Select year and term first'); return; }

  const data = await twFetch('/ops/tw/scenarios/create/', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ academic_year: year, term: term, name: name }),
  });
  if (!data) return;

  notify.success(IS_AR ? 'تم إنشاء السيناريو' : 'Scenario created');
  await loadScenarios();
  $('twScenario').value = data.scenario.id;
  await onScenarioChange();
}

/* ── Boards ── */
async function loadBoards(preferredBoard = TW.boardId) {
  if (!TW.scenarioId) return false;
  const scenarioId = TW.scenarioId;
  const data = await twFetch(`/ops/tw/boards/?scenario_id=${scenarioId}`);
  if (!data || TW.scenarioId !== scenarioId) return false;

  TW.boards = data.boards || [];
  renderBoardTabs();

  if (TW.boards.length > 0) {
    const target = TW.boards.find(board => String(board.id) === String(preferredBoard));
    await selectBoard(target ? target.id : TW.boards[0].id);
    return TW.scenarioId === scenarioId && TW.loadedBoardId === (target ? target.id : TW.boards[0].id);
  }
  return true;
}

let boardLoadRevision = 0;
async function selectBoard(boardId) {
  const revision = ++boardLoadRevision;
  const scenarioId = TW.scenarioId;
  TW_ACTIVITY.boardLoading = revision;
  TW.boardId = boardId;
  syncWorkspaceControls();
  try {
    const data = await twFetch(`/ops/tw/boards/${boardId}/`);
    if (revision !== boardLoadRevision || TW.scenarioId !== scenarioId) return;
    if (!data) {
      TW.boardId = TW.loadedBoardId || null;
      renderBoardTabs();
      if (!TW.boardId) { $('twGrid').classList.add('d-none'); $('twEmptyState').style.display = ''; }
      return;
    }
    if (TW.loadedBoardId !== boardId) {
      TW.selectedPlacementId = null;
      // Another board: the keyboard starts at its first stop.
      boardNav?.reset();
    }
    // Previews answered before this board answer are read again; one still
    // in flight stays joinable (contract E).
    dropSettledPreviews();
    TW.loadedBoardId = boardId;
    persistWorkspaceScenario(TW.scenarioId, boardId);

    TW.placements = data.placements || [];
    // The board carries the scenario's columns; take the ones it has (an
    // undefined field must not wipe a known one).
    const columns = {};
    ['slot_config', 'lab_slot_config', 'effective_slot_config', 'effective_lab_slot_config'].forEach(key => {
      if (data[key] !== undefined) columns[key] = data[key];
    });
    TW.scenario = { ...TW.scenario, ...columns };

    renderBoardTabs();
    await loadConflicts();   // load conflicts BEFORE rendering grid so colors are correct
    if (revision !== boardLoadRevision || TW.scenarioId !== scenarioId) return;
    renderGrid();
    await loadCapacity();
    if (revision !== boardLoadRevision || TW.scenarioId !== scenarioId) return;
    await loadBudget();
    if (revision !== boardLoadRevision || TW.scenarioId !== scenarioId) return;
    await refreshWorkspaceQuality();
    renderWorkspaceQuality();
    syncWorkspaceControls();
  } finally {
    if (TW_ACTIVITY.boardLoading === revision) TW_ACTIVITY.boardLoading = 0;
    syncWorkspaceControls();
  }
}

async function createBoardPrompt() {
  if (!workspaceCanEdit()) return;
  const scenarioId = TW.scenarioId;
  const label = await dlg.confirm({
    title: T.createBoard,
    inputLabel: T.boardLabel,
    inputPlaceholder: 'Term 3 Group A',
    confirmText: IS_AR ? 'إنشاء' : 'Create',
  });
  if (!label || label === true) return;
  if (!workspaceCanEdit() || TW.scenarioId !== scenarioId) return;

  const data = await twFetch('/ops/tw/boards/create/', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ scenario_id: TW.scenarioId, label: label }),
  });
  if (!data) return;

  notify.success(IS_AR ? 'تم إنشاء اللوحة' : 'Board created');
  await loadBoards();
  await selectBoard(data.board.id);
}

/* ── Budget Sidebar ── */
async function loadBudget() {
  if (!TW.scenarioId) return;
  const scenarioId = TW.scenarioId;
  const boardId = TW.boardId;
  const data = await twFetch(`/ops/tw/scenarios/${TW.scenarioId}/budget/`);
  if (TW.scenarioId !== scenarioId) return;
  if (data) {
    TW.sectionBudget = data.budget || [];
    TW.unplacedSections = [];
    if (!TW.sectionBudget.length && boardId) {
      const sections = await twFetch(`/ops/tw/boards/${boardId}/unplaced/`);
      if (TW.scenarioId !== scenarioId || TW.boardId !== boardId) return;
      TW.unplacedSections = sections?.unplaced || [];
    }
    renderBudgetSidebar();
  }
}

function renderBudgetSidebar() {
  if (TW.boardId && !TW.sectionBudget?.length) { renderUnplacedList(); return; }
  const list = $('twSectionList');
  const budget = TW.sectionBudget || [];
  const search = sectionBudgetSearch();

  if (budget.length === 0) {
    list.innerHTML = `<p class="tw-pane-empty">${TW.scenarioId
      ? (IS_AR ? 'لا توجد ميزانية شعب لهذا السيناريو.' : 'This scenario has no section budget.')
      : (IS_AR ? 'افتح سيناريو لعرض الشعب المطلوبة.' : 'Open a scenario to see its required sections.')}</p>`;
    setLeftCount(0);
    return;
  }

  // Filter by active board's nominal_term or search
  let items = budget;
  const activeBoard = TW.boards.find(b => b.id === TW.boardId);
  const activeTerm = activeBoard ? activeBoard.nominal_term : null;

  if (search) {
    items = items.filter(c => budgetMatchesSearch(c, search));
  }

  list.innerHTML = '';

  // Group by programme_term
  const byTerm = {};
  items.forEach(c => {
    const t = c.programme_term || 0;
    if (!byTerm[t]) byTerm[t] = [];
    byTerm[t].push(c);
  });

  const sortedTerms = Object.keys(byTerm).map(Number).sort((a, b) => a - b);

  let leftToPlace = 0;
  let firstGrid = true;
  sortedTerms.forEach(t => {
    // Term header, and its rows as a one-column grid (one keyboard stop for the
    // whole list; design record §7.3, 5a).
    const hdr = document.createElement('h3');
    hdr.className = 'tw-term-head' + (t === activeTerm ? ' is-active' : '');
    hdr.id = `twTerm-${t}`;
    hdr.textContent = `${IS_AR ? 'المستوى' : 'Term'} ${t}`;
    list.appendChild(hdr);
    const termGrid = listGrid(`twTerm-${t}`, firstGrid);
    firstGrid = false;
    list.appendChild(termGrid);

    byTerm[t].forEach(c => {
      const remaining = c.remaining_sections || 0;
      const used = c.used_sections || 0;
      const planned = c.planned_sections || 0;
      const isFullyUsed = remaining <= 0;

      // One card per remaining section
      const count = Math.max(remaining, 0);
      leftToPlace += count;
      if (count === 0 && used > 0) {
        // A course whose sections are all placed: a quiet, readable line (it was
        // faded to 40% opacity, which no one could read).
        const div = listRow(termGrid, `place:${c.course_key}|complete`);
        div.className = 'tw-section-item is-complete';
        div.setAttribute('aria-disabled', 'true');
        div.dataset.course = c.course_code;
        div.innerHTML = `
          <span class="code"><span class="ux-tone-icon" data-tone="ok" aria-hidden="true"></span>${esc(c.course_code)}</span>
          <span class="meta ux-meta"><span>${IS_AR ? `${used}/${planned} موضوعة` : `${used}/${planned} placed`}</span><span>${IS_AR ? `${c.total_demand} طالب` : `demand ${c.total_demand}`}</span></span>
        `;
        return;
      }

      freeSectionLabels(c).forEach(secLabel => {
        const div = listRow(termGrid, `place:${c.course_key}|${secLabel}`);
        div.className = 'tw-section-item';
        div.dataset.course = c.course_code;
        const meetings = Number(c.required_meetings_per_section) || 1;
        div.draggable = workspaceCanEdit();
        div.dataset.twDraggable = 'true';
        // The row is the control (5f): Enter, or a click on its "Place…",
        // opens Place… (an English row: its pattern chooser), a dialog
        // either way. A click anywhere else on it is still a drag.
        div.setAttribute('aria-haspopup', 'dialog');
        const facts = [c.department || '', `${c.credit_hours || '?'} ${IS_AR ? 'س' : 'cr'}`, `${IS_AR ? 'سعة' : 'cap'} ${c.max_per_section || '?'}`,
          meetings > 1 ? PLACE_WORDS.meetingsWeek(meetings) : ''];
        div.innerHTML = `
          <span class="tw-section-head"><span class="code">${esc(c.course_code)} <bdi>${esc(secLabel)}</bdi></span><span class="tw-place-hint" aria-hidden="true">${esc(PLACE_WORDS.hint)}</span></span>
          <span class="meta ux-meta">${facts.filter(Boolean).map(part => `<span>${esc(part)}</span>`).join('')}</span>
          <span class="meta ux-meta"><span>${IS_AR ? `${used}/${planned} موضوعة` : `${used}/${planned} placed`}</span><span>${IS_AR ? `${c.total_demand} طالب` : `demand ${c.total_demand}`}</span></span>
        `;
        div.addEventListener('dragstart', (e) => {
          if (!workspaceCanEdit()) { e.preventDefault(); return; }
          e.dataTransfer.setData('text/plain', JSON.stringify({
            type: 'create_planned',
            course_code: c.course_code,
            course_key: c.course_key,
            course_number: c.course_number,
            course_name: c.course_name,
            section_label: secLabel,
            department: c.department || '',
            credit_hours: c.credit_hours || 0,
            max_per_section: c.max_per_section || 40,
            total_students: c.total_demand || 0,
            required_meetings_per_section: c.required_meetings_per_section || 1,
            programme_term: c.programme_term,
          }));
          e.dataTransfer.effectAllowed = 'copy';
        });
        if (globalThis.TimetableMeetingPolicy?.isEnglish(c)) {
          div._twEnglish = () => chooseEnglishWorkspacePattern({ ...c, type: 'create_planned', section_label: secLabel });
        } else {
          div._twPlace = placeRequestOf(c, secLabel);
        }
      });
    });
  });
  setLeftCount(leftToPlace);
  listNav?.refresh();
  renderBoardTabs();
  renderWorkspaceQuality();
}

/* The labels of a course placed anywhere in the scenario, as the server
   names them. An older server without the list: the first `used_sections`
   labels count as used, as before. */
function usedSectionLabels(c) {
  const placed = Array.isArray(c.used_section_labels)
    ? c.used_section_labels
    : Array.from({ length: Number(c.used_sections) || 0 }, (_, i) => `S${i + 1}`);
  return new Set(placed.map(label => String(label).trim().toUpperCase()));
}
/* The labels a course's next sections take: the first free S1, S2, … (with
   S1 and S3 placed the next are S2 and S4, never S3 again). */
function freeSectionLabels(c) {
  const count = Math.max(Number(c.remaining_sections) || 0, 0);
  const used = usedSectionLabels(c);
  const free = [];
  for (let n = 1; free.length < count && n <= count + used.size; n++) {
    if (!used.has(`S${n}`)) free.push(`S${n}`);
  }
  return free;
}

/* What Place… is asked to place (5f), from a budget row or a list row's
   drag payload: the course's identity, the label, the seats, how many
   meetings a week it needs and its programme term. */
function placeRequestOf(c, label = c.section_label) {
  return {
    course_code: c.course_code, course_key: c.course_key || c.course_code, course_name: c.course_name || c.course_code,
    section_label: label, capacity: Number(c.max_per_section) || 40,
    required: Number(c.required_meetings_per_section) || 1, programme_term: c.programme_term,
  };
}

/* The list's grids: one per term (or one for the unplaced sections), each row
   one cell, the cell the keyboard's stop. */
function listGrid(labelledBy, describe) {
  const grid = document.createElement('div');
  grid.className = 'tw-term-list';
  grid.setAttribute('role', 'grid');
  grid.setAttribute('aria-labelledby', labelledBy);
  if (describe) grid.setAttribute('aria-describedby', 'twListKeys');
  return grid;
}
function listRow(grid, key) {
  const row = document.createElement('div');
  row.className = 'ux-grid-row';
  row.setAttribute('role', 'row');
  const cell = document.createElement('div');
  cell.setAttribute('role', 'gridcell');
  cell.dataset.focusKey = key;
  row.appendChild(cell);
  grid.appendChild(row);
  return cell;
}

/* ── Conflicts ── */
async function loadConflicts() {
  const boardId = TW.boardId;
  const scenarioId = TW.scenarioId;
  if (!TW.boardId) return;
  const data = await twFetch(`/ops/tw/boards/${TW.boardId}/conflicts/`);
  if (TW.boardId !== boardId || TW.scenarioId !== scenarioId) return;
  if (data) {
    TW.conflicts = data;
    TW.conflictsBoardId = boardId;
  } else {
    // A failed load leaves this board's clashes unknown: an edit may have
    // changed them, so the earlier answer is not shown as current.
    TW.conflicts = {};
    TW.conflictsBoardId = null;
  }
  // A failed load redraws too: the inspector then says this board's clashes
  // are not loaded, instead of keeping an earlier or another board's list.
  const activeTab = document.querySelector('.tw-right .tw-panel-tab.active');
  renderRightPanel((activeTab && activeTab.dataset.tab) || 'issues');
  updateQuickStats();
}

/* ── Publish ──
   Publishing is the server's decision (check_publish_readiness): it blocks on
   more than this page's critical count — unroomed meetings, room clashes,
   missing sections, patterns. Readiness is read on scenario load and after each
   edit (cached against TW.dataVersion), so the button says before any click
   whether the scenario can be published, and a refusal lists the reasons. */
// Bilingual wording for every blocker/warning code the server returns
// (core/services/timetable_workspace.py, check_publish_readiness, whose
// docstring lists the vocabulary). An unknown code falls back to the server's
// English sentence, so a new server-side check is never shown blank.
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const READINESS_ITEM_TEXT = {
  no_boards: () => IS_AR ? 'السيناريو بلا لوحات.' : 'The scenario has no boards.',
  board_no_placements: p => IS_AR ? `اللوحة «${p.board_label}» بلا شعب موضوعة.` : `Board “${p.board_label}” has no sections placed.`,
  board_critical_conflicts: p => IS_AR ? `اللوحة «${p.board_label}»: ${p.count} تعارضات مانعة.` : `Board “${p.board_label}”: ${plural(p.count, 'blocking clash', 'blocking clashes')}.`,
  board_blocked_slot_placements: p => {
    const slots = (p.slots || []).join('، ');
    return IS_AR ? `اللوحة «${p.board_label}»: ${p.count} لقاءات في أوقات محجوبة${slots ? ` (${slots})` : ''}.`
      : `Board “${p.board_label}”: ${plural(p.count, 'meeting', 'meetings')} on blocked times${slots ? ` (${(p.slots || []).join(', ')})` : ''}.`;
  },
  board_room_conflicts: p => IS_AR ? `اللوحة «${p.board_label}»: ${p.count} قاعات محجوزة مرتين.` : `Board “${p.board_label}”: ${plural(p.count, 'room double-booked', 'rooms double-booked')}.`,
  board_sections_without_rooms: p => IS_AR ? `اللوحة «${p.board_label}»: ${p.count} لقاءات بلا قاعة.` : `Board “${p.board_label}”: ${plural(p.count, 'meeting', 'meetings')} without a room.`,
  missing_required_section: p => IS_AR
    ? `${p.course_code}: ${p.count} من ${p.planned_sections} شعب لم توضع (المستوى ${p.programme_term}).`
    : `${p.course_code}: ${p.count} of ${p.planned_sections} sections not placed (term ${p.programme_term}).`,
  incomplete_section_pattern: p => IS_AR
    ? `${p.course_code} ${p.section}: ${p.placed_meetings} من ${p.expected_meetings} لقاءات موضوعة.`
    : `${p.course_code} ${p.section}: ${p.placed_meetings} of ${p.expected_meetings} meetings placed.`,
  invalid_section_pattern: p => IS_AR
    ? `${p.course_code} ${p.section}: نمط اللقاءات غير مسموح${p.detail ? ` (${p.detail})` : ''}.`
    : `${p.course_code} ${p.section}: meeting pattern not allowed${p.detail ? ` (${p.detail})` : ''}.`,
  board_warnings: p => IS_AR ? `اللوحة «${p.board_label}»: ${p.count} تحذيرات.` : `Board “${p.board_label}”: ${plural(p.count, 'warning', 'warnings')}.`,
  cross_board_conflicts: p => IS_AR
    ? `${p.count} تداخلات بين اللوحات يشترك فيها ${p.threshold} طالباً أو أكثر.`
    : `${plural(p.count, 'cross-board overlap', 'cross-board overlaps')} shared by ${p.threshold} or more students.`,
};
function readinessItemText(item) {
  if (!item) return '';
  const params = { ...(item.params || {}), board_label: item.board_label || '', count: item.count };
  const render = READINESS_ITEM_TEXT[item.code];
  return render ? render(params) : (item.message || item.code || '');
}
/* The board a readiness item is about: its own, or for a section not yet
   placed anywhere, the board of its programme term. */
function readinessItemBoard(item) {
  if (item?.board_id) return item.board_id;
  const term = item?.params?.programme_term;
  if (term == null) return null;
  return TW.boards.find(board => Number(board.nominal_term) === Number(term))?.id || null;
}

/* One readiness check per scenario and data version: callers arriving while it
   runs share it (a board click no longer starts another full check). The
   background refresh after an edit waits for edits to settle and fails quietly;
   only Publish itself (force) reports a failure. */
let readinessRequest = null;
let readinessTimer = null;
function scheduleReadinessRefresh() {
  clearTimeout(readinessTimer);
  // Scheduled is as good as running for what the page says: "checking".
  TW.readinessLoading = true;
  readinessTimer = setTimeout(() => { refreshPublishReadiness(); }, 800);
}
function refreshPublishReadiness(force = false) {
  const scenarioId = TW.scenarioId;
  // A scheduled check that turns out not to be needed is not "checking".
  if (!scenarioId || TW.scenario?.status === 'published') {
    TW.readiness = null; TW.readinessLoading = Boolean(readinessRequest); renderPublishState(); return Promise.resolve(null);
  }
  if (!force && TW.readiness && TW.readinessVersion === TW.dataVersion) {
    TW.readinessLoading = Boolean(readinessRequest); renderPublishState(); return Promise.resolve(TW.readiness);
  }
  const version = TW.dataVersion;
  // Background callers share one request per version. Publish (force) asks on
  // its own and reports a failure: joining a quiet request would make a failed
  // check a silent click.
  if (!force && readinessRequest && readinessRequest.scenarioId === scenarioId && readinessRequest.version === version) {
    return readinessRequest.promise;
  }
  TW.readinessLoading = true;
  renderPublishState();
  if (document.querySelector('.tw-right .tw-panel-tab.active')?.dataset.tab === 'issues') renderRightPanel('issues');
  const promise = (async () => {
    try {
      const data = await twFetch(`/ops/tw/scenarios/${scenarioId}/readiness/`, { _quiet: !force });
      if (TW.scenarioId !== scenarioId) return null;
      // An answer about older data never replaces one about newer data.
      if (data?.readiness && version >= TW.readinessVersion) { TW.readiness = data.readiness; TW.readinessVersion = version; }
      return data?.readiness || null;
    } finally {
      if (readinessRequest?.promise === promise) readinessRequest = null;
      TW.readinessLoading = Boolean(readinessRequest);
      renderReadinessViews();
    }
  })();
  if (!force) readinessRequest = { scenarioId, version, promise };
  return promise;
}

/* Everything that shows a readiness answer: Publish, the counts, the board
   flags, and the Issues tab when it is open (it lists the board's blockers, so
   without a redraw it shows the previous answer). */
function renderReadinessViews() {
  renderPublishState();
  renderWorkspaceQuality();
  renderBoardTabs();
  const activeTab = document.querySelector('.tw-right .tw-panel-tab.active');
  if (activeTab?.dataset.tab === 'issues') renderRightPanel('issues');
}

/* The publish blockers the server places on one board (its critical clashes
   are already counted from the board's own conflicts). A section not placed
   anywhere belongs to the board of its programme term. */
function boardReadinessBlockers(boardId) {
  if (!TW.readiness || boardId == null) return [];
  return readinessBlockers(TW.readiness)
    .filter(item => item.code !== 'board_critical_conflicts' && readinessItemBoard(item) === boardId);
}

function readinessBlockers(readiness) {
  if (!readiness) return [];
  if (Array.isArray(readiness.blocker_items) && readiness.blocker_items.length) return readiness.blocker_items;
  return (readiness.blockers || []).map(text => ({ code: '', message: text }));
}
function readinessWarnings(readiness) {
  if (!readiness) return [];
  if (Array.isArray(readiness.warning_items) && readiness.warning_items.length) return readiness.warning_items;
  return (readiness.warnings || []).map(text => ({ code: '', message: text }));
}

/* Publish is the one primary action, and only when the server says the
   scenario is ready. Blocked, it is a neutral button that names the count and
   opens the reasons. */
function renderPublishState() {
  const button = $('twPublish');
  if (!button) return;
  const label = $('twPublishLabel');
  const icon = $('twPublishIcon');
  const published = TW.scenario?.status === 'published';
  const blockers = readinessBlockers(TW.readiness).length;
  // The label keeps the last known count while a newer check runs; the button
  // becomes the primary only on an answer about the current data.
  const known = Boolean(TW.readiness);
  const fresh = known && TW.readinessVersion === TW.dataVersion;
  button.disabled = !workspaceCanEdit() || published;
  const ready = fresh && blockers === 0 && !published;
  button.classList.toggle('btn-primary', ready);
  button.classList.toggle('btn-neutral', !ready);
  if (icon) icon.hidden = !(known && blockers > 0);
  let text = T.publish;
  if (published) text = T.published;
  else if (known && blockers > 0) text = IS_AR ? `نشر · ${blockers} موانع` : `Publish · ${blockers} ${blockers === 1 ? 'blocker' : 'blockers'}`;
  if (label) label.textContent = text;
  button.title = published
    ? (IS_AR ? 'هذا السيناريو منشور ولا يمكن تعديله.' : 'This scenario is published and read-only.')
    : TW.readinessLoading ? T.checking
    : known && blockers > 0 ? (IS_AR ? 'اعرض ما يمنع النشر' : 'See what blocks publishing')
    : (IS_AR ? 'نشر السيناريو: يصبح للقراءة فقط' : 'Publish the scenario: it becomes read-only');
}

function readinessListHtml(items, tone) {
  if (!items.length) return '';
  return `<ul class="tw-readiness-list" data-tone="${tone}">${items.map(item => `<li>
    <span class="ux-tone-icon" data-tone="${tone}" aria-hidden="true"></span>
    <span>${esc(readinessItemText(item))}</span>
    ${(() => { const board = readinessItemBoard(item);
      return board && board !== TW.boardId && TW.boards.some(row => row.id === board)
        ? `<button type="button" class="btn btn-sm btn-neutral tw-readiness-go" data-board="${board}">${IS_AR ? 'افتح اللوحة' : 'Open board'}</button>` : ''; })()}
  </li>`).join('')}</ul>`;
}

async function showPublishBlockers(readiness) {
  const blockers = readinessBlockers(readiness);
  const warnings = readinessWarnings(readiness);
  const body = `<p class="tw-readiness-lead">${IS_AR
    ? 'لا يمكن نشر هذا السيناريو حتى تُحل هذه المشاكل:'
    : 'This scenario cannot be published until these are resolved:'}</p>
    ${readinessListHtml(blockers, 'critical')}
    ${warnings.length ? `<p class="tw-readiness-lead">${IS_AR ? 'تحذيرات لا تمنع النشر:' : 'Warnings that do not block publishing:'}</p>${readinessListHtml(warnings, 'warning')}` : ''}`;
  const shown = dlg.confirm({
    title: IS_AR ? 'النشر غير متاح بعد' : 'Not ready to publish',
    body, kind: 'danger', confirmText: IS_AR ? 'حسناً' : 'OK',
  });
  wireReadinessLinks();
  await shown;
}

/* "Open board" in a readiness list takes the user to that board. It closes the
   dialog by CANCELLING it — the same list sits in the publish confirmation,
   whose confirm button publishes. */
function wireReadinessLinks() {
  setTimeout(() => {
    document.querySelectorAll('.dlg-backdrop .tw-readiness-go').forEach(button => button.addEventListener('click', () => {
      document.querySelector('.dlg-backdrop .btn-cancel')?.click();
      selectBoard(Number(button.dataset.board));
    }));
  }, 0);
}

async function publishScenario() {
  if (!workspaceCanEdit()) return;
  const scenarioId = TW.scenarioId;
  if (!TW.scenarioId) return;

  const readiness = await refreshPublishReadiness(true);
  if (TW.scenarioId !== scenarioId) return;
  if (!readiness) return;  // twFetch already said why
  if (readinessBlockers(readiness).length) { await showPublishBlockers(readiness); return; }

  const warnings = readinessWarnings(readiness);
  const body = `<p class="tw-readiness-lead">${IS_AR
    ? 'بعد النشر يصبح هذا السيناريو للقراءة فقط.'
    : 'Once published, this scenario becomes read-only.'}</p>
    ${warnings.length ? `<p class="tw-readiness-lead">${IS_AR ? 'تحذيرات لا تمنع النشر:' : 'Warnings that do not block publishing:'}</p>${readinessListHtml(warnings, 'warning')}` : ''}`;
  const confirmed = dlg.confirm({ title: T.publishConfirm, body, kind: 'warning', confirmText: T.publish });
  wireReadinessLinks();
  const ok = await confirmed;
  if (!ok) return;
  if (!workspaceCanEdit() || TW.scenarioId !== scenarioId) return;

  let refused = null;
  const data = await twFetch(`/ops/tw/scenarios/${TW.scenarioId}/publish/`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    _onError: body => {
      const error = body?.error;
      if (error?.code !== 'PUBLISH_BLOCKED') return false;
      refused = error.details || {};
      return true;
    },
  });
  if (refused) {
    TW.readiness = { ready: false, ...refused };
    TW.readinessVersion = TW.dataVersion;
    renderReadinessViews();
    await showPublishBlockers(TW.readiness);
    return;
  }
  if (!data) return;
  notify.success(T.published_ok);
  TW.scenario = data.scenario;
  TW.readiness = null;
  updateValidationBadge();
  syncWorkspaceControls();
}

function exportScenario(mode) {
  if (!TW.scenarioId) { notify.error(IS_AR ? 'اختر سيناريو' : 'No scenario selected'); return; }
  const params = new URLSearchParams();
  const search = activeWorkspaceSearch();
  if (mode === 'per-plan' && search) params.set('search', search);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const url = mode === 'per-plan'
    ? `/ops/tw/scenarios/${TW.scenarioId}/export-per-plan${suffix}`
    : `/ops/tw/scenarios/${TW.scenarioId}/export.xlsx`;
  window.open(url, '_blank');
}

/* ── Rendering ── */
function resetWorkspace() {
  TW.boards = []; TW.placements = []; TW.conflicts = {}; TW.conflictsBoardId = null; TW.capacityData = null;
  TW.quality = null; TW.undoStack = []; TW.redoStack = []; TW.selectedPlacementId = null;
  TW.readiness = null; TW.readinessVersion = -1; TW.qualityVersion = -1;
  TW.sectionBudget = null; TW.unplacedSections = [];
  resetPlanLens();
  $('twBoardTabs').innerHTML = '';
  $('twSectionList').innerHTML = `<p class="tw-pane-empty">${IS_AR ? 'افتح سيناريو لعرض الشعب المطلوبة.' : 'Open a scenario to see its required sections.'}</p>`;
  $('twGrid').classList.add('d-none');
  $('twGrid').style.display = '';
  $('twEmptyState').style.display = '';
  $('twExport').disabled = true;
  $('twExportMenu').disabled = true;
  updateValidationBadge();
  renderWorkspaceQuality();
  syncWorkspaceControls();
}

/* Sections still to place on a board: the budget rows of its nominal term. */
function boardSectionsLeft(board) {
  if (!board || !TW.sectionBudget?.length) return null;
  return TW.sectionBudget
    .filter(row => Number(row.programme_term) === Number(board.nominal_term))
    .reduce((sum, row) => sum + Math.max(0, Number(row.remaining_sections) || 0), 0);
}

/* Scroll a horizontal strip just enough to show one of its items. scrollLeft
   moves the view right as it grows in either direction (in a right-to-left
   strip it runs from a negative minimum up to 0), so the visual overflow is the
   amount to move. */
function revealInStrip(strip, item) {
  const view = strip.getBoundingClientRect();
  const box = item.getBoundingClientRect();
  // Wider than the strip: always show its start (alternating ends on every
  // reveal — each resize step fires several — would make it flicker).
  if (box.width > view.width - 8) {
    if (getComputedStyle(strip).direction === 'rtl') strip.scrollLeft += box.right - view.right + 4;
    else strip.scrollLeft -= view.left - box.left + 4;
    return;
  }
  if (box.left < view.left) strip.scrollLeft -= view.left - box.left + 4;
  else if (box.right > view.right) strip.scrollLeft += box.right - view.right + 4;
}

/* The open board: what the model knows, and while its clashes are unknown
   the board list's own count for them (rather than none). */
function activeBoardCritical(board) {
  const model = buildBoardIssues();
  return model.counts.critical + (model.known.conflicts ? 0 : (board.critical || 0));
}
/* What blocks publishing on a board that is not open: the publish check's
   items for it (a clash or room count counts each one), or, before any check,
   the board list's own clash count. */
const COUNTED_READINESS_CODES = new Set(['board_critical_conflicts', 'board_room_conflicts', 'board_sections_without_rooms', 'board_blocked_slot_placements']);
function otherBoardCritical(board) {
  if (!TW.readiness) return board.critical || 0;
  return readinessBlockers(TW.readiness)
    .filter(item => readinessItemBoard(item) === board.id)
    .reduce((sum, item) => sum + (COUNTED_READINESS_CODES.has(item.code) ? (item.count || 1) : 1), 0);
}

/* One board switcher: segmented buttons (not tabs — no arrow-key panels are
   promised). Each says what is wrong on its board — blocking issues, with the
   octagon — and how many sections are left; meetings and students are in its
   name and tooltip. */
function renderBoardTabs() {
  const wrap = $('twBoardTabs');
  // A re-render replaces the buttons: keep the keyboard on the same board.
  const focusedBoard = wrap.contains(document.activeElement) ? document.activeElement.dataset.boardId : null;
  wrap.innerHTML = '';
  TW.boards.forEach(b => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ux-seg-item';
    btn.dataset.boardId = String(b.id);
    const active = b.id === TW.boardId;
    if (active) btn.setAttribute('aria-current', 'true');
    const critical = active ? activeBoardCritical(b) : otherBoardCritical(b);
    const left = boardSectionsLeft(b);
    const students = `${b.primary_count || 0}${b.visitor_count ? ` + ${b.visitor_count}` : ''}`;
    const meetings = active ? TW.placements.length : (b.placement_count || 0);
    const parts = [
      IS_AR ? `${students} طالب` : `${students} students`,
      IS_AR ? `${meetings} لقاء` : `${meetings} meetings`,
    ];
    if (critical) parts.push(IS_AR ? `موانع النشر: ${critical}` : `${critical} ${critical === 1 ? 'issue blocks' : 'issues block'} publishing`);
    if (left) parts.push(IS_AR ? `${left} شعب لم توضع` : `${left} sections to place`);
    btn.title = parts.join(' · ');
    btn.setAttribute('aria-label', `${b.label}: ${parts.join(', ')}`);
    btn.innerHTML = `<bdi>${esc(b.label)}</bdi>`
      + (critical ? `<span class="ux-seg-flag" data-tone="critical"><span class="ux-tone-icon" data-tone="critical" aria-hidden="true"></span>${critical}</span>` : '')
      + (left ? `<span class="ux-seg-meta">${IS_AR ? `${left} متبقية` : `${left} left`}</span>` : '');
    btn.addEventListener('click', () => selectBoard(b.id));
    wrap.appendChild(btn);
  });
  if (focusedBoard) wrap.querySelector(`[data-board-id="${focusedBoard}"]`)?.focus({ preventScroll: true });
  // Keep the chosen board in view by scrolling the STRIP only (scrollIntoView
  // also scrolled the page, on every keystroke). It runs after every render,
  // because later renders widen the buttons before it ("N left", flags), and it
  // re-reads the current button: this one may be gone by the next frame.
  (window.requestAnimationFrame || setTimeout)(() => {
    const current = wrap.querySelector('[aria-current="true"]');
    if (current) revealInStrip(wrap, current);
  });
}

/* The columns the scenario really uses: the server's effective config (its
   own, or the defaults). The page keeps no defaults of its own — it used to
   draw Lab 6 and no online windows where the scheduler had the opposite. */
function scenarioLectureSlots() {
  return (TW.scenario && (TW.scenario.effective_slot_config || TW.scenario.slot_config)) || [];
}
function scenarioLabSlots() {
  return (TW.scenario && (TW.scenario.effective_lab_slot_config || TW.scenario.lab_slot_config)) || [];
}

function renderGrid() {
  renderGridWithSlots(scenarioLectureSlots(), scenarioLabSlots());
}

// 20 pastel course colours (a way to find your place, not an identity).
const COURSE_COLORS = [
  '#D4E6F1','#D5F5E3','#FADBD8','#FCF3CF','#D7BDE2',
  '#A9DFBF','#F9E79F','#AED6F1','#F5CBA7','#A3E4D7',
  '#E8DAEF','#FDEBD0','#ABB2B9','#A2D9CE','#F5B7B1',
  '#D6DBDF','#ABEBC6','#FAD7A0','#D2B4DE','#AEB6BF',
];
function courseColorKey(code) {
  return String(code || '').replace(/\u00a0/g, ' ').trim().toUpperCase().replace(/\s+/g, '') || '__UNKNOWN__';
}
/* A course's colour from its identity (course key) alone, by an FNV-1a hash:
   the same on every board and every visit — it used to depend on the order
   courses were first drawn — and most often different for two courses
   sharing a code (20 colours: two courses on a board can share one, so the
   colour helps find a course; it does not identify it). Not shared-utils'
   colorForCourse: that returns a per-theme colour at call time, while a card
   keeps one pastel token that its CSS mixes for the dark theme. */
function courseColor(identity) {
  const key = courseColorKey(identity);
  let hash = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    hash ^= key.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return COURSE_COLORS[hash % COURSE_COLORS.length];
}

function courseKeyOf(item) {
  return String(item?.course_key || item?.course_code || '').trim();
}

// The lens read in flight (shared by every ask for the same data) and the
// count that lets only the newest read land.
let planLensRead = null;
let planLensRevision = 0;
function resetPlanLens() {
  // Whatever read is in flight answers for data that is gone (a reload after
  // a planner job keeps the data version but not the timetable).
  planLensRevision++;
  planLensRead = null;
  TW.planLens = { plans: [], courses: {}, sections: {}, loaded: false };
}

/* The plan lens: who sits where, by programme — a full seating evaluation
   on the server. It is read when a scenario opens and then only on request
   (Selection's Refresh, or after a programme mark is saved), never after
   every edit; `version` says which data it describes. A second request for
   the same data joins the read in flight; a newer read wins over an older. */
function loadPlanLens() {
  const scenarioId = TW.scenarioId;
  if (!scenarioId) {
    resetPlanLens();
    return Promise.resolve();
  }
  const version = TW.dataVersion;
  if (planLensRead && planLensRead.scenarioId === scenarioId && planLensRead.version === version) return planLensRead.promise;
  const revision = ++planLensRevision;
  TW.planLens.loading = true;
  const promise = (async () => {
    try {
      const data = await twFetch(`/ops/tw/scenarios/${scenarioId}/plan-lens/`);
      if (revision !== planLensRevision || TW.scenarioId !== scenarioId) return;
      if (!data || !data.plan_lens) {
        TW.planLens.loaded = false;
        return;
      }
      TW.planLens = Object.assign({ plans: [], courses: {}, sections: {}, loaded: true }, data.plan_lens, { version });
    } finally {
      if (revision === planLensRevision) {
        TW.planLens.loading = false;
        planLensRead = null;
      }
    }
  })();
  planLensRead = { scenarioId, version, promise };
  return promise;
}

/* A search by programme ("AI2") reads the lens: a new lens dims anew, in
   place (the board is not drawn again). */
function reapplyPlanSearch() {
  const tokens = [...workspaceSearchTokens(), ...sectionBudgetSearch().split(/\s+/)];
  if (!tokens.some(token => planTokenFromSearch(token))) return;
  applyGridView();
  renderBudgetSidebar();
}

/* The lens on request: Selection says it is loading, then shows the answer,
   a programme search filters anew, and a screen reader hears it arrive. */
async function refreshPlanLens() {
  const read = loadPlanLens();
  redrawSelection();
  await read;
  redrawSelection();
  reapplyPlanSearch();
  announceInspector(TW.planLens.loaded
    ? (IS_AR ? 'حُدِّث توزيع البرامج.' : 'Programme mix updated.')
    : (IS_AR ? 'توزيع البرامج غير متاح.' : 'The programme mix is not available.'));
}

/* The inspector's own status line (outside the panel it redraws). */
function announceInspector(text) {
  const status = $('twInspectorStatus');
  if (!status) return;
  status.textContent = '';
  status.textContent = text;
}

function sectionLensForPlacement(placement) {
  const sid = placement?.term_section_id == null ? '' : String(placement.term_section_id);
  return TW.planLens.sections[sid] || null;
}

function courseLensForItem(item) {
  return TW.planLens.courses[courseKeyOf(item)] || null;
}

function planTokenFromSearch(search) {
  const q = String(search || '').trim().toUpperCase();
  if (!q) return '';
  if (q === 'SHARED') return 'SHARED';
  return (TW.planLens.plans || []).includes(q) ? q : '';
}

function itemMatchesPlanToken(item, token) {
  if (!token) return false;
  const sectionLens = sectionLensForPlacement(item);
  if (sectionLens) {
    if (token === 'SHARED') return !!sectionLens.shared || sectionLens.role === 'shared';
    return (sectionLens.filter_plans || []).includes(token);
  }
  const courseLens = courseLensForItem(item);
  if (!courseLens) return false;
  if (token === 'SHARED') return !!courseLens.shared_overflow;
  const plans = courseLens.plans || {};
  const allocation = courseLens.allocation || {};
  return Number(plans[token] || 0) > 0 || Number(allocation[token] || 0) > 0;
}

function hasDuplicateCourseCode(code) {
  const target = courseColorKey(code);
  if (!target || target === '__UNKNOWN__') return false;
  const keys = Object.keys(TW.planLens.courses || {}).filter(key => {
    const prefix = String(key).split('::')[0];
    return courseColorKey(prefix) === target;
  });
  return new Set(keys).size > 1;
}

function compactCourseName(name) {
  return String(name || '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
}

function activeWorkspaceSearch() {
  return ($('twSearch')?.value || '').trim().toUpperCase();
}

function workspaceSearchTokens() {
  return activeWorkspaceSearch().split(/\s+/).filter(Boolean);
}

function sectionBudgetSearch() {
  const local = ($('twSectionSearch')?.value || '').trim().toUpperCase();
  return local || activeWorkspaceSearch();
}

/* What the text filter reads of a meeting: what its card and Selection show
   (E7) — never the lens's own words (owner, role, allocation), which no card
   shows ("UNALLOCATED" or "core" finds nothing). */
function placementText(p) {
  // The words the user sees ("No room", "Online"), never the placer's marker;
  // the teacher as the card names it (a meeting row can carry a stale name).
  const meetingBits = (p.meetings || [])
    .map(m => [meetingRoomText(m.room, p), m.day, m.start_time, m.end_time].filter(Boolean).join(' '))
    .join(' ');
  return [
    p.course_code,
    p.course_key,
    p.course_name,
    p.section,
    p.department,
    p.day,
    p.start_time,
    p.end_time,
    placementRoomText(p),
    window.TimetableSectionTeacher.teacherOf(p).name,
    meetingBits,
  ].filter(Boolean).join(' ').toUpperCase();
}

/* The filter's tokens (contract D): `programmes` and `shared` are the lens's
   (a programme the lens knows, and SHARED), the rest is `text`. Until 6d the
   programmes are the plans of the lens this page holds. */
function filterQuery() {
  const query = { programmes: [], shared: false, text: [] };
  workspaceSearchTokens().forEach(token => {
    const plan = planTokenFromSearch(token);
    if (plan === 'SHARED') query.shared = true;
    else if (plan) { if (!query.programmes.includes(plan)) query.programmes.push(plan); }
    else query.text.push(token);
  });
  return query;
}
const filterActive = query => Boolean(query.text.length || query.programmes.length || query.shared);

/* Every text token is in what the meeting shows. */
function textMatch(p, query = filterQuery()) {
  if (!query.text.length) return true;
  const text = placementText(p);
  return query.text.every(token => text.includes(token));
}

/* The lens: null with no programme or SHARED token; otherwise whether the
   meeting is in the union of the programmes (SHARED narrowing it to shared
   sections; SHARED alone, every shared section). The lens only dims, never
   folds (§7.1 #5). Interim (6c): the allocation rule of today's token,
   itemMatchesPlanToken; 6d dims by the per-plan export's roles instead. */
function inLens(p, query = filterQuery()) {
  if (!query.programmes.length && !query.shared) return null;
  if (query.shared && !itemMatchesPlanToken(p, 'SHARED')) return false;
  return !query.programmes.length || query.programmes.some(plan => itemMatchesPlanToken(p, plan));
}

/* Why a meeting is dimmed: 'filter' (the text), 'lens' (the programmes), both
   or neither. A dimmed card is an ordinary card (it drags, Enter, M, R). */
function placementDims(p, query = filterQuery()) {
  const dims = [];
  if (!textMatch(p, query)) dims.push('filter');
  if (inLens(p, query) === false) dims.push('lens');
  return dims;
}

function budgetMatchesSearch(c, search) {
  if (!search) return true;
  const courseLens = courseLensForItem(c);
  const lensBits = courseLens
    ? Object.entries(courseLens.plans || {})
        .filter(([, count]) => Number(count) > 0)
        .map(([plan, count]) => `${plan} ${count}`)
        .join(' ')
    : '';
  const text = [
    c.course_code,
    c.course_key,
    c.course_name,
    c.department,
    c.programme_term,
    lensBits,
  ].filter(v => v != null).join(' ').toUpperCase();
  return search.split(/\s+/).filter(Boolean).every(token => {
    const planToken = planTokenFromSearch(token);
    return planToken ? itemMatchesPlanToken(c, planToken) : text.includes(token);
  });
}

/* Words beside the filter (E21): the count is the meetings not dimmed of
   all the board's meetings; its title and the field's description add the
   tables folded for no match and the programme filter. */
function searchCountWords(shown, total, folded, programmes = []) {
  if (IS_AR) {
    const of = pluralAr(total, { zero: '0 لقاء', one: 'لقاء واحد', two: 'لقاءين', few: '{n} لقاءات', many: '{n} لقاءً', other: '{n} لقاء' });
    return `المطابق للتصفية: ${shown} من ${of}${folded ? `؛ الجداول المطويّة لعدم وجود مطابق: ${folded}` : ''}`
      + `${programmes.length ? `؛ تصفية البرامج: ${programmes.join('، ')}` : ''}.`;
  }
  return `${shown} of ${total} ${total === 1 ? 'meeting matches' : 'meetings match'}${folded ? `; tables folded for no match: ${folded}` : ''}`
    + `${programmes.length ? `; programme filter: ${programmes.join(', ')}` : ''}.`;
}
/* Text and attributes written only when they change: a live region is not
   read again, and the board's roving never hears a change it need not. */
function setText(element, text) {
  if (element && element.textContent !== text) element.textContent = text;
}
function setAttr(element, name, value) {
  if (value == null || value === '') {
    if (element.hasAttribute(name)) element.removeAttribute(name);
  } else if (element.getAttribute(name) !== value) {
    element.setAttribute(name, value);
  }
}

function updateSearchStatus() {
  const input = $('twSearch');
  if (!input) return;
  const q = activeWorkspaceSearch();
  const plan = planTokenFromSearch(q);
  setText($('twExportScope'), !q
    ? (IS_AR ? 'ملف XLSX لكل برنامج بأوراق حسب فصل الخطة' : 'One XLSX per programme, sheets by plan term')
    : plan
      ? (IS_AR ? `برنامج ${plan} فقط` : `Programme ${plan} only`)
      : (IS_AR ? `ما يطابق «${q}» فقط` : `Only what matches "${q}"`));
  if (!q) {
    setAttr(input, 'title', '');
    setText($('twSearchCount'), '');
    setText($('twSearchNote'), '');
    return;
  }
  const query = filterQuery();
  const total = TW.placements.length;
  const shown = TW.placements.filter(p => !placementDims(p, query).length).length;
  // Folded now: a table with no match the user has not opened.
  const folded = document.querySelectorAll('#twGrid .tw-fold-row:not([hidden]) .tw-fold-toggle[aria-expanded="false"]').length;
  const sentence = searchCountWords(shown, total, folded, lensParts(query));
  setAttr(input, 'title', sentence);
  setText($('twSearchNote'), sentence);
  setText($('twSearchCount'), IS_AR ? `${shown} من ${total}` : `${shown} of ${total}`);
}

/* What a table holds, in words (its fold, and its name for assistive technology). */
const TABLE_WORDS = {
  lecture: IS_AR ? 'المحاضرات' : 'Lectures',
  english: IS_AR ? 'الإنجليزية' : 'English',
  lab: IS_AR ? 'المعامل' : 'Labs',
};
/* "{n} meetings", nominative. */
const meetingsCount = n => (IS_AR
  ? pluralAr(n, { zero: 'لا لقاءات', one: 'لقاء واحد', two: 'لقاءان', few: '{n} لقاءات', many: '{n} لقاءً', other: '{n} لقاء' })
  : `${n} ${n === 1 ? 'meeting' : 'meetings'}`);
const matchesCount = n => (IS_AR
  ? pluralAr(n, { zero: 'لا شيء مطابق', one: 'مطابقة واحدة', two: 'مطابقتان', few: '{n} مطابقات', many: '{n} مطابقةً', other: '{n} مطابقة' })
  : `${n} ${n === 1 ? 'match' : 'matches'}`);
const foldLabel = (kind, n) => `${TABLE_WORDS[kind] || ''} · ${IS_AR ? 'لا شيء مطابق' : 'no match'} · ${meetingsCount(n)}`;

/* A dimmed card's reason: one shared note each (outside #twGrid, so writing
   it never touches the board), named by the card's aria-describedby. */
const DIM_NOTE_IDS = { filter: 'twDimNoteFilter', lens: 'twDimNoteLens' };
/* The lens's tokens in words: each programme isolated, then shared sections. */
const lensParts = query => [...query.programmes.map(isolatedText), ...(query.shared ? [IS_AR ? 'الشعب المشتركة' : 'shared sections'] : [])];
function dimNotes(query) {
  const parts = lensParts(query);
  return {
    filter: IS_AR ? 'خارج التصفية' : 'Outside the filter',
    lens: !parts.length ? ''
      : IS_AR ? `خارج تصفية البرامج: ${parts.join('، ')}` : `Outside the programme filter: ${parts.join(', ')}`,
  };
}

/* The filter and the lens over the drawn board, in place (contract D). It
   runs after a draw, a filter change, a tone change and a lens change, and
   writes attributes and text only — never the board's nodes — so a drag, a
   focused card and the keyboard's stop survive it:
   - each card's dim (`data-dim` filter and/or lens, its notes in
     `aria-describedby`, their words in its title);
   - each table the text matches nothing in folds: its fold row shows (the
     table's one stop) and its rows hide, unless the user opened it for this
     board and text (TW.foldOpen); the fold's tone counts;
   - each group heading's matches, the legend's dim line and the count.
   Roving reads its stops again only when a table folded or opened. A caller
   that has the issue model's `byPlacement` passes it (applyCardTones), so a
   tone update builds the model once. */
function applyGridView(byPlacement = buildBoardIssues().byPlacement) {
  const grid = $('twGrid');
  const query = filterQuery();
  const active = filterActive(query);
  const scope = `${TW.loadedBoardId ?? ''}|${query.text.join(' ')}`;
  if (scope !== TW.foldScope) {
    TW.foldScope = scope;
    TW.foldOpen.clear();
  }
  const byId = new Map(TW.placements.map(p => [String(p.id), p]));
  const notes = dimNotes(query);
  setText($('twDimNoteFilter'), notes.filter);
  setText($('twDimNoteLens'), notes.lens);
  const shownInGroup = new Map();
  let moved = false;
  let dimmed = false;
  grid.querySelectorAll('[role="grid"]').forEach(table => {
    const group = table.dataset.group || '-';
    const tones = { critical: 0, warning: 0 };
    let matched = 0;
    table.querySelectorAll('.tw-card[data-placement-id]').forEach(card => {
      const id = card.dataset.placementId;
      const p = byId.get(id);
      const dims = p && active ? placementDims(p, query) : [];
      if (!dims.includes('filter')) matched++;
      if (!dims.length) shownInGroup.set(group, (shownInGroup.get(group) || 0) + 1);
      if (dims.length) dimmed = true;
      setAttr(card, 'data-dim', dims.join(' '));
      setAttr(card, 'aria-describedby', dims.map(dim => DIM_NOTE_IDS[dim]).join(' '));
      const title = [card._twToneReason || '', card.dataset.identity || '', ...dims.map(dim => notes[dim])].filter(Boolean).join(' | ');
      if (card.title !== title) card.title = title;
      const tone = byPlacement.get(id)?.tone;
      if (tone in tones) tones[tone]++;
    });
    const fold = [...table.children].find(element => element.classList.contains('tw-fold-row'));
    const body = [...table.children].find(element => element.classList.contains('tw-fold-body'));
    const toggle = fold?.querySelector('.tw-fold-toggle');
    if (!fold || !body || !toggle) return;
    const noMatch = query.text.length > 0 && matched === 0;
    const open = !noMatch || TW.foldOpen.has(toggle.dataset.foldKey);
    if (fold.hidden !== !noMatch) { fold.hidden = !noMatch; moved = true; }
    if (body.hidden !== !open) { body.hidden = !open; moved = true; }
    setAttr(toggle, 'aria-expanded', String(open));
    fold.querySelectorAll('.tw-fold-tone').forEach(mark => {
      const n = tones[mark.dataset.tone] || 0;
      if (mark.hidden !== !n) mark.hidden = !n;
      const count = mark.querySelector('.tw-fold-n')?.firstChild;
      if (count && count.nodeValue !== String(n)) count.nodeValue = String(n);
    });
  });
  grid.querySelectorAll('.tw-group-head[data-group]').forEach(head => {
    const meta = head.querySelector('.tw-group-meta')?.firstChild;
    const text = `${head.dataset.what || ''}${active ? ` · ${matchesCount(shownInGroup.get(head.dataset.group) || 0)}` : ''}`;
    if (meta && meta.nodeValue !== text) meta.nodeValue = text;
  });
  const legend = $('twLegendDim');
  if (legend && legend.hidden !== !dimmed) legend.hidden = !dimmed;
  if (moved) boardNav?.refresh();
  updateSearchStatus();
}

/* A located card inside a folded table: the fold opens first (as a click on
   its toggle would, remembered for this board and text), so the card can be
   seen and the keyboard's stop can be set on it. */
function openFoldOf(card) {
  const body = card?.closest('.tw-fold-body');
  if (!body || !body.hidden) return false;
  const toggle = body.closest('[role="grid"]')?.querySelector('.tw-fold-toggle');
  body.hidden = false;
  if (toggle) toggle.setAttribute('aria-expanded', 'true');
  if (toggle?.dataset.foldKey) TW.foldOpen.add(toggle.dataset.foldKey);
  (toggle || body).dispatchEvent(new CustomEvent('ux-disclosure', { bubbles: true, detail: { open: true, key: null } }));
  return true;
}

function renderGridWithSlots(slots, labSlots) {
  const gridEl = $('twGrid');
  $('twEmptyState').style.display = 'none';
  gridEl.classList.remove('d-none');
  gridEl.style.display = '';
  gridEl.innerHTML = '';

  labSlots = labSlots || [];
  // Every meeting is drawn: the filter folds and dims in place
  // (applyGridView), so typing never draws the board again.
  const placements = TW.placements;

  // ── Split placements into student groups by section label ──
  const bySec = {};
  placements.forEach(p => {
    const sec = p.section || 'S1';
    if (!bySec[sec]) bySec[sec] = [];
    bySec[sec].push(p);
  });
  // Natural order (S2 before S10), and the group is named by its real label.
  const groupKeys = Object.keys(bySec).sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));
  const numGroups = groupKeys.length;
  // Under a "Section S2" heading every card is S2: the card names its course
  // only. A board with a single group has no heading, so its cards keep it.
  TW.cardsShowSection = numGroups <= 1;

  // Helper: classify a placement as lab (100-min) based on duration
  function isLabPlacement(p) {
    if (globalThis.TimetableMeetingPolicy?.isEnglish(p)) return false;
    const toMin = t => { const [h,m] = t.split(':').map(Number); return h*60+m; };
    return (toMin(p.end_time) - toMin(p.start_time)) > 80;
  }

  // Only the board's first table points at the keys line.
  TW.gridKeysDescribed = false;
  TW.gridTables = 0;
  // If no placements, show empty grid
  if (numGroups === 0) {
    _renderSingleGrid(gridEl, slots, [], 'tw-grid-table', null, 'lecture');
    if (labSlots.length) {
      gridEl.appendChild(gridSubheading(IS_AR ? 'المعامل (100 دقيقة)' : 'Labs (100 min)', 'lab'));
      _renderSingleGrid(gridEl, labSlots, [], 'tw-grid-table tw-lab-grid', null, 'lab');
    }
    applyCardTones();
    boardNav?.refresh();
    return;
  }

  groupKeys.forEach((secKey, gi) => {
    const groupPlacements = bySec[secKey];
    const tsIds = new Set(groupPlacements.map(p => p.term_section_id));
    const allForGroup = placements.filter(p => tsIds.has(p.term_section_id));

    // Separate lecture and lab placements
    const lecturePlacements = allForGroup.filter(p => !isLabPlacement(p));
    const labPlacements = allForGroup.filter(p => isLabPlacement(p));

    // Group header: the section label it really is, and what it holds (the
    // filter's matches are added in place, by applyGridView).
    if (numGroups > 1) {
      const courses = new Set(groupPlacements.map(p => p.course_code));
      const hdr = document.createElement('h3');
      hdr.className = 'tw-group-head';
      hdr.id = `twGroup-${gi}`;
      hdr.dataset.group = secKey;
      const what = IS_AR
        ? pluralAr(courses.size, { one: 'مقرر واحد', two: 'مقرران', few: '{n} مقررات', many: '{n} مقرراً', other: '{n} مقرر' })
        : `${courses.size} ${courses.size === 1 ? 'course' : 'courses'}`;
      hdr.dataset.what = what;
      hdr.innerHTML = `<span class="tw-group-name">${IS_AR ? 'الشعبة' : 'Section'} <bdi>${esc(secKey)}</bdi></span><span class="tw-group-meta">${esc(what)}</span>`;
      gridEl.appendChild(hdr);
    }

    // Lecture grid
    const gridInner = document.createElement('div');
    gridInner.className = 'tw-grid-inner';
    const englishPlacements = lecturePlacements.filter(p => globalThis.TimetableMeetingPolicy?.isEnglish(p));
    _renderSingleGrid(gridInner, slots, lecturePlacements.filter(p => !globalThis.TimetableMeetingPolicy?.isEnglish(p)), 'tw-grid-table', secKey, 'lecture');
    if (englishPlacements.length) {
      const heading = document.createElement('div'); heading.className = 'tw-english-heading';
      heading.textContent = IS_AR ? 'الإنجليزية · لقاءات متصلة لمدة ثلاث ساعات' : 'English · continuous three-hour meetings';
      gridInner.appendChild(heading);
      _renderSingleGrid(gridInner, TimetableMeetingPolicy.gridSlots(englishPlacements), englishPlacements, 'tw-grid-table tw-english-grid', secKey, 'english');
    }
    gridEl.appendChild(gridInner);

    // Lab grid (if any lab slots exist)
    if (labSlots.length) {
      gridEl.appendChild(gridSubheading(IS_AR ? 'المعامل' : 'Labs', 'lab'));
      const labInner = document.createElement('div');
      labInner.className = 'tw-grid-inner';
      _renderSingleGrid(labInner, labSlots, labPlacements, 'tw-grid-table tw-lab-grid', secKey, 'lab');
      gridEl.appendChild(labInner);
    }
  });
  if (TW.placements.length) gridEl.insertAdjacentHTML('beforeend', boardLegendHtml());
  // New cards take their tones, the filter's view and the selection at once,
  // not at the next redraw.
  markSelection();
  applyCardTones();
  boardNav?.refresh();
}

/* A table's name for assistive technology: what it holds, and its section
   group when the board has several. */
function gridLabel(secKey, kind) {
  const what = TABLE_WORDS[kind] || '';
  return secKey && TW.cardsShowSection === false ? `${IS_AR ? 'الشعبة' : 'Section'} ${secKey} · ${what}` : what;
}
const DAY_NAMES_EN = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday'];
const minutesOf = time => { const [h, m] = String(time || '0:0').split(':').map(Number); return h * 60 + m; };

function gridSubheading(text, kind) {
  const heading = document.createElement('div');
  heading.className = `tw-subhead tw-subhead-${kind}`;
  heading.textContent = text;
  return heading;
}

/* One table of the board: an ARIA grid (design record §7.3, 5a) whose rows
   are `.ux-grid-row` (display: contents) on the CSS grid. The keyboard moves
   over its cards, and over the empty cells, with UXControls.roving.
   Its first row is its fold (§7.2 item 6, 6c; contract D), hidden until the
   filter's text matches nothing in the table; the header, day rows follow in
   a `display: contents` rowgroup the fold hides. */
function _renderSingleGrid(container, slots, placements, className, secKey, kind = 'lecture') {
  // Transposed: columns = time slots, rows = days. The column count is the one
  // layout fact the stylesheet cannot know; everything else is in its classes.
  const numSlots = slots.length;
  const table = document.createElement('div');
  table.className = className;
  table.style.setProperty('--tw-slots', String(numSlots));
  table.setAttribute('role', 'grid');
  table.setAttribute('aria-label', gridLabel(secKey, kind));
  if (!TW.gridKeysDescribed) {
    table.setAttribute('aria-describedby', 'twGridKeys');
    TW.gridKeysDescribed = true;
  }
  const group = secKey || '-';
  table.dataset.group = group;
  table.dataset.kind = kind;
  // The fold: one gridcell holding the global disclosure toggle (a roving
  // stop, so never a Tab stop of its own), then its tone counts, each a
  // count beside its icon with the words for assistive technology; the
  // counts are written in place by applyGridView.
  TW.gridTables = (TW.gridTables || 0) + 1;
  const bodyId = `twFoldBody-${TW.gridTables}`;
  const fold = document.createElement('div');
  fold.className = 'ux-grid-row tw-fold-row';
  fold.setAttribute('role', 'row');
  fold.hidden = true;
  const foldCell = document.createElement('div');
  foldCell.className = 'tw-fold-cell';
  foldCell.setAttribute('role', 'gridcell');
  // It spans the table: the day column and every slot.
  foldCell.setAttribute('aria-colspan', String(numSlots + 1));
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'ux-disclosure-toggle tw-fold-toggle';
  toggle.tabIndex = -1;
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', bodyId);
  toggle.dataset.foldKey = `${group}|${kind}`;
  toggle.dataset.focusKey = `fold:${group}|${kind}`;
  toggle.innerHTML = '<span class="ux-disclosure-label"></span><span class="tw-fold-tones">'
    + ['critical', 'warning'].map(tone => `<span class="tw-fold-tone" data-tone="${tone}" hidden><span class="ux-tone-icon" data-tone="${tone}" aria-hidden="true"></span><span class="visually-hidden">${TONE_LEAD[tone]}: </span><span class="tw-fold-n">0</span></span>`).join('')
    + '</span><span class="ux-disclosure-chevron" aria-hidden="true"></span>';
  foldCell.appendChild(toggle);
  fold.appendChild(foldCell);
  table.appendChild(fold);
  const body = document.createElement('div');
  body.className = 'tw-fold-body';
  body.setAttribute('role', 'rowgroup');
  body.id = bodyId;
  table.appendChild(body);
  const row = () => {
    const element = document.createElement('div');
    element.className = 'ux-grid-row';
    element.setAttribute('role', 'row');
    body.appendChild(element);
    return element;
  };

  // One sticky header row: the slot's number above its times.
  const head = row();
  const corner = document.createElement('div');
  corner.className = 'tw-grid-header tw-grid-corner';
  corner.setAttribute('role', 'columnheader');
  corner.innerHTML = `<span class="visually-hidden">${IS_AR ? 'اليوم' : 'Day'}</span>`;
  head.appendChild(corner);
  slots.forEach((s, i) => {
    const hdr = document.createElement('div');
    hdr.className = 'tw-grid-header';
    hdr.setAttribute('role', 'columnheader');
    hdr.setAttribute('aria-label', `${IS_AR ? 'الفترة' : 'Slot'} ${i + 1}${IS_AR ? '،' : ','} \u2066${s.start}–${s.end}\u2069`);
    hdr.innerHTML = `<span class="tw-slot-no">${i + 1}</span><bdi class="tw-slot-time">${esc(s.start)}–${esc(s.end)}</bdi>`;
    head.appendChild(hdr);
  });

  // Day rows
  DAYS.forEach((day, di) => {
    const line = row();
    // Day label
    const lbl = document.createElement('div');
    lbl.className = 'tw-grid-slot-label';
    lbl.setAttribute('role', 'rowheader');
    if (!IS_AR) lbl.setAttribute('aria-label', DAY_NAMES_EN[di]);
    lbl.textContent = DAY_LABELS[di];
    line.appendChild(lbl);

    // Slot cells
    slots.forEach(slot => {
      const cell = document.createElement('div');
      cell.className = 'tw-cell';
      cell.setAttribute('role', 'gridcell');
      cell.dataset.day = day;
      cell.dataset.start = slot.start;
      cell.dataset.end = slot.end;
      cell.dataset.uxCol = String(minutesOf(slot.start));
      cell.dataset.focusKey = `cell:${secKey || '-'}:${kind}|${day}|${slot.start}`;

      cell.addEventListener('dragover', onDragOver);
      cell.addEventListener('dragleave', onDragLeave);
      cell.addEventListener('drop', onDrop);

      // Find placements in this cell
      const cellPlacements = placements.filter(
        p => p.day === day && p.start_time === slot.start
      );

      if (cellPlacements.length === 1) {
        const p = cellPlacements[0];
        const card = createPlacementCard(p);
        cell.appendChild(card);
      } else if (cellPlacements.length > 1) {
        // Sections sharing a time are not a clash by themselves (parallel
        // sections for other cohorts): the model tones what the server calls one.
        // Each says where it is in the stack (the keyboard walks it with ↑/↓).
        cellPlacements.forEach((p, i) => {
          const card = createPlacementCard(p);
          card.insertAdjacentHTML('beforeend', `<span class="visually-hidden">${IS_AR
            ? `(${i + 1} من ${cellPlacements.length} في هذا الوقت)`
            : `(${i + 1} of ${cellPlacements.length} at this time)`}</span>`);
          cell.appendChild(card);
        });
      }

      line.appendChild(cell);
    });
  });

  // What the fold says it holds: the meetings drawn in this table.
  toggle.querySelector('.ux-disclosure-label').textContent = foldLabel(kind, body.querySelectorAll('.tw-card').length);
  container.appendChild(table);
}

/* A placement's room in words, never the placer's "UNASSIGNED" marker. */
function placementRoomText(p) {
  if (globalThis.TimetableMeetingPolicy?.isEnglish(p)) return TimetableMeetingPolicy.roomLabel(p, IS_AR);
  if (p.is_online) return IS_AR ? 'عن بُعد' : 'Online';
  // The placer's marker on a section that needs no room is not a missing room.
  if (p.room_state === 'unassigned' || p.room === 'UNASSIGNED') return p.needs_room === false ? '' : (IS_AR ? 'بلا قاعة' : 'No room');
  if (p.room_state === 'unset' || !String(p.room || '').trim()) return '';
  return p.room;
}
function meetingRoomText(room, p = null) {
  if (p?.is_online) return IS_AR ? 'عن بُعد' : 'Online';
  if (room === 'UNASSIGNED') return p?.needs_room === false ? '—' : (IS_AR ? 'بلا قاعة' : 'No room');
  return String(room || '').trim() || '—';
}

function createPlacementCard(p) {
  const card = document.createElement('div');
  card.className = 'tw-card';
  card.setAttribute('role', 'button');
  card.dataset.focusKey = `placement:${p.id}`;
  // Another scenario owns a shared section's time and room: the server refuses
  // its move, so it does not drag (Remove still works).
  const movable = !p.is_locked && !p.shared_with_other_scenario;
  card.draggable = workspaceCanEdit() && movable;
  card.dataset.twDraggable = 'true';
  card.dataset.twMovable = String(movable);
  card.dataset.placementId = p.id;
  // The course's hue, not a fill: light mode shows the pastel, dark mode mixes
  // it into the card surface so the board is not a wall of light tiles.
  card.style.setProperty('--tw-card-hue', courseColor(p.course_key || p.course_code));
  card.dataset.courseKey = courseColorKey(p.course_key || p.course_code);
  if (p.is_locked) card.classList.add('locked');

  // One teacher per section, by the shared rule (a section still to be
  // decided shows none: its meeting row may carry a stale name).
  const teacherInfo = window.TimetableSectionTeacher.teacherOf(p);
  const teacher = teacherInfo.state === 'named' ? teacherInfo.name : '';
  const courseName = compactCourseName(p.course_name);
  const showCourseName = courseName && hasDuplicateCourseCode(p.course_code);
  // Its identity; applyCardTones() puts the reason for a tone in front of it.
  card.dataset.identity = [p.course_key, courseName].filter(Boolean).join(' | ');
  card.title = card.dataset.identity;
  // Anatomy (design record §7.2, PR 3), within the ~70 px a 96 px slot leaves:
  //   head  code (never shrinks) · section (gives way first)
  //   meta  room (a short word) · teacher (only where the card is wide enough)
  //         then the marks: tone (set by applyCardTones), lock, shared.
  // Capacity is in Selection, not on the card.
  const english = globalThis.TimetableMeetingPolicy?.isEnglish(p);
  const room = placementRoomText(p);
  const lockWords = IS_AR ? 'الوقت مقفل؛ القاعة قابلة للتغيير' : 'Time locked; its room can change';
  const sharedWords = IS_AR ? 'يملكها سيناريو آخر' : 'Another scenario owns it';
  const marks = `<span class="tw-card-tone ux-tone-icon" role="img" hidden></span>`
    + (p.is_locked ? `<span class="ux-icon" data-icon="lock" role="img" aria-label="${lockWords}" title="${lockWords}"></span>` : '')
    + (p.shared_with_other_scenario ? `<span class="ux-icon" data-icon="shared" role="img" aria-label="${sharedWords}" title="${sharedWords}"></span>` : '');
  const roomState = p.room_state === 'unassigned' && p.needs_room !== false ? 'unassigned' : '';
  const dayIndex = DAYS.indexOf(p.day);
  const when = `${IS_AR ? (DAY_LABELS[dayIndex] || p.day) : (DAY_NAMES_EN[dayIndex] || p.day)} \u2066${p.start_time}–${p.end_time}\u2069`;
  card.innerHTML = `
    <div class="cc"><span class="tw-card-code">${esc(p.course_code)}</span>${TW.cardsShowSection === false ? '' : `<bdi class="tw-card-section">${esc(p.section)}</bdi>`}</div>
    ${english ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))}</div>` : ''}
    ${showCourseName ? `<div class="tw-card-name" dir="auto">${esc(courseName)}</div>` : ''}
    <div class="ci">${room ? `<bdi class="tw-card-room"${roomState ? ` data-room-state="${roomState}"` : ''}>${esc(room)}</bdi>` : ''}${teacher ? `<bdi class="tw-card-teacher">${esc(teacher)}</bdi>` : ''}<span class="tw-card-marks">${marks}</span></div>
    <span class="visually-hidden">${esc(when)}</span>
  `;

  card.addEventListener('dragstart', (e) => {
    if (!workspaceCanEdit() || !movable) { e.preventDefault(); return; }
    e.dataTransfer.setData('text/plain', JSON.stringify({type:'move', placement_id: p.id}));
    e.dataTransfer.effectAllowed = 'move';
  });

  // A click selects the section and shows everything about it in the
  // inspector (which also opens if it was collapsed).
  card.addEventListener('click', () => {
    TW.selectedPlacementId = p.id;
    markSelection();
    document.querySelectorAll('.tw-right .tw-panel-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === 'selection');
    });
    renderRightPanel('selection');
    window.dispatchEvent(new CustomEvent('tw:pane-request', { detail: { pane: 'right', open: true } }));
  });
  return card;
}

/* The selected card (and its name for assistive technology), and an echo on
   the other meetings of the same course: where else this course sits. */
function markSelection() {
  const selected = document.querySelector(`#twGrid .tw-card[data-placement-id="${TW.selectedPlacementId}"]`);
  const course = selected?.dataset.courseKey || null;
  document.querySelectorAll('#twGrid .tw-card').forEach(card => {
    const isSelected = card === selected;
    card.classList.toggle('is-selected', isSelected);
    if (isSelected) card.setAttribute('aria-current', 'true'); else card.removeAttribute('aria-current');
    card.classList.toggle('is-same-course', Boolean(course) && !isSelected && card.dataset.courseKey === course);
  });
}

/* What the marks on the board mean: one line after the last table. */
function boardLegendHtml() {
  const item = (swatch, words) => `<span class="ux-legend-item">${swatch}<span>${words}</span></span>`;
  const swatch = name => `<span class="ux-legend-swatch" data-swatch="${name}" aria-hidden="true"></span>`;
  const icon = (attr, value) => `<span class="${attr === 'tone' ? 'ux-tone-icon' : 'ux-icon'}" data-${attr}="${value}" aria-hidden="true"></span>`;
  return `<div class="ux-legend tw-legend" id="twLegend" role="note" aria-labelledby="twLegendTitle">`
    + `<span class="ux-legend-title" id="twLegendTitle">${IS_AR ? 'دليل الرموز' : 'Legend'}</span>`
    + item(swatch('critical') + icon('tone', 'critical'), TONE_LEAD.critical)
    + item(swatch('warning') + icon('tone', 'warning'), TONE_LEAD.warning)
    + item(icon('icon', 'lock'), IS_AR ? 'الوقت مقفل؛ القاعة قابلة للتغيير' : 'Time locked; its room can change')
    + item(icon('icon', 'shared'), IS_AR ? 'يملكها سيناريو آخر' : 'Another scenario owns it')
    + item(swatch('selected'), IS_AR ? 'الشعبة المحددة' : 'The selected section')
    + item(swatch('same'), IS_AR ? 'لقاءات المقرر نفسه الأخرى' : 'The same course’s other meetings')
    // Shown by applyGridView while the filter dims a card.
    + `<span class="ux-legend-item" id="twLegendDim" hidden>${swatch('dim')}<span>${IS_AR ? 'باهتة: خارج التصفية' : 'Dimmed: outside the filter'}</span></span>`
    + `<span class="ux-legend-item">${IS_AR ? 'اللون يساعد على إيجاد المقرر' : 'Colour helps find a course'}</span>`
    + `</div>`;
}

function renderUnplacedList() {
  const list = $('twSectionList');
  const search = sectionBudgetSearch();
  if (TW.unplacedSearch !== search) { TW.unplacedSearch = search; TW.unplacedVisibleLimit = 100; }
  const visibleLimit = TW.unplacedVisibleLimit || 100;
  list.innerHTML = '';

  let items = TW.unplacedSections;
  if (search) {
    items = items.filter(s =>
      (s.course_code || '').toUpperCase().includes(search) ||
      (s.course_name || '').toUpperCase().includes(search) ||
      (s.section || '').toUpperCase().includes(search)
    );
  }

  if (!items.length) {
    list.innerHTML = `<p class="tw-pane-empty">${IS_AR ? 'لا توجد شعب متاحة تطابق البحث.' : 'No available sections match the filter.'}</p>`;
    setLeftCount(0);
    return;
  }

  const unplacedGrid = listGrid('twLeftTitle', true);
  list.appendChild(unplacedGrid);
  items.slice(0, visibleLimit).forEach(s => {
    const div = listRow(unplacedGrid, `ts:${s.term_section_id}`);
    div.className = 'tw-section-item';
    div.draggable = workspaceCanEdit();
    div.dataset.twDraggable = 'true';
    const instructor = (s.meetings && s.meetings[0]) ? s.meetings[0].instructor : '';
    const meetingInfo = (s.meetings || []).map(m => `${m.day} ${m.start_time}-${m.end_time}`).join(', ');
    div.innerHTML = `
      <span class="code">${esc(s.course_code)} ${esc(s.section)}</span>
      <span class="meta ux-meta">${[instructor, s.available_capacity ? (IS_AR ? `سعة ${s.available_capacity}` : `cap ${s.available_capacity}`) : ''].filter(Boolean).map(part => `<span>${esc(part)}</span>`).join('')}</span>
      <span class="meta">${esc(meetingInfo)}</span>
    `;
    if (globalThis.TimetableMeetingPolicy?.isEnglish(s)) {
      const choose = document.createElement('button'); choose.type = 'button'; choose.className = 'tw-pattern-action'; choose.dataset.twMutation = '';
      choose.disabled = !workspaceCanEdit(); choose.textContent = IS_AR ? 'اختر النمط الأسبوعي' : 'Choose weekly pattern';
      choose.tabIndex = -1;
      div._twEnglish = () => chooseEnglishWorkspacePattern({ ...s, type: 'create', section_label: s.section });
      choose.addEventListener('click', () => div._twEnglish());
      div.appendChild(choose);
    }
    div.addEventListener('dragstart', (e) => {
      if (!workspaceCanEdit()) { e.preventDefault(); return; }
      const firstMeeting = (s.meetings && s.meetings[0]) || {};
      e.dataTransfer.setData('text/plain', JSON.stringify({
        type: 'create',
        course_code: s.course_code,
        course_key: s.course_key,
        course_number: s.course_number,
        section_label: s.section,
        term_section_id: s.term_section_id,
        default_day: firstMeeting.day || '',
        default_start: firstMeeting.start_time || '',
        default_end: firstMeeting.end_time || '',
        default_room: firstMeeting.room || '',
      }));
      e.dataTransfer.effectAllowed = 'copy';
    });
  });
  if (items.length > visibleLimit) {
    const more = document.createElement('button');
    more.type = 'button'; more.className = 'btn btn-sm btn-neutral tw-show-more';
    more.textContent = IS_AR ? `عرض المزيد (${visibleLimit}/${items.length})` : `Show more (${visibleLimit}/${items.length})`;
    more.addEventListener('click', () => {
      // The keyboard goes on at the first row it had not seen.
      listNav?.active(`ts:${items[visibleLimit].term_section_id}`, { focus: true });
      TW.unplacedVisibleLimit = visibleLimit + 100;
      renderUnplacedList();
      listNav?.element()?.focus({ preventScroll: true });
    });
    list.appendChild(more);
  }
  listNav?.refresh();
  setLeftCount(items.length);
}

/* The left pane's count: what is still to be placed. Shown in its header and
   on its collapsed rail, so a hidden list never hides unfinished work. */
function setLeftCount(count) {
  const text = Number.isFinite(count) && count > 0 ? count.toLocaleString() : '';
  if ($('twLeftCount')) $('twLeftCount').textContent = text;
  if ($('twLeftRailCount')) $('twLeftRailCount').textContent = text;
}

/* Fixed English patterns are chosen as one section action. */
async function chooseEnglishWorkspacePattern(payload) {
  if (!workspaceCanEdit() || TW.englishPicker) return null;
  const policy = globalThis.TimetableMeetingPolicy;
  const placement = payload.type === 'move' ? TW.placements.find(row => row.id === payload.placement_id) : null;
  const source = placement || payload;
  if (!policy?.isEnglish(source)) return null;
  const scenarioId = TW.scenarioId, boardId = TW.boardId;
  // M on a card (5e): the keyboard comes back to the board after the chooser.
  const fromBoard = Boolean(placement) && $('twGrid').contains(document.activeElement);
  TW.englishPicker = true;
  try {
    const bundle = placement ? TW.placements.filter(row => row.term_section_id === placement.term_section_id) : [];
    if (bundle.some(row => row.is_locked)) { notify.warning(IS_AR ? 'افتح قفل الشعبة قبل تغيير النمط.' : 'Unlock the section before changing its pattern.'); return null; }
    const choices = placement
      ? await twFetch(`/ops/tw/placements/${placement.id}/slot-candidates/`)
      : await twFetch(`/ops/tw/boards/${boardId}/planned-slot-candidates/?${new URLSearchParams({ course_code: source.course_code, course_key: source.course_key || source.course_code, section_label: source.section_label || 'S1' })}`);
    if (!choices) return null;
    const current = choices.section_policy?.current_pattern_id || policy.identify(bundle);
    if (placement && !current) { notify.warning(IS_AR ? 'هذا الجدول المحفوظ لا يستخدم نمط الإنجليزية الجديد.' : 'This saved section does not use the new English pattern.'); return null; }
    const selected = await policy.choose(source.course_code, { arabic: IS_AR, candidates: choices.candidates || [], currentPatternId: current });
    // The chooser gives the keyboard back to the node it was opened from;
    // a card the board drew again meanwhile is no longer there, and the
    // board's own stop takes it (D16b).
    const active = document.activeElement;
    if (fromBoard && (!active || active === document.body || !active.isConnected)) boardNav?.restore();
    if (!selected || TW.scenarioId !== scenarioId || TW.boardId !== boardId || !workspaceCanEdit()) return null;
    if (selected.pattern_id === current) return null;
    const token = beginWorkspaceOperation(placement ? 'switch-pattern' : 'create-pattern');
    if (!token) return null;
    try {
      const path = placement ? `/ops/tw/placements/${placement.id}/switch-pattern/`
        : `/ops/tw/placements/${payload.type === 'create' ? 'create' : 'create-planned'}/`;
      const body = placement ? { pattern_id: selected.pattern_id } : {
        board_id: boardId, term_section_id: payload.term_section_id, course_code: source.course_code,
        course_key: source.course_key || source.course_code, course_name: source.course_name || source.course_code,
        section_label: source.section_label, capacity: source.max_per_section || 40, pattern_id: selected.pattern_id,
        meetings: selected.meetings.map(row => ({ ...row, room: payload.default_room || '' })),
      };
      const data = await twFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), _workspaceToken: token });
      if (!data || data.ok === false) return null;
      const rows = data.placements || [data.placement].filter(Boolean);
      if (placement) TW.undoStack.push({ type: 'pattern', placement_id: placement.id,
        old_pattern_id: data.before_section_policy?.current_pattern_id || current, new_pattern_id: selected.pattern_id,
        before_placements: policy.snapshot(data.before_placements || bundle), placements: policy.snapshot(rows) });
      else TW.undoStack.push({ type: 'create', placement_id: data.placement.id, board_id: boardId,
        term_section_id: data.placement.term_section_id, day: rows[0].day, start_time: rows[0].start_time,
        end_time: rows[0].end_time, room: rows[0].room || '', meetings: policy.snapshot(rows) });
      TW.redoStack = []; notifyInstructorClashBackstop(data);
      // The switch recreates the section's rows: the board's stop goes to
      // the new row of the meeting's day on this board (the keyboard with
      // it, when it was on the old card).
      if (placement) {
        const here = rows.filter(row => String(row.board_id ?? boardId) === String(boardId));
        const next = here.find(row => row.day === placement.day) || here[0] || rows[0];
        if (next?.id != null) boardNav?.active(`placement:${next.id}`);
      }
      await selectBoard(boardId); updateUndoRedoButtons();
      notify.success(IS_AR ? 'تم حفظ نمط الشعبة كاملاً.' : 'Full section pattern saved.');
      return data;
    } finally { finishWorkspaceOperation(token); }
  } finally { TW.englishPicker = false; }
}

/* ── Drag & Drop ── */
function onDragOver(e) {
  if (!workspaceCanEdit()) return;
  e.preventDefault();
  e.currentTarget.classList.add('drop-valid');
}

function onDragLeave(e) {
  e.currentTarget.classList.remove('drop-valid', 'drop-warning', 'drop-critical');
}

// See page-timetable-workspace-split.js for the rationale: `validation` is
// board-scoped and free-text-instructor keyed, so a cross-board instructor
// double-booking is invisible to it. Scoped to the section just touched.
function notifyInstructorClashBackstop(data) {
  const words = instructorBackstopWords(data);
  if (words) notify.warning(words);
}
function instructorBackstopWords(data) {
  const b = (data && data.instructor_clash_backstop) || {};
  if (!b.checked) return '';
  const rows = b.involving_this_section || [];
  if (!rows.length) return '';
  return IS_AR
    ? `تعارض للمحاضر: ${rows.length} حصة متداخلة عبر اللوحات`
    : `Instructor double-booked — ${rows.length} overlapping session(s) across boards`;
}

/* ── After an edit (design record §7.3.2 D, 5e) ──
   What a move left behind, read from the board's one issue model once the
   board has been read again (selectBoard), so the report says what the card's
   ring and Issues say: the edit's words, then the worst lead and the kinds of
   every fresh critical or warning row about these placements (a row from a
   check older than the edit is not counted; info never is). Drag and the
   Move dialog share it. A dialog's report goes where dialogSay puts words —
   the alert line of the helper dialog open by then, named when that dialog
   is about another meeting — and the instructor backstop with it (a toast
   behind a modal is never seen); otherwise each is a toast, as before. */
function editReport(verbWords, ids, hint = '') {
  const wanted = (ids || []).map(String);
  const rows = buildBoardIssues().rows.filter(row => !row.pending && row.tone !== 'info' && row.ids.some(id => wanted.includes(id)));
  const worst = rows.some(row => row.tone === 'critical') ? 'critical' : rows.length ? 'warning' : '';
  let text = verbWords;
  if (worst) text += ` · ${TONE_LEAD[worst]}: ${[...new Set(rows.map(row => row.kind))].map(issueKindLabel).join(', ')}`;
  // A room hint only beside a room problem this report names.
  if (hint && rows.some(row => String(row.kind).startsWith('room'))) text += ` · ${hint}`;
  return { text, tone: worst ? 'warning' : 'success' };
}
function reportEdit(verbWords, ids, { dialog = null, about = null, hint = '', data = null } = {}) {
  const said = editReport(verbWords, ids, hint);
  const backstop = instructorBackstopWords(data);
  if (dialog && sayingDialog(dialog, said.text)) {
    dialogSay(dialog, backstop ? `${said.text} · ${backstop}` : said.text, backstop ? 'warning' : said.tone, about);
    return;
  }
  notify[said.tone](said.text);
  if (backstop) notify.warning(backstop);
}
/* "Moved to Monday 13:00–14:15." — the time isolated (plain text). */
function movedWords(day, start, end) {
  const name = (IS_AR ? DAY_LABELS : DAY_NAMES_EN)[DAYS.indexOf(String(day || '').toUpperCase())] || day || '';
  return IS_AR ? `نُقلت إلى ${name} ${isolatedText(`${start}–${end}`)}.` : `Moved to ${name} ${isolatedText(`${start}–${end}`)}.`;
}

/* One move of one meeting, for drag and the Move dialog alike: the body is
   exactly {day, start_time, end_time} (no room: the meeting keeps its room),
   and a saved move is undone by the same entry whichever sent it, holding
   the raw times the board had before. With a dialog the failure is its own
   {code} (dialogWrite: nothing toasted); without one twFetch says it, as a
   drop always did. */
async function commitPlacementMove(id, { day, start_time, end_time }, { dialog = null } = {}) {
  const before = TW.placements.find(row => row.id === id);
  const url = `/ops/tw/placements/${id}/move/`;
  const body = { day, start_time, end_time };
  let result;
  if (dialog) result = await dialogWrite(dialog, url, body);
  else {
    const data = await twFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    result = data ? { data } : { code: 'FAILED' };
  }
  if (result.code) return result;
  TW.undoStack.push({ type: 'move', placement_id: id,
    old_day: before ? before.day : '', old_start: before ? before.start_time : '', old_end: before ? before.end_time : '',
    new_day: day, new_start: start_time, new_end: end_time });
  TW.redoStack = [];
  return result;
}

/* "Placed AI222 S2." — the course and the label isolated (plain text). */
function placedWords(code, label) {
  return IS_AR ? `وُضعت ${isolatedText(code)} ${isolatedText(label)}.` : `Placed ${isolatedText(code)} ${isolatedText(label)}.`;
}
/* The body of a planned section's create: the course (code, key and name),
   the label, the seats and every meeting as {day, start_time, end_time}. */
function plannedCreateBody(request, boardId, label, meetings) {
  return {
    board_id: boardId, course_code: request.course_code, course_key: request.course_key, course_name: request.course_name,
    section_label: label, capacity: request.capacity,
    meetings: meetings.map(({ day, start, end }) => ({ day, start_time: start, end_time: end })),
  };
}
/* One create of a planned section (5f), for the Place dialog and a drop
   alike: one body (plannedCreateBody), and one undo entry, `section_create`,
   holding the ids the server made, the body sent and the meetings as the
   server stored them (raw days and times), so Undo removes every meeting
   and Redo creates them all again (D23). With a dialog every failure is its
   own {code, status} (dialogWrite: nothing toasted); without one the
   server's "complete pattern" refusal is the caller's {code} (a drop then
   opens Place…), and any other is twFetch's toast, as a drop always did. */
async function commitPlannedCreate(body, { dialog = null } = {}) {
  const url = '/ops/tw/placements/create-planned/';
  let result;
  if (dialog) result = await dialogWrite(dialog, url, body);
  else {
    let code = '';
    const data = await twFetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      _onError: answer => {
        if (answer?.error?.code !== 'FULL_SECTION_PATTERN_REQUIRED') return false;
        code = answer.error.code;
        return true;
      },
    });
    result = data ? { data } : { code: code || 'FAILED' };
  }
  if (result.code) return result;
  const rows = result.data.placements?.length ? result.data.placements : [result.data.placement].filter(Boolean);
  TW.undoStack.push({ type: 'section_create', placement_ids: rows.map(row => row.id), board_id: body.board_id, payload: body,
    meetings: globalThis.TimetableMeetingPolicy.snapshot(rows) });
  TW.redoStack = [];
  return { ...result, rows };
}

async function onDrop(e) {
  e.preventDefault();
  if (!workspaceCanEdit()) return;
  const boardAtStart = TW.boardId;
  const scenarioAtStart = TW.scenarioId;
  const cell = e.currentTarget;
  cell.classList.remove('drop-valid', 'drop-warning', 'drop-critical');

  let payload;
  try { payload = JSON.parse(e.dataTransfer.getData('text/plain')); } catch { return; }

  const day = cell.dataset.day;
  const startTime = cell.dataset.start;
  const endTime = cell.dataset.end;
  const source = payload.type === 'move' ? TW.placements.find(row => row.id === payload.placement_id) : payload;
  if (globalThis.TimetableMeetingPolicy?.isEnglish(source)) {
    await chooseEnglishWorkspacePattern(payload);
    return;
  }

  if (payload.type === 'create_planned') {
    // A planned section is created with its whole weekly pattern
    // (create_planned_section_placements refuses less). A course that meets
    // more than once a week opens Place… on the dropped time (5f, D22c):
    // the patterns that include it, nothing sent. One that meets once is
    // placed here, through the dialog's own commit and report; the server's
    // "complete pattern" refusal (an older budget row) opens Place… the
    // same way, the budget read again.
    const request = placeRequestOf(payload);
    const place = () => openPlaceDialog(request, {
      origin: `place:${request.course_key}|${request.section_label}`, includes: { day, start: startTime, end: endTime },
    });
    if (request.required > 1) {
      place();
      return;
    }
    const created = await commitPlannedCreate(plannedCreateBody(request, boardAtStart, request.section_label, [{ day, start: startTime, end: endTime }]));
    if (created.code === 'FULL_SECTION_PATTERN_REQUIRED') {
      loadBudget();
      place();
      return;
    }
    if (created.code || TW.scenarioId !== scenarioAtStart) return;
    await selectBoard(TW.boardId);
    updateUndoRedoButtons();
    reportEdit(placedWords(request.course_code, request.section_label), created.rows.map(row => row.id), { data: created.data });
    return;
  } else if (payload.type === 'create') {
    const data = await twFetch('/ops/tw/placements/create/', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        board_id: boardAtStart,
        term_section_id: payload.term_section_id,
        day: day,
        start_time: startTime,
        end_time: endTime,
        room: payload.default_room || '',
      }),
    });
    if (!data) return;
    TW.undoStack.push({ type: 'create', placement_id: data.placement.id,
      board_id: boardAtStart, term_section_id: payload.term_section_id,
      day: day, start_time: startTime, end_time: endTime, room: payload.default_room || '' });
    TW.redoStack = [];
    // Its report reads the board's issue model, as every placement's does (D24).
    if (TW.scenarioId !== scenarioAtStart) return;
    await selectBoard(TW.boardId);
    updateUndoRedoButtons();
    reportEdit(placedWords(payload.course_code, payload.section_label), [data.placement.id], { data });
    return;
  } else if (payload.type === 'move') {
    // One write and one report for every move of a meeting (5e): one body,
    // one undo shape, and a report read from the board once it has been
    // read again.
    const moved = await commitPlacementMove(payload.placement_id, { day, start_time: startTime, end_time: endTime });
    if (moved.code || TW.scenarioId !== scenarioAtStart) return;
    await selectBoard(TW.boardId);
    updateUndoRedoButtons();
    reportEdit(movedWords(day, startTime, endTime), [payload.placement_id], { data: moved.data });
    return;
  }

  // Refresh board
  if (TW.scenarioId !== scenarioAtStart) return;
  await selectBoard(TW.boardId);
  updateUndoRedoButtons();
}

/* ── Undo / Redo ── */
function updateUndoRedoButtons() {
  syncWorkspaceControls();
}

/* History retains the original action in both stacks. Recreated rows receive
 * their new ID without losing the board/section/time data for another cycle. */
async function applyHistoryAction(action, undo, token) {
  const post = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body), _workspaceToken: token });
  if (action.type === 'pattern') {
    return twFetch(`/ops/tw/placements/${action.placement_id}/switch-pattern/`, post({
      pattern_id: undo ? action.old_pattern_id : action.new_pattern_id,
      placements: undo ? action.before_placements : action.placements,
    }));
  }
  if (action.type === 'move') {
    const prefix = undo ? 'old' : 'new';
    return twFetch(`/ops/tw/placements/${action.placement_id}/move/`, post({
      day: action[`${prefix}_day`], start_time: action[`${prefix}_start`], end_time: action[`${prefix}_end`],
    }));
  }
  // A planned section's create (5f): undone meeting by meeting, last first;
  // redone by creating it again with every meeting (the ids remapped).
  if (action.type === 'section_create') {
    if (undo) return undoSectionCreate(action, post);
    return twFetch('/ops/tw/placements/create-planned/', post({ ...action.payload,
      meetings: (action.meetings || []).map(({ day, start_time, end_time }) => ({ day, start_time, end_time })) }));
  }
  // A room change goes back and forth by its room alone: no time is ever
  // sent, so it stays room-only and a time lock holds.
  if (action.type === 'room') {
    return twFetch(`/ops/tw/placements/${action.placement_id}/move/`, post({ room: undo ? action.old_room : action.new_room }));
  }
  if (!['create', 'remove'].includes(action.type)) return null;
  const recreate = (action.type === 'create') !== undo;
  return recreate
    ? twFetch('/ops/tw/placements/create/', post({ board_id: action.board_id,
      term_section_id: action.term_section_id, day: action.day, start_time: action.start_time,
      end_time: action.end_time, room: action.room || '', ...(action.meetings ? { meetings: action.meetings } : {}) }))
    : twFetch(`/ops/tw/placements/${action.placement_id}/remove/`, post({}));
}
/* Undo of a section's create (D23): each meeting removed, the last made
   first; a meeting a removal already took with it (a policy bundle answers
   every meeting it removed) is skipped. A removal refused stops there: the
   entry keeps the meetings not yet removed, and the answer is {partial}
   when some went (the board is then read again), else null. */
async function undoSectionCreate(action, post) {
  const ids = [...(action.placement_ids || [])];
  const gone = new Set();
  for (const id of [...ids].reverse()) {
    if (gone.has(String(id))) continue;
    const data = await twFetch(`/ops/tw/placements/${id}/remove/`, post({}));
    if (!data || data.ok === false) {
      action.placement_ids = ids.filter(left => !gone.has(String(left)));
      return gone.size ? { ok: false, partial: true } : null;
    }
    gone.add(String(id));
    (data.removed_placements || []).forEach(row => gone.add(String(row.id)));
  }
  return { ok: true };
}
async function runHistoryAction(undo) {
  if (!workspaceCanEdit()) return;
  const from = undo ? TW.undoStack : TW.redoStack;
  const to = undo ? TW.redoStack : TW.undoStack;
  const action = from.at(-1);
  if (!action) return;
  const token = beginWorkspaceOperation(undo ? 'undo' : 'redo');
  if (!token) return;
  try {
    const data = await applyHistoryAction(action, undo, token);
    if (!data || data.ok === false || TW.scenarioId !== token.scenarioId) {
      // Part of a section's create undone: the entry stays with what is
      // left, and the board shows what went (D23).
      if (data?.partial && TW.scenarioId === token.scenarioId) await selectBoard(TW.boardId);
      return;
    }
    from.pop();
    if (action.meetings && data.placements) globalThis.TimetableMeetingPolicy?.remapHistory([action, ...TW.undoStack, ...TW.redoStack], action.meetings, data.placements);
    to.push(action.type === 'section_create' ? action : { ...action, placement_id: data.placement?.id || action.placement_id });
    await selectBoard(TW.boardId);
  } finally {
    finishWorkspaceOperation(token);
  }
}
$('twUndo').addEventListener('click', () => runHistoryAction(true));
$('twRedo').addEventListener('click', () => runHistoryAction(false));

/* ── Change room (design record §7.3, 5c) ──
   One meeting's room, chosen in #twRoomDialog from the rooms the server
   lists for it at its own time. No time is ever sent, so the change is
   room-only and a time lock holds. A room that fits (type, gender, seats) can
   be chosen even while another section holds it at that time: that saves a
   room clash, which Issues then shows (owner ruling 2026-10-20); one outside
   the board's programme rooms saves a "Room unsuitable" warning, and the
   dialog says so before and after. A room that does not fit is listed with
   its reason and cannot be chosen. Which room is which, and its words, are
   TimetableRoomChoice's, shared with split view.
   Since 5c-audit the rooms are read with ?audit=1: an answer that carries
   the quality audit's answer for the meeting is judged by it (D20c as
   amended): the dialog then says, before a save, the rows Issues will list
   after it; a room short of the audit's seats is a choosable warning, and
   the wrong gender or type is refused (the owner's answer to Q1). Without
   it, or when the quality check cannot judge the scenario's rooms, the
   legacy rule holds exactly as before (the latter with a note). */
const isolatedText = text => `\u2066${text}\u2069`;
const ROOM_WORDS = IS_AR ? {
  change: 'تغيير…', choose: 'اختيار…', changeLabel: 'تغيير القاعة', chooseLabel: 'اختيار قاعة',
  title: 'تغيير القاعة',
  thisMeeting: 'هذا اللقاء فقط؛ تحتفظ لقاءات الشعبة الأخرى بقاعاتها.',
  locked: 'الوقت مقفل؛ القاعة قابلة للتغيير',
  current: 'القاعة الحالية:', notSet: 'لم تُحدَّد',
  timeClashes: n => `أيّاً كانت القاعة، يبقى في وقته ${pluralAr(n, { one: 'تعارض واحد', two: 'تعارضان', few: '{n} تعارضات', many: '{n} تعارضاً', other: '{n} تعارض' })}.`,
  filter: 'تصفية القاعات', filterHint: 'رمز القاعة أو المبنى', noMatch: 'لا توجد قاعة تطابق التصفية.',
  free: n => `متاحة ومناسبة (${n})`,
  flagged: n => `متاحة لكنها تُضعف الجودة (${n})`,
  outside: n => `متاحة لكنها خارج قاعات برامج هذه اللوحة (${n})`,
  inUse: n => `مناسبة لكنها مستخدمة في هذا الوقت (${n})`,
  misfits: n => `قاعات غير مناسبة (${n})`,
  flaggedHint: 'اختيار إحداها يحفظ التحذير المذكور في سطرها، ويظهر بعدها في «المشاكل».',
  outsideHint: 'اختيار إحداها يحفظ تحذير «القاعة غير مناسبة» الذي يظهر بعدها في «المشاكل».',
  inUseHint: 'اختيار إحداها يحفظ تعارض قاعة يظهر بعدها في «المشاكل».',
  unassessedNote: 'لا يستطيع فحص الجودة الحكم على قاعات هذا اللقاء؛ صُنّفت القاعات حسب النوع والجنس والمقاعد فقط.',
  usedBy: (who, board, when) => `تستخدمها ${who} (${board}) في ${when}`,
  more: n => pluralAr(n, { one: 'وشعبة أخرى', two: 'وشعبتان أخريان', few: 'و{n} شعب أخرى', many: 'و{n} شعبةً أخرى', other: 'و{n} شعبة أخرى' }),
  moreJoin: '؛ ', listJoin: '، ',
  loading: 'جارٍ البحث عن قاعات لهذا اللقاء…', failed: 'تعذّرت قراءة القاعات.',
  serverBusy: 'الخادم مشغول؛ أعد المحاولة بعد قليل.', retry: 'إعادة المحاولة',
  empty: 'لا توجد قاعات مسجّلة.', noneFit: 'لا توجد قاعة مناسبة لهذا اللقاء.',
  truncated: (shown, total) => `تُعرض أنسب ${shown} قاعة من أصل ${total}.`,
  stale: 'تغيّر هذا اللقاء منذ قراءة قاعاته.', checkAgain: 'إعادة التحقق',
  chooseNote: 'اختر قاعة.', currentNote: 'هذه هي القاعة الحالية.',
  progress: c => `جارٍ تعيين القاعة ${c}…`,
  assigned: c => `عُيّنت القاعة ${c}.`,
  assignedInUse: (c, who) => `عُيّنت القاعة ${c}، ويستخدمها في هذا الوقت أيضاً: ${who}.`,
  assignedOutside: c => `عُيّنت القاعة ${c}، وستعرضها «المشاكل» غير مناسبة لبرامج هذه اللوحة.`,
  outsideToo: 'وستعرضها «المشاكل» أيضاً غير مناسبة لبرامج هذه اللوحة.',
  assignedFlagged: (c, k) => `عُيّنت القاعة ${c}، وستعرض «المشاكل»: ${k}.`,
  flaggedToo: k => `وستعرض «المشاكل» أيضاً: ${k}.`,
  kindList: kinds => kinds.map(kind => `«${kind}»`).join('، '),
  alsoChanged: n => `وتغيّرت أيضاً في ${pluralAr(n, { one: 'لوحة أخرى', two: 'لوحتين أخريين', few: '{n} لوحات أخرى', many: '{n} لوحةً أخرى', other: '{n} لوحة أخرى' })}.`,
  gone: 'لم يعد هذا اللقاء في اللوحة.', notChanged: 'لم تتغيّر القاعة.',
} : {
  change: 'Change…', choose: 'Choose…', changeLabel: 'Change room', chooseLabel: 'Choose a room',
  title: 'Change room',
  thisMeeting: "this meeting only; the section's other meetings keep their rooms.",
  locked: 'Time locked; its room can change',
  current: 'Current room:', notSet: 'Not set',
  timeClashes: n => `Whatever the room, its time keeps ${n === 1 ? '1 clash' : `${n} clashes`}.`,
  filter: 'Filter rooms', filterHint: 'Room code or building', noMatch: 'No room matches the filter.',
  free: n => `Free and fits (${n})`,
  flagged: n => `Free, but lowers quality (${n})`,
  outside: n => `Free, but outside this board's programme rooms (${n})`,
  inUse: n => `Fits, but in use at this time (${n})`,
  misfits: n => `Rooms that do not fit (${n})`,
  flaggedHint: 'Choosing one saves the warning on its row, which Issues then shows.',
  outsideHint: 'Choosing one saves a “Room unsuitable” warning, which Issues then shows.',
  inUseHint: 'Choosing one saves a room clash, which Issues then shows.',
  unassessedNote: 'The quality check cannot judge rooms for this meeting; rooms are grouped by type, gender and seats only.',
  usedBy: (who, board, when) => `Used by ${who} (${board}) at ${when}`,
  more: n => (n === 1 ? 'and 1 more' : `and ${n} more`),
  moreJoin: '; ', listJoin: ', ',
  loading: 'Finding rooms for this meeting…', failed: 'The rooms could not be read.',
  serverBusy: 'The server is busy; try again in a moment.', retry: 'Try again',
  empty: 'No rooms are on record.', noneFit: 'No room fits this meeting.',
  truncated: (shown, total) => `Showing the best ${shown} of ${total} rooms.`,
  stale: 'This meeting changed since its rooms were read.', checkAgain: 'Check again',
  chooseNote: 'Choose a room.', currentNote: 'This is the current room.',
  progress: c => `Assigning room ${c}…`,
  assigned: c => `Room ${c} assigned.`,
  assignedInUse: (c, who, n) => `Room ${c} assigned; ${who} also ${n === 1 ? 'uses' : 'use'} it at this time.`,
  assignedOutside: c => `Room ${c} assigned; Issues will show it as unsuitable for this board's programmes.`,
  outsideToo: "Issues will also show it as unsuitable for this board's programmes.",
  assignedFlagged: (c, k) => `Room ${c} assigned; Issues will show ${k}.`,
  flaggedToo: k => `Issues will also show ${k}.`,
  kindList: kinds => kinds.map(kind => `“${kind}”`).join(', '),
  alsoChanged: n => `It also changed on ${n === 1 ? '1 other board' : `${n} other boards`}.`,
  gone: 'This meeting is no longer on the board.', notChanged: 'The room was not changed.',
};

/* The dialog's session: the meeting it is about, what it read, what is
   chosen. Each open starts a new session (`token`) and each read a new
   `read`, so a late answer or commit never lands in a later session.
   `version` is the data version the rooms were asked at (null before they
   are asked for, -1 once the meeting was off the board); `picked`, whether
   the user has chosen a row of them; `held`, the page's own hold a commit
   met, said in the alert line until it ends. `target` is what the rooms
   are judged against and `unassessed` whether to say the quality check
   cannot judge them (roomJudgement), both fixed when the answer arrives, so
   a quality check answering later never re-sorts a list a room was chosen
   from. */
const ROOM = {
  token: 0, read: 0, placementId: null, scenarioId: null, boardId: null, origin: '', snapshot: null,
  state: 'loading', answer: null, target: null, unassessed: false, failure: null, choice: '', picked: false, version: null, pending: false,
  blocked: '', held: '', filter: '', view: '', drawn: null,
};
const roomDialogOpen = () => Boolean(window.UXControls?.dialog?.isOpen($('twRoomDialog')));

/* The meeting as the open board has it now; null once it is not there (the
   scenario or the board changed, or it was removed). */
function roomPlacement() {
  if (TW.scenarioId !== ROOM.scenarioId || TW.boardId !== ROOM.boardId || TW.loadedBoardId !== ROOM.boardId) return null;
  return TW.placements.find(row => row.id === ROOM.placementId) || null;
}
const roomDataKey = () => [TW.scenarioId, TW.boardId, TW.loadedBoardId, TW.placements, TW.dataVersion];
/* The rooms asked for (or answered) before the data last moved: a write
   landed since (contract E's data version), or the meeting left the board
   (a scenario reload, which moves no version: `version` is then -1). */
const roomReadOutdated = () => ROOM.version != null && ROOM.version !== TW.dataVersion;

function announceRoom(text) {
  const status = $('twRoomStatus');
  if (!status) return;
  status.textContent = '';
  status.textContent = text;
}

/* Selection's Room button: none where the section needs no room; drawn off,
   with its reason in #twSelActionNote, while the gate holds it. Opening only
   reads, so a write in flight never holds it. */
function roomActionHtml(p) {
  const refusal = cardActionRefusal('room', p);
  if (refusal && NOT_APPLICABLE.has(refusal.code)) return '';
  const set = p.room_state ? p.room_state === 'assigned' : Boolean(String(p.room || '').trim()) && p.room !== 'UNASSIGNED';
  return ` <button type="button" class="btn btn-sm btn-neutral tw-sel-inline" data-tw-action="room" data-focus-key="room" data-placement-id="${p.id}"`
    + ` aria-haspopup="dialog" aria-label="${esc(set ? ROOM_WORDS.changeLabel : ROOM_WORDS.chooseLabel)}"`
    + `${refusal ? ' disabled data-tw-hold aria-describedby="twSelActionNote"' : ''}>${esc(set ? ROOM_WORDS.change : ROOM_WORDS.choose)}</button>`;
}

/* Selection's action buttons follow the gate in place (a job starting, a
   board loading), and the note says each reason holding them once. */
function syncSelectionActions() {
  const body = $('twRightBody');
  if (!body) return;
  const reasons = [];
  body.querySelectorAll('[data-tw-action]').forEach(button => {
    const p = TW.placements.find(row => String(row.id) === button.dataset.placementId);
    const refusal = p ? cardActionRefusal(button.dataset.twAction, p) : { code: 'loading', text: REFUSAL.loading };
    button.disabled = Boolean(refusal);
    button.toggleAttribute('data-tw-hold', Boolean(refusal));
    if (refusal) {
      button.setAttribute('aria-describedby', 'twSelActionNote');
      if (!reasons.includes(refusal.text)) reasons.push(refusal.text);
    } else {
      button.removeAttribute('aria-describedby');
    }
  });
  const note = $('twSelActionNote');
  if (!note) return;
  const text = reasons.join(' ');
  if (note.textContent !== text) note.textContent = text;
  note.hidden = !text;
}

// One listener for Selection's actions, however often the panel is drawn.
$('twRightBody').addEventListener('click', event => {
  const button = event.target.closest && event.target.closest('[data-tw-action]');
  if (!button || button.disabled) return;
  const kind = button.dataset.twAction;
  const p = TW.placements.find(row => String(row.id) === button.dataset.placementId);
  if (p && typeof TW.keyActions?.[kind] === 'function') TW.keyActions[kind](p, { origin: `sel:${kind}:${p.id}` });
});

/* Opened from a card (R) or from Selection; `origin` names where the
   keyboard goes back to at close. The gate first, then the open guard. */
function openRoomDialog(p, { origin = '' } = {}) {
  const refusal = cardActionRefusal('room', p);
  if (refusal) {
    notify.warning(refusal.text);
    return false;
  }
  const dialog = $('twRoomDialog');
  if (!dialog || !window.TimetableRoomChoice || !window.UXControls?.dialog || anyDialogOpen()) return false;
  Object.assign(ROOM, {
    token: ROOM.token + 1, placementId: p.id, scenarioId: TW.scenarioId, boardId: TW.boardId, origin, snapshot: p,
    state: 'loading', answer: null, target: null, unassessed: false, failure: null, choice: '', picked: false, version: null, pending: false,
    blocked: '', held: '', filter: '', drawn: null,
  });
  const alert = $('twRoomAlert');
  if (alert) { alert.textContent = ''; alert.hidden = true; }
  // What it is about, for the words a write of another meeting says in it.
  dialog.dataset.twAbout = `placement:${p.id}`;
  drawRoomDialog();
  if (!window.UXControls.dialog.open(dialog, { initialFocus: () => $('twRoomBody'), returnFocus: () => resolveOrigin(origin) })) return false;
  readRoomCandidates();
  return true;
}

/* How an answer's rooms are judged (5c-audit): by the quality audit when
   the answer carries its answer for the meeting (target.audit.assessed) and
   the quality check has not said it cannot judge the scenario's rooms
   (TW.quality.rooms.assessed false, a fault in another meeting the answer
   cannot see); else by the legacy rule, with a target that carries no
   audit. `unassessed`: the answer asked the audit and it does not apply,
   which the dialog says; an answer that never asked (a server before
   5c-audit) is drawn exactly as before. */
function roomJudgement(answer) {
  const target = answer?.target || {};
  const audit = target.audit;
  if (audit === undefined) return { target, unassessed: false };
  if (audit?.assessed === true && TW.quality?.rooms?.assessed !== false) return { target, unassessed: false };
  const legacy = { ...target };
  delete legacy.audit;
  return { target: legacy, unassessed: true };
}

/* The rooms at the meeting's own time (no slot is sent), with what the
   quality audit will say of each once saved (?audit=1), drawn if the answer
   is still this session's and its latest read. The keyboard then goes from
   the body to the chosen row, or the first one, unless the user has moved it
   (a row it waited on while the list loaded comes back to it). */
async function readRoomCandidates({ fresh = false } = {}) {
  const session = ROOM.token;
  const read = ++ROOM.read;
  Object.assign(ROOM, { state: 'loading', answer: null, target: null, unassessed: false, failure: null, choice: '', version: TW.dataVersion });
  announceRoom(ROOM_WORDS.loading);
  drawRoomDialog();
  const id = ROOM.placementId;
  const answer = await previewRead(`room|${id}`, `/ops/tw/placements/${id}/room-candidates/?audit=1`, { fresh });
  if (session !== ROOM.token || read !== ROOM.read || !roomDialogOpen()) return;
  if (answer.data) {
    Object.assign(ROOM, { state: 'ready', answer: answer.data, ...roomJudgement(answer.data) });
  } else {
    Object.assign(ROOM, { state: 'error', failure: answer });
    announceRoom(roomFailureWords(answer));
  }
  drawRoomDialog();
  const body = $('twRoomBody');
  if (document.activeElement === body) {
    const radios = [...body.querySelectorAll('input[name="twRoomChoice"]')];
    (radios.find(radio => radio.checked) || radios[0])?.focus({ preventScroll: true });
  }
}

function roomFailureWords(failure) {
  if (failure?.code === 'NOT_FOUND') return ROOM_WORDS.gone;
  return failure?.code === 'SERVER_BUSY' || failure?.status === 503 ? ROOM_WORDS.serverBusy : ROOM_WORDS.failed;
}

/* The rooms in their groups, in the server's order, judged against `target`
   (roomJudgement): free; free but lowering quality (judged by the audit:
   too small, unsuitable or leaving the class split, as Issues will list
   it); free but outside the board's programme rooms (the legacy rule's
   only such group); in use at this time; and those that do not fit. A code
   listed twice (an M and an F room) is one row, the first; the placer's
   UNASSIGNED marker is not a room. */
function roomGroups(answer, target, p) {
  const RC = window.TimetableRoomChoice;
  const groups = { free: [], flagged: [], outside: [], inUse: [], misfit: [] };
  const seen = new Set();
  (answer?.candidates || []).forEach(room => {
    const key = RC.codeKey(room?.room_code);
    if (!key || key === 'UNASSIGNED' || seen.has(key)) return;
    seen.add(key);
    const verdict = RC.verdict(room, p, target);
    groups[verdict.group].push({ room, verdict });
  });
  return groups;
}

/* The current room is chosen at first when it can be chosen (Assign holds
   on it until another is chosen); until the user chooses, the choice follows
   the current room as the board has it. */
function initialRoomChoice(answer, target) {
  const p = roomPlacement();
  if (!p) return '';
  const RC = window.TimetableRoomChoice;
  const current = RC.codeKey(p.room);
  const { free, flagged, outside, inUse } = roomGroups(answer, target, p);
  const row = [...free, ...flagged, ...outside, ...inUse].find(item => RC.codeKey(item.room.room_code) === current);
  return row ? String(row.room.room_code).trim() : '';
}

/* What the dialog shows: the read, unless the meeting is gone, the data
   moved after a room was chosen from it or the meeting moved since its rooms
   were read (stale), or it needs no room after all (stale flags). */
function roomDialogView() {
  const live = roomPlacement();
  const p = live || ROOM.snapshot;
  if (!live) return { state: 'gone', p };
  if (ROOM.state !== 'ready') return { state: ROOM.state, p };
  if (roomReadOutdated()) return { state: 'stale', p };
  const target = ROOM.answer?.target || {};
  if (target.is_online) return { state: 'online', p };
  if (target.no_room_required) return { state: 'noRoom', p };
  const same = String(target.day || '').toUpperCase() === String(p.day || '').toUpperCase()
    && String(target.start || '') === String(p.start_time || '') && String(target.end || '') === String(p.end_time || '');
  return { state: same ? 'ready' : 'stale', p };
}

/* Who holds a room at this time: the first class, then how many more (a
   class on several boards at once is one). */
function roomOccupantsHtml(entries) {
  const seen = new Set();
  const classes = (entries || []).filter(entry => {
    const key = String(entry?.term_section_id ?? entry?.section ?? entry?.placement_id);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const first = classes[0];
  if (!first) return '';
  const lead = ROOM_WORDS.usedBy(`<bdi>${esc(first.section || '')}</bdi>`, `<bdi>${esc(first.board_label || '')}</bdi>`,
    `<bdi dir="ltr">${esc(first.start || '')}–${esc(first.end || '')}</bdi>`);
  return classes.length > 1 ? `${lead}${ROOM_WORDS.moreJoin}${esc(ROOM_WORDS.more(classes.length - 1))}` : lead;
}

function roomSubHtml(p) {
  const day = (IS_AR ? DAY_LABELS : DAY_NAMES_EN)[DAYS.indexOf(p.day)] || p.day || '';
  const parts = [`${esc(day)} <bdi dir="ltr">${esc(p.start_time)}–${esc(p.end_time)}</bdi>`];
  // Only this meeting changes: the section's other meetings keep their rooms.
  if ((p.meetings || []).length > 1) parts.push(esc(ROOM_WORDS.thisMeeting));
  if (p.is_locked) parts.push(esc(ROOM_WORDS.locked));
  return parts.join(' · ');
}

/* The body, in order: the current room (judged by the audit, with what
   Issues say of it now); the time's own clashes, which stay whatever the
   room; the audit's notes about the meeting, or that the quality check
   cannot judge its rooms; the filter; the rooms that can be chosen (free,
   then free but lowering quality, or, by the legacy rule, free but outside
   the board's programme rooms, then in use), each group that saves an
   issue saying which; the rooms that do not fit, closed unless nothing
   fits; and how many the server left out. Every other state is one
   line (and its way on); the filter stays while the rooms load, so the
   keyboard in it stays there when they are read again. */
function roomDialogHtml(view) {
  const W = ROOM_WORDS;
  const p = view.p;
  const line = (text, extra = '') => `<p class="ux-choice-status"${extra}>${esc(text)}</p>`;
  const filter = `<div class="ux-field"><label class="ux-field-label" for="twRoomFilter">${esc(W.filter)}</label>`
    + `<input type="search" id="twRoomFilter" class="tw-search" dir="auto" autocomplete="off" aria-controls="twRoomChoices"`
    + ` placeholder="${esc(W.filterHint)}" value="${esc(ROOM.filter)}"></div>`;
  // The current room; judged by the audit, what Issues say of it now.
  const current = now => `<p class="tw-room-current">${esc(W.current)} <bdi>${esc(placementRoomText(p) || W.notSet)}</bdi>`
    + `${now ? ` <span class="tw-room-verdict" data-tone="${now.tone}">· ${esc(now.words)}</span>` : ''}</p>`;
  let html = current(null);
  if (view.state === 'loading') return `${html}${filter}${line(W.loading)}<div class="ux-choice-list" id="twRoomChoices" aria-busy="true"></div>`;
  if (view.state === 'gone') return html + line(W.gone);
  if (view.state === 'online' || view.state === 'noRoom') return html + line(REFUSAL[view.state]);
  if (view.state === 'stale') {
    return html + line(W.stale)
      + `<p class="tw-room-action"><button type="button" class="btn btn-sm btn-neutral" data-tw-room-recheck data-focus-key="room-recheck">${esc(W.checkAgain)}</button></p>`;
  }
  if (view.state === 'error') {
    return html + line(roomFailureWords(ROOM.failure)) + (ROOM.failure?.code === 'NOT_FOUND' ? ''
      : `<p class="tw-room-action"><button type="button" class="btn btn-sm btn-neutral" data-tw-room-retry data-focus-key="room-retry">${esc(W.retry)}</button></p>`);
  }
  const RC = window.TimetableRoomChoice;
  const answer = ROOM.answer || {};
  const target = ROOM.target || {};
  const groups = roomGroups(answer, target, p);
  const currentRow = (answer.candidates || []).find(room => RC.codeKey(room?.room_code) === RC.codeKey(p.room));
  html = current(RC.currentVerdict(target, p, currentRow));
  // Student and teacher clashes belong to the time: no room changes them.
  const clashes = Number(answer.candidates?.[0]?.validation?.critical_count) || 0;
  if (clashes > 0) html += `<p class="tw-room-note">${esc(W.timeClashes(clashes))}</p>`;
  const choosable = groups.free.length + groups.flagged.length + groups.outside.length + groups.inUse.length;
  if (!choosable && !groups.misfit.length) return html + line(W.empty);
  // What the audit says of the meeting (a copy staying in its room, a board
  // with no programme, seats assumed), or that the quality check cannot
  // judge its rooms.
  RC.auditNotes(target, IS_AR).forEach(note => { html += `<p class="tw-room-note" data-tone="${note.tone}">${esc(note.text)}</p>`; });
  if (ROOM.unassessed) html += `<p class="tw-room-note" data-tone="info">${esc(W.unassessedNote)}</p>`;
  const chosen = RC.codeKey(ROOM.choice);
  const filterText = room => [room.room_code, room.building, room.wing].map(value => String(value ?? '').trim()).join(' ').toUpperCase();
  let index = 0;
  const row = ({ room, verdict }) => {
    const id = `twRoomRow${index++}`;
    const code = String(room.room_code).trim();
    const use = verdict.group === 'inUse' ? roomOccupantsHtml(room.occupied_by) : '';
    return `<label class="ux-choice" data-tone="${verdict.tone}" data-filter="${esc(filterText(room))}">`
      + `<input type="radio" name="twRoomChoice" value="${esc(code)}" data-focus-key="room-choice:${esc(RC.codeKey(code))}"`
      + ` aria-labelledby="${id}-code" aria-describedby="${id}-verdict ${id}-facts${use ? ` ${id}-use` : ''}"${RC.codeKey(code) === chosen ? ' checked' : ''}>`
      + `<span class="ux-choice-title"><span class="ux-tone-icon" data-tone="${verdict.tone}" aria-hidden="true"></span>`
      + `<bdi id="${id}-code">${esc(code)}</bdi><span class="tw-room-verdict" id="${id}-verdict">${esc(verdict.words)}</span></span>`
      + `<span class="ux-choice-desc" id="${id}-facts">${RC.factsHtml(room, target, IS_AR)}</span>`
      + (use ? `<span class="ux-choice-status" id="${id}-use">${use}</span>` : '')
      + '</label>';
  };
  // A room that does not fit is a row with its reason, never a radio.
  const misfit = ({ room }) => {
    const reasons = RC.misfitReasons(room, target, IS_AR);
    return `<div class="ux-choice ux-choice-static" data-filter="${esc(filterText(room))}">`
      + `<span class="ux-choice-title"><bdi>${esc(String(room.room_code).trim())}</bdi></span>`
      + (reasons.length ? `<span class="ux-choice-desc">${esc(reasons.join(' · '))}</span>` : '')
      + `<span class="ux-choice-desc">${RC.factsHtml(room, target, IS_AR)}</span></div>`;
  };
  const group = (key, legend, hint, rows) => `<fieldset class="ux-choice-list tw-room-group" data-ux-submit data-group="${key}"`
    + `${hint ? ` aria-describedby="twRoomHint-${key}"` : ''}><legend class="ux-field-label">${esc(legend)}</legend>`
    + `${hint ? `<p class="ux-field-hint" id="twRoomHint-${key}">${esc(hint)}</p>` : ''}${rows.map(row).join('')}</fieldset>`;
  html += filter;
  html += '<div class="tw-room-choices" id="twRoomChoices">';
  if (!choosable) html += line(W.noneFit);
  if (groups.free.length) html += group('free', W.free(groups.free.length), '', groups.free);
  if (groups.flagged.length) html += group('flagged', W.flagged(groups.flagged.length), W.flaggedHint, groups.flagged);
  if (groups.outside.length) html += group('outside', W.outside(groups.outside.length), W.outsideHint, groups.outside);
  if (groups.inUse.length) html += group('inUse', W.inUse(groups.inUse.length), W.inUseHint, groups.inUse);
  html += `${line(W.noMatch, ' id="twRoomNoMatch" hidden')}</div>`;
  if (groups.misfit.length) {
    const open = !choosable || Boolean(window.UXControls?.disclosure?.isOpen('tw-room:misfits', false));
    html += `<div class="tw-room-misfits"><button type="button" class="ux-disclosure-toggle" data-ux-disclosure="tw-room:misfits" aria-expanded="${open}" aria-controls="twRoomMisfits">`
      + `<span class="ux-disclosure-label">${esc(W.misfits(groups.misfit.length))}</span><span class="ux-disclosure-chevron" aria-hidden="true"></span></button>`
      + `<div class="ux-choice-list" id="twRoomMisfits"${open ? '' : ' hidden'}>${groups.misfit.map(misfit).join('')}</div></div>`;
  }
  // The server lists the best rooms only (and the current room past them).
  const total = Number(answer.summary?.total) || 0;
  const listed = (answer.candidates || []).length;
  if (total > listed) html += line(W.truncated(listed, total));
  return html;
}

const setDialogHead = (id, html) => {
  const element = $(id);
  if (element && element._twHtml !== html) { element.innerHTML = html; element._twHtml = html; }
};
/* The whole dialog from ROOM and the board, through the global redraw: the
   same content leaves the nodes (and the keyboard) alone. */
function drawRoomDialog() {
  const body = $('twRoomBody');
  if (!body || ROOM.placementId == null) return;
  const view = roomDialogView();
  ROOM.view = view.state;
  if (!ROOM.picked) ROOM.choice = initialRoomChoice(ROOM.answer, ROOM.target);
  setDialogHead('twRoomTitle', `${esc(ROOM_WORDS.title)} · <bdi>${esc(view.p.course_code)}</bdi> <bdi>${esc(view.p.section)}</bdi>`);
  setDialogHead('twRoomSub', roomSubHtml(view.p));
  const html = roomDialogHtml(view);
  if (window.UXControls?.redraw) window.UXControls.redraw(body, html);
  else body.innerHTML = html;
  // The same markup leaves the rows alone, a row the filter hid unchecked:
  // the rows take the choice again (the filter then drops it if hidden).
  const RC = window.TimetableRoomChoice;
  const chosen = RC.codeKey(ROOM.choice);
  body.querySelectorAll('input[name="twRoomChoice"]').forEach(radio => { radio.checked = RC.codeKey(radio.value) === chosen; });
  // The keyboard on a row while the dialog (not the user) chooses goes with
  // its choice.
  if (!ROOM.picked && document.activeElement?.matches('#twRoomBody input[name="twRoomChoice"]')) {
    body.querySelector('input[name="twRoomChoice"]:checked')?.focus({ preventScroll: true });
  }
  ROOM.drawn = roomDataKey();
  applyRoomFilter();
  syncRoomDialog();
}

/* Assign and its note, in place: cheap, as it runs with every control sync.
   New board data draws the dialog first (the meeting may have moved or gone).
   Assign is held until a room other than the current one is chosen from a
   fresh list, and while a commit is pending or was refused for good; it is
   off while the page cannot write (busy comes back on by itself). */
function syncRoomDialog() {
  if (!roomDialogOpen()) return;
  // Shown as gone (a scenario reload takes every meeting off the board for a
  // moment): rooms read, or still to be read, before it left are out of date
  // if it comes back.
  if (ROOM.view === 'gone') ROOM.version = -1;
  // Rooms asked for before the data last moved are asked for again at once
  // while no row of them has been chosen (the list loads and comes back by
  // itself) and the meeting is on the board; a choice made from them waits
  // for Check again (stale). A commit always follows a choice.
  if (roomReadOutdated() && !ROOM.picked && roomPlacement()) {
    readRoomCandidates();
    return;
  }
  // The page's own hold a commit met is said in the alert line until it ends.
  if (ROOM.held && writeHold() !== ROOM.held) {
    ROOM.held = '';
    dialogSay($('twRoomDialog'), '');
  }
  if (!ROOM.drawn || roomDataKey().some((value, i) => value !== ROOM.drawn[i])) {
    drawRoomDialog();
    return;
  }
  const RC = window.TimetableRoomChoice;
  const p = roomPlacement();
  const chosen = RC.codeKey(ROOM.choice);
  const current = p ? RC.codeKey(p.room) : '';
  const held = ROOM.view !== 'ready' || !chosen || chosen === current || ROOM.pending || Boolean(ROOM.blocked);
  const assign = $('twRoomAssign');
  assign.toggleAttribute('data-tw-hold', held);
  assign.disabled = held || !workspaceCanEdit();
  const hold = writeHold();
  const note = ROOM.pending ? ROOM_WORDS.progress(isolatedText(ROOM.choice))
    : hold ? REFUSAL[hold]
      : ROOM.view !== 'ready' || ROOM.blocked ? ''
        : !chosen ? ROOM_WORDS.chooseNote
          : chosen === current ? ROOM_WORDS.currentNote : '';
  const line = $('twRoomNote');
  if (line.textContent !== note) line.textContent = note;
  // The lists are busy while the commit is out; the status lines are not.
  $('twRoomDialog').querySelectorAll('.ux-choice-list[data-ux-submit]').forEach(list => {
    if (ROOM.pending) list.setAttribute('aria-busy', 'true');
    else list.removeAttribute('aria-busy');
  });
}

/* The filter hides rows in place by code, building or wing (no redraw). A
   chosen row it hides is no longer chosen. */
function applyRoomFilter() {
  const body = $('twRoomBody');
  if (!body) return;
  const needle = ROOM.filter.trim().toUpperCase();
  let shown = 0;
  body.querySelectorAll('[data-filter]').forEach(row => {
    row.hidden = Boolean(needle) && !row.dataset.filter.includes(needle);
    if (!row.hidden) shown++;
  });
  body.querySelectorAll('.tw-room-group').forEach(group => { group.hidden = !group.querySelector('.ux-choice:not([hidden])'); });
  const none = $('twRoomNoMatch');
  if (none) none.hidden = !needle || shown > 0;
  const chosen = body.querySelector('input[name="twRoomChoice"]:checked');
  if (chosen && chosen.closest('[hidden]')) {
    chosen.checked = false;
    // Nothing is chosen now, by the user or the dialog: rooms read before
    // the data moved are read again by themselves, as before any choice.
    Object.assign(ROOM, { choice: '', picked: false });
    syncRoomDialog();
  }
}

/* The meeting changed since its rooms were read: read the board, then its
   rooms (fresh), the choice made from the old ones dropped. A meeting gone
   from the board by then is read once it is back. */
async function checkRoomAgain() {
  const session = ROOM.token;
  Object.assign(ROOM, { state: 'loading', version: null, picked: false });
  drawRoomDialog();
  if (TW.boardId) await selectBoard(TW.boardId);
  if (session !== ROOM.token || !roomDialogOpen()) return;
  if (!roomPlacement()) { drawRoomDialog(); return; }
  readRoomCandidates({ fresh: true });
}

/* What a saved change came to, read after the board: the room, who else uses
   it at this time (a clash is saved, not refused), the rows Issues will list
   for it (judged by the audit: "Room too small", "Room unsuitable", by
   their names; by the legacy rule: unsuitable, outside the board's
   programme rooms), and the other boards where the same class changed with
   it. Any of them makes it a warning, as the row's verdict said. `target`
   is what the row was judged against. */
function roomAssignedWords(p, choice, chosenRow, data, target) {
  const changed = (data?.placements || []).map(row => String(row.id));
  const others = changed.filter(id => id !== String(p.id)).length;
  const users = [...new Set((chosenRow?.occupied_by || [])
    .filter(entry => !changed.includes(String(entry.placement_id)))
    .map(entry => String(entry.section || '').trim()).filter(Boolean))];
  const RC = window.TimetableRoomChoice;
  const room = isolatedText(choice);
  const inUse = users.length ? ROOM_WORDS.assignedInUse(room, users.map(isolatedText).join(ROOM_WORDS.listJoin), users.length) : '';
  let text;
  let flagged;
  if (RC.auditOf(chosenRow, target)) {
    const kinds = RC.issueKinds(chosenRow, target).map(issueKindLabel);
    const list = ROOM_WORDS.kindList(kinds);
    flagged = kinds.length > 0;
    text = inUse || (flagged ? ROOM_WORDS.assignedFlagged(room, list) : ROOM_WORDS.assigned(room));
    if (inUse && flagged) text += ` ${ROOM_WORDS.flaggedToo(list)}`;
  } else {
    flagged = RC.outsidePool(chosenRow);
    text = inUse || (flagged ? ROOM_WORDS.assignedOutside(room) : ROOM_WORDS.assigned(room));
    if (inUse && flagged) text += ` ${ROOM_WORDS.outsideToo}`;
  }
  if (others) text += ` ${ROOM_WORDS.alsoChanged(others)}`;
  return { text, tone: users.length || flagged ? 'warning' : 'success' };
}

/* Assign: the room alone is sent (the lock holds). A refusal stays in the
   dialog; one that asking again cannot change (another scenario's section,
   published, gone) holds Assign. On success the dialog closes, the board's
   tab stop moves to the card, the board is read again, and then the toast.
   Escape while the write is out closes the dialog; the write still ends,
   and says how: as a toast, or in the dialog open by then (R again: a toast
   would sit behind it), where a refusal of the same meeting holds Assign,
   and words about this meeting in a dialog about another one name it. */
const ROOM_FINAL_REFUSALS = ['PLACEMENT_EXTERNALLY_OWNED', 'SCENARIO_PUBLISHED', 'NOT_FOUND'];
async function commitRoom() {
  const dialog = $('twRoomDialog');
  const p = roomPlacement();
  if (!roomDialogOpen() || !p || ROOM.pending || ROOM.view !== 'ready' || ROOM.blocked) return;
  const RC = window.TimetableRoomChoice;
  const choice = ROOM.choice;
  if (!RC.codeKey(choice) || RC.codeKey(choice) === RC.codeKey(p.room)) return;
  const chosenRow = (ROOM.answer?.candidates || []).find(room => RC.codeKey(room.room_code) === RC.codeKey(choice));
  const judgedBy = ROOM.target;
  const session = ROOM.token;
  const scenarioAtStart = TW.scenarioId;
  const oldRoom = p.room ?? '';
  const about = { key: `placement:${p.id}`, name: `${isolatedText(p.course_code)} ${isolatedText(p.section)}` };
  ROOM.pending = true;
  dialogSay(dialog, '');
  announceRoom(ROOM_WORDS.progress(isolatedText(choice)));
  syncRoomDialog();
  const result = await dialogWrite(dialog, `/ops/tw/placements/${p.id}/move/`, { room: choice });
  const here = session === ROOM.token;
  if (here) ROOM.pending = false;
  if (result.code) {
    const words = {
      PLACEMENT_EXTERNALLY_OWNED: REFUSAL.external, SCENARIO_PUBLISHED: REFUSAL.published, NOT_FOUND: ROOM_WORDS.gone,
      SCENARIO_BUSY: REFUSAL.busy, published: REFUSAL.published, job: REFUSAL.job, busy: REFUSAL.busy,
    }[result.code] || ROOM_WORDS.notChanged;
    // The meeting this session is about (a later one too, opened while the
    // write was out): a refusal asking again cannot change holds Assign.
    if (ROOM.placementId === p.id && ROOM_FINAL_REFUSALS.includes(result.code)) ROOM.blocked = result.code;
    // The page's own hold (dialogWrite's check, answered before any await):
    // it ends by itself, and its words go with it.
    if (['published', 'job', 'busy'].includes(result.code)) ROOM.held = result.code;
    dialogSay(dialog, words, 'warning', about);
    syncRoomDialog();
    return;
  }
  TW.undoStack.push({ type: 'room', placement_id: p.id, old_room: oldRoom, new_room: choice });
  TW.redoStack = [];
  if (here) window.UXControls.dialog.close(dialog, 'close');
  boardNav?.active(`placement:${p.id}`);
  if (TW.scenarioId === scenarioAtStart) await selectBoard(TW.boardId);
  updateUndoRedoButtons();
  const said = roomAssignedWords(p, choice, chosenRow, result.data, judgedBy);
  dialogSay(dialog, said.text, said.tone, about);
}

(function wireRoomDialog() {
  const dialog = $('twRoomDialog');
  if (!dialog) return;
  // A <form method="dialog"> closes itself on submit: the page commits, and
  // closes it once the room is saved.
  $('twRoomForm').addEventListener('submit', event => {
    event.preventDefault();
    commitRoom();
  });
  dialog.addEventListener('change', event => {
    if (event.target.name !== 'twRoomChoice') return;
    Object.assign(ROOM, { choice: event.target.value, picked: true });
    syncRoomDialog();
  });
  dialog.addEventListener('input', event => {
    if (event.target.id !== 'twRoomFilter') return;
    ROOM.filter = event.target.value;
    applyRoomFilter();
  });
  // In the filter, Enter takes the one row left (never a submit), and Escape
  // with text clears it, the dialog staying open.
  dialog.addEventListener('keydown', event => {
    const field = event.target;
    if (field.id !== 'twRoomFilter' || event.isComposing) return;
    if (event.key === 'Enter') {
      event.preventDefault();
      const shown = [...dialog.querySelectorAll('input[name="twRoomChoice"]')].filter(radio => !radio.closest('[hidden]'));
      if (shown.length !== 1) return;
      if (!shown[0].checked) {
        shown[0].checked = true;
        shown[0].dispatchEvent(new Event('change', { bubbles: true }));
      }
      shown[0].focus();
    } else if (event.key === 'Escape' && field.value) {
      event.preventDefault();
      event.stopPropagation();
      field.value = '';
      ROOM.filter = '';
      applyRoomFilter();
    }
  });
  dialog.addEventListener('click', event => {
    if (!event.target.closest) return;
    if (event.target.closest('[data-tw-room-retry]')) readRoomCandidates({ fresh: true });
    else if (event.target.closest('[data-tw-room-recheck]')) checkRoomAgain();
  });
})();

/* ── Move (design record §7.3, 5e) ──
   One meeting to another time on the grid, in #twMoveDialog, from M on a
   card or Selection's Move…: the times the server offers a manual move
   (/slot-candidates/?verdict=1&offer=manual), in the order of its verdict —
   what the publish check will say once the move is saved — with the
   meeting's own time as the reference ("Now"). The meeting keeps its room:
   the body is exactly {day, start_time, end_time}, as a drop sends
   (commitPlacementMove), so a room clash at a time is part of that time's
   verdict, and the report then points at R. A time lock or another
   scenario's section refuses before anything is read; an English section
   goes to its pattern chooser (its move preview is a place verdict, blind to
   what leaving the old pattern does). */
const MOVE_WORDS = IS_AR ? {
  action: 'نقل…', title: 'نقل',
  onlyThis: 'هذا اللقاء فقط:', othersStay: 'تبقى لقاءات الشعبة الأخرى في أماكنها.',
  roomStays: c => `وتبقى قاعته ${c}.`, online: 'وهو عن بُعد ولا يحتاج إلى قاعة.', noRoomYet: 'وليست له قاعة بعد.',
  now: 'الآن', legend: 'أوقات أخرى في الجدول، الأفضل أولاً',
  loading: 'جارٍ البحث عن الأوقات…', empty: 'لا يوجد وقت آخر من هذا النوع في الجدول.', failed: 'تعذّر تحميل الأوقات.',
  stale: 'تغيّر الجدول منذ قراءة هذه الأوقات.',
  noClash: 'لا تعارض', ownSection: 'لقاء آخر لهذه الشعبة في هذا الوقت.',
  blockedNoPair: n => pluralAr(n, { one: 'لا يجد طالب واحد شعبتين بلا تعارض', two: 'لا يجد طالبان شعبتين بلا تعارض', few: 'لا يجد {n} طلاب شعبتين بلا تعارض', many: 'لا يجد {n} طالباً شعبتين بلا تعارض', other: 'لا يجد {n} طالب شعبتين بلا تعارض' }),
  more: n => pluralAr(n, { one: 'وبند آخر', two: 'وبندان آخران', few: 'و{n} بنود أخرى', many: 'و{n} بنداً آخر', other: 'و{n} بند آخر' }),
  chosen: text => `المختار: ${text}`,
  progress: 'جارٍ النقل…',
  changed: 'تغيّرت هذه الشعبة أثناء فتح القائمة. لم يُنقل شيء.', notSaved: 'لم يُحفظ النقل.',
  roomHint: 'اضغط R على البطاقة لتغيير قاعتها.', findingPatterns: 'جارٍ البحث عن الأنماط…',
} : {
  action: 'Move…', title: 'Move',
  onlyThis: 'This meeting only:', othersStay: "The section's other meetings stay where they are.",
  roomStays: c => `Its room stays ${c}.`, online: 'It is online and needs no room.', noRoomYet: 'It has no room yet.',
  now: 'Now', legend: 'Other times on the grid, best first',
  loading: 'Finding times…', empty: 'No other time of this kind is on the grid.', failed: 'The times could not be loaded.',
  stale: 'The timetable changed since these times were read.',
  noClash: 'No clash found', ownSection: 'Another meeting of this section is at this time.',
  blockedNoPair: n => `leaves ${n === 1 ? '1 student' : `${n} students`} with no clash-free pair of sections`,
  more: n => (n === 1 ? 'and 1 more' : `and ${n} more`),
  chosen: text => `Chosen: ${text}`,
  progress: 'Moving…',
  changed: 'This section changed while the list was open. Nothing was moved.', notSaved: 'The move was not saved.',
  roomHint: 'Press R on the card to change its room.', findingPatterns: 'Finding patterns…',
};
// The same words as the room dialog's where the two say the same thing.
Object.assign(MOVE_WORDS, { serverBusy: ROOM_WORDS.serverBusy, retry: ROOM_WORDS.retry, gone: ROOM_WORDS.gone, checkAgain: ROOM_WORDS.checkAgain });

/* The dialog's session, as the room dialog's: the meeting, what it read and
   what is chosen. `snapshot` is what its times were read for (null until
   they arrive); `seen`, the meeting as it was last on the board (its title
   once it is gone); `words`, what the status line says about the list. */
const MOVE = {
  token: 0, read: 0, placementId: null, scenarioId: null, boardId: null, origin: '', snapshot: null, seen: null,
  state: 'loading', answer: null, failure: null, choice: '', picked: false, version: null, pending: false,
  blocked: '', held: '', view: '', words: '', drawn: null,
};
function moveDialogOpen() {
  return Boolean(window.UXControls?.dialog?.isOpen($('twMoveDialog')));
}
function movePlacement() {
  if (TW.scenarioId !== MOVE.scenarioId || TW.boardId !== MOVE.boardId || TW.loadedBoardId !== MOVE.boardId) return null;
  return TW.placements.find(row => row.id === MOVE.placementId) || null;
}
const moveDataKey = () => [TW.scenarioId, TW.boardId, TW.loadedBoardId, TW.placements, TW.dataVersion];
const moveReadOutdated = () => MOVE.version != null && MOVE.version !== TW.dataVersion;
/* What the times were read for, taken as they arrive: the meeting's day,
   start and end as the server had them (the answer's own placement: the
   board may not have caught up with a write yet), with the scenario, the
   board and the meeting's lock and claim as the page has them. */
function moveSnapshotOf(p, read = {}) {
  return { scenarioId: TW.scenarioId, boardId: TW.boardId, id: p.id, day: String(read.day || p.day || '').toUpperCase(),
    start: String(read.start || p.start_time || ''), end: String(read.end || p.end_time || ''),
    locked: Boolean(p.is_locked), shared: Boolean(p.shared_with_other_scenario) };
}
/* Whether the meeting is not, now, what its times were read for: another
   scenario or board, the meeting gone, another day, start or end, or a lock
   or another scenario's claim since. A commit stops on any of them. */
function moveSnapshotChanged() {
  const s = MOVE.snapshot;
  if (!s || TW.scenarioId !== s.scenarioId || TW.boardId !== s.boardId || TW.loadedBoardId !== s.boardId) return true;
  const p = TW.placements.find(row => row.id === s.id);
  if (!p) return true;
  return String(p.day || '').toUpperCase() !== s.day || p.start_time !== s.start || p.end_time !== s.end
    || Boolean(p.is_locked) !== s.locked || Boolean(p.shared_with_other_scenario) !== s.shared;
}
/* The gate's refusals that belong to the meeting itself (a lock, another
   scenario's section), as they stand now. */
function moveRefusal(p) {
  const refusal = p ? cardActionRefusal('move', p) : '';
  return refusal && ['external', 'locked'].includes(refusal.code) ? refusal : null;
}
const moveDayWord = day => (IS_AR ? DAY_LABELS : DAY_NAMES_EN)[DAYS.indexOf(String(day || '').toUpperCase())] || day || '';
const moveKey = candidate => `${candidate.day}|${candidate.start}`;
const moveChoosable = candidate => !candidate.verdict?.own_section_meeting;
/* The candidates in the verdict's order (verdict_rank), not the legacy one. */
function rankedMoves(answer) {
  const rank = candidate => (Number.isFinite(Number(candidate.verdict_rank)) && candidate.verdict_rank !== null ? Number(candidate.verdict_rank) : Infinity);
  return (answer?.candidates || []).map((candidate, index) => ({ candidate, index }))
    .sort((a, b) => (rank(a.candidate) - rank(b.candidate)) || (a.index - b.index))
    .map(entry => entry.candidate);
}
function chosenMove() {
  if (!MOVE.choice) return null;
  return rankedMoves(MOVE.answer).find(candidate => moveChoosable(candidate) && moveKey(candidate) === MOVE.choice) || null;
}

/* A verdict in words: its lead and the kinds it names (context kinds only
   when it holds nothing worse), or "No clash found". */
function moveVerdictWords(verdict) {
  if (!verdict) return '';
  if (verdict.severity === 'ok') return MOVE_WORDS.noClash;
  const items = (verdict.items || []).filter(item => ISSUE_KINDS[item.issue_kind]);
  const weighed = items.filter(item => item.severity !== 'info');
  const kinds = [...new Set((weighed.length ? weighed : items).map(item => item.issue_kind))];
  const lead = TONE_LEAD[verdict.severity] || '';
  return kinds.length ? `${lead}: ${kinds.map(issueKindLabel).join(', ')}` : lead;
}
/* One item of a verdict as a line (HTML): what it is, who or what it meets,
   where (a board other than `boardId` named), when, and how many students
   it costs. Move's lines and Place's (5f) alike. */
function moveDetailHtml(item, boardId = MOVE.boardId) {
  const label = esc(issueKindLabel(item.issue_kind));
  if (item.issue_kind === 'blocked') return label;
  const other = item.other || {};
  const course = `<bdi>${esc([other.course_code, other.section].filter(Boolean).join(' '))}</bdi>`;
  const board = other.board_label ? ` (<bdi>${esc(other.board_label)}</bdi>)` : '';
  const elsewhere = other.board_id != null && String(other.board_id) !== String(boardId) ? board : '';
  const when = other.day ? `${esc(dayName(other.day))} <bdi dir="ltr">${esc(other.start || '')}–${esc(other.end || '')}</bdi>` : '';
  const count = n => (n === null || n === undefined || !Number.isFinite(Number(n)) ? '' : Number(n));
  const parts = [];
  if (item.issue_kind === 'instructor' || item.issue_kind === 'instructorElsewhere') {
    parts.push(`${item.teacher ? `<bdi>${esc(item.teacher)}</bdi> — ` : ''}${course}${elsewhere}`, when);
  } else if (item.issue_kind === 'room' || item.issue_kind === 'roomElsewhere') {
    parts.push(`${item.room ? `<bdi>${esc(item.room)}</bdi> — ` : ''}${course}${elsewhere}`, when);
  } else if (item.issue_kind === 'crossBoard' || item.issue_kind === 'crossBoardMinor') {
    const students = count(item.students);
    parts.push(`${course}${board}`, when, students === '' ? '' : esc(studentsNeedBoth(students)));
  } else {
    // time and softOverlap; an H15 effect counts the students it leaves
    // with no clash-free pair.
    const effect = item.source === 'h15_effect';
    const students = count(effect ? item.blocked_students : item.students);
    parts.push(`${course}${elsewhere}`, when, students === '' ? '' : esc(effect ? MOVE_WORDS.blockedNoPair(students) : studentsNeedBoth(students)));
  }
  return `${label}: ${parts.filter(Boolean).join(' · ')}`;
}
/* At most three lines, then how many more the verdict holds. `lead(item)`
   opens a line (Place's: the meeting it belongs to). */
function moveLines(verdict, id, { boardId = MOVE.boardId, lead = () => '' } = {}) {
  const items = (verdict?.items || []).filter(item => ISSUE_KINDS[item.issue_kind]);
  const shown = items.slice(0, 3);
  const lines = shown.map((item, index) => ({ id: `${id}-d${index}`, html: `${lead(item)}${moveDetailHtml(item, boardId)}` }));
  const rest = items.length - shown.length + (Number(verdict?.more) || 0);
  if (rest > 0) lines.push({ id: `${id}-more`, html: esc(MOVE_WORDS.more(rest)) });
  return lines;
}
const moveLinesHtml = lines => lines.map(line => `<span class="ux-choice-desc" id="${line.id}">${line.html}</span>`).join('');
const moveToneIcon = tone => (tone ? `<span class="ux-tone-icon" data-tone="${esc(tone)}" aria-hidden="true"></span>` : '');

function moveSubHtml(p) {
  const W = MOVE_WORDS;
  const when = `${esc(moveDayWord(p.day))} <bdi dir="ltr">${esc(p.start_time)}–${esc(p.end_time)}</bdi>`;
  // "This meeting only" where the section has other meetings to stay put.
  const parts = [(p.meetings || []).length > 1 ? `${esc(W.onlyThis)} ${when}. ${esc(W.othersStay)}` : `${when}.`];
  const noRoom = p.room_state === 'unassigned' || p.room_state === 'unset' || p.room === 'UNASSIGNED' || !String(p.room || '').trim();
  if (p.is_online) parts.push(esc(W.online));
  else if (p.needs_room !== false) parts.push(noRoom ? esc(W.noRoomYet) : W.roomStays(`<bdi>${esc(p.room)}</bdi>`));
  return parts.join(' ');
}

/* What the dialog shows: the meeting gone, refused now (a lock or another
   scenario's claim since it opened), the read's state, or its times — stale
   once the data moved (a write since they were asked for) or the meeting is
   not what they were read for. */
function moveDialogView() {
  const live = movePlacement();
  if (live) MOVE.seen = live;
  const p = live || MOVE.seen;
  if (!live) return { state: 'gone', p };
  const refusal = moveRefusal(live);
  if (refusal) return { state: 'refused', p, refusal };
  if (MOVE.state !== 'ready') return { state: MOVE.state, p };
  if (moveReadOutdated() || moveSnapshotChanged()) return { state: 'stale', p };
  return { state: (MOVE.answer?.candidates || []).length ? 'ready' : 'empty', p };
}
const moveGoneFailure = failure => failure?.code === 'NOT_FOUND' || failure?.status === 404;
function moveFailureWords(failure) {
  if (moveGoneFailure(failure)) return MOVE_WORDS.gone;
  return failure?.code === 'SERVER_BUSY' || failure?.status === 503 ? MOVE_WORDS.serverBusy : MOVE_WORDS.failed;
}
function moveViewWords(view) {
  return {
    loading: MOVE_WORDS.loading, error: moveFailureWords(MOVE.failure), gone: MOVE_WORDS.gone, refused: view.refusal?.text || '',
    stale: MOVE_WORDS.stale, empty: MOVE_WORDS.empty,
  }[view.state] || '';
}

/* The body: "Now" (the meeting's own time and its verdict), then the other
   times, best first; a time where the section already meets is a row with
   its reason, never a radio. Every other state is its words in the status
   line (above the body), with its way on here. */
function moveDialogHtml(view) {
  const W = MOVE_WORDS;
  if (view.state === 'loading') return '<div class="ux-choice-list" id="twMoveChoices" aria-busy="true"></div>';
  if (view.state === 'stale') {
    return `<p class="tw-move-action"><button type="button" class="btn btn-sm btn-neutral" data-tw-move-recheck data-focus-key="move-recheck">${esc(W.checkAgain)}</button></p>`;
  }
  if (view.state === 'error') {
    return moveGoneFailure(MOVE.failure) ? ''
      : `<p class="tw-move-action"><button type="button" class="btn btn-sm btn-neutral" data-tw-move-retry data-focus-key="move-retry">${esc(W.retry)}</button></p>`;
  }
  if (view.state !== 'ready' && view.state !== 'empty') return '';
  const p = view.p;
  const now = MOVE.answer?.current_verdict || null;
  const nowLines = moveLines(now, 'twMoveNow');
  let html = `<div class="ux-choice ux-choice-static tw-move-now"${now ? ` data-tone="${esc(now.severity)}"` : ''}>`
    + `<span class="ux-choice-title">${moveToneIcon(now?.severity)}<span>${esc(W.now)}</span>`
    + `<span>${esc(moveDayWord(p.day))} <bdi dir="ltr">${esc(p.start_time)}–${esc(p.end_time)}</bdi></span>`
    + `<span class="tw-move-verdict">${esc(moveVerdictWords(now))}</span></span>${moveLinesHtml(nowLines)}</div>`;
  if (view.state === 'empty') return html;
  const rows = rankedMoves(MOVE.answer).map((candidate, index) => {
    const id = `twMoveRow${index}`;
    const key = moveKey(candidate);
    const time = `${esc(moveDayWord(candidate.day))} <bdi dir="ltr">${esc(candidate.start)}–${esc(candidate.end)}</bdi>`;
    if (!moveChoosable(candidate)) {
      return `<div class="ux-choice ux-choice-static"><span class="ux-choice-title"><span>${time}</span></span>`
        + `<span class="ux-choice-desc">${esc(W.ownSection)}</span></div>`;
    }
    const verdict = candidate.verdict || null;
    const lines = moveLines(verdict, id);
    return `<label class="ux-choice"${verdict ? ` data-tone="${esc(verdict.severity)}"` : ''}>`
      + `<input type="radio" name="twMoveChoice" value="${esc(key)}" data-focus-key="move-choice:${esc(key)}" aria-labelledby="${id}-time"`
      + ` aria-describedby="${[`${id}-verdict`, ...lines.map(line => line.id)].join(' ')}"${key === MOVE.choice ? ' checked' : ''}>`
      + `<span class="ux-choice-title">${moveToneIcon(verdict?.severity)}<span id="${id}-time">${time}</span>`
      + `<span class="tw-move-verdict" id="${id}-verdict">${esc(moveVerdictWords(verdict))}</span></span>${moveLinesHtml(lines)}</label>`;
  }).join('');
  return `${html}<fieldset class="ux-field"><legend class="ux-field-label">${esc(W.legend)}</legend>`
    + `<div class="ux-choice-list" id="twMoveChoices" data-ux-submit>${rows}</div></fieldset>`;
}

/* The whole dialog from MOVE and the board, through the global redraw (the
   same content leaves the nodes, and the keyboard, alone). Until the user
   chooses, the choice is the best time that can be chosen. */
function drawMoveDialog() {
  const body = $('twMoveBody');
  if (!body || MOVE.placementId == null) return;
  const view = moveDialogView();
  MOVE.view = view.state;
  MOVE.words = moveViewWords(view);
  if (!MOVE.picked) MOVE.choice = view.state === 'ready' ? (rankedMoves(MOVE.answer).filter(moveChoosable).map(moveKey)[0] || '') : '';
  setDialogHead('twMoveTitle', `${esc(MOVE_WORDS.title)} <bdi>${esc(view.p.course_code)}</bdi> <bdi>${esc(view.p.section)}</bdi>`);
  setDialogHead('twMoveSub', moveSubHtml(view.p));
  const html = moveDialogHtml(view);
  if (window.UXControls?.redraw) window.UXControls.redraw(body, html);
  else body.innerHTML = html;
  body.querySelectorAll('input[name="twMoveChoice"]').forEach(radio => { radio.checked = radio.value === MOVE.choice; });
  // The keyboard on a row the dialog (not the user) chose goes with its choice.
  if (!MOVE.picked && document.activeElement?.matches('#twMoveBody input[name="twMoveChoice"]')) {
    body.querySelector('input[name="twMoveChoice"]:checked')?.focus({ preventScroll: true });
  }
  MOVE.drawn = moveDataKey();
  syncMoveDialog();
}

/* Commit, its note and the status line, in place (cheap: it runs with every
   control sync). Times asked for before the data moved are asked for again
   at once while the user has chosen none of them and nothing is being
   moved; a choice made from them waits for Check again. Commit is held
   until a time can be moved to; it is off while the page cannot write, and
   the note says why, in the order the page's holds come (published, a
   planner job, busy), else what the chosen time will bring. */
function syncMoveDialog() {
  if (!moveDialogOpen()) return;
  // Shown as gone (a scenario reload takes every meeting off the board for a
  // moment): times read before it left are out of date if it comes back.
  if (MOVE.view === 'gone') MOVE.version = -1;
  const live = movePlacement();
  if (!MOVE.pending && moveReadOutdated() && !MOVE.picked && live && !moveRefusal(live)) {
    readMoveCandidates();
    return;
  }
  if (MOVE.held && writeHold() !== MOVE.held) {
    MOVE.held = '';
    dialogSay($('twMoveDialog'), '');
  }
  if (!MOVE.drawn || moveDataKey().some((value, i) => value !== MOVE.drawn[i])) {
    drawMoveDialog();
    return;
  }
  const chosen = chosenMove();
  const held = MOVE.view !== 'ready' || !chosen || MOVE.pending || Boolean(MOVE.blocked);
  const commit = $('twMoveCommit');
  commit.toggleAttribute('data-tw-hold', held);
  commit.disabled = held || !workspaceCanEdit();
  const hold = writeHold();
  const note = MOVE.pending ? '' : hold ? REFUSAL[hold] : held || !chosen.verdict ? '' : MOVE_WORDS.chosen(moveVerdictWords(chosen.verdict));
  const line = $('twMoveNote');
  if (line.textContent !== note) line.textContent = note;
  const words = MOVE.pending ? MOVE_WORDS.progress : MOVE.words;
  const status = $('twMoveStatus');
  if (status.textContent !== words) status.textContent = words;
  // The list is busy while the move is out; the status lines are not.
  $('twMoveDialog').querySelectorAll('.ux-choice-list[data-ux-submit]').forEach(list => {
    if (MOVE.pending) list.setAttribute('aria-busy', 'true');
    else list.removeAttribute('aria-busy');
  });
}

/* Opened from a card (M) or from Selection; `origin` names where the
   keyboard goes back to at close. Any open dialog first, then the gate,
   then an English section's own route (its chooser, after "Finding
   patterns…"), then the dialog and its read. */
function openMoveDialog(p, { origin = '' } = {}) {
  if (anyDialogOpen()) return false;
  const refusal = cardActionRefusal('move', p);
  if (refusal) {
    notify.warning(refusal.text);
    return false;
  }
  if (globalThis.TimetableMeetingPolicy?.isEnglish(p)) {
    // The chooser saves as soon as a pattern is chosen: it waits for the page.
    if (!workspaceCanEdit()) {
      notify.warning(REFUSAL.busy);
      return false;
    }
    announceBoard(MOVE_WORDS.findingPatterns);
    chooseEnglishWorkspacePattern({ type: 'move', placement_id: p.id });
    return true;
  }
  const dialog = $('twMoveDialog');
  if (!dialog || !window.UXControls?.dialog) return false;
  Object.assign(MOVE, {
    token: MOVE.token + 1, placementId: p.id, scenarioId: TW.scenarioId, boardId: TW.boardId, origin, snapshot: null, seen: p,
    state: 'loading', answer: null, failure: null, choice: '', picked: false, version: null, pending: false,
    blocked: '', held: '', view: '', words: '', drawn: null,
  });
  const alert = $('twMoveAlert');
  if (alert) { alert.textContent = ''; alert.hidden = true; }
  // What it is about, for the words a write of another meeting says in it.
  dialog.dataset.twAbout = `placement:${p.id}`;
  drawMoveDialog();
  if (!window.UXControls.dialog.open(dialog, { initialFocus: () => $('twMoveBody'), returnFocus: () => resolveOrigin(origin) })) return false;
  readMoveCandidates();
  return true;
}

/* The times, drawn if the answer is still this session's latest read, and
   what they were read for (the snapshot a commit checks), taken as they
   arrive. A new list is a new choice. The keyboard then goes from the body
   to the chosen row, unless the user has moved it. */
async function readMoveCandidates({ fresh = false } = {}) {
  const session = MOVE.token;
  const read = ++MOVE.read;
  Object.assign(MOVE, { state: 'loading', answer: null, failure: null, choice: '', picked: false, version: TW.dataVersion, snapshot: null });
  drawMoveDialog();
  const id = MOVE.placementId;
  const answer = await previewRead(`slot|${id}`, `/ops/tw/placements/${id}/slot-candidates/?verdict=1&offer=manual`, { fresh });
  if (session !== MOVE.token || read !== MOVE.read || !moveDialogOpen()) return;
  const live = movePlacement();
  if (answer.data) Object.assign(MOVE, { state: 'ready', answer: answer.data, snapshot: live ? moveSnapshotOf(live, answer.data.placement) : null });
  else Object.assign(MOVE, { state: 'error', failure: answer });
  drawMoveDialog();
  const body = $('twMoveBody');
  if (document.activeElement === body) body.querySelector('input[name="twMoveChoice"]:checked')?.focus({ preventScroll: true });
}

/* The data changed since the times were read: read the board, then the
   times (fresh), the choice made from the old ones dropped. A meeting the
   board then shows gone or refused is read by itself once it is back and
   free (its times count as out of date until then). */
async function checkMoveAgain() {
  const session = MOVE.token;
  Object.assign(MOVE, { state: 'loading', version: null, picked: false });
  drawMoveDialog();
  if (TW.boardId) await selectBoard(TW.boardId);
  if (session !== MOVE.token || !moveDialogOpen()) return;
  const live = movePlacement();
  if (!live || moveRefusal(live)) {
    MOVE.version = -1;
    drawMoveDialog();
    return;
  }
  readMoveCandidates({ fresh: true });
}

/* Move to this time. Nothing is sent when the meeting is not what its times
   were read for. A refusal is said in the dialog; one that asking again
   cannot change (a lock, another scenario's section, published, gone)
   holds Commit; a failure that does not say whether the move happened reads
   the board, whose answer then stops a second move if it did. On success the
   dialog closes, the board's tab stop moves to the card, the board is read
   again, and the report follows (with R's hint when the time's verdict had
   a room clash: the meeting kept its room). Escape while the write is out
   closes the dialog; the write still ends, and says how where the user is
   (dialogSay, reportEdit). */
const MOVE_FINAL_REFUSALS = ['PLACEMENT_LOCKED', 'PLACEMENT_EXTERNALLY_OWNED', 'SCENARIO_PUBLISHED', 'NOT_FOUND'];
async function commitMove() {
  const dialog = $('twMoveDialog');
  if (!moveDialogOpen() || MOVE.pending) return;
  if (moveSnapshotChanged()) {
    dialogSay(dialog, MOVE_WORDS.changed);
    drawMoveDialog();
    return;
  }
  const p = movePlacement();
  const chosen = chosenMove();
  if (!p || !chosen || MOVE.view !== 'ready' || MOVE.blocked) return;
  const session = MOVE.token;
  const at = { scenarioId: TW.scenarioId, boardId: TW.boardId };
  const about = { key: `placement:${p.id}`, name: `${isolatedText(p.course_code)} ${isolatedText(p.section)}` };
  const hint = (chosen.verdict?.items || []).some(item => item.issue_kind === 'room') ? MOVE_WORDS.roomHint : '';
  const target = { day: chosen.day, start_time: chosen.start, end_time: chosen.end };
  // The keyboard goes to the chosen time before Commit turns off under it.
  dialog.querySelector('input[name="twMoveChoice"]:checked')?.focus({ preventScroll: true });
  MOVE.pending = true;
  dialogSay(dialog, '');
  syncMoveDialog();
  const result = await commitPlacementMove(p.id, target, { dialog });
  if (session === MOVE.token) MOVE.pending = false;
  if (result.code) {
    const known = {
      PLACEMENT_LOCKED: REFUSAL.locked, PLACEMENT_EXTERNALLY_OWNED: REFUSAL.external, PLACEMENT_DUPLICATE: MOVE_WORDS.ownSection,
      SCENARIO_PUBLISHED: REFUSAL.published, SCENARIO_BUSY: REFUSAL.busy, NOT_FOUND: MOVE_WORDS.gone,
      published: REFUSAL.published, job: REFUSAL.job, busy: REFUSAL.busy,
    }[result.code];
    // The meeting this session is about (a later one too, opened while the
    // write was out): a refusal asking again cannot change holds Commit.
    if (MOVE.placementId === p.id && MOVE_FINAL_REFUSALS.includes(result.code)) MOVE.blocked = result.code;
    // The page's own hold (dialogWrite's check): it ends by itself, and its
    // words go with it.
    if (['published', 'job', 'busy'].includes(result.code)) MOVE.held = result.code;
    dialogSay(dialog, known || MOVE_WORDS.notSaved, 'warning', about);
    syncMoveDialog();
    if (!known && TW.scenarioId === at.scenarioId && TW.boardId === at.boardId) await selectBoard(at.boardId);
    return;
  }
  if (session === MOVE.token) window.UXControls.dialog.close(dialog, 'close');
  boardNav?.active(`placement:${p.id}`);
  if (TW.scenarioId === at.scenarioId) await selectBoard(TW.boardId);
  updateUndoRedoButtons();
  reportEdit(movedWords(target.day, target.start_time, target.end_time), [p.id], { dialog, about, hint, data: result.data });
}

(function wireMoveDialog() {
  const dialog = $('twMoveDialog');
  if (!dialog) return;
  // A <form method="dialog"> closes itself on submit: the page commits, and
  // closes it once the meeting has moved.
  $('twMoveForm').addEventListener('submit', event => {
    event.preventDefault();
    commitMove();
  });
  dialog.addEventListener('change', event => {
    if (event.target.name !== 'twMoveChoice') return;
    Object.assign(MOVE, { choice: event.target.value, picked: true });
    syncMoveDialog();
  });
  dialog.addEventListener('click', event => {
    if (!event.target.closest) return;
    if (event.target.closest('[data-tw-move-retry]')) readMoveCandidates({ fresh: true });
    else if (event.target.closest('[data-tw-move-recheck]')) checkMoveAgain();
  });
})();

/* Selection's Move…: none for an English section (it has "Change section
   pattern"); drawn off, with its reason in #twSelActionNote, while the gate
   holds it. Opening only reads, so a write in flight never holds it. */
function moveActionHtml(p) {
  if (globalThis.TimetableMeetingPolicy?.isEnglish(p)) return '';
  const refusal = cardActionRefusal('move', p);
  return `<button type="button" class="btn btn-sm btn-neutral" data-tw-action="move" data-focus-key="move" data-placement-id="${p.id}" aria-haspopup="dialog"`
    + `${refusal ? ' disabled data-tw-hold aria-describedby="twSelActionNote"' : ''}>${esc(MOVE_WORDS.action)}</button>`;
}

/* ── Place (design record §7.3, 5f) ──
   A planned section placed with its whole weekly pattern, in #twPlaceDialog:
   from a list row (Enter, or a click on its "Place…"), or a drop of a course
   that meets more than once a week (filtered to the dropped time). The
   patterns are what the server offers (/planned-slot-candidates/, one read
   with every pattern and its verdict, D15), in the order of the verdict, and
   a day and a time they must include filter them in place. A pattern with
   clashes is chosen like any other (owner rule): its verdict is said, never
   confirmed. The body names the course and sends every meeting
   (commitPlannedCreate, as a one-meeting drop does), and one Undo removes
   every meeting. Rooms come afterwards (R on a card). */
const PLACE_WORDS = IS_AR ? {
  hint: 'وضع…', title: 'وضع',
  meetingsWeek: n => pluralAr(n, { two: 'لقاءان أسبوعياً', few: '{n} لقاءات أسبوعياً', many: '{n} لقاءً أسبوعياً', other: '{n} لقاء أسبوعياً' }),
  whole: 'النمط الأسبوعي كاملاً', once: 'لقاء واحد أسبوعياً', join: '، ',
  termNote: (term, board) => `هذا المقرر في المستوى ${term}، وهذه اللوحة للمستوى ${board}.`,
  mustInclude: 'يشمل', day: 'اليوم', time: 'الوقت', anyDay: 'أي يوم', anyTime: 'أي وقت', lab: 'معمل',
  legend: { patterns: 'الأنماط الأسبوعية الكاملة، الأفضل أولاً', times: 'الأوقات، الأفضل أولاً' },
  loading: { patterns: 'جارٍ ترتيب الأنماط…', times: 'جارٍ ترتيب الأوقات…' },
  count: {
    patterns: n => pluralAr(n, { zero: 'لا أنماط', one: 'نمط واحد', two: 'نمطان', few: '{n} أنماط', many: '{n} نمطاً', other: '{n} نمط' }),
    times: n => pluralAr(n, { zero: 'لا أوقات', one: 'وقت واحد', two: 'وقتان', few: '{n} أوقات', many: '{n} وقتاً', other: '{n} وقت' }),
  },
  clean: {
    patterns: n => pluralAr(n, { zero: 'لا نمط بلا تعارض', one: 'نمط واحد بلا تعارض', two: 'نمطان بلا تعارض', few: '{n} أنماط بلا تعارض', many: '{n} نمطاً بلا تعارض', other: '{n} نمط بلا تعارض' }),
    times: n => pluralAr(n, { zero: 'لا وقت بلا تعارض', one: 'وقت واحد بلا تعارض', two: 'وقتان بلا تعارض', few: '{n} أوقات بلا تعارض', many: '{n} وقتاً بلا تعارض', other: '{n} وقت بلا تعارض' }),
  },
  ranked: (count, clean) => `${count}، الأفضل أولاً · ${clean}`,
  include: (k, n, what) => `${k} من ${n} تشمل ${what}`,
  fallback: what => `لا يوجد نمط يشمل ${what}؛ تُعرض كل الأنماط.`,
  empty: 'لا يوجد نمط كامل يناسب أعمدة التدريس في هذه اللوحة. راجع «تعديل الفترات الزمنية…» والأوقات المحجوبة.',
  failed: { patterns: 'تعذّر ترتيب الأنماط.', times: 'تعذّر ترتيب الأوقات.' },
  stale: { patterns: 'تغيّر الجدول منذ قراءة هذه الأنماط.', times: MOVE_WORDS.stale },
  progress: 'جارٍ وضع الشعبة…',
  rooms: 'تُختار القاعات بعد الوضع (R على البطاقة). ولتغيير لقاء واحد لاحقاً استخدم «نقل…».',
  online: 'عن بُعد في هذه اللوحة: لا تُحجز قاعة.',
  needsPattern: n => `يحتاج هذا المقرر الآن إلى ${pluralAr(n, { one: 'لقاء واحد أسبوعياً', two: 'لقاءين أسبوعياً', few: '{n} لقاءات أسبوعياً', many: '{n} لقاءً أسبوعياً', other: '{n} لقاء أسبوعياً' })}؛ أُعيد ترتيب الأنماط.`,
  duplicate: (was, now) => `وُضعت الشعبة ${was} في هذه الأثناء؛ هذه الآن ${now}.`,
  allPlaced: c => `وُضعت كل شعب ${c}.`,
  boardChanged: 'تغيّرت اللوحة؛ لم يُوضع شيء.', nothingPlaced: 'لم يُوضع شيء.', noBoard: 'أنشئ لوحة أولاً.',
  needsBudget: 'اسحب هذه الشعبة إلى الجدول لوضعها؛ الوضع بلوحة المفاتيح يحتاج إلى ميزانية شعب.',
} : {
  hint: 'Place…', title: 'Place',
  meetingsWeek: n => `${n} meetings a week`,
  whole: 'the whole weekly pattern', once: 'one meeting a week', join: ', ',
  termNote: (term, board) => `This course is in term ${term}; this board is term ${board}.`,
  mustInclude: 'Must include', day: 'Day', time: 'Time', anyDay: 'Any day', anyTime: 'Any time', lab: 'Lab',
  legend: { patterns: 'Complete weekly patterns, best first', times: 'Times, best first' },
  loading: { patterns: 'Ranking the patterns…', times: 'Ranking the times…' },
  count: { patterns: n => (n === 1 ? '1 pattern' : `${n} patterns`), times: n => (n === 1 ? '1 time' : `${n} times`) },
  clean: { patterns: n => `${n} with no clash`, times: n => `${n} with no clash` },
  ranked: (count, clean) => `${count}, best first · ${clean}`,
  include: (k, n, what) => `${k} of ${n} include ${what}`,
  fallback: what => `No pattern includes ${what}; showing every pattern.`,
  empty: "No complete pattern fits this board's teaching columns. Check Edit time slots… and blocked times.",
  failed: { patterns: 'The patterns could not be ranked.', times: 'The times could not be ranked.' },
  stale: { patterns: 'The timetable changed since these patterns were read.', times: MOVE_WORDS.stale },
  progress: 'Placing the section…',
  rooms: 'Rooms are chosen afterwards (R on a card). To change one meeting later, use Move….',
  online: 'Online for this board: no room is booked.',
  needsPattern: n => `This course now needs ${n === 1 ? '1 meeting' : `${n} meetings`} a week; the patterns were ranked again.`,
  duplicate: (was, now) => `Section ${was} was placed meanwhile; this is now ${now}.`,
  allPlaced: c => `Every section of ${c} is placed.`,
  boardChanged: 'The board changed; nothing was placed.', nothingPlaced: 'Nothing was placed.', noBoard: 'Create a board first.',
  needsBudget: 'Drag this section onto the board to place it; keyboard placement needs a section budget.',
};

/* The dialog's session, as Move's: what it is asked to place (`request`)
   and under which label now (`label`: another one once the asked one was
   taken meanwhile), on which board (`scenarioId`, `boardId`: what its
   patterns are read for and a commit is checked against), what it read
   (`rows`, the filter's `options`, at data version `version`), the filter,
   what is chosen. `includes` is a drop's time, taken once as the filter;
   `fallback`, that time when no pattern included it (the filter cleared). */
const PLACE = {
  token: 0, read: 0, request: null, label: '', meetings: 1, scenarioId: null, boardId: null, origin: '', includes: null,
  state: 'loading', answer: null, failure: null, rows: [], options: null, filter: { day: '', time: '' }, fallback: null,
  choice: '', picked: false, version: null, pending: false, blocked: '', held: '', view: '', words: '', drawn: null,
};
function placeDialogOpen() {
  return Boolean(window.UXControls?.dialog?.isOpen($('twPlaceDialog')));
}
/* The board the dialog is about is the one open and loaded now. */
const placeBoardHere = () => TW.scenarioId === PLACE.scenarioId && TW.boardId === PLACE.boardId && TW.loadedBoardId === PLACE.boardId;
const placeDataKey = () => [TW.scenarioId, TW.boardId, TW.loadedBoardId, TW.boards, TW.sectionBudget, TW.placements, TW.dataVersion];
const placeReadOutdated = () => PLACE.version != null && PLACE.version !== TW.dataVersion;
const placeKind = () => (PLACE.meetings > 1 ? 'patterns' : 'times');
const placeDayIndex = day => (DAYS.includes(day) ? DAYS.indexOf(day) : DAYS.length);
const placeMinutes = time => { const [h, m] = String(time || '').split(':').map(Number); return (h || 0) * 60 + (m || 0); };
const placeAbout = (label = PLACE.label) => ({
  key: `place:${PLACE.request.course_key}|${label}`, name: `${isolatedText(PLACE.request.course_code)} ${isolatedText(label)}`,
});
function placeBudgetRow() {
  const key = String(PLACE.request?.course_key || '').trim().toUpperCase();
  return (TW.sectionBudget || []).find(c => String(c.course_key || c.course_code || '').trim().toUpperCase() === key) || null;
}
/* The budget (read again after every write) names the label as placed:
   another section of this course took it while the dialog was open. */
function placeLabelTaken() {
  const row = placeBudgetRow();
  return Boolean(row) && usedSectionLabels(row).has(String(PLACE.label).trim().toUpperCase());
}
/* Online for this board, as the board says of another section of the
   course (a placement's `is_online` is the course's on its board); not
   known until one is placed here (the server has no flag for a course). */
function placeOnline() {
  const code = String(PLACE.request?.course_code || '').trim().toUpperCase();
  return TW.placements.some(p => String(p.course_code || '').trim().toUpperCase() === code && p.is_online);
}

/* The candidates as rows, in the verdict's order (verdict_rank; the
   server's own order breaks ties). A row is a whole pattern: its meetings
   as the server lists them (a verdict item's `meeting` is an index into
   them), and by day (`sorted`, its title and its value `DAY@start-end|…`);
   the same pattern twice is one row, at its best rank. A one-meeting
   candidate is its own meeting. */
function placeRows(answer) {
  const rank = c => (c.verdict_rank !== null && c.verdict_rank !== undefined && Number.isFinite(Number(c.verdict_rank)) ? Number(c.verdict_rank) : Infinity);
  const seen = new Set();
  const rows = [];
  (answer?.candidates || []).map((candidate, index) => ({ candidate, index }))
    .sort((a, b) => (rank(a.candidate) - rank(b.candidate)) || (a.index - b.index))
    .forEach(({ candidate }) => {
      const listed = Array.isArray(candidate.meetings) && candidate.meetings.length ? candidate.meetings : [candidate];
      const meetings = listed.map(m => ({ kind: String(m.kind || candidate.kind || ''), day: String(m.day || '').toUpperCase(), start: String(m.start || ''), end: String(m.end || '') }));
      const sorted = [...meetings].sort((a, b) => (placeDayIndex(a.day) - placeDayIndex(b.day)) || (placeMinutes(a.start) - placeMinutes(b.start)));
      const sig = sorted.map(m => `${m.day}@${m.start}-${m.end}`).join('|');
      if (!meetings.every(m => m.day && m.start && m.end) || seen.has(sig)) return;
      seen.add(sig);
      rows.push({ sig, meetings, sorted, verdict: candidate.verdict || null });
    });
  return rows;
}
/* The filter's choices, from the rows' meetings: the days in week order,
   and each time (start–end) by its start, marked a lab when every meeting
   at it is one. */
function placeOptions(rows) {
  const days = new Set();
  const times = new Map();
  rows.forEach(row => row.meetings.forEach(m => {
    days.add(m.day);
    const key = `${m.start}-${m.end}`;
    times.set(key, { key, start: m.start, end: m.end, lab: (times.get(key)?.lab ?? true) && m.kind === 'lab' });
  }));
  return {
    days: [...days].sort((a, b) => placeDayIndex(a) - placeDayIndex(b)),
    times: [...times.values()].sort((a, b) => (placeMinutes(a.start) - placeMinutes(b.start)) || (placeMinutes(a.end) - placeMinutes(b.end))),
  };
}
/* A row includes the filter's day at its time: one of its meetings is on
   that day at that time (either alone when the other is "any"). */
function placeMatches(row, filter = PLACE.filter) {
  if (!filter.day && !filter.time) return true;
  return row.meetings.some(m => (!filter.day || m.day === filter.day) && (!filter.time || `${m.start}-${m.end}` === filter.time));
}
/* "Thursday 13:00–14:15" (plain text, the time isolated). */
function placeWhat(filter) {
  const [start, end] = String(filter.time || '').split('-');
  return [filter.day ? moveDayWord(filter.day) : '', filter.time ? isolatedText(`${start}–${end}`) : ''].filter(Boolean).join(' ');
}
function chosenPlace() {
  const row = PLACE.choice ? PLACE.rows.find(item => item.sig === PLACE.choice) : null;
  return row && placeMatches(row) ? row : null;
}

/* What the dialog shows: the board gone from view (a reload, or another
   board), every section placed, the read's state, or its patterns — stale
   once the data moved since they were read. (A new label is always read
   again before anything can be chosen.) */
function placeDialogView() {
  if (!placeBoardHere()) return { state: 'gone' };
  if (PLACE.blocked === 'allPlaced') return { state: 'allPlaced' };
  if (PLACE.state !== 'ready') return { state: PLACE.state };
  if (placeReadOutdated()) return { state: 'stale' };
  return { state: PLACE.rows.length ? 'ready' : 'empty' };
}
const placeGoneFailure = failure => failure?.code === 'NOT_FOUND' || failure?.status === 404;
function placeFailureWords(failure) {
  if (placeGoneFailure(failure)) return PLACE_WORDS.boardChanged;
  return failure?.code === 'SERVER_BUSY' || failure?.status === 503 ? ROOM_WORDS.serverBusy : PLACE_WORDS.failed[placeKind()];
}
/* The list's line: how many, best first, and how many with no clash; with
   a filter, how many include what it asks; a drop's time that no pattern
   includes, said with the filter cleared. */
function placeListWords() {
  const kind = placeKind();
  const rows = PLACE.rows;
  if (PLACE.fallback) return PLACE_WORDS.fallback(placeWhat(PLACE.fallback));
  if (PLACE.filter.day || PLACE.filter.time) return PLACE_WORDS.include(rows.filter(row => placeMatches(row)).length, rows.length, placeWhat(PLACE.filter));
  return PLACE_WORDS.ranked(PLACE_WORDS.count[kind](rows.length), PLACE_WORDS.clean[kind](rows.filter(row => row.verdict?.severity === 'ok').length));
}
function placeViewWords(view) {
  const kind = placeKind();
  if (view.state === 'gone') {
    // A reload of the open scenario takes the board away for a moment.
    return TW.scenarioId === PLACE.scenarioId && (!TW.boardId || TW.loadedBoardId !== TW.boardId) ? REFUSAL.loading : PLACE_WORDS.boardChanged;
  }
  return {
    loading: PLACE_WORDS.loading[kind], error: placeFailureWords(PLACE.failure), stale: PLACE_WORDS.stale[kind],
    empty: PLACE_WORDS.empty, ready: view.state === 'ready' ? placeListWords() : '', allPlaced: '',
  }[view.state] || '';
}

/* "Monday 13:00–14:15 · Lab" (HTML, the time isolated left to right); a
   line's lead is short, "Mon 13:00–14:15", as the line's own times are. */
function placeMeetingHtml(m, { lead = false } = {}) {
  const day = lead ? dayName(m.day) : moveDayWord(m.day);
  return `${esc(day)} <bdi dir="ltr">${esc(m.start)}–${esc(m.end)}</bdi>${!lead && m.kind === 'lab' ? ` · ${esc(PLACE_WORDS.lab)}` : ''}`;
}
/* The course's term when it is not the board's: context, never a refusal. */
function placeTermNoteHtml() {
  const term = Number(PLACE.request?.programme_term);
  const board = TW.boards.find(b => b.id === PLACE.boardId) || PLACE.answer?.board;
  const nominal = Number(board?.nominal_term);
  if (!term || !nominal || term === nominal) return '';
  return `<p class="tw-place-note" role="note"><span class="ux-tone-icon" data-tone="warning" aria-hidden="true"></span><span>${esc(PLACE_WORDS.termNote(term, nominal))}</span></p>`;
}
/* Must include: a day and a time, from the patterns read (each "any" first);
   the times as plain text, isolated. */
function placeFilterHtml() {
  const W = PLACE_WORDS;
  const o = PLACE.options;
  const option = (value, text, chosen) => `<option value="${esc(value)}"${value === chosen ? ' selected' : ''}>${esc(text)}</option>`;
  const days = [option('', W.anyDay, PLACE.filter.day), ...o.days.map(day => option(day, moveDayWord(day), PLACE.filter.day))].join('');
  const times = [option('', W.anyTime, PLACE.filter.time),
    ...o.times.map(t => option(t.key, `${isolatedText(`${t.start}–${t.end}`)}${t.lab ? ` · ${W.lab}` : ''}`, PLACE.filter.time))].join('');
  return `<fieldset class="ux-field"><legend class="ux-field-label">${esc(W.mustInclude)}</legend><div class="ux-field-row fb-fit">`
    + `<div class="fb-search fb-sm"><label class="fb-lbl" for="twPlaceDay">${esc(W.day)}</label><select id="twPlaceDay" data-focus-key="place-day" aria-controls="twPlaceChoices">${days}</select></div>`
    + `<div class="fb-search fb-sm"><label class="fb-lbl" for="twPlaceTime">${esc(W.time)}</label><select id="twPlaceTime" data-focus-key="place-time" aria-controls="twPlaceChoices">${times}</select></div>`
    + '</div></fieldset>';
}
function placeSubHtml() {
  const board = TW.boards.find(b => b.id === PLACE.boardId) || PLACE.answer?.board || {};
  const name = `<bdi>${esc(board.label || '')}</bdi>`;
  return PLACE.meetings > 1 ? `${name} · ${esc(PLACE_WORDS.meetingsWeek(PLACE.meetings))} · ${esc(PLACE_WORDS.whole)}` : `${name} · ${esc(PLACE_WORDS.once)}`;
}
/* The body: the term note, the filter (kept drawn while the patterns are
   read again, so a keyboard in it stays there), then the patterns, best
   first: each names every meeting, then its verdict, then at most three of
   its items, each led by the meeting it belongs to. Every other state is its
   words in the status line, with its way on here. */
function placeDialogHtml(view) {
  const W = PLACE_WORDS;
  let html = placeTermNoteHtml();
  if (PLACE.options && (view.state === 'ready' || view.state === 'loading')) html += placeFilterHtml();
  if (view.state === 'loading') return `${html}<div class="ux-choice-list" id="twPlaceChoices" aria-busy="true"></div>`;
  if (view.state === 'stale') {
    return `${html}<p class="tw-place-action"><button type="button" class="btn btn-sm btn-neutral" data-tw-place-recheck data-focus-key="place-recheck">${esc(ROOM_WORDS.checkAgain)}</button></p>`;
  }
  if (view.state === 'error') {
    return html + (placeGoneFailure(PLACE.failure) ? ''
      : `<p class="tw-place-action"><button type="button" class="btn btn-sm btn-neutral" data-tw-place-retry data-focus-key="place-retry">${esc(ROOM_WORDS.retry)}</button></p>`);
  }
  if (view.state !== 'ready') return html;
  const rows = PLACE.rows.map((row, index) => {
    const id = `twPlaceRow${index}`;
    const verdict = row.verdict;
    const lead = item => (Number.isInteger(item.meeting) && row.meetings[item.meeting] ? `${placeMeetingHtml(row.meetings[item.meeting], { lead: true })}: ` : '');
    const lines = moveLines(verdict, id, { boardId: PLACE.boardId, lead });
    return `<label class="ux-choice"${verdict ? ` data-tone="${esc(verdict.severity)}"` : ''}${placeMatches(row) ? '' : ' hidden'}>`
      + `<input type="radio" name="twPlaceChoice" value="${esc(row.sig)}" data-focus-key="place-choice:${esc(row.sig)}" aria-labelledby="${id}-time"`
      + ` aria-describedby="${[`${id}-verdict`, ...lines.map(line => line.id)].join(' ')}"${row.sig === PLACE.choice ? ' checked' : ''}>`
      + `<span class="ux-choice-title">${moveToneIcon(verdict?.severity)}<span id="${id}-time">${row.sorted.map(m => placeMeetingHtml(m)).join(esc(W.join))}</span>`
      + `<span class="tw-move-verdict" id="${id}-verdict">${esc(moveVerdictWords(verdict))}</span></span>${moveLinesHtml(lines)}</label>`;
  }).join('');
  return `${html}<fieldset class="ux-field"><legend class="ux-field-label">${esc(W.legend[placeKind()])}</legend>`
    + `<div class="ux-choice-list" id="twPlaceChoices" data-ux-submit>${rows}</div></fieldset>`;
}

/* The whole dialog from PLACE and the board, through the global redraw (the
   same content leaves the nodes, and the keyboard, alone). Until the user
   chooses, the choice is the best pattern the filter shows. */
function drawPlaceDialog() {
  const body = $('twPlaceBody');
  if (!body || !PLACE.request) return;
  const view = placeDialogView();
  PLACE.view = view.state;
  if (!PLACE.picked) PLACE.choice = view.state === 'ready' ? (PLACE.rows.find(row => placeMatches(row))?.sig || '') : '';
  PLACE.words = placeViewWords(view);
  setDialogHead('twPlaceTitle', `${esc(PLACE_WORDS.title)} <bdi>${esc(PLACE.request.course_code)}</bdi> <bdi>${esc(PLACE.label)}</bdi>`);
  setDialogHead('twPlaceSub', placeSubHtml());
  const html = placeDialogHtml(view);
  if (window.UXControls?.redraw) window.UXControls.redraw(body, html);
  else body.innerHTML = html;
  body.querySelectorAll('input[name="twPlaceChoice"]').forEach(radio => { radio.checked = radio.value === PLACE.choice; });
  ['day', 'time'].forEach(part => {
    const select = $(part === 'day' ? 'twPlaceDay' : 'twPlaceTime');
    if (select && select.value !== PLACE.filter[part]) select.value = PLACE.filter[part];
  });
  // The keyboard on a row the dialog (not the user) chose goes with its choice.
  if (!PLACE.picked && document.activeElement?.matches('#twPlaceBody input[name="twPlaceChoice"]')) {
    body.querySelector('input[name="twPlaceChoice"]:checked')?.focus({ preventScroll: true });
  }
  PLACE.drawn = placeDataKey();
  syncPlaceDialog();
}

/* Commit, its note and the status line, in place (cheap: it runs with
   every control sync). A label taken meanwhile is replaced by the next free
   one; patterns asked for before the data moved are asked for again while
   the user has chosen none (a chosen one waits for Check again), never
   while the dialog's own create is out. Commit is held until a pattern the
   filter shows is chosen from a list that is current; it is off while the
   page cannot write, and the note says why (published, a planner job,
   busy), else what the chosen pattern brings and what comes afterwards. */
function syncPlaceDialog() {
  if (!placeDialogOpen()) return;
  // Shown as gone (a scenario reload takes the board away for a moment):
  // patterns read before it left are out of date when it comes back.
  if (PLACE.view === 'gone') PLACE.version = -1;
  const free = placeBoardHere() && !PLACE.pending && !PLACE.blocked;
  if (free && placeLabelTaken()) {
    relabelPlace();
    return;
  }
  if (free && placeReadOutdated() && !PLACE.picked) {
    readPlaceCandidates();
    return;
  }
  if (PLACE.held && writeHold() !== PLACE.held) {
    PLACE.held = '';
    dialogSay($('twPlaceDialog'), '');
  }
  if (!PLACE.drawn || placeDataKey().some((value, i) => value !== PLACE.drawn[i])) {
    drawPlaceDialog();
    return;
  }
  const chosen = chosenPlace();
  const held = PLACE.view !== 'ready' || !chosen || PLACE.pending || Boolean(PLACE.blocked);
  const commit = $('twPlaceCommit');
  commit.toggleAttribute('data-tw-hold', held);
  commit.disabled = held || !workspaceCanEdit();
  const hold = writeHold();
  const verdict = chosen ? moveVerdictWords(chosen.verdict) : '';
  const note = PLACE.pending ? '' : hold ? REFUSAL[hold] : held ? ''
    : [verdict ? MOVE_WORDS.chosen(verdict) : '', placeOnline() ? PLACE_WORDS.online : PLACE_WORDS.rooms].filter(Boolean).join(' · ');
  const line = $('twPlaceNote');
  if (line.textContent !== note) line.textContent = note;
  const words = PLACE.pending ? PLACE_WORDS.progress : PLACE.words;
  const status = $('twPlaceStatus');
  if (status.textContent !== words) status.textContent = words;
  // The list is busy while the create is out; the status lines are not.
  $('twPlaceDialog').querySelectorAll('.ux-choice-list[data-ux-submit]').forEach(list => {
    if (PLACE.pending) list.setAttribute('aria-busy', 'true');
    else list.removeAttribute('aria-busy');
  });
}

/* The filter hides rows in place (no redraw). A chosen pattern it hides
   gives way to the first one it shows, chosen by the dialog (not the user). */
function applyPlaceFilter() {
  const body = $('twPlaceBody');
  if (!body) return;
  let first = null;
  body.querySelectorAll('input[name="twPlaceChoice"]').forEach(radio => {
    const row = PLACE.rows.find(item => item.sig === radio.value);
    const shown = Boolean(row) && placeMatches(row);
    const label = radio.closest('.ux-choice');
    if (label) label.hidden = !shown;
    if (shown && !first) first = radio;
  });
  const chosen = body.querySelector('input[name="twPlaceChoice"]:checked');
  if (!chosen || chosen.closest('[hidden]')) {
    if (chosen) chosen.checked = false;
    if (first) first.checked = true;
    Object.assign(PLACE, { choice: first ? first.value : '', picked: false });
  }
  if (PLACE.view === 'ready') PLACE.words = placeListWords();
  syncPlaceDialog();
}

/* A drop's time, taken once per open as the filter. No pattern includes
   it: the filter is cleared and that is said. A filter the new list no
   longer offers a value of is "any" for that part. */
function placeTakeIncludes() {
  const includes = PLACE.includes;
  PLACE.includes = null;
  if (includes) {
    const asked = { day: String(includes.day || '').toUpperCase(), time: `${includes.start}-${includes.end}` };
    const found = PLACE.rows.some(row => placeMatches(row, asked));
    PLACE.filter = found ? asked : { day: '', time: '' };
    PLACE.fallback = found ? null : asked;
    return;
  }
  const { days, times } = PLACE.options;
  PLACE.filter = {
    day: days.includes(PLACE.filter.day) ? PLACE.filter.day : '',
    time: times.some(t => t.key === PLACE.filter.time) ? PLACE.filter.time : '',
  };
}

/* Opened from a list row (Enter, its "Place…") or a drop; `origin` names
   where the keyboard goes back to at close (the row, by its key). Any open
   dialog first, then a scenario with no board, then the gate's parts that
   apply to a section not yet placed (published, loading, a planner job). */
function openPlaceDialog(request, { origin = '', includes = null } = {}) {
  if (anyDialogOpen()) return false;
  if (!TW.boardId) {
    notify.warning(PLACE_WORDS.noBoard);
    return false;
  }
  const refusal = cardActionRefusal('place', request);
  if (refusal) {
    notify.warning(refusal.text);
    return false;
  }
  const dialog = $('twPlaceDialog');
  if (!dialog || !request || !window.UXControls?.dialog) return false;
  Object.assign(PLACE, {
    token: PLACE.token + 1, request: { ...request }, label: String(request.section_label || 'S1').trim().toUpperCase(), meetings: Number(request.required) || 1,
    scenarioId: TW.scenarioId, boardId: TW.boardId, origin, includes,
    state: 'loading', answer: null, failure: null, rows: [], options: null, filter: { day: '', time: '' }, fallback: null,
    choice: '', picked: false, version: null, pending: false, blocked: '', held: '', view: '', words: '', drawn: null,
  });
  const alert = $('twPlaceAlert');
  if (alert) { alert.textContent = ''; alert.hidden = true; }
  // What it is about, for the words a write of another section says in it.
  dialog.dataset.twAbout = placeAbout().key;
  drawPlaceDialog();
  if (!window.UXControls.dialog.open(dialog, { initialFocus: () => $('twPlaceBody'), returnFocus: () => resolveOrigin(origin) })) return false;
  readPlaceCandidates();
  return true;
}

/* Every pattern at once (limit past the total) with its verdict, drawn if
   the answer is still this session's latest read. A new list is a new
   choice, except `keep`:
   a pattern the user chose, kept when the list still offers it (a label
   taken meanwhile changes the section, not the user's mind). The keyboard
   then goes from the body to the chosen row, unless the user has moved it. */
async function readPlaceCandidates({ fresh = false, keep = '' } = {}) {
  const session = PLACE.token;
  const read = ++PLACE.read;
  Object.assign(PLACE, { state: 'loading', answer: null, failure: null, rows: [], choice: '', picked: false, version: TW.dataVersion });
  drawPlaceDialog();
  const r = PLACE.request;
  const label = PLACE.label;
  const query = new URLSearchParams({ course_code: r.course_code, course_key: r.course_key, section_label: label, limit: '1000', verdict: '1' });
  const answer = await previewRead(`planned|${PLACE.boardId}|${r.course_key}|${label}`,
    `/ops/tw/boards/${PLACE.boardId}/planned-slot-candidates/?${query}`, { fresh });
  if (session !== PLACE.token || read !== PLACE.read || !placeDialogOpen()) return;
  if (answer.data) {
    const rows = placeRows(answer.data);
    const kept = Boolean(keep) && rows.some(row => row.sig === keep);
    Object.assign(PLACE, {
      state: 'ready', answer: answer.data, rows, options: placeOptions(rows),
      meetings: Number(answer.data.request?.required_meetings_per_section) || PLACE.meetings, choice: kept ? keep : '', picked: kept,
    });
    placeTakeIncludes();
  } else {
    Object.assign(PLACE, { state: 'error', failure: answer });
  }
  drawPlaceDialog();
  const body = $('twPlaceBody');
  if (document.activeElement === body) body.querySelector('input[name="twPlaceChoice"]:checked')?.focus({ preventScroll: true });
}

/* The data changed since the patterns were read: read the board, then the
   patterns (fresh), the choice made from the old ones dropped. A board gone
   from view by then is read once it is back. */
async function checkPlaceAgain() {
  const session = PLACE.token;
  Object.assign(PLACE, { state: 'loading', version: null, picked: false });
  drawPlaceDialog();
  if (TW.boardId) await selectBoard(TW.boardId);
  if (session !== PLACE.token || !placeDialogOpen()) return;
  if (!placeBoardHere()) {
    PLACE.version = -1;
    drawPlaceDialog();
    return;
  }
  if (PLACE.blocked !== 'allPlaced') readPlaceCandidates({ fresh: true });
}

/* The label is not this dialog's any more (the budget names it placed, or
   the server refused it as a duplicate): the next free label, said, its
   patterns read (a section's teacher, and so the verdict, can differ by
   label), the pattern the user chose kept if it is still offered. None
   left: every section of the course is placed, and Commit holds. */
function relabelPlace() {
  const dialog = $('twPlaceDialog');
  const was = PLACE.label;
  const row = placeBudgetRow();
  const next = row ? freeSectionLabels(row).find(label => label !== was) : '';
  if (!next) {
    PLACE.blocked = 'allPlaced';
    dialogSay(dialog, PLACE_WORDS.allPlaced(isolatedText(PLACE.request.course_code)), 'warning');
    drawPlaceDialog();
    return;
  }
  PLACE.label = next;
  dialog.dataset.twAbout = placeAbout().key;
  dialogSay(dialog, PLACE_WORDS.duplicate(isolatedText(was), isolatedText(next)), 'warning');
  readPlaceCandidates({ fresh: true, keep: PLACE.picked ? PLACE.choice : '' });
}

/* Place section. Nothing is sent when the open board is not the one the
   dialog opened on (another scenario or board, or the board still loading),
   or the label was taken meanwhile (the next one is offered). A refusal is said in the dialog: the server's "complete pattern"
   reads the patterns again (the course needs another count), a duplicate
   takes the next free label, and what asking again cannot change
   (published, the board gone) holds Commit; a failure that does not say
   whether the section was made reads the board, whose budget then stops a
   second create of the same label. On success the dialog closes (the
   keyboard back on the list, by the row's key), the board's tab stop moves
   to the first new card, the board is read again, the new cards pulse, and
   the report follows (contract D). Escape while the write is out closes the
   dialog; the write still ends, and says how where the user is. */
const PLACE_FINAL_REFUSALS = ['SCENARIO_PUBLISHED', 'NOT_FOUND'];
async function commitPlace() {
  const dialog = $('twPlaceDialog');
  if (!placeDialogOpen() || PLACE.pending) return;
  if (!placeBoardHere()) {
    dialogSay(dialog, PLACE_WORDS.boardChanged);
    drawPlaceDialog();
    return;
  }
  if (!PLACE.blocked && placeLabelTaken()) {
    relabelPlace();
    return;
  }
  const chosen = chosenPlace();
  if (!chosen || PLACE.view !== 'ready' || PLACE.blocked) return;
  const session = PLACE.token;
  const at = { scenarioId: TW.scenarioId, boardId: TW.boardId };
  const request = PLACE.request;
  const label = PLACE.label;
  const about = placeAbout(label);
  const body = plannedCreateBody(request, at.boardId, label, chosen.sorted);
  // The keyboard goes to the chosen pattern before Commit turns off under it.
  dialog.querySelector('input[name="twPlaceChoice"]:checked')?.focus({ preventScroll: true });
  PLACE.pending = true;
  dialogSay(dialog, '');
  syncPlaceDialog();
  const result = await commitPlannedCreate(body, { dialog });
  const here = session === PLACE.token;
  if (here) PLACE.pending = false;
  if (result.code) {
    await placeRefused(result, { dialog, here, session, about, request, label, at });
    return;
  }
  if (here) window.UXControls.dialog.close(dialog, 'close');
  const ids = result.rows.map(row => row.id);
  if (ids.length) boardNav?.active(`placement:${ids[0]}`);
  if (TW.scenarioId === at.scenarioId) await selectBoard(TW.boardId);
  updateUndoRedoButtons();
  highlightWorkspacePlacements(ids);
  reportEdit(placedWords(request.course_code, label), ids, { dialog, about, hint: MOVE_WORDS.roomHint, data: result.data });
}
/* A create the server or the page refused, said where the user is (this
   dialog, the one open by then, or a toast), named when that dialog is
   about another section. */
async function placeRefused(result, { dialog, here, session, about, request, label, at }) {
  const { code } = result;
  // A create the user left: Escape closes the dialog without starting a new
  // session, so a closed dialog counts as left too. Its words go where the
  // user is (dialogSay: the dialog open by then, named, or a toast), and
  // nothing is read or relabelled for a dialog that is gone.
  const left = !here || !placeDialogOpen();
  if (code === 'FULL_SECTION_PATTERN_REQUIRED') {
    // The course meets another number of times a week now: the list's rows
    // and the patterns are read again.
    loadBudget();
    if (left) {
      dialogSay(dialog, PLACE_WORDS.nothingPlaced, 'warning', about);
      return;
    }
    await readPlaceCandidates({ fresh: true });
    if (session !== PLACE.token) return;
    if (PLACE.state === 'ready') dialogSay(dialog, PLACE_WORDS.needsPattern(PLACE.meetings), 'warning');
    else if (PLACE.state === 'error') dialogSay(dialog, PLACE_WORDS.nothingPlaced, 'warning');
    return;
  }
  if (code === 'PLACEMENT_DUPLICATE') {
    await loadBudget();
    if (left) {
      dialogSay(dialog, PLACE_WORDS.boardChanged, 'warning', about);
      return;
    }
    // The budget read may already have moved the dialog on (its sync).
    if (session === PLACE.token && PLACE.label === label) relabelPlace();
    return;
  }
  const known = {
    SCENARIO_PUBLISHED: REFUSAL.published, SCENARIO_BUSY: REFUSAL.busy, NOT_FOUND: PLACE_WORDS.boardChanged,
    VALIDATION_PLANNED_PATTERN: PLACE_WORDS.nothingPlaced, published: REFUSAL.published, job: REFUSAL.job, busy: REFUSAL.busy,
  }[code];
  // The section this session is about (a later one too, opened while the
  // write was out): a refusal asking again cannot change holds Commit.
  const same = placeDialogOpen() && PLACE.request?.course_key === request.course_key && PLACE.label === label && PLACE.boardId === at.boardId;
  if (same && PLACE_FINAL_REFUSALS.includes(code)) PLACE.blocked = code;
  // The page's own hold (dialogWrite's check): it ends by itself, and its
  // words go with it.
  if (['published', 'job', 'busy'].includes(code)) PLACE.held = code;
  dialogSay(dialog, known || PLACE_WORDS.nothingPlaced, 'warning', about);
  syncPlaceDialog();
  if (!known && TW.scenarioId === at.scenarioId && TW.boardId === at.boardId) await selectBoard(at.boardId);
}

(function wirePlaceDialog() {
  const dialog = $('twPlaceDialog');
  if (!dialog) return;
  // A <form method="dialog"> closes itself on submit: the page commits, and
  // closes it once the section is placed.
  $('twPlaceForm').addEventListener('submit', event => {
    event.preventDefault();
    commitPlace();
  });
  dialog.addEventListener('change', event => {
    const target = event.target;
    if (target.name === 'twPlaceChoice') {
      Object.assign(PLACE, { choice: target.value, picked: true });
      syncPlaceDialog();
    } else if (target.id === 'twPlaceDay' || target.id === 'twPlaceTime') {
      PLACE.filter = { ...PLACE.filter, [target.id === 'twPlaceDay' ? 'day' : 'time']: target.value };
      PLACE.fallback = null;
      applyPlaceFilter();
    }
  });
  dialog.addEventListener('click', event => {
    if (!event.target.closest) return;
    if (event.target.closest('[data-tw-place-retry]')) readPlaceCandidates({ fresh: true });
    else if (event.target.closest('[data-tw-place-recheck]')) checkPlaceAgain();
  });
  // "Place…" on a list row, for the pointer (the row itself is the
  // keyboard's; a click anywhere else on it is a drag's).
  $('twSectionList')?.addEventListener('click', event => {
    const cell = event.target.closest?.('.tw-place-hint')?.closest('[role="gridcell"]');
    if (cell) activateListRow(cell);
  });
})();

/* ── "Mainly for programme" (Selection) ──
   The section's programme mark (#160 parity with split view; the words and
   parts are the shared module's). The seated mix and the grouping trade-off
   are the plan lens this page already holds: after an edit they say they are
   from before it and offer a Refresh — the lens is a full seating evaluation,
   never re-read per edit. A saved mark reads it once more, and the quality
   and the publish check with it (both are about the seating). The panel's
   redraw keeps the keyboard on the select through a save. */
// Section → the mark being saved (the select shows it while it saves).
const PROGRAMME_MARK_PENDING = new Map();
function programmeMarkHtml(p) {
  const PM = window.TimetableProgrammeMark;
  if (!PM || !PM.hasMark(p)) return '';
  const lens = TW.planLens || {};
  const key = String(p.term_section_id);
  const pending = PROGRAMME_MARK_PENDING.has(key);
  const shown = pending ? { ...p, preferred_programme: PROGRAMME_MARK_PENDING.get(key) } : p;
  // Off for its own reasons (held through the page's edit guard), or by it.
  const hold = p.preferred_programme_editable === false || pending;
  const disabled = hold || !workspaceCanEdit();
  const nobody = lens.loaded ? PM.nobodyHtml(p, courseLensForItem(p)) : '';
  const fresh = lens.loaded && !lens.loading && lens.version === TW.dataVersion;
  // A section the lens does not hold (another scenario's, or new) is unknown,
  // not empty.
  const sectionLens = lens.loaded ? sectionLensForPlacement(p) : null;
  const mix = !sectionLens ? '—'
    : (PM.mixHtml(sectionLens) || esc(IS_AR ? 'لا يُقعَد فيها طلاب هذا السيناريو' : 'No student of this scenario sits here'));
  const trade = lens.loaded ? PM.tradeHtml(lens.programme_grouping) : '';
  const state = lens.loading ? PM.words.loading
    : !lens.loaded ? (IS_AR ? 'توزيع البرامج غير متاح.' : 'The programme mix is not available.')
      : fresh ? '' : (IS_AR ? 'من قبل آخر تعديل.' : 'From before your last change.');
  const refresh = fresh ? ''
    : `<button type="button" class="btn btn-sm btn-neutral" data-tw-lens-refresh data-focus-key="lens-refresh"${lens.loading ? ' disabled aria-busy="true"' : ''}>${IS_AR ? 'تحديث' : 'Refresh'}</button>`;
  return `<h3 class="tw-sel-subhead">${IS_AR ? 'البرنامج' : 'Programme'}</h3>
    <dl class="tw-sel-fields">
      <div class="tw-sel-field tw-sel-field-stack"><dt><label for="twSelProgramme">${esc(PM.words.label)}</label></dt>
        <dd><select id="twSelProgramme" class="form-select form-select-sm tw-programme-select" data-placement-id="${p.id}" data-tw-mutation${hold ? ' data-tw-hold' : ''}${disabled ? ' disabled' : ''}${pending ? ' aria-busy="true"' : ''}${nobody ? ' aria-describedby="twSelProgrammeNote"' : ''}>${PM.optionsHtml(shown, lens.plans)}</select>
          ${nobody ? `<span class="tw-sel-note" id="twSelProgrammeNote" data-tone="warning">${nobody}</span>` : ''}</dd></div>
      <div class="tw-sel-field tw-sel-field-stack"><dt>${esc(PM.words.seated)}</dt><dd>${mix}${trade ? `<span class="tw-sel-hint">${trade}</span>` : ''}${state ? `<span class="tw-sel-hint">${esc(state)} ${refresh}</span>` : ''}</dd></div>
    </dl>`;
}

function redrawSelection() {
  if (document.querySelector('.tw-right .tw-panel-tab.active')?.dataset.tab === 'selection') renderRightPanel('selection');
}

// One listener each for the panel's select and Refresh, however often it is
// drawn. A keyboard choice saves once the arrow keys rest.
window.TimetableProgrammeMark?.saveOnSettle($('twRightBody'), '#twSelProgramme',
  (placementId, value) => saveWorkspaceProgrammeMark(placementId, value),
  select => Number(select.dataset.placementId));
$('twRightBody').addEventListener('click', event => {
  const button = event.target.closest && event.target.closest('[data-tw-lens-refresh]');
  if (button && !button.disabled) refreshPlanLens();
});

async function saveWorkspaceProgrammeMark(placementId, value) {
  const p = TW.placements.find(row => row.id === placementId);
  if (!window.TimetableProgrammeMark?.hasMark(p)) return;
  // One write at a time (the page's edit guard). A keyboard choice waits for
  // the keys to rest, and the page can turn busy meanwhile: the select shows
  // the saved mark again and the user hears why, never a silent drop.
  if (!workspaceCanEdit()) {
    const select = document.querySelector(`#twSelProgramme[data-placement-id="${placementId}"]`);
    if (select) select.value = String(p.preferred_programme || '');
    notify.warning(IS_AR ? 'الجدول قيد التحديث؛ لم يُحفظ البرنامج.' : 'The timetable is being updated; the programme was not saved.');
    return;
  }
  const key = String(p.term_section_id);
  PROGRAMME_MARK_PENDING.set(key, value);
  redrawSelection();
  let changed = false;
  try {
    const data = await twFetch(`/ops/tw/placements/${placementId}/programme-preference/`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preferred_programme: value }),
      // The same mark again changes nothing: every answer stays current.
      _changed: body => Boolean(body?.changed),
    });
    if (data && typeof data.preferred_programme === 'string') {
      // Every meeting of the section takes the server's answer.
      TW.placements.forEach(row => { if (String(row.term_section_id) === key) row.preferred_programme = data.preferred_programme; });
      changed = Boolean(data.changed);
      if (changed) notify.success(window.TimetableProgrammeMark.words.saved);
    }
  } finally {
    // The select comes back (with the keyboard, unless the user moved on);
    // a refused save shows the mark the server holds.
    PROGRAMME_MARK_PENDING.delete(key);
    redrawSelection();
  }
  if (!changed) return;
  // The seating changed: the seated mix is read again, once, and then the
  // quality and the publish check (seats and student clashes are seating).
  await refreshPlanLens();
  await refreshWorkspaceQuality();
}

/* ── Right Panel ── */
/* An item from before the last edit says so, in Issues and in Selection. */
function issuePendingWord() {
  return TW.readinessLoading || TW.qualityLoading
    ? (IS_AR ? 'يجري التحقق' : 'checking')
    : (IS_AR ? 'من قبل آخر تعديل' : 'from before your last change');
}

// An Issues row with sections takes the user to them (another board's first).
$('twRightBody').addEventListener('click', async event => {
  const row = event.target.closest && event.target.closest('.tw-issue-row[data-ids]');
  if (!row) return;
  const targetBoard = parseInt(row.dataset.boardTarget || '', 10);
  try {
    const ids = JSON.parse(row.dataset.ids);
    if (Number.isFinite(targetBoard) && targetBoard && targetBoard !== TW.boardId) {
      await selectBoard(targetBoard);
    }
    const card = highlightWorkspacePlacements(ids);
    const p = card && TW.placements.find(row => String(row.id) === card.dataset.placementId);
    if (p) {
      const day = DAY_LABELS[DAYS.indexOf(p.day)] || p.day;
      const where = `${day} \u2066${p.start_time}–${p.end_time}\u2069`;
      const what = `\u2066${p.course_code} ${p.section}\u2069`;
      announceBoard(IS_AR
        ? `عُثر عليها: ${where}، ${what}. والتنقل بالمفتاح Tab إلى الجدول يصل إليها.`
        : `Located: ${where}, ${what}. The board's tab stop is on it.`);
    }
  } catch {}
});

function renderRightPanel(tab) {
  const body = $('twRightBody');
  // The panel is drawn again after every answer from the server: the design
  // system's redraw leaves unchanged content alone and keeps the keyboard on
  // the same control (a toggle, a row, a button) when it changes.
  const draw = html => (window.UXControls?.redraw ? window.UXControls.redraw(body, html) : ((body.innerHTML = html), true));

  if (tab === 'issues') {
    const model = buildBoardIssues();
    setIssuesCount(issueCountOf(model), model.counts.critical);
    // With no board open (a scenario with none yet), the tab has no board to
    // speak for; the scenario's own blockers are on Publish.
    if (!TW.boardId) {
      draw(`<div class="tw-empty tw-empty-sm"><p>${IS_AR ? 'افتح لوحة لعرض مشاكلها.' : 'Open a board to see its issues.'}</p></div>`);
      return;
    }
    const note = text => `<p class="tw-issue-note">${text}</p>`;
    let notes = '';
    if (!model.known.conflicts) notes += note(IS_AR ? 'تعارضات هذه اللوحة غير محمّلة.' : 'This board’s clashes are not loaded.');
    const checking = Boolean(TW.readinessLoading || TW.qualityLoading);
    const pendingWord = issuePendingWord();
    if (model.rows.some(row => row.pending)) {
      notes += note(checking
        ? (IS_AR ? 'بعض البنود من قبل آخر تعديل؛ يجري التحقق منها.' : 'Some items are from before your last change; they are being checked.')
        : (IS_AR ? 'بعض البنود من قبل آخر تعديل؛ لم يُعَد التحقق منها بعد.' : 'Some items are from before your last change and have not been checked again.'));
    }
    if (TW.quality && !TW.quality.rooms?.assessed) notes += note(IS_AR ? 'صلاحية القاعات لم تُقيّم بعد.' : 'Room suitability has not been assessed yet.');

    if (!model.rows.length) {
      draw(model.known.conflicts
        ? `<div class="tw-empty tw-empty-sm"><span class="ux-tone-icon" data-tone="ok" aria-hidden="true"></span><p>${IS_AR ? 'لا مشاكل في هذه اللوحة' : 'No issues on this board'}</p></div>${notes}`
        : notes);
      return;
    }

    // Three groups in severity order, each one opening and closing (global
    // disclosure; the choice holds for the session, across every redraw).
    // Context starts closed. Inside each, rows by kind as the model sorts.
    let html = notes;
    TONE_ORDER.forEach(tone => {
      const rows = model.rows.filter(row => row.tone === tone);
      if (!rows.length) return;
      const key = `tw-issues:${tone}`;
      const fallback = tone !== 'info';
      const open = window.UXControls?.disclosure ? window.UXControls.disclosure.isOpen(key, fallback) : fallback;
      const region = `twIssues-${tone}`;
      html += `<section class="tw-issue-group" data-tone="${tone}"><h3 class="tw-issue-group-head">`
        + `<button type="button" class="ux-disclosure-toggle tw-issue-head" data-tone="${tone}" data-ux-disclosure="${key}" aria-expanded="${open}" aria-controls="${region}">`
        + `<span class="ux-tone-icon" data-tone="${tone}" aria-hidden="true"></span><span class="ux-disclosure-label">${esc(TONE_LEAD[tone])}</span>`
        + `<span class="tw-issue-count tnum">${model.counts[tone]}</span><span class="ux-disclosure-chevron" aria-hidden="true"></span></button></h3>`
        + `<div class="tw-issue-group-body" id="${region}"${open ? '' : ' hidden'}>`;
      rows.forEach(row => {
        const inner = `<span class="tw-issue-kind">${esc(issueKindLabel(row.kind))}${row.pending ? ` · ${pendingWord}` : ''}</span>`
          + `<span class="tw-issue-subject"><bdi>${isolateTimes(row.subject)}</bdi></span>`
          + (row.detail ? `<span class="tw-issue-detail"><bdi>${isolateTimes(row.detail)}</bdi></span>` : '');
        html += row.ids.length && row.locate !== false
          ? `<button type="button" class="tw-issue-row" data-tone="${tone}" data-focus-key="${esc(`${row.kind}:${row.ids.join(',')}`)}" data-ids='${esc(JSON.stringify(row.ids))}'${row.boardTarget ? ` data-board-target="${esc(row.boardTarget)}"` : ''}>${inner}</button>`
          : `<div class="tw-issue-row tw-issue-static" data-tone="${tone}">${inner}</div>`;
      });
      html += '</div></section>';
    });
    draw(html);
  } else if (tab === 'selection') {
    if (!TW.selectedPlacementId) {
      draw(`<div class="tw-empty tw-empty-sm"><p>${IS_AR ? 'اختر شعبة في الجدول لعرض تفاصيلها.' : 'Select a section on the grid to inspect it.'}</p></div>`);
      return;
    }
    const p = TW.placements.find(x => x.id === TW.selectedPlacementId);
    if (!p) { draw(''); return; }
    const english = globalThis.TimetableMeetingPolicy?.isEnglish(p);
    const dayLabel = code => DAY_LABELS[DAYS.indexOf(code)] || code || '—';
    const field = (label, value) => `<div class="tw-sel-field"><dt>${label}</dt><dd>${value}</dd></div>`;
    // A count of 0 is a count; only a missing one is unknown.
    const count = value => (value === null || value === undefined || value === '' || !Number.isFinite(Number(value)))
      ? '—' : esc(Number(value).toLocaleString());
    // The section's teacher by the one-teacher rule (a meeting row can carry a
    // stale name, so the meetings list below names no one).
    const teacher = window.TimetableSectionTeacher.teacherOf(p);
    const teacherHtml = teacher.state === 'tbd'
      ? `${esc(window.TimetableSectionTeacher.words.tbd)}${teacher.hint ? `<span class="tw-sel-hint">${esc(teacher.hint)}</span>` : ''}`
      : teacher.state === 'named' ? `<bdi>${esc(teacher.name)}</bdi>` : esc(window.TimetableSectionTeacher.words.none);
    // What is wrong with this section, from the board's one issue model: what
    // blocks publishing or lowers quality (context stays in the Issues tab),
    // each naming what it is about — a pair shows both sections. An item from
    // before the last edit says so, as in Issues (the card does not ring for it).
    const ownRows = buildBoardIssues().rows.filter(row => row.tone !== 'info' && row.ids.includes(String(p.id)));
    const ownIssues = ownRows.length
      ? `<h3 class="tw-sel-subhead">${IS_AR ? 'مشاكل هذه الشعبة' : 'This section’s issues'}</h3><ul class="tw-sel-issues">${ownRows.map(row =>
        `<li data-tone="${row.tone}"><span class="ux-tone-icon" data-tone="${row.tone}" role="img" aria-label="${esc(TONE_LEAD[row.tone])}"></span>`
        + `<span><span class="tw-issue-kind">${esc(issueKindLabel(row.kind))}${row.pending ? ` · ${esc(issuePendingWord())}` : ''}</span> <bdi class="tw-sel-issue-subject">${isolateTimes(row.subject)}</bdi>${row.detail ? `<span class="tw-sel-hint"><bdi>${isolateTimes(row.detail)}</bdi></span>` : ''}</span></li>`).join('')}</ul>`
      : '';
    const meetings = (p.meetings || []).map(m => `<li class="ux-meta"><span>${esc(dayLabel(m.day))}</span><bdi dir="ltr">${esc(m.start_time)}–${esc(m.end_time)}</bdi><bdi>${esc(english ? TimetableMeetingPolicy.roomLabel(p, IS_AR) : meetingRoomText(m.room, p))}</bdi></li>`).join('');
    const editable = workspaceCanEdit();
    // Change room (5c) and Move… (5e); why each is held is written into
    // #twSelActionNote by syncSelectionActions, right after this draw.
    const roomAction = roomActionHtml(p);
    const moveAction = moveActionHtml(p);
    draw(`
      <div class="tw-sel-head">
        <div class="tw-sel-title"><bdi>${esc(p.course_code)}</bdi> <bdi>${esc(p.section)}</bdi></div>
        ${p.course_name ? `<div class="tw-sel-sub">${esc(compactCourseName(p.course_name))}</div>` : ''}
        ${p.is_locked ? `<span class="pill-status pill-b">${IS_AR ? 'الوقت مقفل' : 'Time locked'}</span>` : ''}
      </div>
      ${ownIssues}
      ${english ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))} · SUN / MON / WED / THU</div><button type="button" class="tw-pattern-action" data-focus-key="pattern" data-tw-mutation ${editable ? '' : 'disabled'} onclick="chooseEnglishWorkspacePattern({type:'move',placement_id:${p.id}})">${IS_AR ? 'تغيير نمط الشعبة' : 'Change section pattern'}</button>` : ''}
      <dl class="tw-sel-fields">
        ${field(IS_AR ? 'اليوم' : 'Day', esc(dayLabel(p.day)))}
        ${field(IS_AR ? 'الوقت' : 'Time', `<bdi>${esc(p.start_time)}–${esc(p.end_time)}</bdi>`)}
        ${field(IS_AR ? 'القاعة' : 'Room', `<bdi>${esc(placementRoomText(p) || (IS_AR ? 'لم تُحدَّد' : 'Not set'))}</bdi>${roomAction}`)}
        ${field(IS_AR ? 'المدرس' : 'Teacher', teacherHtml)}
        ${field(IS_AR ? 'السعة' : 'Capacity', count(p.available_capacity))}
        ${field(IS_AR ? 'المسجلون' : 'Registered', count(p.registered_count))}
      </dl>
      ${roomAction || moveAction ? '<p class="tw-sel-help" id="twSelActionNote" hidden></p>' : ''}
      ${programmeMarkHtml(p)}
      ${meetings ? `<h3 class="tw-sel-subhead">${IS_AR ? 'جميع لقاءات الشعبة' : 'All meetings of this section'}</h3><ul class="tw-sel-meetings">${meetings}</ul>` : ''}
      <div class="tw-sel-actions">
        ${moveAction}
        <button type="button" class="btn btn-sm btn-neutral" data-focus-key="lock" data-tw-mutation ${editable ? '' : 'disabled'} title="${esc(T.lockHelp)}" onclick="toggleLock(${p.id})">${p.is_locked ? (IS_AR ? 'فتح قفل الوقت' : 'Unlock time') : (IS_AR ? 'قفل الوقت' : 'Lock time')}</button>
        <button type="button" class="btn btn-sm btn-outline-danger" data-focus-key="remove" data-tw-mutation ${editable ? '' : 'disabled'} onclick="removePlacement(${p.id})">${IS_AR ? 'إزالة من الجدول' : 'Remove from timetable'}</button>
      </div>
      <p class="tw-sel-help">${esc(T.lockHelp)}</p>
    `);
    // A redraw of the same content leaves the nodes as they were: bring the
    // actions in line with the gate now.
    syncSelectionActions();
  } else if (tab === 'capacity') {
    const courses = (TW.capacityData && TW.capacityData.courses) || [];
    if (courses.length === 0) {
      body.innerHTML = `<div class="tw-empty" style="padding:24px"><p>${IS_AR ? 'لا توجد بيانات سعة' : 'No capacity data'}</p></div>`;
      return;
    }
    const t = (TW.capacityData && TW.capacityData.totals) || {};
    let html = `<dl class="tw-cap-totals">
      <div><dt>${IS_AR ? 'الطلب' : 'Demand'}</dt><dd class="tnum">${(t.demand || 0).toLocaleString()}</dd></div>
      <div><dt>${IS_AR ? 'السعة' : 'Seats'}</dt><dd class="tnum">${(t.raw_capacity || 0).toLocaleString()}</dd></div>
      <div${(t.deficit || 0) > 0 ? ' data-tone="critical"' : ''}><dt>${IS_AR ? 'العجز' : 'Short'}</dt><dd class="tnum">${(t.deficit || 0).toLocaleString()}</dd></div>
    </dl>`;
    // Most constrained first: the courses short of seats lead.
    const ordered = [...courses].sort((a, b) => (b.deficit || 0) - (a.deficit || 0) || (b.demand || 0) - (a.demand || 0));
    ordered.slice(0, 30).forEach(c => {
      const pct = c.demand > 0 ? Math.min(100, Math.round((c.raw_capacity / c.demand) * 100)) : 100;
      html += `<div class="tw-cap-row"${c.deficit > 0 ? ' data-tone="critical"' : ''}>
        <div class="tw-cap-line"><bdi class="fw-semibold">${esc(c.course_code)}</bdi><span class="tnum">${c.raw_capacity} / ${c.demand}${c.deficit > 0 ? ` <strong>−${c.deficit}</strong>` : ''}</span></div>
        <div class="tw-cap-bar"><i style="width:${pct}%"></i></div>
      </div>`;
    });
    if (courses.length > 30) html += `<p class="tw-pane-note">${IS_AR ? `${courses.length - 30} مقرراً آخر في تبويب الطلب.` : `${courses.length - 30} more courses in the Demand tab.`}</p>`;
    body.innerHTML = html;
  } else if (tab === 'demand') {
    renderDemandTable(body);
  } else if (tab === 'mri') {
    renderMriPanel(body);
  }
}

function renderMriPanel(body) {
  const activeBoard = TW.boards.find(b => b.id === TW.boardId) || {};
  const conflicts = boardConflicts();
  const overlaps = conflicts.overlaps || [];
  const iClashes = conflicts.instructor_clashes || [];
  const rClashes = conflicts.room_clashes || [];
  const crossBoard = activeBoardCrossBoardClashes();
  const roomShortages = activeBoardRoomShortages();
  const roomsAssessed = Boolean(TW.quality?.rooms?.assessed);
  const impact = conflicts.student_impact || {};
  const capacity = TW.capacityData || {};
  const capTotals = capacity.totals || {};
  const capCourses = capacity.courses || [];
  const deficitCourses = capCourses.filter(c => (c.deficit || 0) > 0);
  const primary = activeBoard.primary_count || 0;
  const visitor = activeBoard.visitor_count || 0;
  const students = primary + visitor;
  const boardStudents = TW.boards.map(b => (b.primary_count || 0) + (b.visitor_count || 0));
  const avgBoardStudents = boardStudents.length
    ? Math.round(boardStudents.reduce((sum, n) => sum + n, 0) / boardStudents.length)
    : 0;
  const studentDelta = Math.abs(students - avgBoardStudents);
  const criticalOverlaps = overlaps.filter(o => !isWarningOverlap(o));
  const warningOverlaps = overlaps.filter(isWarningOverlap);
  // The same counts as the Issues tab (one model); demand deficits are MRI's own.
  const issueCounts = buildBoardIssues().counts;
  const critical = issueCounts.critical;
  const warnings = issueCounts.warning + deficitCourses.length;
  const affected = impact.affected_count || 0;
  const blocked = impact.blocked_count || 0;
  // A non-blocking overlap is common course demand students can route round,
  // so it weighs what a potential cross-board demand overlap weighs.
  const pressure = Math.min(100, Math.round(
    criticalOverlaps.length * 28 +
    warningOverlaps.length * 4 +
    iClashes.length * 20 +
    rClashes.length * 12 +
    roomShortages.length * 12 +
    crossBoard.length * 4 +
    deficitCourses.length * 8 +
    Math.min(25, Math.ceil(affected / 4)) +
    blocked * 2
  ));
  const status = pressure >= 70 ? 'critical' : pressure >= 35 ? 'watch' : 'stable';

  const rows = [
    {
      key: 'student-flow',
      label: 'Student flow (selected board)',
      value: affected,
      max: Math.max(1, students),
      tone: blocked > 0 ? 'critical' : affected > 0 ? 'watch' : 'stable',
      detail: `${affected} affected, ${impact.resolvable_count || 0} reroutable, ${blocked} blocked`,
    },
    {
      key: 'time-layer',
      label: 'Same-board clash',
      value: overlaps.length,
      max: Math.max(1, TW.placements.length),
      tone: criticalOverlaps.length ? 'critical' : warningOverlaps.length ? 'watch' : 'stable',
      detail: `${criticalOverlaps.length} same-board time clashes, ${warningOverlaps.length} non-blocking overlaps`,
    },
    {
      key: 'cross-board-clash',
      label: 'Potential demand overlap',
      value: crossBoard.length,
      max: Math.max(1, TW.boards.length * 4),
      tone: crossBoard.length ? 'watch' : 'stable',
      detail: `${crossBoard.length} overlapping course-demand pairs; assigned seating is assessed separately`,
    },
    {
      key: 'capacity-layer',
      label: 'Capacity layer',
      value: capTotals.deficit || 0,
      max: Math.max(1, capTotals.demand || 1),
      tone: (capTotals.deficit || 0) > 0 ? 'watch' : 'stable',
      detail: `${deficitCourses.length} courses under capacity`,
    },
    {
      key: 'resource-layer',
      label: 'Room/instructor layer',
      value: iClashes.length + rClashes.length + roomShortages.length,
      max: Math.max(1, TW.placements.length),
      tone: (iClashes.length || rClashes.length) ? 'critical' : (roomShortages.length || !roomsAssessed) ? 'watch' : 'stable',
      detail: `${iClashes.length} instructor clashes, ${rClashes.length} room double-bookings; ${roomsAssessed ? roomShortages.length + ' meetings lack a suitable room' : 'room suitability unknown'}`,
    },
    {
      key: 'term-layer',
      label: 'Term/board layer',
      value: studentDelta,
      max: Math.max(1, avgBoardStudents),
      tone: studentDelta > Math.max(15, avgBoardStudents * 0.35) ? 'watch' : 'stable',
      detail: `Term ${activeBoard.nominal_term || '-'} | ${students} students vs ${avgBoardStudents || 0} board average`,
    },
  ];

  const topPairs = [
    ...roomShortages.map(room => ({ kind: 'Room suitability', tone: 'watch', ids: room.placement_ids,
      title: room.course_code || room.course_key || room.room || (IS_AR ? 'بلا قاعة' : 'No room'),
      detail: `${(room.reasons || []).join(', ')}; required capacity ${room.required_capacity ?? '—'}` })),
    ...[...criticalOverlaps, ...warningOverlaps].map(o => ({
      kind: isWarningOverlap(o) ? 'Same-board overlap (non-blocking)' : 'Same-board time clash',
      tone: isWarningOverlap(o) ? 'watch' : 'critical',
      ids: o.ids || [],
      title: (o.sections || []).join(' / '),
      detail: o.detail || '',
    })),
    ...crossBoard.slice(0, 6).map(c => ({
      kind: 'Potential demand overlap',
      tone: 'watch',
      ids: c.ids || [c.placement_a_id, c.placement_b_id].filter(Boolean),
      targetBoard: c.board_a_id === TW.boardId ? c.board_b_id : c.board_a_id,
      title: `${c.section_a} <-> ${c.section_b}`,
      detail: `${c.overlap_count} students requesting both courses | ${c.board_a_label} <-> ${c.board_b_label}`,
    })),
    ...deficitCourses.slice(0, 4).map(c => ({
      kind: 'Capacity',
      tone: 'watch',
      title: c.course_code,
      detail: `${c.demand} demand / ${c.raw_capacity} seats / ${c.deficit} deficit`,
    })),
  ].slice(0, 10);

  const studentDetails = (impact.overlap_details || []).slice(0, 5);

  body.innerHTML = `
    <div class="tw-mri">
      <div class="tw-mri-hero ${status}">
        <div>
          <div class="tw-mri-kicker">Timetable MRI</div>
          <div class="tw-mri-title">${esc(activeBoard.label || 'No board selected')}</div>
          <div class="tw-mri-sub">${students} students scanned across ${TW.placements.length} placed sections</div>
        </div>
        <div class="tw-mri-score">
          <span>${pressure}</span>
          <small>pressure</small>
        </div>
      </div>

      <div class="tw-mri-scan" aria-label="MRI pressure scan">
        ${rows.map(r => `<span class="${r.tone}" style="--w:${mriPct(r.value, r.max)}%"></span>`).join('')}
      </div>

      <div class="tw-mri-metrics">
        <div><span>${critical}</span><small>critical</small></div>
        <div><span>${warnings}</span><small>warnings</small></div>
        <div><span>${affected}</span><small>affected</small></div>
        <div><span>${capTotals.deficit || 0}</span><small>seat gap</small></div>
      </div>

      <div class="tw-mri-section-title">Diagnostic Layers</div>
      ${rows.map(r => `
        <button class="tw-mri-layer ${r.tone}" type="button" data-mri-layer="${esc(r.key)}">
          <span class="tw-mri-layer-main">
            <strong>${esc(r.label)}</strong>
            <em>${esc(r.detail)}</em>
          </span>
          <span class="tw-mri-bar"><i style="width:${mriPct(r.value, r.max)}%"></i></span>
        </button>
      `).join('')}

      <div class="tw-mri-section-title">Hot Spots</div>
      ${topPairs.length ? topPairs.map(h => `
        <button class="tw-mri-hotspot ${h.tone}" type="button"
          ${h.ids ? `data-ids='${JSON.stringify(h.ids)}'` : ''}
          ${h.targetBoard ? `data-board-target="${h.targetBoard}"` : ''}>
          <span>${esc(h.kind)}</span>
          <strong>${esc(h.title || '-')}</strong>
          <em>${esc(h.detail || '')}</em>
        </button>
      `).join('') : `<div class="tw-mri-empty">No active hot spots in this board.</div>`}

      <div class="tw-mri-section-title">Student Impact Slice (selected board)</div>
      ${studentDetails.length ? studentDetails.map(d => `
        <div class="tw-mri-students ${d.blocked ? 'critical' : 'stable'}">
          <div><strong>${esc((d.courses || []).join(' / '))}</strong><span>${d.affected} affected</span></div>
          <small>${d.resolvable ? 'Alternative route exists' : 'No clean route found'}${(d.students || []).length ? ' | sample: ' + esc((d.students || []).join(', ')) : ''}</small>
        </div>
      `).join('') : `<div class="tw-mri-empty">No student overlap slice for this board.</div>`}
    </div>
  `;

  wireMriInteractions(body);
}

function mriPct(value, max) {
  if (!max || max <= 0) return 0;
  return Math.max(3, Math.min(100, Math.round((value / max) * 100)));
}

// The server decides an overlap's severity once (H15): critical when it is
// same-course or leaves students with no clash-free pair of sections, a
// warning when they can route round it. A row without one stays critical.
function isWarningOverlap(overlap) {
  return overlap?.severity === 'warning';
}

/* The loaded conflicts when they describe the current board, else none: between
   a board switch and that board's own answer (or after a failed load) the
   previous board's clashes are never shown or counted as this board's. */
function boardConflicts() {
  return boardConflictsKnown() ? (TW.conflicts || {}) : {};
}
function boardConflictsKnown() {
  return Boolean(TW.boardId) && TW.conflictsBoardId === TW.boardId;
}

function activeBoardCrossBoardClashes() {
  const boardId = String(TW.boardId);
  return (boardConflicts().cross_board_conflicts || []).filter(c =>
    String(c.board_a_id) === boardId || String(c.board_b_id) === boardId
  );
}

/* Point at cards: a short animation that removes itself (it used to stay,
   and several cards then looked selected). Reduced motion gets a still mark
   for the same time. */
function highlightWorkspacePlacements(ids) {
  document.querySelectorAll('.tw-card.highlight').forEach(c => c.classList.remove('highlight'));
  let firstCard = null;
  (ids || []).forEach(id => {
    const card = document.querySelector(`.tw-card[data-placement-id="${id}"]`);
    if (card) {
      // A card in a table the filter folded: its fold opens first.
      openFoldOf(card);
      // A pulse already running restarts (the class alone would not), and
      // one timer per card, so an earlier call cannot cut a later pulse short.
      void card.offsetWidth;
      card.classList.add('highlight');
      const clear = () => { clearTimeout(card._twLocateTimer); card.classList.remove('highlight'); };
      card.addEventListener('animationend', clear, { once: true });
      clearTimeout(card._twLocateTimer);
      card._twLocateTimer = setTimeout(clear, 1600);
      if (!firstCard) firstCard = card;
    }
  });
  if (firstCard) {
    // Tab into the board reaches it; the keyboard stays where it is.
    boardNav?.active(firstCard.dataset.focusKey);
    firstCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  return firstCard;
}

function wireMriInteractions(body) {
  body.querySelectorAll('.tw-mri-hotspot').forEach(row => {
    row.addEventListener('click', async () => {
      const targetBoard = parseInt(row.dataset.boardTarget || '', 10);
      let ids = [];
      try { ids = JSON.parse(row.dataset.ids || '[]'); } catch {}
      if (Number.isFinite(targetBoard) && targetBoard && targetBoard !== TW.boardId) {
        await selectBoard(targetBoard);
      }
      if (ids.length) highlightWorkspacePlacements(ids);
    });
  });
}

async function toggleLock(pid) {
  if (!workspaceCanEdit()) return;
  await twFetch(`/ops/tw/placements/${pid}/lock/`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
  });
  await selectBoard(TW.boardId);
}

async function removePlacement(pid) {
  if (!workspaceCanEdit()) return;
  const scenarioAtStart = TW.scenarioId;
  const boardAtStart = TW.boardId;
  const ok = await dlg.confirm({ title: IS_AR ? 'إزالة الشعبة؟' : 'Remove placement?', kind: 'danger' });
  if (!ok) return;
  if (TW.scenarioId !== scenarioAtStart || TW.boardId !== boardAtStart || !workspaceCanEdit()) return;
  // Capture placement data before removing for undo
  const p = TW.placements.find(x => x.id === pid);
  const data = await twFetch(`/ops/tw/placements/${pid}/remove/`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
  });
  if (!data) return;
  if (p) {
    TW.undoStack.push({ type: 'remove', placement_id: pid,
      board_id: boardAtStart, term_section_id: p.term_section_id,
      day: p.day, start_time: p.start_time, end_time: p.end_time, room: p.room || '' });
    if (data.removed_placements?.length > 1) TW.undoStack.at(-1).meetings = TimetableMeetingPolicy.snapshot(data.removed_placements);
    TW.redoStack = [];
  }
  TW.selectedPlacementId = null;
  await selectBoard(TW.boardId);
  updateUndoRedoButtons();
}

/* ── Helpers ── */
function updateValidationBadge() {
  const badge = $('twValidationBadge');
  if (!badge) return;
  badge.hidden = !TW.scenario;
  if (!TW.scenario) return;
  const published = TW.scenario.status === 'published';
  badge.className = `pill-status ${published ? 'pill-g' : 'pill-a'}`;
  badge.textContent = published ? T.published : T.draft;
}

function updateQuickStats() {
  renderWorkspaceQuality();
  renderBoardTabs();
}

let qualityLoadRevision = 0;
/* The scenario's quality is a full seating evaluation — the heaviest read this
   page makes. It is scenario-wide, so a board switch reuses it; only a write
   (TW.dataVersion) or a reload makes it stale. */
async function refreshWorkspaceQuality(force = false) {
  const scenarioId = TW.scenarioId;
  const revision = ++qualityLoadRevision;
  if (!scenarioId) { TW.quality = null; renderWorkspaceQuality(); return; }
  if (!force && TW.quality && TW.qualityVersion === TW.dataVersion) {
    renderWorkspaceQuality();
    if (!TW.readiness || TW.readinessVersion !== TW.dataVersion) scheduleReadinessRefresh();
    return;
  }
  const version = TW.dataVersion;
  if ($('twQualityStatus')) $('twQualityStatus').textContent = IS_AR ? 'جاري تقييم جودة السيناريو…' : 'Evaluating scenario quality…';
  TW.qualityLoading = true;
  let data;
  try {
    data = await twFetch(`/ops/tw/scenarios/${scenarioId}/quality/`);
  } finally {
    if (revision === qualityLoadRevision) TW.qualityLoading = false;
  }
  if (revision !== qualityLoadRevision || TW.scenarioId !== scenarioId) { renderWorkspaceQuality(); return; }
  TW.quality = data?.ok ? data : null;
  TW.qualityVersion = version;
  // Schedule the publish check first: the panel then says "checking", not
  // "not checked again", for the debounce before it runs.
  scheduleReadinessRefresh();
  renderWorkspaceQuality();
  const active = document.querySelector('.tw-right .tw-panel-tab.active');
  if (active) renderRightPanel(active.dataset.tab);
}
/* The stat strip. Two scopes (whole scenario / instructors), each number with
   its meaning in its label and its direction in its tone:
   critical blocks something, warning lowers quality, ok meets its target.
   "—" is unknown and says why in its tooltip — never a coloured 0.
   The Issues count is drawn with it: every input of that count (the board's
   conflicts and placements, the quality, the readiness answer, a reset)
   redraws the strip. */
function renderWorkspaceQuality() {
  renderBoardIssueViews();
  const quality = TW.quality;
  const number = value => Number.isFinite(value) ? value.toLocaleString() : '—';
  const put = (id, value, { tone = null, title = '', danger = false } = {}) => {
    const el = $(id);
    if (!el) return;
    el.textContent = number(value);
    // `.danger` stays on the value: it is what a screen-level check (and the
    // one-teacher tests) read; the row's data-tone draws the bar and the icon.
    el.classList.toggle('danger', Boolean(danger));
    const row = el.parentElement;
    if (row && row.dataset) {
      const finalTone = !Number.isFinite(value) ? 'unknown' : tone;
      if (finalTone) row.dataset.tone = finalTone; else delete row.dataset.tone;
      if (title) row.title = title;
    }
  };
  const lowerIsBetter = IS_AR ? 'الأقل أفضل' : 'Lower is better';

  const seats = quality?.seats;
  const tiered = seats?.assessed && seats.layout === 'tiered';
  const highRiskRow = $('twStatHighRiskRow');
  const requiredRow = $('twStatRequiredRow');
  if (highRiskRow) highRiskRow.hidden = Boolean(seats?.assessed) && !tiered;
  if (requiredRow) requiredRow.hidden = Boolean(seats?.assessed) && !tiered;
  put('twQualityHighRisk', tiered ? seats.high_risk : null, { tone: tiered ? (seats.high_risk > 0 ? 'critical' : 'ok') : null, danger: tiered && seats.high_risk > 0,
    title: IS_AR ? 'طلاب معرضون للخطر (رسوب سابق أو إعادة مقرر) بلا مقعد في مقرر تخصص أو مقرر مشترك. الأولوية الأولى.' : 'At-risk students (a failed course on record, or retaking one) left without a seat in a major or foundation course. The first priority.' });
  put('twQualityRequired', tiered ? seats.required : null, { tone: tiered ? (seats.required > 0 ? 'critical' : 'ok') : null, danger: tiered && seats.required > 0,
    title: IS_AR ? 'طلبات مقررات التخصص والمقررات المشتركة فوق الحد المسموح دون مقعد.' : 'Requests for major courses, and shared foundations beyond their tolerance, left without a seat.' });
  // Flexible (soft) requests: gen-ed, electives and foundations within tolerance.
  // With the tiered objective off this one number is every unseated request.
  const flexibleLabel = $('twStatFlexibleRow')?.querySelector?.('dt');
  if (flexibleLabel) flexibleLabel.textContent = seats?.assessed && !tiered
    ? (IS_AR ? 'بلا مقعد' : 'Unseated')
    : (IS_AR ? 'مرنة بلا مقعد' : 'Flexible unseated');
  const flexible = tiered ? seats.flexible : seats?.assessed ? seats.total
    : quality?.soft_seats?.assessed ? quality.soft_seats.unresolved : null;
  put('twQualityUnplaced', flexible, { tone: flexible > 0 ? 'warning' : flexible === 0 ? 'ok' : null, danger: flexible > 0,
    title: IS_AR ? 'مقررات عامة واختيارية ومقررات مشتركة ضمن الحد المسموح دون مقعد.' : 'Gen-ed, elective and shared-foundation requests (within tolerance) left without a seat.' });
  const clashes = quality?.assigned_student_clashes;
  const clashStudents = clashes?.assessed ? (clashes.students ?? clashes.count) : null;
  put('twQualityClashes', clashStudents, { tone: clashStudents > 0 ? 'critical' : clashStudents === 0 ? 'ok' : null, danger: clashStudents > 0,
    title: IS_AR ? 'طلاب في التعيين المُقيّم لديهم شعبتان في وقت واحد.' : 'Students whose evaluated seating puts them in two sections at once.' });
  const rooms = quality?.rooms;
  // No room at all, or a double booking, stops publishing; a room that is merely
  // too small or the wrong type lowers quality.
  const roomsBlock = (rooms?.missing || 0) + (rooms?.clashes || 0) > 0;
  put('twQualityRooms', rooms?.assessed ? rooms.shortages : null, { tone: roomsBlock ? 'critical' : rooms?.shortages > 0 ? 'warning' : rooms?.assessed ? 'ok' : null, danger: rooms?.shortages > 0,
    title: rooms?.assessed
      ? (IS_AR ? `بلا قاعة: ${number(rooms.missing)} · أصغر من اللازم: ${number(rooms.undersized)} · غير مناسبة: ${number(rooms.incompatible)} · تعارض: ${number(rooms.clashes)} من ${number(rooms.physical_meetings)} لقاء` : `No room ${number(rooms.missing)} · too small ${number(rooms.undersized)} · wrong type ${number(rooms.incompatible)} · double-booked ${number(rooms.clashes)} — of ${number(rooms.physical_meetings)} meetings`)
      : (IS_AR ? 'غير مُقيّم' : 'Not assessed') });

  // Instructors: days and idle cover only the instructors the data knows.
  const instructor = quality?.instructor;
  const counted = instructor?.assessed ? instructor.instructors : null;
  if ($('twStatInstructorCount')) $('twStatInstructorCount').textContent = Number.isFinite(counted)
    ? (IS_AR ? `· ${counted.toLocaleString()} محسوب` : `· ${counted.toLocaleString()} counted`) : '';
  const noData = instructor?.assessed && counted === 0;
  const noDataTitle = IS_AR ? 'لا توجد بيانات مدرسين لهذا السيناريو.' : 'No instructor data for this scenario.';
  put('twQualityInstructorDays', instructor?.assessed && !noData ? instructor.working_days : null,
    { title: noData ? noDataTitle : `${IS_AR ? 'مجموع أيام الحضور لكل مدرس. يُحتفظ بيوم راحة واحد على الأقل.' : 'Each instructor’s days on campus, summed. At least one day off is kept.'} ${lowerIsBetter}.` });
  put('twQualityInstructorIdle', instructor?.assessed && !noData ? instructor.idle_minutes : null,
    { title: noData ? noDataTitle : `${IS_AR ? 'دقائق الانتظار بين محاضرات المدرس في اليوم نفسه.' : 'Minutes an instructor waits between classes on the same day.'} ${lowerIsBetter}.` });
  // One teacher per section: shown only when the quality endpoint reports it;
  // a warning when more sections lack a teacher than the D10 limit explains.
  const coverage = instructor?.coverage;
  const coverageRow = $('twQualityTeacherCoverageRow');
  if (coverageRow) coverageRow.hidden = !coverage;
  const coverageCell = $('twQualityTeacherCoverage');
  if (coverage && coverageCell) {
    const over = (coverage.tbd || 0) > (coverage.tbd_d10_floor || 0);
    coverageCell.textContent = `${(coverage.assigned || 0).toLocaleString()} / ${((coverage.assigned || 0) + (coverage.tbd || 0)).toLocaleString()}`;
    coverageCell.title = teacherCoverageText(coverage);
    coverageCell.classList.toggle('danger', over);
    if (coverageRow?.dataset) coverageRow.dataset.tone = over ? 'warning' : 'ok';
  }

  const status = $('twQualityStatus');
  if (!status) return;
  if (!TW.scenarioId) { status.textContent = IS_AR ? 'اختر سيناريو لعرض جودة الجدول.' : 'Select a scenario to view timetable quality.'; return; }
  if (!quality) { status.textContent = IS_AR ? 'التقييم غير متاح؛ — تعني غير معروف.' : 'Assessment unavailable; — means unknown.'; return; }
  status.textContent = IS_AR ? 'تم تحديث مؤشرات الجودة.' : 'Quality figures updated.';
}
function activeBoardRoomShortages() {
  const ids = new Set(TW.placements.map(placement => String(placement.id)));
  return (TW.quality?.rooms?.details || []).filter(detail => (detail.placement_ids || []).some(id => ids.has(String(id))));
}

/* esc() now in shared-utils.js */

/* ── Capacity ── */
async function loadCapacity() {
  if (!TW.boardId) return;
  const boardId = TW.boardId;
  const scenarioId = TW.scenarioId;
  const data = await twFetch(`/ops/tw/boards/${TW.boardId}/capacity/`);
  if (TW.boardId !== boardId || TW.scenarioId !== scenarioId) return;
  if (data) {
    TW.capacityData = data;
    // If capacity tab is active, re-render
    const activeTab = document.querySelector('.tw-right .tw-panel-tab.active');
    if (activeTab && ['capacity', 'demand', 'mri'].includes(activeTab.dataset.tab)) {
      renderRightPanel(activeTab.dataset.tab);
    }
  }
}

/* ── Demand tab: every course's demand against this board's seats ── */
function renderDemandTable(body) {
  const courses = (TW.capacityData && TW.capacityData.courses) || [];
  if (courses.length === 0) {
    body.innerHTML = `<div class="tw-empty tw-empty-sm"><p>${IS_AR ? 'لا توجد بيانات طلب بعد.' : 'No demand data yet.'}</p></div>`;
    return;
  }
  const t = (TW.capacityData && TW.capacityData.totals) || {};
  const rows = courses.map(c => `<tr${c.deficit > 0 ? ' data-tone="critical"' : ''}>
      <th scope="row"><bdi>${esc(c.course_code)}</bdi></th>
      <td>${c.demand}</td><td>${c.raw_capacity}</td><td>${c.placed_sections}</td>
      <td>${c.deficit > 0 ? `−${c.deficit}` : '0'}</td>
    </tr>`).join('');
  body.innerHTML = `<table class="tw-table tnum">
    <thead><tr>
      <th scope="col">${IS_AR ? 'المقرر' : 'Course'}</th>
      <th scope="col">${IS_AR ? 'الطلب' : 'Demand'}</th>
      <th scope="col">${IS_AR ? 'المقاعد' : 'Seats'}</th>
      <th scope="col">${IS_AR ? 'الشعب' : 'Sections'}</th>
      <th scope="col">${IS_AR ? 'العجز' : 'Short'}</th>
    </tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr${(t.deficit || 0) > 0 ? ' data-tone="critical"' : ''}>
      <th scope="row">${IS_AR ? 'الإجمالي' : 'Total'}</th>
      <td>${t.demand || 0}</td><td>${t.raw_capacity || 0}</td><td>${t.course_count || 0}</td>
      <td>${(t.deficit || 0) > 0 ? `−${t.deficit}` : '0'}</td>
    </tr></tfoot>
  </table>`;
}

/* ── The board's issues: one model (design record §7) ──
   The cards, the Issues tab and its count, the inspector rail and the board's
   button all read buildBoardIssues(). Severity follows what the server does:
   critical blocks publishing, warning lowers quality, info is context. Info
   never rings a card and is never counted.

   Each source says whether it is known and fresh. The board's clashes are
   known once loaded for this board; the quality check and the publish check
   are fresh when they answered about the current data (dataVersion). A row
   from a stale check is listed as being checked and gives its card no tone; a
   meeting that needs a room and has none is read from the board itself and
   never waits for a check. */
const ISSUE_KINDS = {
  time:            { tone: 'critical', en: 'Time clash',                         ar: 'تعارض زمني' },
  instructor:      { tone: 'critical', en: 'Instructor clash',                   ar: 'تعارض مدرّس' },
  room:            { tone: 'critical', en: 'Room double-booked',                 ar: 'القاعة محجوزة مرتين' },
  noRoom:          { tone: 'critical', en: 'No room',                            ar: 'بلا قاعة' },
  blocked:         { tone: 'critical', en: 'On a blocked time',                  ar: 'في وقت محجوب' },
  server:          { tone: 'critical', en: 'Publish check',                      ar: 'فحص النشر' },
  softOverlap:     { tone: 'warning',  en: 'Overlap students can route round',   ar: 'تداخل يمكن تجاوزه' },
  roomUnset:       { tone: 'warning',  en: 'Room not assigned yet',              ar: 'لم تُحدَّد القاعة بعد' },
  roomSmall:       { tone: 'warning',  en: 'Room too small',                     ar: 'القاعة أصغر من المطلوب' },
  roomUnsuitable:  { tone: 'warning',  en: 'Room unsuitable',                    ar: 'القاعة غير مناسبة' },
  roomElsewhere:   { tone: 'warning',  en: 'Room also booked on another board',  ar: 'القاعة محجوزة في لوحة أخرى' },
  crossBoard:      { tone: 'warning',  en: 'Overlap with another board',         ar: 'تداخل مع لوحة أخرى' },
  offGrid:         { tone: 'warning',  en: 'Outside the grid’s columns',         ar: 'خارج أعمدة الجدول' },
  crossBoardMinor: { tone: 'info',     en: 'Possible overlap with another board', ar: 'تداخل محتمل مع لوحة أخرى' },
  // The Move dialog's verdict only (5e): the board's model has no such row.
  instructorElsewhere: { tone: 'warning', en: 'Teacher also teaching on another board', ar: 'المدرّس يدرّس في لوحة أخرى في الوقت نفسه' },
};
const ISSUE_KIND_ORDER = Object.keys(ISSUE_KINDS);
const TONE_ORDER = ['critical', 'warning', 'info'];
const TONE_LEAD = {
  critical: IS_AR ? 'يمنع النشر' : 'Blocks publishing',
  warning: IS_AR ? 'يُضعف الجودة' : 'Lowers quality',
  info: IS_AR ? 'للاطلاع' : 'For context',
};
const issueKindLabel = kind => (IS_AR ? ISSUE_KINDS[kind].ar : ISSUE_KINDS[kind].en);
const dayName = code => DAY_LABELS[DAYS.indexOf(String(code || '').toUpperCase().slice(0, 3))] || code || '—';
const studentsNeedBoth = n => (IS_AR
  ? pluralAr(n, { zero: 'لا طلاب يحتاجون كليهما', one: 'طالب واحد يحتاج كليهما', two: 'طالبان يحتاجان كليهما', few: '{n} طلاب يحتاجون كليهما', many: '{n} طالباً يحتاجون كليهما', other: '{n} طالب يحتاجون كليهما' })
  : `${n} ${n === 1 ? 'student needs' : 'students need'} both`);
const seatsNeeded = n => (IS_AR
  ? pluralAr(n, { one: 'يحتاج مقعداً واحداً', two: 'يحتاج مقعدين', few: 'يحتاج {n} مقاعد', many: 'يحتاج {n} مقعداً', other: 'يحتاج {n} مقعد' })
  : `needs ${n} ${n === 1 ? 'seat' : 'seats'}`);
/* Escaped text with every time range isolated left to right: inside an
   Arabic line "09:00–10:15" is otherwise painted "10:15–09:00". */
const isolateTimes = text => esc(text).replace(/(\d{1,2}:\d{2})–(\d{1,2}:\d{2})/g, '<bdi dir="ltr">$1–$2</bdi>');

/* The grid's rule (timetable_slot_config.is_on_grid): a teaching day and a
   column start of the meeting's kind — a lab runs more than 80 minutes.
   English meetings have their own table. */
function isPlacementOnGrid(p) {
  if (!DAYS.includes(String(p.day || '').toUpperCase().slice(0, 3))) return false;
  if (globalThis.TimetableMeetingPolicy?.isEnglish(p)) return true;
  const minutes = t => { const [h, m] = String(t || '').split(':').map(Number); return h * 60 + m; };
  const columns = minutes(p.end_time) - minutes(p.start_time) > 80 ? scenarioLabSlots() : scenarioLectureSlots();
  const start = String(p.start_time || '').slice(0, 5);
  return columns.some(slot => String(slot.start || '').slice(0, 5) === start);
}

function computeBoardIssues() {
  const rows = [];
  // Between a board switch and its answer, TW.placements is still the previous
  // board's: nothing read from the placements counts for the new one.
  const placementsKnown = Boolean(TW.boardId) && TW.loadedBoardId === TW.boardId;
  const placements = placementsKnown ? TW.placements : [];
  const byId = new Map(placements.map(p => [String(p.id), p]));
  const conflictsKnown = boardConflictsKnown();
  const conflicts = boardConflicts();
  const quality = TW.quality;
  const qualityFresh = Boolean(quality) && TW.qualityVersion === TW.dataVersion;
  const readinessFresh = Boolean(TW.readiness) && TW.readinessVersion === TW.dataVersion;
  const name = p => `${p.course_code} ${p.section}`;
  const when = p => `${dayName(p.day)} ${p.start_time}–${p.end_time}`;
  const placementsOf = ids => (ids || []).map(id => byId.get(String(id))).filter(Boolean);
  const add = (kind, ids, subject, detail = '', extra = {}) => rows.push({
    kind, tone: ISSUE_KINDS[kind].tone, ids: (ids || []).map(String), subject, detail, pending: false, ...extra,
  });

  // The board's clashes.
  const roomClashIds = new Set();
  if (conflictsKnown) {
    (conflicts.overlaps || []).forEach(o => {
      const ps = placementsOf(o.ids);
      const subject = (o.sections && o.sections.length ? o.sections : ps.map(name)).join(' / ');
      const detail = [ps[0] ? when(ps[0]) : '', o.shared_students ? studentsNeedBoth(o.shared_students) : ''].filter(Boolean).join(' · ');
      add(isWarningOverlap(o) ? 'softOverlap' : 'time', o.ids, subject, detail);
    });
    (conflicts.instructor_clashes || []).forEach(c => {
      const ps = placementsOf(c.ids);
      add('instructor', c.ids, [c.instructor, (c.sections || ps.map(name)).join(' / ')].filter(Boolean).join(' — '), ps.map(when).join(' · '));
    });
    (conflicts.room_clashes || []).forEach(c => {
      const ps = placementsOf(c.ids);
      (c.ids || []).forEach(id => roomClashIds.add(String(id)));
      add('room', c.ids, [c.room, (c.sections || ps.map(name)).join(' / ')].filter(Boolean).join(' — '), ps.map(when).join(' · '));
    });
    // A pair at the publish check's line is a warning there; below it, context.
    const line = Number.isFinite(conflicts.hard_overlap_threshold) ? conflicts.hard_overlap_threshold : Infinity;
    activeBoardCrossBoardClashes().forEach(c => {
      const ids = c.ids || [c.placement_a_id, c.placement_b_id].filter(Boolean);
      const other = String(c.board_a_id) === String(TW.boardId) ? c.board_b_id : c.board_a_id;
      add((c.overlap_count || 0) >= line ? 'crossBoard' : 'crossBoardMinor', ids,
        `${c.section_a} ↔ ${c.section_b}`,
        [studentsNeedBoth(c.overlap_count || 0), `${c.board_a_label || ''} ↔ ${c.board_b_label || ''}`].join(' · '),
        { boardTarget: other });
    });
  }

  // The board itself: rooms, blocked times, the grid's columns.
  const blocked = new Set((TW.scenario?.blocked_slots || []).map(b => `${b.day}|${b.start}`));
  placements.forEach(p => {
    if (p.needs_room && p.room_state === 'unassigned') add('noRoom', [p.id], name(p), when(p));
    else if (p.needs_room && p.room_state === 'unset') add('roomUnset', [p.id], name(p), when(p));
    if (blocked.has(`${p.day}|${p.start_time}`)) add('blocked', [p.id], name(p), when(p));
    // Not drawn until the grid gets its parking strip: the row cannot point at it.
    if (!isPlacementOnGrid(p)) add('offGrid', [p.id], name(p), when(p), { locate: false });
  });

  // The quality check: room suitability. "missing" is read from the board above.
  if (quality && placementsKnown) {
    activeBoardRoomShortages().forEach(detail => {
      const ids = (detail.placement_ids || []).map(String).filter(id => byId.has(id));
      const reasons = new Set(detail.reasons || []);
      const subject = [detail.course_code || detail.course_key || '', (detail.rooms || []).join(', ')].filter(Boolean).join(' · ');
      const needs = detail.required_capacity != null ? seatsNeeded(detail.required_capacity) : '';
      const pending = !qualityFresh;
      if (reasons.has('undersized')) add('roomSmall', ids, subject, needs, { pending });
      if (reasons.has('incompatible')) add('roomUnsuitable', ids, subject, '', { pending });
      if (conflictsKnown && reasons.has('clash') && !ids.some(id => roomClashIds.has(id))) add('roomElsewhere', ids, subject, '', { pending });
    });
  }

  // The publish check: what only the server knows (sections placed nowhere,
  // section patterns). What the board shows itself is not repeated.
  if (TW.readiness) {
    boardReadinessBlockers(TW.boardId).forEach(item => {
      // The board's own placements say these once loaded; until then the
      // publish check's items stand in for them.
      if (placementsKnown && (item.code === 'board_sections_without_rooms' || item.code === 'board_blocked_slot_placements')) return;
      if (item.code === 'board_room_conflicts' && conflictsKnown) return;
      // A room or blocked-time item stands for each meeting it counts, as on
      // the other boards' buttons.
      const weight = COUNTED_READINESS_CODES.has(item.code) ? (item.count || 1) : 1;
      add('server', [], readinessItemText(item), '', { pending: !readinessFresh, item, weight });
    });
  }

  rows.sort((a, b) => TONE_ORDER.indexOf(a.tone) - TONE_ORDER.indexOf(b.tone)
    || ISSUE_KIND_ORDER.indexOf(a.kind) - ISSUE_KIND_ORDER.indexOf(b.kind));

  // What tones each card: critical and warning rows from known, fresh sources.
  const byPlacement = new Map();
  rows.forEach(row => {
    if (row.tone === 'info' || row.pending) return;
    row.ids.forEach(id => {
      const entry = byPlacement.get(id) || { tone: row.tone, kinds: [] };
      if (row.tone === 'critical') entry.tone = 'critical';
      if (!entry.kinds.includes(row.kind)) entry.kinds.push(row.kind);
      byPlacement.set(id, entry);
    });
  });
  const counts = { critical: 0, warning: 0, info: 0 };
  rows.forEach(row => { counts[row.tone] += row.weight || 1; });
  return {
    rows, byPlacement, counts,
    known: { conflicts: conflictsKnown, placements: placementsKnown, quality: Boolean(quality), readiness: Boolean(TW.readiness) },
    fresh: { quality: qualityFresh, readiness: readinessFresh },
  };
}

/* One model per state of its sources: every redraw (strip, tab, cards,
   buttons) reads the same answer without rebuilding it. */
let issueMemo = null;
function buildBoardIssues() {
  const key = [TW.boardId, TW.loadedBoardId, TW.conflicts, TW.conflictsBoardId, TW.quality, TW.qualityVersion, TW.readiness,
    TW.readinessVersion, TW.dataVersion, TW.placements, TW.scenario, TW.qualityLoading, TW.readinessLoading];
  if (issueMemo && issueMemo.key.every((value, i) => value === key[i])) return issueMemo.model;
  const model = computeBoardIssues();
  issueMemo = { key, model };
  return model;
}

/* The count says how much blocks publishing, or else how much lowers quality;
   context is never counted. */
function issueCountOf(model) {
  return model.counts.critical || model.counts.warning;
}

/* Tone every card from the model, in place: a check that answers later never
   rebuilds the grid (a drag or a focused card survives it). The filter's view
   follows (applyGridView: titles, dims and the folds' tone counts). */
function applyCardTones() {
  const { byPlacement } = buildBoardIssues();
  document.querySelectorAll('#twGrid .tw-card[data-placement-id]').forEach(card => {
    const entry = byPlacement.get(card.dataset.placementId);
    card.classList.toggle('critical', entry?.tone === 'critical');
    card.classList.toggle('warning', entry?.tone === 'warning');
    const reason = entry ? `${TONE_LEAD[entry.tone]}: ${entry.kinds.map(issueKindLabel).join(', ')}` : '';
    const mark = card.querySelector('.tw-card-tone');
    if (mark) {
      mark.hidden = !entry;
      if (entry) {
        mark.dataset.tone = entry.tone;
        mark.setAttribute('aria-label', reason);
      } else {
        delete mark.dataset.tone;
        mark.removeAttribute('aria-label');
      }
    }
    // The title leads with it (applyGridView writes the title).
    card._twToneReason = reason;
  });
  applyGridView(byPlacement);
}

/* The counts and the card tones follow the data, whichever inspector tab is
   open: a card click opens Selection, and the rail of a collapsed inspector
   exists to say how much is wrong. */
function renderBoardIssueViews() {
  const model = buildBoardIssues();
  setIssuesCount(issueCountOf(model), model.counts.critical);
  applyCardTones();
}

/* The Issues tab's count, and the inspector rail's, so a collapsed inspector
   still says how much is wrong. */
function setIssuesCount(total, critical) {
  const text = total > 0 ? total.toLocaleString() : '';
  const badge = $('twIssuesCount');
  if (badge) {
    badge.textContent = text;
    if (critical > 0) badge.dataset.tone = 'critical';
    else if (total > 0) badge.dataset.tone = 'warning';
    else delete badge.dataset.tone;
  }
  if ($('twRightRailCount')) $('twRightRailCount').textContent = text;
}

/* ── Slot Edit Modal ── */
/* The editor opens on the effective columns, or on the text the user had
   typed when a save stopped (a decline, a line it could not read, a column
   the server refused): their edits are never lost. */
async function openSlotEditor(typed = null) {
  if (!TW.scenarioId || !TW.scenario) { notify.error(IS_AR ? 'اختر سيناريو أولا' : 'Select a scenario first'); return; }
  if (TW.scenario.status === 'published') { notify.error(IS_AR ? 'السيناريو منشور' : 'Cannot edit published scenario'); return; }
  const SC = window.TimetableSlotConfig;
  const lectureSlots = scenarioLectureSlots();
  const labSlots = scenarioLabSlots();
  const flagged = SC.flaggedColumnsText(labSlots) || SC.flaggedColumnsText(lectureSlots);
  const lectureText = typed ? typed.lecture : SC.rowsToText(lectureSlots);
  const labText = typed ? typed.lab : SC.rowsToText(labSlots);

  const result = await dlg.confirm({
    title: IS_AR ? 'تعديل الفترات الزمنية' : 'Edit Time Slots',
    body: `<div class="fs-md text-t3 fw-semibold" style="margin-bottom:6px">${IS_AR ? '📚 المحاضرات (75 دقيقة)' : '📚 Lecture Slots (75 min)'}</div>
      <div class="fs-sm text-t4" style="margin-bottom:4px">${IS_AR ? 'الاسم [تبويب] البداية [تبويب] النهاية' : 'Label [tab] Start [tab] End'}</div>
      <textarea class="w-100 font-mono fs-md" id="twSlotInput" rows="6" dir="ltr" style="padding:8px; border:1px solid var(--line); border-radius:var(--radius-sm); background:var(--bg); color:var(--ink); resize:vertical">${esc(lectureText)}</textarea>
      <div class="fs-md text-teal fw-semibold" style="margin:10px 0 6px">${IS_AR ? '🔬 المعامل (100 دقيقة)' : '🔬 Lab Slots (100 min)'}</div>
      <textarea class="w-100 font-mono fs-md" id="twLabSlotInput" rows="6" dir="ltr" style="padding:8px; border:1px solid var(--line); border-radius:var(--radius-sm); background:var(--bg); color:var(--ink); resize:vertical">${esc(labText)}</textarea>
      ${flagged ? `<p class="fs-sm text-t3" style="margin:8px 0 0">${esc(flagged)}</p>` : ''}`,
    confirmText: IS_AR ? 'حفظ' : 'Save Slots',
    kind: 'info',
  });
  if (!result) return;

  const text = {
    lecture: document.getElementById('twSlotInput')?.value ?? lectureText,
    lab: document.getElementById('twLabSlotInput')?.value ?? labText,
  };
  const lectures = SC.parseLines(text.lecture);
  const labs = SC.parseLines(text.lab);
  if (lectures.rejected.length || labs.rejected.length) {
    notify.error(SC.rejectedText(lectures.rejected, labs.rejected));
    return openSlotEditor(text);
  }
  if (lectures.rows.length === 0) { notify.error(IS_AR ? 'لم يتم قراءة فترات' : 'No valid slots parsed'); return openSlotEditor(text); }
  const scenarioId = TW.scenarioId;

  const data = await SC.save({
    send: async extra => {
      let error = null;
      const answer = await twFetch(`/ops/tw/scenarios/${scenarioId}/slots/update/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ slot_config: lectures.rows, lab_slot_config: labs.rows, ...extra }),
        _onError: body => {
          const code = body?.error?.code;
          if (code !== 'SLOT_EDIT_NEEDS_CONFIRMATION' && code !== 'VALIDATION_SLOTS') return false;
          error = body.error;
          return true;
        },
      });
      return { data: answer, error };
    },
    confirm: async details => {
      const ok = await dlg.confirm({
        title: IS_AR ? 'تأكيد تغيير الأعمدة' : 'Confirm the column change',
        body: SC.confirmationHtml(details),
        kind: 'danger',
        confirmText: IS_AR ? 'حفظ على أي حال' : 'Save anyway',
      });
      return ok;
    },
    report: message => notify.error(message),
  });
  if (TW.scenarioId !== scenarioId) return;
  // Nothing saved — declined, refused, busy or offline: the typed text comes
  // back, so it can be adjusted or tried again.
  if (!data) return openSlotEditor(text);

  TW.scenario = data.scenario;
  notify.success(IS_AR ? 'تم تحديث الفترات' : `${lectures.rows.length} lecture + ${labs.rows.length} lab slots saved`);
  renderGrid();
}

/* ── Elective Mapping Dialog ── */
$('twMapElectives').addEventListener('click', async () => {
  // The open scenario's term when there is one: the scope fields can have been
  // moved on to prepare another term.
  const open = (typeof TW !== 'undefined' && TW && TW.scenario) || {};
  const year = String(open.academic_year || $('twYear').value).trim();
  const term = String(open.term || $('twTerm').value).trim();
  if (!year || !term) {
    notify.error(IS_AR ? 'أدخل السنة والفصل أولا' : 'Enter the year and term first');
    return;
  }
  const programme = await electiveMappingProgramme();
  if (!programme) return;

  // Load catalogue + current mappings + actual placeholders in parallel
  let catalogue = [], currentMappings = [], placeholderCodes = [];
  try {
    const [catData, mapData, phData] = await Promise.all([
      twFetch(`/ops/electives/catalogue/?programme=${encodeURIComponent(programme)}`),
      twFetch(`/ops/electives/mapping/?academic_year=${encodeURIComponent(year)}&term=${encodeURIComponent(term)}&programme=${encodeURIComponent(programme)}`),
      twFetch(`/ops/electives/placeholders/?programme=${encodeURIComponent(programme)}`),
    ]);
    if (!catData || !mapData || !phData) {
      throw new Error(IS_AR ? 'تعذّر تحميل الربط. أعد المحاولة.' : 'Mappings could not be loaded. Try again.');
    }
    catalogue = (catData && catData.items) || [];
    currentMappings = (mapData && mapData.items) || [];
    placeholderCodes = (phData && phData.items) || [];
  } catch (e) {
    notify.error(e.message);
    return;
  }

  const represented = new Set();
  const complete = currentMappings.every(mapping => {
    const pair = JSON.stringify([mapping.placeholder_code, mapping.course_code]);
    const visible = placeholderCodes.some(slot => slot.course_code === mapping.placeholder_code)
      && catalogue.some(course => course.course_code === mapping.course_code && course.id === mapping.elective_id);
    if (!visible || represented.has(pair)) return false;
    represented.add(pair);
    return true;
  });
  if (!complete) {
    notify.error(IS_AR
      ? 'بعض الروابط المحفوظة لا يمكن عرضها. أصلح ملكية المقررات أو متطلبات الخطة أو الروابط المكررة قبل تعديل هذا النطاق.'
      : 'Some saved mappings cannot be displayed. Repair catalogue ownership, plan placeholders, or duplicate mappings before editing this scope.');
    return;
  }

  if (!catalogue.length) {
    notify.error(IS_AR ? 'لا يوجد مقررات اختيارية — استيردها من إدارة قواعد البيانات أولا' : 'No elective catalogue found — import from DB Admin first');
    return;
  }

  if (!placeholderCodes.length) {
    notify.error(IS_AR ? 'لا يوجد مقررات اختيارية في الخطة' : 'No elective placeholders found in programme plan');
    return;
  }

  const relevantPlaceholders = placeholderCodes.map(p => p.course_code);

  // Build current mapping lookup: placeholder → [course_codes]
  const mapLookup = {};
  currentMappings.forEach(m => {
    if (!mapLookup[m.placeholder_code]) mapLookup[m.placeholder_code] = [];
    mapLookup[m.placeholder_code].push(m.course_code);
  });

  // Build dialog body
  let body = `<div class="fs-md text-t3 mb-2">${IS_AR ? 'اختر المقررات لكل فتحة اختيارية' : 'Select courses for each elective slot'}</div>`;
  body += `<div style="max-height:400px;overflow-y:auto">`;

  // Build placeholder info lookup
  const phInfo = {};
  placeholderCodes.forEach(p => { phInfo[p.course_code] = p; });

  relevantPlaceholders.forEach(ph => {
    const info = phInfo[ph] || {};
    const selected = mapLookup[ph] || [];
    body += `<div class="u-border" style="margin-bottom:12px; padding:8px; border-radius:var(--radius-sm)">`;
    body += `<div class="fw-bold" style="color:var(--royal); margin-bottom:4px">${ph} <span class="fs-sm text-t4" style="font-weight:400">Term ${info.programme_term || '?'} · ${info.type || 'Elective'} · ${info.credit_hours || 3}cr</span></div>`;
    catalogue.forEach(c => {
      const checked = selected.includes(c.course_code) ? 'checked' : '';
      body += `<label class="d-flex align-items-center fs-md u-cursor-pointer" style="gap:6px; padding:2px 0">`;
      body += `<input type="checkbox" class="elec-chk" data-ph="${ph}" data-code="${c.course_code}" ${checked}>`;
      body += `<span class="font-mono fw-semibold">${c.course_code}</span>`;
      body += `<span class="text-t3">${c.course_name}</span>`;
      body += `<span class="text-t4 fs-xs" style="margin-inline-start:auto">${c.prerequisites_csv || 'no prereq'}</span>`;
      body += `</label>`;
    });
    body += `</div>`;
  });
  body += `</div>`;

  const result = await dlg.confirm({
    title: IS_AR ? `ربط الاختيارية — ${programme} ${year}/${term}` : `Map Electives — ${programme} ${year}/${term}`,
    body,
    confirmText: IS_AR ? 'حفظ' : 'Save Mapping',
    kind: 'info',
  });

  if (!result) return;

  // Collect checked items
  const mappings = [];
  document.querySelectorAll('.elec-chk:checked').forEach(chk => {
    mappings.push({
      placeholder_code: chk.dataset.ph,
      course_code: chk.dataset.code,
    });
  });

  const data = await twFetch('/ops/electives/mapping/set/', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({academic_year: year, term: parseInt(term), programme, mappings}),
  });
  if (data && data.ok) {
    notify.success(IS_AR ? `تم حفظ ${data.total} ربط` : `${data.total} mappings saved`);
  }
});

/* A mapping belongs to one programme in one term: the open scenario's
   programme when it has one; otherwise the user names it. */
async function electiveMappingProgramme() {
  const scenarioProgrammes = ((TW && TW.scenario && TW.scenario.programs) || [])
    .map(code => String(code).trim().toUpperCase()).filter(Boolean);
  if (scenarioProgrammes.length === 1) return scenarioProgrammes[0];
  const known = scenarioProgrammes.length ? scenarioProgrammes
    : [...document.querySelectorAll('input[name="twGenProgramme"]')].map(box => box.value.trim().toUpperCase()).filter(Boolean);
  const typed = await dlg.confirm({
    title: IS_AR ? 'ربط المقررات الاختيارية' : 'Elective mapping',
    inputLabel: IS_AR ? 'لأي برنامج؟' : 'For which programme?',
    inputPlaceholder: known.join(', '),
    confirmText: IS_AR ? 'متابعة' : 'Continue',
  });
  if (!typed || typed === true) return '';
  return String(typed).trim().toUpperCase().split(',')[0].trim();
}

/* ── V2 Optimiser ── */
async function runOptimiseV2(mode = 'current') {
  if (!TW.scenarioId) { notify.error(IS_AR ? 'اختر سيناريو' : 'No scenario selected'); return; }
  if (!workspaceCanEdit()) { notify.warning(IS_AR ? 'السيناريو غير متاح للتعديل حاليا.' : 'This scenario cannot be edited right now.'); return; }
  const token = beginWorkspaceOperation('optimise');
  if (!token) return;

  // Both modes now poll for minutes, during which the registrar can switch
  // scenario. Capture the target up front and refuse to apply results to a
  // different one — the results dialog and refresh below both read TW.scenarioId,
  // so without this they would describe scenario A while showing scenario B.
  const scenarioAtStart = TW.scenarioId;
  const btn = $('twOptimise');
  const menuBtn = $('twOptimiseMenu');
  const label = $('twOptimiseLabel') || btn;
  btn.disabled = true;
  menuBtn.disabled = true;
  const origLabel = label.textContent;
  const modeLabel = mode === 'full' ? (IS_AR ? 'إعادة بناء…' : 'Rebuilding…') : (IS_AR ? 'جارٍ التحسين…' : 'Optimising…');
  btn.classList.add('is-working');
  label.textContent = modeLabel;

  // Build request payload based on mode
  const payload = { mode };
  if (mode === 'full') {
    payload.strategies = ['compact', 'morning', 'balanced', 'load_balanced', 'optimal', 'hybrid', 'adaptive'];
  }
  payload.run_local_search = true;
  payload.max_iterations = 50;
  payload.run_chain_search = true;
  payload.run_cpsat_polish = true;
  payload.cpsat_time_limit = 60;

  try {
    const data = await runOptimiseRequest(mode, payload, label, modeLabel, token);

    if (TW.scenarioId !== scenarioAtStart) {
      notify.warning(IS_AR
        ? 'تغيّر السيناريو أثناء التحسين؛ لم يتم عرض النتائج.'
        : 'Scenario changed while optimising — results not applied to the current view.');
      return;
    }

    if (!data || !data.ok) {
      if (data?.error?.code === 'STATUS_UNCONFIRMED') {
        notify.warning(IS_AR ? 'حالة التحسين غير مؤكدة؛ تستمر متابعة المخطط.' : 'Optimisation status unconfirmed; the planner card continues monitoring.');
        return;
      }
      // Surface the real error code + message from the backend envelope
      // instead of the generic "failed" so the registrar can tell which
      // failure class they hit. twFetch() already toasts on HTTP non-2xx
      // and returns null in that case, so we only raise an extra toast
      // when data is present (2xx with {ok:false}). Persist the last 20
      // errors in localStorage under 'tw.optimiseErrors' so the context
      // outlives the toast and can be copied into a bug report.
      const err = (data && data.error) || {};
      const code = err.code || (data ? 'UNKNOWN' : 'HTTP_ERROR');
      const msg = err.message || (IS_AR ? 'فشل التحسين' : 'Optimisation failed');
      const details = err.details || null;
      if (data) {
        const label = IS_AR ? 'فشل التحسين' : 'Optimisation failed';
        notify.error(`${label}: ${code} — ${msg}`);
      }
      console.error('[runOptimiseV2]', { code, message: msg, details, scenarioId: TW.scenarioId, mode });
      try {
        const key = 'tw.optimiseErrors';
        const history = JSON.parse(localStorage.getItem(key) || '[]');
        history.unshift({
          at: new Date().toISOString(),
          scenarioId: TW.scenarioId,
          mode,
          code,
          message: msg,
          details,
        });
        localStorage.setItem(key, JSON.stringify(history.slice(0, 20)));
      } catch (_) { /* localStorage full / blocked — ignore */ }
      return;
    }

    const o = data.optimisation;
    const score = o.final_score;
    const assigned = o.total_students - o.unresolved_students;

    // Layout-aware score decode — mirrors timetable_student_assignment.decode_score.
    // Legacy 6/7-tuple keeps today's meaning; the tiered 9-tuple ranks by course
    // tier. `unresolved`/`clashes`/`gap` are the cross-layout fields the summary
    // rows and before/after deltas read, so both layouts render correct numbers.
    // For the tiered layout, position 4 is the BLENDED student-cost
    // (real_gap + budget*soft); recover the pure gap using the server-sent budget.
    const softBudget = Number(o.tiered_soft_gap_budget) || 0;
    const decodeScore = (sc) => {
      const s = Array.isArray(sc) ? sc : [];
      if (s.length === 9) {
        return {
          tiered: true,
          highrisk: s[0], clashes: s[1], t1: s[2], t2Over: s[3],
          cost: s[4], gap: (s[4] || 0) - softBudget * (s[5] || 0),
          soft: s[5], reserve: s[6], spread: s[7], instrIdle: s[8],
          unresolved: (s[2] || 0) + (s[3] || 0) + (s[5] || 0),
        };
      }
      return {
        tiered: false,
        highrisk: s[0] || 0, unresolved: s[1] || 0, unassigned: s[2] || 0,
        clashes: s[3] || 0, gap: s[4] || 0, reserve: s[5] || 0,
      };
    };

    // Build results dialog
    let body = `<div style="font-size:.82rem;line-height:1.6">`;
    body += renderRunSeed(o) + renderInstructorOutcome(o) + renderSecondaryRecoveryOutcome(o);
    if (o.safety_blocked) {
      body += `<div style="padding:10px;margin-bottom:10px;color:var(--danger)">${IS_AR ? 'لم تُطبق النتيجة: أعيد الجدول لحماية التسجيل والقيود وأيام المدرسين.' : 'Result not applied: the board was restored to protect enrolment, hard constraints and instructor days.'}</div>`;
    }

    // Summary cards
    body += `<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">`;
    body += `<div class="text-center" style="padding:10px; border-radius:var(--radius-sm); background:rgba(10,142,110,.08)">`;
    body += `<div class="text-t4 text-caps" style="font-size:.68rem">${IS_AR ? 'تم تعيينهم' : 'Assigned'}</div>`;
    body += `<div class="fw-bold text-teal" style="font-size:1.3rem">${assigned}<span class="fs-12 text-t3" style="font-weight:400">/${o.total_students}</span></div></div>`;
    body += `<div class="text-center" style="padding:10px; border-radius:var(--radius-sm); background:rgba(217,119,6,.08)">`;
    body += `<div class="text-t4 text-caps" style="font-size:.68rem">${IS_AR ? 'غير محلول' : 'Unresolved'}</div>`;
    body += `<div class="fw-bold" style="font-size:1.3rem; color:var(--amber)">${o.unresolved_students}</div></div>`;
    body += `<div class="text-center" style="padding:10px; border-radius:var(--radius-sm); background:rgba(99,102,241,.08)">`;
    body += `<div class="text-t4 text-caps" style="font-size:.68rem">${IS_AR ? 'الوقت' : 'Time'}</div>`;
    body += `<div class="fw-bold" style="font-size:1.3rem; color:var(--royal)">${o.elapsed_seconds || '?'}s</div></div>`;
    body += `</div>`;

    // Candidate comparison table
    if (o.all_scores && o.all_scores.length > 1) {
      body += `<div style="margin-bottom:10px"><span class="fs-sm fw-bold text-caps" style="color:var(--royal)">${IS_AR ? 'مقارنة المرشحين' : 'Candidate Comparison'} (${o.candidates_evaluated} strategies)</span>`;
      body += `<table class="w-100 fs-sm" style="border-collapse:collapse; margin-top:4px">`;
      body += `<tr class="text-t4" style="border-bottom:1px solid var(--line)"><th style="text-align:start;padding:3px 4px">Strategy</th><th class="text-end" style="padding:3px 4px">Tier-A</th><th class="text-end" style="padding:3px 4px">Unresolved</th><th class="text-end" style="padding:3px 4px">Clashes</th><th class="text-end" style="padding:3px 4px">Gaps</th><th class="text-end" style="padding:3px 4px">Quality</th></tr>`;
      o.all_scores.forEach(s => {
        const d = decodeScore(s.score);
        const isBest = s.id === o.best_candidate_id;
        const style = isBest ? 'background:rgba(10,142,110,.06);font-weight:600' : '';
        body += `<tr style="border-bottom:1px solid var(--line);${style}">`;
        body += `<td style="padding:3px 4px">${s.id.replace(/_\d+$/, '')}${isBest ? ' ★' : ''}</td>`;
        body += `<td class="font-mono" style="text-align:end; padding:3px 4px">${d.highrisk}</td>`;
        body += `<td class="font-mono" style="text-align:end; padding:3px 4px">${d.unresolved}</td>`;
        body += `<td class="font-mono" style="text-align:end; padding:3px 4px; color:${d.clashes>0?'var(--danger)':'var(--teal)'}">${d.clashes}</td>`;
        body += `<td class="font-mono" style="text-align:end; padding:3px 4px">${d.gap.toLocaleString()}</td>`;
        body += `<td class="font-mono" style="text-align:end; padding:3px 4px">${Number(s.quality_penalty || 0).toLocaleString()}</td>`;
        body += `</tr>`;
      });
      body += `</table></div>`;
    }

    // Final score breakdown
    const scoreLabel = o.mode === 'current'
      ? (IS_AR ? 'النتيجة بعد التحسين' : 'Score After Optimisation')
      : `${IS_AR ? 'النتيجة النهائية' : 'Final Score'} (${o.best_candidate_id.replace(/_\d+$/, '')}${o.local_search_applied ? ' + local search' : ''})`;
    body += `<div style="margin-bottom:10px"><span class="fs-sm fw-bold text-caps text-teal">${scoreLabel}</span>`;
    body += `<table class="w-100 fs-md" style="border-collapse:collapse; margin-top:4px">`;
    if (score && score.length === 9) {
      // Tiered objective breakdown (course-tier-aware).
      const trow = (label, val, danger) => {
        const clr = danger && val > 0 ? 'color:var(--danger)' : (danger ? 'color:var(--teal)' : '');
        body += `<tr style="border-bottom:1px solid var(--line)"><td class="text-t3" style="padding:3px 0">${label}</td><td class="fw-semibold font-mono" style="text-align:end; ${clr}">${val.toLocaleString()}</td></tr>`;
      };
      trow(IS_AR ? 'طلاب الخطر (Tier-1/2)' : 'High-risk unresolved (T1/T2)', score[0], true);
      trow(IS_AR ? 'تعارضات زمنية' : 'Time clashes', score[1], true);
      trow(IS_AR ? 'مقررات أساسية غير معينة (Tier-1)' : 'Tier-1 unassigned (core)', score[2], true);
      trow(IS_AR ? 'Tier-2 فوق الحد المسموح' : 'Tier-2 over tolerance', score[3], true);
      trow(IS_AR ? 'تكلفة الطالب (فراغ + عام)' : 'Student cost (gap + gen-ed)', score[4], false);
      trow(IS_AR ? 'غير محلول (اختياري/ضمن الحد)' : 'Soft unresolved (T3 / within-tol)', score[5], false);
      trow(IS_AR ? 'احتياط مستخدم' : 'Reserve used', score[6], false);
      trow(IS_AR ? 'تشتّت أقسام المقرر' : 'Same-course spread', score[7], false);
    } else {
      const labels = [
        IS_AR ? 'طلاب الخطر A' : 'Tier-A unresolved',
        IS_AR ? 'طلاب غير محلولين' : 'Unresolved students',
        IS_AR ? 'مقررات غير معينة' : 'Unassigned courses',
        IS_AR ? 'تعارضات زمنية' : 'Time clashes',
        IS_AR ? 'دقائق فراغ' : 'Gap minutes',
        IS_AR ? 'احتياط مستخدم' : 'Reserve used',
      ];
      for (let i = 0; i < 6; i++) {
        const val = i === 4 ? score[i].toLocaleString() : score[i];
        const clr = (i === 3 && score[i] > 0) ? 'color:var(--danger)' : (i === 3 ? 'color:var(--teal)' : '');
        body += `<tr style="border-bottom:1px solid var(--line)"><td class="text-t3" style="padding:3px 0">${labels[i]}</td><td class="fw-semibold font-mono" style="text-align:end; ${clr}">${val}</td></tr>`;
      }
    }
    body += `</table></div>`;

    // Before → After summary (current mode only)
    const quality = o.quality_score || {};
    if (quality.penalty != null) {
      const rows = Object.entries(quality.components || {})
        .filter(([, value]) => Number(value || 0) > 0)
        .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
        .map(([name, value]) => `<span style="padding:2px 8px;border-radius:999px;background:rgba(80,104,240,.08);color:var(--royal);font-size:11px">${name.replace(/_/g, ' ')}: ${Number(value || 0).toLocaleString()}</span>`)
        .join('');
      body += `<div style="margin-bottom:10px"><span class="fs-sm fw-bold text-caps" style="color:var(--royal)">${IS_AR ? 'جودة الجدول' : 'Timetable quality'}</span>`;
      body += `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px"><span style="padding:2px 8px;border-radius:999px;background:rgba(10,142,110,.08);color:var(--teal);font-size:11px">Penalty: ${Number(quality.penalty || 0).toLocaleString()}</span>${rows}</div></div>`;
    }

    if (o.mode === 'current' && o.baseline_score) {
      const bs = decodeScore(o.baseline_score);
      const fs = decodeScore(o.final_score);
      const gotBetter = !o.safety_blocked && (instructorScheduleImproved(o) || secondaryRecoveryImproved(o) || fs.unresolved < bs.unresolved || fs.clashes < bs.clashes || fs.gap < bs.gap);
      if (gotBetter) {
        body += `<div class="fs-md mb-2" style="padding:8px 10px; border-radius:var(--radius-sm); background:rgba(10,142,110,.1); border:1px solid rgba(10,142,110,.2)">`;
        body += `<div class="fw-bold text-teal" style="margin-bottom:4px">&#x2714; ${IS_AR ? 'تم التحسين' : 'Board Improved'}</div>`;
        body += `<div>Unresolved: <b>${bs.unresolved}</b> &rarr; <b class="text-teal">${fs.unresolved}</b></div>`;
        if (bs.clashes !== fs.clashes) body += `<div>Clashes: <b>${bs.clashes}</b> &rarr; <b class="text-teal">${fs.clashes}</b></div>`;
        body += `<div>Gap minutes: <b>${bs.gap.toLocaleString()}</b> &rarr; <b>${fs.gap.toLocaleString()}</b></div>`;
        body += `</div>`;
      } else {
        body += `<div class="fs-md mb-2" style="padding:8px 10px; border-radius:var(--radius-sm); background:rgba(99,102,241,.06)">`;
        body += `<span class="fw-semibold" style="color:var(--royal)">&#x2501; ${IS_AR ? 'لم يتم العثور على تحسين تلقائي' : 'No automatic improvement was found'}</span>`;
        body += `</div>`;
      }
    }

    // Local search improvement indicator
    if (o.local_search_applied && o.score_before_local_search) {
      const before = decodeScore(o.score_before_local_search);
      const after = decodeScore(o.final_score);
      const improved = before.unresolved !== after.unresolved || before.gap !== after.gap;
      if (improved) {
        body += `<div class="fs-12 mb-2" style="padding:6px 10px; border-radius:var(--radius-sm); background:rgba(10,142,110,.08)">`;
        body += `<span class="text-teal fw-semibold">${IS_AR ? 'مؤشرات الطلاب أثناء التحسين' : 'Student metrics during optimisation'}:</span> `;
        body += `unresolved ${before.unresolved} → ${after.unresolved}, gaps ${before.gap.toLocaleString()} → ${after.gap.toLocaleString()}`;
        body += `</div>`;
      } else {
        body += `<div class="fs-12 mb-2" style="padding:6px 10px; border-radius:var(--radius-sm); background:rgba(99,102,241,.06)">`;
        body += `<span class="text-royal">&#x2501; ${IS_AR ? 'مؤشرات الطلاب لم تتغير أثناء التحسين' : 'Student metrics unchanged during optimisation'}</span>`;
        body += `</div>`;
      }
    }

    // Courses that left students without a section (shared with split view).
    body += window.TimetableHotspots?.html(o, IS_AR) || '';

    // Reserve-heavy sections
    if (o.reserve_heavy_sections && o.reserve_heavy_sections.length > 0) {
      body += `<div><span class="fs-sm fw-bold text-caps" style="color:var(--amber)">${IS_AR ? 'ضغط احتياطي' : 'Reserve Pressure'}</span>`;
      body += `<div class="d-flex flex-wrap" style="gap:4px; margin-top:4px">`;
      o.reserve_heavy_sections.slice(0, 8).forEach(s => {
        body += `<span class="fs-sm fw-semibold" style="padding:2px 8px; border-radius:999px; background:rgba(217,119,6,.1); color:var(--amber)">${s.section} ${Math.round(s.ratio*100)}%</span>`;
      });
      body += `</div></div>`;
    }

    body += `</div>`;

    const dlgTitle = mode === 'full'
      ? (IS_AR ? 'نتائج إعادة البناء' : 'Full Rebuild Results')
      : (IS_AR ? 'نتائج التحسين' : 'Optimise Current Results');

    await dlg.confirm({
      title: dlgTitle,
      body,
      confirmText: IS_AR ? 'إغلاق' : 'Close',
      kind: 'info',
    });

    // The grid refresh now happens in `finally` so it also covers the failure
    // and rollback paths, which mutate placement ids just the same.
    const modeMsg = mode === 'full'
      ? `Rebuilt: ${o.best_candidate_id.replace(/_\d+$/, '')} won`
      : 'Optimised current board';
    if (o.safety_blocked) notify.warning(IS_AR ? 'لم تُطبق النتيجة' : 'Result not applied');
    else notify.success(IS_AR ? 'اكتمل التحسين' : `${modeMsg} (${o.elapsed_seconds}s)`);

  } catch (e) {
    notify.error(e.message || String(e));
  } finally {
    label.textContent = origLabel;
    btn.classList.remove('is-working');

    // Refresh unconditionally, not just on the success path. An optimise run
    // persists BEFORE its regression gate decides whether to keep the result, so
    // a failed or rolled-back run can still have changed placement rows — and a
    // rollback restores different primary keys than the ones now in the DOM.
    // Leaving the stale grid up means the next drag targets a placement id that
    // no longer exists. The undo/redo stacks hold those same dead ids, so they
    // are cleared too: an undo across an optimise boundary was never coherent.
    if (TW.scenarioId === scenarioAtStart && !ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status)) {
      TW.undoStack = [];
      TW.redoStack = [];
      updateUndoRedoButtons();
      try {
        await onScenarioChange();
      } catch (_) { /* refresh is best-effort */ }
    }

    // Re-enable LAST. onScenarioChange is several seconds of board / conflict /
    // capacity / budget loads on a large scenario; re-enabling before it settles
    // hands the registrar a live button over a half-loaded grid, and one more
    // click dispatches a second ~7-minute run. Previously the refresh sat inside
    // `try` with the button still disabled, so moving it here reopened that
    // window — this closes it again.
    finishWorkspaceOperation(token);
  }
}
/* End V2 optimiser. */
