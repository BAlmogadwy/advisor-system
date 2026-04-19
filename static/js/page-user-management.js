/* ═══════════════════════════════════════════════════════════════
   STATE
   ═══════════════════════════════════════════════════════════════ */
let allUsers = [];
let selectedUsername = null;
let bulkSelected = new Set();

const IS_AR = document.documentElement.lang === 'ar';

const T = {
  // ── Loading & errors ──
  loading:          IS_AR ? 'جارٍ التحميل…'                  : 'Loading…',
  failedLoadUsers:  IS_AR ? 'تعذّر تحميل المستخدمين'          : 'Failed to load users',
  loadedUsers:   (n) => IS_AR ? `تم تحميل ${n} مستخدم`       : `Loaded ${n} users`,
  networkError:     IS_AR ? 'خطأ في الاتصال أثناء تحميل المستخدمين' : 'Network error loading users',
  nUsers:        (n) => IS_AR ? `${n} مستخدم`                 : `${n} users`,

  // ── Empty state ──
  noUsersFound:     IS_AR ? 'لم يُعثر على مستخدمين'           : 'No users found',
  adjustFilters:    IS_AR ? 'جرّب تعديل معايير البحث'          : 'Try adjusting your filters',

  // ── Role badges ──
  superAdmin:       IS_AR ? 'مشرف عام'                        : 'Super Admin',
  genAdvisor:       IS_AR ? 'مرشد عام'                        : 'Gen. Advisor',
  advisor:          IS_AR ? 'مرشد'                             : 'Advisor',

  // ── Status badges ──
  active:           IS_AR ? '● نشط'                            : '● Active',
  disabled:         IS_AR ? '○ معطّل'                          : '○ Disabled',
  clickToDisable:   IS_AR ? 'انقر للتعطيل'                    : 'Click to disable',
  clickToEnable:    IS_AR ? 'انقر للتفعيل'                    : 'Click to enable',

  // ── Time ──
  never:            IS_AR ? 'أبدًا'                            : 'Never',
  justNow:          IS_AR ? 'الآن'                             : 'Just now',
  hoursAgo:      (h) => IS_AR ? `قبل ${h} ساعة`              : `${h}h ago`,
  daysAgo:       (d) => IS_AR ? `قبل ${d} يوم`               : `${d}d ago`,

  // ── Row action buttons ──
  editRoleScope:    IS_AR ? 'تعديل الدور والصلاحيات'          : 'Edit role/scope',
  edit:             IS_AR ? 'تعديل'                            : 'Edit',
  resetPassword:    IS_AR ? 'إعادة تعيين كلمة المرور'         : 'Reset password',
  deleteUser:       IS_AR ? 'حذف المستخدم'                    : 'Delete user',
  deleteLabel:      IS_AR ? 'حذف'                              : 'Delete',

  // ── Validation ──
  usernamePasswordReq: IS_AR ? 'اسم المستخدم وكلمة المرور مطلوبان' : 'Username and password are required',
  noUserSelected:   IS_AR ? 'لم يتم اختيار مستخدم'            : 'No user selected',

  // ── Create user ──
  userCreated:      IS_AR ? 'تم إنشاء المستخدم'               : 'User created',
  failedCreateUser: IS_AR ? 'تعذّر إنشاء المستخدم'            : 'Failed to create user',

  // ── Update role ──
  roleUpdated:      IS_AR ? 'تم تحديث الدور'                  : 'Role updated',
  failedUpdateRole: IS_AR ? 'تعذّر تحديث الدور'               : 'Failed to update role',

  // ── Password reset dialog ──
  resetPwTitle:     IS_AR ? 'إعادة تعيين كلمة المرور'         : 'Reset password',
  resetPwBody:   (u) => IS_AR ? `<p>أدخل كلمة مرور جديدة للمستخدم <strong>${u}</strong>.</p>` : `<p>Enter a new password for <strong>${u}</strong>.</p>`,
  newPassword:      IS_AR ? 'كلمة المرور الجديدة'              : 'New password',
  enterNewPw:       IS_AR ? 'أدخل كلمة المرور الجديدة…'       : 'Enter new password…',
  setPassword:      IS_AR ? 'تعيين كلمة المرور'               : 'Set password',
  confirmResetTitle:IS_AR ? 'تأكيد إعادة تعيين كلمة المرور'   : 'Confirm password reset',
  confirmResetBody:(u) => IS_AR ? `<p>تعيين كلمة مرور جديدة للمستخدم <strong>${u}</strong>؟</p>` : `<p>Set a new password for <strong>${u}</strong>?</p>`,
  confirmReset:     IS_AR ? 'تأكيد إعادة التعيين'              : 'Confirm reset',
  passwordReset:    IS_AR ? 'تم إعادة تعيين كلمة المرور'      : 'Password reset',
  failedResetPw:    IS_AR ? 'تعذّر إعادة تعيين كلمة المرور'   : 'Failed to reset password',

  // ── Toggle active ──
  enable:           IS_AR ? 'تفعيل'                            : 'Enable',
  disable:          IS_AR ? 'تعطيل'                            : 'Disable',
  enableAccountTitle:(u) => IS_AR ? `تفعيل حساب المستخدم؟`    : `Enable user account?`,
  disableAccountTitle:(u)=> IS_AR ? `تعطيل حساب المستخدم؟`    : `Disable user account?`,
  restoreAccess: (u) => IS_AR ? `<p>استعادة صلاحية الدخول للمستخدم <strong>${u}</strong>.</p>` : `<p>Restore login access for <strong>${u}</strong>.</p>`,
  preventLogin:  (u) => IS_AR ? `<p>سيتم منع <strong>${u}</strong> من تسجيل الدخول.</p><p>يمكنك إعادة التفعيل في أي وقت.</p>` : `<p>This will prevent <strong>${u}</strong> from logging in.</p><p>You can re-enable at any time.</p>`,
  enableAccount:    IS_AR ? 'تفعيل الحساب'                     : 'Enable account',
  disableAccount:   IS_AR ? 'تعطيل الحساب'                     : 'Disable account',
  userEnabled:   (u) => IS_AR ? `تم تفعيل المستخدم`           : `User enabled`,
  userDisabled:  (u) => IS_AR ? `تم تعطيل المستخدم`           : `User disabled`,
  failedEnable:     IS_AR ? 'تعذّر تفعيل المستخدم'             : 'Failed to enable user',
  failedDisable:    IS_AR ? 'تعذّر تعطيل المستخدم'             : 'Failed to disable user',

  // ── Delete dialog ──
  deleteAccountTitle: IS_AR ? 'حذف حساب المستخدم؟'            : 'Delete user account?',
  deleteAccountBody:(u) => IS_AR ? `<p>سيتم حذف <strong>${u}</strong> نهائيًا.</p><p>لا يمكن التراجع عن هذا الإجراء.</p>` : `<p>This will permanently delete <strong>${u}</strong>.</p><p>This cannot be undone.</p>`,
  deleteConfirm:    IS_AR ? 'حذف المستخدم'                     : 'Delete user',
  userDeleted:      IS_AR ? 'تم حذف المستخدم'                  : 'User deleted',
  failedDeleteUser: IS_AR ? 'تعذّر حذف المستخدم'               : 'Failed to delete user',

  // ── Password generator ──
  pwGenCopied:      IS_AR ? 'تم إنشاء كلمة المرور ونسخها'     : 'Password generated & copied to clipboard',
  pwGenManual:      IS_AR ? 'تم إنشاء كلمة المرور — انسخها يدويًا' : 'Password generated — copy it manually',

  // ── Bulk actions ──
  bulkDisableTitle:(n) => IS_AR ? `تعطيل ${n} مستخدم؟`        : `Disable ${n} users?`,
  bulkDisableBody:(list)=> IS_AR ? `<p>سيتم تعطيل: <strong>${list}</strong></p>` : `<p>This will disable: <strong>${list}</strong></p>`,
  disableAll:       IS_AR ? 'تعطيل الكل'                       : 'Disable all',
  bulkDisabled: (s,f) => IS_AR ? `تم تعطيل ${s} مستخدم${f ? `، فشل ${f}` : ''}` : `Disabled ${s} users${f ? `, ${f} failed` : ''}`,
  bulkEnableTitle:(n) => IS_AR ? `تفعيل ${n} مستخدم؟`         : `Enable ${n} users?`,
  bulkEnableBody:(list)=> IS_AR ? `<p>سيتم تفعيل: <strong>${list}</strong></p>` : `<p>This will enable: <strong>${list}</strong></p>`,
  enableAll:        IS_AR ? 'تفعيل الكل'                       : 'Enable all',
  bulkEnabled:  (s,f) => IS_AR ? `تم تفعيل ${s} مستخدم${f ? `، فشل ${f}` : ''}` : `Enabled ${s} users${f ? `, ${f} failed` : ''}`,

  // ── CSV export ──
  noUsersExport:    IS_AR ? 'لا يوجد مستخدمون للتصدير'         : 'No users to export',
  csvExported:      IS_AR ? 'تم تصدير ملف CSV'                 : 'CSV exported',
};

