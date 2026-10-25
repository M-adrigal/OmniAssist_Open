/* 03-messages.js
 * 消息渲染：消息气泡、历史/压缩历史、内容渲染、代码块复制、操作行（复制/重新生成）
 *
 * 由 static/app.js 拆分而来（原第 703-1313 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
function clearMessages() {
  const sc = getActiveContainer();
  sc.element.innerHTML = _welcomeHtml();
}

// ===== 消息操作 =====
// 复制只有图标（对勾顶替复制，1.4s 后还原，不写中文、不撑高这一行）；
// 重新生成带文字：单靠一个回转箭头认不出是"重跑这轮"还是"刷新页面"，而且它只挂在最后一条回答上，
// 出现频率低，多两个字反而更好找。
const MSG_ACTIONS_HTML = `
  <div class="msg-actions">
    <button type="button" class="msg-act" data-copy="1" title="复制" aria-label="复制">
      <svg class="ico-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V6.5A2.5 2.5 0 0 1 7.5 4H16"/></svg>
      <svg class="ico-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>
    </button>
  </div>`;

const REGEN_BTN_HTML = `
  <button type="button" class="msg-act" data-regenerate="1" title="重新生成这条回答">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11.5a8 8 0 1 1-2.3-5.4"/><path d="M20.5 4.5v4.2h-4.2"/></svg>
    <span>重新生成</span>
  </button>`;

// 只有"最后一条消息且是助手回答"才挂重新生成：每条回答都挂一个，会让人以为能回到任意一轮
function syncMsgActions(container) {
  let root = container || (state.sessionContainers[state.currentSessionId] || {}).element;
  // 调用方可能直接递进来一个 .message（流式容器就是它本身）：先回到消息列表这一层，
  // 否则 root.querySelectorAll('.message') 在气泡内部找，永远找不到"最后一条"。
  if (root && root.classList && root.classList.contains('message')) root = root.parentElement;
  if (!root) return;
  root.querySelectorAll('.msg-act[data-regenerate]').forEach(b => b.remove());
  const msgs = root.querySelectorAll('.message');
  const last = msgs[msgs.length - 1];
  if (!last || !last.classList.contains('assistant')) return;
  if (last.querySelector('.answer-area.streaming-cursor')) return;
  const actions = last.querySelector('.msg-actions');
  if (!actions || actions.querySelector('[data-regenerate]')) return;
  actions.insertAdjacentHTML('beforeend', REGEN_BTN_HTML);
}

// 流式回答的容器一开始没有操作行（内容还在长），收尾时才补上
function attachMsgActions(msgEl) {
  const body = msgEl && msgEl.querySelector('.message-body');
  if (!body || body.querySelector('.msg-actions')) return;
  body.insertAdjacentHTML('beforeend', MSG_ACTIONS_HTML);
}

// ===== 消息渲染 =====
// ===== 附件展示（对话气泡 / 历史消息共用）=====
// 图片直接显示缩略图，点击看原图；其它类型仍是文件名 chip。
// ⚠️ 为什么不用「缩略图文件」：原图通常几百 KB，浏览器有 max-width 约束就够，
//    额外生成缩略图要多一套磁盘缓存与失效逻辑，不值得。
const _IMG_EXT_RE = /\.(png|jpe?g|gif|webp|bmp)$/i;

function attachmentThumbUrl(name, path) {
  // 当前轮上传：上传接口给了完整相对路径。
  // ⚠️ 必须走 download + inline=true，不能走 preview ——
  //    preview 对图片返回的是 JSON（{"type":"image","path":...}），
  //    <img src> 拿到 JSON 就是一张白图（alt 文本露出来）。
  if (path) {
    return `/api/files/download?path=${encodeURIComponent(path)}&inline=true`;
  }
  // 历史消息只存了文件名：先从当前会话的文件清单里查真实路径
  const meta = (state.uploadFiles || {})[name];
  if (meta && meta.path) {
    return `/api/files/download?path=${encodeURIComponent(meta.path)}&inline=true`;
  }
  // 查不到（刚切换完、清单还没到）才退到「会话 + 文件名」接口
  const sid = state.currentSessionId || '';
  return `/api/files/upload-thumb?session_id=${encodeURIComponent(sid)}`
       + `&filename=${encodeURIComponent(name)}`;
}

// 图片加载失败时退化成文件名 chip。
// 为什么需要：端点挂掉 / 文件被清理 / 权限变化都会让 <img> 白掉，
// 浏览器会把 alt 文本画出来——用户看到的是一个"坏掉的图片"，比不给图还糟。
document.addEventListener('error', function (e) {
  const img = e.target;
  if (!img || img.tagName !== 'IMG' || !img.classList.contains('msg-attach-img')) return;
  const cell = img.closest('.msg-attach-thumb');
  const name = img.dataset.name || img.alt || '图片';
  const meta = (state.uploadFiles || {})[name] || {};
  const iconMap = { text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C',
                    image: '图' };
  const icon = iconMap[meta.type] || 'F';
  const chip = document.createElement('span');
  chip.className = 'msg-attach-tag';
  chip.title = '原图暂时无法加载（可能已被清理），点开这条消息重发一次即可';
  chip.innerHTML = '<span class="tag-icon">' + icon + '</span>' + escapeHtml(name);
  const item = img.closest('.msg-attach-item') || cell;
  if (item) item.replaceWith(chip);
  else img.remove();
}, true);

// 超宽长条图（截图里很常见，如 2716x96）按普通缩略图缩放后只剩几像素高，
// 会被当成"坏图/一条线"。这类图改成铺满气泡宽度，并在下面补一行说明。
const _WIDE_RATIO = 5;

document.addEventListener('load', function (e) {
  const img = e.target;
  if (!img || img.tagName !== 'IMG' || !img.classList.contains('msg-attach-img')) return;
  if (!img.naturalWidth || !img.naturalHeight) return;
  if (img.naturalWidth / img.naturalHeight < _WIDE_RATIO) return;
  const cell = img.closest('.msg-attach-thumb');
  if (!cell) return;
  cell.classList.add('is-wide');
  const item = cell.closest('.msg-attach-item') || cell;
  if (!item.querySelector('.msg-attach-widehint')) {
    const hint = document.createElement('span');
    hint.className = 'msg-attach-widehint';
    hint.textContent = `宽图 ${img.naturalWidth}×${img.naturalHeight} · 点击查看原图`;
    item.appendChild(hint);
  }
}, true);

// ===== 图片预览弹窗 =====
// 点缩略图在本页看原图：不用新开标签页（回来还得重新找会话），
// 关闭方式三选一：右上角 ×、点背景、Esc。
const _LIGHTBOX_HTML = `
  <div class="lightbox-backdrop" data-lightbox-close></div>
  <img class="lightbox-img" src="" alt="">
  <button class="lightbox-close" data-lightbox-close aria-label="关闭">×</button>
  <div class="lightbox-caption"></div>`;

function _ensureLightbox() {
  let box = document.getElementById('image-lightbox');
  if (box) return box;
  box = document.createElement('div');
  box.id = 'image-lightbox';
  box.className = 'hidden';
  box.setAttribute('role', 'dialog');
  box.setAttribute('aria-modal', 'true');
  box.innerHTML = _LIGHTBOX_HTML;
  document.body.appendChild(box);
  box.addEventListener('click', (e) => {
    if (e.target.closest('[data-lightbox-close]')) closeImageLightbox();
  });
  return box;
}

function openImageLightbox(src, caption) {
  if (!src) return;
  const box = _ensureLightbox();
  const img = box.querySelector('.lightbox-img');
  const cap = box.querySelector('.lightbox-caption');
  img.src = src;
  img.alt = caption || '';
  cap.textContent = caption || '';
  box.classList.remove('hidden');
  document.body.classList.add('lightbox-open');
  const btn = box.querySelector('.lightbox-close');
  if (btn) btn.focus();
}

function closeImageLightbox() {
  const box = document.getElementById('image-lightbox');
  if (!box || box.classList.contains('hidden')) return;
  box.classList.add('hidden');
  box.querySelector('.lightbox-img').src = '';
  document.body.classList.remove('lightbox-open');
}

// 点缩略图 → 弹窗（阻止默认的"新标签页打开"）
document.addEventListener('click', function (e) {
  const cell = e.target.closest && e.target.closest('.msg-attach-thumb');
  if (!cell) return;
  const img = cell.querySelector('img');
  if (!img) return;
  e.preventDefault();
  openImageLightbox(img.currentSrc || img.src, img.dataset.name || img.alt || '');
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeImageLightbox();
});

function renderAttachmentChips(files) {
  if (!files || !files.length) return '';
  const tags = files.map(f => {
    // 兼容两种形态：对象 {filename,path,type} 与纯文件名字符串（历史消息）
    const name = typeof f === 'string' ? f : (f && f.filename) || '';
    if (!name) return '';
    // 历史消息的 uploaded_files 只有文件名，类型要从会话文件清单补 ——
    // 否则文档会退化成通用 'F' 图标（图片倒是能靠扩展名认出来）
    const meta = (state.uploadFiles || {})[name] || {};
    const ftype = (f && f.type) || meta.type || '';
    const isImg = ftype === 'image' || _IMG_EXT_RE.test(name);
    if (isImg) {
      const url = attachmentThumbUrl(name, (f && f.path) || '');
      // 包一层 .msg-attach-item：宽图的说明行要单独占一行。
      // 说明行如果直接作为 .msg-attachments 的 flex 子项，会参与"最大内容宽度"
      // 计算，把气泡一路撑到 78%（右侧留白就是这么来的）。
      return `<span class="msg-attach-item">
                <a class="msg-attach-thumb" href="${escapeHtml(url)}" target="_blank"
                   rel="noopener" title="${escapeHtml(name)}（点击查看原图）">
                  <img class="msg-attach-img" src="${escapeHtml(url)}" alt="${escapeHtml(name)}"
                       data-name="${escapeHtml(name)}" loading="lazy">
                </a>
              </span>`;
    }
    const iconMap = { text: 'T', pdf: 'P', docx: 'W', xlsx: 'E', pptx: 'S', csv: 'C' };
    const icon = iconMap[ftype] || 'F';
    return `<span class="msg-attach-tag"><span class="tag-icon">${icon}</span>${escapeHtml(name)}</span>`;
  }).filter(Boolean).join('');
  return tags ? `<div class="msg-attachments">${tags}</div>` : '';
}

function appendMessage(role, content, attachments, targetContainer) {
  const container = targetContainer || getActiveContainer().element;
  const welcome = container.querySelector('.welcome-message');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `message ${role}`;
  const attachHtml = renderAttachmentChips(attachments);
  div.innerHTML = `
    <div class="avatar">${role === 'user' ? 'U' : 'AI'}</div>
    <div class="message-body"><div class="answer-area">${renderContent(content)}${attachHtml}</div>
      ${role === 'assistant' && content ? MSG_ACTIONS_HTML : ''}
    </div>
  `;
  container.appendChild(div);
  scrollToBottom();
  // 渲染完顺手校准"重新生成"挂在谁身上：最后一条若不是助手回答，上一个气泡的按钮要撤掉
  syncMsgActions(container);
  return div;
}

// 「上下文已重置」分隔线：标记之前的消息还留在会话里（可以往回翻），
// 但不会再送给模型。没有这条线的话，用户分不清哪段是模型还记得的。
function renderContextResetDivider(container) {
  const div = document.createElement('div');
  div.className = 'context-reset-divider';
  div.setAttribute('role', 'separator');
  div.innerHTML = '<span>上下文已重置 · 以上内容不再发送给模型</span>';
  container.appendChild(div);
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

  if (m && m.context_reset) {
    renderContextResetDivider(container);
    return;
  }

  if (role === 'system' && compressedMeta && compressedMeta.rounds && compressedMeta.rounds.length > 0) {
    renderCompressedHistory(content, compressedMeta, container);
    return;
  }

  if (role === 'user') {
    // 历史里的附件：后端在用户消息上存了 uploaded_files（只有文件名）。
    // 传下去让 chip / 图片缩略图一起复原 —— 之前这里固定传 null，
    // 导致刷新后"我传过什么"完全消失。
    appendMessage(role, content, (m && m.uploaded_files) || null, container);
    return;
  }

  if (!hasMeta) {
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
      ${content ? MSG_ACTIONS_HTML : ''}
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

  syncMsgActions(container);
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
