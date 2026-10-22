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
  // Every successful write bumps this. Scenario-wide reads (quality, publish
  // readiness) are cached against it, so switching boards never recomputes them.
  dataVersion: 0,
  qualityVersion: -1,
  readiness: null,
  readinessVersion: -1,
};

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
  document.querySelectorAll('.tw-opt-item, [data-tw-mutation]').forEach(el => { el.disabled = !editable; });
  document.querySelectorAll('[data-tw-draggable]').forEach(el => { el.draggable = editable && el.dataset.twMovable !== 'false' && !el.classList.contains('locked'); });
  document.querySelector('.tw-workspace')?.setAttribute('aria-busy', String(busy));
  updateOpenLinks();
  renderPublishState();
  window.dispatchEvent(new CustomEvent('tw:planner-busy', { detail: { busy } }));
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
  const { _workspaceToken, _onError, _quiet, ...requestOptions } = opts;
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
      let msg = `${response.status}`;
      try {
        const d = busyBody || await response.json();
        const e = d.error;
        // A caller that can say more than a toast (Publish lists the server's
        // blockers) takes the error body instead.
        if (typeof opts._onError === 'function' && opts._onError(d, response.status)) return null;
        if (_quiet) return null;
        if (e?.code === 'SCENARIO_BUSY' && e.details?.active_job) {
          announcePlannerJob(e.details.active_job, e.details.scenario_id, e.details.active_job.job_id);
        }
        msg = (e && typeof e === 'object') ? (e.message || e.code || JSON.stringify(e)) : (e || d.message || msg);
        const seedLabel = runSeedText(e?.details || d);
        if (seedLabel) msg += ` · ${seedLabel}`;
      } catch {}
      if (!_quiet) notify.error(msg);
      return null;
    }
    const body = await response.json();
    if (writes) TW.dataVersion++;
    return body;
  } catch (err) {
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
  $('twSearch')?.addEventListener('input', debounce(() => {
    renderGrid();
    renderBudgetSidebar();
    updateSearchStatus();
  }, 120));
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
    if (TW.loadedBoardId !== boardId) TW.selectedPlacementId = null;
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
  sortedTerms.forEach(t => {
    // Term header
    const hdr = document.createElement('h3');
    hdr.className = 'tw-term-head' + (t === activeTerm ? ' is-active' : '');
    hdr.textContent = `${IS_AR ? 'المستوى' : 'Term'} ${t}`;
    list.appendChild(hdr);

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
        const div = document.createElement('div');
        div.className = 'tw-section-item is-complete';
        div.innerHTML = `
          <span class="code"><span class="ux-tone-icon" data-tone="ok" aria-hidden="true"></span>${esc(c.course_code)}</span>
          <span class="meta ux-meta"><span>${IS_AR ? `${used}/${planned} موضوعة` : `${used}/${planned} placed`}</span><span>${IS_AR ? `${c.total_demand} طالب` : `demand ${c.total_demand}`}</span></span>
        `;
        list.appendChild(div);
        return;
      }

      for (let s = used + 1; s <= planned; s++) {
        const secLabel = `S${s}`;
        const div = document.createElement('div');
        div.className = 'tw-section-item';
        div.draggable = workspaceCanEdit();
        div.dataset.twDraggable = 'true';
        div.innerHTML = `
          <span class="code">${esc(c.course_code)} ${secLabel}</span>
          <span class="meta ux-meta">${[c.department || '', `${c.credit_hours || '?'} ${IS_AR ? 'س' : 'cr'}`, `${IS_AR ? 'سعة' : 'cap'} ${c.max_per_section || '?'}`].filter(Boolean).map(part => `<span>${esc(part)}</span>`).join('')}</span>
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
          }));
          e.dataTransfer.effectAllowed = 'copy';
        });
        if (globalThis.TimetableMeetingPolicy?.isEnglish(c)) {
          const choose = document.createElement('button'); choose.type = 'button'; choose.className = 'tw-pattern-action';
          choose.dataset.twMutation = ''; choose.disabled = !workspaceCanEdit();
          choose.textContent = IS_AR ? 'اختر النمط الأسبوعي' : 'Choose weekly pattern';
          choose.addEventListener('click', () => chooseEnglishWorkspacePattern({ ...c, type: 'create_planned', section_label: secLabel }));
          div.appendChild(choose);
        }
        list.appendChild(div);
      }
    });
  });
  setLeftCount(leftToPlace);
  renderBoardTabs();
  renderWorkspaceQuality();
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

// 20 distinct pastel colors matching XLSX export
const COURSE_COLORS = [
  '#D4E6F1','#D5F5E3','#FADBD8','#FCF3CF','#D7BDE2',
  '#A9DFBF','#F9E79F','#AED6F1','#F5CBA7','#A3E4D7',
  '#E8DAEF','#FDEBD0','#ABB2B9','#A2D9CE','#F5B7B1',
  '#D6DBDF','#ABEBC6','#FAD7A0','#D2B4DE','#AEB6BF',
];
const _courseColorMap = {};
function courseColorKey(code) {
  return String(code || '').replace(/\u00a0/g, ' ').trim().toUpperCase().replace(/\s+/g, '') || '__UNKNOWN__';
}
function courseColor(code) {
  const key = courseColorKey(code);
  if (!_courseColorMap[key]) {
    const idx = Object.keys(_courseColorMap).length % COURSE_COLORS.length;
    _courseColorMap[key] = COURSE_COLORS[idx];
  }
  return _courseColorMap[key];
}

function courseKeyOf(item) {
  return String(item?.course_key || item?.course_code || '').trim();
}

function resetPlanLens() {
  TW.planLens = { plans: [], courses: {}, sections: {}, loaded: false };
}

async function loadPlanLens() {
  const scenarioId = TW.scenarioId;
  if (!TW.scenarioId) {
    resetPlanLens();
    return;
  }
  const data = await twFetch(`/ops/tw/scenarios/${TW.scenarioId}/plan-lens/`);
  if (TW.scenarioId !== scenarioId) return;
  if (!data || !data.plan_lens) {
    TW.planLens.loaded = false;
    return;
  }
  TW.planLens = Object.assign({ plans: [], courses: {}, sections: {}, loaded: true }, data.plan_lens);
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

function placementSearchText(p) {
  const lens = sectionLensForPlacement(p);
  const lensBits = lens
    ? [lens.owner, lens.owner_label, lens.role, ...(lens.filter_plans || [])].filter(Boolean).join(' ')
    : '';
  // The words the user sees ("No room", "Online"), never the placer's marker.
  const meetingBits = (p.meetings || [])
    .map(m => [m.instructor, meetingRoomText(m.room, p), m.day, m.start_time, m.end_time].filter(Boolean).join(' '))
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
    p.instructor,
    meetingBits,
    lensBits,
  ].filter(Boolean).join(' ').toUpperCase();
}

function placementMatchesWorkspaceSearch(p) {
  const tokens = workspaceSearchTokens();
  if (!tokens.length) return true;
  const text = placementSearchText(p);
  return tokens.every(token => {
    const planToken = planTokenFromSearch(token);
    return planToken ? itemMatchesPlanToken(p, planToken) : text.includes(token);
  });
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

function updateSearchStatus() {
  const input = $('twSearch');
  if (!input) return;
  const count = $('twSearchCount');
  const scope = $('twExportScope');
  const q = activeWorkspaceSearch();
  const plan = planTokenFromSearch(q);
  if (scope) {
    scope.textContent = !q
      ? (IS_AR ? 'ملف XLSX لكل برنامج بأوراق حسب فصل الخطة' : 'One XLSX per programme, sheets by plan term')
      : plan
        ? (IS_AR ? `برنامج ${plan} فقط` : `Programme ${plan} only`)
        : (IS_AR ? `ما يطابق «${q}» فقط` : `Only what matches "${q}"`);
  }
  if (!q) {
    input.title = '';
    if (count) count.textContent = '';
    return;
  }
  const matched = TW.placements.filter(placementMatchesWorkspaceSearch).length;
  const text = IS_AR ? `${matched} من ${TW.placements.length}` : `${matched} of ${TW.placements.length}`;
  input.title = IS_AR ? `${text} لقاء مطابق` : `${text} meetings match`;
  if (count) count.textContent = text;
}

function renderGridWithSlots(slots, labSlots) {
  const gridEl = $('twGrid');
  $('twEmptyState').style.display = 'none';
  gridEl.classList.remove('d-none');
  gridEl.style.display = '';
  gridEl.innerHTML = '';

  labSlots = labSlots || [];
  const search = activeWorkspaceSearch();
  const visiblePlacements = search
    ? TW.placements.filter(placementMatchesWorkspaceSearch)
    : TW.placements;
  updateSearchStatus();

  // ── Split placements into student groups by section label ──
  const bySec = {};
  visiblePlacements.forEach(p => {
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

  // If no placements, show empty grid
  if (numGroups === 0) {
    _renderSingleGrid(gridEl, slots, [], 'tw-grid-table', null);
    if (labSlots.length) {
      gridEl.appendChild(gridSubheading(IS_AR ? 'المعامل (100 دقيقة)' : 'Labs (100 min)', 'lab'));
      _renderSingleGrid(gridEl, labSlots, [], 'tw-grid-table tw-lab-grid', null);
    }
    return;
  }

  groupKeys.forEach((secKey, gi) => {
    const groupPlacements = bySec[secKey];
    const tsIds = new Set(groupPlacements.map(p => p.term_section_id));
    const allForGroup = visiblePlacements.filter(p => tsIds.has(p.term_section_id));

    // Separate lecture and lab placements
    const lecturePlacements = allForGroup.filter(p => !isLabPlacement(p));
    const labPlacements = allForGroup.filter(p => isLabPlacement(p));

    // Group header: the section label it really is, and what it holds.
    if (numGroups > 1) {
      const courses = new Set(groupPlacements.map(p => p.course_code));
      const hdr = document.createElement('h3');
      hdr.className = 'tw-group-head';
      hdr.id = `twGroup-${gi}`;
      const what = IS_AR ? `${courses.size} مقررات` : `${courses.size} ${courses.size === 1 ? 'course' : 'courses'}`;
      const matches = search ? (IS_AR ? ` · ${groupPlacements.length} مطابقة` : ` · ${groupPlacements.length} matches`) : '';
      hdr.innerHTML = `<span class="tw-group-name">${IS_AR ? 'الشعبة' : 'Section'} <bdi>${esc(secKey)}</bdi></span><span class="tw-group-meta">${what}${matches}</span>`;
      gridEl.appendChild(hdr);
    }

    // Lecture grid
    const gridInner = document.createElement('div');
    gridInner.className = 'tw-grid-inner';
    const englishPlacements = lecturePlacements.filter(p => globalThis.TimetableMeetingPolicy?.isEnglish(p));
    _renderSingleGrid(gridInner, slots, lecturePlacements.filter(p => !globalThis.TimetableMeetingPolicy?.isEnglish(p)), 'tw-grid-table', secKey);
    if (englishPlacements.length) {
      const heading = document.createElement('div'); heading.className = 'tw-english-heading';
      heading.textContent = IS_AR ? 'الإنجليزية · لقاءات متصلة لمدة ثلاث ساعات' : 'English · continuous three-hour meetings';
      gridInner.appendChild(heading);
      _renderSingleGrid(gridInner, TimetableMeetingPolicy.gridSlots(englishPlacements), englishPlacements, 'tw-grid-table tw-english-grid', secKey);
    }
    gridEl.appendChild(gridInner);

    // Lab grid (if any lab slots exist)
    if (labSlots.length) {
      gridEl.appendChild(gridSubheading(IS_AR ? 'المعامل' : 'Labs', 'lab'));
      const labInner = document.createElement('div');
      labInner.className = 'tw-grid-inner';
      _renderSingleGrid(labInner, labSlots, labPlacements, 'tw-grid-table tw-lab-grid', secKey);
      gridEl.appendChild(labInner);
    }
  });
  // New cards take their tones from the model at once, not at the next redraw.
  applyCardTones();
}

function gridSubheading(text, kind) {
  const heading = document.createElement('div');
  heading.className = `tw-subhead tw-subhead-${kind}`;
  heading.textContent = text;
  return heading;
}

function _renderSingleGrid(container, slots, placements, className, secKey) {
  // Transposed: columns = time slots, rows = days. The column count is the one
  // layout fact the stylesheet cannot know; everything else is in its classes.
  const numSlots = slots.length;
  const table = document.createElement('div');
  table.className = className;
  table.style.setProperty('--tw-slots', String(numSlots));

  // One sticky header row: the slot's number above its times.
  const corner = document.createElement('div');
  corner.className = 'tw-grid-header tw-grid-corner';
  table.appendChild(corner);
  slots.forEach((s, i) => {
    const hdr = document.createElement('div');
    hdr.className = 'tw-grid-header';
    hdr.innerHTML = `<span class="tw-slot-no">${i + 1}</span><bdi class="tw-slot-time">${esc(s.start)}–${esc(s.end)}</bdi>`;
    table.appendChild(hdr);
  });

  // Day rows
  DAYS.forEach((day, di) => {
    // Day label
    const lbl = document.createElement('div');
    lbl.className = 'tw-grid-slot-label';
    lbl.textContent = DAY_LABELS[di];
    table.appendChild(lbl);

    // Slot cells
    slots.forEach(slot => {
      const cell = document.createElement('div');
      cell.className = 'tw-cell';
      cell.dataset.day = day;
      cell.dataset.start = slot.start;
      cell.dataset.end = slot.end;

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
        cellPlacements.forEach(p => cell.appendChild(createPlacementCard(p)));
      }

      table.appendChild(cell);
    });
  });

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
  // Another scenario owns a shared section's time and room: the server refuses
  // its move, so it does not drag (Remove still works).
  const movable = !p.is_locked && !p.shared_with_other_scenario;
  card.draggable = workspaceCanEdit() && movable;
  card.dataset.twDraggable = 'true';
  card.dataset.twMovable = String(movable);
  card.dataset.placementId = p.id;
  // The course's hue, not a fill: light mode shows the pastel, dark mode mixes
  // it into the card surface so the board is not a wall of light tiles.
  card.style.setProperty('--tw-card-hue', courseColor(p.course_code));
  if (p.is_locked) card.classList.add('locked');

  const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '';
  const courseName = compactCourseName(p.course_name);
  const showCourseName = courseName && hasDuplicateCourseCode(p.course_code);
  // Its identity; applyCardTones() puts the reason for a tone in front of it.
  card.dataset.identity = [p.course_key, courseName].filter(Boolean).join(' | ');
  card.title = card.dataset.identity;
  // The second line is a meta list: only the parts that exist, separated by
  // the stylesheet (no stray pipes when the instructor is unknown).
  const english = globalThis.TimetableMeetingPolicy?.isEnglish(p);
  const room = placementRoomText(p);
  const meta = [
    room,
    p.available_capacity ? (IS_AR ? `سعة ${p.available_capacity}` : `cap ${p.available_capacity}`) : '',
    instructor,
  ].filter(Boolean).map(part => `<bdi>${esc(part)}</bdi>`).join('');
  card.innerHTML = `
    <div class="cc">${esc(p.course_code)}${TW.cardsShowSection === false ? '' : ` <bdi>${esc(p.section)}</bdi>`}</div>
    ${english ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))}</div>` : ''}
    ${showCourseName ? `<div class="tw-card-name">${esc(courseName)}</div>` : ''}
    ${meta ? `<div class="ci ux-meta">${meta}</div>` : ''}
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
    document.querySelectorAll('.tw-card.is-selected').forEach(other => other.classList.remove('is-selected'));
    card.classList.add('is-selected');
    document.querySelectorAll('.tw-right .tw-panel-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === 'selection');
    });
    renderRightPanel('selection');
    window.dispatchEvent(new CustomEvent('tw:pane-request', { detail: { pane: 'right', open: true } }));
  });
  if (TW.selectedPlacementId === p.id) card.classList.add('is-selected');

  return card;
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

  items.slice(0, visibleLimit).forEach(s => {
    const div = document.createElement('div');
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
      choose.addEventListener('click', () => chooseEnglishWorkspacePattern({ ...s, type: 'create', section_label: s.section }));
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
    list.appendChild(div);
  });
  if (items.length > visibleLimit) {
    const more = document.createElement('button');
    more.type = 'button'; more.className = 'btn btn-sm btn-neutral tw-show-more';
    more.textContent = IS_AR ? `عرض المزيد (${visibleLimit}/${items.length})` : `Show more (${visibleLimit}/${items.length})`;
    more.addEventListener('click', () => { TW.unplacedVisibleLimit = visibleLimit + 100; renderUnplacedList(); });
    list.appendChild(more);
  }
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
  const b = (data && data.instructor_clash_backstop) || {};
  if (!b.checked) return;
  const rows = b.involving_this_section || [];
  if (!rows.length) return;
  notify.warning(IS_AR
    ? `تعارض للمحاضر: ${rows.length} حصة متداخلة عبر اللوحات`
    : `Instructor double-booked — ${rows.length} overlapping session(s) across boards`);
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
    // From generated plan — need to find or create a TermSection first
    // Use the board's create-planned endpoint
    const data = await twFetch('/ops/tw/placements/create-planned/', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        board_id: boardAtStart,
        course_code: payload.course_code,
        section_label: payload.section_label,
        day: day,
        start_time: startTime,
        end_time: endTime,
        capacity: payload.max_per_section || 40,
      }),
    });
    if (!data) return;
    TW.undoStack.push({ type: 'create', placement_id: data.placement.id,
      board_id: boardAtStart, term_section_id: data.placement.term_section_id,
      day: day, start_time: startTime, end_time: endTime, room: '' });
    TW.redoStack = [];
    const v = data.validation || {};
    if (v.critical_count > 0) {
      notify.warning(IS_AR ? 'تم الوضع مع تعارضات' : `Placed with ${v.critical_count} conflict(s)`);
    } else {
      notify.success(IS_AR ? 'تم الوضع' : 'Placed');
    }
    notifyInstructorClashBackstop(data);
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
    const v = data.validation || {};
    if (v.critical_count > 0) {
      notify.warning(IS_AR ? 'تم الوضع مع تعارضات' : `Placed with ${v.critical_count} conflict(s)`);
    } else {
      notify.success(IS_AR ? 'تم الوضع' : 'Placed');
    }
    notifyInstructorClashBackstop(data);
  } else if (payload.type === 'move') {
    // Find old position before move
    const oldP = TW.placements.find(x => x.id === payload.placement_id);
    const oldDay = oldP ? oldP.day : '';
    const oldStart = oldP ? oldP.start_time : '';
    const oldEnd = oldP ? oldP.end_time : '';

    const data = await twFetch(`/ops/tw/placements/${payload.placement_id}/move/`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ day, start_time: startTime, end_time: endTime }),
    });
    if (!data) return;
    TW.undoStack.push({ type: 'move', placement_id: payload.placement_id,
      old_day: oldDay, old_start: oldStart, old_end: oldEnd,
      new_day: day, new_start: startTime, new_end: endTime });
    TW.redoStack = [];
    const v = data.validation || {};
    if (v.critical_count > 0) {
      notify.warning(IS_AR ? 'تم النقل مع تعارضات' : `Moved with ${v.critical_count} conflict(s)`);
    } else {
      notify.success(IS_AR ? 'تم النقل' : 'Moved');
    }
    notifyInstructorClashBackstop(data);
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
  if (!['create', 'remove'].includes(action.type)) return null;
  const recreate = (action.type === 'create') !== undo;
  return recreate
    ? twFetch('/ops/tw/placements/create/', post({ board_id: action.board_id,
      term_section_id: action.term_section_id, day: action.day, start_time: action.start_time,
      end_time: action.end_time, room: action.room || '', ...(action.meetings ? { meetings: action.meetings } : {}) }))
    : twFetch(`/ops/tw/placements/${action.placement_id}/remove/`, post({}));
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
    if (!data || data.ok === false || TW.scenarioId !== token.scenarioId) return;
    from.pop();
    if (action.meetings && data.placements) globalThis.TimetableMeetingPolicy?.remapHistory([action, ...TW.undoStack, ...TW.redoStack], action.meetings, data.placements);
    to.push({ ...action, placement_id: data.placement?.id || action.placement_id });
    await selectBoard(TW.boardId);
  } finally {
    finishWorkspaceOperation(token);
  }
}
$('twUndo').addEventListener('click', () => runHistoryAction(true));
$('twRedo').addEventListener('click', () => runHistoryAction(false));

/* ── Right Panel ── */
function renderRightPanel(tab) {
  const body = $('twRightBody');

  if (tab === 'issues') {
    const model = buildBoardIssues();
    setIssuesCount(issueCountOf(model), model.counts.critical);
    // With no board open (a scenario with none yet), the tab has no board to
    // speak for; the scenario's own blockers are on Publish.
    if (!TW.boardId) {
      body.innerHTML = `<div class="tw-empty tw-empty-sm"><p>${IS_AR ? 'افتح لوحة لعرض مشاكلها.' : 'Open a board to see its issues.'}</p></div>`;
      return;
    }
    const note = text => `<p class="tw-issue-note">${text}</p>`;
    let notes = '';
    if (!model.known.conflicts) notes += note(IS_AR ? 'تعارضات هذه اللوحة غير محمّلة.' : 'This board’s clashes are not loaded.');
    const checking = Boolean(TW.readinessLoading || TW.qualityLoading);
    if (model.rows.some(row => row.pending)) {
      notes += note(checking
        ? (IS_AR ? 'بعض البنود من قبل آخر تعديل؛ يجري التحقق منها.' : 'Some items are from before your last change; they are being checked.')
        : (IS_AR ? 'بعض البنود من قبل آخر تعديل؛ لم يُعَد التحقق منها بعد.' : 'Some items are from before your last change and have not been checked again.'));
    }
    if (TW.quality && !TW.quality.rooms?.assessed) notes += note(IS_AR ? 'صلاحية القاعات لم تُقيّم بعد.' : 'Room suitability has not been assessed yet.');

    if (!model.rows.length) {
      body.innerHTML = model.known.conflicts
        ? `<div class="tw-empty tw-empty-sm"><span class="ux-tone-icon" data-tone="ok" aria-hidden="true"></span><p>${IS_AR ? 'لا مشاكل في هذه اللوحة' : 'No issues on this board'}</p></div>${notes}`
        : notes;
      return;
    }

    // Three groups in severity order; inside each, by kind as the model sorts.
    let html = notes;
    TONE_ORDER.forEach(tone => {
      const rows = model.rows.filter(row => row.tone === tone);
      if (!rows.length) return;
      html += `<h3 class="tw-issue-head" data-tone="${tone}"><span class="ux-tone-icon" data-tone="${tone}" aria-hidden="true"></span><span>${esc(TONE_LEAD[tone])}</span><span class="tw-issue-count tnum">${model.counts[tone]}</span></h3>`;
      rows.forEach(row => {
        const pendingWord = checking ? (IS_AR ? 'يجري التحقق' : 'checking') : (IS_AR ? 'من قبل آخر تعديل' : 'from before your last change');
        const inner = `<span class="tw-issue-kind">${esc(issueKindLabel(row.kind))}${row.pending ? ` · ${pendingWord}` : ''}</span>`
          + `<span class="tw-issue-subject"><bdi>${isolateTimes(row.subject)}</bdi></span>`
          + (row.detail ? `<span class="tw-issue-detail"><bdi>${isolateTimes(row.detail)}</bdi></span>` : '');
        html += row.ids.length && row.locate !== false
          ? `<button type="button" class="tw-issue-row" data-tone="${tone}" data-ids='${esc(JSON.stringify(row.ids))}'${row.boardTarget ? ` data-board-target="${esc(row.boardTarget)}"` : ''}>${inner}</button>`
          : `<div class="tw-issue-row tw-issue-static" data-tone="${tone}">${inner}</div>`;
      });
    });
    body.innerHTML = html;

    // A row with sections takes the user to them (another board's first).
    body.querySelectorAll('.tw-issue-row[data-ids]').forEach(row => {
      row.addEventListener('click', async () => {
        const targetBoard = parseInt(row.dataset.boardTarget || '', 10);
        try {
          const ids = JSON.parse(row.dataset.ids);
          if (Number.isFinite(targetBoard) && targetBoard && targetBoard !== TW.boardId) {
            await selectBoard(targetBoard);
          }
          highlightWorkspacePlacements(ids);
        } catch {}
      });
    });
  } else if (tab === 'selection') {
    if (!TW.selectedPlacementId) {
      body.innerHTML = `<div class="tw-empty tw-empty-sm"><p>${IS_AR ? 'اختر شعبة في الجدول لعرض تفاصيلها.' : 'Click a section on the grid to inspect it.'}</p></div>`;
      return;
    }
    const p = TW.placements.find(x => x.id === TW.selectedPlacementId);
    if (!p) { body.innerHTML = ''; return; }
    const english = globalThis.TimetableMeetingPolicy?.isEnglish(p);
    const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '';
    const dayLabel = code => DAY_LABELS[DAYS.indexOf(code)] || code || '—';
    const field = (label, value) => `<div class="tw-sel-field"><dt>${label}</dt><dd>${value}</dd></div>`;
    const meetings = (p.meetings || []).map(m => `<li class="ux-meta"><span>${esc(dayLabel(m.day))}</span><bdi>${esc(m.start_time)}–${esc(m.end_time)}</bdi><bdi>${esc(english ? TimetableMeetingPolicy.roomLabel(p, IS_AR) : meetingRoomText(m.room, p))}</bdi>${m.instructor ? `<bdi>${esc(m.instructor)}</bdi>` : ''}</li>`).join('');
    const editable = workspaceCanEdit();
    body.innerHTML = `
      <div class="tw-sel-head">
        <div class="tw-sel-title"><bdi>${esc(p.course_code)}</bdi> <bdi>${esc(p.section)}</bdi></div>
        ${p.course_name ? `<div class="tw-sel-sub">${esc(compactCourseName(p.course_name))}</div>` : ''}
        ${p.is_locked ? `<span class="pill-status pill-b">${IS_AR ? 'مقفلة' : 'Locked'}</span>` : ''}
      </div>
      ${english ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))} · SUN / MON / WED / THU</div><button type="button" class="tw-pattern-action" data-tw-mutation ${editable ? '' : 'disabled'} onclick="chooseEnglishWorkspacePattern({type:'move',placement_id:${p.id}})">${IS_AR ? 'تغيير نمط الشعبة' : 'Change section pattern'}</button>` : ''}
      <dl class="tw-sel-fields">
        ${field(IS_AR ? 'اليوم' : 'Day', esc(dayLabel(p.day)))}
        ${field(IS_AR ? 'الوقت' : 'Time', `<bdi>${esc(p.start_time)}–${esc(p.end_time)}</bdi>`)}
        ${field(IS_AR ? 'القاعة' : 'Room', `<bdi>${esc(placementRoomText(p) || (IS_AR ? 'لم تُحدَّد' : 'Not set'))}</bdi>`)}
        ${field(IS_AR ? 'المدرس' : 'Instructor', instructor ? `<bdi>${esc(instructor)}</bdi>` : '—')}
        ${field(IS_AR ? 'السعة' : 'Capacity', p.available_capacity ? esc(p.available_capacity) : '—')}
        ${field(IS_AR ? 'المسجلون' : 'Registered', p.registered_count ? esc(p.registered_count) : '—')}
      </dl>
      ${meetings ? `<h3 class="tw-sel-subhead">${IS_AR ? 'جميع لقاءات الشعبة' : 'All meetings of this section'}</h3><ul class="tw-sel-meetings">${meetings}</ul>` : ''}
      <div class="tw-sel-actions">
        <button type="button" class="btn btn-sm btn-neutral" data-tw-mutation ${editable ? '' : 'disabled'} title="${esc(T.lockHelp)}" aria-pressed="${p.is_locked ? 'true' : 'false'}" onclick="toggleLock(${p.id})">${p.is_locked ? (IS_AR ? 'فتح القفل' : 'Unlock') : (IS_AR ? 'قفل الوقت' : 'Lock time')}</button>
        <button type="button" class="btn btn-sm btn-outline-danger" data-tw-mutation ${editable ? '' : 'disabled'} onclick="removePlacement(${p.id})">${IS_AR ? 'إزالة من الجدول' : 'Remove from timetable'}</button>
      </div>
      <p class="tw-sel-help">${esc(T.lockHelp)}</p>
    `;
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

