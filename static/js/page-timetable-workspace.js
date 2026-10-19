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
  noIssues: IS_AR ? 'لا توجد مشاكل' : 'No issues',
  selectScenario: IS_AR ? 'اختر سيناريو' : 'Select Scenario',
  createScenario: IS_AR ? 'إنشاء سيناريو' : 'Create Scenario',
  scenarioName: IS_AR ? 'اسم السيناريو' : 'Scenario name',
  createBoard: IS_AR ? 'إنشاء لوحة' : 'Create Board',
  boardLabel: IS_AR ? 'اسم اللوحة' : 'Board label',
  placed: IS_AR ? 'موضوعة' : 'Placed',
  conflicts: IS_AR ? 'تعارضات' : 'Conflicts',
  publishConfirm: IS_AR ? 'هل تريد نشر هذا السيناريو؟' : 'Publish this scenario?',
  published_ok: IS_AR ? 'تم النشر' : 'Published successfully',
  overlap: IS_AR ? 'تعارض زمني داخل اللوحة' : 'Same-board time clash',
  instrClash: IS_AR ? 'تعارض مدرس' : 'Instructor clash',
  roomClash: IS_AR ? 'تعارض قاعة' : 'Room clash',
  crossBoardClash: IS_AR ? 'تداخل محتمل في الطلب عبر اللوحات' : 'Potential cross-board demand overlap',
  lockHelp: IS_AR ? 'يثبّت أيام وأوقات هذه الشعبة؛ ويمكن تغيير القاعة.' : "Keeps this section's days and times; its room can still change.",
};

