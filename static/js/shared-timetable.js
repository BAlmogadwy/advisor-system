/* shared-timetable.js — single source of truth for weekly timetable grids.
 *
 * Phase 1 covers the "clock grid": day columns × continuous clock-time rows
 * (default 30-minute ticks) with blocks that span multiple rows via <td rowspan>.
 * Several pages used to hand-roll this identical scaffold; they now feed
 * normalised blocks + small callbacks here so every clock-style timetable looks
 * and behaves the same and a layout/RTL/dark-mode fix lands in one place.
 *
 * The module is colour- and data-agnostic: callers decide a cell's background
 * (bg) and inner markup (cellHtml), and how to resolve two blocks landing in the
 * same cell (pick). This module owns only the grid geometry.
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

  /**
   * renderWeekGrid(opts) -> HTML string
   *
   * @param {Object}   opts
   * @param {Array}    opts.blocks      [{day, start, end, ...any}] — `day` already
   *                                    normalised to a column key (SUN..THU).
   *                                    Invalid/zero-length times are dropped.
   * @param {Function} opts.cellHtml    (block) => inner HTML for a placed cell.
   *                                    `block` carries the original fields plus
   *                                    `span` (number of rows it occupies).
   * @param {Function} [opts.bg]        (block) => CSS background value. Default ''.
   * @param {Function} [opts.pick]      (existing, incoming) => kept block when two
   *                                    blocks start in the same cell.
   *                                    Default keeps the first (existing).
   * @param {string}   [opts.timeLabel] corner header label. Default ''.
   * @param {string}   [opts.empty]     HTML returned when there are no valid
   *                                    blocks. Default ''.
   * @param {string[]} [opts.days]      column order. Default SUN..THU.
   * @param {Object}   [opts.dayLabels] {SUN:'Sun', ...} header labels.
   * @param {number}   [opts.step]      minutes per row. Default 30.
   * @param {number}   [opts.padMinutes] minutes of padding added before the first
   *                                    and after the last meeting. Default 30.
   * @returns {string} HTML (a .table-responsive wrapper around the grid table).
   */
  function renderWeekGrid(opts) {
    opts = opts || {};
    var days = opts.days || DAY_ORDER;
    var dayLabels = opts.dayLabels || {};
    var step = opts.step || 30;
    var pad = opts.padMinutes == null ? 30 : opts.padMinutes;
    var cellHtml = opts.cellHtml || function () { return ''; };
    var bgOf = opts.bg || function () { return ''; };
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

    if (!enriched.length) return opts.empty || '';

    var rawMin = Math.min.apply(null, enriched.map(function (b) { return b._st; }));
    var rawMax = Math.max.apply(null, enriched.map(function (b) { return b._en; }));
    var startMin = Math.max(0, Math.floor((rawMin - pad) / step) * step);
    var endMin = Math.ceil((rawMax + pad) / step) * step;

    var startsByDay = {};
    days.forEach(function (d) { startsByDay[d] = {}; });
    enriched.forEach(function (b) {
      if (!startsByDay[b.day]) startsByDay[b.day] = {};
      var stSlot = Math.floor(b._st / step) * step;
      var span = Math.max(1, Math.ceil((b._en - stSlot) / step));
      b.span = span;
      var cur = startsByDay[b.day][stSlot];
      startsByDay[b.day][stSlot] = cur ? pick(cur, b) : b;
    });

    var html = '<div class="table-responsive"><table class="table table-sm table-bordered align-middle"><thead><tr>';
    html += '<th style="width:70px">' + (opts.timeLabel || '') + '</th>';
    days.forEach(function (d) { html += '<th>' + (dayLabels[d] || d) + '</th>'; });
    html += '</tr></thead><tbody>';

    var carry = {};
    days.forEach(function (d) { carry[d] = 0; });
    for (var t = startMin; t < endMin; t += step) {
      var hh = String(Math.floor(t / 60)).padStart(2, '0');
      var mm = String(t % 60).padStart(2, '0');
      html += '<tr><td class="text-secondary">' + hh + ':' + mm + '</td>';
      days.forEach(function (d) {
        if (carry[d] > 0) { carry[d] -= 1; return; }
        var m = startsByDay[d][t];
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

  global.WeekGrid = { renderWeekGrid: renderWeekGrid, toMinutes: toMinutes, DAY_ORDER: DAY_ORDER };
})(typeof window !== 'undefined' ? window : this);
