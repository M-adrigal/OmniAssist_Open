/* 11-users-files.js
 * 用户与文件库：当前用户/权限/改密、用户管理、文件浏览/预览/删除
 *
 * 由 static/app.js 拆分而来（原第 3811-4253 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
function updateThinkingModeUI() {
  // 思考强度住在模型下拉里：三档分段选择，和「用哪个模型」是同一类设置
  const modes = ['off', 'low', 'high'];
  const mode = modes.includes(state.thinkingMode) ? state.thinkingMode : 'low';
  state.thinkingMode = mode;
  document.querySelectorAll('#mm-think-seg button').forEach(b => {
    b.classList.toggle('active', b.dataset.think === mode);
  });
  localStorage.setItem('thinkingMode', mode);
}





// ===== 用户信息 =====
async function loadCurrentUser() {
  try {
    state.currentUser = await API.get('/api/auth/me');
    const permData = await API.get('/api/auth/permissions');
    state.permissions = permData.permissions || {};
    updateAdminUI();
  } catch (e) {
    state.currentUser = null;
    state.permissions = {};
  }
}

function hasPermission(resource, action) {
  const resPerms = state.permissions[resource];
  return resPerms && resPerms.includes(action);
}

function updateAdminUI() {
  const canManageUsers = hasPermission('users', 'read');
  const canManageGlobalConfig = hasPermission('model_config_global', 'read');
  const canManageSearch = hasPermission('search_config', 'read');
  const showAdmin = canManageUsers || canManageGlobalConfig || canManageSearch;
  $$('.drawer-item-admin').forEach(el => {
    el.classList.toggle('hidden', !showAdmin);
  });
}

// ===== 修改密码 =====
// 与后端 validate_password_strength 保持一致的强度规则（首次登录强制改密 / 平台改密均复用同一函数）：
// 至少 8 位，且需包含大写字母、小写字母、数字、特殊符号中至少两类。
function validatePasswordStrength(pw) {
  if (!pw || pw.length < 8) return { ok: false, msg: '密码至少 8 位' };
  let categories = 0;
  if (/[a-z]/.test(pw)) categories++;
  if (/[A-Z]/.test(pw)) categories++;
  if (/\d/.test(pw)) categories++;
  if (/[^A-Za-z0-9]/.test(pw)) categories++;
  if (categories < 2) return { ok: false, msg: '需包含大写字母、小写字母、数字、特殊符号中至少两类' };
  return { ok: true, msg: '' };
}

function openChangePassword() {
  $('#cp-old-password').value = '';
  $('#cp-new-password').value = '';
  $('#cp-confirm-password').value = '';
  openModal('modal-change-password');
}

async function savePassword() {
  const oldPassword = $('#cp-old-password').value;
  const newPassword = $('#cp-new-password').value;
  const confirmPassword = $('#cp-confirm-password').value;

  if (!oldPassword) { showToast('请输入原密码', 'error'); return; }
  if (!newPassword) { showToast('请输入新密码', 'error'); return; }
  const strength = validatePasswordStrength(newPassword);
  if (!strength.ok) { showToast('新密码强度不足：' + strength.msg + '（至少 8 位，且含大/小写字母、数字、特殊符号中至少两类）', 'error'); return; }
  if (newPassword !== confirmPassword) { showToast('两次输入的新密码不一致', 'error'); return; }

  try {
    await API.put('/api/auth/password', {
      old_password: oldPassword,
      new_password: newPassword,
      confirm_password: confirmPassword,
    });
    showToast('密码修改成功', 'success');
    closeModal('modal-change-password');
  } catch (e) {
    showToast('密码修改失败: ' + e.message, 'error');
  }
}

// ===== 用户管理 =====
let _allUsers = [];

async function loadUsers() {
  const tbody = $('#user-table-body');
  tbody.innerHTML = '<tr><td colspan="6" class="loading-text">加载中...</td></tr>';
  try {
    _allUsers = await API.get('/api/users');
    renderUserTable(_allUsers);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="6" class="loading-text">加载失败: ' + escapeHtml(e.message) + '</td></tr>';
  }
}

function renderUserTable(users) {
  const tbody = $('#user-table-body');
  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="loading-text">暂无用户</td></tr>';
    return;
  }
  tbody.innerHTML = users.map(u => {
    const isAdmin = u.user_type === 'admin';
    // 创建时间：列表只到分钟，完整秒级值放 title 里
    const createdFull = u.created_at ? String(u.created_at) : '';
    const created = createdFull ? createdFull.slice(0, 16) : '-';
    const deleteBtn = isAdmin
      ? '<button class="icon-act" disabled title="管理员不可删除">—</button>'
      : `<button class="icon-act danger" data-action="delete-user" data-id="${u.public_id}" data-username="${escapeHtml(u.username)}" title="删除">${ICON.trash}</button>`;
    return `
      <tr>
        <td title="${escapeHtml(u.public_id)}">${u.public_id}</td>
        <td>${escapeHtml(u.username)}</td>
        <td><span class="user-type-badge ${u.user_type}">${isAdmin ? '管理员' : '普通用户'}</span></td>
        <td title="${escapeHtml(u.description || '')}">${escapeHtml(u.description || '-')}</td>
        <td class="user-created" title="${escapeHtml(createdFull)}">${escapeHtml(created)}</td>
        <td>
          <div class="actions">
            <button class="icon-act" data-action="edit-user" data-id="${u.public_id}" data-username="${escapeHtml(u.username)}" data-type="${u.user_type}" data-desc="${escapeHtml(u.description || '')}" title="编辑">${ICON.pen}</button>
            ${deleteBtn}
          </div>
        </td>
      </tr>
    `;
  }).join('');

  tbody.querySelectorAll('[data-action="edit-user"]').forEach(btn => {
    btn.addEventListener('click', () => openEditUser(btn.dataset));
  });
  tbody.querySelectorAll('[data-action="delete-user"]').forEach(btn => {
    btn.addEventListener('click', () => openDeleteConfirm(btn.dataset));
  });
}

function filterUsers() {
  const keyword = $('#user-search').value.trim().toLowerCase();
  if (!keyword) {
    renderUserTable(_allUsers);
    return;
  }
  const filtered = _allUsers.filter(u => u.username.toLowerCase().includes(keyword));
  renderUserTable(filtered);
}

function openAddUser() {
  $('#user-form-title').textContent = '新增用户';
  $('#user-form-id').value = '';
  $('#user-form-username').value = '';
  $('#user-form-username').readOnly = false;
  $('#user-form-username-error').textContent = '';
  $('#user-form-username').oninput = checkUserUsernameUnique;
  $('#user-form-username').onblur = checkUserUsernameUnique;
  $('#user-form-password').value = '';
  $('#user-form-password').type = 'password';
  $('#btn-toggle-user-password').textContent = '👁';
  $('#user-form-type').value = 'user';
  $('#user-form-desc').value = '';
  openModal('modal-user-form');
}

function openEditUser(dataset) {
  $('#user-form-title').textContent = '编辑用户';
  $('#user-form-id').value = dataset.public_id;
  $('#user-form-username').value = dataset.username;
  $('#user-form-username').readOnly = true;
  $('#user-form-username-error').textContent = '';
  $('#user-form-password').value = '';
  $('#user-form-password').type = 'password';
  $('#btn-toggle-user-password').textContent = '👁';
  $('#user-form-type').value = dataset.type;
  $('#user-form-desc').value = dataset.desc;
  openModal('modal-user-form');
}

// 用户名重复实时校验（基于已加载的 _allUsers 缓存，后端 UNIQUE 约束兜底）
function checkUserUsernameUnique() {
  const id = $('#user-form-id').value;
  const username = $('#user-form-username').value.trim().toLowerCase();
  const errEl = $('#user-form-username-error');
  if (!username) { errEl.textContent = ''; return true; }
  const dup = _allUsers.some(u => u.username.toLowerCase() === username && u.public_id !== id);
  if (dup) {
    errEl.textContent = '该用户名已被使用，请更换';
    return false;
  }
  errEl.textContent = '';
  return true;
}

async function submitUserForm() {
  const id = $('#user-form-id').value;
  const username = $('#user-form-username').value.trim();
  const password = $('#user-form-password').value;
  const userType = $('#user-form-type').value;
  const desc = $('#user-form-desc').value.trim();

  if (!id) {
    if (!username) { showToast('请输入用户名', 'error'); return; }
    if (!checkUserUsernameUnique()) { showToast('该用户名已被使用，请更换', 'error'); return; }
    if (!password) { showToast('请输入密码', 'error'); return; }
    const strength = validatePasswordStrength(password);
    if (!strength.ok) {
      showToast('密码强度不足：' + strength.msg + '（至少 8 位，且含大/小写字母、数字、特殊符号中至少两类）', 'error');
      return;
    }

    try {
      await API.post('/api/users', {
        username, password, user_type: userType, description: desc,
      });
      showToast('用户创建成功', 'success');
      closeModal('modal-user-form');
      loadUsers();
    } catch (e) {
      showToast('创建失败: ' + e.message, 'error');
    }
  } else {
    const body = { user_type: userType, description: desc };
    if (password) {
      const strength = validatePasswordStrength(password);
      if (!strength.ok) {
        showToast('密码强度不足：' + strength.msg + '（至少 8 位，且含大/小写字母、数字、特殊符号中至少两类）', 'error');
        return;
      }
      body.password = password;
    }

    try {
      await API.put('/api/users/' + id, body);
      showToast('用户更新成功', 'success');
      closeModal('modal-user-form');
      loadUsers();
    } catch (e) {
      showToast('更新失败: ' + e.message, 'error');
    }
  }
}

let _deleteUserId = null;

function openDeleteConfirm(dataset) {
  _deleteUserId = dataset.public_id;
  $('#confirm-delete-username').textContent = dataset.username;
  $('#confirm-keep-files').checked = false;
  openModal('modal-confirm-delete');
}

async function confirmDeleteUser() {
  if (!_deleteUserId) return;
  try {
    const keepFiles = $('#confirm-keep-files').checked;
    await API.del(`/api/users/${_deleteUserId}?keep_files=${keepFiles}`);
    showToast('用户已删除', 'success');
    closeModal('modal-confirm-delete');
    _deleteUserId = null;
    loadUsers();
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// 设置抽屉
// （技能管理页面已移除）

// 设置抽屉菜单点击

// ===== 文件列表 =====
async function loadFiles() {
  const container = $('#files-container');
  container.innerHTML = '<p class="loading-text">加载中...</p>';
  try {
    const files = await API.get('/api/files');
    if (files.length === 0) {
      container.innerHTML = '<p class="loading-text">暂无文件</p>';
      return;
    }
    container.innerHTML = files.map(f => renderFileFolder(f)).join('');

    container.querySelectorAll('.file-folder-header').forEach(header => {
      header.addEventListener('click', (e) => {
        e.stopPropagation();
        const body = header.nextElementSibling;
        const arrow = header.querySelector('.arrow');
        body.classList.toggle('hidden');
        arrow.classList.toggle('open');
      });
    });

    container.querySelectorAll('[data-action="download"]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        downloadFile(btn.dataset.path);
      });
    });

    container.querySelectorAll('[data-action="delete-file"]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteFile(btn.dataset.path);
      });
    });
  } catch (e) {
    container.innerHTML = `<p class="loading-text">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function renderFileItem(f) {
  const ext = (f.name || '').split('.').pop().toLowerCase();
  const icon = getFileIcon(ext);
  const previewUrl = `/api/files/download?path=${encodeURIComponent(f.path)}&inline=true`;
  return `
    <div class="file-item">
      <a href="${previewUrl}" target="_blank" class="file-name" title="点击在新标签页中预览">${icon} ${escapeHtml(f.name)}</a>
      <span class="file-size">${formatSize(f.size)}</span>
      <span class="file-actions">
        <button class="btn-sm" data-action="download" data-path="${escapeHtml(f.path)}">下载</button>
        <button class="btn-sm danger" data-action="delete-file" data-path="${escapeHtml(f.path)}">删除</button>
      </span>
    </div>
  `;
}

function renderFileFolder(folder) {
  const children = folder.children || [];
  if (children.length === 0) return '';

  const isTypeFolder = children[0] && children[0].type === 'directory';

  if (isTypeFolder) {
    const subFolders = children.map(sub => {
      const fileItems = (sub.children || []).map(f => renderFileItem(f)).join('');
      return `
        <div class="file-folder file-folder-nested">
          <div class="file-folder-header">
            <span class="arrow">▶</span>
            <span>📂 ${escapeHtml(sub.name)}</span>
            <span style="margin-left:auto;color:var(--text-muted);font-size:12px">${(sub.children || []).length} 个文件</span>
          </div>
          <div class="file-folder-body hidden">${fileItems}</div>
        </div>
      `;
    }).join('');

    return `
      <div class="file-folder">
        <div class="file-folder-header file-folder-header-user">
          <span class="arrow">▶</span>
          <span>👤 ${escapeHtml(folder.name)}</span>
          <span style="margin-left:auto;color:var(--text-muted);font-size:12px">${children.length} 个分类</span>
        </div>
        <div class="file-folder-body hidden">${subFolders}</div>
      </div>
    `;
  }

  const fileItems = children.map(f => renderFileItem(f)).join('');
  return `
    <div class="file-folder">
      <div class="file-folder-header">
        <span class="arrow">▶</span>
        <span>📁 ${escapeHtml(folder.name)}</span>
        <span style="margin-left:auto;color:var(--text-muted);font-size:12px">${children.length} 个文件</span>
      </div>
      <div class="file-folder-body hidden">${fileItems}</div>
    </div>
  `;
}

function getFileIcon(ext) {
  const icons = {
    pdf: '📕', doc: '📘', docx: '📘', xls: '📗', xlsx: '📗',
    ppt: '📙', pptx: '📙', txt: '📄', md: '📝', csv: '📊',
    json: '📋', xml: '📋', html: '🌐', css: '🎨', js: '📜',
    py: '🐍', log: '📃', yaml: '📋', yml: '📋', png: '🖼️',
    jpg: '🖼️', jpeg: '🖼️', gif: '🖼️', svg: '🖼️', webp: '🖼️',
    zip: '📦', gz: '📦', tar: '📦',
  };
  return icons[ext] || '📄';
}

function isPreviewable(ext) {
  const previewable = ['txt', 'md', 'csv', 'json', 'xml', 'html', 'css', 'js', 'py', 'log', 'yaml', 'yml', 'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'pdf'];
  return previewable.includes(ext);
}

function downloadFile(path) {
  window.open(`/api/files/download?path=${encodeURIComponent(path)}`, '_blank');
}

async function previewFile(path, filename) {
  const modal = $('#modal-file-preview');
  const title = $('#preview-title');
  const content = $('#preview-content');

  title.textContent = filename || '文件预览';
  content.innerHTML = '<p class="loading-text">加载中...</p>';
  modal.classList.remove('hidden');

  try {
    const data = await API.get(`/api/files/preview?path=${encodeURIComponent(path)}`);

    if (data.type === 'text') {
      const ext = (filename || '').split('.').pop().toLowerCase();
      const langMap = { js: 'javascript', ts: 'typescript', py: 'python', md: 'markdown', json: 'json', xml: 'xml', html: 'html', css: 'css', yaml: 'yaml', yml: 'yaml', csv: 'csv' };
      const lang = langMap[ext] || '';
      content.innerHTML = `<pre class="preview-code"><code class="${lang ? 'language-' + lang : ''}">${escapeHtml(data.content)}</code></pre>`;
    } else if (data.type === 'image') {
      content.innerHTML = `<div class="preview-image-wrap"><img src="/api/files/download?path=${encodeURIComponent(data.path)}" alt="${escapeHtml(data.filename)}" class="preview-image"></div>`;
    } else if (data.type === 'pdf') {
      content.innerHTML = `<iframe src="/api/files/download?path=${encodeURIComponent(data.path)}" class="preview-pdf"></iframe>`;
    } else {
      content.innerHTML = `<div class="preview-unsupported"><p>📄 此文件类型不支持预览</p><p style="margin-top:8px;font-size:13px;color:var(--text-muted);">${escapeHtml(data.filename)}</p><button class="btn-primary" style="margin-top:12px;" data-action="download" data-path="${escapeHtml(path)}">下载文件</button></div>`;
      const dlBtn = content.querySelector('[data-action="download"]');
      if (dlBtn) {
        dlBtn.addEventListener('click', () => downloadFile(dlBtn.dataset.path));
      }
    }
  } catch (e) {
    content.innerHTML = `<p class="loading-text">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

async function deleteFile(path) {
  const confirmed = await showConfirmDialog('删除文件', `确定要删除 ${path} 吗？`, '删除');
  if (!confirmed) return;
  try {
    await API.del(`/api/files?path=${encodeURIComponent(path)}`);
    showToast('文件已删除', 'success');
    await loadFiles();
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ===== 主题切换 =====
// 图标由 #theme-toggle 的 data-mode 决定（light=月亮 / dark=太阳 / auto=半圆），不再用 emoji
