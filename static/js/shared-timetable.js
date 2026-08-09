/* shared-timetable.js - shared weekly timetable geometry and rendering.
 *
 * `WeekGrid.renderWeekGrid(options)` returns an HTML string in one of two modes:
 *   - `table` (the backwards-compatible default): a compact semantic table.
 *   - `blocks`: a visual clock calendar. It uses five-minute geometry by default,
 *     labels every 30 minutes, and renders overlaps in side-by-side lanes.
 *
 * Common options: blocks, days, dayLabels, timeLabel, empty, padMinutes,
 *                 dir (or direction), cellHtml(block), bg(block).
 * Block options:  step (default 5), labelStep (default 30), majorHeight
 *                 (pixel height per labelled interval, default 42),
 *                 accent(block), cellClass(block).
 * Table options:  step (default 30), pick(existing, incoming).
 *
 * The block calendar is deliberately aria-hidden. Callers must provide an exact
 * semantic representation outside it (StudentTimetable does this automatically).
 */
(function (global) {
  'use strict';

  var DAY_ORDER = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
  var BASE_DAYS = DAY_ORDER.slice(0, 5);

  function _dayKey(value) {
    var raw = String(value == null ? '' : value).trim().toUpperCase();
    if (!raw) return '';
    var short = raw.slice(0, 3);
    return DAY_ORDER.indexOf(short) !== -1 ? short : raw;
  }

  /**
   * Return the ordinary teaching week (Sun-Thu), followed by any additional
   * days actually present in `blocks`. Known extra days retain week order;
   * custom day codes are appended in first-seen order.
   */
  function deriveDays(blocks, baseDays) {
    var base = Array.isArray(baseDays) ? baseDays : BASE_DAYS;
    var seen = Object.create(null);
    var result = [];
    var extras = [];

    base.forEach(function (day) {
      var key = _dayKey(day);
      if (key && !seen[key]) {
        seen[key] = true;
        result.push(key);
      }
    });

    (blocks || []).forEach(function (block) {
      var key = _dayKey(block && block.day);
      if (key && !seen[key]) {
        seen[key] = true;
        extras.push(key);
      }
    });

    extras.sort(function (a, b) {
      var ai = DAY_ORDER.indexOf(a);
      var bi = DAY_ORDER.indexOf(b);
      if (ai === -1 && bi === -1) return 0;
      if (ai === -1) return 1;
      if (bi === -1) return -1;
      return ai - bi;
    });
    return result.concat(extras);
  }

  /** Minutes since midnight from "HH:MM"; null for anything unparseable. */
  function toMinutes(value) {
    if (value == null) return null;
    var text = String(value).trim();
    if (text.indexOf(':') === -1) return null;
    var parts = text.split(':');
    var hours = Number(parts[0]);
    var minutes = Number(parts[1]);
    if (Number.isNaN(hours) || Number.isNaN(minutes)) return null;
    if (hours < 0 || hours > 24 || minutes < 0 || minutes > 59) return null;
    if (hours === 24 && minutes !== 0) return null;
    return (hours * 60) + minutes;
  }

  function _positiveNumber(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }

  function _direction(opts) {
    var requested = opts.dir || opts.direction;
    return requested === 'rtl' || requested === 'ltr' ? requested : '';
  }

  /* Assign the minimum number of lanes required by each connected overlap
   * group. Touching meetings (one ends exactly as the next begins) do not clash. */
  function _assignOverlapLanes(blocks) {
    var sorted = blocks.slice().sort(function (a, b) {
      return (a._st - b._st) || (a._en - b._en);
    });
    var cluster = [];
    var clusterEnd = -1;

    function finishCluster() {
      if (!cluster.length) return;
      var laneEnds = [];
      cluster.forEach(function (block) {
        var lane = 0;
        while (lane < laneEnds.length && laneEnds[lane] > block._st) lane += 1;
        laneEnds[lane] = block._en;
        block._lane = lane;
      });
      var laneCount = Math.max(1, laneEnds.length);
      cluster.forEach(function (block) { block._laneCount = laneCount; });
      cluster = [];
      clusterEnd = -1;
    }

    sorted.forEach(function (block) {
      if (cluster.length && block._st >= clusterEnd) finishCluster();
      cluster.push(block);
      clusterEnd = Math.max(clusterEnd, block._en);
    });
    finishCluster();
    return sorted;
  }

  /* Shared geometry. Block mode retains every valid meeting in blocksByDay;
   * startsByDay remains for the historical table renderer and its `pick` API. */
  function _prepare(opts) {
    var mode = opts.mode === 'blocks' ? 'blocks' : 'table';
    var step = _positiveNumber(opts.step, mode === 'blocks' ? 5 : 30);
    var labelStep = _positiveNumber(opts.labelStep, mode === 'blocks' ? 30 : step);
    var pad = opts.padMinutes == null ? 30 : Math.max(0, Number(opts.padMinutes) || 0);
    var pick = opts.pick || function (existing) { return existing; };

    var enriched = (opts.blocks || [])
      .map(function (block) {
        var copy = {};
        for (var key in block) {
          if (Object.prototype.hasOwnProperty.call(block, key)) copy[key] = block[key];
        }
        copy.day = _dayKey(block.day);
        copy._st = toMinutes(block.start);
        copy._en = toMinutes(block.end);
        return copy;
      })
      .filter(function (block) {
        return block.day && block._st !== null && block._en !== null && block._en > block._st;
      });

    var days = Array.isArray(opts.days)
      ? opts.days.map(_dayKey).filter(Boolean)
      : deriveDays(enriched);
    if (!enriched.length) {
      return { empty: true, days: days, step: step, labelStep: labelStep };
    }

    var rawMin = Math.min.apply(null, enriched.map(function (block) { return block._st; }));
    var rawMax = Math.max.apply(null, enriched.map(function (block) { return block._en; }));
    var windowStep = mode === 'blocks' ? labelStep : step;
    var startMin = Math.max(0, Math.floor((rawMin - pad) / windowStep) * windowStep);
    var endMin = Math.min(24 * 60, Math.ceil((rawMax + pad) / windowStep) * windowStep);

    var startsByDay = {};
    var blocksByDay = {};
    days.forEach(function (day) {
      startsByDay[day] = {};
      blocksByDay[day] = [];
    });
    enriched.forEach(function (block) {
      if (!startsByDay[block.day]) startsByDay[block.day] = {};
      if (!blocksByDay[block.day]) blocksByDay[block.day] = [];
      var startSlot = Math.floor(block._st / step) * step;
      block.span = Math.max(1, Math.ceil((block._en - startSlot) / step));
      var current = startsByDay[block.day][startSlot];
      startsByDay[block.day][startSlot] = current ? pick(current, block) : block;
      blocksByDay[block.day].push(block);
    });
    Object.keys(blocksByDay).forEach(function (day) {
      blocksByDay[day] = _assignOverlapLanes(blocksByDay[day]);
    });

    return {
      empty: false,
      days: days,
      step: step,
      labelStep: labelStep,
      startMin: startMin,
      endMin: endMin,
      startsByDay: startsByDay,
      blocksByDay: blocksByDay
    };
  }

  function _hhmm(minutes) {
    return String(Math.floor(minutes / 60)).padStart(2, '0') + ':' +
      String(minutes % 60).padStart(2, '0');
  }

  /* `table` mode keeps its established scaffold and collision-pick behaviour. */
  function _renderTable(opts, prepared) {
    var days = prepared.days;
    var dayLabels = opts.dayLabels || {};
    var step = prepared.step;
    var cellHtml = opts.cellHtml || function () { return ''; };
    var bgOf = opts.bg || function () { return ''; };
    var dir = _direction(opts);
    var dirAttr = dir ? ' dir="' + dir + '"' : '';

    var html = '<div class="table-responsive"' + dirAttr + '><table class="table table-sm table-bordered align-middle">';
    html += '<thead><tr><th style="width:70px">' + (opts.timeLabel || '') + '</th>';
    days.forEach(function (day) { html += '<th>' + (dayLabels[day] || day) + '</th>'; });
    html += '</tr></thead><tbody>';

    var carry = {};
    days.forEach(function (day) { carry[day] = 0; });
    for (var time = prepared.startMin; time < prepared.endMin; time += step) {
      html += '<tr><td class="text-secondary">' + _hhmm(time) + '</td>';
      days.forEach(function (day) {
        if (carry[day] > 0) {
          carry[day] -= 1;
          return;
        }
        var meeting = (prepared.startsByDay[day] || {})[time];
        if (!meeting) {
          html += '<td></td>';
          return;
        }
        carry[day] = Math.max(0, (meeting.span || 1) - 1);
        var bg = bgOf(meeting);
        var style = bg ? ' style="background:' + bg + '"' : '';
        html += '<td rowspan="' + (meeting.span || 1) + '"' + style + '>' + cellHtml(meeting) + '</td>';
      });
      html += '</tr>';
    }
    html += '</tbody></table></div>';
    return html;
  }

  /* Visual clock grid. Five-minute rows retain exact meeting boundaries while
   * labels and strong grid rules remain comfortably spaced at 30 minutes. */
  function _renderBlocks(opts, prepared) {
    var days = prepared.days;
    var dayLabels = opts.dayLabels || {};
    var step = prepared.step;
    var labelStep = prepared.labelStep;
    var rows = Math.ceil((prepared.endMin - prepared.startMin) / step);
    var cellHtml = opts.cellHtml || function () { return ''; };
    var bgOf = opts.bg || function () { return ''; };
    var accentOf = opts.accent || function () { return ''; };
    var classOf = opts.cellClass || function () { return ''; };
    var dir = _direction(opts);
    var dirAttr = dir ? ' dir="' + dir + '"' : '';
    var majorHeight = _positiveNumber(opts.majorHeight, 42);
    var rowHeight = majorHeight / Math.max(1, labelStep / step);
    var gridStyle = '--wg-days:' + days.length + ';' +
      '--wg-step:' + step + ';--wg-label-step:' + labelStep + ';' +
      'grid-template-rows:28px repeat(' + rows + ',' + rowHeight + 'px);row-gap:0;';

    var html = '<div class="wg-blocks" style="' + gridStyle + '" aria-hidden="true"' + dirAttr + '>';
    html += '<div class="wg-h wg-cor" style="grid-row:1;grid-column:1">' + (opts.timeLabel || '') + '</div>';
    days.forEach(function (day, dayIndex) {
      html += '<div class="wg-h wg-dh" style="grid-row:1;grid-column:' + (dayIndex + 2) + '">' +
        (dayLabels[day] || day) + '</div>';
    });

    var labelRows = Math.max(1, Math.round(labelStep / step));
    for (var row = 0; row < rows; row += 1) {
      var minute = prepared.startMin + (row * step);
      if (minute % labelStep === 0) {
        html += '<div class="wg-t wg-major" data-minute="' + minute + '" style="grid-row:' +
          (row + 2) + ' / span ' + Math.min(labelRows, rows - row) + ';grid-column:1">' +
          '<bdi dir="ltr">' + _hhmm(minute) + '</bdi></div>';
      }
    }

    days.forEach(function (day, dayIndex) {
      for (var row = 0; row < rows; row += 1) {
        var minute = prepared.startMin + (row * step);
        var major = minute % labelStep === 0;
        var majorStyle = major ? ';box-shadow:inset 0 1px 0 var(--line)' : '';
        html += '<div class="wg-cell ' + (major ? 'wg-major' : 'wg-minor') + '" data-minute="' + minute +
          '" style="grid-row:' + (row + 2) + ';grid-column:' + (dayIndex + 2) + majorStyle + '"></div>';
      }
    });

    days.forEach(function (day, dayIndex) {
      (prepared.blocksByDay[day] || []).forEach(function (meeting) {
        var startRow = Math.floor((meeting._st - prepared.startMin) / step) + 2;
        var endRow = Math.ceil((meeting._en - prepared.startMin) / step) + 2;
        var span = Math.max(1, endRow - startRow);
        var lane = meeting._lane || 0;
        var laneCount = meeting._laneCount || 1;
        var style = 'grid-column:' + (dayIndex + 2) + ';grid-row:' + startRow + ' / span ' + span + ';' +
          '--wg-lane:' + lane + ';--wg-lanes:' + laneCount + ';box-sizing:border-box;';
        if (laneCount > 1) {
          style += 'width:calc(100% / var(--wg-lanes));' +
            'inset-inline-start:calc((100% / var(--wg-lanes)) * var(--wg-lane));';
        }
        var bg = bgOf(meeting);
        if (bg) style += 'background:' + bg + ';';
        var accent = accentOf(meeting);
        if (accent) style += 'border-inline-start-color:' + accent + ';';
        var extra = classOf(meeting);
        html += '<div class="wg-cell wg-filled' + (extra ? ' ' + extra : '') + '"' +
          ' data-start-minute="' + meeting._st + '" data-end-minute="' + meeting._en + '"' +
          ' data-lane="' + lane + '" data-lane-count="' + laneCount + '" style="' + style + '">' +
          cellHtml(meeting) + '</div>';
      });
    });
    html += '</div>';
    return html;
  }

  function renderWeekGrid(options) {
    var opts = options || {};
    var prepared = _prepare(opts);
    if (prepared.empty) return opts.empty || '';
    return opts.mode === 'blocks' ? _renderBlocks(opts, prepared) : _renderTable(opts, prepared);
  }

  global.WeekGrid = {
    renderWeekGrid: renderWeekGrid,
    toMinutes: toMinutes,
    deriveDays: deriveDays,
    DAY_ORDER: DAY_ORDER,
    BASE_DAYS: BASE_DAYS
  };
})(typeof window !== 'undefined' ? window : this);
