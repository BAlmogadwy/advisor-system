const IS_AR = 'LANGUAGE_CODE' === 'ar';

const UI = {
  time: IS_AR ? 'الوقت' : 'Time',
  dayShort: IS_AR
    ? {SUN:'أحد',MON:'اثن',TUE:'ثلا',WED:'أرب',THU:'خم'}
    : {SUN:'Sun',MON:'Mon',TUE:'Tue',WED:'Wed',THU:'Thu'},
  dayLong: IS_AR
    ? {SUN:'الأحد',MON:'الاثنين',TUE:'الثلاثاء',WED:'الأربعاء',THU:'الخميس'}
    : {SUN:'Sunday',MON:'Monday',TUE:'Tuesday',WED:'Wednesday',THU:'Thursday'},
};

const T = {
  requestFailed: IS_AR ? 'فشل الطلب' : 'Request failed',
  unknown: IS_AR ? 'غير معروف' : 'Unknown',
  noMappedSlots: IS_AR ? 'لا توجد خانات جدول مرتبطة بعد.' : 'No mapped timetable slots yet.',
  noSectionsForShortlist: IS_AR ? 'لم يتم العثور على شعب للقائمة المختصرة. أضف مقررات مؤهلة أولاً أو تحقق من بيانات الفصل.' : 'No sections found for shortlist. Add eligible courses first or confirm term data.',
  addedSelected: (n)=> IS_AR ? `تمت إضافة ${n} مقرر(ات) للقائمة المختصرة.` : `Added ${n} selected course(s) to shortlist.`,
  fetchStudentFirst: IS_AR ? 'قم بجلب الطالب أولاً.' : 'Fetch student first.',
  enterYearTerm: IS_AR ? 'أدخل العام والفصل.' : 'Enter year and term.',
  selectBuilderFirst: IS_AR ? 'اختر خيار المُنشئ أولاً.' : 'Select a builder option first.',
  optionNotFound: IS_AR ? 'الخيار غير موجود. شغّل المُنشئ مرة أخرى.' : 'Option not found. Run builder again.',
  noMappings: IS_AR ? 'لا توجد روابط' : 'No mappings',
  noSwaps: IS_AR ? 'لا توجد تبديلات مقترحة.' : 'No swaps suggested.',
  noMeetings: IS_AR ? 'لا توجد لقاءات' : 'No meetings',
  noCoursesShortlist: IS_AR ? 'لا توجد مقررات في القائمة المختصرة بعد. أضف مقررات أولاً من خطة الطالب.' : 'No courses in shortlist yet. Add courses from student plan first.',
  loadedSections: (n)=> IS_AR ? `تم تحميل ${n} شعبة للقائمة المختصرة.` : `Loaded ${n} sections for shortlist.`,
  savedMappings: (n)=> IS_AR ? `تم حفظ ${n} ربط شعبة ويتم تحديث الأساس.` : `Saved ${n} section mappings and refreshing baseline.`,
  noMappableInOption: (name)=> IS_AR ? `الخيار ${name} لا يحتوي شعبًا قابلة للربط. استورد أحدث الشعب أو اختر خيارًا آخر.` : `Option ${name} has no mappable sections. Import latest sections or choose another option.`,
  appliedOption: (name,n)=> IS_AR ? `تم تطبيق الخيار ${name}. تم حفظ ${n} عملية ربط.` : `Applied Option ${name}. Saved ${n} mappings.`,
  trustUpdated: (name)=> IS_AR ? `جودة الربط: تم التحديث | غير المحلول: - | آخر بناء: تطبيق الخيار ${name}` : `Mapping quality: updated | Unresolved: - | Last build: applied option ${name}`,
  trustBuilt: (mapped,unresolved,time)=> IS_AR ? `جودة الربط: ${mapped?'ربط متاح':'اعتماد على الاحتياطي'} | غير المحلول: ${unresolved} | آخر بناء: ${time}` : `Mapping quality: ${mapped?'mapped available':'fallback only'} | Unresolved: ${unresolved} | Last build: ${time}`,
  noCoursesFoundStudentPlan: IS_AR ? 'لم يتم العثور على مقررات في خطة الطالب.' : 'No courses found in student plan.',
  failedLoadStudentPlanPanel: IS_AR ? 'فشل تحميل لوحة خطة الطالب.' : 'Failed to load student plan panel.',
};

const errMsg=(d)=> (d?.error?.message || d?.error || T.requestFailed);

function setBanner(kind, text){
  const b=q('statusBanner');
  const map={info:'info',success:'ok',danger:'err',warning:'warn',secondary:'neutral'};
  const cls=map[kind]||'info';
  const icons={
    info:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    ok:'<svg viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
    err:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    warn:'<svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    neutral:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
  };
  b.className=`planner-banner planner-banner-${cls}`;
  b.innerHTML=`<span class="i">${icons[cls]||icons.info}</span>${text}`;
  /* Update fetch dot */
  const dot=q('fetchDot');
  if(dot) dot.className='cb-dot'+(cls==='ok'?' ready':cls==='warn'||cls==='err'?'':' ready');
}

let currentStep='1';
const stepOrder=['1','2','3'];
function setStep(step){
  currentStep=step;
  const idx=stepOrder.indexOf(step);
  stepOrder.forEach((s,i)=>{
    const el=q('step'+s);
    if(!el) return;
    el.classList.remove('active','done');
    if(s===step) el.classList.add('active');
    else if(i<idx) el.classList.add('done');
  });
  const simple=!!q('simpleMode')?.checked;
  document.querySelectorAll('.step-panel').forEach(el=>{
    const s=el.getAttribute('data-step');
    el.classList.toggle('d-none', simple && s!==step);
  });
  if(simple){ q('advancedDiag')?.removeAttribute('open'); }
}

function enforceBuilderProcessLayout(){
  /* No-op: panels are now in a flat .planner-grid (single column) — no column rearrangement needed */
}

let currentRecommendations=[];
let shortlist=[];
let currentCtx={student_id:'',academic_year:'',term:''};
let currentBaseline=[];
let lastBuilderOptions=[];

/* Robust day normalisation: prefer stable codes (SUN/MON/...) */
function normalizeDay(d){
  const v=(d||'').toString().trim();
  const up=v.toUpperCase();
  const map = {
    'SUN':'SUN','SUNDAY':'SUN','الأحد':'SUN',
    'MON':'MON','MONDAY':'MON','الاثنين':'MON','الإثنين':'MON',
    'TUE':'TUE','TUESDAY':'TUE','الثلاثاء':'TUE',
    'WED':'WED','WEDNESDAY':'WED','الأربعاء':'WED',
    'THU':'THU','THURSDAY':'THU','الخميس':'THU'
  };
  return map[up] || map[v] || v || T.unknown;
}

