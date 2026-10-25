/* 02-sessions.js
 * 会话管理：列表/搜索/创建/删除/切换、任务状态轮询
 *
 * 由 static/app.js 拆分而来（原第 284-702 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
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

// 会话按时间分桶：今天 / 昨天 / 更早（updated_at 是本地时间字符串）
function _sessionGroupOf(updatedAt) {
  if (!updatedAt) return '更早';
  const d = new Date(String(updatedAt).replace(' ', 'T'));
  if (isNaN(d.getTime())) return '更早';
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday.getTime() - 86400000);
  if (d >= startOfToday) return '今天';
  if (d >= startOfYesterday) return '昨天';
  return '更早';
}

function _sessionItemHtml(s) {
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
    <div class="session-item${isCurrent ? ' active' : ''}" data-id="${s.id}">
      <span class="title">${escapeHtml(s.title || '新对话')}</span>
      ${badgeHtml}
      <button class="btn-delete" data-action="delete-session" data-id="${s.id}">&times;</button>
    </div>
    `;
}

function renderSessions() {
  const list = $('#session-list');
  const kw = (state.sessionFilter || '').trim().toLowerCase();
  const items = kw
    ? state.sessions.filter(s => (s.title || '').toLowerCase().includes(kw))
    : state.sessions;

  if (!items.length) {
    list.innerHTML = `<div class="session-empty">${kw ? '没有匹配的对话' : '还没有对话'}</div>`;
    return;
  }

  let html;
  if (kw) {
    // 过滤态不再分组：结果本来就少，分组反而增加噪音
    html = items.map(_sessionItemHtml).join('');
  } else {
    const buckets = { '今天': [], '昨天': [], '更早': [] };
    items.forEach(s => buckets[_sessionGroupOf(s.updated_at)].push(s));
    html = ['今天', '昨天', '更早']
      .filter(g => buckets[g].length)
      .map(g => `<div class="side-group-title">${g}</div>${buckets[g].map(_sessionItemHtml).join('')}`)
      .join('');
  }
  list.innerHTML = html;

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
    refreshChatModelBadge();
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
        refreshChatModelBadge();
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
      refreshChatModelBadge();
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
    refreshChatModelBadge();
    _saveCurrentSessionId();
    renderSessions();
    updateChatTitle();
    clearAttachedFiles();
    refreshTrustState();
    // 每个会话可能用了不同模型，切过去要跟着换标识
    refreshChatModelBadge();

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

      // 历史消息里的图片只存了文件名，这里用刚拿到的文件清单补出路径，
      // 必须在 renderHistoryMessage 之前 —— 晚一步图片就已经渲染成空白了。
      // 复用同一个 filesData，不额外发请求。
      state.uploadFiles = {};
      (filesData.files || []).forEach(f => {
        if (f && f.filename && f.path) {
          state.uploadFiles[f.filename] = { path: f.path, type: f.type || '' };
        }
      });

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

