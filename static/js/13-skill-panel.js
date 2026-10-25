/* 13-skill-panel.js
 * 「+」菜单 → 技能二级面板：点菜单「技能」项，在其右缘联动展开技能卡片
 * （顶部搜索 / 徽章列表 / 底部「从本地添加技能」「管理技能」）。
 * 点行 = 选用/取消，选用技能在下一条消息里显式注入（见 05-chat-actions.js）。
 *
 * ⚠️ 与其它 js/ 文件共享全局作用域；$ / $$ 是 01-core.js 的全局助手。
 */
let _skillPanelData = { system: [], user: [] };
let _skillPanelOpen = false;

// ===== 面板开合（二级形态：贴一级菜单右缘，菜单保持展开）=====
function openSkillPanel() {
  const panel = $('#skill-panel');
  const mask = $('#skill-panel-mask');
  const menu = $('#attach-menu');
  const menuItem = document.querySelector('.attach-menu-item[data-action="skill"]');
  if (!panel || !menu) return;

  loadSkillPanel();

  // 贴菜单右缘定位；右侧空间不足时翻到左侧
  const rect = menu.getBoundingClientRect();
  const W = 360;
  let left = rect.right + 8;
  if (left + W > window.innerWidth - 12) left = Math.max(12, rect.left - W - 8);
  let top = rect.top;
  const maxH = Math.min(520, window.innerHeight - 120);
  if (top + maxH > window.innerHeight - 12) top = Math.max(12, window.innerHeight - maxH - 12);
  panel.style.left = left + 'px';
  panel.style.top = top + 'px';

  panel.classList.remove('hidden');
  if (mask) mask.classList.remove('hidden');
  if (menuItem) menuItem.classList.add('active');
  _skillPanelOpen = true;
}

function closeSkillPanel() {
  const panel = $('#skill-panel');
  const mask = $('#skill-panel-mask');
  if (panel) panel.classList.add('hidden');
  if (mask) mask.classList.add('hidden');
  document.querySelectorAll('.attach-menu-item.active').forEach(el => el.classList.remove('active'));
  _skillPanelOpen = false;
}