/* ═══════════════════════════════════════════════════════════════
   API HELPERS
   ═══════════════════════════════════════════════════════════════ */
function apiError(res, data, fallback) {
  const msg = data?.error || data?.message || data?.detail || '';
  const status = res?.status || 0;
  return msg ? `${msg} [HTTP ${status}]` : `${fallback} [HTTP ${status}]`;
}

async function apiPost(url, payload) {
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    return { res, data };
  } catch (err) {
    notify.error(T.networkError, err.message || String(err));
    /* Return a synthetic failed response so callers' res.ok checks still work */
    return { res: { ok: false, status: 0, statusText: 'Network Error' }, data: { error: err.message || String(err) } };
  }
}

/* ═══════════════════════════════════════════════════════════════
   LOAD & RENDER USERS
   ═══════════════════════════════════════════════════════════════ */
async function loadUsers() {
  const tbody = q('umTable')?.querySelector('tbody');
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="9"><div class="um-empty"><span class="um-empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg></span></span><div class="um-empty-title">${T.loading}</div></div></td></tr>`;

  try {
    const res = await fetch('/ops/users/list/');
    const data = await res.json();
    if (!res.ok) { notify.error(apiError(res, data, T.failedLoadUsers)); return; }
    allUsers = Array.isArray(data.items) ? data.items : [];
    updateStats();
    filterTable();
    notify.success(T.loadedUsers(allUsers.length));
  } catch (err) {
    notify.error(T.networkError);
  }
}

