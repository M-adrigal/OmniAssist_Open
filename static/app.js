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
];

let state = {
  sessions: [],
  currentSessionId: null,
  isStreaming: false,
  streamingSessionId: null,
  backgroundStreamSessionId: null,
  completedBgSessions: new Set(),
  sessionContainers: {},  // { sessionId: { element: HTMLElement, stream: object|null } }
  commandMode: false,
  commandFilter: '',
  selectedCommandIdx: -1,
  currentUser: null,
  permissions: {},
  webSearch: 'off',
  thinkingMode: 'low',  // 思考模式：off(关) / low(低) / high(高)
  attachedFiles: [],
  taskStatuses: {},  // { sessionId: { status, user_message, started_at, completed_at } }
  acknowledgedTasks: new Set(),  // 已查看过的完成任务 session_id
  subscribingSessionId: null,  // 正在订阅后台任务的 session_id
};

// ===== 已读任务持久化（刷新后不再重复弹出完成气泡）=====
const _ACK_KEY = 'omni_acknowledged_tasks';
function _loadAcknowledged() {
  try {
    const raw = localStorage.getItem(_ACK_KEY);
    if (raw) state.acknowledgedTasks = new Set(JSON.parse(raw));
  } catch (e) { /* 忽略解析错误 */ }
}
function _saveAcknowledged() {
  try {
    localStorage.setItem(_ACK_KEY, JSON.stringify(Array.from(state.acknowledgedTasks)));
  } catch (e) { /* 忽略写入错误（隐私模式等） */ }
}

// ===== 会话持久化（刷新后恢复当前会话）=====
const _SID_KEY = 'omni_current_session_id';
function _saveCurrentSessionId() {
  try { localStorage.setItem(_SID_KEY, state.currentSessionId || ''); } catch (e) { /* ignore */ }
}
function _loadCurrentSessionId() {
  try { return localStorage.getItem(_SID_KEY) || ''; } catch (e) { return ''; }
}

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

// ===== Session 容器管理（Per-Session DOM 隔离） =====
function getActiveContainer() {
  const sid = state.currentSessionId || '__default__';
  let sc = state.sessionContainers[sid];
  if (!sc) {
    const wrap = $('#chat-messages');
    // 移除初始欢迎页（index.html 中的静态内容）
    const initWelcome = wrap.querySelector(':scope > .welcome-message');
    if (initWelcome) initWelcome.remove();
    // 隐藏所有已有容器
    Object.values(state.sessionContainers).forEach(c => c.element.style.display = 'none');
    const el = document.createElement('div');
    el.className = 'session-messages';
    el.dataset.sessionId = sid;
    el.innerHTML = `
      <div class="welcome-message">
        <h1>OmniAssist</h1>
        <p>计时算文查网，一站式全能辅助</p>
        <p style="margin-top:8px;font-size:13px;">输入消息开始对话，工具管理现已支持自然语言交互</p>
      </div>`;
    wrap.appendChild(el);
    sc = { element: el, stream: null };
    state.sessionContainers[sid] = sc;
  }
  return sc;
}

function showSessionContainer(sessionId) {
  const sid = sessionId || '__default__';
  const wrap = $('#chat-messages');
  // 移除初始欢迎页（index.html 中的静态内容）
  const initWelcome = wrap.querySelector(':scope > .welcome-message');
  if (initWelcome) initWelcome.remove();
  // 隐藏所有容器
  Object.values(state.sessionContainers).forEach(c => c.element.style.display = 'none');
  // 确保目标容器存在
  if (!state.sessionContainers[sid]) {
    const el = document.createElement('div');
    el.className = 'session-messages';
    el.dataset.sessionId = sid;
    el.innerHTML = `
      <div class="welcome-message">
        <h1>OmniAssist</h1>
        <p>计时算文查网，一站式全能辅助</p>
        <p style="margin-top:8px;font-size:13px;">输入消息开始对话，工具管理现已支持自然语言交互</p>
      </div>`;
    wrap.appendChild(el);
    state.sessionContainers[sid] = { element: el, stream: null };
  }
  state.sessionContainers[sid].element.style.display = '';
  wrap.scrollTop = wrap.scrollHeight;
}

function removeSessionContainer(sessionId) {
  const sid = sessionId || '__default__';
  const sc = state.sessionContainers[sid];
  if (sc) {
    sc.element.remove();
    delete state.sessionContainers[sid];
  }
}

function getSessionStream(sessionId) {
  const sc = state.sessionContainers[sessionId];
  return sc ? sc.stream : null;
}

function setSessionStream(sessionId, stream) {
  const sid = sessionId || '__default__';
  let sc = state.sessionContainers[sid];
  if (!sc) {
    // 确保容器存在
    showSessionContainer(sid);
    sc = state.sessionContainers[sid];
  }
  sc.stream = stream;
}

// 切换锁：防止并发切换导致 DOM 状态混乱
let _switchLock = false;
let _switchAbortController = null;
let _switchSeqId = 0;

function scrollToBottom() {
  const wrap = $('#chat-messages');
  wrap.scrollTop = wrap.scrollHeight;
}

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
	      modal.querySelectorAll('.modal-close').forEach(btn => {
	        btn.removeEventListener('click', onCancel);
	      });
	      modal.querySelector('.modal-overlay').removeEventListener('click', onCancel);
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
    updateChatTitle();
  } catch (e) {
    console.error('加载会话失败:', e);
  }
}

// 顶部标题栏：根据当前 session 显示其标题（取自 session 列表，与侧边栏标题同源）
function updateChatTitle() {
  const el = $('#chat-title');
  if (!el) return;
  const s = state.sessions.find(x => x.id === state.currentSessionId);
  el.textContent = (s && s.title) ? s.title : '新对话';
}

function renderSessions() {
  const list = $('#session-list');
  list.innerHTML = state.sessions.map(s => {
    // 当前正在查看的 session 不显示任何任务状态徽标（进度直接体现在对话区）
    const isCurrent = s.id === state.currentSessionId;
    const taskInfo = state.taskStatuses[s.id];
    const isRunning = !isCurrent && taskInfo && taskInfo.status === 'running';
    const isCompleted = !isCurrent && taskInfo && taskInfo.status === 'completed'
      && !state.acknowledgedTasks.has(s.id);
    const showBadge = !isCurrent && (state.completedBgSessions.has(s.id) || isCompleted);
    let badgeHtml = '';
    if (isRunning) badgeHtml += '<span class="badge-running" title="任务执行中"><span class="spinner-dot"></span></span>';
    else if (showBadge) badgeHtml += '<span class="badge-done" title="任务已完成">✓</span>';
    return `
    <div class="session-item${s.id === state.currentSessionId ? ' active' : ''}" data-id="${s.id}">
      <span class="title">${escapeHtml(s.title || '新对话')}</span>
      ${badgeHtml}
      <button class="btn-delete" data-action="delete-session" data-id="${s.id}">&times;</button>
    </div>
    `;
  }).join('');

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

// ===== 任务状态轮询 =====
let _taskPollingTimer = null;
let _taskPollTick = 5000;

async function pollTaskStatus() {
  try {
    const statuses = await API.get('/api/sessions/task-status/all');
    const oldStatuses = state.taskStatuses;
    state.taskStatuses = statuses || {};

    let needsRender = false;
    let hasRunning = false;
    for (const [sid, info] of Object.entries(state.taskStatuses)) {
      const oldInfo = oldStatuses[sid];
      if (info.status === 'running') {
        hasRunning = true;
        // 重新运行后允许再次通知完成（清除旧的已读标记与气泡）
        if (state.acknowledgedTasks.has(sid)) { state.acknowledgedTasks.delete(sid); _saveAcknowledged(); }
        state.completedBgSessions.delete(sid);
        // 运行中的任务：状态从无到有时需要刷新（让其他 session 出现转圈）
        if (!oldInfo) needsRender = true;
        continue;
      }
      // 任务完成
      if (info.status === 'completed') {
        if (state.acknowledgedTasks.has(sid)) {
          // 已读过的（含刷新前已查看过的），不再弹气泡
          state.completedBgSessions.delete(sid);
        } else if (sid === state.currentSessionId) {
          // 正在查看该 session，用户已经看到结果，直接标记为已读，不留气泡
          state.acknowledgedTasks.add(sid);
          _saveAcknowledged();
          state.completedBgSessions.delete(sid);
        } else {
          // 其他 session 完成：弹出完成气泡（直到用户点进去查看）
          state.completedBgSessions.add(sid);
        }
        // 仅在状态发生变化时刷新（首次出现 completed 也算变化）
        if (!oldInfo || oldInfo.status !== 'completed') needsRender = true;
      }
    }
    // 清理已不存在的任务
    for (const sid of Object.keys(oldStatuses)) {
      if (!(sid in state.taskStatuses)) {
        needsRender = true;
      }
    }
    if (needsRender) {
      renderSessions();
    }
    // 有任务运行时提高轮询频率，让完成气泡更及时
    _taskPollTick = hasRunning ? 2000 : 5000;
  } catch (e) {
    // 忽略轮询错误
  }
  _taskPollingTimer = setTimeout(pollTaskStatus, _taskPollTick);
}

function startTaskPolling() {
  if (_taskPollingTimer) clearTimeout(_taskPollingTimer);
  pollTaskStatus();
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
    _saveCurrentSessionId();
    await loadSessions();
    updateChatTitle();
    clearMessages();
    clearAttachedFiles();
    showToast('新对话已创建', 'success');
  } catch (e) {
    showToast('创建会话失败: ' + e.message, 'error');
  }
}

let _deleteLock = false;

async function deleteSession(id) {
  // 删除锁：防止并发删除导致状态混乱
  if (_deleteLock) {
    console.log('[deleteSession] 删除操作进行中，忽略重复请求');
    return;
  }
  _deleteLock = true;

  try {
    // 防御：会话 id 为空/缺失时，无法构造有效删除请求（DELETE /api/sessions/ 会命中根路由返回 405）。
    // 此时直接本地清理即可（此类脏会话在后端已不存在）。
    if (!id) {
      console.warn('[deleteSession] 会话 id 为空，跳过服务端请求，仅本地移除');
      if (state.currentSessionId === id) {
        state.currentSessionId = null;
        _saveCurrentSessionId();
        clearMessages();
        updateChatTitle();
      }
      try { removeSessionContainer(id); } catch (e) { /* ignore */ }
      await loadSessions();
      showToast('会话已删除', 'success');
      return;
    }

    const confirmed = await showConfirmDialog('删除对话', '确定要删除这个对话吗？此操作不可撤销。', '删除');
    if (!confirmed) return;

    // 带超时的删除请求
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const res = await fetch(`/api/sessions/${id}`, { method: 'DELETE', signal: controller.signal });
      clearTimeout(timer);
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || res.statusText);
      }
    } catch (e) {
      clearTimeout(timer);
      if (e.name === 'AbortError') {
        showToast('删除超时，请稍后重试', 'error');
        return;
      }
      throw e;
    }

    if (state.currentSessionId === id) {
      state.currentSessionId = null;
      _saveCurrentSessionId();
      clearMessages();
      updateChatTitle();
    }
    // 清理该 session 的任务状态，避免残留徽标
    delete state.taskStatuses[id];
    state.completedBgSessions.delete(id);
    state.acknowledgedTasks.delete(id);
    _saveAcknowledged();
    // 安全移除容器（忽略不存在的容器）
    try {
      removeSessionContainer(id);
    } catch (e) {
      console.warn('移除 session 容器失败:', e);
    }
    await loadSessions();
    showToast('会话已删除', 'success');
  } catch (e) {
    console.error('删除会话失败:', e);
    showToast('删除失败: ' + e.message, 'error');
  } finally {
    _deleteLock = false;
  }
}