const CSRF = document.querySelector('[name=csrfmiddlewaretoken]')?.value || 'djCsrfToken';
const $ = id => document.getElementById(id);
const DEFAULT_YEAR = 'defaultYear';
const DEFAULT_TERM = 'defaultTerm';

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
  ['twGenerate', 'twGenerateMenu', 'twMapElectives', 'twNewScenario', 'twScenario',
    'twYear', 'twTerm', 'twProgram', 'twSection', 'twStrategy'].forEach(id => {
    if ($(id)) $(id).disabled = busy;
  });
  ['twNewBoard', 'twSlotEdit', 'twOptimise', 'twOptimiseMenu', 'twPublish'].forEach(id => {
    if ($(id)) $(id).disabled = !editable;
  });
  if ($('twUndo')) $('twUndo').disabled = !editable || !TW.undoStack.length;
  if ($('twRedo')) $('twRedo').disabled = !editable || !TW.redoStack.length;
  document.querySelectorAll('.tw-gen-item').forEach(el => { el.disabled = busy; });
  document.querySelectorAll('.tw-opt-item, [data-tw-mutation]').forEach(el => { el.disabled = !editable; });
  document.querySelectorAll('[data-tw-draggable]').forEach(el => { el.draggable = editable && !el.classList.contains('locked'); });
  document.querySelector('.tw-workspace')?.setAttribute('aria-busy', String(busy));
  if ($('twGraphPage')) $('twGraphPage').href = '/timetable-workspace/graph/'
    + (TW.scenarioId ? `?scenario=${encodeURIComponent(TW.scenarioId)}` : '');
  window.dispatchEvent(new CustomEvent('tw:planner-busy', { detail: { busy } }));
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
  const { _workspaceToken, ...requestOptions } = opts;
  const writes = !['GET', 'HEAD'].includes((opts.method || 'GET').toUpperCase());
  if (writes && workspaceBusy() && (!TW_ACTIVITY.owner || _workspaceToken !== TW_ACTIVITY.owner)) {
    notify.warning(IS_AR ? 'الجدول قيد التحديث؛ انتظر انتهاء العملية.' : 'The timetable is being updated; wait for the operation to finish.');
    return null;
  }
  if (writes) { TW_ACTIVITY.writes++; syncWorkspaceControls(); }
  // Wrap safeFetch with better error extraction for our {ok, error: {code, message}} format
  try {
    const response = await fetch(url, _injectCsrf(requestOptions));
    if (!response.ok) {
      let msg = `${response.status}`;
      try {
        const d = await response.json();
        const e = d.error;
        if (e?.code === 'SCENARIO_BUSY' && e.details?.active_job) {
          announcePlannerJob(e.details.active_job, e.details.scenario_id, e.details.active_job.job_id);
        }
        msg = (e && typeof e === 'object') ? (e.message || e.code || JSON.stringify(e)) : (e || d.message || msg);
        const seedLabel = runSeedText(e?.details || d);
        if (seedLabel) msg += ` · ${seedLabel}`;
      } catch {}
      notify.error(msg);
      return null;
    }
    return await response.json();
  } catch (err) {
    notify.error(err.message || String(err));
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
      btn.innerHTML = `&#x23F3; ${modeLabel}${detail}`;
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

/* Which placement engine the plain Generate button uses. The dropdown beside it
   picks explicitly; this is what a bare click means. */
const TW_ENGINE = 'classic';

/* ── Init ── */
(function init() {
  // Events — Generate bar
  $('twGenerate').addEventListener('click', () => runGenerate(TW_ENGINE));

  // Engine picker. The two engines share the whole pipeline except placement:
  // both build the same scenario scaffold, same students, same section budgets.
  const genMenu = $('twGenerateMenu');
  const genDrop = $('twGenerateDropdown');
  if (genMenu && genDrop) {
    genMenu.addEventListener('click', (e) => {
      e.stopPropagation();
      genDrop.style.display = genDrop.style.display === 'none' ? 'block' : 'none';
    });
    document.addEventListener('click', () => { genDrop.style.display = 'none'; });
    genDrop.querySelectorAll('.tw-gen-item').forEach((item) => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        genDrop.style.display = 'none';
        runGenerate(item.dataset.engine || 'classic');
      });
    });
  }

  // Events — Workspace command bar
  $('twScenario').addEventListener('change', onScenarioChange);
  $('twNewScenario').addEventListener('click', createScenarioPrompt);
  $('twCopyScenario')?.addEventListener('click', copyScenarioPrompt);
  $('twNewBoard').addEventListener('click', createBoardPrompt);
  $('twSlotEdit').addEventListener('click', openSlotEditor);
  // Export: main button follows the active Plan Lens/search filter.
  $('twExport').addEventListener('click', () => exportScenario('per-plan'));
  $('twExportMenu').addEventListener('click', (e) => {
    e.stopPropagation();
    const dd = $('twExportDropdown');
    dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
  });
  document.querySelectorAll('.tw-export-item').forEach(item => {
    item.addEventListener('mouseenter', () => { item.style.background = 'var(--brand-soft)'; });
    item.addEventListener('mouseleave', () => { item.style.background = ''; });
    item.addEventListener('click', () => {
      $('twExportDropdown').style.display = 'none';
      exportScenario(item.dataset.mode);
    });
  });
  // Optimise: main button = "current" mode, dropdown items = specific mode
  $('twOptimise').addEventListener('click', () => runOptimiseV2('current'));
  $('twOptimiseMenu').addEventListener('click', (e) => {
    e.stopPropagation();
    const dd = $('twOptimiseDropdown');
    dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
  });
  document.querySelectorAll('.tw-opt-item').forEach(item => {
    item.addEventListener('mouseenter', () => { item.style.background = 'var(--brand-soft)'; });
    item.addEventListener('mouseleave', () => { item.style.background = ''; });
    item.addEventListener('click', () => {
      $('twOptimiseDropdown').style.display = 'none';
      runOptimiseV2(item.dataset.mode);
    });
  });
  // Close dropdowns on outside click
  document.addEventListener('click', (e) => {
    if (!$('twOptimiseGroup').contains(e.target)) {
      $('twOptimiseDropdown').style.display = 'none';
    }
    if (!$('twExportGroup').contains(e.target)) {
      $('twExportDropdown').style.display = 'none';
    }
  });
  $('twPublish').addEventListener('click', publishScenario);
  $('twMriPage').addEventListener('click', openFullMriPage);
  $('twGraphPage').addEventListener('click', openGraphTwinPage);
  $('twSearch')?.addEventListener('input', debounce(() => {
    renderGrid();
    renderBudgetSidebar();
    updateSearchStatus();
  }, 120));
  $('twSectionSearch').addEventListener('input', debounce(renderBudgetSidebar, 200));

  // Bottom panel toggle
  $('twBottomHandle').addEventListener('click', () => {
    $('twBottom').classList.toggle('open');
  });

  // Right panel tabs
  document.querySelectorAll('.tw-right .tw-panel-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tw-right .tw-panel-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      renderRightPanel(tab.dataset.tab);
    });
  });

  // Keep the scenario selector in sync whenever the registrar edits Year/Sem
  // without needing to click Generate.
  ['twYear', 'twTerm'].forEach(id => $(id).addEventListener('change', loadScenarios));

  // Prime the selector on first paint using the default year/term so it is
  // never empty when the page loads.
  loadScenarios().then(restoreWorkspaceContext);
})();

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
async function runGenerate(engine = 'classic') {
  if ($('twGenerate').disabled) return;
  const year = $('twYear').value.trim();
  const term = $('twTerm').value.trim();
  const program = $('twProgram').value.trim().toUpperCase();
  const section = $('twSection').value.trim().toUpperCase() || null;
  const strategy = $('twStrategy').value || 'compact';

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
        strategy: strategy,
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
    TW.quality = null;
    TW.plannerJob = null;
    if (data.build_job_id) announcePlannerJob({ job_id: data.build_job_id, status: 'queued' }, token.scenarioId, data.build_job_id);

    const ss = TW.studentSummary;
    $('twGenStatus').textContent = IS_AR
      ? `${ss.classified || 0} طالب | خطة: ${ss.on_plan || 0} | متقاطع: ${ss.cross_term || 0} | ${TW.boards.length} لوحات`
      : `${ss.classified || 0} students | On-plan: ${ss.on_plan || 0} | Cross-term: ${ss.cross_term || 0} | ${TW.boards.length} boards`;

    // Show workspace command bar
    $('twCmdBar').style.display = 'flex';
    updateValidationBadge();
    $('twPublish').disabled = false;
    $('twNewBoard').style.display = '';
    $('twSlotEdit').style.display = '';
    $('twNewBoard').classList.remove('d-none');
    $('twSlotEdit').classList.remove('d-none');
    $('twExportGroup').style.display = '';
    $('twExport').disabled = false;
    $('twExportMenu').disabled = false;
    $('twOptimiseGroup').style.display = '';
    syncWorkspaceControls();

    // Keep other saved scenarios available after the build finishes.
    const sel = $('twScenario');
    const option = document.createElement('option');
    option.value = data.scenario.id; option.textContent = data.scenario.name;
    sel.appendChild(option); sel.value = data.scenario.id;
    window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: TW.scenarioId, job: TW.plannerJob } }));

    // Render boards and sidebar
    renderBoardsNav();
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
    opt.value = s.id; opt.textContent = `${s.name} (${s.status})`;
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
    TW.placements = []; TW.conflicts = {}; TW.capacityData = null; TW.quality = null;
    if (!sameScenario) { TW.undoStack = []; TW.redoStack = []; TW.plannerJob = null; }
    if (data.active_job) TW.plannerJob = data.active_job;
    else if (!TW_ACTIVITY.owner && ACTIVE_PLANNER_STATUSES.has(TW.plannerJob?.status)) TW.plannerJob = null;
    window.dispatchEvent(new CustomEvent('tw:scenario-selected', { detail: { scenarioId: TW.scenarioId, job: TW.plannerJob || null } }));
    if (data.active_job) announcePlannerJob(data.active_job, TW.scenarioId, data.active_job.job_id);
    const search = $('twSearch');
    if (search) {
      search.value = '';
      search.title = 'Search timetable';
    }

    updateValidationBadge();
    $('twPublish').disabled = data.scenario.status === 'published';
    $('twNewBoard').style.display = '';
    $('twSlotEdit').style.display = data.scenario.status === 'published' ? 'none' : '';
    $('twExportGroup').style.display = '';
    $('twExport').disabled = false;
    $('twExportMenu').disabled = false;
    $('twOptimiseGroup').style.display = '';
    $('twNewBoard').classList.remove('d-none');
    $('twSlotEdit').classList.remove('d-none');
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
  $('twDrawer')?.remove();
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
  renderBoardsNav();
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
      renderBoardsNav(); renderBoardTabs();
      if (!TW.boardId) { $('twGrid').classList.add('d-none'); $('twEmptyState').style.display = ''; }
      return;
    }
    if (TW.loadedBoardId !== boardId) TW.selectedPlacementId = null;
    TW.loadedBoardId = boardId;
    persistWorkspaceScenario(TW.scenarioId, boardId);

    TW.placements = data.placements || [];
    TW.scenario = { ...TW.scenario, slot_config: data.slot_config };

    renderBoardsNav();
    renderBoardTabs();
    await loadConflicts();   // load conflicts BEFORE rendering grid so colors are correct
    if (revision !== boardLoadRevision || TW.scenarioId !== scenarioId) return;
    renderGrid();
    renderBoardStrip();
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
    list.innerHTML = `<div class="text-t4 fs-md" style="padding:12px">${IS_AR ? 'اضغط "توليد" أولاً' : 'Click "Generate" first'}</div>`;
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

  sortedTerms.forEach(t => {
    // Term header
    const hdr = document.createElement('div');
    hdr.style.cssText = 'font-size:.68rem;font-weight:700;color:var(--t4);text-transform:uppercase;letter-spacing:.04em;padding:6px 4px 2px;margin-top:4px;';
    if (t === activeTerm) hdr.style.color = 'var(--teal)';
    hdr.textContent = `${IS_AR ? 'المستوى' : 'Term'} ${t}`;
    list.appendChild(hdr);

    byTerm[t].forEach(c => {
      const remaining = c.remaining_sections || 0;
      const used = c.used_sections || 0;
      const planned = c.planned_sections || 0;
      const isFullyUsed = remaining <= 0;

      // One card per remaining section
      const count = Math.max(remaining, 0);
      if (count === 0 && used > 0) {
        // Show a grayed-out summary for fully used courses
        const div = document.createElement('div');
        div.className = 'tw-section-item';
        div.style.opacity = '0.4';
        div.style.cursor = 'default';
        div.innerHTML = `
          <span class="code">${esc(c.course_code)}</span>
          <span class="meta">${used}/${planned} ${IS_AR ? 'مستخدمة' : 'used'} | ${IS_AR ? 'طلاب' : 'demand'}: ${c.total_demand}</span>
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
          <span class="meta">${esc(c.department || '')} | ${c.credit_hours || '?'}cr | ${IS_AR ? 'سعة' : 'cap'}: ${c.max_per_section || '?'}</span>
          <span class="meta">${IS_AR ? 'الميزانية' : 'budget'}: ${used}/${planned} | ${IS_AR ? 'طلاب' : 'demand'}: ${c.total_demand}</span>
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
    const activeTab = document.querySelector('.tw-right .tw-panel-tab.active');
    renderRightPanel((activeTab && activeTab.dataset.tab) || 'issues');
    updateQuickStats();
  }
}

/* ── Publish ── */
async function publishScenario() {
  if (!workspaceCanEdit()) return;
  const scenarioId = TW.scenarioId;
  if (!TW.scenarioId) return;

  // Gather readiness summary across all boards
  let totalCritical = 0, totalWarning = 0, totalAffected = 0, totalBlocked = 0, boardCount = TW.boards.length;
  TW.boards.forEach(b => { totalCritical += (b.critical || 0); totalWarning += (b.warning || 0); });
  const si = TW.conflicts.student_impact || {};
  totalAffected = si.affected_count || 0;
  totalBlocked = si.blocked_count || 0;

  let summaryHtml = `<div style="font-size:.82rem;margin-bottom:12px">`;
  summaryHtml += `<div class="d-flex mb-2" style="gap:16px">`;
  summaryHtml += `<div><strong>${boardCount}</strong> ${IS_AR ? 'لوحات' : 'boards'}</div>`;
  summaryHtml += `<div style="color:${totalCritical > 0 ? 'var(--danger)' : 'var(--teal)'}"><strong>${totalCritical}</strong> ${IS_AR ? 'حرجة' : 'critical'}</div>`;
  summaryHtml += `<div style="color:${totalWarning > 0 ? 'var(--amber)' : 'var(--teal)'}"><strong>${totalWarning}</strong> ${IS_AR ? 'تحذيرات' : 'warnings'}</div>`;
  summaryHtml += `</div>`;
  if (totalAffected > 0) {
    summaryHtml += `<div class="text-danger">${IS_AR ? 'طلاب متأثرون' : 'Affected students'}: <strong>${totalAffected}</strong> (${IS_AR ? 'محجوبون' : 'blocked'}: <strong>${totalBlocked}</strong>)</div>`;
  }
  if (totalCritical > 0) {
    summaryHtml += `<div class="mt-2 text-danger fw-semibold" style="padding:8px; background:rgba(192,48,48,.08); border-radius:var(--radius-sm)">${IS_AR ? 'لا يمكن النشر: توجد مشاكل حرجة' : 'Cannot publish: critical issues exist'}</div>`;
  }
  summaryHtml += `</div>`;

  if (totalCritical > 0) {
    await dlg.confirm({ title: IS_AR ? 'النشر محجوب' : 'Publish Blocked', body: summaryHtml, kind: 'danger', confirmText: IS_AR ? 'حسنا' : 'OK' });
    return;
  }

  const ok = await dlg.confirm({ title: T.publishConfirm, body: summaryHtml, kind: 'warning', confirmText: IS_AR ? 'نشر' : 'Publish' });
  if (!ok) return;
  if (!workspaceCanEdit() || TW.scenarioId !== scenarioId) return;

  const data = await twFetch(`/ops/tw/scenarios/${TW.scenarioId}/publish/`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
  });
  if (!data) return;
  notify.success(T.published_ok);
  TW.scenario = data.scenario;
  updateValidationBadge();
  $('twPublish').disabled = true;
  $('twSlotEdit').style.display = 'none';
  syncWorkspaceControls();
}

function openGraphTwinPage(event) {
  if (event && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0)) return;
  if (event) event.preventDefault();
  const params = new URLSearchParams();
  if (TW.scenarioId) params.set('scenario', TW.scenarioId);
  window.location.href = `/timetable-workspace/graph/${params.toString() ? '?' + params.toString() : ''}`;
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

function openFullMriPage() {
  const params = new URLSearchParams();
  if (TW.scenarioId) params.set('scenario', TW.scenarioId);
  if (TW.boardId) params.set('board', TW.boardId);
  window.location.href = `/timetable-workspace/mri/${params.toString() ? '?' + params.toString() : ''}`;
}

/* ── Rendering ── */
function resetWorkspace() {
  TW.boards = []; TW.placements = []; TW.conflicts = {}; TW.capacityData = null;
  TW.quality = null; TW.undoStack = []; TW.redoStack = []; TW.selectedPlacementId = null;
  resetPlanLens();
  $('twBoardTabs').innerHTML = '';
  $('twBoardsNav').innerHTML = '';
  $('twSectionList').innerHTML = '';
  $('twGrid').classList.add('d-none');
  $('twGrid').style.display = '';
  $('twEmptyState').style.display = '';
  $('twBoardStrip').style.display = 'none';
  $('twNewBoard').style.display = 'none';
  $('twSlotEdit').style.display = 'none';
  $('twExportGroup').style.display = 'none';
  $('twExport').disabled = true;
  $('twExportMenu').disabled = true;
  $('twOptimiseGroup').style.display = 'none';
  updateQuickStats();
  renderWorkspaceQuality();
  syncWorkspaceControls();
}

function renderBoardsNav() {
  const nav = $('twBoardsNav');
  nav.innerHTML = '';
  TW.boards.forEach(b => {
    const div = document.createElement('div');
    div.className = 'tw-board-item' + (b.id === TW.boardId ? ' active' : '');
    const critical = b.critical || 0;
    const primary = b.primary_count || 0;
    const visitor = b.visitor_count || 0;
    div.innerHTML = `
      <div class="flex-fill">
        <div>${esc(b.label)}</div>
        <div class="text-t4" style="font-size:.65rem">${primary}${visitor > 0 ? '+' + visitor : ''} ${IS_AR ? 'طالب' : 'students'}</div>
      </div>
      <span class="tw-board-chip">${b.placement_count || 0}</span>
      ${critical > 0 ? `<span class="tw-board-chip danger">${critical}</span>` : ''}`;
    div.addEventListener('click', () => selectBoard(b.id));
    nav.appendChild(div);
  });
}

function renderBoardTabs() {
  const wrap = $('twBoardTabs');
  wrap.innerHTML = '';
  TW.boards.forEach(b => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tw-btn' + (b.id === TW.boardId ? ' tw-btn-primary' : '');
    btn.textContent = b.label;
    btn.addEventListener('click', () => selectBoard(b.id));
    wrap.appendChild(btn);
  });
}

function renderBoardStrip() {
  const strip = $('twBoardStrip');
  strip.style.display = 'flex';
  const placed = TW.placements.length;
  const s = TW.conflicts.summary || {};
  const overlaps = (TW.conflicts.overlaps || []).length;
  const instrC = (TW.conflicts.instructor_clashes || []).length;
  const roomC = (TW.conflicts.room_clashes || []).length;
  const si = TW.conflicts.student_impact || {};
  const affected = si.affected_count || 0;
  const crossBoard = activeBoardCrossBoardClashes().length;
  // Get student counts from board detail
  const activeBoard = TW.boards.find(b => b.id === TW.boardId);
  const primary = activeBoard ? (activeBoard.primary_count || 0) : 0;
  const visitor = activeBoard ? (activeBoard.visitor_count || 0) : 0;
  strip.innerHTML = `
    <div class="tw-metric"><div class="num">${primary}${visitor > 0 ? '<span class="fs-xs text-t4">+' + visitor + '</span>' : ''}</div><div class="lbl">${IS_AR ? 'طلاب' : 'Students'}</div></div>
    <div class="tw-metric"><div class="num">${placed}</div><div class="lbl">${T.placed}</div></div>
    <div class="tw-metric"><div class="num${overlaps > 0 ? ' danger' : ''}">${overlaps}</div><div class="lbl">${T.overlap}</div></div>
    <div class="tw-metric"><div class="num${instrC > 0 ? ' danger' : ''}">${instrC}</div><div class="lbl">${T.instrClash}</div></div>
    <div class="tw-metric"><div class="num${roomC > 0 ? ' danger' : ''}">${roomC}</div><div class="lbl">${T.roomClash}</div></div>
    <div class="tw-metric"><div class="num${crossBoard > 0 ? ' danger' : ''}">${crossBoard}</div><div class="lbl">${T.crossBoardClash}</div></div>
  `;
}

function renderGrid() {
  const gridEl = $('twGrid');
  const slots = (TW.scenario && TW.scenario.slot_config) || [];
  const labSlots = (TW.scenario && TW.scenario.lab_slot_config) || [];

  if (slots.length === 0) {
    // Default lecture slots (75 min each)
    const defaults = [
      {label:'09:00-10:15', start:'09:00', end:'10:15'},
      {label:'10:30-11:45', start:'10:30', end:'11:45'},
      {label:'10:50-12:05', start:'10:50', end:'12:05'},
      {label:'13:00-14:15', start:'13:00', end:'14:15'},
      {label:'14:30-15:45', start:'14:30', end:'15:45'},
      {label:'14:45-16:00', start:'14:45', end:'16:00'},
      {label:'16:00-17:15', start:'16:00', end:'17:15'},
    ];
    if (TW.scenario) TW.scenario.slot_config = defaults;
  }
  if (labSlots.length === 0) {
    const defaultLabs = [
      {label:'Lab 1', start:'09:00', end:'10:40'},
      {label:'Lab 2', start:'10:45', end:'12:25'},
      {label:'Lab 3', start:'13:00', end:'14:40'},
      {label:'Lab 4', start:'14:45', end:'16:25'},
      {label:'Lab 5', start:'16:30', end:'18:10'},
      {label:'Lab 6', start:'18:10', end:'19:50'},
    ];
    if (TW.scenario) TW.scenario.lab_slot_config = defaultLabs;
  }

  const finalSlots = (TW.scenario && TW.scenario.slot_config) || slots;
  const finalLabSlots = (TW.scenario && TW.scenario.lab_slot_config) || labSlots;
  renderGridWithSlots(finalSlots, finalLabSlots);
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
  const meetingBits = (p.meetings || [])
    .map(m => [m.instructor, m.room, m.day, m.start_time, m.end_time].filter(Boolean).join(' '))
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
    p.room,
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
  const q = activeWorkspaceSearch();
  if (!q) {
    input.title = IS_AR ? 'Search timetable' : 'Search timetable';
    return;
  }
  const matched = TW.placements.filter(placementMatchesWorkspaceSearch).length;
  input.title = `${matched}/${TW.placements.length} timetable matches`;
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
  const groupKeys = Object.keys(bySec).sort();
  const numGroups = groupKeys.length;

  // Helper: classify a placement as lab (100-min) based on duration
  function isLabPlacement(p) {
    if (globalThis.TimetableMeetingPolicy?.isEnglish(p)) return false;
    const toMin = t => { const [h,m] = t.split(':').map(Number); return h*60+m; };
    return (toMin(p.end_time) - toMin(p.start_time)) > 80;
  }

  // If no placements, show empty grid
  if (numGroups === 0) {
    _renderSingleGrid(gridEl, slots, [], 'tw-grid-inner', null);
    if (labSlots.length) {
      const labHdr = document.createElement('div');
      labHdr.style.cssText = 'text-align:center;font-size:.75rem;font-weight:700;color:var(--teal);padding:10px 0 4px;border-top:2px dashed var(--line);margin-top:6px;';
      labHdr.textContent = IS_AR ? '🔬 المعامل (100 دقيقة)' : '🔬 Labs (100 min)';
      gridEl.appendChild(labHdr);
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

    // Group header
    if (numGroups > 1) {
      const courses = new Set(groupPlacements.map(p => p.course_code));
      const hdr = document.createElement('div');
      hdr.style.cssText = 'text-align:center;font-size:.82rem;font-weight:700;color:var(--royal);padding:8px 0 4px;';
      hdr.textContent = search
        ? `Group ${gi+1} — ${courses.size} courses (${groupPlacements.length} matches)`
        : `Group ${gi+1} — ${courses.size} courses`;
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
      const labHdr = document.createElement('div');
      labHdr.style.cssText = 'text-align:center;font-size:.75rem;font-weight:700;color:var(--teal);padding:6px 0 2px;border-top:2px dashed var(--line);margin-top:2px;';
      labHdr.textContent = IS_AR ? '🔬 معامل' : '🔬 Labs';
      gridEl.appendChild(labHdr);
      const labInner = document.createElement('div');
      labInner.className = 'tw-grid-inner';
      _renderSingleGrid(labInner, labSlots, labPlacements, 'tw-grid-table tw-lab-grid', secKey);
      gridEl.appendChild(labInner);
    }
  });
}

function _renderSingleGrid(container, slots, placements, className, secKey) {
  // Transposed: columns = time slots, rows = days
  const numSlots = slots.length;
  const table = document.createElement('div');
  table.className = className;
  table.style.cssText = `display:grid;grid-template-columns:90px repeat(${numSlots},1fr);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;background:var(--card);margin-bottom:8px;`;

  // Row 1: Slot numbers
  const corner = document.createElement('div');
  corner.className = 'tw-grid-header';
  table.appendChild(corner);
  slots.forEach((s, i) => {
    const hdr = document.createElement('div');
    hdr.className = 'tw-grid-header';
    hdr.textContent = i + 1;
    table.appendChild(hdr);
  });

  // Row 2: Time ranges
  const corner2 = document.createElement('div');
  corner2.className = 'tw-grid-header';
  corner2.style.fontSize = '.65rem';
  table.appendChild(corner2);
  slots.forEach(s => {
    const hdr = document.createElement('div');
    hdr.className = 'tw-grid-header';
    hdr.style.fontSize = '.65rem';
    hdr.textContent = `${s.start}-${s.end}`;
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
        // Conflict: multiple in same cell
        cellPlacements.forEach(p => {
        const card = createPlacementCard(p);
        card.classList.add('critical');
        card.classList.remove('info');
        cell.appendChild(card);
      });
      }

      table.appendChild(cell);
    });
  });

  container.appendChild(table);
}

function createPlacementCard(p) {
  const card = document.createElement('div');
  card.className = 'tw-card';
  card.draggable = workspaceCanEdit() && !p.is_locked;
  card.dataset.twDraggable = 'true';
  card.dataset.placementId = p.id;
  card.style.background = courseColor(p.course_code);
  if (p.is_locked) card.classList.add('locked');

  // Check if this placement has conflicts
  const overlaps = (TW.conflicts.overlaps || []);
  const instrClashes = (TW.conflicts.instructor_clashes || []);
  const roomClashes = (TW.conflicts.room_clashes || []);
  const crossBoardClashes = activeBoardCrossBoardClashes();
  const placementId = String(p.id);
  const issueLabels = [];
  const includesPlacement = row => (row.ids || []).map(String).includes(placementId);
  const includesCrossPlacement = row => {
    const ids = row.ids || [row.placement_a_id, row.placement_b_id].filter(Boolean);
    return ids.map(String).includes(placementId);
  };
  const hasStudentClash = overlaps.some(includesPlacement);
  const hasInstructorClash = instrClashes.some(includesPlacement);
  const hasRoomIssue = roomClashes.some(includesPlacement);
  const hasCrossBoardClash = crossBoardClashes.some(includesCrossPlacement);
  if (hasStudentClash) issueLabels.push(T.overlap);
  if (hasInstructorClash) issueLabels.push(T.instrClash);
  if (hasRoomIssue) issueLabels.push(T.roomClash);
  if (hasCrossBoardClash) issueLabels.push(T.crossBoardClash);
  const hasCritical = hasStudentClash || hasInstructorClash;
  const hasWarning = hasRoomIssue;
  if (hasCritical) card.classList.add('critical');
  else if (hasWarning) card.classList.add('warning');
  else if (hasCrossBoardClash) card.classList.add('info');
  if (issueLabels.length) {
    card.classList.add('has-issue');
    card.title = issueLabels.length === 1
      ? issueLabels[0]
      : `${T.conflicts}: ${issueLabels.join(' | ')}`;
  }

  const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '';
  const courseName = compactCourseName(p.course_name);
  const showCourseName = courseName && hasDuplicateCourseCode(p.course_code);
  const identityTitle = [p.course_key, courseName].filter(Boolean).join(' | ');
  if (identityTitle) {
    card.title = [card.title, identityTitle].filter(Boolean).join(' | ');
  }
  card.innerHTML = `
    <div class="cc">${esc(p.course_code)} ${esc(p.section)}</div>
    ${globalThis.TimetableMeetingPolicy?.isEnglish(p) ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))}</div>` : ''}
    <div class="ci">${showCourseName ? esc(courseName) : esc(instructor)}${globalThis.TimetableMeetingPolicy?.isEnglish(p) ? ' | ' + esc(TimetableMeetingPolicy.roomLabel(p, IS_AR)) : p.room ? ' | ' + esc(p.room) : ''}${p.available_capacity ? ' | ' + p.available_capacity : ''}</div>
  `;

  card.addEventListener('dragstart', (e) => {
    if (!workspaceCanEdit() || p.is_locked) { e.preventDefault(); return; }
    e.dataTransfer.setData('text/plain', JSON.stringify({type:'move', placement_id: p.id}));
    e.dataTransfer.effectAllowed = 'move';
  });

  card.addEventListener('click', () => {
    TW.selectedPlacementId = p.id;
    renderRightPanel('selection');
    document.querySelectorAll('.tw-right .tw-panel-tab').forEach(t => {
      t.classList.toggle('active', t.dataset.tab === 'selection');
    });
  });

  card.addEventListener('dblclick', (e) => {
    e.stopPropagation();
    openSectionDrawer(p.id);
  });

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
    list.innerHTML = `<p class="text-t4 fs-md" style="padding:12px">${IS_AR ? 'لا توجد شعب متاحة تطابق البحث.' : 'No available sections match the filter.'}</p>`;
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
      <span class="meta">${esc(instructor)}${s.available_capacity ? ' | Cap: ' + s.available_capacity : ''}</span>
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
    more.type = 'button'; more.className = 'tw-btn';
    more.textContent = IS_AR ? `عرض المزيد (${visibleLimit}/${items.length})` : `Show more (${visibleLimit}/${items.length})`;
    more.addEventListener('click', () => { TW.unplacedVisibleLimit = visibleLimit + 100; renderUnplacedList(); });
    list.appendChild(more);
  }
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
    const overlaps = TW.conflicts.overlaps || [];
    const iClashes = TW.conflicts.instructor_clashes || [];
    const rClashes = TW.conflicts.room_clashes || [];
    const xConflictsEarly = activeBoardCrossBoardClashes();
    const roomShortages = activeBoardRoomShortages();
    const total = overlaps.length + iClashes.length + rClashes.length + xConflictsEarly.length + roomShortages.length;

    if (total === 0) {
      body.innerHTML = `<div class="tw-empty" style="padding:24px"><p>${T.noIssues}</p><small>${TW.quality?.rooms?.assessed ? '' : (IS_AR ? 'صلاحية القاعات لم تُقيّم بعد.' : 'Room suitability has not been assessed yet.')}</small></div>`;
      return;
    }

    let html = '';
    if (roomShortages.length) {
      html += `<h4 class="fs-xs text-danger">${IS_AR ? 'اجتماعات دون قاعة مناسبة' : 'Meetings without a suitable room'} (${roomShortages.length})</h4>`;
      roomShortages.forEach(room => {
          html += `<button type="button" class="tw-issue-row tw-btn" data-ids='${JSON.stringify(room.placement_ids || [])}'><span class="tw-issue-dot warning"></span><span>${esc(room.course_code || room.course_key || '')} · ${esc(room.rooms?.join(', ') || 'UNASSIGNED')}<small class="d-block text-t4">${esc((room.reasons || []).join(', '))} · ${IS_AR ? 'السعة المطلوبة' : 'Required capacity'}: ${esc(room.required_capacity ?? '—')}</small></span></button>`;
      });
    }
    if (overlaps.length) {
      html += `<div class="fs-xs fw-bold text-danger text-caps" style="margin:8px 0 4px; letter-spacing:.04em">${T.overlap} (${overlaps.length})</div>`;
      overlaps.forEach(o => {
        const secs = (o.sections || []).join(' / ');
        html += `<div class="tw-issue-row" data-ids='${JSON.stringify(o.ids || [])}'>
          <span class="tw-issue-dot critical"></span>
          <div><div class="fw-semibold">${esc(secs)}</div><div class="fs-xs text-t4">${esc(o.detail || '')}</div></div>
        </div>`;
      });
    }
    if (iClashes.length) {
      html += `<div class="fs-xs fw-bold text-danger text-caps" style="margin:8px 0 4px; letter-spacing:.04em">${T.instrClash} (${iClashes.length})</div>`;
      iClashes.forEach(c => {
        const secs = (c.sections || []).join(' / ');
        html += `<div class="tw-issue-row" data-ids='${JSON.stringify(c.ids || [])}'>
          <span class="tw-issue-dot critical"></span>
          <div><div class="fw-semibold">${esc(c.instructor || '')} &mdash; ${esc(secs)}</div><div class="fs-xs text-t4">${esc(c.detail || '')}</div></div>
        </div>`;
      });
    }
    if (rClashes.length) {
      html += `<div class="fs-xs fw-bold text-caps" style="color:var(--amber); margin:8px 0 4px; letter-spacing:.04em">${T.roomClash} (${rClashes.length})</div>`;
      rClashes.forEach(c => {
        const secs = (c.sections || []).join(' / ');
        html += `<div class="tw-issue-row" data-ids='${JSON.stringify(c.ids || [])}'>
          <span class="tw-issue-dot warning"></span>
          <div><div class="fw-semibold">${esc(c.room || '')} &mdash; ${esc(secs)}</div><div class="fs-xs text-t4">${esc(c.detail || '')}</div></div>
        </div>`;
      });
    }
    // Cross-board clashes
    const xConflicts = activeBoardCrossBoardClashes();
    if (xConflicts.length) {
      html += `<div class="fs-xs fw-bold text-caps" style="color:var(--royal); margin:8px 0 4px; letter-spacing:.04em">${T.crossBoardClash} (${xConflicts.length})</div>`;
      xConflicts.forEach(c => {
        const ids = c.ids || [c.placement_a_id, c.placement_b_id].filter(Boolean);
        const targetBoard = c.board_a_id === TW.boardId ? c.board_b_id : c.board_a_id;
        html += `<div class="tw-issue-row u-cursor-pointer" data-ids='${JSON.stringify(ids)}' data-board-target="${targetBoard}">
          <span class="tw-issue-dot info"></span>
          <div>
            <div class="fw-semibold">${esc(c.section_a)} &harr; ${esc(c.section_b)}</div>
            <div class="fs-xs text-t4">${c.overlap_count} ${IS_AR ? 'طلاب يطلبون المقررين المتداخلين' : 'students requesting both overlapping courses'} | ${esc(c.board_a_label)} &harr; ${esc(c.board_b_label)}</div>
          </div>
        </div>`;
      });
    }

    body.innerHTML = html;

    // Wire issue rows to highlight cards on grid
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
      body.innerHTML = `<div class="tw-empty" style="padding:24px"><p>${IS_AR ? 'اختر شعبة' : 'Click a section to inspect'}</p></div>`;
      return;
    }
    const p = TW.placements.find(x => x.id === TW.selectedPlacementId);
    if (!p) { body.innerHTML = ''; return; }
    const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '-';
    body.innerHTML = `
      <div class="tw-sel-field"><span class="label">Course</span><span class="value">${esc(p.course_code)}</span></div>
      <div class="tw-sel-field"><span class="label">Section</span><span class="value">${esc(p.section)}</span></div>
      ${globalThis.TimetableMeetingPolicy?.isEnglish(p) ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))} · SUN / MON / WED / THU</div><button type="button" class="tw-pattern-action" data-tw-mutation ${workspaceCanEdit() ? '' : 'disabled'} onclick="chooseEnglishWorkspacePattern({type:'move',placement_id:${p.id}})">${IS_AR ? 'تغيير نمط الشعبة' : 'Change section pattern'}</button>` : ''}
      <div class="tw-sel-field"><span class="label">Day</span><span class="value">${esc(p.day)}</span></div>
      <div class="tw-sel-field"><span class="label">Time</span><span class="value">${esc(p.start_time)} - ${esc(p.end_time)}</span></div>
      <div class="tw-sel-field"><span class="label">Room</span><span class="value">${esc(globalThis.TimetableMeetingPolicy?.roomLabel(p, IS_AR, '-') || p.room || '-')}</span></div>
      <div class="tw-sel-field"><span class="label">Instructor</span><span class="value">${esc(instructor)}</span></div>
      <div class="tw-sel-field"><span class="label">Capacity</span><span class="value">${p.available_capacity || '-'}</span></div>
      <div class="tw-sel-field"><span class="label">Locked</span><span class="value">${p.is_locked ? 'Yes' : 'No'}</span></div>
      <div class="d-flex" style="margin-top:12px; gap:6px">
        <button class="tw-btn" data-tw-mutation ${workspaceCanEdit() ? '' : 'disabled'} title="${esc(T.lockHelp)}" onclick="toggleLock(${p.id})">${p.is_locked ? (IS_AR ? 'فتح' : 'Unlock') : (IS_AR ? 'قفل' : 'Lock')}</button>
        <button class="tw-btn text-danger" data-tw-mutation ${workspaceCanEdit() ? '' : 'disabled'} onclick="removePlacement(${p.id})">${IS_AR ? 'إزالة' : 'Remove'}</button>
      </div>
    `;
  } else if (tab === 'capacity') {
    const courses = (TW.capacityData && TW.capacityData.courses) || [];
    if (courses.length === 0) {
      body.innerHTML = `<div class="tw-empty" style="padding:24px"><p>${IS_AR ? 'لا توجد بيانات سعة' : 'No capacity data'}</p></div>`;
      return;
    }
    const t = (TW.capacityData && TW.capacityData.totals) || {};
    let html = `<div class="d-flex flex-wrap" style="gap:12px; margin-bottom:10px">
      <div class="tw-metric" style="text-align:left"><div class="num" style="font-size:1rem">${t.demand || 0}</div><div class="lbl">${IS_AR ? 'الطلب' : 'Demand'}</div></div>
      <div class="tw-metric" style="text-align:left"><div class="num" style="font-size:1rem">${t.raw_capacity || 0}</div><div class="lbl">${IS_AR ? 'السعة' : 'Raw Cap.'}</div></div>
      <div class="tw-metric" style="text-align:left"><div class="num${(t.deficit||0) > 0 ? ' danger' : ''}" style="font-size:1rem">${t.deficit || 0}</div><div class="lbl">${IS_AR ? 'العجز' : 'Deficit'}</div></div>
    </div>`;
    courses.slice(0, 30).forEach(c => {
      const pct = c.demand > 0 ? Math.min(100, Math.round((c.raw_capacity / c.demand) * 100)) : 100;
      const barColor = c.deficit > 0 ? 'var(--danger)' : 'var(--teal)';
      html += `<div style="margin-bottom:6px">
        <div class="d-flex justify-content-between fs-12">
          <span class="fw-semibold">${esc(c.course_code)}</span>
          <span class="text-t4">${c.demand} / ${c.raw_capacity}${c.deficit > 0 ? ' <span class="text-danger fw-bold">-' + c.deficit + '</span>' : ''}</span>
        </div>
        <div style="height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin-top:2px">
          <div style="height:100%;width:${pct}%;background:${barColor};border-radius:2px"></div>
        </div>
      </div>`;
    });
    if (courses.length > 30) html += `<p class="fs-xs text-t4">${IS_AR ? 'وأكثر...' : `+${courses.length - 30} more...`}</p>`;
    body.innerHTML = html;
  } else if (tab === 'mri') {
    renderMriPanel(body);
  }
}

function renderMriPanel(body) {
  const activeBoard = TW.boards.find(b => b.id === TW.boardId) || {};
  const overlaps = TW.conflicts.overlaps || [];
  const iClashes = TW.conflicts.instructor_clashes || [];
  const rClashes = TW.conflicts.room_clashes || [];
  const crossBoard = activeBoardCrossBoardClashes();
  const roomShortages = activeBoardRoomShortages();
  const roomsAssessed = Boolean(TW.quality?.rooms?.assessed);
  const impact = TW.conflicts.student_impact || {};
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
  const critical = overlaps.length + iClashes.length;
  const warnings = rClashes.length + crossBoard.length + deficitCourses.length + roomShortages.length;
  const affected = impact.affected_count || 0;
  const blocked = impact.blocked_count || 0;
  const pressure = Math.min(100, Math.round(
    overlaps.length * 28 +
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
      tone: overlaps.length ? 'critical' : 'stable',
      detail: `${overlaps.length} same-board time clashes`,
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
      tone: iClashes.length ? 'critical' : (rClashes.length || roomShortages.length || !roomsAssessed) ? 'watch' : 'stable',
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
      title: room.course_code || room.course_key || room.room || 'UNASSIGNED',
      detail: `${(room.reasons || []).join(', ')}; required capacity ${room.required_capacity ?? '—'}` })),
    ...overlaps.map(o => ({
      kind: 'Same-board time clash',
      tone: 'critical',
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

function activeBoardCrossBoardClashes() {
  const boardId = String(TW.boardId);
  return (TW.conflicts.cross_board_conflicts || []).filter(c =>
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
  if (!TW.scenario) return;
  if (TW.scenario.status === 'published') {
    badge.className = 'tw-badge tw-badge-ok';
    badge.textContent = T.published;
  } else {
    badge.className = 'tw-badge tw-badge-draft';
    badge.textContent = T.draft;
  }
}

function updateQuickStats() {
  $('qsPlaced').textContent = TW.placements.length;
  const critical = (TW.conflicts.summary || {}).critical || 0;
  $('qsConflicts').textContent = critical;
  renderWorkspaceQuality();
}

let qualityLoadRevision = 0;
async function refreshWorkspaceQuality() {
  const scenarioId = TW.scenarioId;
  const revision = ++qualityLoadRevision;
  if (!scenarioId) { TW.quality = null; renderWorkspaceQuality(); return; }
  if ($('twQualityStatus')) $('twQualityStatus').textContent = IS_AR ? 'جاري تقييم جودة السيناريو…' : 'Evaluating scenario quality…';
  const data = await twFetch(`/ops/tw/scenarios/${scenarioId}/quality/`);
  if (revision !== qualityLoadRevision || TW.scenarioId !== scenarioId) return;
  TW.quality = data?.ok ? data : null;
  renderWorkspaceQuality();
  const active = document.querySelector('.tw-right .tw-panel-tab.active');
  if (active) renderRightPanel(active.dataset.tab);
}
function renderWorkspaceQuality() {
  const quality = TW.quality;
  const put = (id, value, danger = false) => {
    const el = $(id);
    if (!el) return;
    el.textContent = Number.isFinite(value) ? value.toLocaleString() : '—';
    el.classList.toggle('danger', Boolean(danger));
  };
  const issues = TW.conflicts.summary?.critical;
  put('twQualityPlaced', TW.boardId ? TW.placements.length : null);
  put('twQualityConflicts', TW.boardId ? issues : null, issues > 0);
  put('twQualityInstructorDays', quality?.instructor?.assessed ? quality.instructor.working_days : null);
  put('twQualityInstructorIdle', quality?.instructor?.assessed ? quality.instructor.idle_minutes : null);
  put('twQualityUnplaced', quality?.soft_seats?.assessed ? quality.soft_seats.unresolved : null, quality?.soft_seats?.unresolved > 0);
  put('twQualityRooms', quality?.rooms?.assessed ? quality.rooms.shortages : null, quality?.rooms?.shortages > 0);
  // One teacher per section: shown only when the quality endpoint reports it;
  // red when a section lacks a teacher for more than the D10 limit's reasons.
  const coverage = quality?.instructor?.coverage;
  const coverageRow = $('twQualityTeacherCoverageRow');
  if (coverageRow) coverageRow.hidden = !coverage;
  const coverageCell = $('twQualityTeacherCoverage');
  if (coverage && coverageCell) {
    coverageCell.textContent = `${(coverage.assigned || 0).toLocaleString()} / ${((coverage.assigned || 0) + (coverage.tbd || 0)).toLocaleString()}`;
    coverageCell.title = teacherCoverageText(coverage);
    coverageCell.classList.toggle('danger', (coverage.tbd || 0) > (coverage.tbd_d10_floor || 0));
  }
  const status = $('twQualityStatus');
  if (!status) return;
  if (!TW.scenarioId) { status.textContent = IS_AR ? 'اختر سيناريو لعرض جودة الجدول.' : 'Select a scenario to view timetable quality.'; return; }
  if (!quality) { status.textContent = IS_AR ? 'التقييم غير متاح؛ — تعني غير معروف.' : 'Assessment unavailable; — means unknown.'; return; }
  const clashes = quality.assigned_student_clashes;
  const seating = clashes?.assessed
    ? `${IS_AR ? 'تعارضات التعيين المُقيّم' : 'Evaluated seating clashes'}: ${clashes.count}.`
    : (IS_AR ? 'تعيين الطلاب غير مُقيّم؛ عدد التعارضات غير معروف.' : 'Student seating not assessed; clash count unknown.');
  status.textContent = (IS_AR ? 'المقاييس للسيناريو كاملاً؛ الاجتماعات والمشاكل الحرجة للوحة الحالية. ' : 'Scenario totals; meetings and critical issues refer to the selected board. ') + seating;
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
    if (activeTab && (activeTab.dataset.tab === 'capacity' || activeTab.dataset.tab === 'mri')) {
      renderRightPanel(activeTab.dataset.tab);
    }
    // Also update bottom demand tab if open
    const activeBottom = document.querySelector('.tw-bottom .tw-panel-tab.active');
    if (activeBottom && activeBottom.dataset.tab === 'demand') renderBottomPanel('demand');
  }
}

/* ── Bottom Panel ── */
function renderBottomPanel(tab) {
  const body = $('twBottomBody');

  if (tab === 'demand') {
    const courses = (TW.capacityData && TW.capacityData.courses) || [];
    if (courses.length === 0) {
      body.innerHTML = `<p class="text-t4" style="font-size:.8rem; padding:12px">${IS_AR ? 'لا توجد بيانات' : 'No capacity data yet'}</p>`;
      return;
    }
    let html = `<table class="w-100 fs-12" style="border-collapse:collapse">
      <thead><tr style="border-bottom:2px solid var(--line);text-align:left">
        <th style="padding:6px 8px">${IS_AR ? 'المقرر' : 'Course'}</th>
        <th style="padding:6px 8px">${IS_AR ? 'الطلب' : 'Demand'}</th>
        <th style="padding:6px 8px">${IS_AR ? 'السعة' : 'Raw Cap.'}</th>
        <th style="padding:6px 8px">${IS_AR ? 'الشعب' : 'Sections'}</th>
        <th style="padding:6px 8px">${IS_AR ? 'العجز' : 'Deficit'}</th>
      </tr></thead><tbody>`;
    courses.forEach(c => {
      const defStyle = c.deficit > 0 ? 'color:var(--danger);font-weight:700' : '';
      html += `<tr style="border-bottom:1px solid var(--line)">
        <td class="fw-semibold" style="padding:4px 8px">${esc(c.course_code)}</td>
        <td style="padding:4px 8px">${c.demand}</td>
        <td style="padding:4px 8px">${c.raw_capacity}</td>
        <td style="padding:4px 8px">${c.placed_sections}</td>
        <td style="padding:4px 8px;${defStyle}">${c.deficit > 0 ? '-' + c.deficit : '0'}</td>
      </tr>`;
    });
    const t = (TW.capacityData && TW.capacityData.totals) || {};
    html += `</tbody><tfoot><tr class="fw-bold" style="border-top:2px solid var(--line)">
      <td style="padding:6px 8px">${IS_AR ? 'الإجمالي' : 'Total'}</td>
      <td style="padding:6px 8px">${t.demand || 0}</td>
      <td style="padding:6px 8px">${t.raw_capacity || 0}</td>
      <td style="padding:6px 8px">${t.course_count || 0}</td>
      <td style="padding:6px 8px;${(t.deficit||0) > 0 ? 'color:var(--danger)' : ''}">${(t.deficit||0) > 0 ? '-' + t.deficit : '0'}</td>
    </tr></tfoot></table>`;
    body.innerHTML = html;

  } else if (tab === 'resources') {
    const iClashes = TW.conflicts.instructor_clashes || [];
    const rClashes = TW.conflicts.room_clashes || [];
    if (iClashes.length === 0 && rClashes.length === 0) {
      body.innerHTML = `<p class="text-t4" style="font-size:.8rem; padding:12px">${IS_AR ? 'لا توجد تعارضات موارد' : 'No resource conflicts'}</p>`;
      return;
    }
    let html = '';
    if (iClashes.length) {
      html += `<div class="fs-sm fw-bold text-danger text-caps" style="margin:8px 8px 4px">${T.instrClash} (${iClashes.length})</div>`;
      html += `<table class="w-100 fs-12" style="border-collapse:collapse"><tbody>`;
      iClashes.forEach(c => {
        html += `<tr style="border-bottom:1px solid var(--line)">
          <td class="fw-semibold" style="padding:4px 8px">${esc(c.instructor)}</td>
          <td style="padding:4px 8px">${(c.sections||[]).join(', ')}</td>
          <td class="text-t4" style="padding:4px 8px">${esc(c.detail)}</td>
        </tr>`;
      });
      html += `</tbody></table>`;
    }
    if (rClashes.length) {
      html += `<div class="fs-sm fw-bold text-caps" style="color:var(--amber); margin:8px 8px 4px">${T.roomClash} (${rClashes.length})</div>`;
      html += `<table class="w-100 fs-12" style="border-collapse:collapse"><tbody>`;
      rClashes.forEach(c => {
        html += `<tr style="border-bottom:1px solid var(--line)">
          <td class="fw-semibold" style="padding:4px 8px">${esc(c.room)}</td>
          <td style="padding:4px 8px">${(c.sections||[]).join(', ')}</td>
          <td class="text-t4" style="padding:4px 8px">${esc(c.detail)}</td>
        </tr>`;
      });
      html += `</tbody></table>`;
    }
    body.innerHTML = html;
  }
}

// Wire bottom panel tabs
document.querySelectorAll('.tw-bottom .tw-panel-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tw-bottom .tw-panel-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    $('twBottom').classList.add('open');
    renderBottomPanel(tab.dataset.tab);
  });
});

/* ── Section Detail Drawer ── */
function openSectionDrawer(placementId) {
  const p = TW.placements.find(x => x.id === placementId);
  if (!p) return;

  // Remove existing drawer
  const old = document.getElementById('twDrawer');
  if (old) old.remove();

  const instructor = (p.meetings && p.meetings[0]) ? p.meetings[0].instructor : '-';
  const meetingsHtml = (p.meetings || []).map(m =>
    `<div class="fs-12" style="padding:2px 0">${esc(m.day)} ${esc(m.start_time)}-${esc(m.end_time)} | ${esc(globalThis.TimetableMeetingPolicy?.isEnglish(p) ? TimetableMeetingPolicy.roomLabel(p, IS_AR) : m.room || '-')} | ${esc(m.instructor || '-')}</div>`
  ).join('');

  const drawer = document.createElement('div');
  drawer.id = 'twDrawer';
  drawer.style.cssText = 'position:fixed;top:0;right:0;width:380px;height:100vh;background:var(--card);border-left:1px solid var(--line);box-shadow:var(--shadow-lift);z-index:200;overflow-y:auto;padding:20px;transition:transform .2s var(--ease);';
  drawer.innerHTML = `
    <div class="d-flex justify-content-between align-items-center" style="margin-bottom:16px">
      <h3 style="margin:0;font-size:.95rem;color:var(--ink)">${IS_AR ? 'تفاصيل الشعبة' : 'Section Detail'}</h3>
      <button class="u-cursor-pointer text-t4" onclick="document.getElementById('twDrawer').remove()" style="background:none; border:none; font-size:1.2rem">&times;</button>
    </div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'المقرر' : 'Course'}</span><span class="value">${esc(p.course_code)}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'الاسم' : 'Name'}</span><span class="value">${esc(p.course_name)}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'الشعبة' : 'Section'}</span><span class="value">${esc(p.section)}</span></div>
    ${globalThis.TimetableMeetingPolicy?.isEnglish(p) ? `<div class="tw-pattern-label">${esc(TimetableMeetingPolicy.label(p, IS_AR))} · SUN / MON / WED / THU</div><button type="button" class="tw-pattern-action" data-tw-mutation ${workspaceCanEdit() ? '' : 'disabled'} onclick="chooseEnglishWorkspacePattern({type:'move',placement_id:${p.id}})">${IS_AR ? 'تغيير نمط الشعبة' : 'Change section pattern'}</button>` : ''}
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'اليوم' : 'Day'}</span><span class="value">${esc(p.day)}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'الوقت' : 'Time'}</span><span class="value">${esc(p.start_time)} - ${esc(p.end_time)}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'القاعة' : 'Room'}</span><span class="value">${esc(globalThis.TimetableMeetingPolicy?.roomLabel(p, IS_AR, '-') || p.room || '-')}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'المدرس' : 'Instructor'}</span><span class="value">${esc(instructor)}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'السعة' : 'Capacity'}</span><span class="value">${p.available_capacity || '-'}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'المسجلون' : 'Registered'}</span><span class="value">${p.registered_count || '-'}</span></div>
    <div class="tw-sel-field"><span class="label">${IS_AR ? 'مقفل' : 'Locked'}</span><span class="value">${p.is_locked ? (IS_AR ? 'نعم' : 'Yes') : (IS_AR ? 'لا' : 'No')}</span></div>
    <div style="margin-top:12px"><strong class="fs-md text-t3">${IS_AR ? 'جميع اللقاءات' : 'All Meetings'}</strong>${meetingsHtml || '<div class="fs-12 text-t4">-</div>'}</div>
    <div class="d-flex" style="margin-top:16px; gap:8px">
      <button class="tw-btn" data-tw-mutation ${workspaceCanEdit() ? '' : 'disabled'} title="${esc(T.lockHelp)}" onclick="toggleLock(${p.id});document.getElementById('twDrawer').remove()">${p.is_locked ? (IS_AR ? 'فتح القفل' : 'Unlock') : (IS_AR ? 'قفل' : 'Lock')}</button>
      <button class="tw-btn text-danger" data-tw-mutation ${workspaceCanEdit() ? '' : 'disabled'} onclick="removePlacement(${p.id});document.getElementById('twDrawer').remove()">${IS_AR ? 'إزالة' : 'Remove'}</button>
    </div>
  `;
  document.body.appendChild(drawer);

  // Close on Escape
  const closeOnEsc = (e) => { if (e.key === 'Escape') { drawer.remove(); document.removeEventListener('keydown', closeOnEsc); } };
  document.addEventListener('keydown', closeOnEsc);
}

/* ── Slot Edit Modal ── */
async function openSlotEditor() {
  if (!TW.scenarioId || !TW.scenario) { notify.error(IS_AR ? 'اختر سيناريو أولا' : 'Select a scenario first'); return; }
  if (TW.scenario.status === 'published') { notify.error(IS_AR ? 'السيناريو منشور' : 'Cannot edit published scenario'); return; }

  const slots = TW.scenario.slot_config || [];
  const labSlots = TW.scenario.lab_slot_config || [];
  const slotsText = slots.map(s => `${s.label || ''}\t${s.start}\t${s.end}`).join('\n')
    || '09:00-10:15\t09:00\t10:15\n10:30-11:45\t10:30\t11:45\n10:50-12:05\t10:50\t12:05\n13:00-14:15\t13:00\t14:15\n14:30-15:45\t14:30\t15:45\n14:45-16:00\t14:45\t16:00\n16:00-17:15\t16:00\t17:15';
  const labText = labSlots.map(s => `${s.label || ''}\t${s.start}\t${s.end}`).join('\n')
    || 'Lab 1\t09:00\t10:40\nLab 2\t10:45\t12:25\nLab 3\t13:00\t14:40\nLab 4\t14:45\t16:25\nLab 5\t16:30\t18:10\nLab 6\t18:10\t19:50';

  const result = await dlg.confirm({
    title: IS_AR ? 'تعديل الفترات الزمنية' : 'Edit Time Slots',
    body: `<div class="fs-md text-t3 fw-semibold" style="margin-bottom:6px">${IS_AR ? '📚 المحاضرات (75 دقيقة)' : '📚 Lecture Slots (75 min)'}</div>
      <div class="fs-sm text-t4" style="margin-bottom:4px">${IS_AR ? 'الاسم [تبويب] البداية [تبويب] النهاية' : 'Label [tab] Start [tab] End'}</div>
      <textarea class="w-100 font-mono fs-md" id="twSlotInput" rows="6" style="padding:8px; border:1px solid var(--line); border-radius:var(--radius-sm); background:var(--bg); color:var(--ink); resize:vertical">${esc(slotsText)}</textarea>
      <div class="fs-md text-teal fw-semibold" style="margin:10px 0 6px">${IS_AR ? '🔬 المعامل (100 دقيقة)' : '🔬 Lab Slots (100 min)'}</div>
      <textarea class="w-100 font-mono fs-md" id="twLabSlotInput" rows="6" style="padding:8px; border:1px solid var(--line); border-radius:var(--radius-sm); background:var(--bg); color:var(--ink); resize:vertical">${esc(labText)}</textarea>`,
    confirmText: IS_AR ? 'حفظ' : 'Save Slots',
    kind: 'info',
  });

  if (!result) return;

  // Parse the textarea
  const textarea = document.getElementById('twSlotInput');
  if (!textarea) return;
  const lines = textarea.value.trim().split('\n').filter(l => l.trim());
  const newSlots = [];
  for (const line of lines) {
    const parts = line.split('\t').map(s => s.trim());
    if (parts.length >= 3) {
      newSlots.push({ label: parts[0], start: parts[1], end: parts[2] });
    } else if (parts.length === 2) {
      newSlots.push({ label: `${parts[0]}-${parts[1]}`, start: parts[0], end: parts[1] });
    } else {
      // Try splitting by space or dash
      const m = line.trim().match(/^(\S+)\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})$/);
      if (m) newSlots.push({ label: m[1], start: m[2], end: m[3] });
    }
  }

  if (newSlots.length === 0) { notify.error(IS_AR ? 'لم يتم قراءة فترات' : 'No valid slots parsed'); return; }

  // Parse lab slots textarea
  const labTextarea = document.getElementById('twLabSlotInput');
  const newLabSlots = [];
  if (labTextarea) {
    const labLines = labTextarea.value.trim().split('\n').filter(l => l.trim());
    for (const line of labLines) {
      const parts = line.split('\t').map(s => s.trim());
      if (parts.length >= 3) {
        newLabSlots.push({ label: parts[0], start: parts[1], end: parts[2] });
      } else if (parts.length === 2) {
        newLabSlots.push({ label: `${parts[0]}-${parts[1]}`, start: parts[0], end: parts[1] });
      } else {
        const m = line.trim().match(/^(\S+)\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})$/);
        if (m) newLabSlots.push({ label: m[1], start: m[2], end: m[3] });
      }
    }
  }

  const data = await twFetch(`/ops/tw/scenarios/${TW.scenarioId}/slots/update/`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ slot_config: newSlots, lab_slot_config: newLabSlots }),
  });
  if (!data) return;

  TW.scenario = data.scenario;
  notify.success(IS_AR ? 'تم تحديث الفترات' : `${newSlots.length} lecture + ${newLabSlots.length} lab slots saved`);
  renderGrid();
}

/* ── Elective Mapping Dialog ── */
$('twMapElectives').addEventListener('click', async () => {
  const year = $('twYear').value.trim();
  const term = $('twTerm').value.trim();
  const programme = $('twProgram').value.trim().toUpperCase().split(',')[0].trim();

  if (!year || !term || !programme) {
    notify.error(IS_AR ? 'أدخل السنة والفصل والبرنامج أولا' : 'Enter Year, Term, and Program first');
    return;
  }

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
  btn.disabled = true;
  menuBtn.disabled = true;
  const origHtml = btn.innerHTML;
  const modeLabel = mode === 'full' ? (IS_AR ? 'إعادة بناء...' : 'Rebuilding...') : (IS_AR ? 'جارٍ التحسين...' : 'Optimising...');
  btn.innerHTML = `&#x23F3; ${modeLabel}`;

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
    const data = await runOptimiseRequest(mode, payload, btn, modeLabel, token);

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
    btn.innerHTML = origHtml;

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
        $('twCmdBar').style.display = 'flex';
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

/* ── Fullscreen: redirect to split-pane workspace ── */
(function(){
  const btn = document.getElementById('twFullscreen');
  if(!btn) return;
  // When clicked, navigate to /timetable-workspace/split/ preserving the
  // active scenario and board so the split view opens pre-loaded.
  btn.addEventListener('click', function(e){
    e.preventDefault();
    const params = new URLSearchParams();
    if (TW.scenarioId) params.set('scenario', String(TW.scenarioId));
    if (TW.boardId) params.set('board', String(TW.boardId));
    const q = params.toString();
    window.location.href = '/timetable-workspace/split/' + (q ? '?' + q : '');
  });
})();
