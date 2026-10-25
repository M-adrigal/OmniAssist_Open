#!/usr/bin/env python3
"""前端主流程运行时探针（无头 Chrome，真实 index.html + 真实 JS，stub 掉网络）。

为什么要有这个：这个前端没有模块系统、15 个文件共享全局作用域，出错时
**不会编译报错** —— 只有在浏览器里真的走一遍才暴露。2026-10-24 的
「发消息全挂（currentAttachments is not defined）」就是这么漏掉的。

这个脚本把主要流程逐个调一遍，任何 ReferenceError / TypeError 都记下来，
再对渲染结果做几条 DOM 断言。它不替代真实点测，但能在改动后 30 秒内
回答「有没有哪条路被改断了」。

覆盖：会话列表/切换/删除 · 消息渲染 · 技能面板（搜索/选中/管理/回收站）
· 流事件 12 种（含审批卡片）· 产出文件 · 停止/重新生成
· 记忆（四段编辑 / 总开关 / 整理 / 导入导出 / 上限）

⚠️ 凡是内部会 await 自定义确认弹窗的函数（deleteSession、memReset），
探针里必须**同步点掉** #btn-generic-confirm：那个 Promise 等人点，
不点就永远不 resolve，整条链会静默停在那一句 —— 页面无报错、
探针输出里也看不出「后面还有 10 步没跑」，只能靠流程步数发现异常。

用法：python3 scripts/test_frontend_flows.py
"""
import html as htmlmod
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
sys.path.insert(0, str(ROOT))


def _load_memory_store():
    """按文件路径加载 memory_store（绕开 server 包对 fastapi 的硬性要求）。

    这个脚本归在「需要 Chrome」那一类，不保证解释器里装了 fastapi；
    而它要验的是「前端拼的文本 ↔ 后端解析」这份契约，只需要纯逻辑部分。
    """
    path = os.path.join(str(ROOT), "server", "memory_store.py")
    spec = importlib.util.spec_from_file_location("memory_store_probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_store_probe"] = mod
    spec.loader.exec_module(mod)
    return mod

HEAD = """
<script>
window.__errors = [];
window.addEventListener('error', function (e) { window.__errors.push('error: ' + (e.message || '')); });
window.addEventListener('unhandledrejection', function (e) {
  window.__errors.push('rejection: ' + (e.reason && e.reason.message ? e.reason.message : e.reason));
});
</script>
"""