function updateStats() {
  const total = allUsers.length;
  const supe = allUsers.filter(u => u.role === 'SUPER_ADMIN').length;
  const gen = allUsers.filter(u => u.role === 'GENERAL_ACADEMIC_ADVISOR').length;
  const adv = allUsers.filter(u => u.role === 'ADVISOR').length;
  const disabled = allUsers.filter(u => !u.is_active).length;

  q('statTotal').textContent = total;
  q('statSuper').textContent = supe;
  q('statGen').textContent = gen;
  q('statAdv').textContent = adv;
  q('statDisabled').textContent = disabled;
  q('umUserCount').textContent = T.nUsers(total);
}

function filterTable() {
  const search = (q('umSearch')?.value || '').trim().toLowerCase();
  const roleFilter = q('umRoleFilter')?.value || '';
  const statusFilter = q('umStatusFilter')?.value || '';

  const filtered = allUsers.filter(u => {
    if (search && !u.username.toLowerCase().includes(search) &&
        !(u.advisor_id || '').toLowerCase().includes(search) &&
        !(u.departments || []).join(',').toLowerCase().includes(search)) return false;
    if (roleFilter && u.role !== roleFilter) return false;
    if (statusFilter === 'active' && !u.is_active) return false;
    if (statusFilter === 'disabled' && u.is_active) return false;
    return true;
  });

  renderTable(filtered);
}
const debouncedFilter = debounce(filterTable, 250);

