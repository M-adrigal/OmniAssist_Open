/* 05-chat-actions.js
 * 对话动作：任务订阅、发送消息、重新生成、停止生成
 *
 * 由 static/app.js 拆分而来（原第 2012-2299 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
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
    showToast('请使用模型菜单里的「深度思考」切换', 'info');
    return;
  }

  input.value = '';
  input.style.height = 'auto';
  hideCommandSuggestions();

  if (!state.currentSessionId) {
    try {
      const s = await API.post('/api/sessions', { title: message.substring(0, 30) });
      state.currentSessionId = s.id;
      refreshChatModelBadge();
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

  // 【单一来源】气泡里画什么、请求体里报什么，必须是同一个数组。
  // 之前两处各取一次 state.attachedFiles，一旦其中一处多/少了项，就会出现
  // 「气泡上有两个图片、请求里一个都没有」这种情况（用户 2026-10-24 反馈）。
  // 过滤掉没有文件名的项：这类项既画不出 chip，也给不出请求体，留着只会制造错觉。
  const currentAttachments = [...(state.attachedFiles || [])].filter(f => f && f.filename);
  const _isCommand = /^\/(reset|help)\b/i.test((message || '').trim());

  if (_isCommand) {
    // 斜杠命令与附件无关。不清的话附件会在输入框里一直挂着，
    // 下一条普通消息就被"粘"上去了 —— 用户明明没粘贴却带了图。
    if (currentAttachments.length || (state.attachedFiles || []).length) clearAttachedFiles();
    await _runChatTurn(message, []);
    return;
  }

  // 附件（含图片缩略图）直接交给 appendMessage 渲染，不再二次拼接 DOM ——
  // 之前这里和 appendMessage 各写了一份 chip 模板，改一处就会两处不一致。
  if (currentAttachments.length > 0) {
    const lastMsg = getActiveContainer().element.lastElementChild;
    if (lastMsg && lastMsg.classList.contains('user')) {
      const areaEl = lastMsg.querySelector('.answer-area');
      if (areaEl) areaEl.insertAdjacentHTML('beforeend', renderAttachmentChips(currentAttachments));
    }
    clearAttachedFiles();
  }
  console.info('[ATTACH] 本轮上报', currentAttachments.map(f => f.filename));

  await _runChatTurn(message, currentAttachments);
}

// ===== 一轮「提问 → 流式回答」 =====
// 发新消息和重新生成共用这一段：两者只差"调用前要不要先插一条用户气泡"。
// 重新生成时气泡已经在页面上（后端也把这条用户消息重新追加了一次），所以不能再插。
// attachments：本轮上传的文件（含图片）。重新生成时没有"本轮上传"，传空数组即可
// ——历史消息不记录原轮上传清单，全发就会回到"图片叠加上传"的老 bug。
async function _runChatTurn(message, attachments = []) {
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
        selected_skills: state.selectedSkills || [],
        // 本轮新上传的文件名（发完清空）。后端据此只把本轮图片送入多模态，
        // 避免会话上传目录里历史图片被反复重发（叠加）。
        uploaded_files: (attachments || []).map(f => f.filename).filter(Boolean),
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

    // 操作行等流结束才补：内容还在长的时候挂上去，会被每一帧顶一次位置
    if (stream.answerContent) {
      attachMsgActions(stream.container);
      syncMsgActions(stream.container.parentElement);
    }

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

// 重新生成是"先删后写"，并发点两次会把上一轮也删掉，所以加锁，连点只认一次
let _regenerating = false;

async function regenerateLastTurn() {
  const sid = state.currentSessionId;
  if (!sid || _regenerating) return;
  if (state.isStreaming && state.streamingSessionId === sid) {
    showToast('正在生成中，请等待完成', 'warning');
    return;
  }
  const taskInfo = state.taskStatuses[sid];
  if (taskInfo && taskInfo.status === 'running') {
    showToast('该会话有正在执行的任务，请等待完成', 'warning');
    return;
  }

  _regenerating = true;
  try {
    // 重新生成不携带图片：会话上传目录跨轮累积，历史消息也不记录"原轮上传了哪些图"，
    // 无法安全还原（全发就回到叠加 bug）。原消息带图时明确告知，绝不静默降级。
    const _lastUserAttach = Array.from(
      document.querySelectorAll('.message.user .msg-attachments')
    ).pop();
    if (_lastUserAttach && Array.from(_lastUserAttach.querySelectorAll('.msg-attach-tag'))
        .some(t => /\.(png|jpe?g|gif|webp|bmp)$/i.test(t.textContent || ''))) {
      showToast('重新生成不携带原消息中的图片，如需模型看图请在新消息里重新上传', 'warning');
    }
    // 先让后端把「最后一条用户消息之后的回复」从历史里删掉，再拿回那条消息重跑。
    // 顺序不能反：万一请求失败，历史还是完整的，页面上也不该已经少了一条回答。
    let payload;
    try {
      payload = await API.post(`/api/chat/regenerate/${sid}`, {});
    } catch (e) {
      showToast('重新生成失败: ' + e.message, 'error');
      return;
    }

    const container = getActiveContainer().element;
    const msgs = Array.from(container.querySelectorAll('.message'));
    let lastUser = -1;
    msgs.forEach((el, idx) => { if (el.classList.contains('user')) lastUser = idx; });
    msgs.slice(lastUser + 1).forEach(el => el.remove());
    syncMsgActions(container);

    await _runChatTurn(payload.message);
  } finally {
    _regenerating = false;
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
