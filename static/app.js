const API = {
  async get(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    return res.json();
  },
  async post(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    return res.json();
  },
  async put(url, body) {
    const res = await fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    return res.json();
  },
  async del(url) {
    const res = await fetch(url, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    return res.json();
  },
};

const COMMANDS = [
  { command: '/help', description: '显示帮助信息', category: '通用' },
  { command: '/reset', description: '重置对话上下文', category: '对话' },
  { command: '/tool list', description: '查看所有已安装的工具', category: '工具' },
  { command: '/tool add', description: '通过自然语言新增工具', category: '工具' },
  { command: '/tool update', description: '通过自然语言修改已有工具', category: '工具' },
  { command: '/tool delete', description: '删除指定工具', category: '工具' },
  { command: '/model set', description: '配置模型参数', category: '模型' },
  { command: '/model show', description: '查看当前模型配置', category: '模型' },
  { command: '/model update', description: '修改单个配置项', category: '模型' },
  { command: '/agent thought on', description: '开启思考过程显示', category: 'Agent' },
  { command: '/agent thought off', description: '关闭思考过程显示', category: 'Agent' },
];

let state = {
  sessions: [],
  currentSessionId: null,
  isStreaming: false,
  commandMode: false,
  commandFilter: '',
  selectedCommandIdx: -1,
  currentUser: null,
  permissions: {},
  webSearch: 'off',
  showThought: false,
};

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function showToast(msg, type = 'info') {
  const toast = $('#toast');
  toast.textContent = msg;
  toast.className = `toast ${type}`;
  clearTimeout(toast._timeout);
  toast._timeout = setTimeout(() => toast.classList.add('hidden'), 3000);
}

function showConfirmDialog(title, message, confirmText = '确认', danger = true) {
  return new Promise((resolve) => {
    const modal = $('#modal-generic-confirm');
    $('#generic-confirm-title').textContent = title;
    $('#generic-confirm-text').textContent = message;
    const confirmBtn = $('#btn-generic-confirm');
    confirmBtn.textContent = confirmText;
    confirmBtn.className = danger ? 'btn-danger' : 'btn-primary';

    const cleanup = () => {
      modal.classList.add('hidden');
      confirmBtn.removeEventListener('click', onConfirm);
    };

    const onConfirm = () => {
      cleanup();
      resolve(true);
    };

    const onCancel = () => {
      cleanup();
      resolve(false);
    };

    confirmBtn.addEventListener('click', onConfirm);

    modal.querySelectorAll('.modal-close').forEach(btn => {
      btn.addEventListener('click', onCancel);
    });
    modal.querySelector('.modal-overlay').addEventListener('click', onCancel);

    modal.classList.remove('hidden');
  });
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatTime(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

// ===== 会话管理 =====
async function loadSessions() {
  try {
    state.sessions = await API.get('/api/sessions');
    renderSessions();
  } catch (e) {
    console.error('加载会话失败:', e);
  }
}

function renderSessions() {
  const list = $('#session-list');
  list.innerHTML = state.sessions.map(s => `
    <div class="session-item${s.id === state.currentSessionId ? ' active' : ''}" data-id="${s.id}">
      <span class="title">${escapeHtml(s.title || '新对话')}</span>
      <button class="btn-delete" data-action="delete-session" data-id="${s.id}">&times;</button>
    </div>
  `).join('');

  list.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.dataset.action === 'delete-session') {
        e.stopPropagation();
        deleteSession(e.target.dataset.id);
        return;
      }
      switchSession(el.dataset.id);
    });
  });
}

let _searchTimer = null;

async function searchSessions(query) {
  if (!query.trim()) {
    await loadSessions();
    return;
  }
  try {
    state.sessions = await API.get(`/api/sessions/search?q=${encodeURIComponent(query.trim())}`);
    renderSessions();
  } catch (e) {
    console.error('搜索会话失败:', e);
  }
}