async function switchSession(id) {
  // 取消上一次未完成的切换请求（必须在锁检查之前，确保旧请求被中止）
  if (_switchAbortController) {
    _switchAbortController.abort();
    _switchAbortController = null;
    // 旧请求已取消，强制释放锁，防止死锁（旧请求 finally 中因序列号不匹配不会释放锁）
    _switchLock = false;
  }

  // 切换锁：防止并发切换导致 DOM 状态混乱
  if (_switchLock) {
    console.log('[switchSession] 切换被锁定，已取消前次请求，请稍后重试');
    return;
  }
  _switchLock = true;

  // 分配序列号，用于丢弃过期结果
  const mySeqId = ++_switchSeqId;
  _switchAbortController = new AbortController();
  const switchSignal = _switchAbortController.signal;

  try {
    console.log(`[switchSession] #${mySeqId} 切换到 session: ${id}`);

    // 如果当前正在流式输出的 session 被切走，标记为后台运行
    if (state.isStreaming && state.currentSessionId === state.streamingSessionId) {
      console.log(`[switchSession] #${mySeqId} session ${state.currentSessionId} 转入后台运行`);
      state.backgroundStreamSessionId = state.currentSessionId;
    }
    // 清除目标 session 的完成气泡和已查看标记
    if (state.completedBgSessions.has(id)) {
      state.completedBgSessions.delete(id);
    }
    // 标记该 session 的任务为已查看（无论是否已完成）
    const _taskInfoPre = state.taskStatuses[id];
    if (_taskInfoPre && _taskInfoPre.status !== 'running') {
      state.acknowledgedTasks.add(id);
      _saveAcknowledged();
    }
    state.currentSessionId = id;
    _saveCurrentSessionId();
    renderSessions();
    updateChatTitle();
    clearAttachedFiles();
    refreshTrustState();

    // 显示目标 session 的容器（保留已有内容，不销毁 DOM）
    showSessionContainer(id);

    // 如果目标 session 正在运行流式任务，容器中已有实时内容，直接返回
    if (id === state.streamingSessionId && state.isStreaming) {
      console.log(`[switchSession] #${mySeqId} session ${id} 正在流式运行，保留容器内容`);
      return;
    }

    // 如果容器已有内容（非欢迎页），说明已加载过，不重复加载
    const sc = state.sessionContainers[id];
    const hasContent = sc && sc.element.querySelector('.message');
    console.log(`[switchSession] #${mySeqId} hasContent=${hasContent}, containerExists=${!!sc}`);
    if (hasContent) {
      return;
    }

    // 如果该 session 有正在运行的后台任务，订阅事件流（重连）
    const taskInfo = state.taskStatuses[id];
    if (taskInfo && taskInfo.status === 'running') {
      console.log(`[switchSession] #${mySeqId} session ${id} has running task, subscribing`);
      state.acknowledgedTasks.add(id);
      subscribeToTask(id);
      return;
    }

    // 从服务器加载历史消息（使用 switchSignal 确保切换时取消请求）
    const fetchWithTimeout = async (url, timeout = 8000) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeout);
      // 监听 switchSignal，当用户切换时取消请求
      const onSwitchAbort = () => controller.abort();
      switchSignal.addEventListener('abort', onSwitchAbort, { once: true });
      try {
        const res = await fetch(url, { signal: controller.signal });
        clearTimeout(timer);
        if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
        return res.json();
      } catch (e) {
        clearTimeout(timer);
        throw e;
      } finally {
        switchSignal.removeEventListener('abort', onSwitchAbort);
      }
    };

    try {
      // 检查是否已被新切换取消
      if (switchSignal.aborted) {
        console.log(`[switchSession] #${mySeqId} 已被取消（切换前）`);
        return;
      }

      console.log(`[switchSession] #${mySeqId} 从服务器加载 session ${id} 的消息`);
      const [s, filesData] = await Promise.all([
        fetchWithTimeout(`/api/sessions/${id}`),
        fetchWithTimeout(`/api/files/uploads?session_id=${id}`),
      ]);

      // 检查是否已被新切换取消，或序列号已过期
      if (switchSignal.aborted || mySeqId !== _switchSeqId) {
        console.log(`[switchSession] #${mySeqId} 结果已过期，丢弃 (current=#${_switchSeqId})`);
        return;
      }

      if (s.messages && s.messages.length > 0) {
        const containerEl = sc.element;
        // 清除欢迎页
        const welcome = containerEl.querySelector('.welcome-message');
        if (welcome) welcome.remove();
        // 切换会话时重置会话文件映射，避免残留其他 session 的产出文件路径
        _sessionFiles = new Map();
        s.messages.forEach(m => renderHistoryMessage(m, containerEl));
        console.log(`[switchSession] #${mySeqId} 已渲染 ${s.messages.length} 条历史消息`);
      }
      if (filesData.files && filesData.files.length > 0) {
        const containerEl = sc.element;
        const iconMap = { text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C' };
        const tags = filesData.files.map(f => {
          const icon = iconMap[f.type] || 'F';
          return `<span class="msg-attach-tag"><span class="tag-icon">${icon}</span>${escapeHtml(f.filename)}</span>`;
        }).join('');
        const attachHtml = `<div class="msg-attachments">${tags}</div>`;
        const userMsgs = containerEl.querySelectorAll('.message.user');
        if (userMsgs.length > 0) {
          const lastUserMsg = userMsgs[userMsgs.length - 1];
          const areaEl = lastUserMsg.querySelector('.answer-area');
          if (areaEl && !areaEl.querySelector('.msg-attachments')) {
            areaEl.insertAdjacentHTML('beforeend', attachHtml);
          }
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') {
        console.log(`[switchSession] #${mySeqId} 请求超时或被取消`);
        return;
      }
      console.error('加载会话消息失败:', e);
      showToast('加载会话失败: ' + e.message, 'error');
    }
  } finally {
    // 只有当前序列号匹配时才释放锁
    if (mySeqId === _switchSeqId) {
      _switchLock = false;
      console.log(`[switchSession] #${mySeqId} 锁已释放`);
    }
  }
}

function clearMessages() {
  const sc = getActiveContainer();
  sc.element.innerHTML = `
    <div class="welcome-message">
      <h1>OmniAssist</h1>
      <p>计时算文查网，一站式全能辅助</p>
      <p style="margin-top:8px;font-size:13px;">输入消息开始对话，工具管理现已支持自然语言交互</p>
    </div>`;
}

