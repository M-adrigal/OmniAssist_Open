/* 09-config.js
 * 配置中心：配置 tab、模型列表、供应商、模型池、搜索配置、温度策略
 *
 * 由 static/app.js 拆分而来（原第 3015-3612 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
let _configTab = 'personal';

function switchConfigTab(tab) {
  _configTab = tab;
  document.querySelectorAll('.config-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  $('#config-tab-personal').classList.toggle('hidden', tab !== 'personal');
  $('#config-tab-global').classList.toggle('hidden', tab !== 'global');
  $('#config-tab-search').classList.toggle('hidden', tab !== 'search');
  // 全局页每个区块都是即时生效的，底部按钮只管「偏好」与「搜索」两页
  const saveBtn = $('#btn-save-config');
  if (saveBtn) {
    saveBtn.classList.toggle('hidden', tab === 'global');
    saveBtn.textContent = tab === 'search' ? '保存配置' : '保存偏好设置';
  }
}

// ===== 两层模型结构：供应商（凭据）+ 模型（条目）=====
// 归属规则：平台级（owner=null）= 全体用户默认可用，只有管理员能管；
// 个人级（owner=自己）= 私有，各人只能看到和修改自己的，不出现在全局配置里。
// 个人凭据有两条来路：① 从平台已登记供应商复制地址 + 自己的 Key；
// ② 完全自建（名称 + 地址 + Key 都自己填），成本与风险自担。

let _selectableModels = [];
let _platformProviders = [];
let _myProviders = [];
let _myModels = [];
let _builtinCatalog = [];
// 供应商列表里「未配置 Key」那组是否展开（默认收起，避免一屏塞满占位项）
let _showIdleProviders = false;
// 正在编辑的对象 id（编辑表单与列表共用一套事件委托，用这两个变量区分目标）
let _editingProviderId = null;
let _editingEntryId = null;
let _editingMyModelId = null;
let _editingMyProviderId = null;

function _ownerLabel(owner) {
  return owner === 'user' ? '自配 Key' : '平台 Key';
}

function _resolvedFromLabel(from) {
  const map = {
    request: '会话内切换',
    session: '本会话',
    user_default: '自设默认',
    platform_default: '默认',
    legacy: '旧版配置',
  };
  return map[from] || '';
}

function renderCurrentModelBox(resolved) {
  const nameEl = $('#cfg-current-name');
  const metaEl = $('#cfg-current-meta');
  if (!nameEl || !metaEl) return;
  if (!resolved || !resolved.display_name) {
    nameEl.textContent = '尚未配置模型';
    metaEl.textContent = '';
    return;
  }
  nameEl.textContent = resolved.display_name;
  const parts = [];
  if (resolved.provider_name) parts.push(resolved.provider_name);
  if (resolved.owner) parts.push(_ownerLabel(resolved.owner));
  if (resolved.context_limit) parts.push('上下文 ' + resolved.context_limit);
  const from = _resolvedFromLabel(resolved.resolved_from);
  if (from) parts.push(from);
  metaEl.textContent = parts.join(' · ');
}

async function loadSelectableModels() {
  try {
    const data = await API.get('/api/config/models');
    _selectableModels = data.models || [];
    renderCurrentModelBox(data.resolved || {});
    const sel = $('#cfg-default-model');
    if (!sel) return;
    if (_selectableModels.length === 0) {
      sel.innerHTML = '<option value="">暂无可用模型</option>';
      return;
    }
    const cur = data.current_entry_id;
    sel.innerHTML = _selectableModels.map(m => {
      const tail = m.owner === 'user' ? '（自配）' : '';
      const label = `${m.display_name} · ${m.provider_name}${tail}`;
      return `<option value="${m.entry_id}"${m.entry_id === cur ? ' selected' : ''}>${escapeHtml(label)}</option>`;
    }).join('');
  } catch (e) {
    console.warn('加载可选模型失败', e);
  }
}

async function loadMyModels() {
  const listEl = $('#my-model-list');
  const providerSel = $('#my-provider');
  try {
    const data = await API.get('/api/config/my-models');
    const models = data.models || [];
    _myModels = models;
    _myProviders = data.providers || [];

    const countEl = $('#my-model-count');
    if (countEl) {
      countEl.textContent = String(models.length);
      countEl.classList.toggle('hidden', models.length === 0);
    }

    if (listEl) {
      if (models.length === 0) {
        listEl.innerHTML = '<p class="cfg-empty">暂无自配模型</p>';
      } else {
        listEl.innerHTML = models.map(m => `
          <div class="cfg-item">
            <div class="cfg-item-main">
              <div class="cfg-item-title">${escapeHtml(m.display_name)}</div>
              <div class="cfg-item-sub">${escapeHtml(m.model_id)} · ${escapeHtml(m.provider_name)}${m.context_limit ? ' · 上下文 ' + escapeHtml(m.context_limit) : ''}</div>
            </div>
            <div class="cfg-item-ops">
              <button class="cfg-op" data-action="test-model" data-entry="${m.entry_id}">测试</button>
              <button class="cfg-op" data-action="edit-my-model" data-entry="${m.entry_id}">编辑</button>
              <button class="cfg-op danger" data-action="del-my-model" data-entry="${m.entry_id}" data-name="${escapeHtml(m.display_name)}">删除</button>
            </div>
          </div>
        `).join('');
      }
    }
    renderMyProviders();
  } catch (e) {
    if (listEl) listEl.innerHTML = `<p class="cfg-empty">加载失败：${escapeHtml(e.message)}</p>`;
  }

  // 可选供应商 = 管理员已开通的（地址由平台控制）；最后一项是「自己填一家」
  try {
    const avail = await API.get('/api/config/available-providers');
    if (providerSel) {
      const usable = (avail || []).filter(p => p.builtin_key);
      providerSel.innerHTML = usable.length
        ? usable.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('')
          + '<option value="__custom">＋ 自定义供应商…</option>'
        : '<option value="">暂无可选供应商</option>'
          + '<option value="__custom">＋ 自定义供应商…</option>';
    }
  } catch (e) {
    if (providerSel) providerSel.innerHTML = '<option value="">加载失败</option>';
  }
}

// 个人供应商列表：从平台复制的 + 自己建的，都能改名称/地址/Key，也能整家删除
function renderMyProviders() {
  const listEl = $('#my-provider-list');
  const countEl = $('#my-provider-count');
  if (!listEl) return;
  if (countEl) {
    countEl.textContent = String(_myProviders.length);
    countEl.classList.toggle('hidden', _myProviders.length === 0);
  }
  if (_myProviders.length === 0) {
    listEl.innerHTML = '<p class="cfg-empty">暂无自定义供应商</p>';
    return;
  }
  listEl.innerHTML = _myProviders.map(p => {
    const keyState = p.has_key
      ? `<span class="cfg-tag ok">${escapeHtml(p.api_key_masked)}</span>`
      : '<span class="cfg-tag warn">未配置 Key</span>';
    return `
      <div class="cfg-item">
        <div class="cfg-item-main">
          <div class="cfg-item-title">${escapeHtml(p.name)} ${keyState}</div>
          <div class="cfg-item-sub">${escapeHtml(p.base_url)}</div>
        </div>
        <div class="cfg-item-ops">
          <button class="cfg-op" data-action="edit-my-provider" data-id="${p.id}" data-name="${escapeHtml(p.name)}">编辑</button>
          <button class="cfg-op danger" data-action="del-my-provider" data-id="${p.id}" data-name="${escapeHtml(p.name)}">删除</button>
        </div>
      </div>`;
  }).join('');
}

async function loadProviders() {
  const listEl = $('#provider-list');
  const entrySel = $('#new-entry-provider');
  try {
    // 全局配置只管理平台级供应商；用户自配是个人数据，各人在个人页管理
    const providers = await API.get('/api/config/providers');
    _platformProviders = providers || [];
    if (listEl) {
      if (_platformProviders.length === 0) {
        listEl.innerHTML = '<p class="cfg-empty">暂无供应商</p>';
      } else {
        const itemHtml = (p) => {
          const keyState = p.has_key
            ? `<span class="cfg-tag ok">${escapeHtml(p.api_key_masked)}</span>`
            : '<span class="cfg-tag warn">未配置 Key</span>';
          const disabledTag = p.enabled ? '' : '<span class="cfg-tag warn">已停用</span>';
          const ops = `
            <button class="cfg-op" data-action="edit-provider" data-id="${p.id}" data-name="${escapeHtml(p.name)}" data-builtin="${p.is_builtin ? '1' : '0'}" data-url="${escapeHtml(p.base_url)}">编辑</button>
            <button class="cfg-op danger" data-action="del-provider" data-id="${p.id}" data-name="${escapeHtml(p.name)}">删除</button>`;
          return `
            <div class="cfg-item">
              <div class="cfg-item-main">
                <div class="cfg-item-title">${escapeHtml(p.name)} ${keyState} ${disabledTag}</div>
                <div class="cfg-item-sub">${escapeHtml(p.base_url)}</div>
              </div>
              <div class="cfg-item-ops">${ops}</div>
            </div>`;
        };

        // 分两组：配了 Key 的才是「真的可用」，没配的只是目录占位。
        // 后者默认收起——8 家平铺是这块看起来拥挤的主因。
        const ready = _platformProviders.filter(p => p.has_key);
        const idle = _platformProviders.filter(p => !p.has_key);
        let html = '';
        if (ready.length) {
          html += `<div class="cfg-group-title">已开通 · ${ready.length}</div>`
            + ready.map(itemHtml).join('');
        }
        if (idle.length) {
          html += `<div class="cfg-group-title">未配置 Key · ${idle.length}`
            + `<button class="cfg-op" data-action="toggle-idle-providers">`
            + `${_showIdleProviders ? '收起' : '展开'}</button></div>`;
          if (_showIdleProviders) html += idle.map(itemHtml).join('');
        }
        listEl.innerHTML = html;
      }
    }
    if (entrySel) {
      entrySel.innerHTML = _platformProviders.length
        ? _platformProviders.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('')
        : '<option value="">请先添加供应商</option>';
    }
  } catch (e) {
    if (listEl) listEl.innerHTML = `<p class="cfg-empty">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

async function loadPool() {
  const listEl = $('#pool-list');
  try {
    const entries = await API.get('/api/config/pool');
    if (!listEl) return;
    if (!entries || entries.length === 0) {
      listEl.innerHTML = '<p class="cfg-empty">模型池为空</p>';
      return;
    }
    listEl.innerHTML = entries.map(e => {
      const tags = [];
      tags.push(e.visible ? '<span class="cfg-tag ok">用户可选</span>' : '<span class="cfg-tag">仅管理员</span>');
      if (e.is_default) tags.push('<span class="cfg-tag accent">默认</span>');
      if (!e.enabled) tags.push('<span class="cfg-tag warn">已停用</span>');
      const ctx = e.context_limit || '未设置';
      return `
        <div class="cfg-item">
          <div class="cfg-item-main">
            <div class="cfg-item-title">${escapeHtml(e.display_name)} ${tags.join(' ')}</div>
            <div class="cfg-item-sub">${escapeHtml(e.model_id)} · ${escapeHtml(e.provider_name)} · 上下文 ${escapeHtml(ctx)}</div>
          </div>
          <div class="cfg-item-ops">
            <button class="cfg-op" data-action="test-model" data-entry="${e.entry_id}">测试</button>
            <button class="cfg-op" data-action="edit-entry" data-entry="${e.entry_id}"
              data-model="${escapeHtml(e.model_id)}" data-name="${escapeHtml(e.display_name)}"
              data-ctx="${escapeHtml(e.context_limit || '')}" data-visible="${e.visible ? '1' : '0'}">编辑</button>
            <button class="cfg-op" data-action="toggle-pool-visible" data-entry="${e.entry_id}" data-visible="${e.visible ? '1' : '0'}">${e.visible ? '隐藏' : '公开'}</button>
            <button class="cfg-op" data-action="set-pool-default" data-entry="${e.entry_id}">设为默认</button>
            <button class="cfg-op danger" data-action="del-pool-entry" data-entry="${e.entry_id}" data-name="${escapeHtml(e.display_name)}">删除</button>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    if (listEl) listEl.innerHTML = `<p class="cfg-empty">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

async function loadBuiltinCatalog() {
  try {
    _builtinCatalog = await API.get('/api/config/builtin-providers') || [];
    const sel = $('#new-provider-builtin');
    if (sel) {
      sel.innerHTML = '<option value="">手动填写地址</option>' +
        _builtinCatalog.map(p => `<option value="${escapeHtml(p.key)}">${escapeHtml(p.name)}</option>`).join('');
    }
  } catch (e) {
    console.warn('加载内置供应商失败', e);
  }
}

async function loadSearchConfig() {
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
}

async function loadConfig() {
  try {
    const config = await API.get('/api/config');
    const maxIterEl = $('#cfg-max-iterations');
    if (maxIterEl) maxIterEl.value = config.max_iterations != null ? config.max_iterations : '';
    const tempModeEl = $('#cfg-temperature-mode');
    if (tempModeEl) tempModeEl.value = config.temperature_mode === 'static' ? 'static' : 'auto';
    const tempEl = $('#cfg-temperature');
    if (tempEl) tempEl.value = config.temperature != null ? config.temperature : '';
    syncTemperatureInputState();

    if (localStorage.getItem('thinkingMode') === null) {
      state.thinkingMode = config.thinking_mode || 'low';
    } else {
      state.thinkingMode = localStorage.getItem('thinkingMode') || 'low';
    }
    updateThinkingModeUI();

    const isAdmin = hasPermission('model_config_global', 'read');
    $('#config-tabs').classList.toggle('hidden', !isAdmin);

    await loadSelectableModels();
    await loadMyModels();
    if (isAdmin) {
      await Promise.all([loadProviders(), loadPool(), loadBuiltinCatalog(), loadSearchConfig()]);
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

  if (tab === 'global') {
    closeModal('modal-config');
    return;
  }

  // 个人页只保存偏好；模型与 Key 由各区块的操作按钮即时写入
  const body = {};
  const maxIterEl = $('#cfg-max-iterations');
  if (maxIterEl) {
    const v = parseInt(maxIterEl.value.trim(), 10);
    if (!isNaN(v) && v >= 1) body.max_iterations = v;
  }
  const tempModeEl = $('#cfg-temperature-mode');
  if (tempModeEl) {
    const m = tempModeEl.value.trim();
    if (m === 'static' || m === 'auto') body.temperature_mode = m;
  }
  const tempEl = $('#cfg-temperature');
  if (tempEl) {
    const tv = parseFloat(tempEl.value.trim());
    if (!isNaN(tv) && tv >= 0 && tv <= 2) body.temperature = tv;
  }

  try {
    await API.put('/api/config', body);
    showToast('偏好设置已保存', 'success');
    closeModal('modal-config');
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

async function setDefaultModelFromSelect() {
  const sel = $('#cfg-default-model');
  if (!sel || !sel.value) {
    showToast('请选择一个模型', 'error');
    return;
  }
  try {
    await API.put('/api/config/current-model', { entry_id: parseInt(sel.value, 10) });
    showToast('默认模型已更新', 'success');
    await loadSelectableModels();
    await refreshChatModelBadge();
  } catch (e) {
    showToast('设置失败: ' + e.message, 'error');
  }
}

async function addMyModel() {
  const providerId = $('#my-provider') ? $('#my-provider').value : '';
  const apiKey = $('#my-api-key') ? $('#my-api-key').value.trim() : '';
  const modelId = $('#my-model-id') ? $('#my-model-id').value.trim() : '';
  const displayName = $('#my-display-name') ? $('#my-display-name').value.trim() : '';
  const contextLimit = $('#my-context-limit') ? $('#my-context-limit').value.trim() : '';
  const custom = providerId === '__custom';
  const customName = $('#my-custom-name') ? $('#my-custom-name').value.trim() : '';
  const customUrl = $('#my-custom-url') ? $('#my-custom-url').value.trim() : '';

  if (!apiKey) { showToast('请填写 API Key', 'error'); return; }
  if (!modelId) { showToast('请填写模型 ID', 'error'); return; }
  if (custom) {
    if (!customUrl) { showToast('请填写供应商地址', 'error'); return; }
  } else if (!providerId) {
    showToast('请选择供应商', 'error'); return;
  }

  const btn = $('#btn-add-my-model');
  if (btn) btn.disabled = true;
  try {
    const body = {
      api_key: apiKey,
      model_id: modelId,
      display_name: displayName || undefined,
      context_limit: contextLimit || undefined,
    };
    if (custom) {
      body.provider_name = customName || undefined;
      body.base_url = customUrl;
    } else {
      body.platform_provider_id = parseInt(providerId, 10);
    }
    await API.post('/api/config/my-models', body);
    showToast('模型已添加', 'success');
    ['#my-api-key', '#my-model-id', '#my-display-name', '#my-context-limit',
     '#my-custom-name', '#my-custom-url'].forEach(id => {
      const el = $(id); if (el) el.value = '';
    });
    collapseConfigForm('#btn-toggle-my-model', '#my-model-form', '添加自配模型');
    await loadMyModels();
    await loadSelectableModels();
    await refreshChatModelBadge();
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function addProvider() {
  const builtinKey = $('#new-provider-builtin') ? $('#new-provider-builtin').value : '';
  const url = $('#new-provider-url') ? $('#new-provider-url').value.trim() : '';
  const name = $('#new-provider-name') ? $('#new-provider-name').value.trim() : '';
  const key = $('#new-provider-key') ? $('#new-provider-key').value.trim() : '';

  if (!builtinKey && !url) { showToast('请选择内置供应商或填写地址', 'error'); return; }

  try {
    await API.post('/api/config/providers', {
      builtin_key: builtinKey || undefined,
      base_url: builtinKey ? (url || '') : url,
      name: name || undefined,
      api_key: key || undefined,
    });
    showToast('供应商已保存', 'success');
    ['#new-provider-url', '#new-provider-name', '#new-provider-key'].forEach(id => {
      const el = $(id); if (el) el.value = '';
    });
    collapseConfigForm('#btn-toggle-provider', '#provider-form', '添加供应商');
    await loadProviders();
    await loadPool();
    await loadMyModels();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

async function addPoolEntry() {
  const providerId = $('#new-entry-provider') ? $('#new-entry-provider').value : '';
  const modelId = $('#new-entry-model-id') ? $('#new-entry-model-id').value.trim() : '';
  const displayName = $('#new-entry-display-name') ? $('#new-entry-display-name').value.trim() : '';
  const contextLimit = $('#new-entry-context') ? $('#new-entry-context').value.trim() : '';
  const visible = $('#new-entry-visible') ? $('#new-entry-visible').checked : true;

  if (!providerId) { showToast('请先添加供应商', 'error'); return; }
  if (!modelId) { showToast('请填写模型 ID', 'error'); return; }

  try {
    await API.post('/api/config/pool', {
      provider_id: parseInt(providerId, 10),
      model_id: modelId,
      display_name: displayName || undefined,
      context_limit: contextLimit || undefined,
      visible: visible,
    });
    showToast('模型已加入模型池', 'success');
    ['#new-entry-model-id', '#new-entry-display-name', '#new-entry-context'].forEach(id => {
      const el = $(id); if (el) el.value = '';
    });
    collapseConfigForm('#btn-toggle-entry', '#entry-form', '添加模型');
    await loadPool();
    await loadSelectableModels();
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  }
}

async function testModelEntry(entryId, btn) {
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '测试中'; }
  try {
    const res = await API.post('/api/config/test', { entry_id: parseInt(entryId, 10) });
    if (res.ok) {
      const extra = res.real_model && res.real_model !== '' ? `（实际响应：${res.real_model}）` : '';
      showToast(`连通正常，${res.latency_ms}ms ${extra}`, 'success');
    } else {
      showToast(`连接失败：${res.hint || res.error}`, 'error');
    }
  } catch (e) {
    showToast('测试失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

// ===== 编辑表单（平台供应商 / 模型池 / 自配模型 / 个人供应商）=====
// 四个编辑表单都遵循同一约定：打开时预填、保存时只提交非空字段、
// 「取消」直接收起。列表事件委托负责打开，保存按钮各自绑定。

function _fill(sel, value) { const el = $(sel); if (el) el.value = value == null ? '' : value; }
function _showForm(formSel) {
  const form = $(formSel);
  if (form) form.classList.remove('hidden');
}
function _hideForm(formSel) {
  const form = $(formSel);
  if (form) form.classList.add('hidden');
}

function openProviderEdit(id, name, baseUrl, isBuiltin) {
  _editingProviderId = id;
  _fill('#edit-provider-name', name);
  _fill('#edit-provider-url', baseUrl);
  _fill('#edit-provider-key', '');
  $('#edit-provider-enabled').checked = true;
  // 内置供应商的地址是官方规范值，不允许在界面上改
  $('#edit-provider-url').disabled = isBuiltin;
  $('#edit-provider-url').title = isBuiltin ? '内置供应商地址不可修改' : '';
  _hideForm('#entry-edit-form');
  _showForm('#provider-edit-form');
}

async function saveProviderEdit() {
  if (!_editingProviderId) return;
  const body = {
    name: ($('#edit-provider-name').value || '').trim() || undefined,
    api_key: ($('#edit-provider-key').value || '').trim() || undefined,
    enabled: $('#edit-provider-enabled').checked,
  };
  if (!$('#edit-provider-url').disabled) {
    body.base_url = ($('#edit-provider-url').value || '').trim() || undefined;
  }
  try {
    await API.put(`/api/config/providers/${_editingProviderId}`, body);
    showToast('供应商已更新', 'success');
    _hideForm('#provider-edit-form');
    _editingProviderId = null;
    await loadProviders();
    await loadPool();
    await loadSelectableModels();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

function openEntryEdit(btn) {
  _editingEntryId = parseInt(btn.dataset.entry, 10);
  _fill('#edit-entry-model-id', btn.dataset.model);
  _fill('#edit-entry-display-name', btn.dataset.name);
  _fill('#edit-entry-context', btn.dataset.ctx);
  $('#edit-entry-visible').checked = btn.dataset.visible === '1';
  _hideForm('#provider-edit-form');
  _showForm('#entry-edit-form');
}

async function saveEntryEdit() {
  if (!_editingEntryId) return;
  const body = {
    model_id: ($('#edit-entry-model-id').value || '').trim() || undefined,
    display_name: ($('#edit-entry-display-name').value || '').trim() || undefined,
    context_limit: ($('#edit-entry-context').value || '').trim() || undefined,
    visible: $('#edit-entry-visible').checked,
  };
  if (!body.model_id) { showToast('模型 ID 不能为空', 'error'); return; }
  try {
    await API.put(`/api/config/pool/${_editingEntryId}`, body);
    showToast('模型已更新', 'success');
    _hideForm('#entry-edit-form');
    _editingEntryId = null;
    await loadPool();
    await loadSelectableModels();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

function openMyModelEdit(entryId) {
  const m = _myModels.find(x => String(x.entry_id) === String(entryId));
  if (!m) return;
  _editingMyModelId = entryId;
  _fill('#edit-my-model-id', m.model_id);
  _fill('#edit-my-display-name', m.display_name);
  _fill('#edit-my-context-limit', m.context_limit);
  _fill('#edit-my-api-key', '');
  _hideForm('#my-provider-edit-form');
  _showForm('#my-model-edit-form');
}

async function saveMyModelEdit() {
  if (!_editingMyModelId) return;
  const body = {
    model_id: ($('#edit-my-model-id').value || '').trim() || undefined,
    display_name: ($('#edit-my-display-name').value || '').trim() || undefined,
    context_limit: ($('#edit-my-context-limit').value || '').trim() || undefined,
    api_key: ($('#edit-my-api-key').value || '').trim() || undefined,
  };
  if (!body.model_id) { showToast('模型 ID 不能为空', 'error'); return; }
  try {
    await API.put(`/api/config/my-models/${_editingMyModelId}`, body);
    showToast('模型已更新', 'success');
    _hideForm('#my-model-edit-form');
    _editingMyModelId = null;
    await loadMyModels();
    await loadSelectableModels();
    await refreshChatModelBadge();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

function openMyProviderEdit(id, name) {
  const p = _myProviders.find(x => String(x.id) === String(id));
  if (!p) return;
  _editingMyProviderId = id;
  _fill('#edit-my-provider-name', p.name);
  _fill('#edit-my-provider-url', p.base_url);
  _fill('#edit-my-provider-key', '');
  _hideForm('#my-model-edit-form');
  _showForm('#my-provider-edit-form');
}

async function saveMyProviderEdit() {
  if (!_editingMyProviderId) return;
  const body = {
    name: ($('#edit-my-provider-name').value || '').trim() || undefined,
    base_url: ($('#edit-my-provider-url').value || '').trim() || undefined,
    api_key: ($('#edit-my-provider-key').value || '').trim() || undefined,
  };
  try {
    await API.put(`/api/config/my-providers/${_editingMyProviderId}`, body);
    showToast('供应商已更新', 'success');
    _hideForm('#my-provider-edit-form');
    _editingMyProviderId = null;
    await loadMyModels();
    await loadSelectableModels();
    await refreshChatModelBadge();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

async function deleteMyProvider(id, name) {
  if (!confirm(`删除供应商「${name}」？其下的自配模型会一并删除，使用中的会话将回落到默认模型。`)) return;
  try {
    await API.del(`/api/config/my-providers/${id}`);
    showToast('已删除', 'success');
    await loadMyModels();
    await loadSelectableModels();
    await refreshChatModelBadge();
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

// 温度策略为「自动」时温度值不参与计算 —— 直接置灰，避免用户填了却不生效还以为在调
function syncTemperatureInputState() {
  const mode = $('#cfg-temperature-mode');
  const temp = $('#cfg-temperature');
  if (!mode || !temp) return;
  const isAuto = mode.value !== 'static';
  temp.disabled = isAuto;
  temp.title = isAuto
    ? '自动模式下按任务类型动态调节，此值不生效'
    : '固定模式下使用此值（0–2）';
}

// 「＋ 添加…」默认收起表单：三块表单常驻会把弹窗撑到两屏，先给列表让位
function bindConfigFormToggle(btnSel, formSel, label) {
  const btn = $(btnSel);
  const form = $(formSel);
  if (!btn || !form) return;
  btn.addEventListener('click', () => {
    const opening = form.classList.contains('hidden');
    form.classList.toggle('hidden', !opening);
    btn.classList.toggle('open', !opening);
    btn.textContent = opening ? '收起' : '＋ ' + label;
    if (opening) {
      const first = form.querySelector('input:not([type="checkbox"]), select');
      if (first) first.focus();
    }
  });
}

function collapseConfigForm(btnSel, formSel, label) {
  const btn = $(btnSel);
  const form = $(formSel);
  if (form && !form.classList.contains('hidden')) form.classList.add('hidden');
  if (btn) {
    btn.classList.remove('open');
    btn.textContent = '＋ ' + label;
  }
}

function initModelConfigEvents() {
  const on = (sel, ev, fn) => { const el = $(sel); if (el) el.addEventListener(ev, fn); };

  bindConfigFormToggle('#btn-toggle-my-model', '#my-model-form', '添加自配模型');
  bindConfigFormToggle('#btn-toggle-provider', '#provider-form', '添加供应商');
  bindConfigFormToggle('#btn-toggle-entry', '#entry-form', '添加模型');
  on('#cfg-temperature-mode', 'change', syncTemperatureInputState);

  on('#btn-set-default-model', 'click', setDefaultModelFromSelect);
  on('#btn-add-my-model', 'click', addMyModel);
  on('#btn-add-provider', 'click', addProvider);
  on('#btn-add-entry', 'click', addPoolEntry);

  // 四个编辑表单的保存 / 取消
  on('#btn-save-provider', 'click', saveProviderEdit);
  on('#btn-cancel-provider', 'click', () => { _hideForm('#provider-edit-form'); _editingProviderId = null; });
  on('#btn-save-entry', 'click', saveEntryEdit);
  on('#btn-cancel-entry', 'click', () => { _hideForm('#entry-edit-form'); _editingEntryId = null; });
  on('#btn-save-my-model', 'click', saveMyModelEdit);
  on('#btn-cancel-my-model', 'click', () => { _hideForm('#my-model-edit-form'); _editingMyModelId = null; });
  on('#btn-save-my-provider', 'click', saveMyProviderEdit);
  on('#btn-cancel-my-provider', 'click', () => { _hideForm('#my-provider-edit-form'); _editingMyProviderId = null; });

  // 选了内置供应商就自动带上名称，地址由后端按目录填
  on('#new-provider-builtin', 'change', (e) => {
    const spec = _builtinCatalog.find(p => p.key === e.target.value);
    const nameEl = $('#new-provider-name');
    const urlEl = $('#new-provider-url');
    if (spec) {
      if (nameEl) nameEl.value = spec.name;
      if (urlEl) { urlEl.value = spec.base_url; urlEl.disabled = true; }
    } else if (urlEl) {
      urlEl.disabled = false;
    }
  });

  // 「自定义供应商」选中时才显示名称/地址输入，否则只是平台列表的复制
  on('#my-provider', 'change', (e) => {
    const custom = e.target.value === '__custom';
    const box = $('#my-custom-provider');
    if (box) box.classList.toggle('hidden', !custom);
    if (custom) {
      const first = box.querySelector('input');
      if (first) first.focus();
    }
  });

  // 列表内的操作统一走事件委托（列表会重渲染，逐个绑定容易漏）
  const delegate = (sel) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const action = btn.dataset.action;
      const entry = btn.dataset.entry;
      const id = btn.dataset.id;
      try {
        if (action === 'test-model') {
          await testModelEntry(entry, btn);
        } else if (action === 'del-my-model') {
          if (!confirm(`删除模型「${btn.dataset.name}」？使用它的会话会回落到默认模型。`)) return;
          await API.del(`/api/config/my-models/${entry}`);
          showToast('已删除', 'success');
          await loadMyModels();
          await loadSelectableModels();
          await refreshChatModelBadge();
        } else if (action === 'edit-my-model') {
          openMyModelEdit(entry);
        } else if (action === 'edit-my-provider') {
          openMyProviderEdit(id, btn.dataset.name);
        } else if (action === 'del-my-provider') {
          await deleteMyProvider(id, btn.dataset.name);
        } else if (action === 'toggle-pool-visible') {
          const visible = btn.dataset.visible === '1';
          await API.put(`/api/config/pool/${entry}`, { visible: !visible });
          await loadPool();
          await loadSelectableModels();
        } else if (action === 'set-pool-default') {
          await API.put(`/api/config/pool/${entry}`, { is_default: true });
          showToast('已设为平台默认模型', 'success');
          await loadPool();
          await loadSelectableModels();
        } else if (action === 'edit-entry') {
          openEntryEdit(btn);
        } else if (action === 'del-pool-entry') {
          if (!confirm(`从模型池删除「${btn.dataset.name}」？`)) return;
          await API.del(`/api/config/pool/${entry}`);
          await loadPool();
          await loadSelectableModels();
        } else if (action === 'del-provider') {
          if (!confirm(`删除供应商「${btn.dataset.name}」？其下所有模型会一并删除，使用中的会话将回落到其他模型。`)) return;
          await API.del(`/api/config/providers/${id}`);
          await loadProviders();
          await loadPool();
          await loadSelectableModels();
        } else if (action === 'edit-provider') {
          openProviderEdit(id, btn.dataset.name, btn.dataset.url, btn.dataset.builtin === '1');
        } else if (action === 'toggle-idle-providers') {
          _showIdleProviders = !_showIdleProviders;
          await loadProviders();
        }
      } catch (err) {
        showToast('操作失败: ' + err.message, 'error');
      }
    });
  };
  delegate('#my-model-list');
  delegate('#pool-list');
  delegate('#provider-list');
  delegate('#my-provider-list');
}

// ===== 聊天页：本会话模型切换 =====
// 切换只影响当前会话，不改用户默认值 —— 用户试一下 A 不该顺手改掉他所有新对话的设置。

