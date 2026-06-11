/* Group Availability finder — paste student IDs, see the group's combined
   weekly busy slots, and spot the slots that are free for everyone. */
(function () {
  "use strict";

  var cfg = window.groupAvailabilityConfig || {};
  var IS_AR = cfg.lang === "ar";

  function t(en, ar) { return IS_AR ? ar : en; }

  var DAY_LABELS = {
    SUN: t("Sun", "الأحد"),
    MON: t("Mon", "الإثنين"),
    TUE: t("Tue", "الثلاثاء"),
    WED: t("Wed", "الأربعاء"),
    THU: t("Thu", "الخميس"),
  };

  // ── DOM refs ──────────────────────────────────────────────
  var $ids = document.getElementById("gaIds");
  var $compute = document.getElementById("gaCompute");
  var $clear = document.getElementById("gaClear");
  var $status = document.getElementById("gaStatus");
  var $countHint = document.getElementById("gaCountHint");
  var $summary = document.getElementById("gaSummary");
  var $summaryStats = document.getElementById("gaSummaryStats");
  var $flags = document.getElementById("gaFlags");
  var $gridWrap = document.getElementById("gaGridWrap");
  var $empty = document.getElementById("gaEmpty");
  var $detail = document.getElementById("gaDetail");
  var $detailTitle = document.getElementById("gaDetailTitle");
  var $detailBody = document.getElementById("gaDetailBody");
  var $detailClose = document.getElementById("gaDetailClose");
  var $tabs = Array.prototype.slice.call(document.querySelectorAll(".ga-tab"));

  var state = { result: null, grid: "lecture", nameById: {} };

  function parseIds(text) {
    var matches = String(text || "").match(/\d+/g) || [];
    var seen = {};
    var out = [];
    matches.forEach(function (m) {
      if (!seen[m]) { seen[m] = true; out.push(m); }
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

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ── Render: summary ───────────────────────────────────────
  function statTile(num, label, good) {
    return '<div class="ga-stat"><div class="ga-stat-num' + (good ? " is-good" : "") +
      '">' + num + '</div><div class="ga-stat-lbl">' + escapeHtml(label) + "</div></div>";
  }

  function renderSummary(data) {
    var lec = data.grids.lecture.free_for_all_count;
    var lab = data.grids.lab.free_for_all_count;
    $summaryStats.innerHTML =
      statTile(data.resolved_count + "/" + data.requested_count, t("With schedule", "لديهم جدول")) +
      statTile(lec, t("Free lecture", "محاضرات متاحة"), lec > 0) +
      statTile(lab, t("Free lab", "معامل متاحة"), lab > 0);

    var flags = "";
    if (data.not_found && data.not_found.length) {
      flags += '<div class="ga-flag is-warn">' +
        t("Not found: ", "غير موجود: ") + data.not_found.length +
        " (" + escapeHtml(data.not_found.slice(0, 8).join(", ")) +
        (data.not_found.length > 8 ? "…" : "") + ")</div>";
    }
    if (data.no_schedule && data.no_schedule.length) {
      flags += '<div class="ga-flag is-info">' +
        t("No registered sections: ", "بدون شعب مسجلة: ") + data.no_schedule.length +
        " (" + escapeHtml(data.no_schedule.slice(0, 8).join(", ")) +
        (data.no_schedule.length > 8 ? "…" : "") + ")</div>";
    }
    $flags.innerHTML = flags;
    $summary.hidden = false;
  }

  // ── Render: grid ──────────────────────────────────────────
  function cellClass(busy, resolved) {
    if (busy <= 0) return "is-free";
    var ratio = busy / Math.max(1, resolved);
    return ratio >= 0.5 ? "is-most" : "is-some";
  }

  function renderGrid() {
    var data = state.result;
    if (!data) return;
    var grid = data.grids[state.grid];
    var days = data.weekdays || ["SUN", "MON", "TUE", "WED", "THU"];
    var resolved = data.resolved_count || 0;

    var wrap = document.createElement("div");
    wrap.className = "ga-grid";
    wrap.style.setProperty("--ga-days", days.length);

    // Header row
    var corner = document.createElement("div");
    corner.className = "ga-grid-corner";
    corner.textContent = t("Time", "الوقت");
    wrap.appendChild(corner);
    days.forEach(function (d) {
      var h = document.createElement("div");
      h.className = "ga-grid-dayhead";
      h.textContent = DAY_LABELS[d] || d;
      wrap.appendChild(h);
    });

    // Slot rows
    grid.slots.forEach(function (slot, si) {
      var lbl = document.createElement("div");
      lbl.className = "ga-grid-slotlabel";
      lbl.innerHTML = "<span>" + escapeHtml(slot.start) + "</span><span>" + escapeHtml(slot.end) + "</span>";
      wrap.appendChild(lbl);

      days.forEach(function (d) {
        var cell = (grid.cells[d] || [])[si] || { busy_count: 0, free: true, occupants: [] };
        var el = document.createElement("div");
        var klass = cellClass(cell.busy_count, resolved);
        el.className = "ga-cell " + klass;
        if (cell.free) {
          el.innerHTML = '<span class="ga-cell-check">✓</span><span class="ga-cell-sub">' +
            t("free", "متاح") + "</span>";
        } else {
          el.innerHTML = '<span class="ga-cell-num">' + cell.busy_count +
            '</span><span class="ga-cell-sub">' + t("busy", "مشغول") + "</span>";
          el.setAttribute("role", "button");
          el.setAttribute("tabindex", "0");
          el.addEventListener("click", function () { showDetail(d, slot, cell, el); });
          el.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showDetail(d, slot, cell, el); }
          });
        }
        wrap.appendChild(el);
      });
    });

    $gridWrap.innerHTML = "";
    $gridWrap.appendChild(wrap);
  }

  function showDetail(day, slot, cell, el) {
    document.querySelectorAll(".ga-cell.is-selected").forEach(function (c) {
      c.classList.remove("is-selected");
    });
    if (el) el.classList.add("is-selected");

    $detailTitle.textContent =
      (DAY_LABELS[day] || day) + " " + slot.start + "–" + slot.end + " · " +
      cell.busy_count + " " + t("busy", "مشغول");

    var occ = cell.occupants || [];
    if (!occ.length) {
      $detailBody.innerHTML = '<span class="ga-occ">' + t("No detail.", "لا تفاصيل.") + "</span>";
    } else {
      $detailBody.innerHTML = occ.map(function (o) {
        var name = state.nameById[o.student_id] || "";
        var course = o.course_code + (o.section ? " · " + o.section : "");
        return '<span class="ga-occ"><b>' + escapeHtml(o.student_id) + "</b> " +
          (name ? escapeHtml(name) + " — " : "") + escapeHtml(course) + "</span>";
      }).join("");
      if (cell.occupants_truncated) {
        $detailBody.innerHTML += '<span class="ga-occ">+' + cell.occupants_truncated +
          " " + t("more", "آخرون") + "</span>";
      }
    }
    $detail.hidden = false;
  }

  // ── Fetch ─────────────────────────────────────────────────
  function compute() {
    var ids = parseIds($ids.value);
    if (!ids.length) {
      setStatus(t("Enter at least one student ID.", "أدخل رقم طالب واحد على الأقل."), true);
      return;
    }
    setStatus(t("Computing…", "جارٍ الحساب…"), false);
    $compute.disabled = true;

    fetch(cfg.computeUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": cfg.csrfToken },
      body: JSON.stringify({ student_ids: ids }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          setStatus((res.body && res.body.error) || t("Request failed.", "فشل الطلب."), true);
          return;
        }
        state.result = res.body;
        state.nameById = {};
        (res.body.students || []).forEach(function (s) { state.nameById[s.student_id] = s.name; });
        $empty.hidden = true;
        $detail.hidden = true;
        renderSummary(res.body);
        renderGrid();
        var termLabel = res.body.academic_year
          ? " · " + t("term ", "الفصل ") + res.body.academic_year + "/" + res.body.term
          : "";
        setStatus(
          t("Showing ", "عرض ") + res.body.resolved_count + t(" of ", " من ") +
          res.body.requested_count + t(" students.", " طلاب.") + termLabel,
          false
        );
      })
      .catch(function () { setStatus(t("Network error.", "خطأ في الشبكة."), true); })
      .finally(function () { $compute.disabled = false; });
  }

  // ── Wire up ───────────────────────────────────────────────
  $compute.addEventListener("click", compute);
  $clear.addEventListener("click", function () {
    $ids.value = "";
    state.result = null;
    $summary.hidden = true;
    $detail.hidden = true;
    $gridWrap.innerHTML = "";
    $gridWrap.appendChild($empty);
    $empty.hidden = false;
    updateCountHint();
    setStatus("", false);
    $ids.focus();
  });
  $ids.addEventListener("input", updateCountHint);
  $detailClose.addEventListener("click", function () {
    $detail.hidden = true;
    document.querySelectorAll(".ga-cell.is-selected").forEach(function (c) {
      c.classList.remove("is-selected");
    });
  });
  $tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      state.grid = tab.getAttribute("data-grid");
      $tabs.forEach(function (x) {
        var active = x === tab;
        x.classList.toggle("is-active", active);
        x.setAttribute("aria-selected", active ? "true" : "false");
      });
      $detail.hidden = true;
      renderGrid();
    });
  });

  updateCountHint();
})();