// ===== 消息渲染 =====
function appendMessage(role, content, attachments, targetContainer) {
  const container = targetContainer || getActiveContainer().element;
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `message ${role}`;
  let attachHtml = '';
  if (attachments && attachments.length > 0) {
    const iconMap = { text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C' };
    const tags = attachments.map(f => {
      const icon = iconMap[f.type] || 'F';
      return `<span class="msg-attach-tag"><span class="tag-icon">${icon}</span>${escapeHtml(f.filename)}</span>`;
    }).join('');
    attachHtml = `<div class="msg-attachments">${tags}</div>`;
  }
  div.innerHTML = `
    <div class="avatar">${role === 'user' ? 'U' : 'AI'}</div>
    <div class="message-body"><div class="answer-area">${renderContent(content)}${attachHtml}</div></div>
  `;
  container.appendChild(div);
  scrollToBottom();
  return div;
}

function renderHistoryMessage(m, targetContainer) {
  const container = targetContainer || getActiveContainer().element;
  const role = typeof m === 'string' ? 'user' : (m.role || 'user');
  const content = typeof m === 'string' ? m : (m.content || '');
  const compressedMeta = (m && m.compressed_metadata) || null;
  const thought = (m && m.thought) || '';
  const showThought = !!thought;
  const tools = (m && m.tools) || null;
  const search = (m && m.search) || null;
  const hasMeta = showThought || (tools && tools.length > 0) || search;

  if (role === 'assistant') {
    console.log('[renderHistoryMessage]', {
      hasThought: showThought,
      thoughtLen: thought.length,
      hasTools: !!(tools && tools.length > 0),
      toolsCount: tools ? tools.length : 0,
      hasSearch: !!search,
      hasMeta: hasMeta,
      contentPreview: typeof content === 'string' ? content.substring(0, 80) : String(content).substring(0, 80)
    });
  }

  if (role === 'system' && compressedMeta && compressedMeta.rounds && compressedMeta.rounds.length > 0) {
    renderCompressedHistory(content, compressedMeta, container);
    return;
  }

  if (role === 'user' || !hasMeta) {
    appendMessage(role, content, null, container);
    return;
  }

  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="avatar">AI</div>
    <div class="message-body">
      ${showThought ? `
        <div class="think-area">
          <div class="think-header">
            <span class="think-status">思考过程</span>
            <span class="think-time"></span>
            <span class="think-toggle">▸</span>
          </div>
          <div class="think-content collapsed">
            ${thought.split('\n').filter(l => l.trim()).map(l => `<div class="think-line">${escapeHtml(l)}</div>`).join('')}
          </div>
        </div>
      ` : ''}
      ${search ? `
        <div class="search-area">
          <div class="search-header">
            <span class="search-status">联网搜索</span>
            <span class="search-toggle">▸</span>
          </div>
          <div class="search-content collapsed">
            <div class="search-info">
              <div class="search-info-item"><span class="search-label">场景：</span>${escapeHtml(search.scenario || '通用搜索')}</div>
              <div class="search-info-item"><span class="search-label">关键词：</span>${escapeHtml(search.query || '')}</div>
            </div>
            <div class="search-results">${escapeHtml((search.results || '').length > 300 ? (search.results || '').substring(0, 300) + '...' : (search.results || ''))}</div>
            ${(search.results || '').length > 300 ? `<button class="search-result-expand" data-full="${escapeHtml(search.results)}">展开全部</button>` : ''}
          </div>
        </div>
      ` : ''}
      ${tools && tools.length > 0 ? `
        <div class="tool-summary">
          <div class="tool-summary-header">
            <span class="tool-summary-title">工具调用 (${tools.length} 个)${tools.filter(t => t.error).length > 0 ? ` <span class="tool-error-badge">${tools.filter(t => t.error).length} 个错误</span>` : ''}</span>
            <span class="tool-summary-toggle">▸</span>
          </div>
          <div class="tool-summary-body collapsed">
            ${tools.map((t, i) => `
              <div class="tool-item${t.error ? ' error' : ''}${t.skipped ? ' skipped' : ''}">
                <div class="tool-item-header">
                  <span class="tool-item-name">${escapeHtml(t.name)}</span>
                  <span class="tool-item-index">#${i + 1}</span>
                </div>
                <div class="tool-item-args">
                  <span class="tool-item-label">参数：</span>
                  <code>${escapeHtml(JSON.stringify(t.arguments, null, 2))}</code>
                </div>
                <div class="tool-item-result">
                  <span class="tool-item-label">结果：</span>
                  <span class="tool-result-text${t.error ? ' error' : ''}${t.skipped ? ' skipped' : ''}">${escapeHtml((t.result || '').length > 200 ? (t.result || '').substring(0, 200) + '...' : (t.result || ''))}</span>
                  ${(t.result || '').length > 200 ? `<button class="tool-result-expand" data-full="${escapeHtml(t.result)}">展开全部</button>` : ''}
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      ` : ''}
      <div class="answer-area">${renderContent(content)}</div>
      <div class="output-files hidden"></div>
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();

  // 历史消息中若携带产出文件元数据，还原下载卡片并填充会话文件映射（刷新/重登录后可见）
  const histFiles = (m && m.files) || null;
  if (histFiles && histFiles.length > 0) {
    const outEl = div.querySelector('.output-files');
    if (outEl) {
      renderOutputFiles({ outputFilesEl: outEl }, histFiles);
      (histFiles || []).forEach(f => { if (f && f.name && f.path) _sessionFiles.set(f.name, f.path); });
    }
    _linkifyFileNames(div.querySelector('.answer-area'));
  }

  if (search) {
    const searchHeader = div.querySelector('.search-header');
    const searchContent = div.querySelector('.search-content');
    const searchToggle = div.querySelector('.search-toggle');
    if (searchHeader && searchContent && searchToggle) {
      searchHeader.addEventListener('click', () => {
        const isCollapsed = searchContent.classList.toggle('collapsed');
        searchToggle.textContent = isCollapsed ? '▸' : '▾';
      });
    }
    const expandBtn = div.querySelector('.search-result-expand');
    if (expandBtn) {
      expandBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const full = expandBtn.dataset.full;
        const resultsEl = div.querySelector('.search-results');
        if (expandBtn.textContent === '展开全部') {
          resultsEl.textContent = full;
          expandBtn.textContent = '收起';
        } else {
          resultsEl.textContent = full.substring(0, 300) + '...';
          expandBtn.textContent = '展开全部';
        }
      });
    }
  }

  if (tools && tools.length > 0) {
    const header = div.querySelector('.tool-summary-header');
    const body = div.querySelector('.tool-summary-body');
    const toggle = div.querySelector('.tool-summary-toggle');
    if (header && body && toggle) {
      header.addEventListener('click', () => {
        const isCollapsed = body.classList.toggle('collapsed');
        toggle.textContent = isCollapsed ? '▸' : '▾';
      });
    }
    div.querySelectorAll('.tool-result-expand').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const full = btn.dataset.full;
        const textEl = btn.previousElementSibling;
        if (btn.textContent === '展开全部') {
          textEl.textContent = full;
          btn.textContent = '收起';
        } else {
          textEl.textContent = full.substring(0, 200) + '...';
          btn.textContent = '展开全部';
        }
      });
    });
  }

  if (showThought) {
    const thinkHeader = div.querySelector('.think-header');
    const thinkContent = div.querySelector('.think-content');
    const thinkToggle = div.querySelector('.think-toggle');
    if (thinkHeader && thinkContent && thinkToggle) {
      thinkHeader.addEventListener('click', () => {
        const isCollapsed = thinkContent.classList.toggle('collapsed');
        thinkToggle.textContent = isCollapsed ? '▸' : '▾';
      });
    }
  }

  return div;
}

function renderCompressedHistory(summaryContent, meta, targetContainer) {
  const container = targetContainer || getActiveContainer().element;
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'message system';

  const roundsHtml = meta.rounds.map((r) => {
    const metaPart = r.meta;
    const hasThought = metaPart && metaPart.thought;
    const hasTools = metaPart && metaPart.tools && metaPart.tools.length > 0;
    const hasSearch = metaPart && metaPart.search;
    const hasAny = hasThought || hasTools || hasSearch;

    if (!hasAny) return '';

    let metaHtml = '';
    if (hasThought) {
      metaHtml += `
        <div class="compressed-think">
          <div class="compressed-think-header">
            <span>思考过程</span>
            <span class="compressed-think-toggle">▸</span>
          </div>
          <div class="compressed-think-content collapsed">${escapeHtml(metaPart.thought)}</div>
        </div>`;
    }
    if (hasSearch) {
      metaHtml += `
        <div class="compressed-search">
          <div class="compressed-search-header">
            <span>联网搜索: ${escapeHtml(metaPart.search.scenario || '')}</span>
            <span class="compressed-search-toggle">▸</span>
          </div>
          <div class="compressed-search-content collapsed">
            <div>关键词: ${escapeHtml(metaPart.search.query || '')}</div>
            <div class="compressed-search-results">${escapeHtml((metaPart.search.results || '').substring(0, 200))}</div>
          </div>
        </div>`;
    }
    if (hasTools) {
      metaHtml += `
        <div class="compressed-tools">
          <div class="compressed-tools-header">
            <span>工具调用 (${metaPart.tools.length} 个)</span>
            <span class="compressed-tools-toggle">▸</span>
          </div>
          <div class="compressed-tools-content collapsed">
            ${metaPart.tools.map(t => `<div class="compressed-tool-item">${escapeHtml(t.name)}${t.error ? ' <span class="tool-error-badge">错误</span>' : ''}</div>`).join('')}
          </div>
        </div>`;
    }

    return `<div class="compressed-round">
      <div class="compressed-round-header">
        <span class="compressed-round-title">历史轮次: ${escapeHtml(r.user)}</span>
      </div>
      ${metaHtml}
    </div>`;
  }).filter(h => h).join('');

  div.innerHTML = `
    <div class="avatar">📋</div>
    <div class="message-body">
      <div class="compressed-summary">
        <div class="compressed-summary-header">
          <span>历史对话已压缩</span>
          <span class="compressed-summary-toggle">▸</span>
        </div>
        <div class="compressed-summary-content collapsed">
          <div class="compressed-summary-text">${escapeHtml(summaryContent)}</div>
          ${roundsHtml ? `
            <div class="compressed-rounds-label">包含 ${meta.rounds.length} 轮历史记录的元数据</div>
            ${roundsHtml}
          ` : ''}
        </div>
      </div>
    </div>
  `;
  container.appendChild(div);

  const summaryHeader = div.querySelector('.compressed-summary-header');
  const summaryContentEl = div.querySelector('.compressed-summary-content');
  const summaryToggle = div.querySelector('.compressed-summary-toggle');
  if (summaryHeader && summaryContentEl && summaryToggle) {
    summaryHeader.addEventListener('click', () => {
      const collapsed = summaryContentEl.classList.toggle('collapsed');
      summaryToggle.textContent = collapsed ? '▸' : '▾';
    });
  }

  div.querySelectorAll('.compressed-think-header').forEach(header => {
    const content = header.nextElementSibling;
    const toggle = header.querySelector('.compressed-think-toggle');
    if (toggle) {
      header.addEventListener('click', (e) => {
        e.stopPropagation();
        const collapsed = content.classList.toggle('collapsed');
        toggle.textContent = collapsed ? '▸' : '▾';
      });
    }
  });

  div.querySelectorAll('.compressed-search-header').forEach(header => {
    const content = header.nextElementSibling;
    const toggle = header.querySelector('.compressed-search-toggle');
    if (toggle) {
      header.addEventListener('click', (e) => {
        e.stopPropagation();
        const collapsed = content.classList.toggle('collapsed');
        toggle.textContent = collapsed ? '▸' : '▾';
      });
    }
  });

  div.querySelectorAll('.compressed-tools-header').forEach(header => {
    const content = header.nextElementSibling;
    const toggle = header.querySelector('.compressed-tools-toggle');
    if (toggle) {
      header.addEventListener('click', (e) => {
        e.stopPropagation();
        const collapsed = content.classList.toggle('collapsed');
        toggle.textContent = collapsed ? '▸' : '▾';
      });
    }
  });

  scrollToBottom();
  return div;
}

function createStreamingMessage() {
  const container = getActiveContainer().element;
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="avatar">AI</div>
    <div class="content streaming-cursor"></div>
  `;
  container.appendChild(div);
  scrollToBottom();
  return div.querySelector('.content');
}

function renderContent(text) {
  if (!text) return '';

  const codeBlocks = [];
  const refSources = [];
  let html = escapeHtml(text);

  // 提取「【标题】(URL)」形式的参考来源，替换为占位符，末尾统一渲染紧凑来源区
  html = html.replace(/【([^】]+)】\s*\(((?:https?:)?\/\/[^\s)]+)\)/g, (_, title, url) => {
    const n = refSources.length + 1;
    refSources.push({ title: title.trim(), url: url.startsWith('//') ? 'https:' + url : url });
    return `\x00RF${n}\x00`;
  });

  // 兜底：提取 [标题](URL) 形式的参考来源（LLM 未遵循【】格式时的降级匹配）
  // 排除导航/操作类短链接文本
  const navPrefixes = /^(点击|查看|详情|这里|链接|更多|下载|原文|阅读|返回|首页|登录|注册|提交|发送|复制|分享|收藏|点赞|评论|next|click|here|link|more|download|read|back|home|login|submit|send|copy|share|like|reply)/i;
  html = html.replace(/\[([^\]]+)\]\(((?:https?:)?\/\/[^\s)]+)\)/g, (_, title, url) => {
    const t = title.trim();
    if (t.length <= 8 && navPrefixes.test(t)) return _; // 短导航词，原样保留
    const n = refSources.length + 1;
    refSources.push({ title: t, url: url.startsWith('//') ? 'https:' + url : url });
    return `\x00RF${n}\x00`;
  });

  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang: lang || 'text', code: code.trim() });
    return `\x00CB${idx}\x00`;
  });

  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');

  const lines = html.split('\n');
  const result = [];
  let inList = false;
  let listType = '';
  let inTable = false;
  let inBlockquote = false;
  let i = 0;

  while (i < lines.length) {
    let line = lines[i];

    if (/^\|.*\|$/.test(line.trim()) && (line.includes('|') && line.trim().split('|').length >= 2)) {
      if (!inTable) {
        if (inList) { result.push('</ul>'); inList = false; }
        if (inBlockquote) { result.push('</blockquote>'); inBlockquote = false; }
        result.push('<table>');
        inTable = true;
      }
      const cells = line.trim().replace(/^\||\|$/g, '').split('|');
      const isHeader = i + 1 < lines.length && /^\|[\s\-:]+\|$/.test(lines[i + 1].trim());
      const tag = isHeader ? 'th' : 'td';
      result.push('<tr>' + cells.map(c => `<${tag}>${c.trim()}</${tag}>`).join('') + '</tr>');
      if (isHeader) { i++; }
      i++;
      continue;
    } else if (inTable) {
      result.push('</table>');
      inTable = false;
    }

    if (/^&gt;\s?/.test(line)) {
      if (!inBlockquote) {
        if (inList) { result.push('</ul>'); inList = false; }
        result.push('<blockquote>');
        inBlockquote = true;
      }
      result.push('<p>' + line.replace(/^&gt;\s?/, '') + '</p>');
      i++;
      continue;
    } else if (inBlockquote) {
      result.push('</blockquote>');
      inBlockquote = false;
    }

    const hMatch = line.match(/^(#{1,3})\s+(.+)$/);
    if (hMatch) {
      if (inList) { result.push('</ul>'); inList = false; }
      const level = hMatch[1].length;
      result.push(`<h${level}>${hMatch[2]}</h${level}>`);
      i++;
      continue;
    }

    const ulMatch = line.match(/^[\-\*]\s+(.+)$/);
    if (ulMatch) {
      if (ulMatch[1].replace(/\x00RF\d+\x00/g, '').trim() === '') { i++; continue; }
      if (!inList || listType !== 'ul') {
        if (inList) result.push(listType === 'ul' ? '</ul>' : '</ol>');
        result.push('<ul>');
        inList = true;
        listType = 'ul';
      }
      result.push('<li>' + ulMatch[1] + '</li>');
      i++;
      continue;
    }

    const olMatch = line.match(/^\d+[\.\)]\s+(.+)$/);
    if (olMatch) {
      if (olMatch[1].replace(/\x00RF\d+\x00/g, '').trim() === '') { i++; continue; }
      if (!inList || listType !== 'ol') {
        if (inList) result.push(listType === 'ul' ? '</ul>' : '</ol>');
        result.push('<ol>');
        inList = true;
        listType = 'ol';
      }
      result.push('<li>' + olMatch[1] + '</li>');
      i++;
      continue;
    }

    if (inList) {
      result.push(listType === 'ul' ? '</ul>' : '</ol>');
      inList = false;
    }

    if (line.trim() === '') {
      i++;
      continue;
    }

    // 跳过纯来源占位符行 与 「信息来源：」标题行
    const onlyRefLine = line.replace(/\x00RF\d+\x00/g, '').trim();
    if (onlyRefLine === '' || /^(信息来源|资料来源|参考来源|引用来源|来源)[：:]\s*$/.test(line.trim())) {
      i++;
      continue;
    }

    result.push('<p>' + line + '</p>');
    i++;
  }

  if (inList) result.push(listType === 'ul' ? '</ul>' : '</ol>');
  if (inTable) result.push('</table>');
  if (inBlockquote) result.push('</blockquote>');

  html = result.join('\n');

  html = html.replace(/<p>\s*<\/p>/g, '');
  html = html.replace(/<p>\s*<p>/g, '<p>');

  html = html.replace(/\x00CB(\d+)\x00/g, (_, idx) => {
    const block = codeBlocks[parseInt(idx)];
    const escapedCode = escapeHtml(block.code);
    return `<div class="code-block"><div class="code-block-header"><span class="code-block-lang">${block.lang}</span><button class="code-block-copy" onclick="copyCodeBlock(this)">复制</button></div><pre><code class="language-${block.lang}">${escapedCode}</code></pre></div>`;
  });

  // 还原来源上标，并在末尾渲染紧凑来源卡片
  html = html.replace(/\x00RF(\d+)\x00/g, (_, n) => `<sup class="ref-sup">[${n}]</sup>`);

  if (refSources.length) {
    const palette = ['#1976d2', '#e53935', '#43a047', '#fb8c00', '#8e24aa', '#00897b', '#3949ab', '#d81b60'];
    const pills = refSources.map((s, i) => {
      const initial = (s.title.trim()[0] || '?').toUpperCase();
      const color = palette[i % palette.length];
      const safeUrl = s.url.replace(/"/g, '&quot;');
      const safeTitle = s.title.replace(/"/g, '&quot;');
      return `<a class="ref-pill" href="${safeUrl}" target="_blank" rel="noopener noreferrer" title="${safeTitle}">`
        + `<span class="ref-avatar" style="background:${color}">${initial}</span>`
        + `<span class="ref-title">${s.title}</span>`
        + `<span class="ref-ext">↗</span>`
        + `</a>`;
    }).join('');
    html += `\n<div class="ref-sources"><span class="ref-tag">来源</span>${pills}</div>`;
  }

  // 隐藏 document_output/.../文件名 完整路径：无论 LLM 是否在文本中泄露了
  // 「document_output/u_xxx/word_output/文件名.docx」这类路径，统一只保留文件名，
  // 避免暴露 public_id 与目录结构。文件名以「.扩展名」结尾（扩展名 1-12 位字母数字）。
  html = html.replace(
    /document_output[/\\][^<>\s'"]*?([^/\\<>\s'"]+\.[A-Za-z0-9]{1,12})/gi,
    '$1'
  );

  return html;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function copyCodeBlock(btn) {
  const codeBlock = btn.closest('.code-block');
  const code = codeBlock.querySelector('code').textContent;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = '复制'; }, 2000);
  }).catch(() => {
    btn.textContent = '失败';
    setTimeout(() => { btn.textContent = '复制'; }, 2000);
  });
}

// ===== 聊天 =====
let abortController = null;

