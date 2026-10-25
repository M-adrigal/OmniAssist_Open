/* 06-attachments-files.js
 * 附件与云文件：上传附件 chips、云文件列表/筛选/渲染
 *
 * 由 static/app.js 拆分而来（原第 2300-2577 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
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
      ${f.type === 'image'
        ? `<img class="chip-thumb" src="/api/files/download?path=${encodeURIComponent(f.path)}&inline=true" alt="">`
        : getFileChipIcon(f.type)}
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
      refreshChatModelBadge();
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

/* 表格行内操作图标：4 个文字按钮宽约 280px，会把表格挤出横向滚动条；
   等宽图标按钮省一半宽度，语义靠 title 兜底 */
const ICON = {
  eye: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="2.6"/></svg>',
  download: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>',
  pen: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  trash: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>',
};

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
          <button class="btn-preview" data-idx="${i}" title="预览">${ICON.eye}</button>
          <a class="btn-download" href="/api/files/download?path=${encodeURIComponent(f.path)}" target="_blank" rel="noopener" title="下载">${ICON.download}</a>
          <button class="btn-rename" data-idx="${i}" title="重命名">${ICON.pen}</button>
          <button class="btn-delete-cloud" data-idx="${i}" title="删除">${ICON.trash}</button>
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