function toMins(t){
  if(!t||!String(t).includes(':')) return null;
  const [h,m]=String(t).split(':').map(Number);
  if(Number.isNaN(h)||Number.isNaN(m)) return null;
  return h*60+m;
}

function courseKey(row){
  const key=String(row?.course_key||'').replace(/\s+/g,'').toUpperCase();
  if(key) return key;
  const code=String(row?.course_code||'').replace(/\s+/g,'').toUpperCase();
  const num=String(row?.course_number||'').replace(/\s+/g,'').toUpperCase();
  if(code&&num&&code!==num) return `${code}${num}`;
  return code||num;
}

function courseLabel(row){
  const code=courseKey(row);
  const section=String(row?.section||'').trim();
  return `${code}${section ? ` ${section}` : ''}`.trim();
}

/* _isDark and colorForCourse now in shared-utils.js */

function renderBaselineWeeklyCompact(baseline){
  const host=q('baselineWeekly');
  const rows=(baseline||[]).filter(r=>r.day&&r.start_time&&r.end_time);
  const unmappedByCode={};
  (baseline||[]).filter(r=>!(r.day&&r.start_time&&r.end_time)).forEach(r=>{
    const code=courseKey(r);
    if(code) unmappedByCode[code]=r;
  });
  const unmapped=Object.values(unmappedByCode);
  const unmappedHtml=unmapped.length
    ? `<div class="planner-banner planner-banner-warn mt-2" style="font-size:12px">
        <span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>
        ${unmapped.length} registered course${unmapped.length===1?'':'s'} have no mapped timetable slots:
        ${unmapped.map(r=>`<strong>${courseKey(r)}</strong>`).join(', ')}
      </div>`
    : '';
  if(!rows.length){ host.innerHTML=`<span class="text-secondary">${T.noMappedSlots}</span>${unmappedHtml}`; return; }

  const dayOrder=['SUN','MON','TUE','WED','THU'];
  const dayLabel = UI.dayShort;

  const mins=rows.flatMap(r=>[toMins(r.start_time),toMins(r.end_time)]).filter(v=>v!==null);
  const rawMin=Math.min(...mins), rawMax=Math.max(...mins);
  const step=30;
  const startMin=Math.max(0, Math.floor((rawMin-30)/step)*step);
  const endMin=Math.ceil((rawMax+30)/step)*step;

  const startsByDay={SUN:{},MON:{},TUE:{},WED:{},THU:{}};
  rows.forEach(r=>{
    const d=normalizeDay(r.day);
    const st=toMins(r.start_time), en=toMins(r.end_time);
    if(st===null||en===null||en<=st) return;
    const stSlot = Math.floor(st/step)*step;
    const span=Math.max(1, Math.ceil((en-stSlot)/step));
    startsByDay[d][stSlot]={label:courseLabel(r), room:r.room||'', start:r.start_time, end:r.end_time, span, source:r.source||''};
  });

  let html='<div class="table-responsive"><table class="table table-sm table-bordered align-middle"><thead><tr>';
  html += `<th style="width:70px">${UI.time}</th>`;
  dayOrder.forEach(d=> html += `<th>${dayLabel[d]}</th>`);
  html+='</tr></thead><tbody>';

  const carry={SUN:0,MON:0,TUE:0,WED:0,THU:0};
  for(let t=startMin;t<endMin;t+=step){
    const hh=String(Math.floor(t/60)).padStart(2,'0');
    const mm=String(t%60).padStart(2,'0');
    html += `<tr><td class="text-secondary">${hh}:${mm}</td>`;
    dayOrder.forEach(d=>{
      if(carry[d]>0){ carry[d]-=1; return; }
      const m=startsByDay[d][t];
      if(!m){ html+='<td></td>'; return; }
      carry[d]=Math.max(0,(m.span||1)-1);
      html += `<td rowspan="${m.span||1}" style="background:${colorForCourse(m.label)}">
        <div class="fw-semibold">${m.label}</div>
        <div class="small text-secondary">${m.start}-${m.end}</div>
      </td>`;
    });
    html+='</tr>';
  }
  html+='</tbody></table></div>';
  host.innerHTML=html+unmappedHtml;
}

function renderVisualTimetable(source='baseline'){
  const host=q('visualGrid');
  const dayOrder=['SUN','MON','TUE','WED','THU'];
  const dayLabel = UI.dayShort;

  const baseline=(currentBaseline||[])
    .filter(x=>x.day&&x.start_time&&x.end_time)
    .map(x=>({
      day: normalizeDay(x.day),
      start:x.start_time,
      end:x.end_time,
      label:`${x.course_code||''} ${x.section||''}`.trim(),
      room:x.room||'',
      kind:'baseline'
    }));

  let planned=[];
  if(source!=='baseline'){
    const pick = source==='overlay'
      ? (lastBuilderOptions||[])[0]
      : (lastBuilderOptions||[]).find(o=>String(o.name||'')===String(source));

    (pick?.mappings||[]).forEach(m=>
      (m.meetings||[]).forEach(mm=>
        planned.push({
          day: normalizeDay(mm.day),
          start:mm.start_time,
          end:mm.end_time,
          label:courseLabel(m),
          room:mm.room||'',
          kind:'planned'
        })
      )
    );
  }

  let meetings=[];
  if(source==='baseline') meetings=baseline;
  else if(source==='overlay') meetings=[...baseline,...planned];
  else meetings=planned;

  const step=30;
  const startsByDay={SUN:{},MON:{},TUE:{},WED:{},THU:{}};

  const enriched=meetings.map(m=>{
    const st=toMins(m.start), en=toMins(m.end);
    return {...m, st, en, conflict:false};
  }).filter(m=>m.st!==null && m.en!==null && m.en>m.st);

  if(!enriched.length){
    host.innerHTML=`<span class="text-secondary">${IS_AR ? 'لا توجد لقاءات جدول لعرضها.' : 'No timetable meetings to display.'}</span>`;
    return;
  }

  const rawMin=Math.min(...enriched.map(m=>m.st));
  const rawMax=Math.max(...enriched.map(m=>m.en));
  const startMin=Math.max(0, Math.floor((rawMin-30)/step)*step);
  const endMin=Math.ceil((rawMax+30)/step)*step;

  // Overlap conflict detection (overlay: baseline vs planned)
  for(let i=0;i<enriched.length;i++){
    for(let j=i+1;j<enriched.length;j++){
      const a=enriched[i], b=enriched[j];
      if(a.day!==b.day) continue;
      const overlaps = a.st < b.en && b.st < a.en;
      if(!overlaps) continue;
      if(source==='overlay' && a.kind!==b.kind){ a.conflict=true; b.conflict=true; }
    }
  }

  enriched.forEach(m=>{
    const stSlot = Math.floor((m.st||0)/step)*step;
    const span=Math.max(1, Math.ceil((m.en-stSlot)/step));
    if(!startsByDay[m.day]) startsByDay[m.day]={};
    const key=`${stSlot}`;
    if(!startsByDay[m.day][key]) startsByDay[m.day][key]={...m, span};
    else if(m.conflict) startsByDay[m.day][key]={...m, span}; // prefer showing conflicts
  });

  let html='<div class="table-responsive"><table class="table table-sm table-bordered align-middle"><thead><tr>';
  html += `<th style="width:70px">${UI.time}</th>`;
  dayOrder.forEach(d=> html += `<th>${dayLabel[d]}</th>`);
  html+='</tr></thead><tbody>';

  const carry={SUN:0,MON:0,TUE:0,WED:0,THU:0};
  for(let t=startMin;t<endMin;t+=step){
    const hh=String(Math.floor(t/60)).padStart(2,'0');
    const mm=String(t%60).padStart(2,'0');
    html += `<tr><td class="text-secondary">${hh}:${mm}</td>`;
    dayOrder.forEach(d=>{
      if(carry[d]>0){ carry[d]-=1; return; }
      const m=startsByDay[d][t];
      if(!m){ html += '<td></td>'; return; }
      carry[d]=Math.max(0,(m.span||1)-1);

      const bg = m.conflict
        ? getComputedStyle(document.documentElement).getPropertyValue('--pl-conflict-cell').trim() || '#fecaca'
        : colorForCourse(m.label);
      const badge = m.conflict
        ? `<span class="badge text-bg-danger">${IS_AR ? 'تعارض' : 'conflict'}</span>`
        : (m.kind==='planned'
          ? `<span class="badge text-bg-success">${IS_AR ? 'مخطط' : 'planned'}</span>`
          : `<span class="badge text-bg-primary">${IS_AR ? 'أساس' : 'baseline'}</span>`);

      html += `<td rowspan="${m.span||1}" style="background:${bg}">
        <div class="fw-semibold">${m.label}</div>
        <div class="small text-secondary">${m.start}-${m.end}</div>
        <div class="mt-1">${badge}</div>
      </td>`;
    });
    html+='</tr>';
  }

  html+='</tbody></table></div>';
  host.innerHTML=html;
}