function createAssistantContainer() {
  const container = getActiveContainer().element;
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = `
    <div class="avatar">AI</div>
    <div class="message-body">
      <div class="think-area hidden">
        <div class="think-header">
          <span class="think-status">思考中</span>
          <span class="think-time"></span>
          <span class="think-toggle">▸</span>
        </div>
        <div class="think-content"></div>
      </div>
      <div class="search-area hidden">
        <div class="search-header">
          <span class="search-status">联网搜索</span>
          <span class="search-toggle">▸</span>
        </div>
        <div class="search-content collapsed"></div>
      </div>
      <div class="tool-summary hidden"></div>
      <div class="answer-area streaming-cursor"></div>
      <div class="output-files hidden"></div>
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();

  const stream = {
    container: div,
    searchEl: div.querySelector('.search-area'),
    searchContentEl: div.querySelector('.search-content'),
    searchHeaderEl: div.querySelector('.search-header'),
    searchToggleEl: div.querySelector('.search-toggle'),
    searchData: null,
    thinkEl: div.querySelector('.think-area'),
    thinkContentEl: div.querySelector('.think-content'),
    thinkHeaderEl: div.querySelector('.think-header'),
    thinkStatusEl: div.querySelector('.think-status'),
    thinkTimeEl: div.querySelector('.think-time'),
    thinkToggleEl: div.querySelector('.think-toggle'),
    thinkContent: '',
    thinkStartTime: null,
    thinkDone: false,
    toolSummaryEl: div.querySelector('.tool-summary'),
    answerEl: div.querySelector('.answer-area'),
    outputFilesEl: div.querySelector('.output-files'),
    answerContent: '',
    tools: [],
    hasTools: false,
  };

  stream.thinkHeaderEl.addEventListener('click', () => {
    const content = stream.thinkContentEl;
    const isHidden = content.classList.toggle('collapsed');
    stream.thinkToggleEl.textContent = isHidden ? '▸' : '▾';
  });

  stream.searchHeaderEl.addEventListener('click', () => {
    const content = stream.searchContentEl;
    const isHidden = content.classList.toggle('collapsed');
    stream.searchToggleEl.textContent = isHidden ? '▸' : '▾';
  });

  if (state.thinkingMode === 'high') {
    stream.thinkEl.classList.remove('hidden');
    stream.thinkStartTime = Date.now();
  }

  // 保存 stream 引用到 session 容器，以便切换时保持状态
  const sid = state.currentSessionId || '__default__';
  if (state.sessionContainers[sid]) {
    state.sessionContainers[sid].stream = stream;
  }

  return stream;
}

function renderToolSummary(stream, tools) {
  if (!tools || tools.length === 0) return;
  stream.hasTools = true;
  stream.tools = tools;

  const errorCount = tools.filter(t => t.error).length;
  const errorBadge = errorCount > 0 ? ` <span class="tool-error-badge">${errorCount} 个错误</span>` : '';

  stream.toolSummaryEl.innerHTML = `
    <div class="tool-summary-header">
      <span class="tool-summary-title">工具调用 (${tools.length} 个)${errorBadge}</span>
      <span class="tool-summary-toggle">▸</span>
    </div>
      <div class="tool-summary-body collapsed">
        ${tools.map((t, i) => `
          <div class="tool-item${t.error ? ' error' : ''}${t.skipped ? ' skipped' : ''}">
            <div class="tool-item-header">
              <span class="tool-item-name">${escapeHtml(t.name)}</span>
              <span class="tool-item-index">#${i + 1}</span>
            </div>
            <div class="tool-item-args">
              <span class="tool-item-label">参数：</span>
              <code>${escapeHtml(JSON.stringify(t.arguments, null, 2))}</code>
            </div>
            <div class="tool-item-result">
              <span class="tool-item-label">结果：</span>
              <span class="tool-result-text${t.error ? ' error' : ''}${t.skipped ? ' skipped' : ''}">${escapeHtml(t.result.length > 200 ? t.result.substring(0, 200) + '...' : t.result)}</span>
              ${t.result.length > 200 ? `<button class="tool-result-expand" data-full="${escapeHtml(t.result)}">展开全部</button>` : ''}
            </div>
          </div>
        `).join('')}
      </div>
  `;

  stream.toolSummaryEl.classList.remove('hidden');

  const header = stream.toolSummaryEl.querySelector('.tool-summary-header');
  const body = stream.toolSummaryEl.querySelector('.tool-summary-body');
  const toggle = stream.toolSummaryEl.querySelector('.tool-summary-toggle');

  header.addEventListener('click', () => {
    const isCollapsed = body.classList.toggle('collapsed');
    toggle.textContent = isCollapsed ? '▸' : '▾';
  });

  stream.toolSummaryEl.querySelectorAll('.tool-result-expand').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const full = btn.dataset.full;
      const textEl = btn.previousElementSibling;
      if (btn.textContent === '展开全部') {
        textEl.textContent = full;
        btn.textContent = '收起';
      } else {
        textEl.textContent = full.substring(0, 200) + '...';
        btn.textContent = '展开全部';
      }
    });
  });
}

// ===== 产出文件卡片（对话内可见可下载）=====
const OUTPUT_TEXT_EXTS = ['txt','md','csv','json','xml','html','css','js','py','log','yaml','yml','ini','cfg','toml'];
const OUTPUT_PREVIEW_EXTS = [...OUTPUT_TEXT_EXTS, 'png','jpg','jpeg','gif','bmp','svg','webp','ico','pdf'];

function outputFileIcon(ext) {
  const map = {
    txt:'📄', md:'📝', csv:'📊', json:'🔧', xml:'📄', html:'🌐', css:'🎨', js:'📜', py:'🐍',
    pdf:'📕', png:'🖼️', jpg:'🖼️', jpeg:'🖼️', gif:'🖼️', bmp:'🖼️', svg:'🖼️', webp:'🖼️', ico:'🖼️',
    doc:'📘', docx:'📘', xls:'📗', xlsx:'📗', ppt:'📙', pptx:'📙'
  };
  return map[ext] || '📄';
}

function formatFileSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

// 将正则特殊字符转义，用于构建安全的正则
function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// 将聊天文本中出现的「已产出文件名」转为可点击下载链接。
// 仅处理纯文本节点，跳过 <a>/<code>/<pre>/<table> 等内部文本，避免破坏代码块或已有链接。
function _linkifyFileNames(root) {
  if (!root || !(_sessionFiles instanceof Map) || _sessionFiles.size === 0) return;
  const map = _sessionFiles;
  // 原始文件名（用于后缀匹配），按长度降序避免子串优先
  const rawNames = [...map.keys()].sort((a, b) => b.length - a.length);
  const escapedNames = rawNames.map(escapeRegExp);
  if (rawNames.length === 0) return;
  // 匹配：（可选的 document_output/.../ 前缀）+ 已知文件名。
  // 替换为仅显示文件名的下载链接，隐藏真实目录路径（含 public_id），避免泄露用户目录结构。
  const alts = escapedNames.map(n => '(?:document_output[/\\\\][^\\s\'"]*?)?(' + n + ')');
  const re = new RegExp(alts.join('|'), 'g');
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      if (p.closest('a, code, pre, table, script, style, .output-files-header')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const textNodes = [];
  let n;
  while ((n = walker.nextNode())) textNodes.push(n);
  for (const tn of textNodes) {
    const txt = tn.nodeValue;
    if (!re.test(txt)) continue;
    re.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(txt))) {
      const full = m[0];
      // 从 m[0] 后缀判定命中的文件名（escaped 版无法直接做后缀比对）
      let name = null;
      for (const cand of rawNames) {
        if (full.endsWith(cand)) { name = cand; break; }
      }
      if (!name) { last = m.index + full.length; continue; }
      if (m.index > last) frag.appendChild(document.createTextNode(txt.slice(last, m.index)));
      const path = map.get(name);
      const a = document.createElement('a');
      a.href = '/api/files/download?path=' + encodeURIComponent(path);
      a.target = '_blank';
      a.rel = 'noopener';
      a.className = 'inline-file-link';
      a.textContent = name;  // 仅展示文件名，不暴露 document_output/.../public_id 路径
      frag.appendChild(a);
      last = m.index + full.length;
    }
    if (last < txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
    tn.parentNode.replaceChild(frag, tn);
  }
}

function renderOutputFiles(stream, files) {
  if (!files || files.length === 0) return;
  const el = stream.outputFilesEl;
  if (!el) return;
  stream.hasOutputFiles = true;

  const cards = files.map(f => {
    const ext = f.ext || '';
    const previewable = OUTPUT_PREVIEW_EXTS.includes(ext);
    const previewBtn = previewable
      ? `<button class="of-preview" data-path="${escapeHtml(f.path)}" data-name="${escapeHtml(f.name)}">预览</button>` : '';
    const sizeStr = formatFileSize(f.size);
    const meta = [ext ? ext.toUpperCase() : 'FILE', sizeStr].filter(Boolean).join(' · ');
    return `<div class="output-file-card">
      <span class="of-icon">${outputFileIcon(ext)}</span>
      <div class="of-info">
        <a class="of-name of-name-link" href="/api/files/download?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener" title="点击下载">${escapeHtml(f.name)}</a>
        <div class="of-meta">${escapeHtml(meta)}</div>
      </div>
      <div class="of-actions">
        ${previewBtn}
        <a class="of-download" href="/api/files/download?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener">下载</a>
      </div>
    </div>`;
  }).join('');

  el.innerHTML = `<div class="output-files-header"><span class="of-title">📎 产出文件 (${files.length})</span></div>${cards}`;
  el.classList.remove('hidden');

  el.querySelectorAll('.of-preview').forEach(btn => {
    btn.addEventListener('click', () => previewOutputFile(btn.dataset.path, btn.dataset.name));
  });
}

async function previewOutputFile(path, filename) {
  const modal = $('#modal-cloud-files');
  if (!modal) {
    showToast('预览组件未加载', 'error');
    return;
  }
  const body = modal.querySelector('.modal-body');
  if (body) body.innerHTML = '<p class="loading-text">加载预览...</p>';
  // 预览模式：放大弹窗
  modal.querySelector('.modal-content').classList.add('preview-mode');
  openModal('modal-cloud-files');
  try {
    const res = await fetch(`/api/files/preview?path=${encodeURIComponent(path)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (!body) return;

    // 返回按钮独占一行（置于标题上方），标题独立一行，内容区自适应填充并滚动
    const backBtn = `<button id="preview-back-btn" class="btn-preview-back">← 返回文件列表</button>`;
    const titleRow = `<div class="cloud-preview-head"><strong>${escapeHtml(filename)}</strong></div>`;
    const wrap = (inner) => `<div class="cloud-preview-panel">${backBtn}${titleRow}<div class="cloud-preview-content">${inner}</div></div>`;

    // ---- 文本预览（txt/md/json/py 等） ----
    if (data.type === 'text') {
      body.innerHTML = wrap(`<pre class="preview-code">${escapeHtml(data.content)}</pre>`);
    }
    // ---- CSV 表格预览 ----
    else if (data.type === 'csv_table') {
      body.innerHTML = wrap(`<p class="preview-meta">${data.rows} 行 × ${data.cols} 列</p><div class="preview-csv-scroll">${data.html}</div>`);
    }
    // ---- docx 文本提取预览 ----
    else if (data.type === 'docx_text') {
      body.innerHTML = wrap(`<span class="badge-source badge-source-generated">Word 文档</span><pre class="preview-code">${escapeHtml(data.content)}</pre>`);
    }
    // ---- xlsx 字符串预览 ----
    else if (data.type === 'xlsx_preview') {
      body.innerHTML = wrap(`<span class="badge-source badge-source-generated">Excel 表格</span><pre class="preview-code" style="white-space:pre-wrap;">${escapeHtml(data.content)}</pre><p class="preview-meta">提示：仅显示文本内容，完整数据请下载文件查看</p>`);
    }
    // ---- 图片预览 ----
    else if (data.type === 'image') {
      body.innerHTML = wrap(`<div class="preview-image-wrap"><img src="/api/files/download?path=${encodeURIComponent(path)}&inline=true" class="preview-image"></div>`);
    }
    // ---- PDF 预览 ----
    else if (data.type === 'pdf') {
      body.innerHTML = wrap(`<iframe src="/api/files/download?path=${encodeURIComponent(path)}&inline=true" class="preview-pdf"></iframe>`);
    }
    // ---- 不支持（带提示） ----
    else {
      const hint = data.hint ? escapeHtml(data.hint) : '该文件类型暂不支持在线预览';
      body.innerHTML = `<div class="cloud-preview-panel">${backBtn}
        <div class="preview-unsupported">
          <strong>${escapeHtml(filename)}</strong><br><br>
          <span style="color:var(--text-muted);font-size:14px;">${hint}</span><br><br>
          <a class="btn-download" href="/api/files/download?path=${encodeURIComponent(path)}" target="_blank" rel="noopener"
             style="display:inline-flex;padding:8px 20px;font-size:14px;">下载文件</a>
        </div></div>`;
    }
  } catch (e) {
    if (body) body.innerHTML = `<p style="color:var(--danger)">预览失败: ${escapeHtml(e.message)}</p>`;
  }
}

/** 从预览状态返回文件列表 */
function returnToFileList() {
  const modal = $('#modal-cloud-files');
  if (modal) {
    const content = modal.querySelector('.modal-content');
    if (content) content.classList.remove('preview-mode');
  }
  loadCloudFiles();
}

// ===== 敏感操作审批卡片 =====
const RISK_LABELS = { safe: '安全', read: '只读', write: '写入', exec: '执行', admin: '管理员' };