function highlightWorkspacePlacements(ids) {
  document.querySelectorAll('.tw-card.highlight').forEach(c => c.classList.remove('highlight'));
  let firstCard = null;
  (ids || []).forEach(id => {
    const card = document.querySelector(`.tw-card[data-placement-id="${id}"]`);
    if (card) {
      card.classList.add('highlight');
      if (!firstCard) firstCard = card;
    }
  });
  if (firstCard) firstCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
  renderWorkspaceQuality();
  const active = document.querySelector('.tw-right .tw-panel-tab.active');
  if (active) renderRightPanel(active.dataset.tab);
  scheduleReadinessRefresh();
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
   rebuilds the grid (a drag or a focused card survives it). */
function applyCardTones() {
  const { byPlacement } = buildBoardIssues();
  document.querySelectorAll('#twGrid .tw-card[data-placement-id]').forEach(card => {
    const entry = byPlacement.get(card.dataset.placementId);
    card.classList.toggle('critical', entry?.tone === 'critical');
    card.classList.toggle('warning', entry?.tone === 'warning');
    card.classList.toggle('has-issue', Boolean(entry));
    const reason = entry ? `${TONE_LEAD[entry.tone]}: ${entry.kinds.map(issueKindLabel).join(', ')}` : '';
    card.title = [reason, card.dataset.identity || ''].filter(Boolean).join(' | ');
  });
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

    // Hotspot courses
    if (o.hotspot_courses && o.hotspot_courses.length > 0) {
      body += `<div class="mb-2"><span class="fs-sm fw-bold text-caps text-danger">${IS_AR ? 'مقررات مزدحمة' : 'Hotspot Courses'}</span>`;
      body += `<div class="d-flex flex-wrap" style="gap:4px; margin-top:4px">`;
      o.hotspot_courses.forEach(c => {
        body += `<span class="text-danger fs-sm fw-semibold" style="padding:2px 8px; border-radius:999px; background:rgba(192,48,48,.1)">${c}</span>`;
      });
      body += `</div></div>`;
    }

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