q('mode').addEventListener('change',()=>{
  q('planningBanner').classList.toggle('d-none', q('mode').value!=='ignore');
  if(currentCtx?.student_id){ renderPlanPalette(currentCtx.student_id); renderAvailableSections(); }
});

q('visualSource').addEventListener('change',(e)=>renderVisualTimetable(e.target.value));
['1','2','3'].forEach(s=> q('step'+s)?.addEventListener('click',()=>setStep(s)));
q('simpleMode')?.addEventListener('change',()=>setStep(currentStep));
q('prevStep')?.addEventListener('click',()=>{ const order=['1','2','3']; const i=Math.max(0,order.indexOf(currentStep)-1); setStep(order[i]); });
q('nextStep')?.addEventListener('click',()=>{ const order=['1','2','3']; const i=Math.min(order.length-1,order.indexOf(currentStep)+1); setStep(order[i]); });

function renderShortlist(){
  const wrap=q('shortlist'); wrap.innerHTML='';
  let credits=0;
  shortlist.forEach((c,idx)=>{
    credits += Number(c.credits||0);
    const row=document.createElement('div');
    row.className='border rounded p-2 mb-2';

    const hasPinned=(c.pinned_sections&&c.pinned_sections.length>0);
    const pinnedHtml=hasPinned
      ? `<div class="d-flex align-items-center flex-wrap" style="margin-top:3px; gap:4px">
           <span class="fs-11 fw-semibold text-teal">${IS_AR?'شعب محددة:':'Pinned:'}</span>
           ${c.pinned_sections.map((p,pi)=>`<span class="sl-pin-badge align-items-center fs-11 fw-semibold text-teal u-cursor-pointer" data-pi="${pi}" title="${IS_AR?'انقر للإزالة':'Click to remove'}" style="display:inline-flex; gap:3px; padding:1px 7px; border-radius:5px; background:var(--teal-dim)">§${p.section} <span style="font-size:9px;opacity:0.6">✕</span></span>`).join('')}
         </div>`
      : `<div class="fs-11 text-t4" style="margin-top:2px">${IS_AR?'أي شعبة (البناء يختار الأنسب)':'Any section (builder picks best)'}</div>`;

    row.innerHTML=`
      <div class="d-flex justify-content-between">
        <div class="flex-fill">
          <strong>${c.course_code}</strong> ${c.course_name||''}
          <span class="text-secondary">(${c.credits||0} ${IS_AR?'ساعة':'cr'})</span>
          ${pinnedHtml}
        </div>
        <button class="pl-btn pl-btn-red" data-i="${idx}"><span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span>${IS_AR?'إزالة':'Remove'}</button>
      </div>
      <div class="form-check mt-1">
        <input type="checkbox" class="form-check-input" id="m${idx}" ${c.must_take?'checked':''}>
        <label class="form-check-label" for="m${idx}">${IS_AR?'إلزامي':'Must-take'}</label>
      </div>`;

    /* Remove individual pinned section badges */
    row.querySelectorAll('.sl-pin-badge').forEach(badge=>{
      badge.onclick=(e)=>{
        e.stopPropagation();
        const pi=Number(badge.dataset.pi);
        c.pinned_sections.splice(pi,1);
        if(!c.pinned_sections.length) delete c.pinned_sections;
        renderShortlist();
      };
    });

    wrap.appendChild(row);
    row.querySelector('button[data-i]').onclick=()=>{shortlist.splice(idx,1);renderShortlist();};
    row.querySelector('input[type="checkbox"]').onchange=(e)=>{shortlist[idx].must_take=e.target.checked;};
  });
  q('shortCredits').textContent=String(credits);
}