function renderTable(users) {
  const tbody = q('umTable')?.querySelector('tbody');
  if (!tbody) return;

  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div class="um-empty">
      <span class="um-empty-icon"><span class="i i-xl" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></span></span>
      <div class="um-empty-title">${T.noUsersFound}</div>
      <div class="um-empty-hint">${T.adjustFilters}</div>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = users.map(u => {
    const isSelected = u.username === selectedUsername;
    const isChecked = bulkSelected.has(u.username);
    const roleBadge = u.role === 'SUPER_ADMIN'
      ? `<span class="um-role-badge um-role-super">${T.superAdmin}</span>`
      : u.role === 'GENERAL_ACADEMIC_ADVISOR'
        ? `<span class="um-role-badge um-role-gen">${T.genAdvisor}</span>`
        : `<span class="um-role-badge um-role-adv">${T.advisor}</span>`;

    const activeBadge = u.is_active
      ? `<span class="um-active-badge um-active-on" role="button" tabindex="0" onclick="event.stopPropagation();toggleActive('${u.username}',false)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();toggleActive('${u.username}',false)}" title="${T.clickToDisable}">${T.active}</span>`
      : `<span class="um-active-badge um-active-off" role="button" tabindex="0" onclick="event.stopPropagation();toggleActive('${u.username}',true)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();toggleActive('${u.username}',true)}" title="${T.clickToEnable}">${T.disabled}</span>`;

    const lastLogin = u.last_login
      ? `<span class="um-time">${formatDate(u.last_login)}</span>`
      : `<span class="um-time-never">${T.never}</span>`;

    const created = u.date_joined
      ? `<span class="um-time">${formatDate(u.date_joined)}</span>`
      : '<span class="um-time-never">—</span>';

    return `<tr class="${isSelected ? 'um-selected' : ''}" data-username="${esc(u.username)}" onclick="selectUserFromRow(this)">
      <td onclick="event.stopPropagation()"><input type="checkbox" class="um-check" ${isChecked ? 'checked' : ''} onchange="toggleBulkCheck('${esc(u.username)}', this.checked)"></td>
      <td dir="auto"><span class="fw-semibold">${esc(u.username)}</span></td>
      <td>${roleBadge}</td>
      <td dir="auto">${u.advisor_id ? esc(u.advisor_id) : '<span class="um-empty-cell">—</span>'}</td>
      <td dir="auto">${(u.departments || []).length ? esc((u.departments||[]).join(', ')) : '<span class="um-empty-cell">—</span>'}</td>
      <td>${activeBadge}</td>
      <td>${lastLogin}</td>
      <td>${created}</td>
      <td>
        <div class="um-row-actions">
          <button class="um-row-btn um-btn-edit" title="${T.editRoleScope}" aria-label="${T.edit}" onclick="event.stopPropagation();selectUser('${esc(u.username)}')"><span class="i i-13" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></span></button>
          <button class="um-row-btn um-btn-key" title="${T.resetPassword}" aria-label="${T.resetPassword}" onclick="event.stopPropagation();resetPasswordFor('${esc(u.username)}')"><span class="i i-13" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg></span></button>
          <button class="um-row-btn um-btn-del" title="${T.deleteUser}" aria-label="${T.deleteLabel}" onclick="event.stopPropagation();deleteUserFor('${esc(u.username)}')"><span class="i i-13" aria-hidden="true"><svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></span></button>
        </div>
      </td>
    </tr>`;
  }).join('');

  updateBulkBar();
}

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 86400000) {
    const hrs = Math.floor(diff / 3600000);
    return hrs < 1 ? T.justNow : T.hoursAgo(hrs);
  }
  if (diff < 604800000) {
    const days = Math.floor(diff / 86400000);
    return T.daysAgo(days);
  }
  return d.toLocaleDateString(IS_AR ? 'ar-SA' : 'en-US', { month: 'short', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined });
}

