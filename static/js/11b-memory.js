/* 11b-memory.js
 * 记忆：每位用户一份，按「段」组织（段定义来自接口 registry，加新段不用改这里）
 *
 * 四条规则写死在这里，改之前先读：
 * 1. 模型只提议：整理 / 提炼的结果都只是建议，落库必须由人点确认。
 * 2. 认不出来的内容不丢：导入没归到任何一段的行，原样显示出来。
 * 3. 有未保存改动时，关闭弹窗要拦一下 —— 静默丢失是最坏的结果。
 * 4. 破坏性操作（清空）不放一级按钮，收在「⋯」里，且必须二次确认。
 *
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 *    本文件所有顶层符号统一以 mem 前缀命名。
 */

// 兼容旧引用（探针与其它地方可能还在用）
const MEM_SECTIONS = [
  ['work', '工作背景'],
  ['personal', '个人背景'],
  ['focus', '当前关注'],
  ['recent', '近期动态'],
];

// 段定义（以接口返回的为准，这里是打开前的兜底）
let memRegistry = MEM_SECTIONS.map(([k, t]) => ({
  key: k, title: t, kind: (k === 'focus' || k === 'recent') ? 'list' : 'text',
  max_chars: 2000, max_items: (k === 'focus') ? 30 : 20, order: 0, inject: true, hint: '',
}));

let memLimits = { total_chars: 4000, recent_items: 20 };

// 已保存到服务端的那份（界面一切以它为准；编辑器里的是临时态）
let memData = {
  items: {}, sections: {}, enabled: 1, updated_at: null,
  text: '', versions: [], version_count: 0,
};

let memEditingKey = null;   // 正在编辑哪一段（一次只编辑一段）
let memProposals = [];      // 模型提炼出的提议（未入库）
let memPendingImport = null;

function memSpec(key) {
  return memRegistry.filter(s => s.key === key)[0] || { key: key, title: key, kind: 'text' };
}

// ===== 查看态渲染 =====

function memRenderView() {
  const view = $('#mem-view');
  if (!view) return;
  view.textContent = '';
  view.classList.toggle('is-off', !memData.enabled);

  let any = false;
  memRegistry.forEach(spec => {
    const entries = memData.items[spec.key] || [];
    if (entries.length) any = true;

    const sec = document.createElement('div');
    sec.className = 'mem-view-sec';

    const head = document.createElement('div');
    head.className = 'mem-view-head';
    const title = document.createElement('span');
    title.className = 'mem-view-title';
    title.textContent = spec.title;
    head.appendChild(title);

    const right = document.createElement('span');
    right.className = 'mem-view-actions';
    if (spec.kind === 'list') {
      const cap = spec.max_items || 0;
      const cnt = document.createElement('span');
      cnt.className = 'mem-count';
      cnt.textContent = cap ? `${entries.length} / ${cap} 条` : `${entries.length} 条`;
      right.appendChild(cnt);
    }
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'mem-link-btn';
    edit.textContent = '编辑';
    edit.setAttribute('data-edit', spec.key);
    right.appendChild(edit);
    head.appendChild(right);
    sec.appendChild(head);

    const body = document.createElement('div');
    body.className = 'mem-view-text';
    if (!entries.length) {
      body.textContent = '暂无内容';
      body.classList.add('is-empty');
    } else if (spec.kind === 'list') {
      entries.forEach(e => {
        const row = document.createElement('div');
        row.className = 'mem-item-row';
        const txt = document.createElement('span');
        txt.className = 'mem-item-text';
        txt.textContent = e.content;
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'mem-link-btn danger';
        del.textContent = '删除';
        del.setAttribute('data-del', String(e.id));
        row.appendChild(txt);
        row.appendChild(del);
        body.appendChild(row);
      });
    } else {
      body.textContent = entries.map(e => e.content).join('\n');
    }
    sec.appendChild(body);
    view.appendChild(sec);
  });

  if (!any) {
    const empty = document.createElement('div');
    empty.className = 'mem-view-empty';
    empty.textContent = '还没有内容。';
    const tip = document.createElement('div');
    tip.className = 'mem-hint';
    tip.textContent = '可以自己写，也可以让模型从最近的对话里提炼。';
    empty.appendChild(tip);
    view.appendChild(empty);
  }

  memRenderMeta();
}