async function renderPlanPalette(studentId){
  const wrap=q('planPalette');
  wrap.innerHTML=`<span class="text-secondary">${IS_AR ? 'جارٍ تحميل خطة الطالب...' : 'Loading student plan...'}</span>`;
  try{
    const planRes=await fetch(`/report/student-plan/?student_id=${encodeURIComponent(studentId)}`);
    const planData=await planRes.json();
    if(planData.error){ wrap.innerHTML=`<span class="text-danger">${planData.error}</span>`; return; }

    const allCourses=[];
    (planData.terms||[]).forEach(t=> (t.courses||[]).forEach(c=> allCourses.push({...c, _term:t.term})));
    const codes=[...new Set(allCourses.map(c=>String(c.course_code||'').replace(/\s+/g,'').toUpperCase()).filter(Boolean))];

    let sections=[];
    if(codes.length){
      const secRes=await fetch('/ops/planner/sections-catalog/',{
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
        body:JSON.stringify({student_id:currentCtx.student_id,academic_year:currentCtx.academic_year,term:currentCtx.term,course_codes:codes})
      });
      const secData=await secRes.json();
      sections=secData.sections||[];
    }

    const secByCourse={};
    sections.forEach(s=>{
      const k=courseKey(s);
      (secByCourse[k] ||= []).push(s);
    });

    const terms = [...(planData.terms||[])].sort((a,b)=>Number(a.term||0)-Number(b.term||0));
    wrap.innerHTML='';

    terms.forEach(t=>{
      const rows=(t.courses||[]).slice().sort((a,b)=>Number(b.importance_score||0)-Number(a.importance_score||0));
      const details=document.createElement('details');
      details.className='mb-2 border rounded p-2';
      if(Number(t.term||0) <= 2) details.open=true;

      const summary=document.createElement('summary');
      summary.innerHTML=`<strong>${IS_AR?'الفصل':'Term'} ${t.term}</strong> <span class="small text-secondary">(${rows.length} ${IS_AR?'مقرر':'courses'})</span>`;
      details.appendChild(summary);

      const table=document.createElement('table');
      table.className='table table-sm mt-2 mb-0';
      table.innerHTML=`<thead><tr>
        <th>${IS_AR?'المقرر':'Course'}</th>
        <th>${IS_AR?'الحالة':'Status'}</th>
        <th>${IS_AR?'الأولوية':'Importance'}</th>
        <th>${IS_AR?'الإتاحة':'Offer'}</th>
        <th></th>
      </tr></thead>`;
      const tb=document.createElement('tbody');

      rows.forEach(c=>{
        const code=String(c.course_code||'').replace(/\s+/g,'').toUpperCase();
        const list=secByCourse[code]||[];
        const hasAny=list.length>0;
        const hasOpen=list.some(s=> Number(s.available_capacity||0) > 0);
        const prereqOk = (String(c.status||'') !== 'not_taken') ? true : Boolean(c.can_register);

        let cls='plan-black';
        let label=IS_AR?'غير مطروح':'not offered';

        if(!prereqOk){
          cls='plan-prereq'; label=IS_AR?'المتطلبات غير مستوفاة':'prereq missing';
        }else if(hasAny && hasOpen){
          cls='plan-green'; label=IS_AR?'شعب متاحة':'open sections';
        }else if(hasAny){
          cls='plan-red'; label=IS_AR?'كل الشعب ممتلئة':'all full';
        }

        const modeIgnoreRegistered = (q('mode')?.value === 'ignore');
        const canAdd=((String(c.status||'')==='not_taken') || modeIgnoreRegistered) && prereqOk;

        const onlyNotTaken=q('fltNotTaken')?.checked;
        const onlyGreen=q('fltGreen')?.checked;
        const onlyHigh=q('fltHighImp')?.checked;
        if(onlyNotTaken && String(c.status||'')!=='not_taken') return;
        if(onlyGreen && !(hasOpen && prereqOk)) return;
        if(onlyHigh && Number(c.importance_score||0) < 1) return;

        const tr=document.createElement('tr');
        tr.style.cursor=canAdd?'pointer':'default';
        tr.innerHTML=`
          <td>
            <input type="checkbox" class="form-check-input me-2 planPick"
              data-code="${code}"
              data-credits="${Number(c.credit_hours||0)}"
              data-score="${Math.round(Number(c.importance_score||0)*20)}"
              ${canAdd?'':'disabled'}>
            <strong>${code}</strong> <span class="text-secondary">(${c.credit_hours||0}${IS_AR?' ساعة':'cr'})</span>
          </td>
          <td>${c.status||''}</td>
          <td>${Number(c.importance_score||0).toFixed(2)}</td>
          <td><span class="plan-chip ${cls}">${label}</span></td>
          <td><button class="pl-btn pl-btn-teal" ${canAdd?'':'disabled'}><span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></span>${IS_AR?'إضافة':'Add'}</button></td>`;

        const addCourse=()=>{
          if(!canAdd) return;
          if(shortlist.find(x=>String(x.course_code||'').toUpperCase()===code)) return;
          shortlist.push({
            course_code:code, course_name:'', credits:Number(c.credit_hours||0),
            priority:'Med', status:'Eligible', missing_prerequisites:[],
            must_take:false, score:Math.round(Number(c.importance_score||0)*20)
          });
          renderShortlist();
          setStep('2');
        };

        tr.querySelector('button').onclick=(e)=>{ e.stopPropagation(); addCourse(); };
        tr.onclick=(e)=>{ if(e.target.closest('input,button')) return; addCourse(); };
        tb.appendChild(tr);
      });

      table.appendChild(tb);
      details.appendChild(table);
      wrap.appendChild(details);
    });

    if(!terms.length) wrap.innerHTML=`<span class="text-secondary">${T.noCoursesFoundStudentPlan}</span>`;
  }catch(e){
    wrap.innerHTML=`<span class="text-danger">${T.failedLoadStudentPlanPanel}</span>`;
    notify.error(T.failedLoadStudentPlanPanel, e.message || String(e));
  }
}

