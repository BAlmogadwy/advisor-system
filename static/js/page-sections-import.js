const IS_AR = 'LANGUAGE_CODE' === 'ar';
const T = {
  noAction: IS_AR ? 'لا يوجد إجراء بعد.' : 'No action yet.',
  requestFailed: IS_AR ? 'فشل الطلب' : 'Request failed',
  chooseFileFirst: IS_AR ? 'اختر ملف Oracle HTML أولاً.' : 'Choose Oracle HTML file first.',
  parsingFile: IS_AR ? 'جارٍ تحليل الملف...' : 'Parsing file...',
  confirmInsertAll: IS_AR ? 'إدخال جميع الصفوف المحللة الآن؟' : 'Insert all parsed rows now?',
  parseFailed: IS_AR ? 'فشل التحليل' : 'Parse failed',
  previewReady: (shown,total)=> IS_AR ? `المعاينة جاهزة: ${shown} معروض، ${total} محلل.` : `Preview ready: ${shown} shown, ${total} parsed.`,
  parsePreviewFirst: IS_AR ? 'قم بتشغيل تحليل + معاينة أولاً.' : 'Parse + Preview first.',
  inserting: IS_AR ? 'جارٍ إدخال الشعب في قاعدة البيانات...' : 'Inserting sections into database...',
  insertFailed: IS_AR ? 'فشل الإدخال' : 'Insert failed',
  insertedOk: (mode,rows,meet,ds,dm)=> IS_AR ? `تم الإدخال بنجاح [${mode}]. الشعب: ${rows}، اللقاءات: ${meet}. المحذوف: شعب ${ds}، لقاءات ${dm}.` : `Inserted successfully [${mode}]. Sections: ${rows}, meetings: ${meet}. Deleted: sections ${ds}, meetings ${dm}.`,
  noFilterResults: IS_AR ? 'لا توجد نتائج مطابقة للفلتر' : 'No results match your filter',
  insertAll: IS_AR ? 'إدخال' : 'Insert',
  cancel: IS_AR ? 'إلغاء' : 'Cancel',
};
let previewToken = null;
let rowsAll = [];
let currentTag = '';

function setStatus(kind, text){
  const el=q('status');
  el.className=`alert mt-2 py-2 mb-0 alert-${kind}`;
  el.textContent=text;
}

function rowKey(r){
  return [r.course_code,r.course_number,r.section,r.day,r.start_time,r.end_time,r.room,r.instructor].map(x=>(x||'').trim().toUpperCase()).join('|');
}

function dedupeRows(rows){
  const seen=new Set();
  const out=[];
  for(const r of rows){
    const k=rowKey(r);
    if(seen.has(k)) continue;
    seen.add(k); out.push(r);
  }
  return out;
}

function updateSummary(rawRows, dedupRows){
  q('kRaw').textContent = String(rawRows.length);
  q('kDedup').textContent = String(dedupRows.length);
  q('kDup').textContent = String(rawRows.length - dedupRows.length);
  const secSet = new Set(dedupRows.map(r=>`${r.course_code||''}-${r.course_number||''}-${r.section||''}`));
  q('kSec').textContent = String(secSet.size);
}

function filteredRows(rows){
  const c=(q('fCode').value||'').trim().toUpperCase();
  const s=(q('fSection').value||'').trim().toUpperCase();
  const d=(q('fDay').value||'').trim().toUpperCase();
  return rows.filter(r=>{
    const okC = !c || String(r.course_code||'').toUpperCase().includes(c);
    const okS = !s || String(r.section||'').toUpperCase().includes(s);
    const okD = !d || String(r.day||'').toUpperCase().includes(d);
    return okC && okS && okD;
  });
}

