/* 07-skills.js
 * 技能管理：列表/切换/启停/预览
 *
 * 由 static/app.js 拆分而来（原第 2578-2779 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
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
  // 筛选行只在用户技能 tab 出现（系统技能没有来源/卸载概念）
  const bar = $('#skill-user-filterbar');
  if (bar) bar.style.display = tab === 'user' ? '' : 'none';
  // 回收站同理：它是「用户技能」的附属区。切到系统技能必须收起，
  // 否则用户在这里点开过回收站、再切 tab，那块会一直赖在页面上。
  const trash = $('#skill-trash-panel');
  if (trash && tab !== 'user') trash.classList.add('hidden');
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

  // 用户技能按来源/启停筛选（上传安装 / 手动创建 / 文件放置）
  if (_skillTab === 'user') {
    const f = ($('#skill-user-filter') || {}).value || 'all';
    if (f === 'enabled') filtered = filtered.filter(s => s.enabled !== false);
    else if (f === 'disabled') filtered = filtered.filter(s => s.enabled === false);
    else if (f === 'uploaded') filtered = filtered.filter(s => s.source === 'uploaded');
    else if (f === 'editor') filtered = filtered.filter(s => s.source !== 'uploaded' && s.source !== 'filesystem');
    else if (f === 'filesystem') filtered = filtered.filter(s => s.source === 'filesystem');
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
    if (s.source === 'uploaded') meta.push('上传安装');
    else if (s.source === 'filesystem') meta.push('文件');
    if (s.risk_level === 'medium') meta.push('敏感操作');
    return `
      <div class="skill-item${enabled ? '' : ' disabled'}" data-name="${escapeHtml(name)}">
        <span class="skill-badge ${badgeColor(name)}">${escapeHtml(badgeInitial(name))}</span>
        <div class="skill-info">
          <div class="skill-name">
            ${escapeHtml(name)}
            ${meta.length ? `<span class="skill-meta">${escapeHtml(meta.join(' · '))}</span>` : ''}
          </div>
          ${desc ? `<div class="skill-desc" title="${escapeHtml(desc)}">${escapeHtml(desc)}</div>` : ''}
        </div>
        <div class="skill-actions">
          <button class="btn-preview" data-skill-action="preview" data-idx="${idx}">预览</button>
          <button class="btn-preview" data-skill-action="export" data-idx="${idx}"
                  title="导出为 zip（SKILL.md + scripts/），可备份或分享给他人上传">导出</button>
          ${isSystem ? '' : '<button class="btn-preview skill-uninstall" data-skill-action="uninstall" data-idx="' + idx + '">卸载</button>'}
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
      const action = btn.dataset.skillAction;
      if (action === 'preview') {
        previewSkill(skill, isSystem);
      } else if (action === 'export') {
        exportSkill(skill);
      } else if (action === 'uninstall') {
        uninstallUserSkill(skill);
      } else {
        toggleSkill(skill, isSystem, btn);
      }
    });
  });
}

// ===== 卸载（软删：备份进回收站，保留 30 天可恢复）=====
async function uninstallUserSkill(skill) {
  const name = skill.name || skill.skill_name;
  if (!confirm(`卸载「${name}」？\n\n卸载后进入回收站保留 30 天，期间可从回收站恢复。`)) return;
  try {
    await API.del(`/api/skills/upload/${encodeURIComponent(name)}`);
    showToast(`技能「${name}」已卸载`, 'success');
    await loadSkills();
    if (!$('#skill-trash-panel').classList.contains('hidden')) loadSkillTrash();
  } catch (e) {
    showToast(`卸载失败: ${e.message}`, 'error');
  }
}

// ===== 回收站 =====
function toggleSkillTrash() {
  const panel = $('#skill-trash-panel');
  if (!panel) return;
  if (panel.classList.contains('hidden')) {
    panel.classList.remove('hidden');
    loadSkillTrash();
  } else {
    panel.classList.add('hidden');
  }
}

async function loadSkillTrash() {
  // ⚠️ $ 是 querySelector 语义，必须带 #。曾写成 $('skill-trash-list')，
  //    查不到元素 → 这里直接 return，界面永远停在静态的「加载中...」，且不报错。
  const box = $('#skill-trash-list');
  if (!box) return;
  box.innerHTML = '<p class="loading-text">加载中...</p>';
  try {
    const data = await API.get('/api/skills/trash');
    const items = data.items || [];
    if (items.length === 0) {
      box.innerHTML = '<p class="loading-text" style="padding:12px;">回收站为空</p>';
      return;
    }
    box.innerHTML = items.map(it => `
      <div id="skill-trash-item">
        <span class="trash-name">
          <span class="skill-badge ${badgeColor(it.name)}">${escapeHtml(badgeInitial(it.name))}</span>
          <span>${escapeHtml(it.name)}${it.risk_level === 'medium' ? ' <span class="skill-meta">敏感操作</span>' : ''}</span>
        </span>
        <span>
          <span class="trash-meta">${escapeHtml(it.deleted_at || '')}</span>
          <button class="skill-mg-ghostbtn" data-trash-restore="${escapeHtml(it.name)}" style="margin-left:8px;">恢复</button>
          <button class="skill-mg-ghostbtn skill-trash-del" data-trash-delete="${escapeHtml(it.backup)}"
                  data-trash-name="${escapeHtml(it.name)}" style="margin-left:6px;">彻底删除</button>
        </span>
      </div>`).join('');
    box.querySelectorAll('[data-trash-delete]').forEach(btn => {
      btn.addEventListener('click', () => {
        deleteTrashItem(btn.dataset.trashDelete, btn.dataset.trashName, btn);
      });
    });
    box.querySelectorAll('[data-trash-restore]').forEach(btn => {
      btn.addEventListener('click', async () => {
        try {
          await API.post(`/api/skills/upload/${encodeURIComponent(btn.dataset.trashRestore)}/restore`, {});
          showToast(`技能「${btn.dataset.trashRestore}」已恢复`, 'success');
          await loadSkills();
          await loadSkillTrash();
        } catch (e) {
          showToast(`恢复失败: ${e.message}`, 'error');
        }
      });
    });
  } catch (e) {
    box.innerHTML = `<p class="loading-text" style="color:var(--danger);">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function exportSkill(skill) {
  const name = skill.name || skill.skill_name || '';
  if (!name) return;
  // 走 <a download>：浏览器会带上 Cookie，且失败时能落到后端统一的 JSON 错误里
  const a = document.createElement('a');
  a.href = `/api/skills/export/${encodeURIComponent(name)}`;
  a.download = `${name}.zip`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  showToast(`正在导出「${name}」`, 'success');
}

async function deleteTrashItem(backup, name, btn) {
  if (!confirm(`彻底删除「${name}」的备份？\n\n此操作不可恢复（不是移入回收站，而是直接删除）。`)) return;
  if (btn) btn.disabled = true;
  try {
    await API.del(`/api/skills/trash/${encodeURIComponent(backup)}`);
    showToast(`「${name}」的备份已彻底删除`, 'success');
    await loadSkillTrash();
  } catch (e) {
    showToast(`删除失败: ${e.message}`, 'error');
    if (btn) btn.disabled = false;
  }
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
