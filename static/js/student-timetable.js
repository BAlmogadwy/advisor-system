/* student-timetable.js - student-facing adapter for the shared WeekGrid.
 *
 * API:
 *   StudentTimetable.render(host, meetings, options) -> wrapper element
 *   StudentTimetable.normalizeMeetings(meetings) -> normalized meeting array
 *
 * `host` is an element or selector. `meetings` is either a flat array or
 * `{current: [], proposed: []}`. Accepted fields include day, start/end or
 * start_time/end_time, course_code/code, course_name/name, section, room,
 * instructor, and source (`current`, `proposed`, `baseline`, or `planned`).
 *
 * Options: lang/locale, dir, dayLabels, timeLabel, emptyText, agendaLabel,
 * days, padMinutes, labelStep, majorHeight, compressGaps,
 * compressGapMinutes, showCourseName, showSource, currentLabel, proposedLabel.
 * The output always pairs an aria-hidden visual calendar with an exact,
 * semantic day-by-day agenda. All meeting and option text is escaped.
 */
(function (global) {
  'use strict';

  var instanceCount = 0;
  var DAY_ORDER = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
  var DAY_ALIASES = {
    SUNDAY: 'SUN', MONDAY: 'MON', TUESDAY: 'TUE', WEDNESDAY: 'WED',
    THURSDAY: 'THU', FRIDAY: 'FRI', SATURDAY: 'SAT',
    'الأحد': 'SUN', 'الاحد': 'SUN', 'الإثنين': 'MON', 'الاثنين': 'MON',
    'الثلاثاء': 'TUE', 'الأربعاء': 'WED', 'الاربعاء': 'WED',
    'الخميس': 'THU', 'الجمعة': 'FRI', 'السبت': 'SAT'
  };
  var LABELS = {
    en: {
      days: { SUN: 'Sunday', MON: 'Monday', TUE: 'Tuesday', WED: 'Wednesday', THU: 'Thursday', FRI: 'Friday', SAT: 'Saturday' },
      time: 'Time', empty: 'No scheduled meetings.', agenda: 'Weekly agenda by day',
      current: 'Current', proposed: 'Proposed',
      gap: 'No scheduled meetings during this interval'
    },
    ar: {
      days: { SUN: 'الأحد', MON: 'الاثنين', TUE: 'الثلاثاء', WED: 'الأربعاء', THU: 'الخميس', FRI: 'الجمعة', SAT: 'السبت' },
      time: 'الوقت', empty: 'لا تتوفر مواعيد دراسية لعرضها في هذا الجدول.', agenda: 'تفاصيل الجدول الأسبوعي مرتبة حسب اليوم',
      current: 'الجدول المسجّل فعليًا', proposed: 'الجدول المقترح',
      gap: 'لا توجد محاضرات مجدولة خلال هذه الفترة'
    }
  };

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function dayKey(value) {
    var raw = String(value == null ? '' : value).trim();
    if (!raw) return '';
    if (DAY_ALIASES[raw]) return DAY_ALIASES[raw];
    var upper = raw.toUpperCase();
    if (DAY_ALIASES[upper]) return DAY_ALIASES[upper];
    var short = upper.slice(0, 3);
    return DAY_ORDER.indexOf(short) !== -1 ? short : upper;
  }

  function sourceKey(value) {
    var source = String(value == null ? '' : value).trim().toLowerCase();
    return source === 'proposed' || source === 'planned' || source === 'recommended' || source === 'new'
      ? 'proposed' : 'current';
  }

  function collectMeetings(input) {
    if (Array.isArray(input)) return input.slice();
    if (!input || typeof input !== 'object') return [];
    var result = [];
    (input.current || []).forEach(function (meeting) {
      result.push(Object.assign({}, meeting, { source: 'current' }));
    });
    (input.proposed || []).forEach(function (meeting) {
      result.push(Object.assign({}, meeting, { source: 'proposed' }));
    });
    return result;
  }

  function normalizeMeetings(input) {
    return collectMeetings(input).map(function (meeting, index) {
      var start = String(meeting.start != null ? meeting.start : (meeting.start_time || '')).trim();
      var end = String(meeting.end != null ? meeting.end : (meeting.end_time || '')).trim();
      return {
        day: dayKey(meeting.day),
        start: start,
        end: end,
        course_code: String(meeting.course_code || meeting.code || meeting.course || '').trim(),
        course_name: String(meeting.course_name || meeting.name || '').trim(),
        section: String(meeting.section || meeting.section_code || '').trim(),
        room: String(meeting.room || '').trim(),
        instructor: String(meeting.instructor || '').trim(),
        source: sourceKey(meeting.source),
        _inputIndex: index
      };
    }).filter(function (meeting) {
      if (!meeting.day || !global.WeekGrid) return false;
      var start = global.WeekGrid.toMinutes(meeting.start);
      var end = global.WeekGrid.toMinutes(meeting.end);
      return start !== null && end !== null && end > start;
    }).sort(function (a, b) {
      var ai = DAY_ORDER.indexOf(a.day);
      var bi = DAY_ORDER.indexOf(b.day);
      if (ai === -1) ai = DAY_ORDER.length;
      if (bi === -1) bi = DAY_ORDER.length;
      return (ai - bi) || (global.WeekGrid.toMinutes(a.start) - global.WeekGrid.toMinutes(b.start)) ||
        (a._inputIndex - b._inputIndex);
    });
  }

  function resolveHost(host) {
    return typeof host === 'string' ? global.document.querySelector(host) : host;
  }

  function localeOptions(options) {
    var opts = options || {};
    var language = String(opts.lang || opts.locale || '').toLowerCase();
    var dir = opts.dir === 'rtl' || opts.dir === 'ltr'
      ? opts.dir : (language.indexOf('ar') === 0 ? 'rtl' : 'ltr');
    var locale = language.indexOf('ar') === 0 || dir === 'rtl' ? 'ar' : 'en';
    var defaults = LABELS[locale];
    var labels = {};
    DAY_ORDER.forEach(function (day) {
      labels[day] = (opts.dayLabels && opts.dayLabels[day]) || defaults.days[day];
    });
    return {
      dir: dir,
      labels: labels,
      timeLabel: opts.timeLabel == null ? defaults.time : opts.timeLabel,
      emptyText: opts.emptyText == null ? defaults.empty : opts.emptyText,
      agendaLabel: opts.agendaLabel == null ? defaults.agenda : opts.agendaLabel,
      currentLabel: opts.currentLabel == null ? defaults.current : opts.currentLabel,
      proposedLabel: opts.proposedLabel == null ? defaults.proposed : opts.proposedLabel,
      gapLabel: opts.gapLabel == null ? defaults.gap : opts.gapLabel
    };
  }

  function agendaMeeting(meeting, options, text, showSource) {
    var sourceClass = 'student-timetable-meeting--' + meeting.source;
    var html = '<li class="student-timetable-meeting ' + sourceClass + '">';
    html += '<article><div class="student-timetable-meeting-head">' +
      '<bdi dir="ltr" class="student-timetable-code">' + esc(meeting.course_code || '—') + '</bdi>';
    if (meeting.section) {
      html += '<bdi dir="ltr" class="student-timetable-section">' + esc(meeting.section) + '</bdi>';
    }
    if (showSource) {
      html += '<span class="student-timetable-source student-timetable-source--' + meeting.source + '">' +
        esc(meeting.source === 'proposed' ? text.proposedLabel : text.currentLabel) + '</span>';
    }
    html += '</div><span class="student-timetable-time" dir="ltr">' +
      '<time datetime="' + esc(meeting.start) + '">' + esc(meeting.start) + '</time>' +
      '<span aria-hidden="true">–</span><time datetime="' + esc(meeting.end) + '">' + esc(meeting.end) + '</time></span>';
    if (options.showCourseName !== false && meeting.course_name) {
      html += '<div class="student-timetable-name" dir="auto">' + esc(meeting.course_name) + '</div>';
    }
    if (meeting.room || meeting.instructor) {
      html += '<div class="student-timetable-meta" dir="auto">';
      if (meeting.room) html += '<span class="student-timetable-room">' + esc(meeting.room) + '</span>';
      if (meeting.room && meeting.instructor) html += '<span aria-hidden="true"> · </span>';
      if (meeting.instructor) html += '<span class="student-timetable-instructor">' + esc(meeting.instructor) + '</span>';
      html += '</div>';
    }
    return html + '</article></li>';
  }

  function render(host, meetings, options) {
    if (!global.WeekGrid) throw new Error('StudentTimetable requires WeekGrid.');
    var target = resolveHost(host);
    if (!target || typeof target.innerHTML === 'undefined') throw new TypeError('StudentTimetable.render requires a valid host.');
    var opts = options || {};
    var text = localeOptions(opts);
    var normalized = normalizeMeetings(meetings);
    var days = Array.isArray(opts.days)
      ? opts.days.map(dayKey).filter(Boolean)
      : global.WeekGrid.deriveDays(normalized);
    var safeDayLabels = {};
    days.forEach(function (day) { safeDayLabels[day] = esc(text.labels[day] || day); });
    var hasProposed = normalized.some(function (meeting) { return meeting.source === 'proposed'; });
    var showSource = opts.showSource == null ? hasProposed : Boolean(opts.showSource);
    var idPrefix = 'student-timetable-' + (++instanceCount);

    var html = '<div class="student-timetable" dir="' + text.dir + '">';
    if (!normalized.length) {
      html += '<p class="student-timetable-empty">' + esc(text.emptyText) + '</p></div>';
      target.innerHTML = html;
      return target.firstElementChild;
    }

    var grid = global.WeekGrid.renderWeekGrid({
      mode: 'blocks',
      blocks: normalized,
      days: days,
      dayLabels: safeDayLabels,
      timeLabel: esc(text.timeLabel),
      dir: text.dir,
      padMinutes: opts.padMinutes == null ? 0 : opts.padMinutes,
      labelStep: opts.labelStep == null ? 60 : opts.labelStep,
      majorHeight: opts.majorHeight == null ? 72 : opts.majorHeight,
      timeColumnWidth: opts.timeColumnWidth == null ? 60 : opts.timeColumnWidth,
      dayMinWidth: opts.dayMinWidth == null ? 124 : opts.dayMinWidth,
      minWidth: opts.minWidth == null ? 720 : opts.minWidth,
      compressGaps: opts.compressGaps !== false,
      compressGapMinutes: opts.compressGapMinutes,
      segmentPadMinutes: opts.segmentPadMinutes,
      gapLabel: esc(text.gapLabel),
      cellClass: function (meeting) {
        return 'student-timetable-block student-timetable-block--' + meeting.source;
      },
      cellHtml: function (meeting) {
        var block = '<span class="student-timetable-block-head"><bdi dir="ltr" class="wg-cid">' +
          esc(meeting.course_code || '—') + '</bdi>';
        if (meeting.section) block += '<bdi dir="ltr" class="student-timetable-block-section">' + esc(meeting.section) + '</bdi>';
        block += '</span><time class="wg-meta student-timetable-block-time" dir="ltr">' +
          esc(meeting.start) + '–' + esc(meeting.end) + '</time>';
        if (opts.showCourseName !== false && meeting.course_name) {
          block += '<span class="student-timetable-block-name" dir="auto">' + esc(meeting.course_name) + '</span>';
        }
        if (showSource) {
          block += '<span class="student-timetable-block-source">' +
            esc(meeting.source === 'proposed' ? text.proposedLabel : text.currentLabel) + '</span>';
        }
        return block;
      }
    });
    html += '<div class="student-timetable-calendar" tabindex="0" role="region" aria-label="' +
      esc(text.agendaLabel) + '">' + grid + '</div>';
    html += '<div class="student-timetable-agenda" aria-label="' + esc(text.agendaLabel) + '">';
    days.forEach(function (day) {
      var items = normalized.filter(function (meeting) { return meeting.day === day; });
      if (!items.length) return;
      var headingId = idPrefix + '-day-' + day.toLowerCase();
      html += '<section class="student-timetable-day" aria-labelledby="' + headingId + '">' +
        '<h3 class="student-timetable-day-title" id="' + headingId + '">' + esc(text.labels[day] || day) + '</h3>' +
        '<ol class="student-timetable-list">';
      items.forEach(function (meeting) { html += agendaMeeting(meeting, opts, text, showSource); });
      html += '</ol></section>';
    });
    html += '</div></div>';
    target.innerHTML = html;
    return target.firstElementChild;
  }

  global.StudentTimetable = {
    render: render,
    normalizeMeetings: normalizeMeetings
  };
})(typeof window !== 'undefined' ? window : this);