function renderTable(rows){
  const tb = q('tbody'); tb.innerHTML = '';
  if (!rows.length && rowsAll.length) {
    tb.innerHTML = `<tr><td colspan="12"><div class="empty-state"><span class="empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></span></span><div class="empty-title">${T.noFilterResults}</div></div></td></tr>`;
    return;
  }
  for (const row of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${row.course_code||''}</td><td>${row.course_number||''}</td><td>${row.course_name||''}</td><td>${row.section||''}</td><td>${row.day||''}</td><td>${row.start_time||''}</td><td>${row.end_time||''}</td><td>${row.available_capacity||''}</td><td>${row.registered_count||''}</td><td>${row.room||''}</td><td>${row.instructor||''}</td><td><span class="badge text-bg-secondary">${currentTag||''}</span></td>`;
    tb.appendChild(tr);
  }
}

['fCode','fSection','fDay'].forEach(id=>q(id).addEventListener('input',()=>renderTable(filteredRows(rowsAll))));

/* ── Column filter popover logic ── */
(function(){
  const filters = ['fCode','fSection','fDay'];
  function closeAll(){
    filters.forEach(id=>q(id+'Pop').classList.remove('open'));
  }
  function syncIcon(id){
    const val = q(id).value.trim();
    q(id+'Icon').classList.toggle('active', val.length > 0);
    q(id+'Clear').classList.toggle('visible', val.length > 0);
  }
  filters.forEach(id=>{
    /* Toggle popover on header/icon click */
    const th = q(id+'Icon').closest('.si-filterable');
    th.addEventListener('click', function(e){
      /* Don't toggle when clicking inside the popover */
      if(e.target.closest('.si-filter-pop')) return;
      e.stopPropagation();
      const pop = q(id+'Pop');
      const isOpen = pop.classList.contains('open');
      closeAll();
      if(!isOpen){ pop.classList.add('open'); q(id).focus(); }
    });
    /* Prevent clicks inside popover from closing it */
    q(id+'Pop').addEventListener('click', function(e){ e.stopPropagation(); });
    /* Update icon state on input */
    q(id).addEventListener('input', function(){ syncIcon(id); });
    /* Close on Escape */
    q(id).addEventListener('keydown', function(e){ if(e.key==='Escape'){ closeAll(); } });
    /* Clear button */
    q(id+'Clear').addEventListener('click', function(){
      q(id).value=''; syncIcon(id); renderTable(filteredRows(rowsAll)); closeAll();
    });
  });
  /* Close all popovers on outside click */
  document.addEventListener('click', closeAll);
})();

/* ── Drag-and-drop file upload zone ── */
const dropzone = q('siDropzone');
const fileInput = q('oracleFile');
if (dropzone && fileInput) {
  dropzone.addEventListener('click', () => fileInput.click());
  dropzone.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
  dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
  dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
      fileInput.files = e.dataTransfer.files;
      fileInput.dispatchEvent(new Event('change'));
    }
  });
  fileInput.addEventListener('change', () => {
    const name = fileInput.files[0]?.name || (IS_AR ? 'لم يتم اختيار ملف' : 'No file chosen');
    q('oracleFileName').textContent = name;
    if (fileInput.files[0]) {
      dropzone.classList.add('has-file');
    } else {
      dropzone.classList.remove('has-file');
    }
  });
}

q('toggleTech').onclick = ()=> q('techWrap').classList.toggle('d-none');

q('parseBtn').onclick = async () => {
  const file = q('oracleFile').files[0];
  if (!file) { notify.warning(T.chooseFileFirst); return; }
  q('parseBtn').disabled = true; q('parseBtn').textContent = IS_AR ? 'جارٍ التحليل...' : 'Parsing...';
  setStatus('info', T.parsingFile);
  const fd = new FormData();
  fd.append('oracle_file', file);
  fd.append('is_department', q('dept').checked ? '1' : '0');

  try {
    const r = await fetch('/ops/sections-import/preview/', {
      method:'POST', headers:{'X-CSRFToken': getCsrfToken()}, body:fd
    });
    const data = await r.json();
    q('out').textContent = JSON.stringify(data, null, 2);

    if (data.error || !Array.isArray(data.preview_rows)) {
      rowsAll = [];
      renderTable([]);
      previewToken = null;
      q('insertBtn').disabled = true;
      q('insertBtn').classList.replace('btn-danger','btn-outline-danger');
      setStatus('danger', data.error || T.parseFailed);
      q('parseBtn').disabled = false; q('parseBtn').textContent = IS_AR ? 'تحليل + معاينة' : 'Parse + Preview';
      return;
    }

    const dedup = dedupeRows(data.preview_rows);
    rowsAll = dedup;
    currentTag = data.source_tag || '';
    updateSummary(data.preview_rows, dedup);
    renderTable(filteredRows(rowsAll));

    previewToken = data.token;
    q('insertBtn').disabled = false;
    q('insertBtn').classList.replace('btn-outline-danger','btn-danger');
    q('parseBtn').disabled = false; q('parseBtn').textContent = IS_AR ? 'تحليل + معاينة' : 'Parse + Preview';
    setStatus('success', T.previewReady(data.preview_count||0, data.total_rows||0));
  } catch (err) {
    q('out').textContent = JSON.stringify({ error: T.requestFailed, details: String(err || '') }, null, 2);
    setStatus('danger', T.requestFailed);
    q('parseBtn').disabled = false; q('parseBtn').textContent = IS_AR ? 'تحليل + معاينة' : 'Parse + Preview';
  }
};

q('insertBtn').onclick = async () => {
  if (!previewToken) { notify.warning(T.parsePreviewFirst); return; }
  const ok = await dlg.confirm({title: T.confirmInsertAll, icon:'warning', confirmLabel: T.insertAll || 'Insert', cancelLabel: T.cancel || 'Cancel'});
  if (!ok) return;
  q('insertBtn').disabled = true; q('insertBtn').textContent = IS_AR ? 'جارٍ الإدخال...' : 'Inserting...';
  setStatus('info', T.inserting);
  try {
    const r = await fetch('/ops/sections-import/insert/', {
      method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken': getCsrfToken()},
      body: JSON.stringify({
        token: previewToken,
        is_department: q('dept').checked,
        truncate_existing: q('truncate').checked
      })
    });
    const data = await r.json();
    q('out').textContent = JSON.stringify(data, null, 2);
    if(data.error){
      setStatus('danger', data.error || T.insertFailed);
    }else{
      const mode = data.truncate_existing ? 'REPLACE-ALL' : 'MERGE';
      setStatus('success', T.insertedOk(mode, data.rows_total||0, data.meetings_total||0, data.deleted_sections||0, data.deleted_meetings||0));
    }
    q('insertBtn').disabled = false; q('insertBtn').textContent = IS_AR ? 'إدخال الكل' : 'Insert All';
  } catch (err) {
    q('out').textContent = JSON.stringify({ error: T.requestFailed, details: String(err || '') }, null, 2);
    setStatus('danger', T.requestFailed);
    q('insertBtn').disabled = false; q('insertBtn').textContent = IS_AR ? 'إدخال الكل' : 'Insert All';
  }
};