function memRenderMeta() {
  const u = $('#mem-usage');
  if (u) {
    let total = 0;
    memRegistry.forEach(s => {
      (memData.items[s.key] || []).forEach(e => { total += (e.content || '').length; });
    });
    u.textContent = `共 ${total} 字 / 上限 ${memLimits.total_chars || 4000} 字`
      + (memData.updated_at ? ` · 更新于 ${memData.updated_at}` : '')
      + (memData.version_count ? ` · 已留档 ${memData.version_count} 版` : '');
    u.classList.toggle('over', total > (memLimits.total_chars || 4000));
  }
  const st = $('#mem-state-desc');
  if (st) {
    st.textContent = memData.enabled
      ? '每次对话都会带上这些背景；关掉后内容仍保留，不会删除'
      : '已关闭，对话中不会使用；内容保留着，重新打开即可';
  }
}

// ===== 编辑（一次一段）=====

function memEnterEdit(key) {
  const k = key || (memRegistry[0] || {}).key;
  if (!k) return;
  memEditingKey = k;
  const spec = memSpec(k);
  memRegistry.forEach(s => {
    const el = $('#mem-' + s.key);
    if (!el) return;
    const on = (s.key === k);
    el.classList.toggle('hidden', !on);
    if (on) {
      const entries = memData.items[s.key] || [];
      el.value = (s.kind === 'list')
        ? entries.map(e => `- ${e.content}`).join('\n')
        : entries.map(e => e.content).join('\n');
    }
  });
  const title = $('#mem-edit-title');
  if (title) title.textContent = spec.title;
  const hint = $('#mem-edit-hint');
  if (hint) {
    hint.textContent = (spec.kind === 'list')
      ? '一条一行，超出上限时会去掉最旧的'
      : (spec.hint || '');
  }
  const v = $('#mem-view');
  const e = $('#mem-edit');
  const f = $('#mem-footer');
  if (v) v.classList.add('hidden');
  if (e) e.classList.remove('hidden');
  if (f) f.classList.remove('hidden');
  memClosePanels(['#mem-proposals', '#mem-versions', '#mem-menu', '#mem-io-box', '#mem-import-preview']);
  memRefreshCount();
}

function memExitEdit() {
  memEditingKey = null;
  const e = $('#mem-edit');
  const v = $('#mem-view');
  const f = $('#mem-footer');
  if (e) e.classList.add('hidden');
  if (v) v.classList.remove('hidden');
  if (f) f.classList.add('hidden');
  memShowWarn('');
  memRenderView();
}

function memCancelEdit() {
  if (memIsDirty() && !window.confirm('这段有没保存的改动，确定放弃吗？')) return;
  memExitEdit();
}

function memEditorValue() {
  if (!memEditingKey) return '';
  const el = $('#mem-' + memEditingKey);
  return el ? (el.value || '') : '';
}

function memSavedText(key) {
  const spec = memSpec(key);
  const entries = memData.items[key] || [];
  return (spec.kind === 'list')
    ? entries.map(e => `- ${e.content}`).join('\n')
    : entries.map(e => e.content).join('\n');
}

function memIsDirty() {
  if (!memEditingKey) return false;
  return memEditorValue().trim() !== memSavedText(memEditingKey).trim();
}

function memOverLimit() {
  if (!memEditingKey) return false;
  const spec = memSpec(memEditingKey);
  const val = memEditorValue();
  if (spec.kind === 'list') {
    const cap = spec.max_items || 20;
    return val.split('\n').filter(s => s.trim()).length > cap;
  }
  return val.trim().length > (spec.max_chars || 2000);
}

function memRefreshCount() {
  if (!memEditingKey) return;
  const spec = memSpec(memEditingKey);
  const c = $('#mem-edit-count');
  const val = memEditorValue();
  const over = memOverLimit();
  if (c) {
    if (spec.kind === 'list') {
      const n = val.split('\n').filter(s => s.trim()).length;
      c.textContent = `${n} 条 / 上限 ${spec.max_items || 20} 条`;
    } else {
      c.textContent = `${val.trim().length} 字 / 上限 ${spec.max_chars || 2000} 字`;
    }
    c.classList.toggle('over', over);
  }
  const btn = $('#btn-mem-save');
  // 两种情况点不了：没改（省一次无意义请求）、超上限（服务端一定会拒，别让人白点）
  if (btn) btn.disabled = !memIsDirty() || over;
}

// ===== 读写 =====