BODY = r"""
<script>
(function () {
  var out = { flows: [], dom: {}, notes: [], errors: [], stage: 'init' };

  // 先建好输出节点、每次推进都 flush —— 万一中途卡死/抛错，
  // 也能从 DOM 里看到走到了哪一步（否则只会得到「探针未执行」）
  var pre = document.createElement('pre');
  pre.id = 'probe-out';
  document.addEventListener('DOMContentLoaded', function () { document.body.appendChild(pre); });
  function flush() {
    out.errors = window.__errors || [];
    pre.textContent = JSON.stringify(out);
    if (!pre.parentNode && document.body) document.body.appendChild(pre);
  }

  function step(name, fn) {
    out.stage = name;
    try { fn(); out.flows.push([name, 'ok']); }
    catch (e) { out.flows.push([name, 'ERR ' + (e && e.message)]); }
    flush();
  }
  async function astep(name, fn) {
    out.stage = name;
    try { await fn(); out.flows.push([name, 'ok']); }
    catch (e) { out.flows.push([name, 'ERR ' + (e && e.message)]); }
    flush();
  }

  // ===== 网络桩 =====
  var SKILLS = {
    system_skills: [
      { name: 'document', description: 'Word/Excel/PPT 生成', enabled: true, source: 'system' },
      { name: 'web-fetch', description: '抓取网页正文', enabled: true, source: 'system' }
    ],
    user_skills: [
      { name: 'my_skill', description: '我的技能', enabled: true, source: 'uploaded', risk_level: 'low' },
      { name: 'old_skill', description: '已禁用技能', enabled: false, source: 'editor', risk_level: 'medium' }
    ]
  };
  var SESSIONS = [
    { id: 's1', title: '会话一', updated_at: '2026-10-24 10:00:00', created_at: '2026-10-24 09:00:00' },
    { id: 's2', title: '会话二', updated_at: '2026-10-23 10:00:00', created_at: '2026-10-23 09:00:00' }
  ];
  // showConfirmDialog 走的是自定义弹窗，不是 window.confirm；
  // 探针里用一个开关控制"用户点确认还是取消"，未保存保护那条用例要按取消。
  window.__confirmAnswer = true;
  window.confirm = function () { return window.__confirmAnswer !== false; };
  // 记忆桩：接口同形（段文本 + 条目 + 注册表 + 版本），文本由后端 render_text 生成
  var MEM_REGISTRY = [
    { key: 'work', title: '工作背景', kind: 'text', max_chars: 2000, max_items: 0, order: 10, inject: true, hint: '' },
    { key: 'personal', title: '个人背景', kind: 'text', max_chars: 2000, max_items: 0, order: 20, inject: true, hint: '' },
    { key: 'focus', title: '当前关注', kind: 'list', max_chars: 2000, max_items: 30, order: 30, inject: true, hint: '' },
    { key: 'recent', title: '近期动态', kind: 'list', max_chars: 2000, max_items: 20, order: 40, inject: true, hint: '' }
  ];
  var MEMORY_STUB = {
    work: '做数据集成', personal: '偏好结构化交付',
    focus: '- NDI 标准映射', recent: '- 提交需求文档',
    sections: { work: '做数据集成', personal: '偏好结构化交付',
                focus: '- NDI 标准映射', recent: '- 提交需求文档' },
    items: {
      work: [{ id: 11, content: '做数据集成' }],
      personal: [{ id: 12, content: '偏好结构化交付' }],
      focus: [{ id: 13, content: 'NDI 标准映射' }],
      recent: [{ id: 14, content: '提交需求文档' }]
    },
    registry: MEM_REGISTRY,
    enabled: 1, updated_at: '2026-10-24 22:00:00', text: __MEM_TEXT__, total_chars: 20,
    warnings: [], limits: { total_chars: 4000, recent_items: 20 },
    version_count: 2,
    versions: [
      { id: 2, reason: 'manual', chars: 120, created_at: '2026-10-24 22:00:00' },
      { id: 1, reason: 'reset', chars: 60, created_at: '2026-10-24 21:00:00' }
    ]
  };

  var realFetch = window.fetch;
  window.fetch = function (url, opts) {
    var u = String(url);
    var method = (opts && opts.method) || 'GET';
    if (u.indexOf('/api/chat/stream') >= 0) {
      var enc = new TextEncoder();
      return Promise.resolve(new Response(new ReadableStream({
        start: function (c) {
          c.enqueue(enc.encode('data: {"type":"token","content":"ok"}\n\n'));
          c.enqueue(enc.encode('data: {"type":"done"}\n\n'));
          c.close();
        }
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } }));
    }
    var body = {};
    if (u.indexOf('/api/files/uploads') === 0) {
      body = { files: [
        { filename: 'b.jpg', type: 'image', size: 10,
          path: 'document_output/u_probe/uploads/s1/b.jpg' },
        { filename: 'x.docx', type: 'docx', size: 10,
          path: 'document_output/u_probe/uploads/s1/x.docx' }
      ] };
    }
    else if (u.indexOf('/api/skills/trash') === 0) {
      // 回收站要单独给形状：{items:[...]}，不能落进通用 skills 分支
      body = { items: [{ name: 'old_skill', kind: 'db_skill',
                         deleted_at: '2026-10-24 21:57:31', risk_level: 'low' }] };
    }
    else if (u.indexOf('/api/skills') === 0) body = SKILLS;
    else if (u.indexOf('/api/sessions') === 0) {
      body = (method === 'GET' && /\/api\/sessions\/[^/?]+/.test(u))
        ? { id: 's1', title: '会话一', messages: [] } : SESSIONS;
    }
    else if (u.indexOf('/api/auth/me') === 0) body = { public_id: 'u_probe', username: 'probe', user_type: 'admin' };
    else if (u.indexOf('/api/chat/regenerate/') === 0) body = { message: '上一轮问题' };
    else if (u.indexOf('/api/usage') === 0) body = { total_prompt: 0, total_completion: 0, days: [], pending: {} };
    else if (u.indexOf('/api/config') === 0) body = { model: 'probe', providers: [], model_entries: [] };
    else if (u.indexOf('/api/memory/evolve') === 0) {
      body = { proposals: [
        { section_key: 'focus', section_title: '当前关注', content: '在推进 NDI 标准落地' },
        { section_key: 'recent', section_title: '近期动态', content: '去宜春出差' }
      ], session_id: 's1', session_title: '会话一', message_count: 12,
         duplicate_skipped: 1, dropped: 0, raw: '' };
    }
    else if (u.indexOf('/api/memory/item') === 0) body = MEMORY_STUB;
    else if (u.indexOf('/api/memory/versions/') === 0) body = MEMORY_STUB;
    else if (u.indexOf('/api/memory/extract') === 0) {
      body = { work: '产品经理', personal: '偏好结构化', focus: '- 数据集成',
               recent: '', unmatched: '这句不知道归哪段', raw: '## 工作背景\n产品经理' };
    }
    else if (u.indexOf('/api/memory/parse') === 0) {
      body = { work: '导入的工作', personal: '', focus: '', recent: '', unmatched: '' };
    }
    else if (u.indexOf('/api/memory/reset') === 0) {
      body = { work: '', personal: '', focus: '', recent: '', sections: {}, items: {},
               registry: MEM_REGISTRY, enabled: 1,
               updated_at: '2026-10-24 22:30:00', text: '', total_chars: 0,
               warnings: [], limits: { total_chars: 4000, recent_items: 20 },
               version_count: 3, versions: MEMORY_STUB.versions };
    }
    else if (u.indexOf('/api/memory/enabled') === 0) {
      body = { work: '做数据集成', personal: '偏好结构化交付', focus: '- NDI 标准映射',
               recent: '- 提交需求文档',
               sections: MEMORY_STUB.sections, items: MEMORY_STUB.items, registry: MEM_REGISTRY,
               enabled: 0, updated_at: '2026-10-24 22:20:00', text: '', total_chars: 0,
               warnings: [], limits: { total_chars: 4000, recent_items: 20 },
               version_count: 2, versions: MEMORY_STUB.versions };
    }
    else if (u.indexOf('/api/memory') === 0) body = MEMORY_STUB;
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' } }));
  };

  (async function () {
    // ===== 1. 会话 =====
    await astep('loadSessions', loadSessions);
    out.dom.sessionRows = document.querySelectorAll('#session-list .session-item, .session-item').length;

    await astep('switchSession(s1)', function () { return switchSession('s1'); });
    out.dom.currentSession = state.currentSessionId;
    // 桩里 /api/config/models 返回空列表 → 角标必须说「无可用模型」，
    // 不能显示上次选中的模型名（管理员隐藏全部模型后就是这样）
    out.dom.modelLabel = (document.getElementById('model-label') || {}).textContent || '';

    step('appendMessage(user)', function () {
      appendMessage('user', '探针提问');
    });
    step('appendMessage(assistant,带meta)', function () {
      appendMessage('assistant', '探针回答', { tools: [], thought: '想了下', search: null });
    });

    // ===== 2. 技能面板 =====
    await astep('loadSkillPanel', loadSkillPanel);
    step('openSkillPanel', openSkillPanel);
    step('renderSkillPanelList', renderSkillPanelList);
    out.dom.skillPanelRows = document.querySelectorAll('#skill-panel-list .skill-row').length;
    out.dom.skillPanelVisible = !!document.querySelector('#skill-panel:not(.hidden)');

    step('面板搜索过滤', function () {
      var q = document.getElementById('skill-panel-q');
      if (q) { q.value = 'my_skill'; renderSkillPanelList(); }
    });
    out.dom.skillPanelRowsFiltered = document.querySelectorAll('#skill-panel-list .skill-row').length;

    step('点技能行选中', function () {
      var row = document.querySelector('#skill-panel-list .skill-row:not(.disabled)');
      if (row) row.click();
    });
    out.dom.selectedSkills = (state.selectedSkills || []).slice();
    step('closeSkillPanel', closeSkillPanel);

    // ===== 3. 技能管理弹窗 =====
    await astep('loadSkills', loadSkills);
    step('switchSkillTab(user)', function () { switchSkillTab('user'); });
    out.dom.skillFilterbarShown = (function () {
      var bar = document.getElementById('skill-user-filterbar');
      return bar ? bar.style.display !== 'none' : null;
    })();
    step('切换来源筛选', function () {
      var sel = document.querySelector('.skill-mg-select');
      if (sel) { sel.value = 'uploaded'; sel.dispatchEvent(new Event('change')); }
    });
    out.dom.skillListItems = document.querySelectorAll('#skills-container .skill-item').length;
    out.dom.skillExportBtns = document.querySelectorAll('#skills-container [data-skill-action="export"]').length;

    // 附件渲染：图片应是缩略图（<img>），其它类型仍是 chip
    out.dom.attachImage = renderAttachmentChips([{ filename: 'a.png', type: 'image' }]);
    out.dom.attachPdf = renderAttachmentChips([{ filename: 'a.pdf', type: 'pdf' }]);
    out.dom.attachHist = renderAttachmentChips(['b.jpg']);   // 历史消息只有文件名字符串
    // 兜底路径：清单还没就绪时（例如刚发完消息、尚未重新拉列表）应退到 upload-thumb
    var _savedFiles = state.uploadFiles;
    state.uploadFiles = {};
    out.dom.attachHistFallback = renderAttachmentChips(['b.jpg']);
    state.uploadFiles = _savedFiles;
    // 当前轮：上传接口给了 path —— 必须走 download&inline，不能走 preview
    // （preview 对图片返回 JSON，<img> 拿到必然白图，2026-10-24 就这么翻过车）
    out.dom.attachNow = renderAttachmentChips([{
      filename: 'c.png', type: 'image',
      path: 'document_output/u_probe/uploads/s1/c.png'
    }]);

    await astep('loadSkillTrash', loadSkillTrash);
    // 回收站真的渲染了吗？—— 只断言「没抛异常」抓不到 $('id') 少 # 这种静默空操作
    out.dom.trashRestoreBtns = document.querySelectorAll('#skill-trash-list [data-trash-restore]').length;
    out.dom.trashDeleteBtns = document.querySelectorAll('#skill-trash-list [data-trash-delete]').length;
    // 历史图片：会话文件清单已建立 → 应直接用真实路径，不依赖 upload-thumb
    out.dom.uploadPathB = ((state.uploadFiles || {})['b.jpg'] || {}).path || null;
    out.dom.attachHistMapped = renderAttachmentChips(['b.jpg']);
    // 历史里的文档：类型从清单补，图标应是 W 而不是兜底的 F
    out.dom.attachHistDoc = renderAttachmentChips(['x.docx']);

    // 气泡宽度：短消息 + 一张图时不该吃满 78%（2026-10-24 用户反馈"右侧空太多"）
    step('appendMessage(user, 带图) 用于量气泡宽度', function () {
      appendMessage('user', '短消息', [{ filename: 'a.png', type: 'image', path: 'a/a.png' }]);
    });
    // /reset 边界：历史渲染时画分隔线，而不是显示一条 /reset 气泡
    step('renderHistoryMessage(context_reset)', function () {
      renderHistoryMessage({ role: 'user', content: '/reset', context_reset: true });
    });
    out.dom.resetDivider = document.querySelectorAll('.context-reset-divider').length;
    out.dom.resetBubble = (function () {
      var els = document.querySelectorAll('.session-messages .message.user');
      var last = els[els.length - 1];
      return last ? (last.textContent || '').indexOf('/reset') >= 0 : null;
    })();

    out.dom.userBubbleWidth = (function () {
      var msgs = document.querySelectorAll('.session-messages .message.user');
      var last = msgs[msgs.length - 1];
      return last ? Math.round(last.getBoundingClientRect().width) : null;
    })();
    out.dom.msgColWidth = (function () {
      var c = document.querySelector('.session-messages');
      return c ? Math.round(c.getBoundingClientRect().width) : null;
    })();

    // 点缩略图 → 本页弹窗（不是新标签页）
    step('点缩略图打开弹窗', function () {
      var cell = document.querySelector('.msg-attach-thumb');
      if (cell) cell.click();
    });
    out.dom.lightboxOpen = !!document.querySelector('#image-lightbox:not(.hidden)');
    out.dom.lightboxHasImg = (function () {
      var i = document.querySelector('#image-lightbox .lightbox-img');
      return !!(i && i.getAttribute('src'));
    })();
    step('Esc 关闭弹窗', function () {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    });
    out.dom.lightboxClosed = !document.querySelector('#image-lightbox:not(.hidden)');
    out.dom.trashText = ((document.querySelector('#skill-trash-list') || {}).textContent || '').trim().slice(0, 40);

    step('switchSkillTab(system) 应收起回收站', function () {
      var panel = document.querySelector('#skill-trash-panel');
      if (panel) panel.classList.remove('hidden');   // 模拟用户先点开过
      switchSkillTab('system');
    });
    out.dom.trashHiddenOnSystem = (function () {
      var panel = document.querySelector('#skill-trash-panel');
      return panel ? panel.classList.contains('hidden') : null;
    })();
    step('switchSkillTab(user) 复位', function () { switchSkillTab('user'); });
    step('openModal(modal-skills)', function () { openModal('modal-skills'); });
    step('closeModal(modal-skills)', function () { closeModal('modal-skills'); });

    // ===== 4. 流事件：后端会发的每一种都喂一遍 =====
    var stream = createAssistantContainer();
    var events = [
      ['status', { type: 'status', content: '正在处理...' }],
      ['thought', { type: 'thought', content: '思考中' }],
      ['tool_call', { type: 'tool_call', name: 'save_word', arguments: { filename: 'x' } }],
      ['tool_result', { type: 'tool_result', name: 'save_word', content: 'saved' }],
      ['tool_summary', { type: 'tool_summary', tools: [{ name: 'save_word', result: 'saved' }] }],
      ['approval_required', { type: 'approval_required', group_id: 'g1', session_id: 's1',
        items: [{ item_id: 'i1', tool: 'run_command', args: { cmd: 'ls' }, risk: 'exec', desc: '执行命令' }] }],
      ['approval_resolved', { type: 'approval_resolved', group_id: 'g1', decisions: {} }],
      ['approval_skipped', { type: 'approval_skipped', session_id: 's1', reason: 'trusted', items: [] }],
      ['files_created', { type: 'files_created', files: [{ name: 'a.docx', path: 'document_output/u_probe/word_output/a.docx', size: 10, ext: 'docx' }] }],
      ['token', { type: 'token', content: '正文' }],
      ['done', { type: 'done' }]
    ];
    // 每个事件处理后记一次状态行快照（用来验证 status 的「出现 → 收起 → 常驻」节奏）
    function snapStatus(tag) {
      var el = stream.statusEl;
      if (!el) { out.dom['status_' + tag] = 'NO-EL'; return; }
      out.dom['status_' + tag] = el.classList.contains('hidden')
        ? 'hidden'
        : (el.classList.contains('warn') ? 'warn:' : 'shown:') + (el.textContent || '');
    }
    events.forEach(function (pair) {
      step('handleStreamEvent:' + pair[0], function () {
        var r = handleStreamEvent(stream, pair[1]);
        out.dom['event_' + pair[0]] = String(r);
      });
      snapStatus(pair[0]);
    });

    // status 的两种结局：普通进度提示被正文/结束收起；warn 必须常驻
    step('warn 状态（提前结束）应常驻', function () {
      handleStreamEvent(stream, { type: 'status', content: '检测到工具调用异常，提前结束' });
    });
    snapStatus('warn_after');
    handleStreamEvent(stream, { type: 'done' });
    snapStatus('warn_after_done');
    out.dom.answerText = (stream.answerEl.textContent || '').slice(0, 60);
    out.dom.approvalCard = !!document.querySelector('#approval-bar .approval-card');
    out.dom.outputFiles = document.querySelectorAll('.output-file-card, .output-files-header').length;

    // ===== 5. 停止 / 重新生成 =====
    await astep('stopGeneration(无任务时)', function () { return stopGeneration(); });
    state.currentSessionId = 's1';
    await astep('regenerateLastTurn', function () { return regenerateLastTurn(); });

    // ===== 6. 删除会话 =====
    // deleteSession 内部会 await showConfirmDialog —— 那是等用户点确认的 Promise，
    // 不点就永远不 resolve，整条探针链会**静默停在这里**（之前就是这么停在
    // 第 36 步，后面加了什么都跑不到，而页面无报错，从外面完全看不出来）。
    await astep('deleteSession(s2)', function () {
      var p = deleteSession('s2');
      var btn = document.getElementById('btn-generic-confirm');
      if (btn) btn.click();
      return p;
    });

    // ===== 7. 记忆（注册表渲染 + 逐段编辑 + 提炼 + 历史 + 导入导出）=====
    await astep('memLoad', memLoad);
    out.dom.memEnabled = (document.getElementById('mem-enabled') || {}).checked;
    // 默认查看态：按 registry 渲染成文本，编辑器与底栏都藏着
    out.dom.memViewShownAfterLoad = !document.getElementById('mem-view').classList.contains('hidden');
    out.dom.memEditHiddenAfterLoad = document.getElementById('mem-edit').classList.contains('hidden');
    out.dom.memFooterHiddenAfterLoad = document.getElementById('mem-footer').classList.contains('hidden');
    out.dom.memViewTextAfterLoad = (document.getElementById('mem-view') || {}).textContent || '';
    out.dom.memEditBtns = document.querySelectorAll('#mem-view [data-edit]').length;
    out.dom.memDelBtns = document.querySelectorAll('#mem-view [data-del]').length;

    // 逐段编辑：进入后只有那一段的编辑器可见
    step('memEnterEdit(work)', function () { memEnterEdit('work'); });
    out.dom.memWork = (document.getElementById('mem-work') || {}).value || null;
    out.dom.memRecent = (document.getElementById('mem-recent') || {}).value || null;
    out.dom.memSaveDisabledWhenClean = (document.getElementById('btn-mem-save') || {}).disabled;

    // 模型整理一段自述：只填编辑器
    step('填入自述', function () {
      var raw = document.getElementById('mem-raw');
      if (raw) raw.value = '我是产品经理，正在做数据集成';
    });
    await astep('memExtract', memExtract);
    out.dom.memWorkAfterExtract = (document.getElementById('mem-work') || {}).value || null;
    out.dom.memWarnShown = !!document.querySelector('#mem-warn:not(.hidden)');
    out.dom.memWarnText = (document.getElementById('mem-warn') || {}).textContent || '';

    // 超段上限：保存禁用 + 计数变警示
    step('把这段撑到超限', function () {
      var el = document.getElementById('mem-work');
      if (el) el.value = '甲'.repeat(2100);
      memRefreshCount();
    });
    out.dom.memSaveDisabledWhenOver = (document.getElementById('btn-mem-save') || {}).disabled;
    out.dom.memCountOver = (function () {
      var c = document.getElementById('mem-edit-count');
      return !!(c && c.classList.contains('over'));
    })();
    out.dom.memCountText = (document.getElementById('mem-edit-count') || {}).textContent || '';

    step('改成正常且**有改动**的内容', function () {
      var el = document.getElementById('mem-work');
      if (el) el.value = '做数据集成（改过）';
      memRefreshCount();
    });
    out.dom.memSaveEnabledAfterRestore = !(document.getElementById('btn-mem-save') || {}).disabled;
    await astep('memSave', memSave);
    out.dom.memEditHiddenAfterSave = document.getElementById('mem-edit').classList.contains('hidden');
    out.dom.memFooterHiddenAfterSave = document.getElementById('mem-footer').classList.contains('hidden');
    out.dom.memViewShownAfterSave = !document.getElementById('mem-view').classList.contains('hidden');

    // 未保存保护：有改动时点关闭，用户选「不放弃」就**不该**被关掉
    step('改一点但不保存', function () {
      memEnterEdit('personal');
      var el = document.getElementById('mem-personal');
      if (el) el.value = '随手改的';
      memRefreshCount();
    });
    out.dom.memUnsavedDetected = memHasUnsaved();
    await astep('memTryClose（用户选「不放弃」）', function () {
      var closed = false;
      var p = memTryClose(document.getElementById('modal-memory'), function () { closed = true; });
      // 自定义确认弹窗必须点一下才会 resolve：点「取消/X」= 不放弃（resolve false）。
      // 不点的话这个 Promise 永远挂着，后面的步骤一个都跑不到（之前踩过两次）。
      var cancelBtn = document.querySelector('#modal-generic-confirm .modal-close');
      if (cancelBtn) cancelBtn.click();
      return p.then(function () { out.dom.memClosedWhenCancelled = closed; });
    });
    step('memCancelEdit 放弃改动', memCancelEdit);
    out.dom.memUnsavedAfterCancel = memHasUnsaved();

    // 进化：模型从对话里提炼 → 勾选 → 加入
    await astep('memEvolve', memEvolve);
    out.dom.memProposalsShown = !document.getElementById('mem-proposals').classList.contains('hidden');
    out.dom.memProposalCount = document.querySelectorAll('#mem-proposals-list input[data-prop]').length;
    out.dom.memProposalSrc = (document.getElementById('mem-proposals-src') || {}).textContent || '';
    await astep('memApplyProposals', memApplyProposals);
    out.dom.memProposalsHiddenAfterApply = document.getElementById('mem-proposals').classList.contains('hidden');

    // 历史版本：能列出、能恢复
    step('memShowVersions', memShowVersions);
    out.dom.memVersionsShown = !document.getElementById('mem-versions').classList.contains('hidden');
    out.dom.memVersionRows = document.querySelectorAll('#mem-versions-list [data-restore]').length;
    await astep('memRestore（含确认弹窗）', function () {
      var btn = document.getElementById('btn-generic-confirm');
      if (!btn) return Promise.resolve();
      var p = memRestore(2);
      btn.click();
      return p;
    });
    step('收起历史', function () {
      var b = document.getElementById('btn-mem-versions-close');
      if (b) b.click();
    });

    // 导入：先给差异预览，确认才写入（不能一解析就覆盖）
    step('memShowImport', memShowImport);
    out.dom.memImportReadonly = (document.getElementById('mem-io-text') || {}).readOnly;
    out.dom.memParseShown = (function () {
      var b = document.getElementById('btn-mem-parse');
      return !!(b && !b.classList.contains('hidden'));
    })();
    await astep('memParseImport', memParseImport);
    out.dom.memPreviewShown = !document.getElementById('mem-import-preview').classList.contains('hidden');
    out.dom.memDiffRows = document.querySelectorAll('#mem-import-preview .mem-diff').length;
    await astep('memConfirmImport', memConfirmImport);

    // 导出：一份纯文本，段标题齐全（后端 parse_text 认这个格式）
    step('memShowExport', memShowExport);
    out.dom.memExportText = (document.getElementById('mem-io-text') || {}).value || '';
    out.dom.memExportReadonly = (document.getElementById('mem-io-text') || {}).readOnly;
    out.dom.memCopyShown = (function () {
      var b = document.getElementById('btn-mem-copy');
      return !!(b && !b.classList.contains('hidden'));
    })();
    out.dom.memParseHidden = (function () {
      var b = document.getElementById('btn-mem-parse');
      return !!(b && b.classList.contains('hidden'));
    })();
    step('memCloseIoBox', memCloseIoBox);

    step('关掉开关', function () {
      var en = document.getElementById('mem-enabled');
      if (en) en.checked = false;
    });
    await astep('memToggleEnabled', memToggleEnabled);
    out.dom.memEnabledAfterToggle = (document.getElementById('mem-enabled') || {}).checked;
    // 关掉之后界面必须看得出来：说明文案变了 + 内容区变暗
    out.dom.memStateDescWhenOff = (document.getElementById('mem-state-desc') || {}).textContent || '';
    out.dom.memViewDimWhenOff = document.getElementById('mem-view').classList.contains('is-off');

    await astep('memReset（含确认弹窗）', function () {
      var btn = document.getElementById('btn-generic-confirm');
      if (!btn) return Promise.resolve();
      var p = memReset();
      btn.click();
      return p;
    });
    // 清空后按查看区文本判断：写 `|| null` 会让「空」和「元素不存在」长得一样
    out.dom.memViewEmptyAfterReset = (function () {
      var v = document.getElementById('mem-view');
      return v ? (v.textContent.indexOf('还没有内容') >= 0) : null;
    })();

    out.stage = 'done';
    flush();
  })().catch(function (e) {
    out.stage = 'fatal';
    out.notes.push('顶层异常: ' + (e && e.message));
    flush();
  });
})();
</script>
"""


