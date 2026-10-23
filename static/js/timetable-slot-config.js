/* Timetable slot columns: editing them without losing data.

   Shared by the Timetable Workspace, its split view and the MRI page. They
   draw and edit the scenario's EFFECTIVE columns (`effective_slot_config`,
   `effective_lab_slot_config`: its own, or the server's defaults) and never
   defaults of their own; the server keeps each column's flags (online-only
   evening windows) when a save names the same start and end.

   A save can come back with two answers this module handles:
   - VALIDATION_SLOTS: which column is wrong, in the user's language;
   - SLOT_EDIT_NEEDS_CONFIRMATION: the meetings the change would leave off the
     grid, and the flagged columns it would remove. The user sees them by name
     and confirms, and the save is repeated with the server's fingerprint of
     exactly that list.

   The workspace's slot dialog (design record §7.2 item 6, 6f) edits the
   columns as rows: `editorRows` makes them, `rowsToPayload` is what a save
   sends, and `rowWords`, `lengthText` and `fieldErrorText` are the words of a
   row. `minutes` reads a time as the server does; the workspace's grid reads
   its cells with it too (6e). */
(function () {
  'use strict';
  const AR = document.documentElement.lang === 'ar';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // Plain text (an aria-label) keeps a name the user typed apart from the words around it.
  const isolate = text => `\u2066${text}\u2069`;

  /* A stored time as the server reads it (timetable_slot_config._TIME and
     _minutes): H:MM or HH:MM, seconds allowed, hours 0–23 — "9:00" and
     "09:00:00" are 09:00 — and anything else is no time (null). The one
     client copy of that rule. */
  const SERVER_TIME = /^\s*([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?\s*$/;
  function minutes(value) {
    const match = SERVER_TIME.exec(value == null ? '' : String(value));
    return match ? Number(match[1]) * 60 + Number(match[2]) : null;
  }
  const DAY = AR
    ? { SUN: 'الأحد', MON: 'الاثنين', TUE: 'الثلاثاء', WED: 'الأربعاء', THU: 'الخميس' }
    : { SUN: 'Sun', MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu' };
  const time = (start, end) => `<bdi dir="ltr">${escape(start)}–${escape(end)}</bdi>`;

  /* "N meetings": in Arabic through the global plural helper (shared-utils). */
  const meetingsText = n => (AR
    ? pluralAr(n, { zero: 'لا لقاءات', one: 'لقاءً واحداً', two: 'لقاءين', few: '{n} لقاءات', many: '{n} لقاءً', other: '{n} لقاء' })
    : `${n} ${n === 1 ? 'meeting' : 'meetings'}`);

  /* The columns as the editor's text: one per line, label TAB start TAB end. */
  function rowsToText(slots) {
    return (slots || []).map(slot => `${slot.label || ''}\t${slot.start}\t${slot.end}`).join('\n');
  }

  /* The editor's text as columns. Tabs or runs of spaces separate the parts;
     Windows line endings are fine. A line that cannot be read is returned by
     its number, never dropped: an editor saves nothing while one remains (a
     list emptied that way would quietly become the defaults). Flags are not
     typed: the server carries them over from the column with the same start
     and end. */
  function parseLines(text) {
    const rows = [];
    const rejected = [];
    String(text || '').replace(/\r\n/g, '\n').split('\n').forEach((line, index) => {
      if (!line.trim()) return;
      const parts = line.split(/\t|\s{2,}/).map(part => part.trim()).filter(Boolean);
      const isTime = part => /^\d{1,2}:\d{2}$/.test(part);
      if (parts.length >= 3 && isTime(parts[parts.length - 2]) && isTime(parts[parts.length - 1])) {
        rows.push({ label: parts.slice(0, -2).join(' '), start: parts[parts.length - 2], end: parts[parts.length - 1] });
      } else if (parts.length === 2 && isTime(parts[0]) && isTime(parts[1])) {
        rows.push({ label: `${parts[0]}-${parts[1]}`, start: parts[0], end: parts[1] });
      } else {
        const match = line.trim().match(/^(.+?)\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})$/);
        if (match) rows.push({ label: match[1], start: match[2], end: match[3] });
        else rejected.push(index + 1);
      }
    });
    return { rows, rejected };
  }
  const parseText = text => parseLines(text).rows;

  /* Lines an editor could not read, as one sentence. */
  function rejectedText(lectureLines, labLines) {
    const part = (lines, en, ar) => (lines.length ? (AR ? `${ar}: السطر ${lines.join('، ')}` : `${en}: line ${lines.join(', ')}`) : '');
    const parts = [part(lectureLines, 'Lectures', 'المحاضرات'), part(labLines, 'Labs', 'المعامل')].filter(Boolean);
    return AR
      ? `لم يُحفظ شيء — لم تُفهم هذه الأسطر (الاسم ثم البداية ثم النهاية): ${parts.join('؛ ')}`
      : `Nothing was saved — these lines were not understood (label, start, end): ${parts.join('; ')}`;
  }

  /* What a flagged column is, for the editor's hint. */
  function flaggedColumnsText(slots) {
    const online = (slots || []).filter(slot => slot.online_only).map(slot => slot.start);
    if (!online.length) return '';
    return AR
      ? `أعمدة «عن بُعد فقط» (${online.join('، ')}) تبقى كذلك ما دامت بالبداية والنهاية نفسيهما.`
      : `The online-only columns (${online.join(', ')}) stay online-only while they keep the same start and end.`;
  }

  const LIST = { slot_config: AR ? 'المحاضرات' : 'Lectures', lab_slot_config: AR ? 'المعامل' : 'Labs' };
  const FIELD = AR
    ? { start: 'البداية', end: 'النهاية', row: 'السطر', list: 'القائمة' }
    : { start: 'start', end: 'end', row: 'row', list: 'list' };
  const problemText = message => {
    if (!AR) return message;
    const tooMany = /^has more than (\d+) columns$/.exec(message);
    if (tooMany) return `فيها أكثر من ${tooMany[1]} عموداً`;
    return {
      'must be a time as HH:MM': 'يجب أن تكون وقتاً بالصيغة HH:MM', 'must be after the start': 'يجب أن تكون بعد البداية',
      'repeats another column': 'تكرر عموداً آخر', 'must be an object': 'غير صالح', 'must be an array': 'غير صالحة',
    }[message] || message;
  };

  /* ── The slot dialog's rows (6f) ──
     One row per column, {key, label, start, end, flags}. The key is the
     row's own for the dialog's life: rows move and go while it is open, and
     the server names a wrong column by the index it was sent at. The flags
     are the column's (online_only, policy_only): shown, never edited and
     never sent, since the server carries each over by start and end. */
  const FLAGS = ['online_only', 'policy_only'];
  let rowSerial = 0;
  function editorRows(slots) {
    return (slots || []).map(slot => ({
      key: `r${++rowSerial}`,
      label: String(slot?.label ?? ''),
      start: String(slot?.start ?? ''),
      end: String(slot?.end ?? ''),
      flags: FLAGS.filter(flag => slot?.[flag]),
    }));
  }

  /* The rows as a save sends them: label, start and end, trimmed, an empty
     value kept (the server names an empty time; an empty label is its
     "start-end"), and no flag. */
  function rowsToPayload(rows) {
    const text = value => String(value ?? '').trim();
    return (rows || []).map(row => ({ label: text(row.label), start: text(row.start), end: text(row.end) }));
  }

  /* A row's length, for display only: nothing is checked here (the server
     marks a wrong time); a start or end that is no time, or an end not after
     the start, has no length. */
  function lengthText(start, end) {
    const from = minutes(start);
    const to = minutes(end);
    if (from === null || to === null || to <= from) return '—';
    const n = to - from;
    return AR
      ? pluralAr(n, { one: 'دقيقة واحدة', two: 'دقيقتان', few: '{n} دقائق', many: '{n} دقيقةً', other: '{n} دقيقة' })
      : `${n} min`;
  }

  /* A row's words: the table's headers, the row header's hidden lead, a
     flag, and the row's buttons, each named by the row (`name`: its label, or
     its list's word and number). */
  const ROW_WORDS = AR ? {
    headers: { label: 'الاسم', start: 'البداية', end: 'النهاية', length: 'المدة', rule: 'القيد' },
    column: { slot_config: 'عمود محاضرات', lab_slot_config: 'عمود معامل' },
    flag: { online_only: 'عن بُعد فقط', policy_only: 'عمود سياسة' },
    up: name => `نقل ${isolate(name)} إلى الأعلى`,
    down: name => `نقل ${isolate(name)} إلى الأسفل`,
    remove: name => `حذف ${isolate(name)}`,
  } : {
    headers: { label: 'Label', start: 'Start', end: 'End', length: 'Length', rule: 'Rule' },
    column: { slot_config: 'Lecture column', lab_slot_config: 'Lab column' },
    flag: { online_only: 'Online only', policy_only: 'Policy column' },
    up: name => `Move ${isolate(name)} up`,
    down: name => `Move ${isolate(name)} down`,
    remove: name => `Remove ${isolate(name)}`,
  };

  /* One wrong field of a VALIDATION_SLOTS answer, under its row:
     "{field}: {problem}", the field named as its column header. */
  function fieldErrorText(error) {
    const field = ROW_WORDS.headers[error.field] || FIELD[error.field] || error.field;
    return `${field}: ${problemText(error.message)}`;
  }

  /* A VALIDATION_SLOTS answer as one sentence per wrong column. */
  function errorsText(errors) {
    const lines = (errors || []).slice(0, 6).map(error => {
      const where = error.index >= 0
        ? (AR ? `${LIST[error.list] || error.list}، السطر ${error.index + 1}` : `${LIST[error.list] || error.list}, line ${error.index + 1}`)
        : (LIST[error.list] || error.list);
      const field = error.field === 'list' ? '' : `${FIELD[error.field] || error.field} `;
      return `${where}: ${field}${problemText(error.message)}`;
    });
    const head = AR ? 'لم تُحفظ الأعمدة:' : 'The columns were not saved:';
    return `${head} ${lines.join(AR ? '؛ ' : '; ')}`;
  }

  /* What the change asks the user to accept, for the confirm dialog: the
     meetings it leaves off the grid, and the flagged columns it removes.
     `parked`: the page draws a meeting off the grid in a strip under its
     table (the workspace, design record §7.2 item 6, 6e), so the lead says
     where it will be; without it the words are the ones a page that does not
     draw such a meeting needs (split view). */
  function confirmationHtml(details, { parked = false } = {}) {
    const hidden = details.hidden || [];
    const count = details.hidden_count ?? hidden.length;
    let html = '';
    if (count) {
      const lead = parked
        ? (AR
          ? `هذا التغيير يترك ${meetingsText(count)} خارج أعمدة الجدول. تبقى في الجدول وتُنشر، ويظهر كلٌّ منها أسفل جدوله حتى يعود عمود بوقته:`
          : `This change leaves ${meetingsText(count)} outside the grid's columns. They stay in the timetable and are still published; each is shown under its table until a column has its time again:`)
        : AR
          ? `هذا التغيير يترك ${meetingsText(count)} خارج أعمدة الجدول. تبقى في الجدول وتُنشر، لكن لا يظهر أي منها في الشبكة حتى تعود أعمدة بأوقاتها:`
          : `This change leaves ${meetingsText(count)} outside the grid's columns. They stay in the timetable and are still published, but none is drawn on the grid until a column has its time again:`;
      const rows = hidden.map(item => {
        const locked = item.is_locked ? (AR ? ' · الوقت مقفل' : ' · time locked') : '';
        return `<li><bdi>${escape(item.board_label)}</bdi> · <bdi>${escape(item.course_code)} ${escape(item.section)}</bdi> · ${escape(DAY[item.day] || item.day)} ${time(item.start_time, item.end_time)}${locked}</li>`;
      }).join('');
      const more = count > hidden.length
        ? `<p class="tw-readiness-lead">${AR ? `عدد إضافي غير معروض: ${count - hidden.length}.` : `And ${meetingsText(count - hidden.length)} more.`}</p>` : '';
      html += `<p class="tw-readiness-lead">${lead}</p><ul class="tw-readiness-list">${rows}</ul>${more}`;
    }
    const dropped = details.dropped_flags || [];
    if (dropped.length) {
      const lead = AR
        ? 'وهذه الأعمدة تفقد قيدها؛ العمود الذي لم يعد «عن بُعد فقط» يمكن أن يضع فيه المُجدول الآلي محاضرات تحتاج قاعة:'
        : 'And these columns lose their rule; a column that is no longer online-only is one the automatic placers may put room-using classes in:';
      const rows = dropped.map(column => {
        const what = (column.flags || []).includes('online_only') ? (AR ? 'عن بُعد فقط' : 'online-only') : (AR ? 'عمود عرض للسياسة' : 'policy display column');
        return `<li>${escape(LIST[column.list] || column.list)} · <bdi>${escape(column.label)}</bdi> ${time(column.start, column.end)} · ${what}</li>`;
      }).join('');
      html += `<p class="tw-readiness-lead">${lead}</p><ul class="tw-readiness-list">${rows}</ul>`;
    }
    return html;
  }

  /* Save the columns.
     send(extra) posts the body plus `extra` and resolves to {data, error}:
     data on success; error = the server's {code, details} for the two codes
     above (any other failure is reported by the page's own fetch helper and
     resolves to {data: null}). confirm(details) asks the user and resolves to
     true to go on. report(text) shows why nothing was saved.
     invalid(errors), when given, hears a VALIDATION_SLOTS answer's columns
     ({list, index, field, message}) first, so a page can mark the fields it
     sent; report follows with the same answer as a sentence.
     Resolves to the saved answer, or null. */
  async function save({ send, confirm, report, invalid }) {
    let extra = {};
    // Another answer after a confirmation means the list changed while the
    // user read it (another edit landed): show the new list, a few times.
    for (let attempt = 0; attempt < 4; attempt++) {
      const { data, error } = await send(extra);
      if (!error) return data || null;
      if (error.code === 'VALIDATION_SLOTS') {
        if (typeof invalid === 'function') invalid(error.details?.errors || []);
        report(errorsText(error.details?.errors));
        return null;
      }
      if (error.code !== 'SLOT_EDIT_NEEDS_CONFIRMATION') return null;
      if (!(await confirm(error.details || {}))) return null;
      extra = { confirm: error.details?.fingerprint };
    }
    report(AR
      ? 'تغيّر الجدول أثناء المراجعة أكثر من مرة؛ لم يُحفظ شيء. أعد المحاولة.'
      : 'The timetable kept changing while you reviewed it; nothing was saved. Try again.');
    return null;
  }

  /* The columns a manual move may offer a meeting: never a policy column, and
     an online-only (evening) column only to a meeting already in one, so a
     room-consuming class is never offered an evening window. */
  function offeredColumns(slots, meetingStart) {
    const inOnline = (slots || []).some(slot => slot.online_only && slot.start === String(meetingStart || '').slice(0, 5));
    return (slots || []).filter(slot => !slot.policy_only && (!slot.online_only || inOnline));
  }

  window.TimetableSlotConfig = {
    rowsToText, parseText, parseLines, rejectedText, flaggedColumnsText, errorsText,
    confirmationHtml, save, offeredColumns, meetingsText,
    minutes, editorRows, rowsToPayload, lengthText, fieldErrorText, rowWords: ROW_WORDS,
  };
})();