function renderApprovalCard(stream, parsed) {
  // 渲染到底部操作栏 #approval-bar（而非消息内）
  const bar = $('#approval-bar');
  if (!bar) return;
  // 防止重连重放时重复渲染同一分组
  if (bar.querySelector('.approval-card[data-group="' + parsed.group_id + '"]')) return;

  bar.classList.remove('hidden');

  const card = document.createElement('div');
  card.className = 'approval-card';
  card.dataset.group = parsed.group_id;
  card.dataset.session = parsed.session_id || '';

  let itemsHtml = '';
  (parsed.items || []).forEach(it => {
    const risk = it.risk || 'write';
    const argsStr = Object.keys(it.args || {}).length ? JSON.stringify(it.args, null, 2) : '(无参数)';
    itemsHtml += `
      <div class="approval-item" data-item="${escapeHtml(it.item_id)}">
        <div class="approval-item-head">
          <span class="approval-tool">${escapeHtml(it.tool)}</span>
          <span class="risk-badge risk-${escapeHtml(risk)}">${RISK_LABELS[risk] || risk}</span>
        </div>
        <div class="approval-desc">${escapeHtml(it.desc || '')}</div>
        <details class="approval-args"><summary>参数</summary><pre>${escapeHtml(argsStr)}</pre></details>
        <div class="approval-actions">
          <button class="btn-skip" data-dec="skip">跳过</button>
          <button class="btn-approve" data-dec="approve">允许</button>
          <button class="btn-reject" data-dec="reject">拒绝</button>
        </div>
        <div class="approval-status"></div>
      </div>`;
  });

  card.innerHTML = `
    <div class="approval-card-header">
      <span class="approval-icon">⚠️</span>
      <span>检测到敏感操作，需要你确认</span>
    </div>
    <div class="approval-items">${itemsHtml}</div>`;
  bar.appendChild(card);

  const sid = parsed.session_id;
  const gid = parsed.group_id;

  // 每项三个按钮：点击即单项提交并执行
  card.querySelectorAll('.approval-item').forEach(item => {
    const itemId = item.dataset.item;
    const status = item.querySelector('.approval-status');
    const btns = item.querySelectorAll('.approval-actions button');
    btns.forEach(b => b.addEventListener('click', async () => {
      const dec = b.dataset.dec;
      // 立即禁用该项所有按钮，防止重复点击
      btns.forEach(x => { x.disabled = true; x.classList.remove('chosen'); });
      b.classList.add('chosen');
      if (status) status.textContent = '提交中...';
      try {
        const res = await fetch(`/api/chat/${sid}/approve`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ group_id: gid, decisions: [{ item_id: itemId, decision: dec }] }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          if (status) status.textContent = '提交失败：' + (err.detail || res.status);
          btns.forEach(x => { x.disabled = false; });
          return;
        }
        const label = dec === 'skip' ? '已跳过' : dec === 'approve' ? '已允许' : '已拒绝';
        if (status) {
          status.textContent = label + '，正在执行...';
          status.className = 'approval-status dec-' + dec;
        }
        item.classList.add('decided', 'dec-' + dec);
      } catch (e) {
        if (status) status.textContent = '提交异常：' + e.message;
        btns.forEach(x => { x.disabled = false; });
      }
    }));
  });
}

function updateApprovalCardResolved(stream, parsed) {
  // 底部审批栏也响应 resolved 事件并隐藏
  const bar = $('#approval-bar');
  const card = bar?.querySelector('.approval-card[data-group="' + parsed.group_id + '"]');
  if (card) {
    setTimeout(() => {
      if (bar) { bar.classList.add('hidden'); bar.innerHTML = ''; }
    }, 600);
    return;
  }
  // 兜底：历史消息内嵌卡片（只更新状态，不隐藏）
  const legacyCard = stream.container?.querySelector('.approval-card[data-group="' + parsed.group_id + '"]');
  if (!legacyCard) return;
  legacyCard.classList.add('resolved');
  const decisions = parsed.decisions || {};
  legacyCard.querySelectorAll('.approval-item').forEach(item => {
    const dec = decisions[item.dataset.item];
    if (dec) {
      const head = item.querySelector('.approval-item-head');
      if (head && !head.querySelector('.resolved-badge')) {
        const badge = document.createElement('span');
        badge.className = 'resolved-badge ' + (dec === 'approve' ? 'ok' : 'no');
        badge.textContent = dec === 'approve' ? '已允许' : '已拒绝';
        head.appendChild(badge);
      }
    }
  });
  const hint = legacyCard.querySelector('.approval-hint');
  if (hint) hint.textContent = '已确认，正在继续...';
  card.querySelectorAll('button').forEach(b => b.disabled = true);
}

function renderApprovalSkipped(stream, parsed) {
  const container = stream.container;
  if (!container) return;
  const card = document.createElement('div');
  card.className = 'approval-skipped';
  let itemsHtml = '';
  (parsed.items || []).forEach(it => {
    itemsHtml += `<li><b>${escapeHtml(it.tool)}</b>：${escapeHtml(it.desc || '')}</li>`;
  });
  card.innerHTML = `
    <div class="skipped-head"><span>⏩</span><span>${escapeHtml(parsed.reason || '本会话为「完全访问权限」，敏感操作直接执行')}</span></div>
    <ul>${itemsHtml}</ul>`;
  container.appendChild(card);
  scrollToBottom();
}

function renderSearchArea(stream) {
  if (!stream.searchData) return;

  const el = stream.searchEl;
  if (el.classList.contains('hidden')) {
    el.classList.remove('hidden');
  }

  const d = stream.searchData;
  const resultsPreview = (d.results || '').length > 300
    ? escapeHtml(d.results.substring(0, 300)) + '...'
    : escapeHtml(d.results || '');

  stream.searchContentEl.innerHTML = `
    <div class="search-info">
      <div class="search-info-item"><span class="search-label">场景：</span>${escapeHtml(d.scenario || '通用搜索')}</div>
      <div class="search-info-item"><span class="search-label">关键词：</span>${escapeHtml(d.query || '')}</div>
    </div>
    <div class="search-results">${resultsPreview}</div>
    ${(d.results || '').length > 300 ? `<button class="search-result-expand" data-full="${escapeHtml(d.results)}">展开全部</button>` : ''}
  `;

  const expandBtn = stream.searchContentEl.querySelector('.search-result-expand');
  if (expandBtn) {
    expandBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const full = expandBtn.dataset.full;
      const resultsEl = stream.searchContentEl.querySelector('.search-results');
      if (expandBtn.textContent === '展开全部') {
        resultsEl.textContent = full;
        expandBtn.textContent = '收起';
      } else {
        resultsEl.textContent = full.substring(0, 300) + '...';
        expandBtn.textContent = '展开全部';
      }
    });
  }

  scrollToBottom();
}

function updateThinkArea(stream) {
  if (state.thinkingMode !== 'high') return;

  if (stream.thinkEl.classList.contains('hidden')) {
    stream.thinkEl.classList.remove('hidden');
  }

  let html = '';
  const lines = stream.thinkContent.split('\n');
  for (const line of lines) {
    if (!line.trim()) continue;
    html += `<div class="think-line">${escapeHtml(line)}</div>`;
  }
  stream.thinkContentEl.innerHTML = html;
  stream.thinkContentEl.scrollTop = stream.thinkContentEl.scrollHeight;

  scrollToBottom();
}

function finalizeThinkArea(stream) {
  if (!stream.thinkStartTime || stream.thinkDone) return;
  stream.thinkDone = true;

  const elapsed = ((Date.now() - stream.thinkStartTime) / 1000).toFixed(2);
  stream.thinkStatusEl.textContent = `已思考（${elapsed}s）`;
  stream.thinkTimeEl.textContent = '';

  stream.thinkContentEl.classList.add('collapsed');
  stream.thinkToggleEl.textContent = '▸';

  if (stream.searchData) {
    renderSearchArea(stream);
  }
}

function updateAnswerArea(stream) {
  stream.answerEl.innerHTML = renderContent(stream.answerContent);
  stream.answerEl.classList.add('streaming-cursor');
  scrollToBottom();
}

// ===== 可复用的 SSE 事件处理 =====

function handleStreamEvent(stream, parsed) {
  // 返回: 'done' | 'error' | null
  if (parsed.type === 'error') {
    stream.answerEl.textContent = '\u9519\u8bef: ' + parsed.content;
    stream.answerEl.classList.remove('streaming-cursor');
    showToast(parsed.content, 'error');
    return 'error';
  }
  if (parsed.type === 'web_search') {
    stream.searchData = {
      query: parsed.query,
      scenario: parsed.scenario,
      results: parsed.results,
    };
    if (!stream.thinkStartTime) stream.thinkStartTime = Date.now();
    stream.thinkContent += `\n\u8054\u7f51\u641c\u7d22: ${parsed.query}\n\u573a\u666f: ${parsed.scenario}\n`;
    updateThinkArea(stream);
    return null;
  }
  if (parsed.type === 'thought') {
    if (!stream.thinkStartTime) stream.thinkStartTime = Date.now();
    stream.thinkContent += parsed.content;
    updateThinkArea(stream);
    return null;
  }
  if (parsed.type === 'tool_call') {
    if (!stream.thinkStartTime) stream.thinkStartTime = Date.now();
    stream.tools.push({
      name: parsed.name,
      arguments: parsed.arguments || {},
      result: null,
      error: false,
    });
    stream.hasTools = true;
    stream.thinkContent += `\n\u8c03\u7528\u5de5\u5177: ${parsed.name}\n\u53c2\u6570: ${JSON.stringify(parsed.arguments, null, 2)}\n`;
    updateThinkArea(stream);
    return null;
  }
  if (parsed.type === 'tool_result') {
    const tool = stream.tools.find(t => t.name === parsed.name && t.result === null);
    if (tool) {
      tool.result = parsed.content || '';
      tool.error = tool.result.startsWith('[\u6c99\u7bb1\u6267\u884c\u5931\u8d25]') ||
                   tool.result.startsWith('[\u6c99\u7bb1\u6267\u884c\u8d85\u65f6]') ||
                   tool.result.startsWith('[\u6c99\u7bb1\u5f02\u5e38]') ||
                   tool.result.startsWith('[\u5de5\u5177\u6267\u884c\u5f02\u5e38]');
    }
    stream.thinkContent += `\u5de5\u5177\u7ed3\u679c: ${parsed.content || '(\u7a7a)'}\n`;
    updateThinkArea(stream);
    return null;
  }
  if (parsed.type === 'approval_required') {
    renderApprovalCard(stream, parsed);
    return null;
  }
  if (parsed.type === 'approval_resolved') {
    updateApprovalCardResolved(stream, parsed);
    return null;
  }
  if (parsed.type === 'approval_skipped') {
    renderApprovalSkipped(stream, parsed);
    return null;
  }
  if (parsed.type === 'tool_summary') {
    renderToolSummary(stream, parsed.tools);
    return null;
  }
  if (parsed.type === 'files_created') {
    renderOutputFiles(stream, parsed.files);
    // 记录本会话已产出文件：文件名 -> 路径，用于把助手消息中的文件名转成下载链接
    (parsed.files || []).forEach(f => { if (f && f.name && f.path) _sessionFiles.set(f.name, f.path); });
    _linkifyFileNames(stream.answerEl);
    return null;
  }
  if ((parsed.type === 'content' || parsed.type === 'token') && parsed.content) {
    if (!stream.thinkDone && stream.thinkStartTime) {
      finalizeThinkArea(stream);
    }
    stream.answerContent += parsed.content;
    updateAnswerArea(stream);
    return null;
  }
  if (parsed.type === 'done') {
    stream.answerEl.classList.remove('streaming-cursor');
    if (!stream.answerContent) {
      stream.answerEl.textContent = parsed.content || '(\u65e0\u54cd\u5e94)';
    }
    if (stream.thinkStartTime && !stream.thinkDone) {
      finalizeThinkArea(stream);
    }
    if (stream.searchData && stream.searchEl.classList.contains('hidden')) {
      renderSearchArea(stream);
    }
    return 'done';
  }
  return null;
}

async function readSSEStream(res, stream) {
  // 读取 SSE 流，返回 'done' | 'error' | null
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

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
        const result = handleStreamEvent(stream, parsed);
        if (result === 'done' || result === 'error') {
          return result;
        }
      } catch (e) {
        // ignore parse errors
      }
    }
  }

  // 流结束但没有显式 done 事件
  stream.answerEl.classList.remove('streaming-cursor');
  if (!stream.answerContent) {
    stream.answerEl.textContent = '(\u65e0\u54cd\u5e94)';
  }
  return null;
}

// ===== 订阅正在运行的后台任务（用于切换 session 后重连） =====