/* ── Available Courses & Sections panel ─────────────────── */
async function renderAvailableSections(){
  const grid=q('availGrid');
  const summary=q('availSummary');
  if(!grid) return;
  grid.innerHTML=`<span class="text-secondary">${IS_AR?'جارٍ تحميل الشعب...':'Loading sections...'}</span>`;

  try{
    /* 1. Get student plan */
    const planRes=await fetch(`/report/student-plan/?student_id=${encodeURIComponent(currentCtx.student_id)}`);
    const planData=await planRes.json();
    if(planData.error){ grid.innerHTML=`<span class="text-danger">${planData.error}</span>`; return; }

    /* 2. Collect eligible courses
          Normal mode  → not_taken + can_register
          Ignore mode  → not_taken + can_register  AND  studying (currently registered) */
    const eligible=[];
    const modeIgnore=(q('mode')?.value==='ignore');
    (planData.terms||[]).forEach(t=>{
      (t.courses||[]).forEach(c=>{
        const status=String(c.status||'');
        const includeNormal=(status==='not_taken'&&c.can_register);
        const includeStudying=(modeIgnore&&status==='studying');
        if(includeNormal||includeStudying){
          eligible.push({
            code: String(c.course_code||'').replace(/\s+/g,'').toUpperCase(),
            name: c.course_name||c.description||'',
            credits: Number(c.credit_hours||0),
            importance: Number(c.importance_score||0)
          });
        }
      });
    });

    if(!eligible.length){
      grid.innerHTML=`<div class="pl-empty" style="grid-column:1/-1"><div class="pl-empty-title">${IS_AR?'لا توجد مقررات مؤهلة':'No eligible courses found'}</div></div>`;
      if(summary) summary.innerHTML='';
      return;
    }

    /* 3. Fetch sections catalog */
    const codes=[...new Set(eligible.map(e=>e.code))];
    const secRes=await fetch('/ops/planner/sections-catalog/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
      body:JSON.stringify({student_id:currentCtx.student_id,academic_year:currentCtx.academic_year,term:currentCtx.term,course_codes:codes})
    });
    const secData=await secRes.json();
    const sections=secData.sections||[];

    /* 4. Group sections by course key */
    const secByCourse={};
    sections.forEach(s=>{
      const k=courseKey(s);
      (secByCourse[k]||=[]).push(s);
    });

    /* 5. Build baseline meetings for conflict detection */
    const baseMeets=(currentBaseline||[])
      .filter(b=>b.day&&b.start_time&&b.end_time)
      .map(b=>({day:normalizeDay(b.day),st:toMins(b.start_time),en:toMins(b.end_time)}));

    function hasConflict(meetings){
      return (meetings||[]).some(m=>{
        const mDay=normalizeDay(m.day);
        const mSt=toMins(m.start_time), mEn=toMins(m.end_time);
        if(mSt===null||mEn===null) return false;
        return baseMeets.some(b=>b.day===mDay&&b.st<mEn&&mSt<b.en);
      });
    }

    /* 6. Render course cards */
    const counts={green:0,pink:0,red:0,gray:0};
    grid.innerHTML='';

    const coursesWithSections=eligible
      .filter(e=>(secByCourse[e.code]||[]).length>0)
      .sort((a,b)=>b.importance-a.importance);

    if(!coursesWithSections.length){
      grid.innerHTML=`<div class="pl-empty" style="grid-column:1/-1"><div class="pl-empty-title">${IS_AR?'لا توجد شعب متاحة هذا الفصل':'No sections available this term'}</div></div>`;
      if(summary) summary.innerHTML='';
      return;
    }

    coursesWithSections.forEach(course=>{
      const secs=secByCourse[course.code]||[];
      const card=document.createElement('div');
      card.className='avail-course-card';
      card.dataset.code=course.code;

      let secHtml='';
      secs.forEach(s=>{
        const cap=Number(s.available_capacity||0);
        const hasSeats=cap>0;
        const conflict=hasConflict(s.meetings);

        let colorClass, dotClass, statusLabel;
        if(!conflict&&hasSeats){
          colorClass='sec-teal'; dotClass='sec-dot-teal';
          statusLabel=IS_AR?'متاح':'Open'; counts.green++;
        }else if(conflict&&hasSeats){
          colorClass='sec-rose'; dotClass='sec-dot-rose';
          statusLabel=IS_AR?'تعارض':'Conflict'; counts.pink++;
        }else if(conflict&&!hasSeats){
          colorClass='sec-red'; dotClass='sec-dot-red';
          statusLabel=IS_AR?'ممتلئ+تعارض':'Full+Conflict'; counts.red++;
        }else{
          colorClass='sec-gray'; dotClass='sec-dot-gray';
          statusLabel=IS_AR?'ممتلئ':'Full'; counts.gray++;
        }

        const timeStr=(s.meetings||[]).map(m=>
          `${UI.dayShort[normalizeDay(m.day)]||normalizeDay(m.day)} ${m.start_time||''}-${m.end_time||''}`
        ).join(', ')||'TBA';

        const totalSeats=(Number(s.registered_count||0)+cap);

        secHtml+=`
          <div class="avail-sec-row ${colorClass}" data-tsid="${s.term_section_id}" title="${statusLabel}">
            <span class="sec-dot ${dotClass}"></span>
            <span class="sec-id">${s.section||'?'}</span>
            <span class="sec-time">${timeStr}</span>
            <span class="sec-capacity">${s.registered_count||0}/${totalSeats}</span>
          </div>`;
      });

      card.innerHTML=`
        <div class="avail-course-head u-cursor-pointer" title="${IS_AR?'انقر لإضافة المقرر (أي شعبة)':'Click to add course (any section)'}">
          <div>
            <div class="avail-course-code">${course.code}</div>
            <div class="avail-course-name">${course.name}</div>
          </div>
          <span class="avail-course-credits" style="margin-inline-end:4px">${course.credits} ${IS_AR?'ساعة':'cr'}</span>
          <span class="avail-course-credits" style="background:${course.importance>=3?'var(--teal-dim);color:var(--teal)':course.importance>=1?'var(--amber-dim);color:var(--warning)':'var(--pl-overlay-2);color:var(--t4)'}">⚡${course.importance.toFixed(1)}</span>
        </div>
        ${secHtml}`;

      /* Click section row → pin that specific section to the shortlist */
      card.querySelectorAll('.avail-sec-row').forEach(row=>{
        row.addEventListener('click',()=>{
          const tsid=Number(row.dataset.tsid);
          const secLabel=row.querySelector('.sec-id')?.textContent?.trim()||'?';

          let existing=shortlist.find(x=>String(x.course_code||'').toUpperCase()===course.code);

          if(!existing){
            /* New entry with pinned section */
            existing={
              course_code:course.code, course_name:course.name,
              credits:course.credits, priority:'Med',
              score:Math.round(course.importance*20),
              status:'Eligible', missing_prerequisites:[], must_take:false,
              pinned_sections:[{term_section_id:tsid, section:secLabel}]
            };
            shortlist.push(existing);
          } else {
            /* Add section to existing entry */
            if(!existing.pinned_sections) existing.pinned_sections=[];
            if(!existing.pinned_sections.find(p=>p.term_section_id===tsid)){
              existing.pinned_sections.push({term_section_id:tsid, section:secLabel});
            } else {
              setBanner('info', IS_AR?`الشعبة ${secLabel} مضافة مسبقاً`:`Section ${secLabel} already pinned`);
              return;
            }
          }

          renderShortlist();
          setBanner('success', IS_AR?`تمت إضافة ${course.code} §${secLabel} للقائمة`:`Added ${course.code} §${secLabel} to shortlist`);
        });
      });

      /* Click course header → add as full course (any section, no pinning) */
      card.querySelector('.avail-course-head')?.addEventListener('click',(e)=>{
        /* Don't fire if user clicked a child inside the credits badge etc. – keep it simple */
        if(shortlist.find(x=>String(x.course_code||'').toUpperCase()===course.code)){
          setBanner('info', IS_AR?`${course.code} مضاف مسبقاً في القائمة`:`${course.code} already in shortlist`);
          return;
        }
        shortlist.push({
          course_code:course.code, course_name:course.name,
          credits:course.credits, priority:'Med',
          score:Math.round(course.importance*20),
          status:'Eligible', missing_prerequisites:[], must_take:false
        });
        renderShortlist();
        setBanner('success', IS_AR?`تمت إضافة ${course.code} (أي شعبة)`:`Added ${course.code} (any section)`);
      });

      grid.appendChild(card);
    });

    /* 7. Summary bar */
    const total=counts.green+counts.pink+counts.red+counts.gray;
    if(summary){
      summary.innerHTML=`
        <span class="avail-summary-item"><span class="sec-dot sec-dot-teal"></span> ${counts.green} ${IS_AR?'متاح':'open'}</span>
        <span class="avail-summary-item"><span class="sec-dot sec-dot-rose"></span> ${counts.pink} ${IS_AR?'تعارض':'conflict'}</span>
        <span class="avail-summary-item"><span class="sec-dot sec-dot-red"></span> ${counts.red} ${IS_AR?'ممتلئ+تعارض':'full+conflict'}</span>
        <span class="avail-summary-item"><span class="sec-dot sec-dot-gray"></span> ${counts.gray} ${IS_AR?'ممتلئ':'full'}</span>
        <span class="fw-semibold" style="margin-inline-start:auto">${total} ${IS_AR?'شعبة':'sections'}</span>`;
    }
  }catch(e){
    grid.innerHTML=`<span class="text-danger">${IS_AR?'فشل تحميل الشعب المتاحة':'Failed to load available sections'}</span>`;
    notify.error(IS_AR?'فشل تحميل الشعب المتاحة':'Failed to load available sections', e.message || String(e));
    console.error('renderAvailableSections error:',e);
  }
}