/* ═══════════════════════════════════════════════════════════════
   SELECT / DESELECT USER
   ═══════════════════════════════════════════════════════════════ */
function selectUserFromRow(tr) {
  const username = tr.dataset.username;
  if (selectedUsername === username) { deselectUser(); return; }
  selectUser(username);
}

function selectUser(username) {
  const user = allUsers.find(u => u.username === username);
  if (!user) return;

  selectedUsername = username;
  q('editPanel').classList.remove('d-none');
  q('editingLabel').innerHTML = `<span class="i i-xs" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg></span> ${esc(username)}`;

  // Populate edit fields
  q('eRole').value = user.role;
  q('eAdvisorId').value = user.advisor_id || '';
  q('eDepartments').value = (user.departments || []).join(', ');
  updateEditScopeFields();

  // Highlight row
  document.querySelectorAll('#umTable tbody tr').forEach(r => {
    r.classList.toggle('um-selected', r.dataset.username === username);
  });

  // Scroll edit panel into view smoothly
  q('editPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function deselectUser() {
  selectedUsername = null;
  q('editPanel').classList.add('d-none');
  document.querySelectorAll('#umTable tbody tr.um-selected').forEach(r => r.classList.remove('um-selected'));
}

/* ═══════════════════════════════════════════════════════════════
   ROLE-BASED SCOPE FIELDS
   ═══════════════════════════════════════════════════════════════ */
function updateScopeFields(roleId, advisorWrapId, deptWrapId) {
  const role = q(roleId)?.value || '';
  const advisorWrap = q(advisorWrapId);
  const deptWrap = q(deptWrapId);
  if (!advisorWrap || !deptWrap) return;

  if (role === 'SUPER_ADMIN') {
    advisorWrap.classList.add('hidden');
    deptWrap.classList.add('hidden');
  } else if (role === 'GENERAL_ACADEMIC_ADVISOR') {
    advisorWrap.classList.add('hidden');
    deptWrap.classList.remove('hidden');
  } else {
    advisorWrap.classList.remove('hidden');
    deptWrap.classList.add('hidden');
  }
}
function updateCreateScopeFields() { updateScopeFields('cRole', 'cAdvisorWrap', 'cDeptWrap'); }
function updateEditScopeFields() { updateScopeFields('eRole', 'eAdvisorWrap', 'eDeptWrap'); }

/* ═══════════════════════════════════════════════════════════════
   CREATE USER
   ═══════════════════════════════════════════════════════════════ */
async function createUser() {
  const username = (q('cUsername')?.value || '').trim();
  const password = (q('cPassword')?.value || '').trim();
  const role = (q('cRole')?.value || '').trim();
  const advisor_id = (q('cAdvisorId')?.value || '').trim();
  const departments = (q('cDepartments')?.value || '').trim();

  if (!username || !password) { notify.warning(T.usernamePasswordReq); return; }

  const { res, data } = await apiPost('/ops/users/create/', { username, password, role, advisor_id, departments });
  if (res.ok) {
    notify.success(T.userCreated, username);
    q('cUsername').value = '';
    q('cPassword').value = '';
    q('cAdvisorId').value = '';
    q('cDepartments').value = '';
    loadUsers();
  } else {
    notify.error(apiError(res, data, T.failedCreateUser));
  }
}

/* ═══════════════════════════════════════════════════════════════
   UPDATE ROLE / SCOPE
   ═══════════════════════════════════════════════════════════════ */
async function updateRole() {
  if (!selectedUsername) { notify.warning(T.noUserSelected); return; }
  const { res, data } = await apiPost('/ops/users/update-role/', {
    username: selectedUsername,
    role: q('eRole')?.value || '',
    advisor_id: (q('eAdvisorId')?.value || '').trim(),
    departments: (q('eDepartments')?.value || '').trim(),
  });
  if (res.ok) {
    notify.success(T.roleUpdated, selectedUsername);
    loadUsers();
  } else {
    notify.error(apiError(res, data, T.failedUpdateRole));
  }
}

/* ═══════════════════════════════════════════════════════════════
   RESET PASSWORD
   ═══════════════════════════════════════════════════════════════ */
async function resetPassword() {
  if (!selectedUsername) return;
  await resetPasswordFor(selectedUsername);
}

async function resetPasswordFor(username) {
  const newPassword = await dlg.prompt({
    title: T.resetPwTitle,
    body: T.resetPwBody(username),
    label: T.newPassword,
    placeholder: T.enterNewPw,
    kind: 'warning',
    confirmText: T.setPassword,
  });
  if (!newPassword) return;

  const ok = await dlg.confirm({
    title: T.confirmResetTitle,
    body: T.confirmResetBody(username),
    typed: 'RESET',
    confirmText: T.confirmReset,
    kind: 'warning',
  });
  if (!ok) return;

  const { res, data } = await apiPost('/ops/users/set-password/', { username, new_password: newPassword });
  if (res.ok) notify.success(T.passwordReset, username);
  else notify.error(apiError(res, data, T.failedResetPw));
}

/* ═══════════════════════════════════════════════════════════════
   TOGGLE ACTIVE (inline badge click)
   ═══════════════════════════════════════════════════════════════ */
async function toggleActive(username, enable) {
  const ok = await dlg.confirm({
    title: enable ? T.enableAccountTitle(username) : T.disableAccountTitle(username),
    body: enable ? T.restoreAccess(username) : T.preventLogin(username),
    typed: enable ? undefined : 'DISABLE',
    confirmText: enable ? T.enableAccount : T.disableAccount,
    kind: enable ? 'info' : 'warning',
  });
  if (!ok) return;

  const { res, data } = await apiPost('/ops/users/set-active/', { username, is_active: enable });
  if (res.ok) {
    notify.success(enable ? T.userEnabled(username) : T.userDisabled(username), username);
    loadUsers();
  } else {
    notify.error(apiError(res, data, enable ? T.failedEnable : T.failedDisable));
  }
}

/* ═══════════════════════════════════════════════════════════════
   DELETE USER
   ═══════════════════════════════════════════════════════════════ */
async function deleteUser() {
  if (!selectedUsername) return;
  await deleteUserFor(selectedUsername);
}

async function deleteUserFor(username) {
  const ok = await dlg.confirm({
    title: T.deleteAccountTitle,
    body: T.deleteAccountBody(username),
    typed: username,
    confirmText: T.deleteConfirm,
    kind: 'danger',
  });
  if (!ok) return;

  const { res, data } = await apiPost('/ops/users/delete/', { username });
  if (res.ok) {
    notify.success(T.userDeleted, username);
    if (selectedUsername === username) deselectUser();
    loadUsers();
  } else {
    notify.error(apiError(res, data, T.failedDeleteUser));
  }
}

/* ═══════════════════════════════════════════════════════════════
   GENERATE PASSWORD
   ═══════════════════════════════════════════════════════════════ */
function generatePassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789!@#$%&*';
  const arr = new Uint8Array(16);
  crypto.getRandomValues(arr);
  const pw = Array.from(arr).map(b => chars[b % chars.length]).join('');
  q('cPassword').value = pw;
  q('cPassword').type = 'text';

  // Copy to clipboard
  navigator.clipboard.writeText(pw).then(() => {
    notify.info(T.pwGenCopied);
  }).catch(() => {
    notify.info(T.pwGenManual);
  });
}

