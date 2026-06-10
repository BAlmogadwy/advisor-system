/* shared-timetable.js — single source of truth for weekly timetable grids.
 *
 * Covers the "clock grid": day columns × continuous clock-time rows (default
 * 30-minute ticks) with blocks that span multiple rows. Two visual modes share
 * one geometry pass (_prepare):
 *   - mode:'table'  (default) — Bootstrap <table> with <td rowspan> (compact list look)
 *   - mode:'blocks'           — themed CSS-grid of pastel cells with an accent
 *                               left-border + clash markers (the "split-screen" look)
 *
 * The module is colour- and data-agnostic: callers decide a cell's background
 * (bg), accent border (accent), extra classes (cellClass) and inner markup
 * (cellHtml), plus how to resolve two blocks landing in the same cell (pick).
 * This module owns only grid geometry + scaffold.
 *
 * Exposed as the global `WeekGrid` (the codebase uses plain globals, not ES
 * modules — see shared-utils.js / shared-ux.js).
 */
(function (global) {
  'use strict';

  var DAY_ORDER = ['SUN', 'MON', 'TUE', 'WED', 'THU'];

  /** Minutes-since-midnight from "HH:MM"; null for anything unparseable. */
  function toMinutes(t) {
    if (t == null) return null;
    var s = String(t).trim();
    if (s.indexOf(':') === -1) return null;
    var parts = s.split(':');
    var h = Number(parts[0]);
    var m = Number(parts[1]);
    if (Number.isNaN(h) || Number.isNaN(m)) return null;
    return h * 60 + m;
  }

  /* Shared geometry: filter valid blocks, compute the visible time window, and
   * bucket each block by day + start tick (carrying its row span). Used by both
   * render modes so they never disagree on layout. */
  function _prepare(opts) {
    var days = opts.days || DAY_ORDER;
    var step = opts.step || 30;
    var pad = opts.padMinutes == null ? 30 : opts.padMinutes;
    var pick = opts.pick || function (existing) { return existing; };

    var enriched = (opts.blocks || [])
      .map(function (b) {
        var copy = {};
        for (var k in b) { if (Object.prototype.hasOwnProperty.call(b, k)) copy[k] = b[k]; }
        copy._st = toMinutes(b.start);
        copy._en = toMinutes(b.end);
        return copy;
      })
      .filter(function (b) { return b._st !== null && b._en !== null && b._en > b._st; });

    if (!enriched.length) return { empty: true, days: days, step: step };

    var rawMin = Math.min.apply(null, enriched.map(function (b) { return b._st; }));
    var rawMax = Math.max.apply(null, enriched.map(function (b) { return b._en; }));
    var startMin = Math.max(0, Math.floor((rawMin - pad) / step) * step);
    var endMin = Math.ceil((rawMax + pad) / step) * step;

    var startsByDay = {};
    days.forEach(function (d) { startsByDay[d] = {}; });
    enriched.forEach(function (b) {
      if (!startsByDay[b.day]) startsByDay[b.day] = {};
      var stSlot = Math.floor(b._st / step) * step;
      b.span = Math.max(1, Math.ceil((b._en - stSlot) / step));
      var cur = startsByDay[b.day][stSlot];
      startsByDay[b.day][stSlot] = cur ? pick(cur, b) : b;
    });

    return { empty: false, days: days, step: step, startMin: startMin, endMin: endMin, startsByDay: startsByDay };
  }

  function _hhmm(t) {
    return String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
  }

  /* mode:'table' — preserves the historical Bootstrap-table output byte-for-byte. */
  function _renderTable(opts, p) {
    var days = p.days, dayLabels = opts.dayLabels || {}, step = p.step;
    var cellHtml = opts.cellHtml || function () { return ''; };
    var bgOf = opts.bg || function () { return ''; };

    var html = '<div class="table-responsive"><table class="table table-sm table-bordered align-middle"><thead><tr>';
    html += '<th style="width:70px">' + (opts.timeLabel || '') + '</th>';
    days.forEach(function (d) { html += '<th>' + (dayLabels[d] || d) + '</th>'; });
    html += '</tr></thead><tbody>';

    var carry = {};
    days.forEach(function (d) { carry[d] = 0; });
    for (var t = p.startMin; t < p.endMin; t += step) {
      html += '<tr><td class="text-secondary">' + _hhmm(t) + '</td>';
      days.forEach(function (d) {
        if (carry[d] > 0) { carry[d] -= 1; return; }
        var m = p.startsByDay[d][t];
        if (!m) { html += '<td></td>'; return; }
        carry[d] = Math.max(0, (m.span || 1) - 1);
        var bg = bgOf(m);
        var style = bg ? ' style="background:' + bg + '"' : '';
        html += '<td rowspan="' + (m.span || 1) + '"' + style + '>' + cellHtml(m) + '</td>';
      });
      html += '</tr>';
    }
    html += '</tbody></table></div>';
    return html;
  }

  /* mode:'blocks' — themed CSS-grid (the split-screen look). Empty cells paint
   * the grid lines; placed blocks overlay them via explicit grid-row spans. */
  function _renderBlocks(opts, p) {
    var days = p.days, dayLabels = opts.dayLabels || {}, step = p.step;
    var rows = (p.endMin - p.startMin) / step;
    var cellHtml = opts.cellHtml || function () { return ''; };
    var bgOf = opts.bg || function () { return ''; };
    var accentOf = opts.accent || function () { return ''; };
    var classOf = opts.cellClass || function () { return ''; };

    var h = '<div class="wg-blocks" style="--wg-days:' + days.length + '" role="grid">';
    // Header row (row 1): time corner + day labels.
    h += '<div class="wg-h wg-cor" style="grid-row:1;grid-column:1">' + (opts.timeLabel || '') + '</div>';
    days.forEach(function (d, di) {
      h += '<div class="wg-h wg-dh" style="grid-row:1;grid-column:' + (di + 2) + '">' + (dayLabels[d] || d) + '</div>';
    });
    // Time labels (column 1) + empty day cells — paint the background grid.
    for (var i = 0; i < rows; i++) {
      h += '<div class="wg-t" style="grid-row:' + (i + 2) + ';grid-column:1">' + _hhmm(p.startMin + i * step) + '</div>';
    }
    days.forEach(function (d, di) {
      for (var r = 0; r < rows; r++) {
        h += '<div class="wg-cell" style="grid-row:' + (r + 2) + ';grid-column:' + (di + 2) + '"></div>';
      }
    });
    // Placed blocks overlay the empty cells (later in source → on top).
    days.forEach(function (d, di) {
      var byTick = p.startsByDay[d] || {};
      Object.keys(byTick).forEach(function (slotKey) {
        var m = byTick[slotKey];
        var startTick = (Number(slotKey) - p.startMin) / step;
        if (startTick < 0) return;
        var style = 'grid-column:' + (di + 2) + ';grid-row:' + (startTick + 2) + ' / span ' + (m.span || 1) + ';';
        var bg = bgOf(m); if (bg) style += 'background:' + bg + ';';
        var accent = accentOf(m); if (accent) style += 'border-inline-start-color:' + accent + ';';
        var extra = classOf(m);
        h += '<div class="wg-cell wg-filled' + (extra ? ' ' + extra : '') + '" style="' + style + '">' + cellHtml(m) + '</div>';
      });
    });
    h += '</div>';
    return h;
  }

  /**
   * renderWeekGrid(opts) -> HTML string. See file header for opts.
   *  Common: blocks, days, dayLabels, step, padMinutes, timeLabel, empty,
   *          cellHtml(block), bg(block), pick(existing,incoming).
   *  mode:'blocks' also reads: accent(block), cellClass(block).
   */
  function renderWeekGrid(opts) {
    opts = opts || {};
    var p = _prepare(opts);
    if (p.empty) return opts.empty || '';
    return opts.mode === 'blocks' ? _renderBlocks(opts, p) : _renderTable(opts, p);
  }

  global.WeekGrid = { renderWeekGrid: renderWeekGrid, toMinutes: toMinutes, DAY_ORDER: DAY_ORDER };
})(typeof window !== 'undefined' ? window : this);
