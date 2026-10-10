const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const sources = Object.fromEntries(['page-timetable-workspace.js', 'page-timetable-workspace-split.js']
  .map(name => [name, fs.readFileSync(path.join(__dirname, '../../static/js', name), 'utf8')]));
const slice = (source, begin, end) => source.slice(source.indexOf(begin), source.indexOf(end, source.indexOf(begin)));
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function harness(name, arabic = false) {
  const source = sources[name];
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, { value: '', innerHTML: '', textContent: '', style: {}, classList: { remove: () => {} }, appendChild: () => {}, querySelector: () => null });
    return elements.get(id);
  };
  const dialogs = [], notices = { error: [], success: [], warning: [] };
  const context = vm.createContext({
    IS_AR: arabic, $, esc: escapeHtml, Number, console,
    document: { getElementById: $, createElement: () => ({}) },
    window: { dispatchEvent: () => {} }, CustomEvent: class {},
    notify: Object.fromEntries(Object.keys(notices).map(kind => [kind, message => notices[kind].push(message)])),
    openModal: opts => dialogs.push(opts),
    dlg: { confirm: async opts => { dialogs.push(opts); return true; } },
    TW: { scenarioId: 1 }, S: { scenarioMeta: { name: 'Test' } },
    updateUndoRedoButtons: () => {}, onScenarioChange: async () => {},
    updateValidationBadge: () => {}, renderBoardsNav: () => {}, renderBoardTabs: () => {}, renderBudgetSidebar: () => {},
    DEFAULT_YEAR: '1448', DEFAULT_TERM: '1', loadScenarios: async () => {},
    workspaceCanEdit: () => true, beginWorkspaceOperation: () => ({ scenarioId: 1 }),
    syncWorkspaceControls: () => {}, announcePlannerJob: () => {}, refreshWorkspaceQuality: async () => {},
    finishWorkspaceOperation: () => { $('twGenerate').disabled = false; $('twOptimise').disabled = false; $('twOptimiseMenu').disabled = false; },
    ACTIVE_PLANNER_STATUSES: new Set(['queued', 'running', 'unknown']),
  });
  vm.runInContext(slice(source, '/* Timetable outcome helpers', '/* End timetable outcome helpers. */'), context);
  if (name.endsWith('-split.js')) {
    vm.runInContext(slice(source, 'function showOptimiseResults(', 'async function doPublish('), context);
    vm.runInContext(slice(source, 'async function openGenerateModal(', '/* ── New empty scenario'), context);
  } else {
    vm.runInContext(slice(source, 'async function runOptimiseV2(', '/* ── Fullscreen:'), context);
    vm.runInContext(slice(source, "async function runGenerate(engine =", '/* debounce()'), context);
  }
  return { context, $, dialogs, notices };
}

function result(worseStudentGap = false) {
  return {
    mode: 'current', total_students: 1, unresolved_students: 0,
    baseline_score: [0, 0, 0, 0, 15, 0], final_score: [0, 0, 0, 0, worseStudentGap ? 105 : 15, 0],
    best_candidate_id: 'test', elapsed_seconds: 2, optimisation_policy: 'instructor_first',
    instructor_schedule_metric: {
      before: { working_days: 3, idle_minutes: 105 }, after: { working_days: 2, idle_minutes: 15 },
    },
  };
}