/* ═══════════════════════════════════════════════════════════════
   BULK ACTIONS
   ═══════════════════════════════════════════════════════════════ */
function toggleBulkCheck(username, checked) {
  if (checked) bulkSelected.add(username);
  else bulkSelected.delete(username);
  updateBulkBar();
}

function toggleAllChecks(master) {
  const checkboxes = document.querySelectorAll('#umTable tbody .um-check');
  checkboxes.forEach(cb => {
    const tr = cb.closest('tr');
    const uname = tr?.dataset?.username;
    if (uname) {
      cb.checked = master.checked;
      if (master.checked) bulkSelected.add(uname);
      else bulkSelected.delete(uname);
    }
  });
  updateBulkBar();
}

function clearBulk() {
  bulkSelected.clear();
  document.querySelectorAll('#umTable tbody .um-check').forEach(cb => cb.checked = false);
  q('umCheckAll').checked = false;
  q('umCheckAll').indeterminate = false;
  updateBulkBar();
}

function updateBulkBar() {
  const bar = q('bulkBar');
  const n = bulkSelected.size;
  q('bulkCount').textContent = IS_AR ? `${n} محدد` : `${n} selected`;
  bar.classList.toggle('visible', n > 0);
  /* Keep header "select all" checkbox in sync */
  const visibleCbs = document.querySelectorAll('#umTable tbody .um-check');
  const allChecked = visibleCbs.length > 0 && Array.from(visibleCbs).every(cb => cb.checked);
  q('umCheckAll').checked = allChecked;
  q('umCheckAll').indeterminate = !allChecked && n > 0;
}

