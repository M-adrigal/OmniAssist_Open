/* 04-stream.js
 * 流式对话渲染：流容器、工具摘要、产出文件预览、审批卡片、搜索区、思考区、SSE 读取
 *
 * 由 static/app.js 拆分而来（原第 1314-2011 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
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
      <div class="tool-summary hidden"></div>
      <div class="stream-status hidden"></div>
      <div class="answer-area streaming-cursor"></div>
      <div class="output-files hidden"></div>
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();

  const stream = {
    container: div,
    // 一行「当前在干什么」的状态提示（后端 status 事件）。普通进度提示在正文
    // 开始输出后自动收起；带 warn 的（异常/提前结束/重试）会一直留着。
    statusEl: div.querySelector('.stream-status'),
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
    const argKeys = Object.keys(it.args || {});
    // 参数以「键: 值」逐行展示，比压缩成一坨 JSON 好读；无参数时不做折叠区
    // （空折叠区点开是空的，比没有更让人困惑）
    const argsStr = argKeys.map(k => {
      const v = it.args[k];
      const val = (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v);
      return `${k}: ${val}`;
    }).join('\n');
    const argsBlock = argKeys.length
      ? `<details class="approval-args"><summary>参数</summary><pre>${escapeHtml(argsStr)}</pre></details>`
      : '';
    itemsHtml += `
      <div class="approval-item" data-item="${escapeHtml(it.item_id)}">
        <div class="approval-item-head">
          <span class="approval-tool">${escapeHtml(it.tool)}</span>
          <span class="risk-badge risk-${escapeHtml(risk)}">${RISK_LABELS[risk] || risk}</span>
        </div>
        <div class="approval-desc">${escapeHtml(it.desc || '')}</div>
        ${argsBlock}
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
  // 「正在干什么」的进度提示。后端 status 事件有 6 处：正在处理 / 正在调用工具 /
  // 正在并行调用 N 个工具 /「x」将在本轮回复后执行 / 模型调用异常正在重试 /
  // 检测到工具调用异常提前结束。
  // 以前这个事件**前端完全不处理**，于是循环防护把任务掐断时，用户看到的是
  // 回答戛然而止且没有任何解释。普通提示随正文出现即收起，warn 类常驻。
  if (parsed.type === 'status') {
    const text = (parsed.content || '').trim();
    if (text) {
      const warn = /异常|提前结束|重试|失败|中断/.test(text);
      stream.statusEl.textContent = text;
      stream.statusEl.classList.toggle('warn', warn);
      stream.statusEl.classList.remove('hidden');
      stream.statusWarn = warn;
      scrollToBottom();
    }
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
    // 正文开始输出了，进度提示（「正在调用工具…」之类）就没用了；warn 留着
    if (!stream.statusWarn) {
      stream.statusEl.classList.add('hidden');
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
    // 普通的进度提示到这儿收掉；warn（异常/提前结束）必须留着，
    // 否则用户永远不知道这轮为什么断在半截
    if (!stream.statusWarn) {
      stream.statusEl.classList.add('hidden');
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