q('resetShortlist').onclick=()=>{
  shortlist=currentRecommendations.filter(x=>x.status==='Eligible').map(x=>({...x,must_take:false}));
  renderShortlist(); setStep('2');
};
q('clearShortlist').onclick=()=>{shortlist=[];renderShortlist(); setStep('2');};

['fltNotTaken','fltGreen','fltHighImp'].forEach(id=>
  q(id)?.addEventListener('change',()=>{ if(currentCtx.student_id) renderPlanPalette(currentCtx.student_id); })
);

q('addSelectedPlan').onclick=()=>{
  const picks=[...document.querySelectorAll('.planPick:checked')];
  let added=0;
  picks.forEach(p=>{
    const code=String(p.dataset.code||'').toUpperCase();
    if(!code) return;
    if(shortlist.find(x=>String(x.course_code||'').toUpperCase()===code)) return;
    shortlist.push({course_code:code,course_name:'',credits:Number(p.dataset.credits||0),priority:'Med',status:'Eligible',missing_prerequisites:[],must_take:false,score:Number(p.dataset.score||0)});
    added++;
  });
  renderShortlist();
  setBanner('info', T.addedSelected(added));
  setStep('2');
};

q('applyOption').onclick=async()=>{
  const name=q('selectedOption').value;
  if(!name) return notify.warning(T.selectBuilderFirst);
  if(!currentCtx.student_id) return notify.warning(T.fetchStudentFirst);

  const opt=(lastBuilderOptions||[]).find(x=>String(x.name||'')===String(name));
  if(!opt) return notify.warning(T.optionNotFound);

  const ids=(opt.mappings||[]).map(m=>m.term_section_id).filter(Boolean);
  if(!ids.length){ setBanner('warning', T.noMappableInOption(name)); return; }

  let r, data;
  try{
    r=await fetch('/ops/planner/save-student-sections/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
      body:JSON.stringify({
        student_id:currentCtx.student_id,
        academic_year:currentCtx.academic_year,
        term:currentCtx.term,
        term_section_ids:ids,
        confirm_replace:true
      })
    });
    data=await r.json();
  }catch(err){
    setBanner('danger', IS_AR?'فشل حفظ الشعب':'Failed to save sections');
    notify.error(IS_AR?'فشل حفظ الشعب':'Failed to save sections', err.message||String(err));
    return;
  }
  if(data.error){ setBanner('danger', errMsg(data)); return; }

  setBanner('success', T.appliedOption(name, data.inserted||0));
  q('trustStrip').textContent=T.trustUpdated(name);
  setStep('3');
  q('fetchBtn').click();
};

