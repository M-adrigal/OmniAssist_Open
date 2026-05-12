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
  { command: '/model set', description: '配置模型参数，如 /model set model_name=gpt-4', category: '模型' },
  { command: '/model status', description: '查看当前模型配置', category: '模型' },
  { command: '/tool list', description: '列出所有可用工具', category: '工具' },
  { command: '/tool create', description: '创建新工具，如 /tool create 描述', category: '工具' },
  { command: '/tool delete', description: '删除工具，如 /tool delete 工具名', category: '工具' },
  { command: '/file list', description: '列出所有输出文件', category: '文件' },
  { command: '/clear', description: '清空当前对话', category: '通用' },
];

let state = {
  sessions: [],
  currentSessionId: null,
  isStreaming: false,
  commandMode: false,
  commandFilter: '',
  selectedCommandIdx: -1,
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
  if (!confirm('确定要删除这个会话吗？此操作不可撤销。')) return;
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
      <h1>Agent Framework</h1>
      <p>输入消息开始对话，或输入 <code>/</code> 查看可用命令</p>
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
async function loadConfig() {
  try {
    const config = await API.get('/api/config');
    $('#cfg-api-key').value = '';
    $('#cfg-api-key').placeholder = config.api_key_masked;
    $('#cfg-base-url').value = config.base_url;
    $('#cfg-model-name').value = config.model_name;
    $('#cfg-max-rounds').value = config.max_history_rounds;
  } catch (e) {
    showToast('加载配置失败: ' + e.message, 'error');
  }
}

async function saveConfig() {
  const body = {};
  const apiKey = $('#cfg-api-key').value.trim();
  if (apiKey) body.api_key = apiKey;
  body.base_url = $('#cfg-base-url').value.trim();
  body.model_name = $('#cfg-model-name').value.trim();
  body.max_history_rounds = parseInt($('#cfg-max-rounds').value) || 10;

  try {
    await API.put('/api/config', body);
    showToast('配置已保存', 'success');
    closeModal('modal-config');
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
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
  if (!confirm(`确定要删除 ${path} 吗？`)) return;
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