async function createSession() {
  try {
    const s = await API.post('/api/sessions', { title: '新对话' });
    state.currentSessionId = s.id;
    await loadSessions();
    clearMessages();
    showToast('新对话已创建', 'success');
  } catch (e) {
    showToast('创建会话失败: ' + e.message, 'error');
  }
}

async function deleteSession(id) {
  const confirmed = await showConfirmDialog('删除对话', '确定要删除这个对话吗？此操作不可撤销。', '删除');
  if (!confirmed) return;
  try {
    await API.del(`/api/sessions/${id}`);
    if (state.currentSessionId === id) {
      state.currentSessionId = null;
      clearMessages();
    }
    await loadSessions();
    showToast('会话已删除', 'success');
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

async function switchSession(id) {
  state.currentSessionId = id;
  renderSessions();
  clearMessages();
  try {
    const s = await API.get(`/api/sessions/${id}`);
    if (s.messages && s.messages.length > 0) {
      s.messages.forEach(m => appendMessage(m.role, m.content));
    }
  } catch (e) {
    console.error('加载会话消息失败:', e);
  }
}

function clearMessages() {
  const container = $('#chat-messages');
  container.innerHTML = `
    <div class="welcome-message">
      <h1>OmniAssist</h1>
      <p>计时算文查网，一站式全能辅助</p>
      <p style="margin-top:8px;font-size:13px;">输入消息开始对话，或输入 <code>/</code> 查看可用命令</p>
    </div>`;
}

// ===== 消息渲染 =====
function appendMessage(role, content) {
  const container = $('#chat-messages');
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `message ${role}`;
  div.innerHTML = `
    <div class="avatar">${role === 'user' ? 'U' : 'AI'}</div>
    <div class="content">${renderContent(content)}</div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function createStreamingMessage() {
  const container = $('#chat-messages');
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="avatar">AI</div>
    <div class="content streaming-cursor"></div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div.querySelector('.content');
}

function renderContent(text) {
  let html = escapeHtml(text);
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    return `<pre><code class="language-${lang}">${escapeHtml(code.trim())}</code></pre>`;
  });
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\n{2,}/g, '\n');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ===== 聊天 =====
let abortController = null;

async function sendMessage() {
  const input = $('#chat-input');
  const message = input.value.trim();
  if (!message || state.isStreaming) return;

  input.value = '';
  input.style.height = 'auto';
  hideCommandSuggestions();

  if (!state.currentSessionId) {
    try {
      const s = await API.post('/api/sessions', { title: message.substring(0, 30) });
      state.currentSessionId = s.id;
      await loadSessions();
    } catch (e) {
      showToast('创建会话失败: ' + e.message, 'error');
      return;
    }
  }

  appendMessage('user', message);

  const contentEl = createStreamingMessage();
  state.isStreaming = true;
  $('#btn-send').classList.add('streaming');
  $('#btn-send').title = '停止生成';

  abortController = new AbortController();

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.currentSessionId,
        message: message,
        web_search: state.webSearch,
      }),
      signal: abortController.signal,
    });

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullContent = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if (!data || data === '[DONE]') continue;

        try {
          const parsed = JSON.parse(data);
          if (parsed.type === 'error') {
            contentEl.textContent = '错误: ' + parsed.content;
            contentEl.classList.remove('streaming-cursor');
            showToast(parsed.content, 'error');
            return;
          }
          if (parsed.type === 'tool_call') {
            fullContent += `\n\n🔧 调用工具: **${parsed.name}**\n`;
            contentEl.innerHTML = renderContent(fullContent);
            contentEl.classList.add('streaming-cursor');
            continue;
          }
          if (parsed.type === 'tool_result') {
            fullContent += `📋 工具结果: ${parsed.content}\n`;
            contentEl.innerHTML = renderContent(fullContent);
            contentEl.classList.add('streaming-cursor');
            continue;
          }
          if ((parsed.type === 'content' || parsed.type === 'token') && parsed.content) {
            fullContent += parsed.content;
            contentEl.innerHTML = renderContent(fullContent);
            contentEl.classList.add('streaming-cursor');
            const container = $('#chat-messages');
            container.scrollTop = container.scrollHeight;
          }
          if (parsed.type === 'done') {
            contentEl.classList.remove('streaming-cursor');
            if (!fullContent) {
              contentEl.textContent = parsed.content || '(无响应)';
            }
          }
        } catch (e) {
          // ignore parse errors
        }
      }
    }

    contentEl.classList.remove('streaming-cursor');
    if (!fullContent) {
      contentEl.textContent = '(无响应)';
    }

    if (state.currentSessionId) {
      await loadSessions();
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      contentEl.classList.remove('streaming-cursor');
      if (!fullContent) {
        contentEl.textContent = '(已停止生成)';
      }
      return;
    }
    contentEl.textContent = '请求失败: ' + e.message;
    contentEl.classList.remove('streaming-cursor');
    showToast('发送失败: ' + e.message, 'error');
  } finally {
    state.isStreaming = false;
    $('#btn-send').classList.remove('streaming');
    $('#btn-send').title = '发送';
    abortController = null;
  }
}

function stopGeneration() {
  if (abortController) {
    abortController.abort();
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

// ===== 模型配置 =====
let _configTab = 'personal';

function switchConfigTab(tab) {
  _configTab = tab;
  document.querySelectorAll('.config-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  $('#config-tab-personal').classList.toggle('hidden', tab !== 'personal');
  $('#config-tab-global').classList.toggle('hidden', tab !== 'global');
  $('#config-tab-search').classList.toggle('hidden', tab !== 'search');
}

function updateThoughtButton() {
  const btn = $('#btn-thought');
  if (!btn) return;
  if (state.showThought) {
    btn.classList.add('active');
    btn.querySelector('span').textContent = '思考过程·开';
  } else {
    btn.classList.remove('active');
    btn.querySelector('span').textContent = '思考过程';
  }
}

async function loadConfig() {
  try {
    const config = await API.get('/api/config');
    const apiKeyEl = $('#cfg-api-key');
    const baseUrlEl = $('#cfg-base-url');
    const modelNameEl = $('#cfg-model-name');
    const contextLimitEl = $('#cfg-context-limit');
    if (apiKeyEl) {
      apiKeyEl.value = '';
      apiKeyEl.placeholder = config.api_key_masked || '';
    }
    if (baseUrlEl) baseUrlEl.value = config.base_url || '';
    if (modelNameEl) modelNameEl.value = config.model_name || '';
    if (contextLimitEl) contextLimitEl.value = config.context_limit || '';

    state.showThought = config.show_thought || false;
    updateThoughtButton();

    if (hasPermission('model_config_global', 'read')) {
      $('#config-tabs').classList.remove('hidden');
      try {
        const globalCfg = await API.get('/api/config/global');
        const gApiKeyEl = $('#cfg-global-api-key');
        const gBaseUrlEl = $('#cfg-global-base-url');
        const gModelNameEl = $('#cfg-global-model-name');
        const gContextLimitEl = $('#cfg-global-context-limit');
        if (gApiKeyEl) {
          gApiKeyEl.value = '';
          gApiKeyEl.placeholder = globalCfg.api_key_masked || '';
        }
        if (gBaseUrlEl) gBaseUrlEl.value = globalCfg.base_url || '';
        if (gModelNameEl) gModelNameEl.value = globalCfg.model_name || '';
        if (gContextLimitEl) gContextLimitEl.value = globalCfg.context_limit || '';
      } catch (e) {
        // 全局配置加载失败不阻塞
      }
      try {
        const searchCfg = await API.get('/api/config/search');
        const searchKeyEl = $('#cfg-search-tavily-key');
        if (searchKeyEl) {
          searchKeyEl.value = '';
          searchKeyEl.placeholder = searchCfg.tavily_api_key_masked || '';
        }
      } catch (e) {
        // 搜索配置加载失败不阻塞
      }
    } else {
      $('#config-tabs').classList.add('hidden');
    }
    switchConfigTab('personal');
  } catch (e) {
    showToast('加载配置失败: ' + e.message, 'error');
  }
}

async function saveConfig() {
  const tab = _configTab;

  if (tab === 'search') {
    const searchKeyEl = $('#cfg-search-tavily-key');
    const body = {};
    if (searchKeyEl) {
      const key = searchKeyEl.value.trim();
      if (key) body.tavily_api_key = key;
    }
    try {
      await API.put('/api/config/search', body);
      showToast('联网搜索配置已保存', 'success');
      closeModal('modal-config');
    } catch (e) {
      showToast('保存失败: ' + e.message, 'error');
    }
    return;
  }

  const isGlobal = tab === 'global';
  const prefix = isGlobal ? 'cfg-global-' : 'cfg-';
  const body = {};

  const apiKeyEl = $(`#${prefix}api-key`);
  const baseUrlEl = $(`#${prefix}base-url`);
  const modelNameEl = $(`#${prefix}model-name`);
  const contextLimitEl = $(`#${prefix}context-limit`);

  if (apiKeyEl) {
    const apiKey = apiKeyEl.value.trim();
    if (apiKey) body.api_key = apiKey;
  }
  if (baseUrlEl) body.base_url = baseUrlEl.value.trim();
  if (modelNameEl) body.model_name = modelNameEl.value.trim();
  if (contextLimitEl) body.context_limit = contextLimitEl.value.trim();

  const url = isGlobal ? '/api/config/global' : '/api/config';

  try {
    await API.put(url, body);
    showToast('配置已保存', 'success');
    closeModal('modal-config');
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
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
  if (newPassword.length < 6) { showToast('新密码长度不能少于6位', 'error'); return; }
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
  tbody.innerHTML = '<tr><td colspan="5" class="loading-text">加载中...</td></tr>';
  try {
    _allUsers = await API.get('/api/users');
    renderUserTable(_allUsers);
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="5" class="loading-text">加载失败: ' + escapeHtml(e.message) + '</td></tr>';
  }
}

function renderUserTable(users) {
  const tbody = $('#user-table-body');
  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="loading-text">暂无用户</td></tr>';
    return;
  }
  tbody.innerHTML = users.map(u => {
    const isAdmin = u.user_type === 'admin';
    const deleteBtn = isAdmin
      ? '<button class="btn-sm" disabled style="opacity:0.4;cursor:not-allowed;" title="管理员不可删除">-</button>'
      : `<button class="btn-sm danger" data-action="delete-user" data-id="${u.id}" data-username="${escapeHtml(u.username)}">删除</button>`;
    return `
      <tr>
        <td>${u.id}</td>
        <td>${escapeHtml(u.username)}</td>
        <td><span class="user-type-badge ${u.user_type}">${isAdmin ? '管理员' : '普通用户'}</span></td>
        <td>${escapeHtml(u.description || '-')}</td>
        <td>
          <div class="actions">
            <button class="btn-sm" data-action="edit-user" data-id="${u.id}" data-username="${escapeHtml(u.username)}" data-type="${u.user_type}" data-desc="${escapeHtml(u.description || '')}">编辑</button>
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
  $('#user-form-password').value = '';
  $('#user-form-password').type = 'password';
  $('#btn-toggle-user-password').textContent = '👁';
  $('#user-form-type').value = 'user';
  $('#user-form-desc').value = '';
  openModal('modal-user-form');
}

function openEditUser(dataset) {
  $('#user-form-title').textContent = '编辑用户';
  $('#user-form-id').value = dataset.id;
  $('#user-form-username').value = dataset.username;
  $('#user-form-username').readOnly = true;
  $('#user-form-password').value = '';
  $('#user-form-password').type = 'password';
  $('#btn-toggle-user-password').textContent = '👁';
  $('#user-form-type').value = dataset.type;
  $('#user-form-desc').value = dataset.desc;
  openModal('modal-user-form');
}

async function submitUserForm() {
  const id = $('#user-form-id').value;
  const username = $('#user-form-username').value.trim();
  const password = $('#user-form-password').value;
  const userType = $('#user-form-type').value;
  const desc = $('#user-form-desc').value.trim();

  if (!id) {
    if (!username) { showToast('请输入用户名', 'error'); return; }
    if (!password) { showToast('请输入密码', 'error'); return; }
    if (password.length < 6) { showToast('密码长度不能少于6位', 'error'); return; }

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
      if (password.length < 6) { showToast('密码长度不能少于6位', 'error'); return; }
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
  _deleteUserId = dataset.id;
  $('#confirm-delete-username').textContent = dataset.username;
  openModal('modal-confirm-delete');
}

async function confirmDeleteUser() {
  if (!_deleteUserId) return;
  try {
    await API.del('/api/users/' + _deleteUserId);
    showToast('用户已删除', 'success');
    closeModal('modal-confirm-delete');
    _deleteUserId = null;
    loadUsers();
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// ===== 工具列表 =====
async function loadTools() {
  const container = $('#tools-container');
  container.innerHTML = '<p class="loading-text">加载中...</p>';
  try {
    const tools = await API.get('/api/tools');
    if (tools.length === 0) {
      container.innerHTML = '<p class="loading-text">暂无工具</p>';
      return;
    }
    container.innerHTML = tools.map(t => `
      <div class="tool-card">
        <div class="tool-name">${escapeHtml(t.name)}</div>
        <div class="tool-desc">${escapeHtml(t.description)}</div>
        <div class="tool-meta">
          <span>${escapeHtml(t.execution_mode)}</span>
          ${t.output_dir ? `<span>输出: ${escapeHtml(t.output_dir)}</span>` : ''}
          ${t.dependencies && t.dependencies.length > 0 ? `<span>依赖: ${escapeHtml(t.dependencies.join(', '))}</span>` : ''}
        </div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<p class="loading-text">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

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
      header.addEventListener('click', () => {
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

function renderFileFolder(folder) {
  const children = (folder.children || []).map(f => `
    <div class="file-item">
      <span class="file-name">📄 ${escapeHtml(f.name)}</span>
      <span class="file-size">${formatSize(f.size)}</span>
      <span class="file-actions">
        <button class="btn-sm" data-action="download" data-path="${escapeHtml(f.path)}">下载</button>
        <button class="btn-sm danger" data-action="delete-file" data-path="${escapeHtml(f.path)}">删除</button>
      </span>
    </div>
  `).join('');

  return `
    <div class="file-folder">
      <div class="file-folder-header">
        <span class="arrow">▶</span>
        <span>📁 ${escapeHtml(folder.name)}</span>
        <span style="margin-left:auto;color:var(--text-muted);font-size:12px">${(folder.children || []).length} 个文件</span>
      </div>
      <div class="file-folder-body hidden">${children}</div>
    </div>
  `;
}

function downloadFile(path) {
  window.open(`/api/files/download?path=${encodeURIComponent(path)}`, '_blank');
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
function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark') {
    document.documentElement.classList.add('dark');
    $('#theme-toggle').textContent = '☀️';
  } else {
    document.documentElement.classList.remove('dark');
    $('#theme-toggle').textContent = '🌙';
  }
}

function toggleTheme() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
  $('#theme-toggle').textContent = isDark ? '☀️' : '🌙';
}

// ===== 事件绑定 =====
document.addEventListener('DOMContentLoaded', () => {
  initTheme();

  $('#theme-toggle').addEventListener('click', toggleTheme);

  loadSessions();

  $('#btn-new-session').addEventListener('click', createSession);
  $('#btn-settings').addEventListener('click', toggleDrawer);
  $('#btn-close-drawer').addEventListener('click', () => $('#settings-drawer').classList.add('hidden'));

  // 搜索对话弹窗
  const searchDialog = $('#modal-search-dialog');
  const searchDialogInput = $('#search-dialog-input');
  const searchDialogResults = $('#search-dialog-results');
  let _searchDialogTimer = null;

  $('#btn-search-dialog').addEventListener('click', () => {
    searchDialog.classList.remove('hidden');
    setTimeout(() => searchDialogInput.focus(), 100);
    searchDialogInput.value = '';
    searchDialogResults.innerHTML = '<p class="search-dialog-hint">输入关键词搜索历史对话</p>';
  });

  $('#btn-search-dialog-close').addEventListener('click', () => {
    searchDialog.classList.add('hidden');
  });

  searchDialog.querySelector('.modal-overlay').addEventListener('click', () => {
    searchDialog.classList.add('hidden');
  });

  searchDialogInput.addEventListener('input', () => {
    clearTimeout(_searchDialogTimer);
    const query = searchDialogInput.value.trim();
    if (!query) {
      searchDialogResults.innerHTML = '<p class="search-dialog-hint">输入关键词搜索历史对话</p>';
      return;
    }
    _searchDialogTimer = setTimeout(() => searchSessionsDialog(query), 300);
  });

  async function searchSessionsDialog(query) {
    try {
      const sessions = await API.get(`/api/sessions/search?q=${encodeURIComponent(query)}`);
      if (sessions.length === 0) {
        searchDialogResults.innerHTML = '<p class="search-dialog-empty">未找到匹配的对话</p>';
        return;
      }
      searchDialogResults.innerHTML = sessions.map(s => {
        const date = new Date(s.created_at * 1000);
        const timeStr = date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
        return `
          <div class="search-dialog-item" data-id="${s.id}">
            <div class="item-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            </div>
            <div class="item-info">
              <div class="item-title">${escapeHtml(s.title || '新对话')}</div>
              <div class="item-meta">${timeStr} · ${s.message_count || 0} 条消息</div>
            </div>
            <div class="item-enter">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            </div>
          </div>
        `;
      }).join('');

      searchDialogResults.querySelectorAll('.search-dialog-item').forEach(el => {
        el.addEventListener('click', () => {
          searchDialog.classList.add('hidden');
          switchSession(el.dataset.id);
        });
      });
    } catch (e) {
      searchDialogResults.innerHTML = '<p class="search-dialog-empty">搜索失败: ' + escapeHtml(e.message) + '</p>';
    }
  }

  $('#btn-send').addEventListener('click', () => {
    if (state.isStreaming) {
      stopGeneration();
    } else {
      sendMessage();
    }
  });

  const input = $('#chat-input');
  let isComposing = false;

  input.addEventListener('compositionstart', () => { isComposing = true; });
  input.addEventListener('compositionend', () => { isComposing = false; });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (isComposing) return;
      e.preventDefault();
      if (state.commandMode && state.selectedCommandIdx >= 0) {
        const container = $('#command-suggestions');
        const selected = container.querySelector('.command-item.selected');
        if (selected) {
          const idx = parseInt(selected.dataset.idx);
          const filter = state.commandFilter.toLowerCase();
          const filtered = COMMANDS.filter(c => c.command.toLowerCase().includes(filter));
          if (filtered[idx]) selectCommand(filtered[idx]);
          return;
        }
      }
      sendMessage();
      return;
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); navigateCommand(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); navigateCommand(-1); return; }
    if (e.key === 'Escape') { hideCommandSuggestions(); return; }
  });

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 150) + 'px';
    handleCommandInput(input.value);
  });

  input.addEventListener('blur', () => {
    setTimeout(() => hideCommandSuggestions(), 150);
  });

  input.addEventListener('focus', () => {
    if (input.value.startsWith('/')) {
      state.commandMode = true;
      state.commandFilter = input.value;
      state.selectedCommandIdx = 0;
      showCommandSuggestions();
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.chat-input-container') && !e.target.closest('#command-suggestions')) {
      hideCommandSuggestions();
    }
  });

  // 设置抽屉菜单
  $$('.drawer-item').forEach(item => {
    item.addEventListener('click', () => {
      const action = item.dataset.action;
      $('#settings-drawer').classList.add('hidden');
      if (action === 'config') { loadConfig(); openModal('modal-config'); }
      if (action === 'tools') { loadTools(); openModal('modal-tools'); }
      if (action === 'files') { loadFiles(); openModal('modal-files'); }
      if (action === 'change-password') { openChangePassword(); }
      if (action === 'user-management') { loadUsers(); openModal('modal-user-management'); }
      if (action === 'logout') {
        document.cookie = 'auth_token=; path=/; max-age=0';
        window.location.href = '/login.html';
      }
    });
  });

  // 模态框关闭
  $$('.modal-close').forEach(btn => {
    btn.addEventListener('click', () => {
      btn.closest('.modal').classList.add('hidden');
    });
  });

  $$('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', () => {
      overlay.closest('.modal').classList.add('hidden');
    });
  });

  // 配置保存
  $('#btn-save-config').addEventListener('click', saveConfig);

  // 修改密码
  $('#btn-save-password').addEventListener('click', savePassword);

  // 用户管理
  $('#btn-add-user').addEventListener('click', openAddUser);
  $('#btn-submit-user').addEventListener('click', submitUserForm);
  $('#btn-confirm-delete').addEventListener('click', confirmDeleteUser);
  $('#user-search').addEventListener('input', filterUsers);

  // 密码显示切换
  $('#btn-toggle-user-password').addEventListener('click', () => {
    const input = $('#user-form-password');
    const btn = $('#btn-toggle-user-password');
    if (input.type === 'password') {
      input.type = 'text';
      btn.textContent = '🙈';
    } else {
      input.type = 'password';
      btn.textContent = '👁';
    }
  });

  // 加载当前用户信息
  loadCurrentUser();

  // 联网搜索三态切换: off -> auto -> on -> off
  const WEB_SEARCH_MODES = ['off', 'auto', 'on'];
  const WEB_SEARCH_LABELS = {
    off: '联网搜索',
    auto: '联网搜索·自动',
    on: '联网搜索·开启',
  };
  $('#btn-web-search').addEventListener('click', () => {
    const btn = $('#btn-web-search');
    const currentIdx = WEB_SEARCH_MODES.indexOf(state.webSearch);
    const nextIdx = (currentIdx + 1) % WEB_SEARCH_MODES.length;
    state.webSearch = WEB_SEARCH_MODES[nextIdx];
    btn.dataset.mode = state.webSearch;
    btn.querySelector('span').textContent = WEB_SEARCH_LABELS[state.webSearch];
    btn.classList.remove('active', 'auto');
    if (state.webSearch === 'on') btn.classList.add('active');
    if (state.webSearch === 'auto') btn.classList.add('auto');
    btn.title = WEB_SEARCH_LABELS[state.webSearch];
  });

  $('#btn-thought').addEventListener('click', async () => {
    state.showThought = !state.showThought;
    updateThoughtButton();
    try {
      await API.put('/api/config', { show_thought: state.showThought });
    } catch (e) {
      showToast('保存思考设置失败: ' + e.message, 'error');
      state.showThought = !state.showThought;
      updateThoughtButton();
    }
  });

  // 保存搜索配置
  // 配置标签页切换
  document.querySelectorAll('.config-tab').forEach(tab => {
    tab.addEventListener('click', () => switchConfigTab(tab.dataset.tab));
  });

  // 快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllModals();
      hideCommandSuggestions();
      $('#settings-drawer').classList.add('hidden');
    }
  });

  // 点击聊天区域关闭抽屉
  $('#chat-area').addEventListener('click', () => {
    $('#settings-drawer').classList.add('hidden');
  });
});