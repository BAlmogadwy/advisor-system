/* Shared planner card: REST client and classic workspace subscriber.
 * Events: tw:planner-job {job, scenarioId, jobId, source},
 * tw:planner-request {scenarioId, pending, source}, tw:planner-busy {busy},
 * tw:scenario-selected {scenarioId, job?}. Only a terminal poll ends a run.
 */
(function () {
  "use strict";
  var ACTIVE = { queued: 1, running: 1, unknown: 1 };
  var TERMINAL = { succeeded: 1, failed: 1, cancelled: 1 };
  var AR = document.documentElement.lang === "ar";
  function say(en, ar) { return AR ? ar : en; }
  function qs(root, sel) { return root.querySelector(sel); }
  function same(a, b) { return String(a == null ? "" : a) === String(b == null ? "" : b); }
  function parse(node) {
    if (!node) return null;
    try { return JSON.parse(node.textContent || "{}"); } catch (_) { return null; }
  }
  // A full ISO timestamp reads as a local time; anything else is shown as given.
  function shownValue(key, value) {
    if (value == null || value === "") return "-";
    var text = String(value);
    if (/_at$/.test(key) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text)) {
      var date = new Date(text);
      if (!isNaN(date.getTime())) {
        try { return date.toLocaleString(AR ? "ar" : "en-GB", { dateStyle: "medium", timeStyle: "medium" }); } catch (_) { return text; }
      }
    }
    return text;
  }
  function setField(card, key, value) {
    var node = qs(card, '[data-pr8-field="' + key + '"]');
    if (node) node.textContent = shownValue(key, value);
  }
  // The design system's pill tone for each status (global.css .pill-*).
  var TONES = { submitting: "pill-b", queued: "pill-b", running: "pill-b", unknown: "pill-a",
    succeeded: "pill-g", failed: "pill-r", cancelled: "pill-n", idle: "pill-n" };
  function scenarioLabel(id) {
    if (id == null) return "-";
    var select = document.getElementById("twScenario");
    if (select) {
      for (var i = 0; i < select.options.length; i++) {
        if (same(select.options[i].value, id)) return select.options[i].text + " (#" + id + ")";
      }
    }
    return "#" + id;
  }
  function resolveScenarioId(cfg) {
    if (window.TW && window.TW.scenarioId != null) return window.TW.scenarioId;
    var select = document.getElementById("twScenario");
    if (select) return select.value ? Number(select.value) : null;
    return cfg.scenarioId == null ? null : cfg.scenarioId;
  }
  function readOnly(state) {
    return !!(window.TW && same(window.TW.scenarioId, state.scenarioId)
      && window.TW.scenario && window.TW.scenario.status === "published");
  }
  function paint(state) {
    var job = state.currentJob || {};
    var status = job.status || (state.inFlight === "submit" ? "submitting" : "idle");
    var names = {
      idle: say("Ready", "جاهز"), submitting: say("Submitting…", "جارٍ الإرسال…"),
      queued: say("Queued", "في الانتظار"), running: say("Running", "قيد التشغيل"),
      unknown: say("Status unconfirmed", "الحالة غير مؤكدة"),
      succeeded: say("Completed", "اكتمل"), failed: say("Failed", "فشل"), cancelled: say("Cancelled", "ملغي")
    };
    var pill = qs(state.card, "[data-pr8-status]");
    if (pill) {
      pill.textContent = names[status] || status;
      pill.className = "pr8-status-pill pill-status " + (TONES[status] || "pill-n") + " status-" + status;
    }
    setField(state.card, "scenario", scenarioLabel(state.scenarioId));
    ["submitted_at", "started_at", "finished_at", "last_stage_seen", "error_message"].forEach(function (key) {
      setField(state.card, key, job[key]);
    });
    state.card.querySelectorAll("[data-pr8-error-row]").forEach(function (node) { node.hidden = !job.error_message; });
    var active = !!ACTIVE[job.status];
    var blocked = !!state.inFlight || state.externalPending || state.externalBusy || readOnly(state);
    state.card.dataset.scenarioId = state.scenarioId == null ? "" : String(state.scenarioId);
    state.card.dataset.jobId = state.jobId || "";
    state.card.setAttribute("aria-busy", String(active || !!state.inFlight || state.externalPending));
    var submit = qs(state.card, '[data-pr8-action="submit"]');
    var cancel = qs(state.card, '[data-pr8-action="cancel"]');
    var view = qs(state.card, '[data-pr8-action="view-result"]');
    var rerun = qs(state.card, '[data-pr8-action="rerun"]');
    if (submit) submit.disabled = active || blocked || state.scenarioId == null;
    if (cancel) {
      cancel.hidden = !active;
      cancel.disabled = !!state.inFlight || state.externalPending || !!job.cancel_requested;
      cancel.textContent = job.cancel_requested ? say("Cancellation requested…", "تم طلب الإلغاء…") : say("Cancel", "إلغاء");
    }
    if (view) { view.hidden = job.status !== "succeeded"; view.disabled = !state.jobId; }
    if (rerun) { rerun.hidden = !(job.status === "failed" || job.status === "cancelled"); rerun.disabled = blocked || state.scenarioId == null; }
    var message = qs(state.card, "[data-pr8-message]");
    if (message) { message.textContent = state.message || ""; message.hidden = !state.message; }
    // The card is shown only with something to say: a run in progress, a run
    // that ended (until closed), or a message. Idle, it stays out of the way.
    var terminal = !!TERMINAL[job.status];
    var dismissed = terminal && state.jobId && same(state.dismissedJobId, state.jobId);
    state.card.dataset.status = status;
    state.card.hidden = !(active || !!state.inFlight || !!state.message || (terminal && !dismissed));
    var dismiss = qs(state.card, "[data-pr8-dismiss]");
    if (dismiss) dismiss.hidden = active || !!state.inFlight;
  }
  function publish(state, name, detail) {
    state.dispatching = true;
    try { window.dispatchEvent(new CustomEvent(name, { detail: Object.assign({ source: "adapter" }, detail) })); }
    finally { state.dispatching = false; }
  }
  function fillUrl(template, jobId) { return template.replace("{job_id}", encodeURIComponent(jobId)); }
  function jsonFetch(url, opts) {
    opts = Object.assign({ credentials: "same-origin" }, opts || {});
    opts.headers = Object.assign({ "Content-Type": "application/json", Accept: "application/json" }, opts.headers || {});
    return fetch(url, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = body.error;
          throw Object.assign(new Error((error && (error.message || error.code)) || body.detail || body.message || say("Request failed", "فشل الطلب") + " (HTTP " + response.status + ")"),
            { status: response.status, response: body });
        }
        return body;
      });
    });
  }
  function stopPolling(state) {
    if (state.pollTimer != null) clearTimeout(state.pollTimer);
    state.pollTimer = null;
    state.pollEpoch++;
  }
  function acceptJob(state, job, scenarioId, jobId, emit) {
    if (!job || !(ACTIVE[job.status] || TERMINAL[job.status])) return;
    jobId = jobId || job.job_id;
    if (!jobId) return;
    var previous = state.jobs[String(scenarioId)];
    if (previous && !same(previous.job_id, jobId) && ACTIVE[previous.status]) return;
    // A slower poll can arrive after a cancellation/terminal event from another subscriber.
    if (previous && same(previous.job_id, jobId)) {
      if (TERMINAL[previous.status] && ACTIVE[job.status]) return;
      if (previous.cancel_requested && ACTIVE[job.status]) job = Object.assign({}, job, { cancel_requested: true });
    }
    job = Object.assign({}, job, { job_id: jobId });
    state.jobs[String(scenarioId)] = job;
    if (same(state.scenarioId, scenarioId)) {
      if (!same(state.jobId, jobId)) stopPolling(state);
      state.jobId = jobId;
      state.currentJob = job;
      state.message = "";
      paint(state);
      if (TERMINAL[job.status]) stopPolling(state);
      else startPolling(state);
    }
    if (emit) publish(state, "tw:planner-job", { job: job, scenarioId: scenarioId, jobId: jobId });
  }
  function startPolling(state) {
    if (state.pollTimer != null || state.polling || !state.jobId || !ACTIVE[(state.currentJob || {}).status]) return;
    var epoch = state.pollEpoch;
    state.pollTimer = setTimeout(function () {
      state.pollTimer = null;
      var jobId = state.jobId;
      var scenarioId = state.scenarioId;
      state.polling = true;
      jsonFetch(fillUrl(state.cfg.pollUrl, jobId)).then(function (job) {
        if (epoch !== state.pollEpoch || !same(state.jobId, jobId) || !same(state.scenarioId, scenarioId)) return;
        if ((!ACTIVE[job.status] && !TERMINAL[job.status]) || (job.job_id && !same(job.job_id, jobId))) throw new Error(say("Invalid planner status", "حالة المخطط غير صالحة"));
        acceptJob(state, job, scenarioId, jobId, true);
      }).catch(function (error) {
        if (epoch !== state.pollEpoch) return;
        state.message = say("Status unavailable; the run may still be active. Retrying. ", "الحالة غير متاحة؛ قد يظل التشغيل نشطًا. جارٍ إعادة المحاولة. ") + error.message;
        paint(state);
      }).then(function () { state.polling = false; startPolling(state); });
    }, state.cfg.pollIntervalMs || 2000);
  }
  function selectScenario(state, scenarioId, job) {
    stopPolling(state);
    state.scenarioId = scenarioId === "" || scenarioId == null ? null : scenarioId;
    state.currentJob = null;
    state.jobId = null;
    state.message = "";
    // A selected scenario's authoritative server snapshot can supersede a
    // cached job after reconciliation or activity in another browser tab.
    if (job && !same(job.job_id, (state.jobs[String(state.scenarioId)] || {}).job_id)) {
      delete state.jobs[String(state.scenarioId)];
    }
    var saved = job || state.jobs[String(state.scenarioId)];
    if (saved) acceptJob(state, saved, state.scenarioId, saved.job_id, false);
    paint(state);
  }
  // "Run in background" and "Run again" start the full rebuild, which replaces
  // every unlocked placement: where the page has the shared dialog, ask first.
  function confirmSubmit() {
    var dialog = window.dlg;
    return Promise.resolve(dialog.confirm({
      title: say("Rebuild in the background?", "إعادة البناء في الخلفية؟"),
      body: say("Every unlocked placement in this scenario is replaced by a new build. Locked sections keep their days and times.",
        "تُستبدل كل الشعب غير المقفلة في هذا السيناريو ببناء جديد. الشعب المقفلة تبقى في أيامها وأوقاتها."),
      kind: "warning", confirmText: say("Rebuild", "إعادة البناء")
    }));
  }
  function onSubmit(state) {
    if (state.inFlight || state.externalPending || state.externalBusy || readOnly(state) || ACTIVE[(state.currentJob || {}).status]) return;
    var scenarioId = state.scenarioId;
    if (scenarioId == null) return;
    state.inFlight = "submit";
    state.message = "";
    paint(state);
    publish(state, "tw:planner-request", { scenarioId: scenarioId, pending: true });
    jsonFetch(state.cfg.submitUrl, {
      method: "POST", headers: { "X-CSRFToken": state.cfg.csrfToken || "" },
      body: JSON.stringify({ scenario_id: Number(scenarioId), mode: state.cfg.mode || "full_rebuild" })
    }).then(function (response) {
      if (!response.job_id) throw new Error(say("Planner did not return a job ID", "لم يُرجع المخطط معرّف التشغيل"));
      if (response.status && !ACTIVE[response.status] && !TERMINAL[response.status]) throw new Error(say("Invalid planner status", "حالة المخطط غير صالحة"));
      acceptJob(state, Object.assign({ status: "queued" }, response), scenarioId, response.job_id, true);
    }).catch(function (error) {
      var serverError = error.response && error.response.error;
      var details = serverError && serverError.details;
      if (serverError && serverError.code === "SCENARIO_BUSY" && details && details.active_job) {
        var serverScenario = details.scenario_id == null ? details.active_job.scenario_id : details.scenario_id;
        if (same(serverScenario, scenarioId)) acceptJob(state, details.active_job, scenarioId, details.active_job.job_id, true);
      }
      if (same(state.scenarioId, scenarioId)) state.message = error.message;
    })
      .then(function () {
        state.inFlight = false;
        publish(state, "tw:planner-request", { scenarioId: scenarioId, pending: false });
        paint(state);
      });
  }
  function onCancel(state) {
    if (state.inFlight || state.externalPending || !state.jobId || !ACTIVE[(state.currentJob || {}).status] || state.currentJob.cancel_requested) return;
    var jobId = state.jobId;
    var scenarioId = state.scenarioId;
    state.inFlight = "cancel";
    state.message = "";
    paint(state);
    jsonFetch(fillUrl(state.cfg.cancelUrl, jobId), {
      method: "POST", headers: { "X-CSRFToken": state.cfg.csrfToken || "" }
    }).then(function (response) {
      var job = state.jobs[String(scenarioId)];
      if (job && same(job.job_id, jobId) && ACTIVE[job.status]) {
        acceptJob(state, Object.assign({}, job, { cancel_requested: !!response.cancel_requested }), scenarioId, jobId, true);
      }
    }).catch(function (error) { if (same(state.scenarioId, scenarioId)) state.message = error.message; })
      .then(function () { state.inFlight = false; paint(state); });
  }
  function wire(card) {
    var cfg = parse(qs(card, "script[data-pr8-config]"));
    if (!cfg) return;
    var state = { card: card, cfg: cfg, scenarioId: resolveScenarioId(cfg), jobId: null, currentJob: null,
      jobs: {}, pollTimer: null, pollEpoch: 0, polling: false, inFlight: false,
      externalPending: false, externalBusy: !!(window.TW && window.TW.busy), message: "", dispatching: false };
    card.addEventListener("click", function (event) {
      if (event.target.closest("[data-pr8-dismiss]")) {
        state.dismissedJobId = state.jobId;
        state.message = "";
        paint(state);
        return;
      }
      var target = event.target.closest("[data-pr8-action]");
      if (!target || !card.contains(target) || target.disabled || target.hidden) return;
      event.preventDefault();
      var action = target.getAttribute("data-pr8-action");
      if (action === "submit" || action === "rerun") {
        // Without the shared dialog (a page that does not load it) the click
        // submits at once, as before; with it, after the user agrees.
        if (window.dlg && typeof window.dlg.confirm === "function") {
          confirmSubmit().then(function (ok) { if (ok) onSubmit(state); });
        } else {
          onSubmit(state);
        }
      }
      else if (action === "cancel") onCancel(state);
      else if (action === "view-result" && state.jobId && (state.currentJob || {}).status === "succeeded") {
        var opened = window.open(fillUrl(cfg.resultUrl, state.jobId), "_blank", "noopener");
        if (opened) opened.focus();
      }
    });
    window.addEventListener("tw:planner-job", function (event) {
      if (state.dispatching) return;
      var detail = event.detail || {};
      if (detail.scenarioId == null) return;
      if (detail.job) acceptJob(state, detail.job, detail.scenarioId, detail.jobId, false);
      else if (same(state.scenarioId, detail.scenarioId)) {
        delete state.jobs[String(detail.scenarioId)];
        selectScenario(state, detail.scenarioId);
      }
    });
    window.addEventListener("tw:planner-request", function (event) {
      if (state.dispatching) return;
      state.externalPending = !!(event.detail || {}).pending;
      paint(state);
    });
    window.addEventListener("tw:planner-busy", function (event) { state.externalBusy = !!(event.detail || {}).busy; paint(state); });
    window.addEventListener("tw:scenario-selected", function (event) {
      var detail = event.detail || {};
      if (detail.job === null) delete state.jobs[String(detail.scenarioId)];
      selectScenario(state, detail.scenarioId, detail.job);
    });
    var select = document.getElementById("twScenario");
    if (select) select.addEventListener("change", function () { selectScenario(state, select.value ? Number(select.value) : null); });
    var initial = parse(qs(card, "#pr8-initial-job")) || cfg.job || (window.TW && window.TW.plannerJob);
    if (initial) {
      // Accept the shared event snapshot or the backend's plain poll JSON.
      acceptJob(state, initial.job || initial, initial.scenarioId == null ? state.scenarioId : initial.scenarioId,
        initial.jobId || initial.job_id || (initial.job && initial.job.job_id), false);
    }
    paint(state);
  }
  function init() { document.querySelectorAll("[data-pr8-card]").forEach(wire); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
