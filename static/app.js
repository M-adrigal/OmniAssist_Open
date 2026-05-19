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
      s.messages.forEach(m => renderHistoryMessage(m));
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
      <p style="margin-top:8px;font-size:13px;">输入消息开始对话，工具管理现已支持自然语言交互</p>
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
    <div class="message-body"><div class="answer-area">${renderContent(content)}</div></div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function renderHistoryMessage(m) {
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
    renderCompressedHistory(content, compressedMeta);
    return;
  }

  if (role === 'user' || !hasMeta) {
    appendMessage(role, content);
    return;
  }

  const container = $('#chat-messages');
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
              <div class="tool-item${t.error ? ' error' : ''}">
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
                  <span class="tool-result-text${t.error ? ' error' : ''}">${escapeHtml((t.result || '').length > 200 ? (t.result || '').substring(0, 200) + '...' : (t.result || ''))}</span>
                  ${(t.result || '').length > 200 ? `<button class="tool-result-expand" data-full="${escapeHtml(t.result)}">展开全部</button>` : ''}
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      ` : ''}
      <div class="answer-area">${renderContent(content)}</div>
    </div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;

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

function renderCompressedHistory(summaryContent, meta) {
  const container = $('#chat-messages');
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = 'message system';

  const roundsHtml = meta.rounds.map((r, i) => {
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
  if (!text) return '';

  const codeBlocks = [];
  let html = escapeHtml(text);

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
  const container = $('#chat-messages');
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
      <div class="answer-area streaming-cursor">正在处理...</div>
    </div>
  `;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;

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

  if (state.showThought) {
    stream.thinkEl.classList.remove('hidden');
    stream.thinkStartTime = Date.now();
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
        <div class="tool-item${t.error ? ' error' : ''}">
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
            <span class="tool-result-text${t.error ? ' error' : ''}">${escapeHtml(t.result.length > 200 ? t.result.substring(0, 200) + '...' : t.result)}</span>
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

  const container = $('#chat-messages');
  container.scrollTop = container.scrollHeight;
}

function updateThinkArea(stream) {
  if (!state.showThought) return;

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

  const container = $('#chat-messages');
  container.scrollTop = container.scrollHeight;
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
  const container = $('#chat-messages');
  container.scrollTop = container.scrollHeight;
}

async function sendMessage() {
  const input = $('#chat-input');
  const message = input.value.trim();
  if (!message || state.isStreaming) return;

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
      await loadSessions();
    } catch (e) {
      showToast('创建会话失败: ' + e.message, 'error');
      return;
    }
  }

  appendMessage('user', message);

  const stream = createAssistantContainer();
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
        show_thought: state.showThought,
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
            stream.answerEl.textContent = '错误: ' + parsed.content;
            stream.answerEl.classList.remove('streaming-cursor');
            showToast(parsed.content, 'error');
            return;
          }

          if (parsed.type === 'web_search') {
            stream.searchData = {
              query: parsed.query,
              scenario: parsed.scenario,
              results: parsed.results,
            };
            if (!stream.thinkStartTime) {
              stream.thinkStartTime = Date.now();
            }
            stream.thinkContent += `\n联网搜索: ${parsed.query}\n场景: ${parsed.scenario}\n`;
            updateThinkArea(stream);
            continue;
          }

          if (parsed.type === 'thought') {
            if (!stream.thinkStartTime) {
              stream.thinkStartTime = Date.now();
            }
            stream.thinkContent += parsed.content;
            updateThinkArea(stream);
            continue;
          }

          if (parsed.type === 'tool_call') {
            if (!stream.thinkStartTime) {
              stream.thinkStartTime = Date.now();
            }
            stream.tools.push({
              name: parsed.name,
              arguments: parsed.arguments || {},
              result: null,
              error: false,
            });
            stream.hasTools = true;
            stream.thinkContent += `\n调用工具: ${parsed.name}\n参数: ${JSON.stringify(parsed.arguments, null, 2)}\n`;
            updateThinkArea(stream);
            continue;
          }

          if (parsed.type === 'tool_result') {
            const tool = stream.tools.find(t => t.name === parsed.name && t.result === null);
            if (tool) {
              tool.result = parsed.content || '';
              tool.error = tool.result.startsWith('[沙箱执行失败]') ||
                           tool.result.startsWith('[沙箱执行超时]') ||
                           tool.result.startsWith('[沙箱异常]') ||
                           tool.result.startsWith('[工具执行异常]');
            }
            stream.thinkContent += `工具结果: ${parsed.content || '(空)'}\n`;
            updateThinkArea(stream);
            continue;
          }

          if (parsed.type === 'tool_summary') {
            renderToolSummary(stream, parsed.tools);
            continue;
          }

          if ((parsed.type === 'content' || parsed.type === 'token') && parsed.content) {
            if (!stream.thinkDone && stream.thinkStartTime) {
              finalizeThinkArea(stream);
            }
            stream.answerContent += parsed.content;
            updateAnswerArea(stream);
          }

          if (parsed.type === 'done') {
            stream.answerEl.classList.remove('streaming-cursor');
            if (!stream.answerContent) {
              stream.answerEl.textContent = parsed.content || '(无响应)';
            }
            if (stream.thinkStartTime && !stream.thinkDone) {
              finalizeThinkArea(stream);
            }
            if (stream.searchData && stream.searchEl.classList.contains('hidden')) {
              renderSearchArea(stream);
            }
          }
        } catch (e) {
          // ignore parse errors
        }
      }
    }

    stream.answerEl.classList.remove('streaming-cursor');
    if (!stream.answerContent) {
      stream.answerEl.textContent = '(无响应)';
    }

    if (state.currentSessionId) {
      await loadSessions();
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      stream.answerEl.classList.remove('streaming-cursor');
      if (!stream.answerContent) {
        stream.answerEl.textContent = '(已停止生成)';
      }
      return;
    }
    stream.answerEl.textContent = '请求失败: ' + e.message;
    stream.answerEl.classList.remove('streaming-cursor');
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
  localStorage.setItem('showThought', state.showThought ? '1' : '0');
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

    if (localStorage.getItem('showThought') === null) {
      state.showThought = config.show_thought || false;
    } else {
      state.showThought = localStorage.getItem('showThought') === '1';
    }
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

  $('#theme-toggle').addEventListener('click', toggleTheme);

  if (localStorage.getItem('showThought') === '1') {
    state.showThought = true;
  }
  updateThoughtButton();

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