async function bulkDisable() {
  if (!bulkSelected.size) return;
  const names = Array.from(bulkSelected);
  const ok = await dlg.confirm({
    title: T.bulkDisableTitle(names.length),
    body: T.bulkDisableBody(names.join(', ')),
    typed: 'DISABLE',
    confirmText: T.disableAll,
    kind: 'warning',
  });
  if (!ok) return;

  let success = 0, fail = 0;
  for (const username of names) {
    const { res } = await apiPost('/ops/users/set-active/', { username, is_active: false });
    if (res.ok) success++; else fail++;
  }
  notify.success(T.bulkDisabled(success, fail));
  clearBulk();
  loadUsers();
}

async function bulkEnable() {
  if (!bulkSelected.size) return;
  const names = Array.from(bulkSelected);
  const ok = await dlg.confirm({
    title: T.bulkEnableTitle(names.length),
    body: T.bulkEnableBody(names.join(', ')),
    confirmText: T.enableAll,
    kind: 'info',
  });
  if (!ok) return;

  let success = 0, fail = 0;
  for (const username of names) {
    const { res } = await apiPost('/ops/users/set-active/', { username, is_active: true });
    if (res.ok) success++; else fail++;
  }
  notify.success(T.bulkEnabled(success, fail));
  clearBulk();
  loadUsers();
}

/* ═══════════════════════════════════════════════════════════════
   EXPORT CSV
   ═══════════════════════════════════════════════════════════════ */
function exportCSV() {
  if (!allUsers.length) { notify.warning(T.noUsersExport); return; }
  const header = 'username,role,advisor_id,departments,is_active,last_login,date_joined';
  const rows = allUsers.map(u =>
    [u.username, u.role, u.advisor_id || '', (u.departments||[]).join(';'), u.is_active, u.last_login||'', u.date_joined||''].join(',')
  );
  const csv = '\ufeff' + header + '\n' + rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `users_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  notify.success(T.csvExported, T.nUsers(allUsers.length));
}

/* ═══════════════════════════════════════════════════════════════
   CREATE PANEL TOGGLE
   ═══════════════════════════════════════════════════════════════ */
function toggleCreatePanel() {
  const fields = q('createFields');
  const icon = q('createToggleIcon');
  if (fields.style.display === 'none') {
    fields.style.display = 'block';
    icon.style.transform = '';
  } else {
    fields.style.display = 'none';
    icon.style.transform = 'rotate(-90deg)';
  }
}

/* ═══════════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════════ */
updateCreateScopeFields();
wireSortableTable('umTable');
loadUsers();

/* Mark current sidebar link as active */
document.querySelectorAll('.sidebar .nav-link').forEach(link => {
  if (link.getAttribute('href') === '/user-management/') link.classList.add('active');
});