function memApply(data) {
  if (!data) return;
  memData.items = data.items || {};
  memData.sections = data.sections || {};
  memData.enabled = data.enabled ? 1 : 0;
  memData.updated_at = data.updated_at || null;
  memData.text = data.text || '';
  memData.version_count = data.version_count || 0;
  memData.versions = data.versions || memData.versions || [];
  if (data.registry && data.registry.length) memRegistry = data.registry;
  if (data.limits) memLimits = data.limits;
  const en = $('#mem-enabled');
  if (en) en.checked = !!memData.enabled;
  memRenderView();
}

async function memLoad() {
  try {
    memApply(await API.get('/api/memory'));
  } catch (e) {
    showToast('记忆加载失败: ' + e.message, 'error');
  }
}

async function memSave() {
  if (!memEditingKey) return;
  const btn = $('#btn-mem-save');
  if (btn) { btn.disabled = true; btn.textContent = '保存中'; }
  const key = memEditingKey;
  const payload = {};
  memRegistry.forEach(s => {
    payload[s.key] = (s.key === key) ? memEditorValue() : memSavedText(s.key);
  });
  try {
    const saved = await API.put('/api/memory', { sections: payload });
    memApply(saved);
    (saved.warnings || []).forEach(w => showToast(w, 'warning'));
    showToast('已保存', 'success');
    memExitEdit();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '保存'; }
    memRefreshCount();
  }
}

async function memDeleteItem(id) {
  try {
    const saved = await API.del('/api/memory/item?entry_id=' + Number(id));
    memApply(saved);
    (saved.warnings || []).forEach(w => showToast(w, 'warning'));
    showToast('已删除，可在「历史版本」里恢复', 'success');
  } catch (e) {
    showToast('删除失败: ' + e.message, 'error');
  }
}

async function memToggleEnabled() {
  const en = $('#mem-enabled');
  if (!en) return;
  const want = en.checked;
  en.disabled = true;
  try {
    memApply(await API.post('/api/memory/enabled', { enabled: want }));
  } catch (e) {
    en.checked = !want;
    showToast('开关没生效: ' + e.message, 'error');
  } finally {
    en.disabled = false;
  }
}

async function memReset() {
  const ok = await showConfirmDialog(
    '清空记忆',
    '所有内容会被清空（开关状态不变）。清空前的版本会自动留档，可以在「历史版本」里恢复。',
    '清空'
  );
  if (!ok) return;
  try {
    const saved = await API.post('/api/memory/reset', {});
    memApply(saved);
    showToast('已清空，可在「历史版本」里恢复', 'success');
  } catch (e) {
    showToast('清空失败: ' + e.message, 'error');
  }
}

// ===== 进化：模型从对话里提炼 =====

async function memEvolve() {
  const btn = $('#btn-mem-evolve');
  if (btn) { btn.disabled = true; btn.textContent = '提炼中'; }
  try {
    const res = await API.post('/api/memory/evolve', {});
    memProposals = res.proposals || [];
    if (!memProposals.length) {
      showToast(res.duplicate_skipped
        ? '这次对话里没有新的可记内容（和已有的重复了）'
        : '这次对话里没提炼出值得长期记住的内容', 'warning');
      return;
    }
    memRenderProposals(res);
    const p = $('#mem-proposals');
    const v = $('#mem-view');
    if (p) p.classList.remove('hidden');
    if (v) v.classList.add('hidden');
  } catch (e) {
    showToast('提炼失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '从对话提炼'; }
  }
}

function memRenderProposals(res) {
  const list = $('#mem-proposals-list');
  if (!list) return;
  list.textContent = '';
  const src = $('#mem-proposals-src');
  if (src) {
    src.textContent = res.session_title
      ? `来自「${res.session_title}」最近 ${res.message_count} 条对话`
      : `最近 ${res.message_count} 条对话`;
  }
  memProposals.forEach((p, i) => {
    const row = document.createElement('label');
    row.className = 'mem-prop';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.setAttribute('data-prop', String(i));
    const txt = document.createElement('span');
    txt.className = 'mem-prop-text';
    const tag = document.createElement('span');
    tag.className = 'mem-prop-tag';
    tag.textContent = p.section_title || memSpec(p.section_key).title;
    const body = document.createElement('span');
    body.textContent = p.content;
    txt.appendChild(tag);
    txt.appendChild(body);
    row.appendChild(cb);
    row.appendChild(txt);
    list.appendChild(row);
  });
}

async function memApplyProposals() {
  const boxes = document.querySelectorAll('#mem-proposals-list input[data-prop]');
  const picked = [];
  boxes.forEach(b => {
    if (b.checked) picked.push(memProposals[Number(b.getAttribute('data-prop'))]);
  });
  if (!picked.length) {
    showToast('先勾选要加入的条目', 'warning');
    return;
  }
  const btn = $('#btn-mem-apply');
  if (btn) { btn.disabled = true; btn.textContent = '加入中'; }
  let ok = 0;
  try {
    for (const p of picked) {
      await API.post('/api/memory/item', { section_key: p.section_key, content: p.content });
      ok += 1;
    }
    memApply(await API.get('/api/memory'));
    showToast(`已加入 ${ok} 条`, 'success');
    memCloseProposals();
  } catch (e) {
    showToast(ok ? `已加入 ${ok} 条，之后失败：${e.message}` : `加入失败：${e.message}`, 'error');
    try { memApply(await API.get('/api/memory')); } catch (e2) { /* 读不回来就保持现状 */ }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '加入记忆'; }
  }
}

