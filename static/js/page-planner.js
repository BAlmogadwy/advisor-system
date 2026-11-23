const IS_AR = document.documentElement.lang === 'ar';

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
  /* The status line's right end, the build time beside the results, and the one
     mapping-quality state that is still shown (a partial one). */
  fetchIdle: IS_AR ? 'في الانتظار' : 'Idle',
  fetchLoading: IS_AR ? 'جارٍ التحميل…' : 'Loading…',
  fetchError: IS_AR ? 'خطأ' : 'Error',
  fetchedAt: IS_AR ? 'تم الجلب' : 'Fetched',
  lastBuildAt: IS_AR ? 'آخر بناء' : 'Last build',
  appliedOptionShort: (name)=> IS_AR ? `تم تطبيق الخيار ${name}` : `Applied option ${name}`,
  qualityPartial: (n)=> IS_AR ? `جودة الربط: جزئي (${n} احتياطي)` : `Mapping quality: partial (${n} fallback)`,
  noCoursesFoundStudentPlan: IS_AR ? 'لم يتم العثور على مقررات في خطة الطالب.' : 'No courses found in student plan.',
  failedLoadStudentPlanPanel: IS_AR ? 'فشل تحميل لوحة خطة الطالب.' : 'Failed to load student plan panel.',
  noRunYet: IS_AR ? 'لم يتم التشغيل بعد.' : 'No run yet.',
  noOptionSelected: IS_AR ? 'لم يتم اختيار أي خيار.' : 'No option selected.',
  noFeasibleHardConstraints: IS_AR
    ? 'لا توجد نتيجة صالحة تحقق جميع المقررات الإلزامية والشُعب المثبّتة ضمن القيود الحالية.'
    : 'No valid result satisfies every must-take course and exact pinned section under the current constraints.',
  builderRunning: IS_AR ? 'جارٍ بناء خيارات الجدول...' : 'Building timetable options...',
  crossProgramApplyDisabled: IS_AR
    ? 'الخطة التي تسمح بشُعب برامج أخرى مخصصة للمقارنة فقط. فعّل «شُعب برنامج الطالب فقط» قبل الحفظ الداخلي.'
    : 'Cross-programme options are comparison-only. Enable “Student programme sections only” before internal saving.',
};

const errMsg=(d)=> (d?.error?.message || d?.error || T.requestFailed);
const escapeHtml=(value)=>String(value??'').replace(/[&<>"]/g,(char)=>({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'
}[char]));

/* A time range as ONE left-to-right unit. Beside Arabic letters the bidi
   algorithm turns the digits into "Arabic numbers" and swaps the two times
   around the hyphen (10:15-11:55 paints as 11:55-10:15), so the range is
   isolated. In a list it must stay on one line (nowrap); in a narrow week-grid
   block `breakAfterHyphen` gives it ONE place to wrap, after the hyphen. */
function timeRangeHtml(start, end, breakAfterHyphen){
  return breakAfterHyphen
    ? safeHtml`<bdi dir="ltr">${start}-<wbr>${end}</bdi>`
    : safeHtml`<bdi dir="ltr" class="sec-range">${start}-${end}</bdi>`;
}

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
}

/* A clock time as Latin digits, 24 hours: the Arabic locale draws Arabic-Indic
   digits and a "ص"/"م" suffix, and next to Arabic letters the bidi algorithm
   would then reorder the groups. Always inside a left-to-right <bdi>. */