for (const name of Object.keys(sources)) {
  test(`${name} room-only recovery is applied and includes undersized rooms`, async () => {
    const h = harness(name), outcome = result();
    outcome.instructor_schedule_metric.after = { ...outcome.instructor_schedule_metric.before };
    outcome.secondary_recovery = {
      applied: true, soft_unresolved_before: 4, soft_unresolved_after: 4,
      room_shortages_before: 3, room_shortages_after: 2,
    };
    if (name.endsWith('-split.js')) h.context.showOptimiseResults(outcome, 'current');
    else {
      h.context.runOptimiseRequest = async () => ({ ok: true, optimisation: outcome });
      await h.context.runOptimiseV2('current');
    }
    assert.match(h.dialogs[0].body, /Board [Ii]mproved/);
    assert.match(h.dialogs[0].body, /Meetings without a suitable room: 3 → 2/);
    assert.match(h.dialogs[0].body, /Room shortages include undersized rooms/);
  });
  test(`${name} discarded secondary candidate has no applied recovery claim`, () => {
    const h = harness(name), outcome = { ...result(), safety_blocked: true,
      secondary_recovery: { applied: true, soft_unresolved_before: 17, soft_unresolved_after: 4,
        room_shortages_before: 3, room_shortages_after: 2 } };
    assert.equal(h.context.renderSecondaryRecoveryOutcome(outcome), '');
    outcome.safety_blocked = false;
    outcome.secondary_recovery.applied = false;
    assert.equal(h.context.renderSecondaryRecoveryOutcome(outcome), '');
  });
  for (const arabic of [false, true]) {
    test(`${name} ${arabic ? 'ar' : 'en'} reports teacher gain despite worse student gap`, async () => {
      const h = harness(name, arabic), outcome = result(true);
      if (name.endsWith('-split.js')) h.context.showOptimiseResults(outcome, 'current');
      else {
        h.context.runOptimiseRequest = async () => ({ ok: true, optimisation: outcome });
        await h.context.runOptimiseV2('current');
      }
      assert.deepEqual(h.notices.error, []);
      const body = h.dialogs[0].body;
      assert.match(body, /3 → 2/);
      assert.match(body, /105 → 15/);
      assert.match(body, arabic ? /تم التحسين/ : /Board [Ii]mproved/);
      assert.match(body, arabic ? /الأولوية/ : /Priority: fewer instructor days/);
      assert.doesNotMatch(body, /already optimal|No automatic improvement/);
      assert.equal(h.dialogs[0].confirmText || h.dialogs[0].buttons[0].label, arabic ? 'إغلاق' : 'Close');
    });
  }
  test(`${name} unknown seating never becomes zero or a success`, () => {
    const h = harness(name);
    const text = h.context.schedulerOutcomeText({ expected_clashes: 0, student_seating: {
      solution_found: false, unseated_demands: null, total_clashes: null, status: 'UNKNOWN',
    } });
    assert.match(text, /not confirmed/);
    assert.match(text, /missing seats and clashes are unknown/);
    assert.match(text, /Estimated overlaps \(proxy\): 0/);
    assert.doesNotMatch(text, /missing seats 0|clashes 0|optimum proven/);
  });
  test(`${name} feasible seating is distinguished from proven optimal seating`, () => {
    const h = harness(name);
    const seating = { solution_found: true, proven_optimal: false, students: 10,
      students_fully_seated: 9, unseated_demands: 2, total_clashes: 1 };
    const text = h.context.schedulerSeatingText({ student_seating: seating });
    assert.match(text, /feasible solution; optimality not proven/);
    assert.match(text, /fully seated students 9\/10; missing seats 2; clashes 1/);
    seating.proven_optimal = true;
    assert.match(h.context.schedulerSeatingText({ student_seating: seating }), /optimum proven/);
  });
  test(`${name} blocked teacher candidate is not called improved`, async () => {
    const h = harness(name), outcome = { ...result(), safety_blocked: true };
    if (name.endsWith('-split.js')) h.context.showOptimiseResults(outcome, 'current');
    else {
      h.context.runOptimiseRequest = async () => ({ ok: true, optimisation: outcome });
      await h.context.runOptimiseV2('current');
    }
    assert.deepEqual(h.notices.error, []);
    assert.match(h.dialogs[0].body, /Result not applied/);
    assert.doesNotMatch(h.dialogs[0].body, /Board [Ii]mproved/);
  });
}

test('split Generate waits for portfolio job, reports seating, and keeps results open', async () => {
  const h = harness('page-timetable-workspace-split.js');
  const requests = [], progress = [];
  h.context.api = async (url, opts) => {
    requests.push({ url, body: opts?.body ? JSON.parse(opts.body) : null });
    if (url === '/ops/tw/generate-workspace/') return { scenario: { id: 4 }, boards: [], build_job_id: 'job-4' };
    if (url === '/planner-jobs/job-4/result/') return { result: { instructor_days: 2,
      instructor_idle_minutes: 15, student_seating: { solution_found: true, proven_optimal: false,
        students: 2, students_fully_seated: 1, unseated_demands: 1, total_clashes: 0 } } };
    assert.fail(`Unexpected request ${url}`);
  };
  h.context.pollPlannerJob = async (id, tick) => {
    assert.equal(id, 'job-4');
    assert.equal(h.$('twsGenSubmit').disabled, true);
    tick({ last_stage_seen: 'portfolio run 2' }, 190000);
    progress.push(h.$('twsGenStatus').textContent);
    return { status: 'succeeded' };
  };
  await h.context.openGenerateModal();
  assert.match(h.dialogs[0].body, /portfolio of 7 strategies/);
  assert.match(h.dialogs[0].body, /multiple-run portfolio/);
  for (const [id, value] of Object.entries({ twsGenYear: '1448', twsGenSem: '1', twsGenProgram: 'AI',
    twsGenSection: 'M', twsGenStrategy: 'compact', twsGenEngine: 'scheduler' })) h.$(id).value = value;
  const keepOpen = await h.dialogs[0].buttons[1].onClick({});
  assert.equal(keepOpen, false);
  assert.equal(requests[0].body.defer_build, true);
  assert.equal(requests[0].body.engine, 'scheduler');
  assert.match(progress[0], /190s · portfolio run 2/);
  assert.match(h.dialogs[1].body, /missing seats 1; clashes 0/);
  assert.equal(h.$('twsGenSubmit').disabled, false);
  assert.deepEqual(h.notices.error, []);
});