function memCloseProposals() {
  memProposals = [];
  const p = $('#mem-proposals');
  const v = $('#mem-view');
  if (p) p.classList.add('hidden');
  if (v) v.classList.remove('hidden');
}

// ===== 历史版本 =====

function memShowVersions() {
  const box = $('#mem-versions');
  const list = $('#mem-versions-list');
  if (!box || !list) return;
  list.textContent = '';
  const vs = memData.versions || [];
  if (!vs.length) {
    const p = document.createElement('div');
    p.className = 'mem-hint';
    p.textContent = '还没有留档。每次改动前都会自动存一份。';
    list.appendChild(p);
  }
  vs.forEach(v => {
    const row = document.createElement('div');
    row.className = 'mem-ver';
    const left = document.createElement('div');
    const t = document.createElement('div');
    t.textContent = v.created_at || '';
    const d = document.createElement('div');
    d.className = 'mem-hint';
    d.textContent = `${memReasonText(v.reason)} · ${v.chars || 0} 字`;
    left.appendChild(t);
    left.appendChild(d);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mem-link-btn';
    btn.textContent = '恢复';
    btn.setAttribute('data-restore', String(v.id));
    row.appendChild(left);
    row.appendChild(btn);
    list.appendChild(row);
  });
  box.classList.remove('hidden');
  const v = $('#mem-view');
  if (v) v.classList.add('hidden');
}

function memReasonText(reason) {
  return ({
    manual: '手动保存前', extract: '手动保存前', import: '导入前',
    reset: '清空前', restore: '恢复前', add: '新增前', delete: '删除前',
  })[reason] || '改动前';
}

async function memRestore(id) {
  const ok = await showConfirmDialog(
    '恢复这个版本', '当前内容会被这一版替换。恢复前的样子也会留档，可以再退回来。', '恢复'
  );
  if (!ok) return;
  try {
    memApply(await API.post('/api/memory/versions/' + id + '/restore', {}));
    showToast('已恢复', 'success');
    const box = $('#mem-versions');
    const v = $('#mem-view');
    if (box) box.classList.add('hidden');
    if (v) v.classList.remove('hidden');
  } catch (e) {
    showToast('恢复失败: ' + e.message, 'error');
  }
}

// ===== 导入 / 导出 =====

function memShowExport() {
  const box = $('#mem-io-box');
  const ta = $('#mem-io-text');
  if (!box || !ta) return;
  ta.value = memData.text || '';
  ta.readOnly = true;
  box.classList.remove('hidden');
  const copy = $('#btn-mem-copy');
  const parse = $('#btn-mem-parse');
  if (copy) copy.classList.remove('hidden');
  if (parse) parse.classList.add('hidden');
  memClosePanels(['#mem-menu', '#mem-import-preview']);
}

function memShowImport() {
  const box = $('#mem-io-box');
  const ta = $('#mem-io-text');
  if (!box || !ta) return;
  ta.value = '';
  ta.readOnly = false;
  box.classList.remove('hidden');
  const copy = $('#btn-mem-copy');
  const parse = $('#btn-mem-parse');
  if (copy) copy.classList.add('hidden');
  if (parse) parse.classList.remove('hidden');
  const pv = $('#mem-import-preview');
  if (pv) pv.classList.add('hidden');
  memClosePanels(['#mem-menu']);
  ta.focus();
}