async function subscribeToTask(sessionId) {
  const taskInfo = state.taskStatuses[sessionId];
  if (!taskInfo) return;

  // 添加用户消息到 DOM
  const sc = state.sessionContainers[sessionId];
  if (sc) {
    const welcome = sc.element.querySelector('.welcome-message');
    if (welcome) welcome.remove();
    appendMessage('user', taskInfo.user_message, null, sc.element);
  }

  // 创建助手容器（确保 currentSessionId 正确）
  const prevSessionId = state.currentSessionId;
  state.currentSessionId = sessionId;
  const stream = createAssistantContainer();
  state.currentSessionId = prevSessionId;

  // 不设置 state.isStreaming — 允许用户在其他 session 发送消息
  // 仅标记当前正在订阅任务
  state.subscribingSessionId = sessionId;
  if (state.currentSessionId === sessionId) {
    $('#btn-send').classList.add('streaming');
    $('#btn-send').title = '\u4efb\u52a1\u6267\u884c\u4e2d';
  }

  try {
    const res = await fetch(`/api/chat/subscribe/${sessionId}`);
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }

    await readSSEStream(res, stream);

    // 后续处理：本地立即落状态
    if (state.taskStatuses[sessionId]) {
      state.taskStatuses[sessionId].status = 'completed';
    }
    if (state.currentSessionId !== sessionId) {
      state.completedBgSessions.add(sessionId);
    } else {
      state.acknowledgedTasks.add(sessionId);
      _saveAcknowledged();
      state.completedBgSessions.delete(sessionId);
    }
    await loadSessions();
    if (state.sessionContainers[sessionId]) {
      state.sessionContainers[sessionId].stream = null;
    }
  } catch (e) {
    console.error('\u8ba2\u9605\u4efb\u52a1\u5931\u8d25:', e);
    stream.answerEl.textContent = '\u8ba2\u9605\u5931\u8d25: ' + e.message;
    stream.answerEl.classList.remove('streaming-cursor');
  } finally {
    state.subscribingSessionId = null;
    if (state.currentSessionId === sessionId) {
      $('#btn-send').classList.remove('streaming');
      $('#btn-send').title = '\u53d1\u9001';
    }
  }
}

async function sendMessage() {
  const input = $('#chat-input');
  const message = input.value.trim();
  if (!message) return;
  // 只阻止在当前正在流式输出的 session 发送消息（允许在其他 session 发送）
  if (state.isStreaming && state.streamingSessionId === state.currentSessionId) {
    showToast('当前任务正在执行中，请等待完成或点击发送按钮暂停', 'warning');
    return;
  }
  // 检查当前 session 是否有后台运行的任务
  const _taskInfo = state.taskStatuses[state.currentSessionId];
  if (_taskInfo && _taskInfo.status === 'running') {
    showToast('该会话有正在执行的任务，请等待完成', 'warning');
    return;
  }

  if (/^\/agent\s+thought\s+(on|off)/i.test(message)) {
    showToast('请使用输入框下方的「思考过程」按钮来切换', 'info');
    return;
  }

  input.value = '';
  input.style.height = 'auto';
  hideCommandSuggestions();

  if (!state.currentSessionId) {
    try {
      const s = await API.post('/api/sessions', { title: message.substring(0, 30) });
      state.currentSessionId = s.id;
      _saveCurrentSessionId();
      await loadSessions();
      updateChatTitle();
      refreshTrustState();
    } catch (e) {
      showToast('创建会话失败: ' + e.message, 'error');
      return;
    }
  }

  appendMessage('user', message);

  const currentAttachments = [...state.attachedFiles];
  if (currentAttachments.length > 0) {
    const lastMsg = getActiveContainer().element.lastElementChild;
    if (lastMsg && lastMsg.classList.contains('user')) {
      const iconMap = { text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C' };
      const tags = currentAttachments.map(f => {
        const icon = iconMap[f.type] || 'F';
        return `<span class="msg-attach-tag"><span class="tag-icon">${icon}</span>${escapeHtml(f.filename)}</span>`;
      }).join('');
      const areaEl = lastMsg.querySelector('.answer-area');
      if (areaEl) {
        areaEl.insertAdjacentHTML('beforeend', `<div class="msg-attachments">${tags}</div>`);
      }
    }
    clearAttachedFiles();
  }

  const stream = createAssistantContainer();
  _sessionFiles = new Map();  // 新一轮对话，清空会话文件映射
  state.isStreaming = true;
  state.streamingSessionId = state.currentSessionId;
  $('#btn-send').classList.add('streaming');
  $('#btn-send').title = '停止生成';

  abortController = new AbortController();
  const streamSessionId = state.currentSessionId;

  // 乐观标记为运行中：立刻切走时也能看到转圈，不用等轮询
  state.taskStatuses[streamSessionId] = {
    status: 'running',
    user_message: message,
    started_at: Date.now() / 1000,
  };
  state.acknowledgedTasks.delete(streamSessionId);
  state.completedBgSessions.delete(streamSessionId);
  _saveAcknowledged();

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: state.currentSessionId,
        message: message,
        web_search: state.webSearch,
        show_thought: state.thinkingMode === 'high',
      }),
      signal: abortController.signal,
    });

    if (res.status === 409) {
      const err = await res.json();
      showToast(err.detail || '该会话有正在执行的任务', 'warning');
      // 移除空的助手容器
      stream.container.remove();
      return;
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || res.statusText);
    }

    await readSSEStream(res, stream);

    // 后续处理：本地立即落状态，不等轮询
    if (state.taskStatuses[streamSessionId]) {
      state.taskStatuses[streamSessionId].status = 'completed';
    }
    if (streamSessionId === state.currentSessionId) {
      // 用户就在这个 session，结果已可见，不留气泡
      state.acknowledgedTasks.add(streamSessionId);
      _saveAcknowledged();
      state.completedBgSessions.delete(streamSessionId);
    } else {
      state.completedBgSessions.add(streamSessionId);
    }
    if (state.backgroundStreamSessionId === streamSessionId) {
      state.backgroundStreamSessionId = null;
    }
    await loadSessions();
    if (state.sessionContainers[streamSessionId]) {
      state.sessionContainers[streamSessionId].stream = null;
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      stream.answerEl.classList.remove('streaming-cursor');
      if (!stream.answerContent) {
        stream.answerEl.textContent = '(\u5df2\u505c\u6b62\u751f\u6210)';
      }
      return;
    }
    stream.answerEl.textContent = '\u8bf7\u6c42\u5931\u8d25: ' + e.message;
    stream.answerEl.classList.remove('streaming-cursor');
    showToast('\u53d1\u9001\u5931\u8d25: ' + e.message, 'error');
  } finally {
    state.isStreaming = false;
    state.streamingSessionId = null;
    $('#btn-send').classList.remove('streaming');
    $('#btn-send').title = '\u53d1\u9001';
    abortController = null;
  }
}

async function stopGeneration() {
  const sessionId = state.streamingSessionId || state.subscribingSessionId;
  // 1. 中止前端 fetch 读取
  if (abortController) {
    abortController.abort();
  }
  // 2. 通知后端取消后台任务
  if (sessionId) {
    try {
      await fetch(`/api/chat/stop/${sessionId}`, { method: 'POST' });
    } catch (e) {
      // 忽略网络错误
    }
    // 3. 立即更新前端状态，隐藏运行指示器
    if (state.taskStatuses[sessionId]) {
      state.taskStatuses[sessionId].status = 'failed';
    }
    state.acknowledgedTasks.add(sessionId);
    _saveAcknowledged();
    state.completedBgSessions.delete(sessionId);
    renderSessions();
  }
}

// ===== 文件上传 =====
const UPLOAD_FILE_ICONS = {
  text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C',
};

function getFileChipIcon(fileType) {
  const cls = UPLOAD_FILE_ICONS[fileType] ? fileType : 'text';
  return `<span class="chip-icon ${cls}">${UPLOAD_FILE_ICONS[fileType] || 'F'}</span>`;
}

function renderFileChips() {
  const container = $('#file-chips');
  if (!state.attachedFiles || state.attachedFiles.length === 0) {
    container.classList.add('hidden');
    container.innerHTML = '';
    return;
  }
  container.classList.remove('hidden');
  container.innerHTML = state.attachedFiles.map((f, i) => `
    <div class="file-chip" title="${escapeHtml(f.summary || f.filename)}">
      ${getFileChipIcon(f.type)}
      <span class="chip-name">${escapeHtml(f.filename)}</span>
      <button class="chip-remove" data-idx="${i}" title="移除文件">&times;</button>
    </div>
  `).join('');

  container.querySelectorAll('.chip-remove').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.idx);
      const file = state.attachedFiles[idx];
      if (!file) return;
      try {
        await API.del(`/api/files/upload?session_id=${state.currentSessionId}&filename=${encodeURIComponent(file.filename)}`);
      } catch (_) {}
      state.attachedFiles.splice(idx, 1);
      renderFileChips();
    });
  });
}

async function loadAttachedFiles() {
  if (!state.currentSessionId) {
    state.attachedFiles = [];
    renderFileChips();
    return;
  }
  try {
    const data = await API.get(`/api/files/uploads?session_id=${state.currentSessionId}`);
    state.attachedFiles = data.files || [];
  } catch (_) {
    state.attachedFiles = [];
  }
  renderFileChips();
}

