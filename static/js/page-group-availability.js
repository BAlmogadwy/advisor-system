/* Group Availability finder — compare registered schedules,
   while keeping unresolved or partially timed students visible without
   blocking the calculation for the rest of the group. */
(function () {
  "use strict";

  var cfg = window.groupAvailabilityConfig || {};
  var IS_AR = cfg.lang === "ar";

  function t(en, ar) { return IS_AR ? ar : en; }

  var DAY_LABELS = {
    SUN: t("Sunday", "الأحد"),
    MON: t("Monday", "الإثنين"),
    TUE: t("Tuesday", "الثلاثاء"),
    WED: t("Wednesday", "الأربعاء"),
    THU: t("Thursday", "الخميس"),
  };

  var REGISTERED_LABEL = t("Registered", "مسجل");

  // ── DOM refs ──────────────────────────────────────────────
  var $ids = document.getElementById("gaIds");
  var $compute = document.getElementById("gaCompute");
  var $clear = document.getElementById("gaClear");
  var $status = document.getElementById("gaStatus");
  var $countHint = document.getElementById("gaCountHint");
  var $summary = document.getElementById("gaSummary");
  var $summaryStats = document.getElementById("gaSummaryStats");
  var $provenance = document.getElementById("gaProvenance");
  var $flags = document.getElementById("gaFlags");
  var $legendFree = document.getElementById("gaLegendFree");
  var $detail = document.getElementById("gaDetail");
  var $detailTitle = document.getElementById("gaDetailTitle");
  var $detailBody = document.getElementById("gaDetailBody");
  var $detailClose = document.getElementById("gaDetailClose");
  var $tabs = Array.prototype.slice.call(document.querySelectorAll(".ga-tab"));
  var $panels = {
    lecture: document.getElementById("gaPanelLecture"),
    lab: document.getElementById("gaPanelLab"),
    timeline: document.getElementById("gaPanelTimeline"),
  };

  var state = {
    result: null,
    grid: "lecture",
    nameById: {},
    detailTrigger: null,
    requestVersion: 0,
    controller: null,
    pending: false,
  };

  function parseIds(text) {
    var matches = String(text || "").match(/\d+/g) || [];
    var seen = {};
    var out = [];
    matches.forEach(function (match) {
      if (!seen[match]) { seen[match] = true; out.push(match); }
    });
    return out;
  }

  function updateCountHint() {
    var ids = parseIds($ids.value);
    var msg = ids.length === 1 ? t("1 ID", "رقم واحد") : ids.length + " " + t("IDs", "أرقام");
    if (cfg.maxStudents && ids.length > cfg.maxStudents) {
      msg += " — " + t("only first ", "أول ") + cfg.maxStudents + t(" used", " فقط");
    }
    $countHint.textContent = ids.length ? msg : "";
  }

  function setStatus(msg, bad) {
    $status.textContent = msg || "";
    $status.classList.toggle("is-bad", !!bad);
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
    });
  }

  function count(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : (fallback || 0);
  }

  function emptyMarkup(message) {
    return '<div class="ga-empty">' + escapeHtml(message || t(
      "Enter student IDs and press “Show availability”.",
      "أدخل أرقام الطلاب ثم اضغط «عرض التوافر»."
    )) + "</div>";
  }

  function closeDetail(restoreFocus) {
    var trigger = state.detailTrigger;
    $detail.hidden = true;
    document.querySelectorAll(".ga-cell.is-selected").forEach(function (cell) {
      cell.classList.remove("is-selected");
    });
    document.querySelectorAll('.ga-cell-button[aria-expanded="true"]').forEach(function (button) {
      button.setAttribute("aria-expanded", "false");
    });
    state.detailTrigger = null;
    if (restoreFocus && trigger && document.documentElement.contains(trigger)) trigger.focus();
  }

  function clearResultDisplay(message) {
    closeDetail(false);
    state.result = null;
    state.nameById = {};
    $summary.hidden = true;
    $summaryStats.innerHTML = "";
    $provenance.innerHTML = "";
    $flags.innerHTML = "";
    if ($legendFree) {
      $legendFree.textContent = t("Free", "متاح");
    }
    Object.keys($panels).forEach(function (key) {
      if ($panels[key]) $panels[key].innerHTML = emptyMarkup(message);
    });
  }

  function cancelPendingRequest() {
    state.requestVersion += 1;
    if (state.controller) state.controller.abort();
    state.controller = null;
    state.pending = false;
    $compute.disabled = false;
  }

  // ── Render: summary ───────────────────────────────────────
  function statTile(number, label, good) {
    return '<div class="ga-stat"><div class="ga-stat-num' + (good ? " is-good" : "") +
      '">' + escapeHtml(number) + '</div><div class="ga-stat-lbl">' + escapeHtml(label) + "</div></div>";
  }

  function gridFreeCount(grid) {
    if (!grid) return 0;
    if (grid.free_for_resolved_count != null) return count(grid.free_for_resolved_count, 0);
    return count(grid.free_for_all_count, 0);
  }

  function registeredScheduleCount(data, resolved) {
    var supplied = data.snapshot_class_counts || {};
    return supplied.registrar == null ? resolved : count(supplied.registrar, resolved);
  }

  function renderSummary(data) {
    var requested = count(data.requested_count, 0);
    var resolved = count(data.resolved_count, 0);
    var unresolved = data.unresolved_count == null
      ? Math.max(0, requested - resolved)
      : count(data.unresolved_count, 0);
    var partial = data.partial_schedule_count == null
      ? count(data.partial_schedule && data.partial_schedule.length, 0)
      : count(data.partial_schedule_count, 0);
    var coverageComplete = data.coverage_complete == null
      ? unresolved === 0 && partial === 0
      : !!data.coverage_complete;
    var lectureFree = gridFreeCount(data.grids && data.grids.lecture);
    var labFree = gridFreeCount(data.grids && data.grids.lab);
    var timelineFree = gridFreeCount(data.grids && data.grids.timeline);
    var freePrefix = t("Free", "متاح");

    $summaryStats.innerHTML =
      statTile(resolved + "/" + requested, t("Registered schedules", "الجداول المسجلة"), coverageComplete) +
      statTile(unresolved, t("Unresolved", "غير محسوم"), false) +
      statTile(resolved > 0 ? lectureFree : "—", freePrefix + " · " + t("lecture slots", "فترات محاضرات"), resolved > 0 && lectureFree > 0) +
      statTile(resolved > 0 ? labFree : "—", freePrefix + " · " + t("lab slots", "فترات معامل"), resolved > 0 && labFree > 0) +
      statTile(resolved > 0 ? timelineFree : "—", freePrefix + " · " + t("10m periods", "فترات 10 د"), resolved > 0 && timelineFree > 0);

    var registered = registeredScheduleCount(data, resolved);
    $provenance.innerHTML =
      '<span class="ga-provenance-label">' + escapeHtml(t("Registered schedules:", "الجداول المسجلة:")) + "</span>" +
      '<span class="ga-source is-registrar">' + registered + "</span>";

    if ($legendFree) {
      $legendFree.textContent = t("Free", "متاح");
    }

    var flags = "";
    if (resolved === 0) {
      flags += '<div class="ga-flag is-warn">' + escapeHtml(t(
        "No registered schedules. Availability cannot be determined; unresolved students remain flagged.",
        "لا توجد جداول مسجلة. لا يمكن تحديد التوافر، ويظل الطلاب غير المحسومين مميزين."
      )) + "</div>";
    } else if (!coverageComplete || unresolved > 0) {
      var registeredScheduleNoun = resolved === 1
        ? t(" registered schedule. ", " جدولًا مسجلًا. ")
        : t(" registered schedules. ", " جدولًا مسجلًا. ");
      flags += '<div class="ga-flag is-warn">' + escapeHtml(
        t("Availability uses ", "تم حساب التوافر اعتمادًا على ") + resolved +
        registeredScheduleNoun +
        t("Coverage is incomplete; available registered schedules were still calculated and flagged records did not block the group.", "التغطية غير مكتملة؛ استمر حساب الجداول المسجلة المتاحة ولم تمنع السجلات المميزة حساب المجموعة.")
      ) + "</div>";
    }
    if (data.not_found && data.not_found.length) {
      flags += '<div class="ga-flag is-warn">' +
        t("Not found: ", "غير موجود: ") + data.not_found.length +
        " (" + escapeHtml(data.not_found.slice(0, 8).join(", ")) +
        (data.not_found.length > 8 ? "…" : "") + ")</div>";
    }
    if (data.no_schedule && data.no_schedule.length) {
      flags += '<div class="ga-flag is-info">' +
        t("No registered schedule: ", "لا يوجد جدول مسجل: ") + data.no_schedule.length +
        " (" + escapeHtml(data.no_schedule.slice(0, 8).join(", ")) +
        (data.no_schedule.length > 8 ? "…" : "") + ")</div>";
    }
    if (partial > 0) {
      var partialIds = data.partial_schedule || [];
      flags += '<div class="ga-flag is-info">' +
        t("Partially timed schedules: ", "جداول بتوقيت جزئي: ") + partial +
        (partialIds.length ? " (" + escapeHtml(partialIds.slice(0, 8).join(", ")) +
          (partialIds.length > 8 ? "…" : "") + ")" : "") + " — " +
        t("some sections have no usable meeting time; known meetings were still calculated.", "بعض الشعب بلا وقت اجتماع صالح؛ استمر الحساب باستخدام الاجتماعات المعروفة.") +
        "</div>";
    }
    $flags.innerHTML = flags;
    $summary.hidden = false;
  }

  // ── Render: semantic weekly tables ────────────────────────
  function cellClass(busy, resolved) {
    if (resolved <= 0) return "is-unknown";
    if (busy <= 0) return "is-free";
    var ratio = busy / Math.max(1, resolved);
    return ratio >= 0.5 ? "is-most" : "is-some";
  }

  function coverageSuffix(unresolved, partial, coverageComplete) {
    if (coverageComplete) return "";
    return t(
      " Coverage is incomplete: " + unresolved + " unresolved and " + partial + " partially timed student(s) may have additional conflicts.",
      " التغطية غير مكتملة: قد توجد تعارضات إضافية لدى " + unresolved + " طالب غير محسوم و" + partial + " طالب بتوقيت جزئي."
    );
  }

  function renderGrid(gridType) {
    var data = state.result;
    var panel = $panels[gridType];
    if (!data || !panel) return;
    var grid = data.grids && data.grids[gridType];
    if (!grid) {
      panel.innerHTML = emptyMarkup(t("This grid is unavailable.", "هذه الشبكة غير متاحة."));
      return;
    }

    var isTimeline = gridType === "timeline";
    var days = data.weekdays || ["SUN", "MON", "TUE", "WED", "THU"];
    var requested = count(data.requested_count, 0);
    var resolved = count(data.resolved_count, 0);
    var unresolved = data.unresolved_count == null
      ? Math.max(0, requested - resolved)
      : count(data.unresolved_count, 0);
    var partial = data.partial_schedule_count == null
      ? count(data.partial_schedule && data.partial_schedule.length, 0)
      : count(data.partial_schedule_count, 0);
    var coverageComplete = data.coverage_complete == null
      ? unresolved === 0 && partial === 0
      : !!data.coverage_complete;
    var table = document.createElement("table");
    table.className = "ga-grid" + (isTimeline ? " is-timeline" : "");

    var caption = document.createElement("caption");
    caption.className = "ga-sr-only";
    caption.textContent = (gridType === "lecture" ? t("Lecture", "المحاضرات")
      : gridType === "lab" ? t("Lab", "المعامل") : t("Full-day 10-minute", "اليوم الكامل بفترات 10 دقائق")) +
      t(" weekly availability. ", "، التوافر الأسبوعي. ") + resolved + t(" of ", " من ") + requested +
      t(" registered schedules.", " جدولًا مسجلًا.");
    table.appendChild(caption);

    var thead = document.createElement("thead");
    var headerRow = document.createElement("tr");
    var corner = document.createElement("th");
    corner.className = "ga-grid-corner";
    corner.scope = "col";
    corner.textContent = t("Time", "الوقت");
    headerRow.appendChild(corner);
    days.forEach(function (day) {
      var heading = document.createElement("th");
      heading.className = "ga-grid-dayhead";
      heading.scope = "col";
      heading.textContent = DAY_LABELS[day] || day;
      headerRow.appendChild(heading);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    (grid.slots || []).forEach(function (slot, slotIndex) {
      var row = document.createElement("tr");
      var slotLabel = document.createElement("th");
      slotLabel.className = "ga-grid-slotlabel";
      slotLabel.scope = "row";
      slotLabel.innerHTML = "<span>" + escapeHtml(slot.start) + "</span><span>" + escapeHtml(slot.end) + "</span>";
      row.appendChild(slotLabel);

      days.forEach(function (day) {
        var cell = (grid.cells && grid.cells[day] || [])[slotIndex] || {
          busy_count: 0,
          free_for_resolved: true,
          occupants: [],
        };
        var busy = count(cell.busy_count, 0);
        var freeForResolved = cell.free_for_resolved == null
          ? (cell.free == null ? busy === 0 : !!cell.free)
          : !!cell.free_for_resolved;
        var dayAndTime = (DAY_LABELS[day] || day) + " " + slot.start + "–" + slot.end + ". ";
        var caution = coverageSuffix(unresolved, partial, coverageComplete);
        var td = document.createElement("td");
        td.className = "ga-cell " + cellClass(busy, resolved);

        if (resolved <= 0) {
          td.setAttribute("aria-label", dayAndTime + t(
            "0 known busy; no registered schedules, so availability is unknown.",
            "0 مشغول معروف؛ لا توجد جداول مسجلة ولذلك التوافر غير معروف."
          ));
          td.innerHTML = '<span class="ga-cell-content">' +
            (isTimeline ? '<span class="ga-cell-num">0</span>' : '<span class="ga-cell-unknown">?</span>') +
            '<span class="ga-cell-sub">' + escapeHtml(isTimeline ? t("busy · no data", "مشغول · بلا بيانات") : t("no schedule data", "لا توجد بيانات")) +
            "</span></span>";
        } else if (freeForResolved && busy === 0) {
          var freeText = t("Free.", "متاح.");
          td.setAttribute("aria-label", dayAndTime + (isTimeline ? t("0 busy. ", "0 مشغول. ") : "") + freeText + caution);
          td.innerHTML = '<span class="ga-cell-content">' +
            (isTimeline ? '<span class="ga-cell-num">0</span>' : '<span class="ga-cell-check">✓</span>') +
            '<span class="ga-cell-sub">' + escapeHtml(t("Free", "متاح")) +
            "</span></span>";
        } else {
          var button = document.createElement("button");
          button.type = "button";
          button.className = "ga-cell-button";
          button.setAttribute("aria-controls", "gaDetail");
          button.setAttribute("aria-expanded", "false");
          button.setAttribute("aria-label", dayAndTime + t(
            busy + " busy among " + resolved + " registered schedules. Show details.",
            busy + " مشغول من بين " + resolved + " جدولًا مسجلًا. عرض التفاصيل."
          ) + caution);
          button.innerHTML = '<span class="ga-cell-num">' + busy + '</span><span class="ga-cell-sub">' +
            escapeHtml(t("busy", "مشغول")) + "</span>";
          button.addEventListener("click", function (event) {
            showDetail(day, slot, cell, button, event.detail === 0);
          });
          td.appendChild(button);
        }
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    panel.innerHTML = "";
    panel.appendChild(table);
  }

  function renderAllGrids() {
    renderGrid("lecture");
    renderGrid("lab");
    renderGrid("timeline");
  }

  function showDetail(day, slot, cell, trigger, moveFocus) {
    closeDetail(false);
    state.detailTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
    var selectedCell = trigger.closest(".ga-cell");
    if (selectedCell) selectedCell.classList.add("is-selected");

    $detailTitle.textContent =
      (DAY_LABELS[day] || day) + " " + slot.start + "–" + slot.end + " · " +
      count(cell.busy_count, 0) + " " + t("busy", "مشغول");

    var occupants = cell.occupants || [];
    if (!occupants.length) {
      $detailBody.innerHTML = '<span class="ga-occ">' + t("No detail.", "لا تفاصيل.") + "</span>";
    } else {
      $detailBody.innerHTML = occupants.map(function (occupant) {
        var studentId = occupant.student_id;
        var name = state.nameById[studentId] || "";
        var course = occupant.course_code || t("Course unavailable", "المقرر غير متاح");
        if (occupant.section) course += " · " + occupant.section;
        return '<span class="ga-occ"><span><b>' + escapeHtml(studentId) + "</b> " +
          (name ? escapeHtml(name) + " — " : "") + escapeHtml(course) + '</span><span class="ga-occ-source is-' +
          'registrar">' + escapeHtml(REGISTERED_LABEL) + "</span></span>";
      }).join("");
      if (cell.occupants_truncated) {
        $detailBody.innerHTML += '<span class="ga-occ">+' + escapeHtml(cell.occupants_truncated) +
          " " + t("more", "آخرون") + "</span>";
      }
    }
    $detail.hidden = false;
    if (moveFocus) $detailClose.focus();
  }

  // ── Fetch ─────────────────────────────────────────────────
  function compute() {
    var ids = parseIds($ids.value);
    cancelPendingRequest();
    clearResultDisplay(ids.length
      ? t("Computing availability…", "جارٍ حساب التوافر…")
      : t("Enter student IDs and press “Show availability”.", "أدخل أرقام الطلاب ثم اضغط «عرض التوافر»."));

    if (!ids.length) {
      setStatus(t("Enter at least one student ID.", "أدخل رقم طالب واحد على الأقل."), true);
      return;
    }

    var requestVersion = ++state.requestVersion;
    var controller = typeof AbortController === "undefined" ? null : new AbortController();
    state.controller = controller;
    state.pending = true;
    setStatus(t("Computing…", "جارٍ الحساب…"), false);
    $compute.disabled = true;

    var options = {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": cfg.csrfToken },
      body: JSON.stringify({ student_ids: ids }),
    };
    if (controller) options.signal = controller.signal;

    fetch(cfg.computeUrl, options)
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (response) {
        if (requestVersion !== state.requestVersion) return;
        if (!response.ok) {
          clearResultDisplay(t("No availability results.", "لا توجد نتائج للتوافر."));
          setStatus((response.body && response.body.error) || t("Request failed.", "فشل الطلب."), true);
          return;
        }

        state.result = response.body;
        state.nameById = {};
        (response.body.students || []).forEach(function (student) {
          state.nameById[student.student_id] = student.name;
        });
        closeDetail(false);
        renderSummary(response.body);
        renderAllGrids();

        var requested = count(response.body.requested_count, 0);
        var resolved = count(response.body.resolved_count, 0);
        var unresolved = response.body.unresolved_count == null
          ? Math.max(0, requested - resolved)
          : count(response.body.unresolved_count, 0);
        var partial = response.body.partial_schedule_count == null
          ? count(response.body.partial_schedule && response.body.partial_schedule.length, 0)
          : count(response.body.partial_schedule_count, 0);
        var termLabel = response.body.academic_year
          ? " · " + t("term ", "الفصل ") + response.body.academic_year + "/" + response.body.term
          : "";

        if (resolved === 0) {
          setStatus(
            t("No registered schedules; availability cannot be determined. ", "لا توجد جداول مسجلة؛ لا يمكن تحديد التوافر. ") +
            unresolved + t(" unresolved flagged.", " غير محسوم تم تمييزه.") + termLabel,
            true
          );
        } else {
          setStatus(
            t("Availability calculated from ", "تم حساب التوافر اعتمادًا على ") + resolved +
            t(" of ", " من ") + requested + t(" registered schedules", " جدولًا مسجلًا") +
            (unresolved ? " · " + unresolved + t(" unresolved flagged", " غير محسوم تم تمييزه") : "") +
            (partial ? " · " + partial + t(" partially timed flagged", " بتوقيت جزئي تم تمييزه") : "") + termLabel,
            false
          );
        }
      })
      .catch(function (error) {
        if (requestVersion !== state.requestVersion || error && error.name === "AbortError") return;
        clearResultDisplay(t("No availability results.", "لا توجد نتائج للتوافر."));
        setStatus(t("Network error.", "خطأ في الشبكة."), true);
      })
      .finally(function () {
        if (requestVersion !== state.requestVersion) return;
        state.controller = null;
        state.pending = false;
        $compute.disabled = false;
      });
  }

  // ── Tabs and controls ─────────────────────────────────────
  function activateTab(tab, moveFocus) {
    state.grid = tab.getAttribute("data-grid");
    $tabs.forEach(function (candidate) {
      var active = candidate === tab;
      var panel = $panels[candidate.getAttribute("data-grid")];
      candidate.classList.toggle("is-active", active);
      candidate.setAttribute("aria-selected", active ? "true" : "false");
      candidate.tabIndex = active ? 0 : -1;
      if (panel) panel.hidden = !active;
    });
    closeDetail(false);
    if (moveFocus) tab.focus();
  }

  $compute.addEventListener("click", compute);
  $clear.addEventListener("click", function () {
    cancelPendingRequest();
    $ids.value = "";
    clearResultDisplay();
    updateCountHint();
    setStatus("", false);
    $ids.focus();
  });

  $ids.addEventListener("input", function () {
    var hadResultsOrRequest = !!state.result || state.pending;
    cancelPendingRequest();
    clearResultDisplay(hadResultsOrRequest
      ? t("Inputs changed. Run availability again.", "تغيرت المدخلات. أعد حساب التوافر.")
      : null);
    updateCountHint();
    setStatus(hadResultsOrRequest
      ? t("Inputs changed; previous results were cleared.", "تغيرت المدخلات؛ تم مسح النتائج السابقة.")
      : "", false);
  });

  $detailClose.addEventListener("click", function () { closeDetail(true); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !$detail.hidden) {
      event.preventDefault();
      closeDetail(true);
    }
  });

  $tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { activateTab(tab, false); });
    tab.addEventListener("keydown", function (event) {
      var current = $tabs.indexOf(tab);
      var next = current;
      var rtl = document.documentElement.dir === "rtl" || IS_AR;
      if (event.key === "ArrowRight") next = (current + (rtl ? -1 : 1) + $tabs.length) % $tabs.length;
      else if (event.key === "ArrowLeft") next = (current + (rtl ? 1 : -1) + $tabs.length) % $tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = $tabs.length - 1;
      else return;
      event.preventDefault();
      activateTab($tabs[next], true);
    });
  });

  activateTab($tabs[0], false);
  updateCountHint();
})();