async function memParseImport() {
  const ta = $('#mem-io-text');
  if (!ta) return;
  const btn = $('#btn-mem-parse');
  if (btn) { btn.disabled = true; btn.textContent = '解析中'; }
  try {
    memPendingImport = await API.post('/api/memory/parse', { text: ta.value || '' });
    memRenderImportPreview(memPendingImport);
  } catch (e) {
    showToast('解析失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '解析并填入'; }
  }
}

function memRenderImportPreview(res) {
  const pv = $('#mem-import-preview');
  if (!pv) return;
  pv.textContent = '';

  const head = document.createElement('div');
  head.className = 'mem-subhead';
  const hs = document.createElement('span');
  hs.textContent = '导入会整份替换，先看看哪些段会变';
  head.appendChild(hs);
  pv.appendChild(head);

  memRegistry.forEach(s => {
    const oldText = memSavedText(s.key).trim();
    const newText = (res[s.key] || '').trim();
    const row = document.createElement('div');
    row.className = 'mem-diff';
    const name = document.createElement('span');
    name.className = 'mem-diff-name';
    name.textContent = s.title;
    const state = document.createElement('span');
    state.className = 'mem-diff-state';
    if (oldText === newText) {
      state.textContent = '不变';
    } else if (!newText && oldText) {
      state.textContent = `将清空（原 ${oldText.length} 字）`;
      state.classList.add('danger');
    } else if (!oldText) {
      state.textContent = `新增 ${newText.length} 字`;
      state.classList.add('ok');
    } else {
      state.textContent = `覆盖（${oldText.length} 字 → ${newText.length} 字）`;
      state.classList.add('warn');
    }
    row.appendChild(name);
    row.appendChild(state);
    pv.appendChild(row);
  });

  if (res.unmatched) {
    const w = document.createElement('div');
    w.className = 'mem-hint';
    w.textContent = '有 ' + res.unmatched.split('\n').length + ' 行没认出属于哪一段，导入后不会保留';
    pv.appendChild(w);
  }

  const actions = document.createElement('div');
  actions.className = 'mem-tool-actions';
  const ok = document.createElement('button');
  ok.type = 'button';
  ok.className = 'btn-primary btn-sm';
  ok.id = 'btn-mem-import-confirm';
  ok.textContent = '确认导入';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'btn-sm';
  cancel.id = 'btn-mem-import-cancel';
  cancel.textContent = '取消';
  ok.addEventListener('click', memConfirmImport);
  cancel.addEventListener('click', function () { pv.classList.add('hidden'); });
  actions.appendChild(ok);
  actions.appendChild(cancel);
  pv.appendChild(actions);
  pv.classList.remove('hidden');
}

async function memConfirmImport() {
  if (!memPendingImport) return;
  const payload = {};
  memRegistry.forEach(s => { payload[s.key] = memPendingImport[s.key] || ''; });
  try {
    const saved = await API.put('/api/memory', { sections: payload });
    memApply(saved);
    (saved.warnings || []).forEach(w => showToast(w, 'warning'));
    showToast('已导入，可在「历史版本」里恢复', 'success');
    memPendingImport = null;
    const pv = $('#mem-import-preview');
    if (pv) pv.classList.add('hidden');
    memCloseIoBox();
  } catch (e) {
    showToast('导入失败: ' + e.message, 'error');
  }
}

function memCopyExport() {
  const ta = $('#mem-io-text');
  if (!ta) return;
  ta.select();
  ta.setSelectionRange(0, 999999);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  if (!ok && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(ta.value)
      .then(() => showToast('已复制', 'success'))
      .catch(() => showToast('复制失败，请手动全选复制', 'error'));
    return;
  }
  showToast(ok ? '已复制' : '复制失败，请手动全选复制', ok ? 'success' : 'error');
}

function memCloseIoBox() {
  const box = $('#mem-io-box');
  if (box) box.classList.add('hidden');
}

function memClosePanels(ids) {
  (ids || []).forEach(id => {
    const el = $(id);
    if (el) el.classList.add('hidden');
  });
}

// ===== 模型整理一段自述（只填编辑器）=====

async function memExtract() {
  const raw = $('#mem-raw');
  const src = raw ? (raw.value || '').trim() : '';
  if (!src) {
    showToast('先写点内容，再让模型整理', 'warning');
    return;
  }
  const btn = $('#btn-mem-extract');
  if (btn) { btn.disabled = true; btn.textContent = '整理中'; }
  try {
    const res = await API.post('/api/memory/extract', { text: src });
    const el = $('#mem-' + memEditingKey);
    if (el && res[memEditingKey] !== undefined) el.value = res[memEditingKey] || '';
    memShowWarn(res.unmatched);
    showToast(res.unmatched ? '已填入，另有几行没认出归属' : '已填入，确认后点保存',
              res.unmatched ? 'warning' : 'success');
    memRefreshCount();
  } catch (e) {
    // 失败时输入框内容保持不动：用户敲进去的字不能因为一次调用失败就没了
    showToast('整理失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '整理成这一段'; }
  }
}

function memShowWarn(text) {
  const w = $('#mem-warn');
  if (!w) return;
  if (text && text.trim()) {
    w.textContent = '这几行没认出属于哪一段，请手动挪到对应段里：\n' + text.trim();
    w.classList.remove('hidden');
  } else {
    w.textContent = '';
    w.classList.add('hidden');
  }
}

// ===== 关闭前拦一下：有未保存改动就问一句 =====

function memHasUnsaved() {
  return memIsDirty();
}

async function memGuardClose() {
  if (!memHasUnsaved()) return true;
  return await showConfirmDialog('有没保存的改动', '这段记忆还没保存，关掉就丢了。', '放弃改动');
}

// 关闭弹窗的唯一入口：点 X、点遮罩、按 Esc 都走这里。
// 分散在各处直接 classList.add('hidden') 的话，未保存的改动一定会被静默丢掉。
async function memTryClose(modalEl, doClose) {
  if (!modalEl || modalEl.id !== 'modal-memory') {
    doClose();
    return;
  }
  const ok = await memGuardClose();
  if (!ok) return;
  memExitEdit();
  doClose();
}

// ===== 事件绑定 =====

function initMemoryEvents() {
  const bind = (id, fn) => {
    const el = $(id);
    if (el) el.addEventListener('click', fn);
  };
  bind('#btn-mem-save', memSave);
  bind('#btn-mem-cancel', memCancelEdit);
  bind('#btn-mem-edit', function () { memEnterEdit(); });
  bind('#btn-mem-evolve', memEvolve);
  bind('#btn-mem-apply', memApplyProposals);
  bind('#btn-mem-proposals-close', memCloseProposals);
  bind('#btn-mem-versions-close', function () {
    const box = $('#mem-versions');
    const v = $('#mem-view');
    if (box) box.classList.add('hidden');
    if (v) v.classList.remove('hidden');
  });
  bind('#btn-mem-extract', memExtract);
  bind('#btn-mem-export', memShowExport);
  bind('#btn-mem-import', memShowImport);
  bind('#btn-mem-parse', memParseImport);
  bind('#btn-mem-copy', memCopyExport);
  bind('#btn-mem-io-close', memCloseIoBox);
  bind('#btn-mem-reset', memReset);

  const more = $('#btn-mem-more');
  if (more) {
    more.addEventListener('click', function () {
      const m = $('#mem-menu');
      if (m) m.classList.toggle('hidden');
    });
  }
  const menu = $('#mem-menu');
  if (menu) {
    menu.addEventListener('click', function (e) {
      const btn = e.target.closest('.mem-menu-item');
      if (!btn) return;
      const act = btn.getAttribute('data-act');
      menu.classList.add('hidden');
      if (act === 'import') memShowImport();
      if (act === 'export') memShowExport();
      if (act === 'history') memShowVersions();
      if (act === 'reset') memReset();
    });
  }

  // 查看区里的「编辑 / 删除」与历史区里的「恢复」都是动态渲染的，用事件委托
  const view = $('#mem-view');
  if (view) {
    view.addEventListener('click', function (e) {
      const edit = e.target.closest('[data-edit]');
      if (edit) { memEnterEdit(edit.getAttribute('data-edit')); return; }
      const del = e.target.closest('[data-del]');
      if (del) memDeleteItem(del.getAttribute('data-del'));
    });
  }
  const vers = $('#mem-versions-list');
  if (vers) {
    vers.addEventListener('click', function (e) {
      const btn = e.target.closest('[data-restore]');
      if (btn) memRestore(btn.getAttribute('data-restore'));
    });
  }

  memRegistry.forEach(s => {
    const el = $('#mem-' + s.key);
    if (el) el.addEventListener('input', memRefreshCount);
  });
  const en = $('#mem-enabled');
  if (en) en.addEventListener('change', memToggleEnabled);
}