// ===== 数据加载 =====
async function loadSkillPanel() {
  const box = $('#skill-panel-list');
  if (!box) return;
  box.innerHTML = '<p class="loading-text">加载中...</p>';
  try {
    const [all, userOnly] = await Promise.all([
      API.get('/api/skills'),
      API.get('/api/skills?user_only=1'),
    ]);
    _skillPanelData.system = all.system_skills || [];
    _skillPanelData.user = userOnly.user_skills || [];
    renderSkillPanelList();
  } catch (e) {
    box.innerHTML = `<p class="loading-text" style="color:var(--danger);">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function renderSkillPanelList() {
  const box = $('#skill-panel-list');
  if (!box) return;
  const q = ($('#skill-panel-q').value || '').trim().toLowerCase();

  const rows = [];
  const push = (list, kind) => {
    list.forEach(s => {
      const name = s.name || s.skill_name || '';
      if (!name) return;
      const desc = s.description || '';
      if (q && !name.toLowerCase().includes(q) && !desc.toLowerCase().includes(q)) return;
      rows.push({ name, desc, enabled: s.enabled !== false, kind, source: s.source });
    });
  };
  push(_skillPanelData.system, '系统');
  push(_skillPanelData.user, '我的');

  if (rows.length === 0) {
    box.innerHTML = '<p class="loading-text" style="padding:20px;">暂无技能</p>';
    return;
  }

  box.innerHTML = rows.map(r => {
    const checked = state.selectedSkills.includes(r.name);
    const initial = badgeInitial(r.name);
    return `
      <div class="skill-row${checked ? ' selected' : ''}${r.enabled ? '' : ' disabled'}" data-name="${escapeHtml(r.name)}" role="button" tabindex="0">
        <span class="skill-badge ${badgeColor(r.name)}">${escapeHtml(initial)}</span>
        <span class="skill-row-name">${escapeHtml(r.name)}<span class="skill-row-tag">${r.kind}</span></span>
        <span class="skill-row-desc">${escapeHtml(r.desc)}</span>
        <svg class="skill-row-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
      </div>`;
  }).join('');

  // 点行 = 选用/取消（禁用技能不可选）；state.selectedSkills 只存内存，刷新即清
  box.querySelectorAll('.skill-row').forEach(row => {
    const toggle = () => {
      if (row.classList.contains('disabled')) return;
      const name = row.dataset.name;
      if (state.selectedSkills.includes(name)) {
        state.selectedSkills = state.selectedSkills.filter(n => n !== name);
        row.classList.remove('selected');
      } else {
        state.selectedSkills.push(name);
        row.classList.add('selected');
      }
    };
    row.addEventListener('click', toggle);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });
}

// 徽章首字符：中文取首字，英文取大写首字母
function badgeInitial(name) {
  const ch = (name || '?').trim().charAt(0);
  return /[a-z]/.test(ch) ? ch.toUpperCase() : ch;
}

// 徽章色：按名字稳定取色（同一技能每次颜色一致）
function badgeColor(name) {
  const palette = ['bg-green', 'bg-amber', 'bg-blue', 'bg-teal', 'bg-purple', 'bg-pink'];
  let h = 0;
  for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) % 997;
  return palette[h % palette.length];
}

// ===== 上传本地技能 =====
// 选择文件 → POST /api/skills/upload（FormData）→ 三态渲染
function pickSkillFile() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.zip,.md';
  input.onchange = () => {
    if (input.files.length > 0) uploadSkillFile(input.files[0]);
  };
  input.click();
}

async function uploadSkillFile(file, acknowledge) {
  const body = $('#skill-upload-body');
  const title = $('#skill-upload-title');
  $('#skill-upload-file').textContent = file.name;
  body.innerHTML = '<p class="loading-text">正在解析与扫描...</p>';
  try {
    const fd = new FormData();
    fd.append('file', file);
    const url = '/api/skills/upload' + (acknowledge ? '?acknowledge=true' : '');
    const res = await fetch(url, { method: 'POST', body: fd });
    const data = await res.json().catch(() => ({}));

    if (!res.ok) {
      // 400 = 结构错误或 P0 拦截；409 = 同名冲突
      const detail = data.detail || {};
      renderSkillUploadResult(title, body, {
        status: detail.code === 'blocked' ? 'blocked' : 'error',
        message: detail.message || data.detail || ('上传失败 ' + res.status),
        findings: detail.findings || [],
      });
      return;
    }

    if (data.status === 'needs_confirm') {
      renderSkillUploadResult(title, body, {
        status: 'confirm',
        name: data.name,
        message: `「${data.name}」包含敏感操作，确认后才会安装`,
        findings: (data.scan && data.scan.findings) || [],
        file: file,
      });
      return;
    }

    // installed
    renderSkillUploadResult(title, body, {
      status: 'ok',
      name: data.name,
      message: `「${data.name}」已安装（${data.scripts_count || 0} 个脚本），可在技能面板勾选使用`,
    });
    loadSkillPanel();   // 面板列表刷新
  } catch (e) {
    renderSkillUploadResult(title, body, { status: 'error', message: e.message });
  }
}

// 三态结果渲染。confirm 态带「确认安装」按钮（带 acknowledge=true 重传同一文件）
function renderSkillUploadResult(title, body, r) {
  title.textContent = '上传本地技能';
  let html = '';
  if (r.status === 'ok') {
    html = `<div class="skill-upload-banner ok">${escapeHtml(r.message)}</div>`;
  } else if (r.status === 'confirm') {
    html = `<div class="skill-upload-banner warn">${escapeHtml(r.message)}</div>`;
  } else {
    html = `<div class="skill-upload-banner block">${escapeHtml(r.message)}</div>`;
  }

  if (r.findings && r.findings.length) {
    html += '<div class="skill-upload-findings">' + r.findings.map(f => `
      <div class="skill-finding severity-${f.severity === 'block' ? 'block' : 'confirm'}">
        <div class="skill-finding-msg">${escapeHtml(f.message)}</div>
        <div class="skill-finding-meta">${escapeHtml(f.rule || '')}${f.file ? ' · ' + escapeHtml(f.file) : ''}${f.line ? ':' + f.line : ''}</div>
      </div>`).join('') + '</div>';
  }

  if (r.status === 'confirm' && r.file) {
    html += `<button class="btn-sm btn-accent" id="btn-skill-confirm">我了解上述风险，确认安装</button>
             <button class="btn-sm" id="btn-skill-cancel-confirm" style="margin-left:8px;">取消</button>`;
  } else if (r.status === 'ok') {
    html += `<button class="btn-sm" id="btn-skill-upload-another">再传一个</button>`;
  } else {
    html += `<button class="btn-sm" id="btn-skill-upload-retry">重新选择</button>`;
  }
  body.innerHTML = html;

  const bind = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
  bind('btn-skill-confirm', () => {
    // 同一文件重传 + acknowledge=true；P0 此时仍会被服务端拒绝
    uploadSkillFile(r.file, true);
  });
  bind('btn-skill-cancel-confirm', resetSkillUploadModal);
  bind('btn-skill-upload-another', resetSkillUploadModal);
  bind('btn-skill-upload-retry', resetSkillUploadModal);
}

// acknowledge 重传：同一文件 + ?acknowledge=true；P0 此时仍会被服务端拒绝
function resetSkillUploadModal() {
  const body = $('#skill-upload-body');
  const title = $('#skill-upload-title');
  title.textContent = '上传本地技能';
  body.innerHTML = `
    <p style="color:var(--text-muted);font-size:13px;margin:0 0 12px;">
      支持 zip 压缩包（Anthropic 技能规范：内含 SKILL.md 的文件夹）或单个 SKILL.md 文件。
      安装前会自动扫描风险：涉及敏感操作需确认，高风险直接拦截。
    </p>
    <button class="btn-sm btn-accent" id="btn-skill-pick-file">选择文件</button>
    <span id="skill-upload-file" style="margin-left:8px;font-size:12px;color:var(--text-muted);"></span>`;
  $('#btn-skill-pick-file').addEventListener('click', pickSkillFile);
}

// ===== 初始化 =====
document.addEventListener('DOMContentLoaded', () => {
  const mask = $('#skill-panel-mask');
  if (mask) mask.addEventListener('click', closeSkillPanel);
  const search = $('#skill-panel-q');
  if (search) search.addEventListener('input', renderSkillPanelList);
  const upBtn = $('#btn-skill-upload');
  // 弹窗 z-index 低于二级面板，先收面板再开弹窗，否则弹窗被盖住
  if (upBtn) upBtn.addEventListener('click', () => { closeSkillPanel(); resetSkillUploadModal(); openModal('modal-skill-upload'); });
  const mgBtn = $('#btn-skill-manage');
  if (mgBtn) mgBtn.addEventListener('click', () => { closeSkillPanel(); loadSkills(); openModal('modal-skills'); });

  // Esc：弹窗开着时优先关弹窗（由弹窗自身逻辑处理），否则关面板
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !_skillPanelOpen) return;
    const modalOpen = $('#modal-skill-upload') && !$('#modal-skill-upload').classList.contains('hidden');
    if (!modalOpen) closeSkillPanel();
  });
});
