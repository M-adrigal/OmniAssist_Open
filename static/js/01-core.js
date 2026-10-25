/* 01-core.js
 * 核心：API 封装、全局 state、通用工具（$/$$/toast/confirm/formatSize/formatTime）、会话容器、localStorage
 *
 * 由 static/app.js 拆分而来（原第 1-283 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
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
  // 当前会话已上传文件：文件名 → { path, type }。
  // 历史消息只存文件名（不存路径也不存类型），靠它才能拼出图片地址、
  // 给文档选对图标（switchSession 从 /api/files/uploads 拿到后填充，不额外发请求）。
  uploadFiles: {},
  selectedSkills: [],   // 技能面板里勾选的技能（本轮显式使用，刷新即清空）
  taskStatuses: {},  // { sessionId: { status, user_message, started_at, completed_at } }
  acknowledgedTasks: new Set(),  // 已查看过的完成任务 session_id
  subscribingSessionId: null,  // 正在订阅后台任务的 session_id
  sessionFilter: '',  // 侧栏本地过滤词（只筛标题，不做全文搜索）
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

// ===== 欢迎页（品牌 + 能力入口）=====
// 与 index.html 的静态首屏保持一致，三处容器创建统一调用这里
function _welcomeHtml() {
  const caps = [
    ['blue', '对话问答', '直接提问，可切换模型与思考强度', '我想咨询一个问题：',
      '<path d="M21 11.5a8.4 8.4 0 0 1-8.5 8.4 9 9 0 0 1-3.9-.9L3 21l1.9-5.1a8.3 8.3 0 0 1-.9-3.8 8.4 8.4 0 0 1 8.5-8.4A8.4 8.4 0 0 1 21 11.5z"/>'],
    ['violet', '文档分析', '上传文件，提取、整理与改写内容', '帮我分析这份文件，提炼要点和结论。',
      '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>'],
    ['green', '技能执行', '调用已装技能，沙箱内运行并产出文件', '请用可用技能帮我完成：',
      '<path d="M12 3l2 5.5 5.5 2-5.5 2L12 18l-2-5.5L4.5 10.5 10 8.5z"/>'],
    ['amber', '数据查询', '查用量、跑脚本、出一张统计图', '查一下最近七天的 Token 消耗，按天汇总并画一张趋势图。',
      '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'],
  ];
  const cards = caps.map(([color, title, desc, prompt, path]) => `
          <button type="button" class="cap-card" data-prompt="${escapeHtml(prompt)}">
            <span class="cap-ico ${color}"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${path}</svg></span>
            <span class="cap-body"><span class="cap-title">${title}</span><span class="cap-desc">${desc}</span></span>
          </button>`).join('');
  return `
      <div class="welcome-message">
        <div class="welcome-brand">
          <span class="welcome-logo">
            <svg width="38" height="38" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="OmniAssist">
              <defs>
                <linearGradient id="oa-brand-mark" x1="0" y1="0" x2="48" y2="48" gradientUnits="userSpaceOnUse">
                  <stop offset="0%" stop-color="#3B82F6"/>
                  <stop offset="52%" stop-color="#6366F1"/>
                  <stop offset="100%" stop-color="#8B5CF6"/>
                </linearGradient>
              </defs>
              <rect width="48" height="48" rx="13" fill="url(#oa-brand-mark)"/>
              <path d="M24 36 A 12 12 0 1 1 36 24" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round"/>
              <path d="M24 18 A 6 6 0 1 1 18 24" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round"/>
            </svg>
          </span>
        </div>
        <h1>OmniAssist</h1>
        <p>智能对话 · 沙箱执行 · 审批管控</p>
        <div class="cap-grid">${cards}</div>
      </div>`;
}

// 能力卡点击：把起始问题填进输入框（只填不发，留给用户改写）
// 用事件委托，静态首屏与 JS 动态生成的欢迎页都走同一套逻辑
function initCapCards() {
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.cap-card');
    if (!btn) return;
    const input = $('#chat-input');
    if (!input) return;
    input.value = btn.dataset.prompt || '';
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
}

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
    el.innerHTML = _welcomeHtml();
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
    el.innerHTML = _welcomeHtml();
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
