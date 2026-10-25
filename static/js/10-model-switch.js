/* 10-model-switch.js
 * 模型切换：聊天工具栏模型菜单、思考模式开关
 *
 * 由 static/app.js 拆分而来（原第 3613-3810 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
let _chatModels = [];
let _chatResolved = {};
let _pendingSwitch = null;

async function refreshChatModelBadge() {
  const label = $('#model-label');
  if (!label) return;
  try {
    const sid = state.currentSessionId || '';
    const url = '/api/config/models' + (sid ? `?session_id=${encodeURIComponent(sid)}` : '');
    const data = await API.get(url);
    _chatModels = data.models || [];
    _chatResolved = data.resolved || {};
    // 管理员把模型全部隐藏时，这里必须说「无可用模型」——
    // 之前会显示上次选中的那个（已下架）模型名，看着像能用。
    const _hasModels = (_chatModels || []).length > 0;
    label.textContent = _hasModels ? (_chatResolved.display_name || '模型') : '无可用模型';
    const btn = $('#btn-model');
    if (btn) {
      btn.classList.toggle('model-unavailable', !_hasModels);
      if (!_hasModels) {
        btn.title = '管理员未开放任何模型，请联系管理员开通';
      } else {
        const who = _chatResolved.owner === 'user' ? '（自配 Key）' : '';
        btn.title = `${_chatResolved.display_name || '未配置模型'} · ${_chatResolved.provider_name || ''}${who}`;
      }
    }
  } catch (e) {
    label.textContent = '模型';
  }
}

// 列表首字图标：显示名首字符（英文取大写），代替厂商 logo——没有可用的 logo 资源，
// 首字符至少能让用户按「同一个模型族」快速扫读，且不会引入外部图片依赖。
function _modelInitial(name) {
  const s = (name || '').trim();
  if (!s) return '·';
  const ch = Array.from(s)[0];   // 用 Array.from 取首个完整字符，避免 emoji 被切成半个代理对
  return /[a-z]/i.test(ch) ? ch.toUpperCase() : ch;
}

function closeModelMenu() {
  const m = $('#model-menu');
  if (m) m.classList.add('hidden');
  const btn = $('#btn-model');
  if (btn) btn.classList.remove('open');
}

function renderModelMenuList() {
  const list = $('#model-menu-list');
  if (!list) return;

  if (!_chatModels.length) {
    list.innerHTML = '<div class="mm-empty">没有可用模型<br>'
      + '<span>点下方「配置自定义模型」添加，或联系管理员开通</span></div>';
    return;
  }

  const curId = _chatResolved.entry_id;
  list.innerHTML = _chatModels.map(x => {
    const active = x.entry_id === curId;
    const tags = [
      `<span class="mm-tag${x.owner === 'user' ? ' mm-tag-user' : ''}">`
      + (x.owner === 'user' ? '自配' : '平台') + '</span>',
    ];
    if (x.is_default) tags.push('<span class="mm-tag">默认</span>');

    // 右侧数值位：选中项放勾；其余放上下文长度（没有倍率数据，不用假数字占位）
    const right = active
      ? '<svg class="mm-check" width="16" height="16" viewBox="0 0 24 24" fill="none" '
        + 'stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">'
        + '<path d="M20 6L9 17l-5-5"/></svg>'
      : `<span class="mm-ctx">${escapeHtml(x.context_limit || '—')}</span>`;

    return `<button type="button" class="mm-item${active ? ' active' : ''}" `
      + `data-entry="${x.entry_id}" title="${escapeHtml(x.model_id || x.display_name || '')}">`
      + `<span class="mm-ico">${escapeHtml(_modelInitial(x.display_name))}</span>`
      + '<span class="mm-text">'
      + `<span class="mm-name">${escapeHtml(x.display_name)}</span>`
      + `<span class="mm-tags">${tags.join('')}</span>`
      + '</span>'
      + right
      + '</button>';
  }).join('');
}

function openModelMenu() {
  const m = $('#model-menu');
  if (!m) return;
  if (!m.classList.contains('hidden')) { closeModelMenu(); return; }

  renderModelMenuList();
  m.classList.remove('hidden');
  const btn = $('#btn-model');
  if (btn) {
    btn.classList.add('open');
    // 菜单比按钮宽得多：让菜单右边缘与按钮右边缘对齐（向左展开），
    // 避免从工具栏最左侧展开、离点击位置太远。
    const left = btn.offsetLeft + btn.offsetWidth - m.offsetWidth;
    m.style.left = Math.max(0, left) + 'px';
  }
  // 模型多时把当前项滚进可视区，避免打开后还要自己找
  const act = m.querySelector('.mm-item.active');
  if (act) act.scrollIntoView({ block: 'nearest' });
}

async function onPickModel(entryId) {
  closeModelMenu();
  entryId = parseInt(entryId, 10);
  const target = _chatModels.find(x => x.entry_id === entryId);
  if (!target) return;
  if (_chatResolved.entry_id === entryId) return;

  // 还没有会话：不存在「本会话」可切，直接改默认模型，不弹成本提示（还没开始花钱）
  if (!state.currentSessionId) {
    try {
      await API.put('/api/config/current-model', { entry_id: entryId });
      showToast('默认模型已更新', 'success');
      await refreshChatModelBadge();
    } catch (e) {
      showToast('切换失败: ' + e.message, 'error');
    }
    return;
  }

  _pendingSwitch = target;
  const fromEl = $('#switch-from-name');
  const toEl = $('#switch-to-name');
  if (fromEl) fromEl.textContent = _chatResolved.display_name || '当前模型';
  if (toEl) toEl.textContent = target.display_name;

  const meta = [];
  if (target.provider_name) meta.push(target.provider_name);
  meta.push(target.owner === 'user' ? '自配 Key' : '平台 Key');
  if (target.context_limit) meta.push('上下文 ' + target.context_limit);
  const metaEl = $('#switch-to-meta');
  if (metaEl) metaEl.textContent = meta.join(' · ');

  // 切到自配模型等于把内容送到另一家的地址，这点必须当场说清楚
  const note = $('#switch-privacy-note');
  if (note) {
    if (target.owner === 'user' && target.provider_name) {
      note.textContent = `本会话的对话内容与上传文件会发送到「${target.provider_name}」。`;
      note.classList.remove('hidden');
    } else {
      note.classList.add('hidden');
    }
  }

  openModal('modal-switch-model');
}

async function confirmSwitchModel() {
  if (!_pendingSwitch) return;
  const entryId = _pendingSwitch.entry_id;
  const name = _pendingSwitch.display_name;
  const btn = $('#btn-confirm-switch-model');
  if (btn) btn.disabled = true;
  try {
    await API.put(`/api/sessions/${state.currentSessionId}/model`, { entry_id: entryId });
    _pendingSwitch = null;
    closeModal('modal-switch-model');
    showToast(`已切换到 ${name}`, 'success');
    await refreshChatModelBadge();
  } catch (e) {
    showToast('切换失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initModelSwitchEvents() {
  const btn = $('#btn-model');
  if (btn) btn.addEventListener('click', (e) => { e.stopPropagation(); openModelMenu(); });

  const menu = $('#model-menu');
  if (menu) {
    menu.addEventListener('click', (e) => {
      const item = e.target.closest('[data-entry]');
      if (item) onPickModel(item.dataset.entry);
    });
  }

  // 底部固定入口：进「设置 → 模型配置 · 个人」自配或改默认
  const cfgBtn = $('#mm-configure');
  if (cfgBtn) cfgBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    closeModelMenu();
    switchConfigTab('personal');
    loadConfig();
    openModal('modal-config');
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#model-menu') && !e.target.closest('#btn-model')) closeModelMenu();
  });

  const ok = $('#btn-confirm-switch-model');
  if (ok) ok.addEventListener('click', confirmSwitchModel);
}