q('runBuilder').onclick=async()=>{
  setStep('3');

  /* Fix: correct preconditions + correct message */
  if(!currentCtx.student_id) return notify.warning(T.fetchStudentFirst);
  if(!currentCtx.academic_year || !currentCtx.term) return notify.warning(T.enterYearTerm);

  let r, data;
  try{
    r=await fetch('/ops/planner/build/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
      body:JSON.stringify({
        student_id:currentCtx.student_id,
        academic_year:currentCtx.academic_year,
        term:currentCtx.term,
        mode:q('mode').value,
        swap:q('swap').checked,
        strict_sections:q('strictSections')?.checked||false,
        ignore_capacity:q('ignoreCapacity')?.checked||false,
        max_credits:Number(q('maxCredits')?.value||0),
        shortlist,
        baseline:currentBaseline
      })
    });
    data=await r.json();
  }catch(err){
    setBanner('danger', IS_AR?'فشل تشغيل المُنشئ':'Builder request failed');
    notify.error(IS_AR?'فشل تشغيل المُنشئ':'Builder request failed', err.message||String(err));
    return;
  }
  if(data.error){setBanner('danger', errMsg(data)); return;}

  const s=data.summary||{};
  q('builderSummary').innerHTML=`
    <div><strong>${IS_AR ? 'تمت الجدولة' : 'Scheduled'}:</strong> ${s.scheduled||0}/${s.target||0}</div>
    <div><strong>${IS_AR ? 'التعارضات' : 'Conflicts'}:</strong> ${s.conflicts||0}</div>
    <div><strong>${IS_AR ? 'التبديلات المطلوبة' : 'Swaps required'}:</strong> ${s.swaps_required||0}</div>
    <div><strong>${IS_AR ? 'الحالة' : 'Status'}:</strong> ${s.best_feasible?(IS_AR?'تم العثور على أفضل خطة ممكنة':'Best feasible plan found'):(IS_AR?'لا توجد خطة ممكنة':'No feasible plan')}</div>`;

  setBanner('success', IS_AR ? 'اكتمل تشغيل المُنشئ. راجع الخيارات أدناه.' : 'Builder run completed. Review options below.');

  const unresolved=(data.options||[])[0]?.unscheduled?.length || 0;
  q('trustStrip').textContent=T.trustBuilt(!!currentBaseline.length, unresolved, new Date().toLocaleTimeString());

  const wrap=q('builderOptions');
  wrap.innerHTML='';
  lastBuilderOptions=(data.options||[]);
  q('selectedOption').value='';
  const cards=q('optionCards');
  cards.innerHTML='';

  const vs=q('visualSource');
  if(vs){
    const prev=vs.value;
    vs.innerHTML='';
    const mk=(v,t)=>{const o=document.createElement('option');o.value=v;o.textContent=t;vs.appendChild(o);};
    mk('baseline', IS_AR ? 'الأساس' : 'Baseline');
    (data.options||[]).forEach(o=>mk(String(o.name||''), IS_AR ? `خيار المُنشئ ${o.name||''}` : `Builder ${o.name||''}`));
    mk('overlay', IS_AR ? 'تراكب (الأساس + المحدد)' : 'Overlay (Baseline + Selected)');
    if([...vs.options].some(o=>o.value===prev)) vs.value=prev;
  }

  let bestName=''; let bestScore=-1;
  (data.options||[]).forEach(opt=>{ if((opt.scheduled||0)>bestScore){bestScore=(opt.scheduled||0); bestName=String(opt.name||'');} });

  function chooseOption(name){
    q('selectedOption').value=String(name||'');
    const bar=q('selectedOptionBar');
    if(name){
      bar.className='planner-banner planner-banner-ok';
      bar.innerHTML=`<span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></span>${IS_AR ? 'الخيار المحدد: '+name : 'Selected option: '+name}`;
    } else {
      bar.className='planner-banner planner-banner-neutral';
      bar.innerHTML=`<span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg></span>${IS_AR ? 'لا يوجد خيار محدد.' : 'No option selected.'}`;
    }

    cards.querySelectorAll('[data-opt]').forEach(el=>{
      const sel=el.getAttribute('data-opt')===String(name||'');
      el.style.borderColor=sel?'var(--teal)':'';
      el.style.boxShadow=sel?'0 0 0 2px var(--brand-glow)':'';
    });

    const vs=q('visualSource');
    let chosenSource='baseline';
    if(vs && name && [...vs.options].some(o=>String(o.value)===String(name))){
      vs.value=String(name);
      chosenSource=String(name);
    }else if(vs){
      chosenSource=String(vs.value||'baseline');
    } else if(name){
      chosenSource=String(name);
    }

    renderVisualTimetable(chosenSource);
  }

  (data.options||[]).forEach(opt=>{
    const col=document.createElement('div');
    col.className='col-md-4';
    col.innerHTML=`
      <div class="planner-panel u-cursor-pointer" data-opt="${opt.name}" style="margin-bottom:0; padding:12px 14px">
        <div class="fw-semibold" style="font-size:13px; color:var(--navy)">${IS_AR?'الخيار':'Option'} ${opt.name}</div>
        <div class="fs-12" style="margin-top:4px">${IS_AR?'تمت الجدولة':'Scheduled'} <strong class="text-teal">${opt.scheduled||0}</strong>/${opt.target||0}</div>
        <div class="fs-11 text-t4">${IS_AR?'غير مجدول':'Unscheduled'} ${(opt.unscheduled||[]).length||0}</div>
        <button class="pl-btn pl-btn-teal w-100 justify-content-center mt-2">${IS_AR?'اختيار':'Select'}</button>
      </div>`;
    col.querySelector('button').onclick=()=>chooseOption(opt.name);
    cards.appendChild(col);
  });

  if(bestName){ chooseOption(bestName); }

  (data.options||[]).forEach(opt=>{
    const div=document.createElement('div');
    div.className='border rounded p-2 mb-2';
    const method = String(opt.method||opt.name||'').charAt(0).toUpperCase();
    const rationale = method==='A'
      ? (IS_AR ? 'تعظيم عدد المقررات المجدولة' : 'Maximise scheduled courses')
      : (method==='B'
        ? (IS_AR ? 'ضغط بالبت ماسك (أيام/فجوات)' : 'Bitmask compactness (days/gaps)')
        : (IS_AR ? 'بحث DFS بالبت ماسك مع ترتيب معجمي (أيام/فجوات/كسر تعادل)' : 'Bitmask DFS lexicographic (days/gaps/tiebreakers)'));

    div.innerHTML=`
      <div><strong>${IS_AR?'الخيار':'Option'} ${opt.name}</strong> - ${IS_AR?'تمت الجدولة':'Scheduled'} ${opt.scheduled}/${opt.target}</div>
      <div class="small text-secondary">${rationale}</div>
      <div class="mt-1">
        ${(opt.mappings||[]).map(m=>`<div>• ${courseKey(m)} → ${m.section}</div>`).join('') || `<span class="text-secondary">${T.noMappings}</span>`}
      </div>
      <div class="mt-1 text-danger">
        ${(opt.unscheduled||[]).map(u=>`<div>• ${u.course_code}: ${u.reason}</div>`).join('')}
      </div>`;
    wrap.appendChild(div);
  });

  q('swapSuggestions').innerHTML=(data.swap_suggestions||[]).map(s=>`<div>• ${s.course_code}: ${s.from_section} → ${s.to_section} (${s.reason})</div>`).join('') || `<span class="text-secondary">${T.noSwaps}</span>`;

  renderVisualTimetable(q('visualSource').value || 'baseline');
};

