/* PR8 — async job UI adapter.
 *
 * Thin client for the PR7 REST surface. Wires a status card on the
 * scenario page: submit, poll (2s while active, stop on terminal),
 * cancel, fetch final result. No framework — native fetch + DOM.
 *
 * The card element is located by `[data-pr8-card]`. Controls inside it
 * carry `data-pr8-action="submit|cancel|rerun|view-result"`.
 *
 * A `<script data-pr8-config>` tag carries a JSON blob with:
 *   { submitUrl, pollUrl, resultUrl, cancelUrl, pollIntervalMs,
 *     csrfToken, scenarioId, mode }
 * where pollUrl/resultUrl/cancelUrl contain a `{job_id}` placeholder.
 */
(function () {
  "use strict";

  var ACTIVE = { queued: 1, running: 1 };
  var TERMINAL = { succeeded: 1, failed: 1, cancelled: 1 };

  function readConfig() {
    var node = document.querySelector("script[data-pr8-config]");
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || "{}");
    } catch (e) {
      return null;
    }
  }

  function qs(root, sel) {
    return root.querySelector(sel);
  }

  function setStatusPill(card, status) {
    var pill = qs(card, "[data-pr8-status]");
    if (!pill) return;
    pill.textContent = status || "idle";
    pill.className = "pr8-status-pill status-" + (status || "idle");
  }

  function setField(card, key, value) {
    var node = qs(card, '[data-pr8-field="' + key + '"]');
    if (node) node.textContent = value == null ? "" : String(value);
  }

  function paint(card, job) {
    if (!job) {
      setStatusPill(card, "idle");
      return;
    }
    setStatusPill(card, job.status);
    setField(card, "submitted_at", job.submitted_at);
    setField(card, "started_at", job.started_at);
    setField(card, "finished_at", job.finished_at);
    setField(card, "last_stage_seen", job.last_stage_seen);
    setField(card, "error_message", job.error_message);

    // Toggle controls by status.
    var isActive = !!ACTIVE[job.status];
    var isTerminal = !!TERMINAL[job.status];
    var submit = qs(card, '[data-pr8-action="submit"]');
    var cancel = qs(card, '[data-pr8-action="cancel"]');
    var viewResult = qs(card, '[data-pr8-action="view-result"]');
    var rerun = qs(card, '[data-pr8-action="rerun"]');
    if (submit) submit.disabled = isActive;
    if (cancel) cancel.hidden = !isActive;
    if (viewResult) viewResult.hidden = job.status !== "succeeded";
    if (rerun) rerun.hidden = !(job.status === "failed" || job.status === "cancelled");
  }

  function fillUrl(template, jobId) {
    return template.replace("{job_id}", encodeURIComponent(jobId));
  }

  function jsonFetch(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign(
      { "Content-Type": "application/json", Accept: "application/json" },
      opts.headers || {}
    );
    return fetch(url, opts).then(function (r) {
      if (!r.ok) throw Object.assign(new Error("HTTP " + r.status), { status: r.status });
      return r.json();
    });
  }

  function startPolling(state) {
    if (state.pollTimer) return;
    var tick = function () {
      if (!state.jobId) return;
      jsonFetch(fillUrl(state.cfg.pollUrl, state.jobId))
        .then(function (job) {
          state.currentJob = job;
          paint(state.card, job);
          if (TERMINAL[job.status]) stopPolling(state);
        })
        .catch(function () {
          /* non-blocking: UI keeps the previous state; next tick retries */
        });
    };
    state.pollTimer = setInterval(tick, state.cfg.pollIntervalMs || 2000);
    tick();
  }

  function stopPolling(state) {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function onSubmit(state) {
    if (state.inFlight) return;
    state.inFlight = true;
    var body = JSON.stringify({
      scenario_id: state.cfg.scenarioId,
      mode: state.cfg.mode || "full_rebuild",
    });
    jsonFetch(state.cfg.submitUrl, {
      method: "POST",
      headers: { "X-CSRFToken": state.cfg.csrfToken || "" },
      body: body,
    })
      .then(function (resp) {
        state.jobId = resp.job_id;
        state.currentJob = { status: resp.status || "queued", job_id: resp.job_id };
        paint(state.card, state.currentJob);
        startPolling(state);
      })
      .catch(function () {
        // swallow — user can retry
      })
      .then(function () {
        state.inFlight = false;
      });
  }

  function onCancel(state) {
    if (!state.jobId) return;
    jsonFetch(fillUrl(state.cfg.cancelUrl, state.jobId), {
      method: "POST",
      headers: { "X-CSRFToken": state.cfg.csrfToken || "" },
    }).catch(function () {
      /* non-blocking */
    });
  }

  function onRerun(state) {
    state.jobId = null;
    state.currentJob = null;
    stopPolling(state);
    paint(state.card, null);
    onSubmit(state);
  }

  function onViewResult(state) {
    if (!state.jobId) return;
    var w = window.open(fillUrl(state.cfg.resultUrl, state.jobId), "_blank");
    if (w) w.focus();
  }

  function wire(card, cfg) {
    var state = {
      card: card,
      cfg: cfg,
      jobId: null,
      currentJob: null,
      pollTimer: null,
      inFlight: false,
    };
    card.addEventListener("click", function (ev) {
      var target = ev.target.closest("[data-pr8-action]");
      if (!target || !card.contains(target)) return;
      ev.preventDefault();
      var action = target.getAttribute("data-pr8-action");
      if (action === "submit") onSubmit(state);
      else if (action === "cancel") onCancel(state);
      else if (action === "rerun") onRerun(state);
      else if (action === "view-result") onViewResult(state);
    });
    paint(card, null);
  }

  function init() {
    var cfg = readConfig();
    if (!cfg) return;
    var cards = document.querySelectorAll("[data-pr8-card]");
    for (var i = 0; i < cards.length; i++) {
      wire(cards[i], cfg);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