for (const name of Object.keys(sources)) {
  test(`${name} teacher-only gain is visible with unchanged public student score`, async () => {
    const h = harness(name), outcome = result();
    if (name.endsWith('-split.js')) h.context.showOptimiseResults(outcome, 'current');
    else {
      h.context.runOptimiseRequest = async () => ({ ok: true, optimisation: outcome });
      await h.context.runOptimiseV2('current');
    }
    assert.match(h.dialogs[0].body, /Board [Ii]mproved/);
    assert.doesNotMatch(h.dialogs[0].body, /already optimal|No automatic improvement/);
  });
  for (const engine of ['classic', 'scheduler']) {
    test(`${name} completed sync ${engine} build renders build_summary without polling`, async () => {
      const h = harness(name);
      const summary = engine === 'classic' ? result(true) : { instructor_days: 2, instructor_idle_minutes: 15,
        student_seating: { solution_found: true, proven_optimal: true, students: 2, students_fully_seated: 2,
          unseated_demands: 0, total_clashes: 0 } };
      const response = { scenario: { id: 4, name: 'Generated' }, boards: [], build_job_id: null, build_summary: summary };
      h.context.api = h.context.twFetch = async url => {
        assert.equal(url, '/ops/tw/generate-workspace/');
        return response;
      };
      h.context.pollPlannerJob = async () => assert.fail('completed sync build must not poll');
      let body;
      if (name.endsWith('-split.js')) {
        await h.context.openGenerateModal();
        for (const [id, value] of Object.entries({ twsGenYear: '1448', twsGenSem: '1', twsGenProgram: 'AI',
          twsGenSection: 'M', twsGenStrategy: 'compact', twsGenEngine: engine })) h.$(id).value = value;
        assert.equal(await h.dialogs[0].buttons[1].onClick({}), false);
        body = h.dialogs[1].body;
      } else {
        for (const [id, value] of Object.entries({ twYear: '1448', twTerm: '1', twProgram: 'AI', twSection: 'M', twStrategy: 'compact' })) h.$(id).value = value;
        await h.context.runGenerate(engine);
        body = h.$('twGenStatus').innerHTML || h.$('twGenStatus').textContent;
      }
      assert.match(body, engine === 'classic' ? /3 → 2/ : /optimum proven.*missing seats 0; clashes 0/);
      assert.deepEqual(h.notices.error, []);
      assert.deepEqual(h.notices.warning, []);
      assert.ok(h.notices.success.some(message => message.includes('Generated + built')));
    });
  }
  test(`${name} dispatch failure does not claim the scaffold is a built timetable`, async () => {
    const h = harness(name);
    h.context.api = h.context.twFetch = async () => ({ scenario: { id: 4, name: 'Scaffold' }, boards: [], build_job_id: null });
    if (name.endsWith('-split.js')) {
      await h.context.openGenerateModal();
      for (const [id, value] of Object.entries({ twsGenYear: '1448', twsGenSem: '1', twsGenProgram: 'AI',
        twsGenSection: 'M', twsGenStrategy: 'compact', twsGenEngine: 'classic' })) h.$(id).value = value;
      await h.dialogs[0].buttons[1].onClick({});
    } else {
      for (const [id, value] of Object.entries({ twYear: '1448', twTerm: '1', twProgram: 'AI', twSection: 'M', twStrategy: 'compact' })) h.$(id).value = value;
      await h.context.runGenerate('classic');
    }
    assert.deepEqual(h.notices.success, []);
    assert.match(h.notices.warning[0], /build did not start.*Full rebuild/);
  });
}