async function uploadFiles(fileList) {
  if (!state.currentSessionId) {
    try {
      const s = await API.post('/api/sessions', { title: '文件对话' });
      state.currentSessionId = s.id;
      await loadSessions();
    } catch (e) {
      showToast('创建会话失败: ' + e.message, 'error');
      return;
    }
  }

  const formData = new FormData();
  for (const file of fileList) {
    formData.append('files', file);
  }

  try {
    const res = await fetch(`/api/files/upload?session_id=${state.currentSessionId}`, {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '上传失败');

    if (data.errors && data.errors.length > 0) {
      data.errors.forEach(e => showToast(e, 'warning'));
    }
    if (data.uploaded && data.uploaded.length > 0) {
      state.attachedFiles = state.attachedFiles.concat(data.uploaded);
      showToast(`已上传 ${data.uploaded.length} 个文件`, 'success');
    }
    renderFileChips();
  } catch (e) {
    showToast('上传失败: ' + e.message, 'error');
  }
}

function clearAttachedFiles() {
  state.attachedFiles = [];
  renderFileChips();
}

// ===== 我的文件库（合并生成文件与上传文件）=====
let _cloudFiles = [];  // 当前用户文件库全量缓存（用于前端按类型/来源筛选）
let _sessionFiles = new Map();  // 当前会话已产出文件：文件名 -> 相对路径（用于把聊天文本中的文件名转成下载链接）

// 重建文件库 modal-body 骨架（预览模式会通过 innerHTML 覆盖整个 body，导致工具栏/表格丢失）
function _rebuildCloudFileSkeleton() {
  const modal = $('#modal-cloud-files');
  if (!modal) return;
  const body = modal.querySelector('.modal-body');
  if (!body) return;
  // 如果关键元素已存在，说明骨架完好，无需重建
  if ($('#cloud-search') && $('#cloud-table-body')) return;
  body.innerHTML = `
    <div class="cloud-toolbar">
      <input type="text" id="cloud-search" class="cloud-search-input" placeholder="搜索文件名...">
      <select id="cloud-type-filter" class="cloud-filter-select" title="按文档类型筛选">
        <option value="">全部类型</option>
      </select>
      <select id="cloud-source-filter" class="cloud-filter-select" title="按来源方式筛选">
        <option value="">全部来源</option>
        <option value="generated">平台生成</option>
        <option value="upload">用户上传</option>
      </select>
    </div>
    <div class="cloud-table-wrap">
      <table class="cloud-table">
        <thead>
          <tr>
            <th>文件名</th><th>所有者</th><th>来源</th><th>类型</th><th>大小</th><th>时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="cloud-table-body"><tr><td colspan="7" class="loading-text">加载中...</td></tr></tbody>
      </table>
    </div>`;
}

async function loadCloudFiles() {
  // 确保骨架存在（从预览返回时 innerHTML 已破坏原有结构）
  _rebuildCloudFileSkeleton();

  const search = $('#cloud-search') ? $('#cloud-search').value.trim() : '';
  const tbody = $('#cloud-table-body');
  if (!tbody) return; // 安全兜底：骨架重建失败时不崩溃
  tbody.innerHTML = '<tr><td colspan="6" class="loading-text">加载中...</td></tr>';

  try {
    const params = new URLSearchParams();
    if (search) params.set('search', search);
    // 加载文件库：普通用户仅自身文件；管理员返回全部用户文件（后端已按角色区分），表格含「所有者」列
    const data = await API.get(`/api/files/library?${params.toString()}`);
    _cloudFiles = data.files || [];
    _populateCloudTypeFilter();
    _applyCloudFilters();
  } catch (e) {
    const tb = $('#cloud-table-body');
    if (tb) tb.innerHTML = `<tr><td colspan="7" class="loading-text" style="color:var(--danger)">加载失败: ${escapeHtml(e.message)}</td></tr>`;
  }
}

// 根据已加载文件动态填充「文档类型」筛选项（保留当前选择）
function _populateCloudTypeFilter() {
  const sel = $('#cloud-type-filter');
  if (!sel) return;
  const prev = sel.value;
  const cats = [...new Set(_cloudFiles.map(f => f.category).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">全部类型</option>' +
    cats.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  sel.value = cats.includes(prev) ? prev : '';
}

// 按「文档类型」+「来源方式」+「文件名搜索」三重筛选并渲染
function _applyCloudFilters() {
  const typeSel = $('#cloud-type-filter');
  const srcSel = $('#cloud-source-filter');
  const typeFilter = typeSel ? typeSel.value : '';
  const srcFilter = srcSel ? srcSel.value : '';
  const filtered = _cloudFiles.filter(f => {
    if (typeFilter && f.category !== typeFilter) return false;
    if (srcFilter && f.source !== srcFilter) return false;
    return true;
  });
  renderCloudFiles(filtered);
}

function renderCloudFiles(files) {
  const tbody = $('#cloud-table-body');
  if (!files || files.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="loading-text">文件库为空</td></tr>';
    return;
  }

  const iconMap = {
    txt: 'T', md: 'M', csv: 'C', json: 'J', xml: 'X', html: 'H', css: 'C', js: 'JS',
    py: 'P', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', png: 'I', jpg: 'I', jpeg: 'I',
    gif: 'I', bmp: 'I', svg: 'I', webp: 'I', ico: 'I',
  };

  tbody.innerHTML = files.map((f, i) => {
    const sizeStr = f.size < 1024 ? `${f.size}B` : f.size < 1024 * 1024 ? `${(f.size / 1024).toFixed(1)}KB` : `${(f.size / (1024 * 1024)).toFixed(1)}MB`;
    const iconCls = iconMap[f.ext] ? f.ext : 'text';
    const icon = iconMap[f.ext] || 'F';
    const dpath = escapeHtml(f.path);
    const dname = escapeHtml(f.name);
    const ownerName = escapeHtml(f.owner_name || '我');
    // 来源徽标：用户上传（蓝）/ 平台生成（绿）
    const isUpload = f.source === 'upload';
    const sourceBadge = `<span class="badge badge-source ${isUpload ? 'badge-source-upload' : 'badge-source-generated'}">${isUpload ? '&#128229; 用户上传' : '&#129302; 平台生成'}</span>`;
    return `
      <tr>
        <td title="${dname}">
          <a class="cloud-file-link" href="/api/files/download?path=${dpath}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">
            <span class="picker-file-icon ${iconCls}" style="display:inline-flex;vertical-align:middle;margin-right:6px;">${icon}</span>
            ${dname}
          </a>
        </td>
        <td>${ownerName}</td>
        <td>${sourceBadge}</td>
        <td><span class="badge badge-type">${(f.ext || 'file').toUpperCase()}</span></td>
        <td>${sizeStr}</td>
        <td>${escapeHtml(f.mtime || '')}</td>
        <td class="cloud-ops">
          <button class="btn-preview" data-idx="${i}">预览</button>
          <a class="btn-download" href="/api/files/download?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener">下载</a>
          <button class="btn-rename" data-idx="${i}">重命名</button>
          <button class="btn-delete-cloud" data-idx="${i}">删除</button>
        </td>
      </tr>`;
  }).join('');

  tbody.querySelectorAll('.btn-preview').forEach(btn => {
    btn.addEventListener('click', () => {
      const f = files[parseInt(btn.dataset.idx)];
      if (f) previewOutputFile(f.path, f.name);
    });
  });

  tbody.querySelectorAll('.btn-rename').forEach(btn => {
    btn.addEventListener('click', async () => {
      const f = files[parseInt(btn.dataset.idx)];
      if (!f) return;
      const newName = prompt(`重命名文件「${f.name}」为：`, f.name);
      if (!newName || newName.trim() === f.name) return;
      try {
        await API.post('/api/files/rename', { path: f.path, new_name: newName.trim() });
        showToast('已重命名', 'success');
        loadCloudFiles();
      } catch (e) {
        showToast('重命名失败: ' + e.message, 'error');
      }
    });
  });

  tbody.querySelectorAll('.btn-delete-cloud').forEach(btn => {
    btn.addEventListener('click', async () => {
      const f = files[parseInt(btn.dataset.idx)];
      if (!f) return;
      const confirmed = await showConfirmDialog('删除文件', `确定要删除 "${f.name}" 吗？此操作不可撤销。`, '删除');
      if (!confirmed) return;
      try {
        await API.del(`/api/files?path=${encodeURIComponent(f.path)}`);
        showToast(`已删除 "${f.name}"`, 'success');
        loadCloudFiles();
      } catch (e) {
        showToast('删除失败: ' + e.message, 'error');
      }
    });
  });
}

// ===== 技能列表 =====
let _skillData = { system: [], user: [] };
let _skillTab = 'system';
let _skillRendered = [];   // 当前渲染出来的技能（供事件委托按下标取用）

async function loadSkills() {
  const container = $('#skills-container');
  container.innerHTML = '<p class="loading-text">加载中...</p>';
  try {
    const data = await API.get('/api/skills');
    _skillData.system = data.system_skills || [];
    _skillData.user = data.user_skills || [];
    renderSkillList();
  } catch (e) {
    container.innerHTML = `<p class="loading-text" style="color:var(--danger);">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function switchSkillTab(tab) {
  _skillTab = tab;
  $$('#skills-tabs .skill-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  $('#skill-search').value = '';
  renderSkillList();
}

function filterSkills() {
  renderSkillList();
}

function renderSkillList() {
  const container = $('#skills-container');
  const query = ($('#skill-search').value || '').trim().toLowerCase();
  const skills = _skillTab === 'system' ? _skillData.system : _skillData.user;

  let filtered = skills;
  if (query) {
    filtered = skills.filter(s => {
      const name = (s.name || '').toLowerCase();
      const desc = (s.description || '').toLowerCase();
      return name.includes(query) || desc.includes(query);
    });
  }

  if (filtered.length === 0) {
    container.innerHTML = '<p class="loading-text" style="padding:20px;">暂无技能</p>';
    return;
  }

  const isSystem = _skillTab === 'system';
  container.innerHTML = filtered.map((s, idx) => {
    const name = s.name || s.skill_name || '';
    const desc = s.description || '';
    const enabled = s.enabled !== false;
    const count = typeof s.scripts_count === 'number' ? s.scripts_count : null;
    const meta = [];
    if (count !== null) meta.push(`${count} 个脚本`);
    if (s.source === 'filesystem') meta.push('文件');
    return `
      <div class="skill-item${enabled ? '' : ' disabled'}" data-name="${escapeHtml(name)}">
        <div class="skill-info">
          <div class="skill-name">
            ${escapeHtml(name)}
            ${meta.length ? `<span class="skill-meta">${escapeHtml(meta.join(' · '))}</span>` : ''}
          </div>
          ${desc ? `<div class="skill-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</div>` : ''}
        </div>
        <div class="skill-actions">
          <button class="btn-preview" data-skill-action="preview" data-idx="${idx}">预览</button>
          <button class="skill-toggle ${enabled ? 'on' : ''}" data-skill-action="toggle" data-idx="${idx}"
                  title="${enabled ? '点击禁用' : '点击启用'}"></button>
        </div>
      </div>
    `;
  }).join('');

  // 事件委托：技能名可能含引号/反斜杠，用 inline onclick 拼字符串会破坏语法
  _skillRendered = filtered;
  container.querySelectorAll('[data-skill-action]').forEach(btn => {
    btn.addEventListener('click', () => {
      const skill = _skillRendered[Number(btn.dataset.idx)];
      if (!skill) return;
      if (btn.dataset.skillAction === 'preview') {
        previewSkill(skill, isSystem);
      } else {
        toggleSkill(skill, isSystem, btn);
      }
    });
  });
}

async function toggleSkill(skill, isSystem, btn) {
  const name = skill.name || skill.skill_name;
  const enable = skill.enabled === false;   // 当前禁用 → 点击后启用

  if (btn) btn.disabled = true;
  try {
    await API.put('/api/skills/0/toggle', { name, enabled: enable });
    // 后端成功后再落本地状态，避免"界面变了但实际没生效"
    const list = isSystem ? _skillData.system : _skillData.user;
    const item = list.find(s => (s.name || s.skill_name) === name);
    if (item) item.enabled = enable;
    skill.enabled = enable;
    renderSkillList();
    showToast(`技能「${name}」已${enable ? '启用' : '禁用'}`, 'success');
  } catch (e) {
    if (btn) btn.disabled = false;
    showToast(`操作失败: ${e.message}`, 'error');
  }
}

async function previewSkill(skill, isSystem) {
  const name = typeof skill === 'string' ? skill : (skill.name || skill.skill_name);
  const content = $('#skill-preview-content');
  const title = $('#skill-preview-title');
  title.textContent = `${name} - 技能详情`;
  content.innerHTML = '<p class="loading-text">加载中...</p>';
  openModal('modal-skill-preview');

  try {
    let url;
    if (isSystem) {
      url = `/api/skills/system/${encodeURIComponent(name)}`;
    } else if (skill && skill.id) {
      url = `/api/skills/${skill.id}`;
    } else {
      // 文件系统用户技能没有数据库 id，按名称查
      url = `/api/skills/user/${encodeURIComponent(name)}`;
    }
    renderSkillPreview(await API.get(url));
  } catch (e) {
    content.innerHTML = `<p class="loading-text" style="color:var(--danger);">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function formatSkillParams(params) {
  if (!params || typeof params !== 'object') return '无参数';
  // 兼容两种结构：{name: {...}} 或 JSON Schema {type:'object', properties:{...}}
  const props = params.properties && typeof params.properties === 'object'
    ? params.properties : params;
  const required = Array.isArray(params.required) ? params.required : [];
  const keys = Object.keys(props).filter(k => k !== 'type' && k !== 'required');
  if (keys.length === 0) return '无参数';
  return keys.map(k => {
    const v = props[k];
    const desc = v && typeof v === 'object' ? (v.description || '') : '';
    const mark = required.includes(k) ? '*' : '';
    return `${k}${mark}${desc ? ': ' + desc : ''}`;
  }).join(', ');
}

function renderSkillPreview(data) {
  const content = $('#skill-preview-content');
  const scripts = Array.isArray(data.scripts) ? data.scripts : [];

  let html = '';
  if (data.description) {
    html += `<div class="skill-preview-section">
      <p style="color:var(--text-muted);margin:0;">${escapeHtml(data.description)}</p>
    </div>`;
  }

  // SKILL.md 正文（此前完全没渲染，用户技能预览等于一片空白）
  const instructions = data.instructions || '';
  if (instructions) {
    html += `<div class="skill-preview-section">
      <h4>技能说明</h4>
    </div>
    <div class="skill-preview-script">
      <div style="padding:8px 12px;">
        <pre style="white-space:pre-wrap;">${escapeHtml(instructions)}</pre>
      </div>
    </div>`;
  }

  html += `<div class="skill-preview-section">
    <h4>脚本列表 (${scripts.length})</h4>
  </div>`;

  if (scripts.length === 0) {
    html += '<p class="loading-text">暂无脚本</p>';
  } else {
    html += scripts.map((s, i) => {
      const code = s.execution_code || s.code || s.source || '';
      const paramStr = formatSkillParams(s.parameters);
      return `
        <div class="skill-preview-script">
          <h4>${i + 1}. ${escapeHtml(s.name || '脚本')}</h4>
          <div style="padding:8px 12px;">
            ${s.description ? `<p style="color:var(--text-muted);font-size:12px;margin:0 0 6px;">${escapeHtml(s.description)}</p>` : ''}
            <p style="font-size:11px;color:var(--text-muted);margin:0 0 6px;">参数: ${escapeHtml(paramStr)}</p>
            ${code ? `<pre>${escapeHtml(code)}</pre>` : ''}
          </div>
        </div>
      `;
    }).join('');
  }

  content.innerHTML = html;
}

// ===== 云文件选择器 =====
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

  // active 类复用 btn-web-search / btn-thought 的高亮样式
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

function updateThinkingModeUI() {
  const btn = $('#btn-thought');
  if (!btn) return;
  // off 仅显示「深度思考」（无状态后缀）；low 加绿色（与「联网搜索·自动」一致）；high 加蓝色高亮
  const labels = { off: '深度思考', low: '深度思考：低', high: '深度思考：高' };
  const mode = state.thinkingMode in labels ? state.thinkingMode : 'low';
  btn.classList.toggle('active', mode === 'high');
  btn.classList.toggle('auto', mode === 'low');
  const span = btn.querySelector('span');
  if (span) span.textContent = labels[mode];
  localStorage.setItem('thinkingMode', mode);
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
    const maxIterEl = $('#cfg-max-iterations');
    if (maxIterEl) maxIterEl.value = config.max_iterations != null ? config.max_iterations : '';

    const tempModeEl = $('#cfg-temperature-mode');
    if (tempModeEl) tempModeEl.value = config.temperature_mode === 'static' ? 'static' : 'auto';
    const tempEl = $('#cfg-temperature');
    if (tempEl) tempEl.value = config.temperature != null ? config.temperature : '';

    if (localStorage.getItem('thinkingMode') === null) {
      state.thinkingMode = config.thinking_mode || 'low';
    } else {
      state.thinkingMode = localStorage.getItem('thinkingMode') || 'low';
    }
    updateThinkingModeUI();

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
        const gMaxIterEl = $('#cfg-global-max-iterations');
        if (gMaxIterEl) gMaxIterEl.value = globalCfg.max_iterations != null ? globalCfg.max_iterations : '';
        const gTempModeEl = $('#cfg-global-temperature-mode');
        if (gTempModeEl) gTempModeEl.value = globalCfg.temperature_mode === 'static' ? 'static' : 'auto';
        const gTempEl = $('#cfg-global-temperature');
        if (gTempEl) gTempEl.value = globalCfg.temperature != null ? globalCfg.temperature : '';
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
  const maxIterEl = $(`#${prefix}max-iterations`);
  if (maxIterEl) {
    const v = parseInt(maxIterEl.value.trim(), 10);
    if (!isNaN(v) && v >= 1) body.max_iterations = v;
  }

  const tempModeEl = $(`#${prefix}temperature-mode`);
  if (tempModeEl) {
    const m = tempModeEl.value.trim();
    if (m === 'static' || m === 'auto') body.temperature_mode = m;
  }
  const tempEl = $(`#${prefix}temperature`);
  if (tempEl) {
    const tv = parseFloat(tempEl.value.trim());
    if (!isNaN(tv) && tv >= 0 && tv <= 2) body.temperature = tv;
  }

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
      : `<button class="btn-sm danger" data-action="delete-user" data-id="${u.public_id}" data-username="${escapeHtml(u.username)}">删除</button>`;
    return `
      <tr>
        <td>${u.public_id}</td>
        <td>${escapeHtml(u.username)}</td>
        <td><span class="user-type-badge ${u.user_type}">${isAdmin ? '管理员' : '普通用户'}</span></td>
        <td>${escapeHtml(u.description || '-')}</td>
        <td>
          <div class="actions">
            <button class="btn-sm" data-action="edit-user" data-id="${u.public_id}" data-username="${escapeHtml(u.username)}" data-type="${u.user_type}" data-desc="${escapeHtml(u.description || '')}">编辑</button>
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
function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark') {
    document.documentElement.classList.add('dark');
    $('#theme-toggle').textContent = '☀️';
    $('#theme-toggle').title = '当前：暗色模式（点击切换）';
  } else if (saved === 'light') {
    document.documentElement.classList.remove('dark');
    $('#theme-toggle').textContent = '🌙';
    $('#theme-toggle').title = '当前：亮色模式（点击切换）';
  } else {
    applySystemTheme();
    $('#theme-toggle').title = '当前：自动模式（点击切换）';
  }
}

function applySystemTheme() {
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  if (prefersDark) {
    document.documentElement.classList.add('dark');
    $('#theme-toggle').textContent = '🌓';
  } else {
    document.documentElement.classList.remove('dark');
    $('#theme-toggle').textContent = '🌓';
  }
}

function toggleTheme() {
  const saved = localStorage.getItem('theme');
  if (!saved || saved === 'auto') {
    localStorage.setItem('theme', 'dark');
    document.documentElement.classList.add('dark');
    $('#theme-toggle').textContent = '☀️';
    $('#theme-toggle').title = '当前：暗色模式（点击切换）';
  } else if (saved === 'dark') {
    localStorage.setItem('theme', 'light');
    document.documentElement.classList.remove('dark');
    $('#theme-toggle').textContent = '🌙';
    $('#theme-toggle').title = '当前：亮色模式（点击切换）';
  } else {
    localStorage.removeItem('theme');
    applySystemTheme();
    $('#theme-toggle').title = '当前：自动模式（点击切换）';
  }
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  const saved = localStorage.getItem('theme');
  if (!saved || saved === 'auto') {
    if (e.matches) {
      document.documentElement.classList.add('dark');
      $('#theme-toggle').textContent = '☀️';
    } else {
      document.documentElement.classList.remove('dark');
      $('#theme-toggle').textContent = '🌙';
    }
  }
});

// ===== 事件绑定 =====
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  // 刷新前已查看过的完成任务，恢复已读标记，避免完成气泡重复弹出
  _loadAcknowledged();

  // 文件库预览「返回文件列表」按钮（事件委托，兼容动态生成的按钮）
  document.addEventListener('click', (e) => {
    if (e.target.closest('#preview-back-btn')) {
      e.preventDefault();
      returnToFileList();
    }
  });

  $('#theme-toggle').addEventListener('click', toggleTheme);

  if (localStorage.getItem('thinkingMode')) {
    state.thinkingMode = localStorage.getItem('thinkingMode') || 'low';
  }
  updateThinkingModeUI();

  loadSessions();

  // 恢复上次活跃的会话（刷新后不再丢失当前会话）
  const _savedSid = _loadCurrentSessionId();
  if (_savedSid) {
    // 延迟一帧等待 loadSessions 渲染完成
    requestAnimationFrame(() => {
      const exists = state.sessions.some(s => s.id === _savedSid);
      if (exists) { switchSession(_savedSid); }
      else { localStorage.removeItem(_SID_KEY); }  // session 已被删除，清除残留
    });
  }

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

  // 文件库搜索（事件委托：骨架重建后旧 input 会被销毁，绑定在 document 上可持久生效）
  let cloudSearchTimer;
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'cloud-search') {
      clearTimeout(cloudSearchTimer);
      cloudSearchTimer = setTimeout(() => loadCloudFiles(), 300);
    }
  });

  // 文件库「文档类型」「来源方式」筛选（事件委托，同上原因）
  document.addEventListener('change', (e) => {
    if (e.target && (e.target.id === 'cloud-type-filter' || e.target.id === 'cloud-source-filter')) {
      _applyCloudFilters();
    }
  });

  const pickerSearch = $('#picker-search');
  if (pickerSearch) {
    let pickerSearchTimer;
    pickerSearch.addEventListener('input', () => {
      clearTimeout(pickerSearchTimer);
      pickerSearchTimer = setTimeout(() => loadPickerFiles(pickerSearch.value), 300);
    });
  }

  const btnPickerConfirm = $('#btn-picker-confirm');
  if (btnPickerConfirm) {
    btnPickerConfirm.addEventListener('click', confirmPickerSelection);
  }

  const btnPickerCancel = $('#btn-picker-cancel');
  if (btnPickerCancel) {
    btnPickerCancel.addEventListener('click', () => {
      closeModal('modal-file-picker');
    });
  }

  $('#btn-send').addEventListener('click', () => {
    if (state.isStreaming || (state.subscribingSessionId === state.currentSessionId)) {
      stopGeneration();
    } else if (state.subscribingSessionId) {
      showToast('后台任务正在执行中，请等待完成', 'info');
    } else {
      sendMessage();
    }
  });

  $('#btn-attach').addEventListener('click', (e) => {
    e.stopPropagation();
    const menu = $('#attach-menu');
    if (!menu.classList.contains('hidden')) {
      menu.classList.add('hidden');
      return;
    }
    const btn = $('#btn-attach');
    const rect = btn.getBoundingClientRect();
    menu.style.left = rect.left + 'px';
    menu.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
    menu.style.position = 'fixed';
    menu.classList.remove('hidden');
  });

  $$('.attach-menu-item').forEach(item => {
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      $('#attach-menu').classList.add('hidden');
      const action = item.dataset.action;
      if (action === 'upload') {
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.accept = '.txt,.md,.csv,.json,.xml,.html,.css,.js,.py,.log,.yaml,.yml,.ini,.cfg,.toml,.sh,.bash,.zsh,.sql,.r,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.ts,.tsx,.jsx,.vue,.conf,.env,.properties,.pdf,.docx,.xlsx,.pptx';
        input.onchange = () => {
          if (input.files.length > 0) {
            uploadFiles(input.files);
          }
        };
        input.click();
      } else if (action === 'cloud') {
        openFilePicker();
      }
    });
  });

  // ===== 拖拽上传：拖文件到页面任意位置即弹出整屏提示，任意位置松手均可上传 =====
  (function initDropZone() {
    const overlay = $('#drop-overlay');
    if (!overlay) return;
    let dragDepth = 0;  // 进入/离开子元素会触发 dragenter/leave，用计数器避免闪烁

    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');

    const show = () => overlay.classList.remove('hidden');
    const hide = () => { dragDepth = 0; overlay.classList.add('hidden'); };

    // 在 window 级别拦截，确保拖到页面任意位置都能响应
    window.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth++;
      show();
    });
    window.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      // 离开浏览器窗口（拖出到外部）时 relatedTarget 为空，直接隐藏
      if (e.relatedTarget === null) { hide(); return; }
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) hide();
    });
    window.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      hide();
      const files = e.dataTransfer.files;
      if (files && files.length > 0) {
        uploadFiles(files);
      }
    });
  })();

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
    if (!e.target.closest('#attach-menu') && !e.target.closest('#btn-attach')) {
      $('#attach-menu').classList.add('hidden');
    }
  });

  // 设置抽屉菜单
  $$('.drawer-item').forEach(item => {
    item.addEventListener('click', () => {
      const action = item.dataset.action;
      $('#settings-drawer').classList.add('hidden');
      if (action === 'config') { loadConfig(); openModal('modal-config'); }
      if (action === 'files') { loadFiles(); openModal('modal-files'); }
      if (action === 'cloud-files') { loadCloudFiles(); openModal('modal-cloud-files'); }
      if (action === 'skills') { loadSkills(); openModal('modal-skills'); }
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

  // 启动任务状态轮询（检测后台任务完成，显示气泡通知）
  startTaskPolling();

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('must_change_password') === '1') {
    setTimeout(() => {
      openChangePassword();
      showToast('首次登录，请修改默认密码', 'info');
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', '/');
      }
    }, 500);
  }

  // 联网搜索两态切换: off -> auto -> off
  const WEB_SEARCH_MODES = ['off', 'auto'];
  const WEB_SEARCH_LABELS = {
    off: '联网搜索',
    auto: '联网搜索·自动',
  };
  $('#btn-web-search').addEventListener('click', () => {
    const btn = $('#btn-web-search');
    const currentIdx = WEB_SEARCH_MODES.indexOf(state.webSearch);
    const nextIdx = (currentIdx + 1) % WEB_SEARCH_MODES.length;
    state.webSearch = WEB_SEARCH_MODES[nextIdx];
    btn.dataset.mode = state.webSearch;
    btn.querySelector('span').textContent = WEB_SEARCH_LABELS[state.webSearch];
    btn.classList.remove('active', 'auto');
    if (state.webSearch === 'auto') btn.classList.add('auto');
    btn.title = WEB_SEARCH_LABELS[state.webSearch];
  });

  $('#btn-thought').addEventListener('click', async () => {
    const order = ['off', 'low', 'high'];
    const idx = order.indexOf(state.thinkingMode);
    const next = order[(idx + 1) % order.length];
    const prev = state.thinkingMode;
    state.thinkingMode = next;
    updateThinkingModeUI();
    try {
      await API.put('/api/config', { thinking_mode: next });
    } catch (e) {
      showToast('保存思考设置失败: ' + e.message, 'error');
      state.thinkingMode = prev;
      updateThinkingModeUI();
    }
  });

  // 会话权限模式（请求批准 / 完全访问权限）：事件绑定
  // 注意：updateTrustUI / refreshTrustState 定义在顶层作用域，
  // 因为 switchSession 等顶层函数需要调用它们。
  const btnPerm = $('#btn-perm');

  // 权限按钮点击 → 切换模式（切到 full 时弹确认窗）
  if (btnPerm) {
    btnPerm.addEventListener('click', async () => {
      // 未在任何 session 中：权限模式需绑定到具体会话，
      // 先发送一条消息创建会话后再切换（符合「输入发送后创建新对话」的逻辑）
      if (!state.currentSessionId) {
        showToast('请先在下方输入并发送一条消息创建会话，再切换权限模式', 'info');
        return;
      }
      const currentMode = btnPerm.dataset.mode || 'request';

      // 当前是 full → 直接切回 request（安全方向，无需确认）
      if (currentMode === 'full') {
        try {
          const res = await API.post(`/api/chat/${state.currentSessionId}/trust`, { mode: 'request' });
          if (!res || res.success !== true) throw new Error('返回异常');
          updateTrustUI('request');
          showToast('已切回「请求批准」：敏感操作前将再次需要你确认', 'info');
        } catch (e) {
          showToast('切换权限模式失败: ' + (e.message || e), 'error');
        }
        return;
      }

      // 当前是 request → 弹确认窗
      openModal('modal-perm-confirm');
    });

    // 确认弹窗：确认按钮
    const permConfirmOk = $('#perm-confirm-ok');
    if (permConfirmOk) {
      permConfirmOk.addEventListener('click', async () => {
        closeModal('modal-perm-confirm');
        btnPerm.disabled = true;
        try {
          const res = await API.post(`/api/chat/${state.currentSessionId}/trust`, { mode: 'full' });
          if (!res || res.success !== true) throw new Error('返回异常');
          updateTrustUI('full');
          showToast('已切换为「完全访问权限」：敏感操作将直接执行，不再确认', 'info');
        } catch (e) {
          showToast('切换权限模式失败: ' + (e.message || e), 'error');
        } finally {
          btnPerm.disabled = false;
        }
      });
    }

    // 确认弹窗：取消按钮
    const permConfirmCancel = $('#perm-confirm-cancel');
    if (permConfirmCancel) {
      permConfirmCancel.addEventListener('click', () => closeModal('modal-perm-confirm'));
    }
  }

  // 权限范围说明已统一放入切换确认弹窗（modal-perm-confirm），不再单独维护 ⓘ 气泡

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