def main():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', f'href="file://{ROOT}/static/')
    html = html.replace('src="/static/', f'src="file://{ROOT}/static/')
    html = html.replace("<head>", "<head>" + HEAD, 1)
    html = html.replace("</body>", BODY + "</body>", 1)
    # 记忆桩里的导出文本必须用后端同一份 render_text 生成 —— 前端自己拼、后端自己解析，
    # 两边格式对不上时界面毫无异常，只有用户把文本粘回来那一次才发现全跑到 unmatched 里。
    _ms = _load_memory_store()
    _mem_text = _ms.render_text({
        "work": "做数据集成", "personal": "偏好结构化交付",
        "focus": "- NDI 标准映射", "recent": "- 提交需求文档",
    })
    html = html.replace("__MEM_TEXT__", json.dumps(_mem_text))

    tmp = Path(tempfile.mkdtemp()) / "flows.html"
    tmp.write_text(html, encoding="utf-8")

    with tempfile.TemporaryDirectory() as profile:
        cmd = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
               f"--user-data-dir={profile}", "--window-size=1440,1000",
               "--allow-file-access-from-files",
               "--virtual-time-budget=10000", "--dump-dom", tmp.as_uri()]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    dom = res.stdout
    idx = dom.find('<pre id="probe-out"')
    if idx < 0:
        print("✗ 探针未执行（页面可能加载失败）")
        print(dom[:800])
        return 1
    start = dom.find(">", idx) + 1
    end = dom.find("</pre>", start)
    data = json.loads(htmlmod.unescape(dom[start:end]))

    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    flows = data.get("flows") or []
    print(f"跑了 {len(flows)} 个流程")
    errs = [(n, s) for n, s in flows if s != "ok"]
    for n, s in errs:
        print(f"  ✗ {n}: {s}")
    check("所有流程无运行时异常", not errs, errs)
    check("无 window 级 JS 错误", not data.get("errors"), data.get("errors"))

    d = data.get("dom") or {}
    print("\n渲染结果：")
    for k in sorted(d):
        print(f"    {k} = {d[k]}")

    print("\n断言：")
    check("会话列表渲染出 2 条", d.get("sessionRows") == 2, d.get("sessionRows"))
    check("/reset 渲染成「上下文已重置」分隔线",
          (d.get("resetDivider") or 0) >= 1, d.get("resetDivider"))
    check("/reset 不再当成普通气泡显示", d.get("resetBubble") is False, d.get("resetBubble"))
    check("没有可用模型时角标显示「无可用模型」",
          d.get("modelLabel") == "无可用模型", d.get("modelLabel"))
    check("切换会话后 currentSessionId 正确", d.get("currentSession == undefined") or
          d.get("currentSession") == "s1", d.get("currentSession"))
    check("技能面板行数 = 4（2 系统 + 2 用户）", d.get("skillPanelRows") == 4, d.get("skillPanelRows"))
    check("搜索过滤后只剩 1 行", d.get("skillPanelRowsFiltered") == 1, d.get("skillPanelRowsFiltered"))
    check("点行后 selectedSkills 有 1 个", len(d.get("selectedSkills") or []) == 1, d.get("selectedSkills"))
    check("回收站每条都有「彻底删除」按钮", (d.get("trashDeleteBtns") or 0) >= 1, d.get("trashDeleteBtns"))
    check("技能行有「导出」按钮", (d.get("skillExportBtns") or 0) >= 1, d.get("skillExportBtns"))
    check("图片附件渲染成缩略图（<img>）",
          "<img" in (d.get("attachImage") or "") and "msg-attach-thumb" in (d.get("attachImage") or ""),
          d.get("attachImage"))
    check("非图片附件仍是文件名 chip",
          "msg-attach-tag" in (d.get("attachPdf") or "") and "<img" not in (d.get("attachPdf") or ""),
          d.get("attachPdf"))
    check("历史消息的图片文件名字符串也能出缩略图",
          "<img" in (d.get("attachHist") or ""), d.get("attachHist"))
    check("历史图片有清单时走 download（当前会话可解析到真实路径）",
          "/api/files/download" in (d.get("attachHist") or ""), d.get("attachHist"))
    check("清单缺失时退到 upload-thumb（兜底，不露白图）",
          "/api/files/upload-thumb" in (d.get("attachHistFallback") or ""),
          d.get("attachHistFallback"))
    check("切换会话后会建立「文件名→路径+类型」清单",
          (d.get("uploadPathB") or "").endswith("uploads/s1/b.jpg"), d.get("uploadPathB"))
    check("图片点击在本页弹窗打开（不新开标签页）",
          d.get("lightboxOpen") is True and d.get("lightboxHasImg") is True,
          {"open": d.get("lightboxOpen"), "hasImg": d.get("lightboxHasImg")})
    check("Esc 能关闭图片弹窗", d.get("lightboxClosed") is True, d.get("lightboxClosed"))
    # 断的是绝对宽度：一张 420 的图 + 内边距 ≈ 452；
    # 去掉 width:fit-content 后气泡会吃满 78%（810×0.78 = 632），这条就会挂。
    check("短消息气泡随内容收缩（右侧不该有空白）",
          (d.get("userBubbleWidth") or 0) > 0 and (d.get("userBubbleWidth") or 0) < 520,
          {"bubble": d.get("userBubbleWidth"), "col": d.get("msgColWidth")})
    check("历史里的文档图标按类型渲染（W，不是兜底 F）",
          'tag-icon">W<' in (d.get("attachHistDoc") or ""), d.get("attachHistDoc"))
    check("历史图片有映射时走 download（不依赖新接口，重启即生效）",
          "/api/files/download" in (d.get("attachHistMapped") or "")
          and "upload-thumb" not in (d.get("attachHistMapped") or ""),
          d.get("attachHistMapped"))
    check("当前轮图片走 download&inline（不是 preview —— 那个返回 JSON）",
          "/api/files/download" in (d.get("attachNow") or "")
          and "inline=true" in (d.get("attachNow") or "")
          and "preview" not in (d.get("attachNow") or ""),
          d.get("attachNow"))
    check("回收站渲染出可恢复条目（不是停在「加载中」）",
          (d.get("trashRestoreBtns") or 0) >= 1 and "old_skill" in (d.get("trashText") or ""),
          {"btns": d.get("trashRestoreBtns"), "text": d.get("trashText")})
    check("切到系统技能 tab 时回收站收起", d.get("trashHiddenOnSystem") is True, d.get("trashHiddenOnSystem"))
    check("审批卡片渲染出来了", d.get("approvalCard") is True, d.get("approvalCard"))
    check("产出文件卡片渲染出来了", (d.get("outputFiles") or 0) >= 1, d.get("outputFiles"))
    check("正文累积到 answerEl", "正文" in (d.get("answerText") or ""), d.get("answerText"))

    # status 事件（2026-10-24 之前前端完全不处理，任务被掐断时用户毫无提示）
    check("status 事件会显示状态行",
          str(d.get("status_status", "")).startswith("shown:") and "正在处理" in str(d.get("status_status")),
          d.get("status_status"))
    check("正文开始输出后普通状态行收起", d.get("status_token") == "hidden", d.get("status_token"))
    check("「提前结束」以 warn 样式常驻",
          str(d.get("status_warn_after", "")).startswith("warn:") and "提前结束" in str(d.get("status_warn_after")),
          d.get("status_warn_after"))
    check("done 之后 warn 仍然可见（用户能看到中断原因）",
          str(d.get("status_warn_after_done", "")).startswith("warn:"),
          d.get("status_warn_after_done"))

    print("\n记忆面板：")
    check("查看态按 registry 渲染出每一段",
          all(t in (d.get("memViewTextAfterLoad") or "")
              for t in ("工作背景", "个人背景", "当前关注", "近期动态")),
          (d.get("memViewTextAfterLoad") or "")[:80])
    check("查看态每段都有编辑入口", (d.get("memEditBtns") or 0) == 4, d.get("memEditBtns"))
    check("列表段每条都有删除按钮", (d.get("memDelBtns") or 0) >= 2, d.get("memDelBtns"))
    check("默认是查看态（编辑器和底栏都藏着）",
          d.get("memViewShownAfterLoad") is True and d.get("memEditHiddenAfterLoad") is True
          and d.get("memFooterHiddenAfterLoad") is True,
          {"view": d.get("memViewShownAfterLoad"), "edit": d.get("memEditHiddenAfterLoad"),
           "footer": d.get("memFooterHiddenAfterLoad")})
    check("逐段编辑：进入后那一段的内容在编辑器里",
          d.get("memWork") == "做数据集成", d.get("memWork"))
    check("没改动时保存按钮禁用（不让人白点一次）",
          d.get("memSaveDisabledWhenClean") is True, d.get("memSaveDisabledWhenClean"))
    check("保存后回到查看态",
          d.get("memEditHiddenAfterSave") is True and d.get("memFooterHiddenAfterSave") is True
          and d.get("memViewShownAfterSave") is True,
          {"edit": d.get("memEditHiddenAfterSave"), "footer": d.get("memFooterHiddenAfterSave"),
           "view": d.get("memViewShownAfterSave")})
    check("未保存改动能被检测到", d.get("memUnsavedDetected") is True, d.get("memUnsavedDetected"))
    check("用户不放弃时弹窗不关（改动不会静默丢）",
          d.get("memClosedWhenCancelled") is False, d.get("memClosedWhenCancelled"))
    check("放弃改动后回到已保存版本",
          d.get("memUnsavedAfterCancel") is False, d.get("memUnsavedAfterCancel"))
    check("总开关按接口状态勾选", d.get("memEnabled") is True, d.get("memEnabled"))
    check("关掉开关后界面看得出来（文案 + 内容变暗）",
          "已关闭" in (d.get("memStateDescWhenOff") or "") and d.get("memViewDimWhenOff") is True,
          {"desc": (d.get("memStateDescWhenOff") or "")[:40], "dim": d.get("memViewDimWhenOff")})
    check("提炼结果渲染成可勾选的提议",
          d.get("memProposalsShown") is True and (d.get("memProposalCount") or 0) == 2,
          {"shown": d.get("memProposalsShown"), "n": d.get("memProposalCount")})
    check("提议标明了来自哪个会话", "会话一" in (d.get("memProposalSrc") or ""),
          d.get("memProposalSrc"))
    check("加入后提议区收起", d.get("memProposalsHiddenAfterApply") is True,
          d.get("memProposalsHiddenAfterApply"))
    check("历史版本列出可恢复的条目",
          d.get("memVersionsShown") is True and (d.get("memVersionRows") or 0) == 2,
          {"shown": d.get("memVersionsShown"), "rows": d.get("memVersionRows")})
    # 前端自己拼导出文本、后端自己解析导入文本 —— 两边格式对不上的话，
    # 界面上一切正常，只有用户把文本粘回来那一次才发现内容全跑到 unmatched 里。
    _ms = _load_memory_store()
    _expect = {
        "work": "做数据集成", "personal": "偏好结构化交付",
        "focus": "- NDI 标准映射", "recent": "- 提交需求文档",
    }
    _js = d.get("memExportText") or ""
    _parsed = _ms.parse_text(_js)
    check("前端导出文本与后端 render_text 逐字一致",
          _js == _ms.render_text(_expect), repr(_js[:60]))
    check("前端导出的文本后端能原样认回四段",
          _parsed["sections"] == _expect and _parsed["unmatched"] == "",
          {"sections": _parsed["sections"], "unmatched": _parsed["unmatched"]})
    check("导出态：只读 + 只显示复制按钮",
          d.get("memExportReadonly") is True and d.get("memCopyShown") is True
          and d.get("memParseHidden") is True,
          {"ro": d.get("memExportReadonly"), "copy": d.get("memCopyShown"),
           "parse": d.get("memParseHidden")})
    check("导入态：可编辑 + 换成解析按钮",
          d.get("memImportReadonly") is False and d.get("memParseShown") is True,
          {"ro": d.get("memImportReadonly"), "parse": d.get("memParseShown")})
    check("导入先给差异预览（不是一解析就覆盖）",
          d.get("memPreviewShown") is True and (d.get("memDiffRows") or 0) == 4,
          {"shown": d.get("memPreviewShown"), "rows": d.get("memDiffRows")})
    check("模型整理把结果填进编辑器",
          d.get("memWorkAfterExtract") == "产品经理", d.get("memWorkAfterExtract"))
    check("认不出归属的行原样显示出来（不静默丢）",
          d.get("memWarnShown") is True and "这句不知道归哪段" in (d.get("memWarnText") or ""),
          {"shown": d.get("memWarnShown"), "text": d.get("memWarnText")})
    check("超出段上限时保存按钮禁用",
          d.get("memSaveDisabledWhenOver") is True, d.get("memSaveDisabledWhenOver"))
    check("超限时计数变警示态并带上限值",
          d.get("memCountOver") is True and "2000" in (d.get("memCountText") or ""),
          {"over": d.get("memCountOver"), "text": d.get("memCountText")})
    check("改回上限内且有改动时保存按钮恢复可用",
          d.get("memSaveEnabledAfterRestore") is True, d.get("memSaveEnabledAfterRestore"))
    check("关掉开关后状态留在关闭",
          d.get("memEnabledAfterToggle") is False, d.get("memEnabledAfterToggle"))
    check("重置后回到空态", d.get("memViewEmptyAfterReset") is True,
          d.get("memViewEmptyAfterReset"))

    print()
    print("✓ 通过" if ok else "✗ 存在问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
