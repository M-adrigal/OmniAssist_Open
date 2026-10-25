/* 08-picker-ui.js
 * 通用 UI：文件选择器、命令建议、抽屉、模态框、信任模式 UI
 *
 * 由 static/app.js 拆分而来（原第 2780-3014 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
let pickerAllFiles = [];
let pickerSelected = {};

async function openFilePicker() {
  const pickerSearch = $('#picker-search');
  if (pickerSearch) pickerSearch.value = '';
  pickerSelected = {};
  openModal('modal-file-picker');
  await loadPickerFiles('');
}

async function loadPickerFiles(search) {
  const list = $('#picker-list');
  list.innerHTML = '<div class="loading-text" style="padding:40px;text-align:center;">加载中...</div>';

  try {
    const data = await API.get(`/api/files/all-uploads?search=${encodeURIComponent(search)}`);
    pickerAllFiles = data.files || [];
    renderPickerFiles();
  } catch (e) {
    list.innerHTML = `<div class="loading-text" style="padding:40px;text-align:center;color:var(--danger);">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderPickerFiles() {
  const list = $('#picker-list');
  const iconMap = {
    text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C',
  };

  if (pickerAllFiles.length === 0) {
    list.innerHTML = '<div class="loading-text" style="padding:40px;text-align:center;">暂无上传文件</div>';
  } else {
    list.innerHTML = pickerAllFiles.map(f => {
      const sizeStr = f.size < 1024 ? `${f.size}B` : f.size < 1024 * 1024 ? `${(f.size / 1024).toFixed(1)}KB` : `${(f.size / (1024 * 1024)).toFixed(1)}MB`;
      const icon = iconMap[f.type] || 'F';
      const iconCls = iconMap[f.type] ? f.type : 'text';
      const checked = pickerSelected[f.path] ? ' checked' : '';
      return `
        <div class="picker-item" data-path="${escapeHtml(f.path)}">
          <input type="checkbox"${checked} data-path="${escapeHtml(f.path)}">
          <span class="picker-file-icon ${iconCls}">${icon}</span>
          <div class="picker-file-info">
            <div class="picker-file-name">${escapeHtml(f.filename)}</div>
            <div class="picker-file-meta">${sizeStr} · ${escapeHtml(f.session_id || '')} · ${escapeHtml(f.upload_time || '')}</div>
          </div>
        </div>`;
    }).join('');
  }

  updatePickerCount();
  bindPickerEvents();
}

function bindPickerEvents() {
  $$('#picker-list .picker-item input[type="checkbox"]').forEach(cb => {
    cb.removeEventListener('change', handlePickerCheck);
    cb.addEventListener('change', handlePickerCheck);
  });
}

function handlePickerCheck(e) {
  const path = e.target.dataset.path;
  if (e.target.checked) {
    pickerSelected[path] = true;
  } else {
    delete pickerSelected[path];
  }
  updatePickerCount();
}

function updatePickerCount() {
  const count = Object.keys(pickerSelected).length;
  $('#picker-count').textContent = `已选 ${count} 个文件`;
}

async function confirmPickerSelection() {
  const paths = Object.keys(pickerSelected);
  if (paths.length === 0) {
    showToast('请先选择文件', 'info');
    return;
  }

  if (!state.currentSessionId) {
    try {
      const s = await API.post('/api/sessions', { title: '文件对话' });
      state.currentSessionId = s.id;
      refreshChatModelBadge();
      await loadSessions();
    } catch (e) {
      showToast('创建会话失败: ' + e.message, 'error');
      return;
    }
  }

  try {
    const data = await API.post('/api/files/reference-files', {
      paths: paths,
      session_id: state.currentSessionId,
    });

    if (data.errors && data.errors.length > 0) {
      data.errors.forEach(e => showToast(e, 'warning'));
    }
    if (data.referenced && data.referenced.length > 0) {
      state.attachedFiles = state.attachedFiles.concat(data.referenced);
      showToast(`已引用 ${data.referenced.length} 个文件`, 'success');
    }
    renderFileChips();
    closeModal('modal-file-picker');
  } catch (e) {
    showToast('引用失败: ' + e.message, 'error');
  }
}

// ===== 命令自动完成 =====
function handleCommandInput(value) {
  if (value.startsWith('/')) {
    state.commandMode = true;
    state.commandFilter = value;
    state.selectedCommandIdx = 0;
    showCommandSuggestions();
  } else {
    state.commandMode = false;
    hideCommandSuggestions();
  }
}

function showCommandSuggestions() {
  const container = $('#command-suggestions');
  const filter = state.commandFilter.toLowerCase();
  const filtered = COMMANDS.filter(c => c.command.toLowerCase().includes(filter));

  if (filtered.length === 0) {
    container.classList.add('hidden');
    return;
  }

  container.innerHTML = filtered.map((c, i) => `
    <div class="command-item${i === state.selectedCommandIdx ? ' selected' : ''}" data-idx="${i}">
      <span class="cmd-name">${escapeHtml(c.command)}</span>
      <span class="cmd-desc">${escapeHtml(c.description)}</span>
      <span class="cmd-category">${escapeHtml(c.category)}</span>
    </div>
  `).join('');

  container.classList.remove('hidden');

  container.querySelectorAll('.command-item').forEach(el => {
    el.addEventListener('click', () => {
      const cmd = filtered[parseInt(el.dataset.idx)];
      selectCommand(cmd);
    });
  });
}

function hideCommandSuggestions() {
  $('#command-suggestions').classList.add('hidden');
  state.selectedCommandIdx = -1;
}

function selectCommand(cmd) {
  $('#chat-input').value = cmd.command + ' ';
  hideCommandSuggestions();
  $('#chat-input').focus();
}

function navigateCommand(direction) {
  if (!state.commandMode) return;
  const container = $('#command-suggestions');
  const items = container.querySelectorAll('.command-item');
  if (items.length === 0) return;

  state.selectedCommandIdx += direction;
  if (state.selectedCommandIdx < 0) state.selectedCommandIdx = items.length - 1;
  if (state.selectedCommandIdx >= items.length) state.selectedCommandIdx = 0;

  items.forEach((el, i) => {
    el.classList.toggle('selected', i === state.selectedCommandIdx);
  });

  const selected = items[state.selectedCommandIdx];
  if (selected) {
    selected.scrollIntoView({ block: 'nearest' });
  }
}

// ===== 设置抽屉 =====
function toggleDrawer() {
  const drawer = $('#settings-drawer');
  drawer.classList.toggle('hidden');
}

// ===== 模态框 =====
function openModal(id) {
  $(`#${id}`).classList.remove('hidden');
}

function closeModal(id) {
  $(`#${id}`).classList.add('hidden');
}

function closeAllModals() {
  $$('.modal').forEach(m => m.classList.add('hidden'));
}

// ===== 会话权限模式（请求批准 / 完全访问权限）=====
// 定义在顶层作用域：switchSession / sendMessage 等顶层函数需要调用。
function updateTrustUI(mode) {
  const btnPerm = $('#btn-perm');
  if (!btnPerm) return;
  const m = (mode === 'full') ? 'full' : 'request';
  btnPerm.dataset.mode = m;

  // active 类复用 btn-web-search 的高亮样式
  if (m === 'full') btnPerm.classList.add('active');
  else btnPerm.classList.remove('active');

  const permLabel = $('#perm-label');
  if (permLabel) permLabel.textContent = (m === 'full') ? '完全访问' : '请求批准';
}

// 拉取当前会话的权限模式并刷新 UI（切换/新建会话时调用）
async function refreshTrustState() {
  const sid = state.currentSessionId;
  if (!sid) { updateTrustUI('request'); return; }
  try {
    const data = await API.get(`/api/chat/${sid}/trust`);
    updateTrustUI(data && data.mode);
  } catch (e) {
    updateTrustUI('request');
  }
}

// ===== 模型配置 =====