q('fetchBtn').onclick=async()=>{
  setStep('1');
  q('fetchState').textContent = IS_AR ? 'جارٍ التحميل...' : 'Loading...';
  setBanner('info', IS_AR ? 'جارٍ جلب بيانات الطالب...' : 'Fetching student context...');

  let r, data;
  try{
    r=await fetch('/ops/planner/context/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
      body:JSON.stringify({
        student_id:q('studentId').value,
        academic_year:q('year').value,
        term:q('term').value
      })
    });
    data=await r.json();
  }catch(err){
    q('fetchState').textContent = IS_AR ? 'خطأ' : 'Error';
    setBanner('danger', IS_AR?'فشل جلب بيانات الطالب':'Failed to fetch student context');
    notify.error(IS_AR?'فشل جلب بيانات الطالب':'Failed to fetch student context', err.message||String(err));
    return;
  }
  if(data.error){
    q('fetchState').textContent = IS_AR ? 'خطأ' : 'Error';
    setBanner('danger', errMsg(data));
    return;
  }

  q('fetchState').textContent = (IS_AR ? 'تم الجلب @ ' : 'Fetched @ ') + new Date().toLocaleTimeString();

  currentCtx={
    student_id:String(data.student?.student_id||q('studentId').value),
    academic_year:String(data.year||q('year').value),
    term:String(data.term||q('term').value)
  };

  currentBaseline=(data.baseline||[]);

  const st=data.student||{};
  q('studentSummary').innerHTML=`
    <div><strong>${IS_AR ? 'المعرّف' : 'ID'}:</strong> ${st.student_id||''}</div>
    <div><strong>${IS_AR ? 'الاسم' : 'Name'}:</strong> ${st.name||''}</div>
    <div><strong>${IS_AR ? 'البرنامج' : 'Program'}:</strong> ${st.program||''}</div>
    <div><strong>${IS_AR ? 'المرشد' : 'Advisor'}:</strong> ${st.advisor_id||''}</div>
    <div><strong>${IS_AR ? 'المعدل التراكمي' : 'GPA'}:</strong> ${st.gpa??''}</div>
    <div><strong>${IS_AR ? 'الساعات المسجلة' : 'Registered credits'}:</strong> ${st.registered_credits||0}</div>
    <div><strong>${IS_AR ? 'الحد الأعلى للساعات' : 'Credit cap'}:</strong> ${st.credit_cap||0}</div>`;

  /* Auto-fill max credits box from student credit cap */
  if(st.credit_cap && Number(st.credit_cap)>0) q('maxCredits').value=Number(st.credit_cap);

  q('baselineTotals').textContent=`${IS_AR ? 'المقررات' : 'Courses'}: ${data.baseline_totals.courses} | ${IS_AR ? 'الساعات' : 'Credits'}: ${data.baseline_totals.credits}`;

  const srcCounts={};
  (data.baseline||[]).forEach(x=>{
    const s=(x.source||'mapped');
    const key=x.term_section_id || `${courseKey(x)}|${x.section||''}`;
    (srcCounts[s] ||= new Set()).add(String(key));
  });
  const srcText=Object.entries(srcCounts).map(([k,v])=>`${k}:${v.size}`).join(' | ');
  if(srcText){ q('baselineTotals').textContent += IS_AR ? ` | المصدر: ${srcText}` : ` | Source: ${srcText}`; }

  const fallbackKeys=new Set();
  (data.baseline||[]).forEach(x=>{
    if(String(x.source||'').toLowerCase().includes('fallback')){
      fallbackKeys.add(String(x.term_section_id || `${courseKey(x)}|${x.section||''}`));
    }
  });
  const fallbackRows=fallbackKeys.size;
  const quality=fallbackRows
    ? (IS_AR ? `جزئي (${fallbackRows} احتياطي)` : `partial (${fallbackRows} fallback)`)
    : (IS_AR ? 'جيد' : 'good');

  q('trustStrip').textContent = IS_AR
    ? `جودة الربط: ${quality} | غير المحلول: - | آخر بناء: -`
    : `Mapping quality: ${quality} | Unresolved: - | Last build: -`;

  setBanner('success', IS_AR ? 'تم تحميل بيانات الطالب بنجاح.' : 'Student context loaded successfully.');
  setStep('2');

  renderBaselineWeeklyCompact(data.baseline||[]);
  renderVisualTimetable(q('visualSource').value || 'baseline');

  currentRecommendations=data.recommendations||[];
  const rt=q('recTable'); rt.innerHTML='';
  currentRecommendations.forEach(rec=>{
    const tag=rec.status==='Eligible'?'success':'danger';
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td>
        <div><strong>${rec.course_code}</strong></div>
        <div class="small text-secondary">${rec.course_name||''}</div>
      </td>
      <td>
        <span class="badge text-bg-${tag}">${rec.status}</span>
        ${rec.missing_prerequisites?.length?`<div class="small text-danger">${IS_AR?'ناقص:':'Missing:'} ${rec.missing_prerequisites.join(', ')}</div>`:''}
      </td>
      <td>${rec.priority}</td>
      <td><button class="pl-btn pl-btn-teal" ${rec.status!=='Eligible'?'disabled':''}><span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></span>${IS_AR?'إضافة':'Add'}</button></td>`;

    tr.querySelector('button').onclick=()=>{
      if(shortlist.find(x=>x.course_code===rec.course_code)) return;
      shortlist.push({...rec,must_take:false});
      renderShortlist();
    };
    rt.appendChild(tr);
  });

  shortlist=currentRecommendations.filter(x=>x.status==='Eligible').map(x=>({...x,must_take:false}));
  renderShortlist();
  renderPlanPalette(currentCtx.student_id);
  renderAvailableSections();
};

enforceBuilderProcessLayout();
setStep('1');

/* ── Available sections search filter ──────────────────────── */
q('availSearch')?.addEventListener('input',function(){
  const val=this.value.trim().toUpperCase();
  document.querySelectorAll('.avail-course-card').forEach(card=>{
    card.style.display=card.dataset.code.includes(val)?'':'none';
  });
});

/* ── URL param deep-link: /planner/?student=123456&year=1447&term=1 ──────
   Advisors can open the planner pre-loaded for a specific student by
   passing URL params. All three params are optional — if year/term are
   omitted the inputs are left as-is (their defaults apply).
─────────────────────────────────────────────────────────────────────── */
(function initFromUrlParams() {
  const params = new URLSearchParams(window.location.search);
  const student = params.get('student') || params.get('student_id') || params.get('sid');
  const year    = params.get('year') || params.get('academic_year') || 'defaultYear';
  const term    = params.get('term') || 'defaultTerm';

  if (!student) return; // no deep-link, normal load

  const sidInput  = q('studentId');
  const yearInput = q('year');
  const termInput = q('term');

  if (sidInput)  sidInput.value  = student;
  if (yearInput && !yearInput.value) yearInput.value = year;
  if (termInput && !termInput.value) termInput.value = term;

  /* Update page-header chip immediately so it shows the student ID
     even before the fetch completes */
  const chip = q('plannerContextChip');
  if (chip) {
    chip.textContent = `${student}`;
    chip.classList.remove('d-none');
  }

  /* Auto-trigger fetch after a short delay so the page is fully painted */
  setTimeout(() => {
    q('fetchBtn')?.click();
  }, 120);
})();

/* UX: update page-header context chip after fetch */
const _origFetchBtn = q('fetchBtn');
if (_origFetchBtn) {
  _origFetchBtn.addEventListener('click', function patchContext() {
    const chip = q('plannerContextChip');
    if (!chip) return;
    const sid = q('studentId')?.value;
    const yr  = q('year')?.value;
    const tm  = q('term')?.value;
    if (sid) {
      chip.textContent = `${sid} · ${yr || ''}/${tm || ''}`;
      chip.classList.remove('d-none');
    }
  });
}
