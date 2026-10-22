/* Timetable slot columns: editing them without losing data.

   Shared by the Timetable Workspace and its split view. Both pages draw and
   edit the scenario's EFFECTIVE columns (`effective_slot_config`,
   `effective_lab_slot_config`: its own, or the server's defaults) and never
   defaults of their own; the server keeps each column's flags (online-only
   evening windows) when a save names the same start and end.

   A save can come back with two answers this module handles:
   - VALIDATION_SLOTS: which column is wrong, in the user's language;
   - SLOT_EDIT_HIDES_PLACEMENTS: the meetings the change would leave off the
     grid. The user sees them by name and confirms, and the save is repeated
     with the server's fingerprint of exactly that set. */
(function () {
  'use strict';
  const AR = document.documentElement.lang === 'ar';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const DAY = AR
    ? { SUN: 'الأحد', MON: 'الاثنين', TUE: 'الثلاثاء', WED: 'الأربعاء', THU: 'الخميس' }
    : { SUN: 'Sun', MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu' };

  /* The columns as the editor's text: one per line, label TAB start TAB end. */
  function rowsToText(slots) {
    return (slots || []).map(slot => `${slot.label || ''}\t${slot.start}\t${slot.end}`).join('\n');
  }

  /* The editor's text as columns. Tabs or runs of spaces separate the parts;
     Windows line endings are fine. Flags are not typed: the server carries
     them over from the column with the same start and end. */
  function parseText(text) {
    const out = [];
    String(text || '').replace(/\r\n/g, '\n').trim().split('\n').filter(line => line.trim()).forEach(line => {
      const parts = line.split(/\t|\s{2,}/).map(part => part.trim()).filter(Boolean);
      if (parts.length >= 3) out.push({ label: parts[0], start: parts[1], end: parts[2] });
      else if (parts.length === 2) out.push({ label: `${parts[0]}-${parts[1]}`, start: parts[0], end: parts[1] });
      else {
        const match = line.trim().match(/^(\S+)\s+(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})$/);
        if (match) out.push({ label: match[1], start: match[2], end: match[3] });
      }
    });
    return out;
  }

  /* What a flagged column is, for the editor's hint. */
  function flaggedColumnsText(slots) {
    const online = (slots || []).filter(slot => slot.online_only).map(slot => slot.start);
    if (!online.length) return '';
    const times = online.join('، ');
    return AR
      ? `أعمدة «عن بُعد فقط» (${times}) تبقى كذلك ما دامت بالبداية والنهاية نفسيهما.`
      : `The online-only columns (${online.join(', ')}) stay online-only while they keep the same start and end.`;
  }

  const LIST = { slot_config: AR ? 'المحاضرات' : 'Lectures', lab_slot_config: AR ? 'المعامل' : 'Labs' };
  const FIELD = AR
    ? { start: 'البداية', end: 'النهاية', row: 'السطر', list: 'القائمة' }
    : { start: 'start', end: 'end', row: 'row', list: 'list' };
  const PROBLEM = AR
    ? { 'must be a time as HH:MM': 'يجب أن تكون وقتاً بالصيغة HH:MM', 'must be after the start': 'يجب أن تكون بعد البداية',
        'repeats another column': 'تكرر عموداً آخر', 'must be an object': 'غير صالح', 'must be an array': 'غير صالحة' }
    : {};

  /* A VALIDATION_SLOTS answer as one sentence per wrong column. */
  function errorsText(errors) {
    const lines = (errors || []).slice(0, 6).map(error => {
      const where = error.index >= 0
        ? (AR ? `${LIST[error.list] || error.list}، السطر ${error.index + 1}` : `${LIST[error.list] || error.list}, line ${error.index + 1}`)
        : (LIST[error.list] || error.list);
      const field = FIELD[error.field] || error.field;
      return `${where}: ${field} ${PROBLEM[error.message] || error.message}`;
    });
    const head = AR ? 'لم تُحفظ الأعمدة:' : 'The columns were not saved:';
    return `${head} ${lines.join(AR ? '؛ ' : '; ')}`;
  }

  /* The meetings a change would leave off the grid, for the confirm dialog. */
  function hiddenMeetingsHtml(details) {
    const hidden = details.hidden || [];
    const count = details.hidden_count ?? hidden.length;
    const lead = AR
      ? `هذا التغيير يترك ${count} من اللقاءات خارج أعمدة الجدول. تبقى في الجدول وتُنشر، لكن لا يظهر أي منها في الشبكة حتى تعود أعمدة بأوقاتها:`
      : `This change leaves ${count} ${count === 1 ? 'meeting' : 'meetings'} outside the grid's columns. They stay in the timetable and are still published, but none is drawn on the grid until a column has its time again:`;
    const rows = hidden.map(item => {
      const time = `<bdi dir="ltr">${escape(item.start_time)}–${escape(item.end_time)}</bdi>`;
      const locked = item.is_locked ? (AR ? ' · الوقت مقفل' : ' · time locked') : '';
      return `<li><bdi>${escape(item.board_label)}</bdi> · <bdi>${escape(item.course_code)} ${escape(item.section)}</bdi> · ${escape(DAY[item.day] || item.day)} ${time}${locked}</li>`;
    }).join('');
    const more = count > hidden.length
      ? `<p class="tw-readiness-lead">${AR ? `و${count - hidden.length} غيرها.` : `And ${count - hidden.length} more.`}</p>` : '';
    return `<p class="tw-readiness-lead">${lead}</p><ul class="tw-readiness-list">${rows}</ul>${more}`;
  }

  /* Save the columns.
     send(extra) posts the body plus `extra` and resolves to {data, error}:
     data on success; error = the server's {code, details} for the two codes
     above (any other failure is reported by the page's own fetch helper and
     resolves to {data: null}). confirmHidden(details) asks the user and
     resolves to true to go on. reportInvalid(text) shows a validation error.
     Resolves to the saved answer, or null. */
  async function save({ send, confirmHidden, reportInvalid }) {
    let extra = {};
    // A second "hides" answer means the set changed while the user read the
    // first (another edit landed): show the new set rather than saving blind.
    for (let attempt = 0; attempt < 3; attempt++) {
      const { data, error } = await send(extra);
      if (!error) return data || null;
      if (error.code === 'VALIDATION_SLOTS') { reportInvalid(errorsText(error.details?.errors)); return null; }
      if (error.code !== 'SLOT_EDIT_HIDES_PLACEMENTS') return null;
      if (!(await confirmHidden(error.details || {}))) return null;
      extra = { confirm_hidden: error.details?.fingerprint };
    }
    return null;
  }

  /* The columns a manual move may offer a meeting: never a policy column, and
     an online-only (evening) column only to a meeting already in one, so a
     room-consuming class is never offered an evening window. */
  function offeredColumns(slots, meetingStart) {
    const inOnline = (slots || []).some(slot => slot.online_only && slot.start === String(meetingStart || '').slice(0, 5));
    return (slots || []).filter(slot => !slot.policy_only && (!slot.online_only || inOnline));
  }

  window.TimetableSlotConfig = { rowsToText, parseText, flaggedColumnsText, errorsText, hiddenMeetingsHtml, save, offeredColumns };
})();