function clockTime(){
  return new Date().toLocaleTimeString('en-GB',{hour12:false});
}
function timeNode(){
  const bdi=document.createElement('bdi');
  bdi.dir='ltr';
  bdi.textContent=clockTime();
  return bdi;
}
/* "Fetched 11:21:23": the label, then the time in its own isolate. */
function setFetchState(kind){
  const el=q('fetchState');
  if(!el) return;
  el.textContent='';
  if(kind==='fetched'){
    el.append(`${T.fetchedAt} `, timeNode());
  } else {
    el.textContent={loading:T.fetchLoading, error:T.fetchError}[kind] || T.fetchIdle;
  }
}
/* The build time under the build summary: "Last build 11:21:31" or the applied option. */
function setLastBuildAt(label, withTime){
  const el=q('lastBuildAt');
  if(!el) return;
  el.textContent='';
  if(!label) return;
  el.append(withTime ? `${label} ` : label);
  if(withTime) el.append(timeNode());
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
    /* The classes are the only thing that changed before, so assistive tech had
       no idea which step was current or which were done. */
    if(s===step){
      el.classList.add('active');
      el.setAttribute('aria-current','step');
    } else {
      el.removeAttribute('aria-current');
      if(i<idx) el.classList.add('done');
    }
    const stepName=(el.querySelector('.wf-num')?.nextSibling?.textContent||el.textContent||'').trim();
    el.setAttribute('aria-label', `${stepName} — ${
      s===step ? (IS_AR?'الخطوة الحالية':'current step')
      : i<idx  ? (IS_AR?'مكتملة':'completed')
               : (IS_AR?'لم تبدأ':'not started')}`);
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
let currentCtx={student_id:'',academic_year:'',term:'',eligibility_mode:'strict'};
let currentBaseline=[];
let lastBuilderOptions=[];
let builderGeneration=0;
/* Same guard as builderGeneration, for the student fetch. Without it a slow
   response for student A could land after a fast one for student B and
   overwrite the whole page — summary, baseline, recommendations, shortlist —
   leaving B's ID on screen above A's data. */
let fetchGeneration=0;

/* The backend's status vocabulary is English snake_case ("passed", "studying",
   "not_taken"). Rendering it raw dropped untranslated tokens into the Arabic
   plan table; this maps them once for every caller. */
const PLAN_STATUS_AR={passed:'مجتاز', studying:'قيد الدراسة', not_taken:'لم يُدرس', failed:'راسب'};
const PLAN_STATUS_EN={passed:'Passed', studying:'Studying', not_taken:'Not taken', failed:'Failed'};
function planStatusLabel(raw){
  const key=String(raw||'').trim().toLowerCase();
  if(!key) return '';
  return (IS_AR?PLAN_STATUS_AR:PLAN_STATUS_EN)[key] || raw;
}

/* What the suggestions table DRAWS for a status or a priority. The values kept
   in the shortlist objects and sent to the server stay English; an unknown
   value is shown as it came. */
const REC_STATUS_AR={Eligible:'مؤهل', Blocked:'غير مؤهل'};
const REC_PRIORITY_AR={High:'عالية', Med:'متوسطة', Low:'منخفضة'};
function recLabel(map,raw){
  return IS_AR ? (map[raw] || raw) : raw;
}

/* The three builder methods, by plain name. */
const METHOD_NAME={
  A: IS_AR ? 'أكبر عدد من المقررات' : 'Most courses scheduled',
  B: IS_AR ? 'أسبوع مضغوط: أيام وفجوات أقل معًا' : 'Compact week: few days and few gaps together',
  C: IS_AR ? 'أقل أيام أولًا ثم أقل فجوات' : 'Fewest days first, then fewest gaps',
};
function methodName(opt){
  return METHOD_NAME[String(opt.method||opt.name||'').charAt(0).toUpperCase()] || METHOD_NAME.C;
}

/* Why a course was not scheduled, from the server's structured reason_code
   (the English sentence is free to change; the code is the contract). Numbers
   are isolated so they read left to right inside an Arabic sentence. An unknown
   code shows the server's own sentence. Built with safeHtml, so every value is
   escaped. */
function reasonHtml(u){
  const detail=(Array.isArray(u.details) && u.details[0]) || {};
  const limit=Number(detail.credit_limit);
  const credits=Number(detail.course_credits);
  const n=(v)=> IS_AR ? safeHtml`<bdi dir="ltr">${v}</bdi>` : v;
  switch(String(u.reason_code||'')){
    case 'DID_NOT_FIT':
      if(Number.isFinite(limit) && limit>0){
        if(Number.isFinite(credits) && credits>limit){
          return IS_AR
            ? safeHtml`${n(credits)} ساعات، أعلى من حد ${n(limit)} ساعة لهذا البناء`
            : safeHtml`${n(credits)} credits, above the ${n(limit)}-credit limit of this build`;
        }
        return IS_AR
          ? safeHtml`تعذّر إدراجه ضمن حد ${n(limit)} ساعة، أو أن شُعبه متعارضة`
          : safeHtml`Could not fit within the ${n(limit)}-credit limit, or its sections clash`;
      }
      break;
    case 'ALL_SECTIONS_CLASH':
      return safeHtml`${IS_AR ? 'كل الشُعب تتعارض مع الخطة المختارة' : 'Every section clashes with the chosen plan'}`;
    case 'NOT_ON_FILE':
      return safeHtml`${IS_AR ? 'لا توجد شعبة مطروحة لهذا الطالب هذا الفصل' : 'No section is offered to this student this term'}`;
    case 'MEETING_DATA_INCOMPLETE':
      return safeHtml`${IS_AR ? 'بيانات لقاءات الشعبة غير مكتملة' : 'Section meeting data is incomplete'}`;
    case 'SEARCH_BUDGET_EXHAUSTED':
      return safeHtml`${IS_AR ? 'انتهى وقت البحث قبل إدراجه' : 'The search ran out of time before placing it'}`;
    case 'SOLVER_MODEL_INVALID':
      return safeHtml`${IS_AR ? 'رفض المُحلّل هذا البناء' : 'The solver rejected this build'}`;
  }
  return safeHtml`${u.reason || (IS_AR ? 'سبب غير محدد' : 'no reason given')}`;
}

/* One line per distinct reason: "Not scheduled (3): A, B, C — reason". */
const WARN_ICON=safeHtml`<span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>`;
function unscheduledByReasonHtml(list){
  const groups=new Map();
  (list||[]).forEach(u=>{
    const why=reasonHtml(u);
    const key=String(why);
    if(!groups.has(key)) groups.set(key,{why, codes:[]});
    const code=String(u.course_code||'');
    if(code) groups.get(key).codes.push(code);
  });
  const lines=[...groups.values()].map(({why,codes})=>{
    const codeList=codes.map(c=>safeHtml`<bdi dir="ltr">${c}</bdi>`).flatMap((h,i)=>i?[', ',h]:[h]);
    const head=codes.length
      ? safeHtml`${IS_AR?'غير مجدولة':'Not scheduled'} (${IS_AR?safeHtml`<bdi dir="ltr">${codes.length}</bdi>`:codes.length}): ${codeList} — `
      : '';
    return safeHtml`<div class="opt-reason">${WARN_ICON}<span>${head}${why}</span></div>`;
  });
  return safeHtml`${lines}`;
}

/* A section's meetings, one group per distinct start-end: the days that share
   it (week order) and the range. */
const WEEK_ORDER=['SUN','MON','TUE','WED','THU','FRI','SAT'];
function groupMeetingsByTime(meetings){
  const byTime=new Map();
  (meetings||[]).forEach(m=>{
    const start=m.start_time||'', end=m.end_time||'';
    const key=`${start}-${end}`;
    if(!byTime.has(key)) byTime.set(key,{start, end, days:[]});
    const day=normalizeDay(m.day);
    const g=byTime.get(key);
    if(!g.days.includes(day)) g.days.push(day);
  });
  const rank=(d)=>{ const i=WEEK_ORDER.indexOf(d); return i<0 ? 99 : i; };
  return [...byTime.values()]
    .map(g=>({...g, days:g.days.slice().sort((a,b)=>rank(a)-rank(b))}))
    .sort((a,b)=>(toMins(a.start)??1e9)-(toMins(b.start)??1e9));
}

function useProgrammeSectionsOnly(){
  return !!q('programSectionsOnly')?.checked;
}

function allowFullSections(){
  return !!q('allowFullSections')?.checked;
}

function sectionCapacity(section){
  const maximumRaw=section?.available_capacity;
  const registeredRaw=section?.registered_count;
  const maximumKnown=maximumRaw!==null && maximumRaw!==undefined && maximumRaw!=='' && Number.isFinite(Number(maximumRaw)) && Number(maximumRaw)>=0;
  const registeredKnown=registeredRaw!==null && registeredRaw!==undefined && registeredRaw!=='' && Number.isFinite(Number(registeredRaw)) && Number(registeredRaw)>=0;
  if(!maximumKnown || !registeredKnown){
    return {known:false, full:false, maximum:null, registered:null, remaining:null};
  }
  const maximum=Number(maximumRaw);
  const registered=Number(registeredRaw);
  const remaining=Math.max(0, maximum-registered);
  return {known:true, full:remaining===0, maximum, registered, remaining};
}

function invalidateBuilderResults(){
  builderGeneration += 1;
  lastBuilderOptions=[];
  const selected=q('selectedOption');
  if(selected) selected.value='';
  const cards=q('optionCards');
  if(cards) cards.innerHTML='';
  const details=q('builderOptions');
  if(details) details.innerHTML='';
  const swaps=q('swapSuggestions');
  if(swaps) swaps.innerHTML=`<span class="text-secondary">${T.noSwaps}</span>`;
  const summary=q('builderSummary');
  if(summary) summary.textContent=T.noRunYet;
  setLastBuildAt('');
  const selectedBar=q('selectedOptionBar');
  if(selectedBar){
    selectedBar.className='planner-banner planner-banner-neutral';
    selectedBar.textContent=T.noOptionSelected;
  }
  const apply=q('applyOption');
  if(apply) apply.disabled=true;
  const source=q('visualSource');
  if(source){
    source.innerHTML='';
    const baseline=document.createElement('option');
    baseline.value='baseline';
    baseline.textContent=IS_AR?'الأساس':'Baseline';
    source.appendChild(baseline);
    source.value='baseline';
  }
  renderVisualTimetable('baseline');
}

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
        ${IS_AR
          ? `${unmapped.length} مقرر مسجّل بدون مواعيد في الجدول:`
          : `${unmapped.length} registered course${unmapped.length===1?'':'s'} have no mapped timetable slots:`}
        ${unmapped.map(r=>`<strong>${courseKey(r)}</strong>`).join(', ')}
      </div>`
    : '';

  const blocks=rows.map(r=>({
    day: normalizeDay(r.day),
    start: r.start_time,
    end: r.end_time,
    label: courseLabel(r),
    room: r.room||'',
    source: r.source||'',
  }));
  const grid=WeekGrid.renderWeekGrid({
    mode: 'blocks',
    blocks,
    timeLabel: UI.time,
    dayLabels: UI.dayShort,
    a11yLabel: IS_AR ? 'الجدول المسجّل' : 'Registered timetable',
    a11yHeadings: IS_AR ? ['اليوم','المقرر','الوقت','القاعة'] : ['Day','Course','Time','Room'],
    /* A registered week is rarely continuous: morning classes and a single
       evening one stretched this card to ~11 empty-ish hours, pushing the
       Suggested-to-add pane against a wall of blank grid. The shared renderer
       already splits on a fully-empty span and PRINTS the omitted interval
       between segments, so compression can never read as adjacent classes. */
    compressGaps: true,
    empty: `<span class="text-secondary">${T.noMappedSlots}</span>`,
    pick: (cur,inc)=>inc, // last write wins
    bg: m=>colorForCourse(m.label),
    accent: m=>colorForCourseBorder(m.label),
    cellHtml: m=>safeHtml`<span class="wg-cid">${m.label}</span><span class="wg-meta">${timeRangeHtml(m.start,m.end,true)}</span>`,
  });
  host.innerHTML=grid+unmappedHtml;
}

function renderVisualTimetable(source='baseline'){
  const host=q('visualGrid');

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

  /* Keep Registered mode: a builder plan ADDS to the student's current
     registrations, so builder views must show baseline + planned together.
     Ignore Registered mode: the plan replaces registrations — planned only. */
  const keepRegistered=(q('mode')?.value!=='ignore');
  let meetings=[];
  if(source==='baseline') meetings=baseline;
  else if(source==='overlay') meetings=[...baseline,...planned];
  else meetings=keepRegistered?[...baseline,...planned]:planned;

  // Conflict pre-pass (baseline vs planned) — applies to any mixed view.
  const mixedView=(source==='overlay')||(source!=='baseline'&&keepRegistered);
  const enriched=meetings
    .map(m=>({...m, st:toMins(m.start), en:toMins(m.end), conflict:false}))
    .filter(m=>m.st!==null && m.en!==null && m.en>m.st);
  for(let i=0;i<enriched.length;i++){
    for(let j=i+1;j<enriched.length;j++){
      const a=enriched[i], b=enriched[j];
      if(a.day!==b.day) continue;
      if(!(a.st < b.en && b.st < a.en)) continue;
      if(mixedView && a.kind!==b.kind){ a.conflict=true; b.conflict=true; }
    }
  }

  const conflictBg=(getComputedStyle(document.documentElement).getPropertyValue('--pl-conflict-cell').trim() || '#fecaca');
  host.innerHTML=WeekGrid.renderWeekGrid({
    mode: 'blocks',
    blocks: enriched,
    timeLabel: UI.time,
    dayLabels: UI.dayShort,
    a11yLabel: IS_AR ? 'الجدول المقترح' : 'Proposed timetable',
    a11yHeadings: IS_AR ? ['اليوم','المقرر','الوقت','القاعة'] : ['Day','Course','Time','Room'],
    /* Same treatment as the registered card above — and it matters more here,
       because a proposed plan is compared against it side by side. Two grids
       on one page must not use different vertical scales. */
    compressGaps: true,
    empty: `<span class="text-secondary">${IS_AR ? 'لا توجد لقاءات جدول لعرضها.' : 'No timetable meetings to display.'}</span>`,
    pick: (cur,inc)=>(inc.conflict?inc:cur), // keep first unless incoming is a conflict
    bg: m=>(m.conflict?conflictBg:colorForCourse(m.label)),
    accent: m=>(m.conflict?'#ef4444':colorForCourseBorder(m.label)),
    cellClass: m=>(m.conflict?'wg-clash':(m.kind==='planned'?'wg-planned':'')),
    cellHtml: m=>safeHtml`<span class="wg-cid">${m.label}</span><span class="wg-meta">${timeRangeHtml(m.start,m.end,true)}</span>`,
  });
}

/* The sidebar's theme toggle only sets data-theme on <html>. The grid blocks
   carry inline colours chosen at render time, so a switch left them in the old
   theme (light text on a light block, 1.15:1). Redraw just the two grids. */
new MutationObserver(()=>{
  if(!currentCtx?.student_id) return;
  renderBaselineWeeklyCompact(currentBaseline);
  renderVisualTimetable(q('visualSource').value || 'baseline');
}).observe(document.documentElement,{attributes:true, attributeFilter:['data-theme']});

q('mode').addEventListener('change',()=>{
  invalidateBuilderResults();
  q('planningBanner').classList.toggle('d-none', q('mode').value!=='ignore');
  if(currentCtx?.student_id){ renderPlanPalette(currentCtx.student_id); renderAvailableSections(); }
  renderVisualTimetable(q('visualSource').value || 'baseline');
});

function selectedEligibilityMode(){
  return q('eligibilityMode')?.value === 'relaxed' ? 'relaxed' : 'strict';
}

q('eligibilityMode')?.addEventListener('change',()=>{
  invalidateBuilderResults();
  invalidatePlanCache();
  currentCtx.eligibility_mode=selectedEligibilityMode();
  /* Eligibility changes invalidate every course picked under the old rule.
     Re-fetching also refreshes the strict-aware recommendation and plan APIs. */
  currentRecommendations=[];
  shortlist=[];
  renderShortlist();
  if(currentCtx?.student_id){
    q('fetchBtn')?.click();
  }else{
    setBanner('info', IS_AR
      ? 'تم تغيير وضع الأهلية. اجلب الطالب لتطبيقه.'
      : 'Eligibility mode changed. Fetch the student to apply it.');
  }
});

q('programSectionsOnly')?.addEventListener('change',()=>{
  invalidateBuilderResults();
  if(currentCtx?.student_id){
    renderPlanPalette(currentCtx.student_id);
    renderAvailableSections();
  }
  setBanner('info', useProgrammeSectionsOnly()
    ? (IS_AR?'سيستخدم البناء شُعب برنامج الطالب فقط. شغّل البناء من جديد.':'The builder will use student-programme sections only. Run it again.')
    : (IS_AR?'يمكن للبناء الآن مقارنة شُعب برامج أخرى؛ هذه النتائج للمقارنة فقط.':'The builder may now compare other programmes’ sections; those results are comparison-only.'));
});

q('allowFullSections')?.addEventListener('change',()=>{
  invalidateBuilderResults();
  setBanner('info', allowFullSections()
    ? (IS_AR?'سيسمح البناء بالشُعب الممتلئة للتخطيط فقط. شغّل البناء من جديد.':'Full sections will be allowed for planning only. Run the builder again.')
    : (IS_AR?'سيستبعد البناء الشُعب المعروفة بأنها ممتلئة. شغّل البناء من جديد.':'Known full sections will be excluded. Run the builder again.'));
});

q('maxCredits')?.addEventListener('change',invalidateBuilderResults);
q('swap')?.addEventListener('change',invalidateBuilderResults);

q('visualSource').addEventListener('change',(e)=>renderVisualTimetable(e.target.value));
['1','2','3'].forEach(s=> q('step'+s)?.addEventListener('click',()=>setStep(s)));
q('simpleMode')?.addEventListener('change',()=>setStep(currentStep));
q('prevStep')?.addEventListener('click',()=>{ const order=['1','2','3']; const i=Math.max(0,order.indexOf(currentStep)-1); setStep(order[i]); });
q('nextStep')?.addEventListener('click',()=>{ const order=['1','2','3']; const i=Math.min(order.length-1,order.indexOf(currentStep)+1); setStep(order[i]); });

function renderShortlist(){
  invalidateBuilderResults();
  const wrap=q('shortlist'); wrap.innerHTML='';
  let credits=0;
  shortlist.forEach((c,idx)=>{
    credits += Number(c.credits||0);
    const row=document.createElement('div');
    row.className='sl-row';

    const code=escapeHtml(c.course_code);
    const hasPinned=(c.pinned_sections&&c.pinned_sections.length>0);
    const pinnedHtml=hasPinned
      ? `<span class="sl-pin">
           <span class="fs-11 fw-semibold text-teal" title="${IS_AR?'هذه هي الشعبة الوحيدة المسموح بها لهذا المقرر':'This is the only allowed section for this course'}">${IS_AR?'الشعبة المثبّتة:':'Pinned section:'}</span>
           ${c.pinned_sections.map((p,pi)=>`<button type="button" class="sl-pin-badge fs-11 fw-semibold" data-pi="${pi}" aria-label="${IS_AR?'إزالة تثبيت الشعبة':'Remove pinned section'} ${escapeHtml(p.section)}">§${escapeHtml(p.section)} <span class="sl-pin-x" aria-hidden="true">✕</span></button>`).join('')}
         </span>`
      : `<span class="sl-pin fs-11 text-t4">${IS_AR?'أي شعبة':'Any section'}</span>`;
    const removeLabel=`${IS_AR?'إزالة':'Remove'} ${c.course_code}`;

    row.innerHTML=`
      <span class="sl-main"><strong>${code}</strong> ${escapeHtml(c.course_name||'')}
        <span class="text-secondary">(${escapeHtml(c.credits||0)} ${IS_AR?'ساعة':'cr'})</span></span>
      ${pinnedHtml}
      <span class="sl-must">
        <input type="checkbox" class="form-check-input" id="m${idx}" ${c.must_take?'checked':''}>
        <label class="form-check-label" for="m${idx}" title="${IS_AR?'يجب أن يظهر هذا المقرر في كل نتيجة':'This course is required in every result'}">${IS_AR?'إلزامي — في كل نتيجة':'Must-take — every result'}</label>
      </span>
      <button type="button" class="btn btn-sm btn-neutral btn-icon sl-remove" data-sl-remove data-i="${idx}" aria-label="${escapeHtml(removeLabel)}" title="${escapeHtml(removeLabel)}"><span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span></button>`;

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
    row.querySelector('input[type="checkbox"]').onchange=(e)=>{
      shortlist[idx].must_take=e.target.checked;
      invalidateBuilderResults();
    };
  });
  q('shortCredits').textContent=String(credits);
}

/* One cached copy of the student plan per student. The three checkboxes below
   the palette are display filters — they change which rows are drawn, nothing
   the server computes — yet each toggle used to re-issue this request AND the
   sections-catalog POST. Cleared whenever a different student is fetched. */
let _planCache={student:null, eligibilityMode:null, data:null, sections:null};
function invalidatePlanCache(){
  _planCache={student:null, eligibilityMode:null, data:null, sections:null};
}

function studentPlanUrl(studentId, eligibilityMode){
  return `/report/student-plan/?student_id=${encodeURIComponent(studentId)}&eligibility_mode=${encodeURIComponent(eligibilityMode)}`;
}

async function renderPlanPalette(studentId){
  const wrap=q('planPalette');
  const eligibilityMode=currentCtx.eligibility_mode || selectedEligibilityMode();
  const cacheMatches=_planCache.student===String(studentId)
    && _planCache.eligibilityMode===eligibilityMode;
  const cached=cacheMatches ? _planCache.data : null;
  if(!cached){
    wrap.innerHTML=`<span class="text-secondary">${IS_AR ? 'جارٍ تحميل خطة الطالب...' : 'Loading student plan...'}</span>`;
  }
  try{
    let planData=cached;
    if(!planData){
      const planRes=await fetch(studentPlanUrl(studentId, eligibilityMode));
      planData=await planRes.json();
      if(!planData.error){
        _planCache={student:String(studentId), eligibilityMode, data:planData, sections:null};
      }
    }
    if(planData.error){ wrap.innerHTML=`<span class="text-danger">${planData.error}</span>`; return; }

    const allCourses=[];
    (planData.terms||[]).forEach(t=> (t.courses||[]).forEach(c=> allCourses.push({...c, _term:t.term})));
    const codes=[...new Set(allCourses.map(c=>String(c.course_code||'').replace(/\s+/g,'').toUpperCase()).filter(Boolean))];

    let sections=cacheMatches ? (_planCache.sections||null) : null;
    if(sections===null && codes.length){
      sections=[];
      const secRes=await fetch('/ops/planner/sections-catalog/',{
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
        body:JSON.stringify({
          student_id:currentCtx.student_id,
          academic_year:currentCtx.academic_year,
          term:currentCtx.term,
          course_codes:codes,
          program_sections_only:useProgrammeSectionsOnly()
        })
      });
      const secData=await secRes.json();
      sections=secData.sections||[];
      if(_planCache.student===String(studentId)
          && _planCache.eligibilityMode===eligibilityMode) _planCache.sections=sections;
    }
    if(sections===null) sections=[];

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
        const hasOpen=list.some(s=> !sectionCapacity(s).full);
        const prereqOk = (String(c.status||'') !== 'not_taken') ? true : Boolean(c.can_register);

        let offer='none';
        let label=IS_AR?'غير مطروح':'not offered';

        if(!prereqOk){
          offer='prereq'; label=IS_AR?'المتطلبات غير مستوفاة':'prereq missing';
        }else if(hasAny && hasOpen){
          offer='open'; label=IS_AR?'شعب متاحة':'open sections';
        }else if(hasAny){
          offer='full'; label=IS_AR?'كل الشعب ممتلئة':'all full';
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
        if(canAdd) tr.className='is-addable';
        tr.innerHTML=`
          <td>
            <input type="checkbox" class="form-check-input me-2 planPick"
              data-code="${code}"
              data-credits="${Number(c.credit_hours||0)}"
              data-score="${Math.round(Number(c.importance_score||0)*20)}"
              aria-label="${IS_AR?'اختيار':'Select'} ${code}"
              ${canAdd?'':'disabled'}>
            <strong>${code}</strong> <span class="text-secondary">(${c.credit_hours||0}${IS_AR?' ساعة':'cr'})</span>
          </td>
          <td>${planStatusLabel(c.status)}</td>
          <td>${Number(c.importance_score||0).toFixed(2)}</td>
          <td><span class="pl-offer" data-state="${safeHtml`${offer}`}">${label}</span></td>
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
    const eligibilityMode=currentCtx.eligibility_mode || selectedEligibilityMode();
    const planRes=await fetch(studentPlanUrl(currentCtx.student_id, eligibilityMode));
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
      body:JSON.stringify({
        student_id:currentCtx.student_id,
        academic_year:currentCtx.academic_year,
        term:currentCtx.term,
        course_codes:codes,
        program_sections_only:useProgrammeSectionsOnly()
      })
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
        const capacity=sectionCapacity(s);
        const hasSeats=!capacity.full;
        const conflict=hasConflict(s.meetings);

        let colorClass, dotClass, statusLabel;
        if(!conflict&&hasSeats){
          colorClass='sec-teal'; dotClass='sec-dot-teal';
          statusLabel=capacity.known?(IS_AR?'متاح':'Open'):(IS_AR?'السعة غير معروفة':'Capacity unknown'); counts.green++;
        }else if(conflict&&hasSeats){
          colorClass='sec-rose'; dotClass='sec-dot-rose';
          statusLabel=capacity.known?(IS_AR?'تعارض':'Conflict'):(IS_AR?'تعارض · السعة غير معروفة':'Conflict · capacity unknown'); counts.pink++;
        }else if(conflict&&!hasSeats){
          colorClass='sec-red'; dotClass='sec-dot-red';
          statusLabel=IS_AR?'ممتلئ+تعارض':'Full+Conflict'; counts.red++;
        }else{
          colorClass='sec-gray'; dotClass='sec-dot-gray';
          statusLabel=IS_AR?'ممتلئ':'Full'; counts.gray++;
        }

        /* one line per distinct start-end: the days that share it, then the range */
        const timeGroups=groupMeetingsByTime(s.meetings);
        const groupDays=g=>g.days.map(d=>UI.dayShort[d]||d).join(' ');
        const timeStr=timeGroups.map(g=>`${groupDays(g)} ${g.start}-${g.end}`).join(', ')
          ||(IS_AR?'غير محدد':'TBA');
        /* the HTML twin of timeStr for the visible cell; timeStr stays plain text
           for the aria-label */
        const timeHtml=timeGroups.length
          ? safeHtml`${timeGroups.map(g=>safeHtml`<span class="sec-time-line">${groupDays(g)} ${timeRangeHtml(g.start,g.end)}</span>`)}`
          : (IS_AR?'غير محدد':'TBA');

        const capacityText=capacity.known
          ? `${capacity.registered}/${capacity.maximum}`
          : (IS_AR?'السعة غير معروفة':'Capacity unknown');
        const programs=(s.programs||[]).join('/') || (IS_AR?'غير مرتبط ببرنامج':'Unassigned');

        /* A real <button>: this is the ONLY way to pin an exact section, and as a
           plain div it was unreachable by keyboard entirely. statusLabel is now
           VISIBLE text as well as the accessible name — the four states used to
           differ only by a tint and a 6px dot, which a colour-blind adviser
           cannot read, and the sole text lived in a title attribute on a
           non-focusable element where no screen reader would announce it. */
        const secAria=[
          `${IS_AR?'الشعبة':'Section'} ${s.section||'?'}`, statusLabel, timeStr,
          capacityText===statusLabel ? '' : capacityText,   // don't say it twice
        ].filter(Boolean).join(' · ');
        secHtml+=`
          <button type="button" class="avail-sec-row ${colorClass}" data-tsid="${s.term_section_id}" aria-label="${secAria}">
            <span class="sec-dot ${dotClass}" aria-hidden="true"></span>
            <span class="sec-id">${s.section||'?'}</span>
            <span class="sec-time">${safeHtml`${timeHtml}`}</span>
            <span class="sec-program">${programs}</span>
            <span class="sec-capacity">${capacityText}</span>
            <span class="sec-status">${statusLabel}</span>
          </button>`;
      });

      card.innerHTML=`
        <button type="button" class="avail-course-head u-cursor-pointer" aria-label="${course.code} — ${IS_AR?'إضافة المقرر (أي شعبة)':'add course (any section)'}">
          <div>
            <div class="avail-course-code">${course.code}</div>
            <div class="avail-course-name">${course.name}</div>
          </div>
          <span class="avail-course-credits" style="margin-inline-end:4px">${course.credits} ${IS_AR?'ساعة':'cr'}</span>
          <span class="avail-course-credits" data-tone="${course.importance>=3?'high':course.importance>=1?'mid':'low'}">⚡${course.importance.toFixed(1)}</span>
        </button>
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
            /* A pin is exact and singular: choosing another section replaces it. */
            if(existing.pinned_sections?.length===1 && existing.pinned_sections[0].term_section_id===tsid){
              setBanner('info', IS_AR?`الشعبة ${secLabel} مضافة مسبقاً`:`Section ${secLabel} already pinned`);
              return;
            }
            existing.pinned_sections=[{term_section_id:tsid, section:secLabel}];
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
  if(!useProgrammeSectionsOnly()) return notify.warning(T.crossProgramApplyDisabled);

  const opt=(lastBuilderOptions||[]).find(x=>String(x.name||'')===String(name));
  if(!opt) return notify.warning(T.optionNotFound);

  const ids=(opt.mappings||[]).map(m=>m.term_section_id).filter(Boolean);
  if(!ids.length){ setBanner('warning', T.noMappableInOption(name)); return; }

  /* This is the ONLY operation on the page that writes, and it is a REPLACE:
     any planner section saved earlier that is not in this option is deleted.
     The request has always carried confirm_replace:true - a flag whose name
     promises a gate that did not exist anywhere in the UI. Ask first, naming
     what is about to be written. */
  const courseList=(opt.mappings||[])
    .map(m=>`${m.course_code||''} ${m.section||''}`.trim()).filter(Boolean);
  const proceed = window.confirm(
    (IS_AR
      ? `سيتم استبدال جدول الطالب المخطط بالخيار ${name}:

`
      : `This replaces the student's planned timetable with option ${name}:

`)
    + courseList.join(String.fromCharCode(10))
    + (IS_AR
      ? `

سيتم حذف أي مقررات مخططة سابقًا غير موجودة في هذا الخيار. متابعة؟`
      : `

Any previously planned courses not in this option will be removed. Continue?`)
  );
  if(!proceed) return;

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

  /* Report what the write actually did, including what it took away. */
  const removed=Array.isArray(data.removed)?data.removed:[];
  const clashes=Array.isArray(data.clashes_with_registered)?data.clashes_with_registered:[];
  setBanner('success', T.appliedOption(name, data.inserted||0));
  if(removed.length){
    notify.warning(
      IS_AR ? `أُزيلت ${removed.length} مقرر مخطط سابقًا` : `Removed ${removed.length} previously planned course(s)`,
      removed.map(x=>`${x.course_code||''} ${x.section||''}`.trim()).join(', ')
    );
  }
  if(clashes.length){
    notify.warning(
      IS_AR ? `تعارض مع الجدول المسجّل (${clashes.length})` : `Clashes with the registered timetable (${clashes.length})`,
      clashes.map(x=>`${x.course_code} ${x.section} @ ${x.slot} vs ${x.clashes_with}`).join('; ')
    );
  }
  setStep('3');
  q('fetchBtn').click();
  /* After the click: the re-fetch it starts empties this line first (it
     invalidates the build results), so the applied option is written after. */
  setLastBuildAt(T.appliedOptionShort(name), false);
};

q('runBuilder').onclick=async()=>{
  setStep('3');
  const applyButton=q('applyOption');
  if(applyButton) applyButton.disabled=true;

  /* Fix: correct preconditions + correct message */
  if(!currentCtx.student_id) return notify.warning(T.fetchStudentFirst);
  if(!currentCtx.academic_year || !currentCtx.term) return notify.warning(T.enterYearTerm);

  invalidateBuilderResults();
  const runGeneration=builderGeneration;
  setBanner('info', T.builderRunning);

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
        eligibility_mode:currentCtx.eligibility_mode || selectedEligibilityMode(),
        swap:q('swap').checked,
        program_sections_only:useProgrammeSectionsOnly(),
        allow_full_sections:allowFullSections(),
        max_credits:Number(q('maxCredits')?.value||0),
        shortlist,
        baseline:currentBaseline
      })
    });
    data=await r.json();
  }catch(err){
    if(runGeneration!==builderGeneration) return;
    setBanner('danger', IS_AR?'فشل تشغيل المُنشئ':'Builder request failed');
    notify.error(IS_AR?'فشل تشغيل المُنشئ':'Builder request failed', err.message||String(err));
    return;
  }
  if(runGeneration!==builderGeneration) return;
  if(data.error){setBanner('danger', errMsg(data)); return;}

  const s=data.summary||{};
  const options=Array.isArray(data.options)?data.options:[];
  const hardFailures=Array.isArray(s.hard_constraint_failures)?s.hard_constraint_failures:[];
  const feasible=Boolean(s.best_feasible && options.length);
  const failureHtml=hardFailures.map(f=>{
    const code=escapeHtml(f.course_code||'');
    const reason=IS_AR
      ? (f.kind==='must_take'
        ? 'تعذر إدراج المقرر الإلزامي ضمن القيود الحالية.'
        : (f.kind==='pinned_section'
          ? 'تعذر استخدام الشعبة المثبّتة بالضبط.'
          : 'يتعارض حد الساعات مع القيود المطلوبة.'))
      : escapeHtml(f.reason||T.noFeasibleHardConstraints);
    return `<div class="mt-1">• ${code?`<strong>${code}</strong>: `:''}${reason}</div>`;
  }).join('');
  /* The per-course reasons the server computed. They were only ever rendered
     INSIDE an option card, so when a build produced no options — the exact
     moment an adviser needs to know why — the page showed one generic banner
     and discarded every reason. An adviser who cannot tell "impossible" from
     "nothing found" cannot act. */
  const blocked=Array.isArray(data.unscheduled)?data.unscheduled:[];
  const blockedHtml=(!feasible && blocked.length)
    ? `<div class="mt-2"><strong>${IS_AR?'المقررات التي تعذّرت جدولتها':'Courses that could not be scheduled'}:</strong></div>`
      + `<div class="opt-diag">${safeHtml`${unscheduledByReasonHtml(blocked)}`}</div>`
    : '';

  const summaryEl=q('builderSummary');
  summaryEl.className=`planner-banner planner-banner-${feasible?'ok':'warn'}`;
  summaryEl.innerHTML=`
    <div><strong>${IS_AR ? 'تمت الجدولة' : 'Scheduled'}:</strong> ${s.scheduled||0}/${s.target||0}</div>
    <div><strong>${IS_AR ? 'التعارضات' : 'Conflicts'}:</strong> ${s.conflicts||0}</div>
    <div><strong>${IS_AR ? 'التبديلات المطلوبة' : 'Swaps required'}:</strong> ${s.swaps_required||0}</div>
    <div><strong>${IS_AR ? 'الحالة' : 'Status'}:</strong> ${feasible?(IS_AR?'تم العثور على أفضل خطة ممكنة':'Best feasible plan found'):(IS_AR?'لا توجد خطة تحقق جميع القيود الإلزامية':'No plan satisfies all hard constraints')}</div>
    ${failureHtml}${blockedHtml}`;

  setBanner(
    feasible?'success':'warning',
    feasible
      ? (IS_AR ? 'اكتمل تشغيل المُنشئ. راجع الخيارات أدناه.' : 'Builder run completed. Review options below.')
      : T.noFeasibleHardConstraints
  );

  setLastBuildAt(T.lastBuildAt, true);

  const wrap=q('builderOptions');
  wrap.innerHTML='';
  lastBuilderOptions=options;
  q('selectedOption').value='';
  const cards=q('optionCards');
  cards.innerHTML='';
  if(!feasible){
    cards.innerHTML=`<div class="col-12"><div class="planner-banner planner-banner-warn">${T.noFeasibleHardConstraints}</div></div>`;
  }

  const vs=q('visualSource');
  if(vs){
    const prev=vs.value;
    vs.innerHTML='';
    const mk=(v,t)=>{const o=document.createElement('option');o.value=v;o.textContent=t;vs.appendChild(o);};
    mk('baseline', IS_AR ? 'الأساس' : 'Baseline');
    options.forEach(o=>mk(String(o.name||''), IS_AR ? `الخيار ${o.name||''}` : `Option ${o.name||''}`));
    mk('overlay', IS_AR ? 'تراكب (الأساس + المحدد)' : 'Overlay (Baseline + Selected)');
    if([...vs.options].some(o=>o.value===prev)) vs.value=prev;
  }

  let bestName=''; let bestScore=-1;
  options.forEach(opt=>{ if((opt.scheduled||0)>bestScore){bestScore=(opt.scheduled||0); bestName=String(opt.name||'');} });

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

    const apply=q('applyOption');
    if(apply){
      apply.disabled=!name || !useProgrammeSectionsOnly();
      apply.title=(!useProgrammeSectionsOnly())?T.crossProgramApplyDisabled:'';
    }

    cards.querySelectorAll('[data-opt]').forEach(el=>{
      const sel=el.getAttribute('data-opt')===String(name||'');
      el.classList.toggle('is-selected', sel);
      const pick=el.querySelector('.opt-select');
      pick.setAttribute('aria-pressed', sel?'true':'false');
      pick.innerHTML=sel
        ? `<span class="i" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg></span>${IS_AR?'محدد':'Selected'}`
        : (IS_AR?'اختيار':'Select');
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

  options.forEach(opt=>{
    const col=document.createElement('div');
    col.className='col-md-4';
    col.innerHTML=`
      <div class="planner-panel option-card" data-opt="${escapeHtml(opt.name)}">
        <div class="fw-semibold opt-name">${IS_AR?'الخيار':'Option'} ${escapeHtml(opt.name)}</div>
        <div class="opt-method">${escapeHtml(methodName(opt))}</div>
        <div class="fs-12 opt-count">${IS_AR?'تمت الجدولة':'Scheduled'} <strong class="text-teal">${opt.scheduled||0}</strong>/${opt.target||0}</div>
        <div class="fs-11 text-t4">${IS_AR?'غير مجدول':'Unscheduled'} ${(opt.unscheduled||[]).length||0}</div>
        <button type="button" class="pl-btn pl-btn-teal w-100 justify-content-center mt-2 opt-select" aria-pressed="false">${IS_AR?'اختيار':'Select'}</button>
      </div>`;
    col.querySelector('button').onclick=()=>chooseOption(opt.name);
    cards.appendChild(col);
  });

  if(bestName){ chooseOption(bestName); }

  options.forEach(opt=>{
    const div=document.createElement('div');
    div.className='border rounded p-2 mb-2';
    div.dataset.diag=String(opt.name||'');

    div.innerHTML=`
      <div><strong>${IS_AR?'الخيار':'Option'} ${escapeHtml(opt.name)}</strong> - ${IS_AR?'تمت الجدولة':'Scheduled'} ${escapeHtml(opt.scheduled)}/${escapeHtml(opt.target)}</div>
      <div class="small text-secondary">${escapeHtml(methodName(opt))}</div>
      <div class="mt-1">
        ${(opt.mappings||[]).map(m=>`<div>• ${escapeHtml(courseKey(m))} → ${escapeHtml(m.section)}</div>`).join('') || `<span class="text-secondary">${T.noMappings}</span>`}
      </div>
      <div class="mt-1 opt-diag">
        ${safeHtml`${unscheduledByReasonHtml(opt.unscheduled)}`}
      </div>`;
    wrap.appendChild(div);
  });

  q('swapSuggestions').innerHTML=(data.swap_suggestions||[]).map(s=>`<div>• ${escapeHtml(s.course_code)}: ${escapeHtml(s.from_section)} → ${escapeHtml(s.to_section)} (${escapeHtml(s.reason)})</div>`).join('') || `<span class="text-secondary">${T.noSwaps}</span>`;

  renderVisualTimetable(q('visualSource').value || 'baseline');
};

q('fetchBtn').onclick=async()=>{
  invalidateBuilderResults();
  setStep('1');
  const runFetch = ++fetchGeneration;
  invalidatePlanCache();
  const fetchBtnEl = q('fetchBtn');
  if(fetchBtnEl) fetchBtnEl.disabled = true;   // removes the easy double-click trigger
  setFetchState('loading');
  const qualityEl=q('baselineQuality');
  if(qualityEl){ qualityEl.className=''; qualityEl.textContent=''; }
  setBanner('info', IS_AR ? 'جارٍ جلب بيانات الطالب...' : 'Fetching student context...');

  let r, data;
  try{
    r=await fetch('/ops/planner/context/',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':getCsrfToken()},
      body:JSON.stringify({
        student_id:q('studentId').value,
        academic_year:q('year').value,
        term:q('term').value,
        eligibility_mode:selectedEligibilityMode()
      })
    });
    data=await r.json();
  }catch(err){
    if(runFetch!==fetchGeneration) return;          // a newer fetch owns the page
    if(fetchBtnEl) fetchBtnEl.disabled = false;
    setFetchState('error');
    setBanner('danger', IS_AR?'فشل جلب بيانات الطالب':'Failed to fetch student context');
    notify.error(IS_AR?'فشل جلب بيانات الطالب':'Failed to fetch student context', err.message||String(err));
    return;
  }
  /* Everything past this point WRITES page state, so a superseded response must
     stop here — before it touches currentCtx, the baseline or the shortlist. */
  if(runFetch!==fetchGeneration) return;
  if(fetchBtnEl) fetchBtnEl.disabled = false;
  if(data.error){
    setFetchState('error');
    setBanner('danger', errMsg(data));
    return;
  }

  setFetchState('fetched');
  /* A student is loaded: the panels replace the one placeholder, and stay even
     if a later fetch fails. */
  document.querySelector('.planner-stage')?.setAttribute('data-loaded','true');

  currentCtx={
    student_id:String(data.student?.student_id||q('studentId').value),
    academic_year:String(data.year||q('year').value),
    term:String(data.term||q('term').value),
    eligibility_mode:String(data.eligibility_mode||selectedEligibilityMode())
  };
  if(q('eligibilityMode')) q('eligibilityMode').value=currentCtx.eligibility_mode;

  currentBaseline=(data.baseline||[]);

  const st=data.student||{};
  /* The student's figures as a stat strip. Every value comes from the response
     goes through safeHtml, which escapes it (the name used to be written as
     markup). */
  const gpaNumber=Number(st.gpa);
  const gpaText=(st.gpa===null || st.gpa===undefined || st.gpa==='' || !Number.isFinite(gpaNumber))
    ? '—' : gpaNumber.toFixed(2);
  const capUnit=(st.regulatory_max_credits && Number(st.regulatory_max_credits)>Number(st.credit_cap||0))
    ? safeHtml` <span class="ux-stat-unit">${IS_AR
        ? safeHtml`إرشادي · النظامي ${st.regulatory_max_credits}`
        : safeHtml`advisory · regulatory ${st.regulatory_max_credits}`}</span>`
    : '';
  const advisorPart=st.advisor_id
    ? safeHtml` · ${IS_AR ? 'المرشد' : 'Advisor'}: <bdi>${st.advisor_id}</bdi>`
    : '';
  q('studentSummary').innerHTML=String(safeHtml`
    <div class="planner-student">
      <div class="planner-student-who">
        <strong class="planner-student-name">${st.name||''}</strong>
        <span class="planner-student-meta"><bdi>${st.student_id||''}</bdi> · ${st.program||''}${advisorPart}</span>
      </div>
      <section class="ux-stat-strip" aria-label="${IS_AR ? 'أرقام الطالب' : 'Student figures'}">
        <div class="ux-stat-group">
          <dl>
            <div class="ux-stat"><dt>${IS_AR ? 'المعدل التراكمي' : 'GPA'}</dt><dd>${gpaText}</dd></div>
            <div class="ux-stat"><dt>${IS_AR ? 'الساعات المسجلة' : 'Registered credits'}</dt><dd>${st.registered_credits||0}</dd></div>
            <div class="ux-stat"><dt>${IS_AR ? 'الحد الأعلى للساعات' : 'Credit cap'}</dt><dd>${st.credit_cap||0}${capUnit}</dd></div>
          </dl>
        </div>
      </section>
    </div>`);

  /* Auto-fill max credits box from student credit cap */
  if(st.credit_cap && Number(st.credit_cap)>0) q('maxCredits').value=Number(st.credit_cap);
  /* The builder turns this box into a hard sum(credits) <= max constraint, so
     pre-filling it with the ADVISORY 18 silently forbade the 19th credit the
     student is entitled to. The regulatory ceiling is now stated where the
     adviser sets the number, instead of only being sent and ignored. */
  const maxHint=q('maxCreditsHint');
  if(maxHint && st.regulatory_max_credits){
    maxHint.textContent = IS_AR
      ? `إرشادي ${st.credit_cap||0} · النظامي ${st.regulatory_max_credits}`
      : `advisory ${st.credit_cap||0} · regulatory ${st.regulatory_max_credits}`;
    q('maxCredits')?.setAttribute('max', String(st.regulatory_max_credits));
  }

  q('baselineTotals').textContent=`${IS_AR ? 'المقررات' : 'Courses'}: ${data.baseline_totals.courses} | ${IS_AR ? 'الساعات' : 'Credits'}: ${data.baseline_totals.credits}`;

  const fallbackKeys=new Set();
  (data.baseline||[]).forEach(x=>{
    if(String(x.source||'').toLowerCase().includes('fallback')){
      fallbackKeys.add(String(x.term_section_id || `${courseKey(x)}|${x.section||''}`));
    }
  });
  const fallbackRows=fallbackKeys.size;
  /* Mapping quality is only worth a line when it is NOT good. */
  if(qualityEl && fallbackRows){
    qualityEl.className='planner-banner planner-banner-warn';
    qualityEl.textContent=T.qualityPartial(fallbackRows);
  }

  setBanner('success', IS_AR ? 'تم تحميل بيانات الطالب بنجاح.' : 'Student context loaded successfully.');
  setStep('2');

  renderBaselineWeeklyCompact(data.baseline||[]);
  renderVisualTimetable(q('visualSource').value || 'baseline');

  currentRecommendations=data.recommendations||[];
  const rt=q('recTable'); rt.innerHTML='';
  currentRecommendations.forEach(rec=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td>
        <div><strong>${escapeHtml(rec.course_code)}</strong></div>
        <div class="small text-secondary">${escapeHtml(rec.course_name||'')}</div>
      </td>
      <td>
        <span class="rec-status" data-tone="${rec.status==='Eligible'?'ok':'blocked'}">${escapeHtml(recLabel(REC_STATUS_AR,rec.status))}</span>
        ${rec.missing_prerequisites?.length?`<div class="small text-danger">${IS_AR?'ناقص:':'Missing:'} ${rec.missing_prerequisites.map(m=>`<bdi dir="ltr">${escapeHtml(m)}</bdi>`).join(', ')}</div>`:''}
      </td>
      <td>${escapeHtml(recLabel(REC_PRIORITY_AR,rec.priority))}</td>
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

/* ── Options toggle (a phone folds the settings under the session bar) ──────
   From 769px the CSS shows them whatever state this left; the toggle only
   keeps aria-expanded true to what a phone shows. */
(function initOptionsToggle(){
  const toggle=document.querySelector('.planner-options-toggle');
  const options=q('plannerOptions');
  if(!toggle || !options) return;
  toggle.addEventListener('click',()=>{
    const open=toggle.getAttribute('aria-expanded')!=='true';
    toggle.setAttribute('aria-expanded', String(open));
    options.classList.toggle('is-open', open);
  });
})();
