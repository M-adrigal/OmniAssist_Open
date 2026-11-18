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
  // 联网搜索开关持久化：刷新后保持上次选择。
  // 之前只存内存，每次刷新都静默回到「关」，而按钮又显示成「联网搜索」，
  // 于是「我开了」和「实际没开」能同时成立 —— 排查成本极高，别改回去。
  webSearch: (function () {
    try { return localStorage.getItem('webSearch') === 'auto' ? 'auto' : 'off'; }
    catch (e) { return 'off'; }
  })(),
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

/**
 * 取单个元素。
 *
 * @param {string} sel
 * @returns {HTMLElement | null}
 */
function $(sel) { return /** @type {any} */ (document.querySelector(sel)); }

/**
 * 取多个元素（静态 NodeList）。
 * @param {string} sel
 * @returns {NodeListOf<HTMLElement>}
 */
function $$(sel) { return /** @type {any} */ (document.querySelectorAll(sel)); }

/**
 * 判断是否指定标签名的元素，收窄类型。
 *
 * 为什么需要：`e.target.tagName === 'IMG'` 这种写法运行时是安全的，但 TS
 * 不会据此收窄 EventTarget，后面读 naturalWidth / closest 依然报错。
 * 用这个函数把"判断 + 收窄"合成一步：
 *   const img = asTag(e.target, 'IMG'); if (!img) return; img.naturalWidth
 *
 * @param {EventTarget | null | undefined} t
 * @param {string} tag  大写标签名，如 'IMG'
 * @returns {any}  命中返回该元素（调用方可自行标注具体类型），否则 null
 */
function asTag(t, tag) {
  return (t && /** @type {any} */ (t).nodeType === 1
    && /** @type {Element} */ (t).tagName === tag) ? /** @type {any} */ (t) : null;
}

/**
 * 把事件目标收窄成元素。
 *
 * 为什么需要：`e.target` 的静态类型是 EventTarget，运行时不保证是元素 ——
 * 点到按钮里的文字节点时 target 是 Text（没有 closest），点 SVG 图形时是
 * SVGElement（有 closest 但语义不同）。原来 16 处直接 `e.target.closest(...)`
 * 在这种情形下抛 TypeError，整个点击处理器静默失效。
 *
 * @param {Event} e
 * @returns {Element | null}  不是元素时返回 null，调用方按"没命中"处理即可
 */
function evTarget(e) {
  const t = e && e.target;
  return (t && /** @type {any} */ (t).nodeType === 1) ? /** @type {Element} */ (t) : null;
}

/**
 * 在 textarea 光标处插入文本，并保持「可撤销」。
 *
 * 为什么不用 `input.value = a + text + b` 这种拼法：
 * 直接赋值会把整个撤销栈清掉（用户按 Cmd+Z 会一次性丢掉全部内容），
 * 也不会触发 `input` 事件 —— 而输入框的自动增高、命令补全都挂在
 * input 事件上，拼完高度不更新，多插一个换行也看不出来。
 *
 * `setRangeText` 会同时做对这三件事：保留撤销栈、把光标移到插入内容之后、
 * 触发 input 事件。Safari 从 10.1 起支持，够用。
 *
 * @param {HTMLTextAreaElement} el
 * @param {string} text  要插入的文本（换行传 '\n'）
 */
function insertTextAtCursor(el, text) {
  if (!el) return;
  const start = el.selectionStart != null ? el.selectionStart : el.value.length;
  const end = el.selectionEnd != null ? el.selectionEnd : el.value.length;
  // 'end' = 插入后光标落在文本末尾（默认 replace 会把选区一起替换掉）
  if (typeof el.setRangeText === 'function') {
    el.setRangeText(text, start, end, 'end');
  } else {
    // 兜底：老浏览器。功能可用，代价是撤销栈不连续
    el.value = el.value.slice(0, start) + text + el.value.slice(end);
    el.selectionStart = el.selectionEnd = start + text.length;
  }
  // setRangeText 在部分浏览器里不派发 input 事件，手动补一次，
  // 保证自动增高与命令补全都能跟上。重复派发是无害的（处理器幂等）。
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

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
    const btn = evTarget(e)?.closest('.cap-card');
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
  // 换了一个会话 → 刻度条要按新会话的用户消息重建。
  // 不重置 dataset.fp 的话，两个会话的标题恰好相同时会误判成"没变化"而跳过重建。
  const nav = $('#msg-nav');
  if (nav) nav.dataset.fp = '';
  scheduleMsgNavRefresh();
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
  // 主动滚到底了，浮标得立刻收起来 —— 只依赖 scroll 事件会慢一帧（那是异步派发的），
  // 用户看到的是"已经到底了但箭头还挂着"。
  refreshScrollFab();
}

function showToast(msg, type = 'info') {
  const toast = $('#toast');
  if (!toast) return;
  toast.textContent = msg;
  toast.className = `toast ${type}`;
  // _timeout 是挂在元素上的自定义属性（非 DOM 标准），用来清掉上一条 toast 的定时器
  const t = /** @type {any} */ (toast);
  clearTimeout(t._timeout);
  t._timeout = setTimeout(() => toast.classList.add('hidden'), 3000);
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

// 时间显示。**同时接受秒级时间戳和 "YYYY-MM-DD HH:MM:SS" 字符串**。
// 历史上这里只按时间戳处理（ts * 1000），而后端 created_at 后来改成了字符串，
// 于是字符串 * 1000 = NaN → 界面显示 "Invalid Date"。这类「字段格式变了、消费方
// 没跟上」不会抛错，只会显示错，所以统一收在一个函数里，调用方不用关心形态。
function formatTime(value) {
  if (value === null || value === undefined || value === '') return '';

  let d;
  const raw = String(value);
  if (typeof value === 'number' || /^\d+$/.test(raw)) {
    const n = Number(value);
    d = new Date(raw.length > 10 ? n : n * 1000);   // 10 位按秒，13 位按毫秒
  } else {
    // 浏览器要求 T 分隔符才按本地时间解析 "2026-10-26 06:00:00"
    d = new Date(raw.replace(' ', 'T'));
  }

  if (isNaN(d.getTime())) return raw.slice(0, 16);   // 解析不了就原样截断，不显示 Invalid Date

  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
}

// ===== 会话管理 =====
