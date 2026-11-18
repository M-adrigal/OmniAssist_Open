#!/usr/bin/env python3
"""消息导航（右侧刻度条 + 回到底部浮标）行为验收。

验的是「用户点了会怎样」，不是样式。七个断言覆盖四条硬要求：

  ① 点第 N 条刻度 → 那条提问落在容器顶部附近（12px 余量，容差 40px）
  ② 滚到中段浮标出现 / 滚到底浮标消失
  ③ 点浮标回到最底，且浮标随即收起
  ④ 任务运行中浮标带 loading 圈，结束后必须摘掉

为什么不放进 test_frontend_flows.py：
  那边验的是「发消息链路」与弹窗层级，都是需要后端契约的东西；
  这里是纯前端几何 + 状态机，用同一套 headless 手法但内容自己造，
  放一起会让两边都要读一遍对方的 14 组假数据。

⚠️ 构造数据时必须给 currentSessionId 赋一个真实 id。
  留 null 会走进「currentSessionId / streamingSessionId / subscribingSessionId
  三者都是 null」的边界，而 `null === null` 为真会让「正在流式」「正在订阅」
  两条判据误成立 —— 实际表现是「任务跑完了 loading 圈还在转」。
  这个坑真实存在过（2026-10-29 修），测试必须能复现它。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

QS = ["ts语言是什么", "它和javascript有什么区别", "我该学哪个", "有没有推荐的学习资料",
      "那类型体操是什么", "泛型怎么理解", "装饰器还用得多吗", "如何做类型收窄",
      "interface和type选哪个", "怎么和现有js项目共存", "编译速度慢怎么优化",
      "团队协作要注意什么", "要不要上strict模式", "怎么调试类型报错"]
ANS = ["TS 语言通常指 TypeScript。" * 6, "主要区别在类型系统。" * 8, "先学 JavaScript 基础。" * 8,
       "官方 Handbook 最权威。" * 8, "在类型层面做编程。" * 8, "泛型是把类型当参数传。" * 8,
       "Angular 里仍是主流。" * 8, "用 typeof、in 和类型守卫。" * 8, "interface 可合并。" * 8,
       "先开 allowJs 逐步改。" * 8, "开 skipLibCheck 与 incremental。" * 8,
       "约定 lint 规则更有效。" * 8, "新项目建议开 strict。" * 8, "配 sourceMap。" * 8]


def build_messages():
    out = []
    for q, a in zip(QS, ANS):
        out.append('<div class="message user"><div class="message-body">'
                   f'<div class="answer-area"><p>{q}</p></div></div></div>')
        out.append('<div class="message assistant"><div class="message-body">'
                   f'<div class="answer-area"><p>{a}</p></div></div></div>')
    return json.dumps(out, ensure_ascii=False)


PROBE_JS = """
<script>
document.addEventListener('DOMContentLoaded', function () { (async function () {
  var wrap = document.getElementById('chat-messages');
  var host = document.createElement('div');
  host.className = 'session-messages';
  host.innerHTML = @@MSGS@@.join('');
  var old = wrap.querySelector('.welcome-message'); if (old) old.remove();
  wrap.appendChild(host);
  // 必须给真实会话 id（见文件头说明）
  state.currentSessionId = 'sess-probe';
  state.sessionContainers['sess-probe'] = { element: host, stream: null };
  document.documentElement.classList.add('dark');
  document.body.classList.add('dark');

  var out = {};
  var fab = document.getElementById('scroll-bottom-fab');
  var nav = document.getElementById('msg-nav');

  // ---- ① 刻度跳转 ----
  refreshMsgNav();
  wrap.scrollTop = 0;
  var item3 = nav.querySelector('[data-nav-idx="3"]');
  out.hasItem3 = !!item3;
  out.navCount = nav.querySelectorAll('.msg-nav-item').length;
  if (item3) {
    var t = document.querySelectorAll('.session-messages .message.user')[3];
    var wrapTop = wrap.getBoundingClientRect().top;
    var elTop = t.getBoundingClientRect().top;
    var expect = wrap.scrollTop + (elTop - wrapTop) - 12;
    wrap.scrollTop = expect;   // smooth 的终值，headless 不推进动画帧
    out.afterJump3 = { expect: Math.round(expect), actual: Math.round(wrap.scrollTop) };
    out.jump3Offset = Math.round(t.getBoundingClientRect().top - wrap.getBoundingClientRect().top);
  }

  // ---- ② 浮标显隐 ----
  wrap.scrollTop = Math.round(wrap.scrollHeight * 0.5);
  refreshScrollFab();
  out.midShow = fab.classList.contains('show');
  wrap.scrollTop = wrap.scrollHeight;
  refreshScrollFab();
  out.bottomShow = fab.classList.contains('show');

  // ---- ③ 点浮标回底 ----
  wrap.scrollTop = Math.round(wrap.scrollHeight * 0.4);
  refreshScrollFab();
  fab.click();
  wrap.scrollTop = wrap.scrollHeight;
  refreshScrollFab();
  out.afterFabClick = {
    remain: Math.round(wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight),
    show: fab.classList.contains('show')
  };

  // ---- ④ busy 圈 ----
  wrap.scrollTop = Math.round(wrap.scrollHeight * 0.5);
  state.isStreaming = true;
  state.streamingSessionId = state.currentSessionId;
  refreshScrollFab();
  out.busyRunning = fab.classList.contains('busy');
  state.isStreaming = false;
  state.streamingSessionId = null;
  refreshScrollFab();
  out.busyAfterDone = fab.classList.contains('busy');

  // 无会话（欢迎页）时不应转圈
  var savedSid = state.currentSessionId;
  state.currentSessionId = null;
  state.subscribingSessionId = null;
  state.taskStatuses = {};
  refreshScrollFab();
  out.busyNoSession = fab.classList.contains('busy');
  state.currentSessionId = savedSid;

  // ---- ⑤ 面板展开 / 收起 ----
  // 收起态：窄轨，标题宽度为 0（不是 opacity:0 —— 那样仍占位）
  nav.classList.remove('expanded');
  out.stripWidth = Math.round(nav.getBoundingClientRect().width);
  var t0 = nav.querySelector('.msg-nav-title');
  out.stripTitleW = t0 ? Math.round(t0.getBoundingClientRect().width) : null;

  // 展开态：面板变宽、标题可见、内部可滚动
  nav.classList.add('expanded');
  out.panelWidth = Math.round(nav.getBoundingClientRect().width);
  out.panelTitleW = t0 ? Math.round(t0.getBoundingClientRect().width) : null;
  out.panelTitleOpacity = t0 ? getComputedStyle(t0).opacity : null;
  out.panelRowH = nav.querySelector('.msg-nav-item')
    ? Math.round(nav.querySelector('.msg-nav-item').getBoundingClientRect().height) : null;

  /* 面板高度上限分两种情况验，缺任一条都会漏掉一种观感崩坏：
       ① 条数不多（本例 14 条）→ 不该出滚动条。max-height 曾写 62%，
          那会让 14 条里一半要滚，失去"一眼看到总共问了什么"的意义。
       ② 条数很多（临时塞到 44 条）→ 必须内部滚动，且整体高度不能顶破
          视口。只验 ① 会得到"面板无限长、跑出屏幕"；只验 ② 会回到 62%。
     上限本身也验：曾写到 92%，14 条能铺开但上下只留 4%，几乎把正文
     整列盖死 —— 用户原话「弹窗不用那么长」。60% 上下各留 20%。 */
  out.panelShortScrollable = nav.scrollHeight > nav.clientHeight;
  out.panelShortH = Math.round(nav.getBoundingClientRect().height);

  var areaH = Math.round(document.getElementById('chat-area').getBoundingClientRect().height);
  out.panelShortRatio = areaH ? +(out.panelShortH / areaH).toFixed(3) : 1;
  // 上下余量（面板垂直居中，所以各占一半）
  var navRect0 = nav.getBoundingClientRect();
  out.panelGapTop = Math.round(navRect0.top);
  out.panelGapBottom = Math.round(areaH - navRect0.bottom);

  var extra = [], proto = nav.querySelector('.msg-nav-item');
  for (var k = 0; k < 30 && proto; k++) {
    var b = proto.cloneNode(true);
    b.removeAttribute('data-nav-idx');
    nav.appendChild(b); extra.push(b);
  }
  out.panelTallScrollable = nav.scrollHeight > nav.clientHeight;
  out.panelTallH = Math.round(nav.getBoundingClientRect().height);
  out.panelTallRatio = areaH ? out.panelTallH / areaH : 1;
  while (extra.length) { extra.pop().remove(); }

  /* ---- 位置 ----
     两类控件，两套基准，不能混：
       · 刻度面板是**导航浮层**，从窗口右边拉出来，贴 #chat-area 右缘；
       · 回底浮标是**输入区的附属**，必须落在输入卡的宽度范围内、贴着上沿。
     拐过一次弯：为了"两者共用一根右缘竖线"把浮标挂到 #chat-area 上，
     结果它落到 x=1270，而输入卡右缘在 1177 —— **整个飘在卡片外侧 93px
     的空白里**。垂直只差 14px 也救不回来，读起来就是一个跟输入框无关
     的按钮。所以下面这几条量的是各自真正的关系。 */
  var fab2 = document.getElementById('scroll-bottom-fab');
  fab2.classList.add('show');     // 让位规则只在"浮标本该出现"时才有意义
  var areaEl = document.getElementById('chat-area');
  var compEl = document.querySelector('.chat-composer');
  var cb = compEl.getBoundingClientRect();
  var fb = fab2.getBoundingClientRect();

  out.navRightGap = Math.round(areaEl.getBoundingClientRect().right - nav.getBoundingClientRect().right);

  // 浮标必须**在输入卡内**（水平方向），且贴着卡片上沿
  out.fabInsideComposer = fb.left >= cb.left - 0.5 && fb.right <= cb.right + 0.5;
  out.fabInsetFromCardRight = Math.round(cb.right - fb.right);
  out.fabAboveCard = Math.round(cb.top - fb.bottom);
  out.composerBox = [Math.round(cb.left), Math.round(cb.top), Math.round(cb.right)];
  out.fabBox = [Math.round(fb.left), Math.round(fb.top), Math.round(fb.right), Math.round(fb.bottom)];

  // 面板必须压住浮标（两者同一列），且浮标此刻应让位
  out.navZ = getComputedStyle(nav).zIndex;
  out.fabOpacityWhenPanelOpen = getComputedStyle(fab2).opacity;
  fab2.classList.remove('show');

  /* ---- ⑥ 刻度条是「11 行取景窗口」，不是「压扁的长条」----
     方案换过三版，这段断言按第三版写：
       第一版由内容撑高 → 14 条 350px、25 条 625px，长度不可控；
       第二版固定 200px 轨道 + flex:1 均分 → 40 条时行距 4.6px，
         横线糊成灰带，数不清也点不准；
       第三版（现在）：行高固定、只开 11 行窗口、超出部分靠 track 平移翻页。

     必须验的三件事：
       ① 高度不随条数变（44 条和 11 条一样高）；
       ② 窗口内**恰好** 11 行整行，不能露半行（多 1px 边框就会露）；
       ③ 条目数超过窗口时，位移按"行高 × 跳过行数"走，且首末行贴边。

     两个踩过的坑：
       · 量之前必须先把 nav 从 expanded 摘掉。面板态 height:auto，
         量出来永远是内容高度，会得出"封顶失败"的假结论。
       · 不能"增删后就地复原"——删掉的条目是后面的跳转与高亮断言的
         数据源。记下 innerHTML 整体还原。 */
  nav.classList.remove('expanded');
  var savedHtml = nav.innerHTML;

  /* 把刻度条改成 cnt 条，返回窗口几何。
     注意用 window.refreshMsgNav 让真实渲染函数重跑一次 —— 手工塞 DOM
     不会更新 --nav-slots，量到的还是上一版的窗口高度。 */
  out.windowAt = function (cnt) {
    // 直接改宿主消息数，再走真实渲染路径
    var keep = host.querySelectorAll('.message.user').length;
    if (cnt > keep) {
      var proto = host.querySelector('.message');
      for (var i = 0; i < cnt - keep; i++) {
        var u = document.createElement('div');
        u.className = 'message user';
        u.innerHTML = '<div class="message-body"><div class="answer-area"><p>补充问题 '
          + (keep + i) + '</p></div></div>';
        host.appendChild(u);
      }
    } else if (cnt < keep) {
      var us = host.querySelectorAll('.message.user');
      for (var j = cnt; j < keep; j++) us[j].remove();
    }
    delete nav.dataset.fp;          // 强制重建
    delete nav.dataset.activeIdx;
    refreshMsgNav();
    _syncNavWindow();

    var its = nav.querySelectorAll('.msg-nav-item');
    var nr = nav.getBoundingClientRect();
    var full = 0, half = 0;
    for (var k = 0; k < its.length; k++) {
      var r = its[k].getBoundingClientRect();
      var top = r.top - nr.top, bot = r.bottom - nr.top;
      if (top >= -0.5 && bot <= nr.height + 0.5) full++;
      else if (bot > 0.5 && top < nr.height - 0.5) half++;
    }
    return { h: Math.round(nr.height), full: full, half: half, cnt: its.length,
             slots: nav.style.getPropertyValue('--nav-slots'),
             shift: nav.style.getPropertyValue('--nav-shift') || '' };
  };

  out.w2 = out.windowAt(2);
  out.w11 = out.windowAt(11);
  out.w14 = out.windowAt(14);
  out.w44 = out.windowAt(44);

  /* ⚠️ 必须把**宿主消息数**也还原。windowAt 是往宿主里增删 .message.user
     再走真实渲染路径的；只还原 nav.innerHTML 会留下
     「宿主 44 条、刻度条 14 条」的不一致状态。
     还原方式用"整体重建"而不是按索引删：windowAt 只追加 .message.user
     （不带对应 .message.assistant），按 28 这个固定索引删会误删真正的助手消息，
     造成"提问位置间距异常"——表现就是某几档高亮被跳过（曾缺 idx10）。 */
  host.innerHTML = @@MSGS@@.join('');
  delete nav.dataset.fp;
  delete nav.dataset.activeIdx;
  refreshMsgNav();
  updateMsgNavActive();
  _syncNavWindow();

  // 行高：CSS 里的 --nav-row 必须与实测一致（JS 靠它算位移）
  var rowVar = parseFloat(getComputedStyle(nav).getPropertyValue('--nav-row'));
  out.navRowVar = rowVar;
  var firstItem = nav.querySelector('.msg-nav-item');
  out.navRowMeasured = firstItem ? Math.round(firstItem.getBoundingClientRect().height) : null;

  // 窗口内相邻两行的实际间距（= 行高，横线之间才均匀）
  var twoIt = nav.querySelectorAll('.msg-nav-item');
  out.navRowGap = twoIt.length > 1
    ? Math.round(twoIt[1].getBoundingClientRect().top - twoIt[0].getBoundingClientRect().top) : null;

  /* 翻页：44 条时滚到中段，位移必须是 -(行高 × 跳过行数)，
     且窗口内不能出现半行（那是位移量算错的唯一表现）。 */
  wrap.scrollTop = Math.round((wrap.scrollHeight - wrap.clientHeight) * 0.5);
  updateMsgNavActive();
  var actMid = Number(nav.dataset.activeIdx || 0);
  var nrMid = nav.getBoundingClientRect();
  var shiftMid = Math.abs(parseFloat(nav.style.getPropertyValue('--nav-shift')) || 0);
  out.paged = {
    active: actMid,
    shift: shiftMid,
    expectShift: Math.round(shiftMid / (rowVar || 1)),
    isMultipleOfRow: rowVar ? (shiftMid % rowVar === 0) : false
  };
  var halfMid = 0;
  var itsMid = nav.querySelectorAll('.msg-nav-item');
  for (var m = 0; m < itsMid.length; m++) {
    var rm = itsMid[m].getBoundingClientRect();
    var tm = rm.top - nrMid.top, bm = rm.bottom - nrMid.top;
    if ((bm > 0.5 && tm < nrMid.height - 0.5) && !(tm >= -0.5 && bm <= nrMid.height + 0.5)) halfMid++;
  }
  out.pagedHalfRows = halfMid;

  // 窗口内可见条目（完全落在窗口里的那些），供边缘 shortening 断言用
  function visibleIdx() {
    var out2 = [], nrr = nav.getBoundingClientRect();
    var all = nav.querySelectorAll('.msg-nav-item');
    for (var k = 0; k < all.length; k++) {
      var r = all[k].getBoundingClientRect();
      if (r.top - nrr.top >= -0.5 && r.bottom - nrr.top <= nrr.height + 0.5) {
        out2.push(Number(all[k].dataset.navIdx));
      }
    }
    return out2;
  }
  out.visibleMid = visibleIdx();

  /* 边缘缩短必须打在**窗口内**的首末行上。
     这里踩过真坑：最初 CSS 写的是 `.is-truncated-top .msg-nav-item:first-child`，
     而 :first-child 是**整个列表**的第一条 —— 窗口平移到中段后它早滚出
     可见区了，于是缩短效果一次都没出现过：class、位移、变量全对，
     只有视觉提示永远缺席。所以改由 JS 逐行打 is-edge-top/bottom，
     并且断言要看的是"可见行里的第一条"，不是 DOM 里的第一条。 */
  function dashOf(ix) {
    var b = nav.querySelector('.msg-nav-item[data-nav-idx="' + ix + '"]');
    if (!b) return null;
    var d = b.querySelector('.msg-nav-dash').getBoundingClientRect();
    var cs = getComputedStyle(b.querySelector('.msg-nav-dash'));
    return { w: +d.width.toFixed(1), h: +d.height.toFixed(1), op: +cs.opacity };
  }
  var vis = out.visibleMid;
  out.edge = {
    truncTop: nav.classList.contains('is-truncated-top'),
    truncBottom: nav.classList.contains('is-truncated-bottom'),
    topClass: nav.querySelector('.msg-nav-item.is-edge-top')
      ? Number(nav.querySelector('.msg-nav-item.is-edge-top').dataset.navIdx) : null,
    bottomClass: nav.querySelector('.msg-nav-item.is-edge-bottom')
      ? Number(nav.querySelector('.msg-nav-item.is-edge-bottom').dataset.navIdx) : null,
    wantTop: vis.length ? vis[0] : null,
    wantBottom: vis.length ? vis[vis.length - 1] : null,
    dashTop: vis.length ? dashOf(vis[0]) : null,
    dashBottom: vis.length ? dashOf(vis[vis.length - 1]) : null,
    dashInner: vis.length > 2 ? dashOf(vis[1]) : null
  };

  // 贴顶时首行不该缩短（上面没有更多了）；贴底时末行同理
  wrap.scrollTop = 0; updateMsgNavActive();
  var visTop = visibleIdx();
  out.atTop = { first: visTop[0], dash: visTop.length ? dashOf(visTop[0]) : null,
                truncTop: nav.classList.contains('is-truncated-top') };
  wrap.scrollTop = wrap.scrollHeight; updateMsgNavActive();
  var visBot = visibleIdx();
  out.atBottom = { last: visBot[visBot.length - 1],
                   dash: visBot.length ? dashOf(visBot[visBot.length - 1]) : null,
                   truncBottom: nav.classList.contains('is-truncated-bottom') };

  // 还原成原始的 14 条
  nav.innerHTML = savedHtml;
  delete nav.dataset.activeIdx;
  delete nav.dataset.fp;
  refreshMsgNav();
  _syncNavWindow();

  // 面板上下余量（垂直居中，上下各一半 —— 只要求至少一侧留出 15%）
  out.panelHasBreathing = (out.panelGapTop >= areaH * 0.15)
    || (out.panelGapBottom >= areaH * 0.15);

  // 点某一条应能跳转（面板展开状态下）
  var target4 = nav.querySelector('[data-nav-idx="4"]');
  out.hasTarget4 = !!target4;
  if (target4) {
    var m4 = document.querySelectorAll('.session-messages .message.user')[4];
    var wt = wrap.getBoundingClientRect().top;
    var et = m4.getBoundingClientRect().top;
    wrap.scrollTop = wrap.scrollTop + (et - wt) - 12;
    out.jump4Offset = Math.round(m4.getBoundingClientRect().top - wrap.getBoundingClientRect().top);
  }
  nav.classList.remove('expanded');

  /* ---- ⑥ 指针轨迹：路过不弹、停住才弹、弹出来要落在你指的那条 ----
     这一组是用户两次反馈后重写的，前一组断言的是**旧行为**（进即展开），
     而旧行为正是要改掉的：

       「鼠标移上去弹窗出来的很快」 —— pointerenter 零延迟展开，
       从正文横向扫到右侧、或去够滚动条时，面板会自己弹出来盖住正文。
       现在排队 160ms 确认意图：pointerenter 后 pointerleave 先到，
       就说明指针没停住，直接取消排队。

       「没有定位到刻度对应的对话」 —— 窄条是 11 行窗口（第 6~16 条），
       展开后是完整清单（40 条从第 1 条铺）。同一块屏幕位置，展开前指针下
       是第 11 条、展开后变成第 7 条，瞄准的那条跑掉了。

       ⚠️ 这条**第一次没修对、断言也没抓住**，记下来：
       第一次只调 scrollTop，判断标准写成"指针那条还**可见**"。
       实测 14 条时面板装得下（406 < 430），scrollTop 恒为 0，
       指针停在第 8 条、展开后指针下成了第 12 条（偏 115px）——
       但第 12 条也在面板里，"可见"照样成立，断言绿着，
       用户点下去却跳到错误的对话。
       **"可见"是错判据，必须是"指针下还是同一条"。**
       现在改成 weigh 指针下那条的 idx 前后一致 + 中心偏移 ≤ 20px。

     三段必须分开验，合成一条会互相掩盖：
       ① 路过（enter → leave）→ 220ms 后仍不展开
       ② 停留（enter → 等 220ms）→ 展开
       ③ 展开后指针底下那条 → 与展开前是同一条（且完整可见） */
  function move(x, y) {
    document.dispatchEvent(new MouseEvent('pointermove',
      { clientX: x, clientY: y, bubbles: true }));
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function underY(y) {
    var hit = null;
    nav.querySelectorAll('.msg-nav-item').forEach(function (b) {
      var q = b.getBoundingClientRect();
      if (q.top <= y && q.bottom > y) hit = Number(b.dataset.navIdx);
    });
    return hit;
  }
  function fullyVisible(b) {
    if (!b) return false;
    var r = b.getBoundingClientRect(), nn = nav.getBoundingClientRect();
    return r.top >= nn.top - 0.5 && r.bottom <= nn.bottom + 0.5;
  }

  // ① 路过不该展开
  var r0 = nav.getBoundingClientRect();
  nav.dispatchEvent(new MouseEvent('pointerenter',
    { clientX: r0.right - 7, clientY: r0.top + r0.height / 2, bubbles: false }));
  out.passByExpanded0 = nav.classList.contains('expanded');
  nav.dispatchEvent(new MouseEvent('pointerleave', {}));
  await sleep(220);
  out.passByExpanded220 = nav.classList.contains('expanded');

  /* ② 停留应展开；③ 展开定位。
     为了验定位，先把内容滚到中段（此时窗口显示第 6~16 条，
     展开后的完整清单有 40 条，两套坐标完全不同 —— 正是会错位的场景）。

     ⚠️ 之前必须把面板完全收起并把指针位置清空：上一步"路过"测试留了
     一个 pointerleave，若此时还残留旧的 _navLastPointerY，
     _openMsgNavPanel 会按旧坐标定位，得到"展开前 5 → 展开后 7"的假错位。 */
  nav.classList.remove('expanded');
  nav.scrollTop = 0;
  nav.dispatchEvent(new Event('pointerleave'));
  await sleep(240);
  wrap.scrollTop = Math.round((wrap.scrollHeight - wrap.clientHeight) * 0.5);
  updateMsgNavActive();
  _syncNavWindow();
  var r0b = nav.getBoundingClientRect();
  /* 展开前先发一次真实位置的 pointermove —— 这样 _navLastPointerY 一定是
     "本次要用的那个坐标"，不会沿用上一步路过测试留下的旧值。
     （它是模块内 let 变量，测试改不到；只能靠真实事件喂进去。） */
  nav.dispatchEvent(new MouseEvent('pointermove',
    { clientX: r0b.right - 7, clientY: r0b.top + r0b.height / 2, bubbles: true }));
  var hoverY = r0b.top + r0b.height / 2;
  out.openUnderBefore = underY(hoverY);
  nav.dispatchEvent(new MouseEvent('pointerenter',
    { clientX: r0b.right - 7, clientY: hoverY, bubbles: false }));
  out.stayExpanded0 = nav.classList.contains('expanded');
  await sleep(220);
  out.stayExpanded220 = nav.classList.contains('expanded');
  out.openUnderAfter = underY(hoverY);
  var elOpen = out.openUnderBefore != null
    ? nav.querySelector('.msg-nav-item[data-nav-idx="' + out.openUnderBefore + '"]')
    : null;
  out.openHoveredVisible = fullyVisible(elOpen);
  /* 核心判据：指针下必须还是**同一条**，且该行中心贴着指针高度。
     "可见"太宽松 —— 展开后指针下换成第 12 条，第 12 条也是可见的，
     但用户点下去跳到错的对话。 */
  out.openSameRow = (out.openUnderBefore != null)
    && (out.openUnderAfter === out.openUnderBefore);
  out.openDist = elOpen
    ? Math.round(elOpen.getBoundingClientRect().top
                 + elOpen.getBoundingClientRect().height / 2 - hoverY)
    : null;

  // 面板内部移动不闪退
  var r1 = nav.getBoundingClientRect();
  move(r1.left + 40, r1.top + 30);
  out.hoverInsideKeeps = nav.classList.contains('expanded');
  // 纵向滑出面板上下沿但在余量内（正在读被盖住的正文）→ 应保持
  move(r1.left + 40, r1.bottom + 60);
  out.hoverVerticalKeeps = nav.classList.contains('expanded');
  // 横向退到正文 → 宽限期内仍展开（不闪退）
  move(r1.left - 200, r1.bottom + 200);
  out.hoverLeaveGrace = nav.classList.contains('expanded');

  /* ---- 展开定位的精度：必须用"面板装不下"的场景验 ----
     14 条时 14×29=406 < 430，面板不滚动，定位无从谈起。
     这里临时把宿主撑到 40 条，此时面板必然要滚，才能验"滚到对的位置"。
     ⚠️ 必须放在上面三条 hover 断言**之后** —— 这一段会收起面板并改宿主体量，
     放前面会让 hover 断言读到"已收起"的假失败。 */
  nav.dispatchEvent(new Event('pointerleave'));
  await sleep(240);
  var hostU = host.querySelectorAll('.message.user');
  var keepCount = hostU.length;
  for (var e1 = 0; e1 < 26; e1++) {
    var nu = document.createElement('div');
    nu.className = 'message user';
    nu.innerHTML = '<div class="message-body"><div class="answer-area"><p>补充问题' + e1 + '</p></div></div>';
    host.appendChild(nu);
  }
  delete nav.dataset.fp; delete nav.dataset.activeIdx;
  refreshMsgNav();
  wrap.scrollTop = Math.round((wrap.scrollHeight - wrap.clientHeight) * 0.5);
  updateMsgNavActive(); _syncNavWindow();

  var nrL = nav.getBoundingClientRect();
  var yL = nrL.top + nrL.height / 2;
  nav.dispatchEvent(new MouseEvent('pointermove', { clientX: nrL.right - 7, clientY: yL, bubbles: true }));
  var underL = underY(yL);
  nav.dispatchEvent(new MouseEvent('pointerenter', { clientX: nrL.right - 7, clientY: yL }));
  await sleep(240);
  var elL = underL != null ? nav.querySelector('.msg-nav-item[data-nav-idx="' + underL + '"]') : null;
  if (elL) {
    var rr = elL.getBoundingClientRect();
    // 该行中心离指针高度多远（0 = 正好在指针下）
    out.locateWithScroll = {
      idx: underL,
      distToPointer: Math.round(rr.top + rr.height / 2 - yL),
      navScrollTop: Math.round(nav.scrollTop),
      scrollable: nav.scrollHeight > nav.clientHeight + 1
    };
  } else {
    out.locateWithScroll = null;
  }

  /* 让位后的面板不能跑出聊天区。
     计算里有两次夹取（scrollTop 一个、panel shift 一个），曾经各自合法、
     合起来却让面板滑出上沿。这里直接量最终几何，不信算出来的值。 */
  var navFin = nav.getBoundingClientRect();
  var areaFin = document.getElementById('chat-area').getBoundingClientRect();
  out.panelWithinArea = navFin.top >= areaFin.top - 0.5
    && navFin.bottom <= areaFin.bottom + 0.5;
  out.panelShift = Math.round(
    parseFloat(nav.style.getPropertyValue('--nav-panel-shift')) || 0);
  nav.dispatchEvent(new Event('pointerleave'));
  await sleep(240);
  // 还原宿主条数
  var hu2 = host.querySelectorAll('.message.user');
  for (var e2 = keepCount; e2 < hu2.length; e2++) hu2[e2].remove();
  delete nav.dataset.fp; delete nav.dataset.activeIdx;
  refreshMsgNav(); updateMsgNavActive(); _syncNavWindow();

  /* ④ 面板里滚过一段后收起：scrollTop 必须归零。
     overflow:hidden 不重置滚动位置，残留的 scrollTop 会和 track 的
     transform 叠加 —— 位移算的是第 6 行开头、实际看到第 13 行。
     这是用户说的「翻页也有些问题」的根因。 */
  nav.scrollTop = Math.round(nav.scrollHeight * 0.6);
  out.panelScrolledBefore = Math.round(nav.scrollTop);
  nav.dispatchEvent(new Event('pointerleave'));
  await sleep(260);
  out.afterCollapseExpanded = nav.classList.contains('expanded');
  out.afterCollapseScrollTop = Math.round(nav.scrollTop);
  // 收起后：位移算出的首行必须等于实际可见首行（两者不一致 = 位移被叠加了）
  var rowPx2 = parseFloat(getComputedStyle(nav).getPropertyValue('--nav-row')) || 48;
  var shiftPx = Math.abs(parseFloat(nav.style.getPropertyValue('--nav-shift')) || 0);
  out.afterCollapseFirstFromShift = Math.round(shiftPx / rowPx2);
  var visAfter = visibleIdx();
  out.afterCollapseFirstVisible = visAfter.length ? visAfter[0] : null;

  // ---- 当前高亮不永远是第 0 条 ----
  wrap.scrollTop = Math.round(wrap.scrollHeight * 0.6);
  updateMsgNavActive();
  var act = nav.querySelector('.msg-nav-item.active');
  out.activeIdx = act ? Number(act.dataset.navIdx) : null;

  /* ---- ⑦ 高亮必须覆盖到最后一条（曾是 bug：滚到底停在第 11 / 共 14）----
     实测根因不是阈值，而是几何不可能：最大滚动 1838px，而第 12、13 条
     提问的绝对位置是 1968 / 2129 —— 它们根本滚不到顶部，所以"顶部越过
     上沿"这个条件对它们永远不成立。

     ⚠️ 采样步长必须细。用 20 格粗采样时 95%→100% 从 11 直接跳到 13，
     看起来像"跳过了 12"，但 4px 细采样证明 12 在 1764 处确实出现过。
     粗采样会把"尾段切换快"误判成"漏档"，也会把真漏档藏起来。

     ⚠️ 分母必须是**刻度条上实际画出的条数**，不是消息总数。
     上一段把消息临时增到 44 条，而 MSG_NAV_MAX=40 只画最后 40 条
     （索引 4..43），最早的 4 条根本没有对应的刻度可点。
     拿 44 当分母会得到"覆盖 40/44 档"的假红灯 —— 那是断言写错了，
     不是实现漏档。 */
  /* 采样前把面板彻底收起、滚动归零。
     若此时还残留 .expanded，面板高度是 430、行高 29，_syncNavWindow 会早退，
     而 updateMsgNavActive 仍按静止态几何算 —— 两者不一致会让某几档被跳过。
     这也是"覆盖 13/14、缺 idx10"的直接原因。 */
  nav.classList.remove('expanded');
  nav.scrollTop = 0;
  nav.dispatchEvent(new Event('pointerleave'));
  await sleep(240);
  _syncNavWindow();

  var totScroll = wrap.scrollHeight - wrap.clientHeight;
  var seen = {}, order = [];
  /* 分母改成"刻度条上真实存在的 data-nav-idx 集合"，不再用条数。
     条数会在采样过程中漂（窗口重建、MSG_NAV_MAX 截断都会改变它），
     而"哪些 idx 存在"是采样期间不该变的量 —— 两边对比才是同口径。
     这也是前面踩过的坑：44 条消息只画最后 40 条（索引 4..43），
     拿消息总数 44 当分母必然得到假红灯。 */
  var idxSet = {};
  nav.querySelectorAll('.msg-nav-item').forEach(function (b) {
    idxSet[Number(b.dataset.navIdx)] = true;
  });
  var idxList = Object.keys(idxSet).map(Number);
  for (var st = 0; st <= totScroll; st += 4) {
    wrap.scrollTop = st;
    updateMsgNavActive();
    var a2 = nav.querySelector('.msg-nav-item.active');
    var ix = a2 ? Number(a2.dataset.navIdx) : null;
    if (seen[ix] === undefined) { seen[ix] = st; order.push(ix); }
  }
  wrap.scrollTop = totScroll;
  updateMsgNavActive();
  var lastA = nav.querySelector('.msg-nav-item.active');
  out.highlightFinalIdx = lastA ? Number(lastA.dataset.navIdx) : null;
  out.highlightLastItem = idxList.length ? Math.max.apply(null, idxList) : null;
  // 采样中必须每个存在的 idx 都点亮过至少一次
  var missingIdx = idxList.filter(function (k) { return seen[k] === undefined; });
  out.highlightCoversAll = missingIdx.length === 0;
  out.highlightMissing = missingIdx;
  out.highlightCovered = Object.keys(seen).length;
  out.highlightTotal = idxList.length;
  // 高亮必须单调递增，不能来回跳
  var mono = true;
  for (var q = 1; q < order.length; q++) { if (order[q] <= order[q - 1]) mono = false; }
  out.highlightMonotonic = mono;

  // 收起有 160ms 宽限，必须等它过去才能读到终态
  nav.dispatchEvent(new Event('pointerleave'));
  await sleep(260);
  out.hoverLeaveCollapsed = !nav.classList.contains('expanded');

  var pre = document.createElement('pre'); pre.id = 'probe';
  pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})(); });
</script>
"""


def run():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', f'href="file://{ROOT}/static/')
    html = html.replace('src="/static/', f'src="file://{ROOT}/static/')
    # 无头里过渡可能冻在中间帧，读到中间值会误判。所以默认屏蔽。
    # ⚠️ 但「展开定位」那几条**必须开着过渡测** —— 见 run2()：
    #    真实浏览器里 .expanded 是带着过渡生效的，屏蔽掉就等于测了另一套几何。
    html = html.replace("</head>",
                        "<style>*{transition:none!important;animation:none!important}</style></head>", 1)
    html = html.replace("</body>", PROBE_JS.replace("@@MSGS@@", build_messages()) + "</body>", 1)

    tmp = Path(tempfile.mkdtemp()) / "nav.html"
    tmp.write_text(html, encoding="utf-8")
    with tempfile.TemporaryDirectory() as profile:
        base = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                f"--user-data-dir={profile}", "--window-size=1280,860",
                "--allow-file-access-from-files", "--virtual-time-budget=8000",
                "--run-all-compositor-stages-before-draw", "--force-device-scale-factor=1"]
        r = subprocess.run(base + ["--dump-dom", tmp.as_uri()],
                           capture_output=True, text=True, timeout=120)

    dom = r.stdout
    i = dom.find('<pre id="probe">')
    if i < 0:
        print("探针未执行（页面脚本报错？）")
        return 1, {}
    s = dom.find(">", i) + 1
    e = dom.find("</pre>", s)
    d = json.loads(dom[s:e].replace("&quot;", '"').replace("&amp;", "&"))

    checks = [
        ("刻度条渲染出全部提问", d.get("navCount") == len(QS), f"实际 {d.get('navCount')} 条"),
        ("点第 3 条刻度跳到对应提问", abs(d.get("jump3Offset", 999)) <= 40,
         f"落点偏移 {d.get('jump3Offset')}px"),
        ("滚到中段浮标出现", d.get("midShow") is True, ""),
        ("滚到底浮标消失", d.get("bottomShow") is False, ""),
        ("点浮标回到最底", d.get("afterFabClick", {}).get("remain", 999) <= 2,
         f"剩余 {d.get('afterFabClick', {}).get('remain')}px"),
        ("回到底后浮标收起", d.get("afterFabClick", {}).get("show") is False, ""),
        ("任务运行中浮标带 loading 圈", d.get("busyRunning") is True, ""),
        ("任务结束后 loading 圈摘掉", d.get("busyAfterDone") is False, ""),
        ("无会话时不转圈（null===null 边界）", d.get("busyNoSession") is False, ""),
        ("高亮跟随阅读位置而非恒为第 0 条", d.get("activeIdx") not in (None, 0),
         f"当前高亮第 {d.get('activeIdx')} 条"),
        ("收起态是窄轨（不占正文宽度）", d.get("stripWidth", 999) <= 40,
         f"宽 {d.get('stripWidth')}px"),
        ("收起态标题宽度为 0（不占位）", d.get("stripTitleW") == 0,
         f"宽 {d.get('stripTitleW')}px"),
        ("展开成面板", d.get("panelWidth", 0) >= 250, f"宽 {d.get('panelWidth')}px"),
        ("展开后标题可见", d.get("panelTitleOpacity") == "1"
         and (d.get("panelTitleW") or 0) > 40, f"宽 {d.get('panelTitleW')}"),
        ("展开后行高足够点击", (d.get("panelRowH") or 0) >= 28, f"行高 {d.get('panelRowH')}px"),
        ("条数不多时面板不滚（一眼看全）", d.get("panelShortScrollable") is False,
         f"高 {d.get('panelShortH')}px 却需要滚动"),
        ("条数很多时面板内部可滚动", d.get("panelTallScrollable") is True,
         f"高 {d.get('panelTallH')}px 仍不滚"),
        ("面板高度不超过聊天区 95%（不跑出屏幕）",
         (d.get("panelTallRatio") or 1) <= 0.95, f"占比 {d.get('panelTallRatio')}"),
        ("面板不占满整屏（≤62%，用户「弹窗不用那么长」）",
         (d.get("panelShortRatio") or 1) <= 0.62, f"占比 {d.get('panelShortRatio')}"),
        ("面板上下留出余量（至少一侧 ≥15%）", d.get("panelHasBreathing") is True,
         f"上 {d.get('panelGapTop')} / 下 {d.get('panelGapBottom')}"),
        ("刻度条随条数变长（2 条时不留大片空白）",
         (d.get("w14") or {}).get("h", 0) > (d.get("w2") or {}).get("h", 0),
         f"2 条 {(d.get('w2') or {}).get('h')}px / 14 条 {(d.get('w14') or {}).get('h')}px"),
        ("刻度条长度封顶（44 条与 11 条同高，用户「长度缩小」）",
         (d.get("w44") or {}).get("h") == (d.get("w11") or {}).get("h"),
         f"11 条 {(d.get('w11') or {}).get('h')}px / 44 条 {(d.get('w44') or {}).get('h')}px"),
        ("窗口恰好装 11 行（不多不少，用户「九条完整+上下各一条」）",
         (d.get("w44") or {}).get("full") == 11,
         f"装满 {(d.get('w44') or {}).get('full')} 行"),
        ("窗口内不露半行（多 1px 边框就会露）",
         (d.get("w44") or {}).get("half") == 0,
         f"半行 {(d.get('w44') or {}).get('half')} 个"),
        ("行高是 CSS 变量而非 JS 常量（两边不漂）",
         abs((d.get("navRowVar") or 0) - (d.get("navRowMeasured") or 0)) <= 0.5,
         f"变量 {d.get('navRowVar')} / 实测 {d.get('navRowMeasured')}"),
        ("行距等于行高（横线均匀，用户「间距大一些」）",
         d.get("navRowGap") == d.get("navRowMeasured"), 
         f"间距 {d.get('navRowGap')} / 行高 {d.get('navRowMeasured')}"),
        # 原先这条写 `>= 40`，行距正好 40 时卡在边界上：**把行距改回 48 也照样绿**，
        # 等于只验了"没变短"，没验"比例对不对"。真正决定观感的是横线长与
        # 相邻间距的**密度比**（刻度是等距排列的，跟视口宽、正文行距都无关）：
        # DeepSeek 参考图 11.5/39.5 = 0.291。只缩横线不动行距会掉到 0.208，
        # 整列退化成稀疏点阵 —— 那正是用户说"刻度大小还有问题"的第二次返工。
        # 区间取 [0.26, 0.325] 而不是更宽的 ±15%：**旧版 16/48 = 0.333 必须红**。
        # 写过 [0.25, 0.34] 试过，旧值 0.333 落在里面 —— 回退实现也不会红，
        # 那条断言就白写了。容差按 DPR 取整的最坏组合收敛：
        # 横线 11~13、行距 39~41 → 0.268~0.333，仍以 0.325 为界，越界即真异常。
        ("横线/行距的密度比贴近参考图（0.291，两者必须联动改）",
         (lambda r: r is not None and 0.26 <= r <= 0.325)(
             (lambda w, g: (w / g) if (w and g) else None)(
                 ((d.get("edge") or {}).get("dashInner") or {}).get("w"),
                 d.get("navRowMeasured"))),
         f"实测 {((d.get('edge') or {}).get('dashInner') or {}).get('w')}"
         f"/{d.get('navRowMeasured')} = "
         f"{(lambda w, g: round(w / g, 3) if (w and g) else '?')(((d.get('edge') or {}).get('dashInner') or {}).get('w'), d.get('navRowMeasured'))}"
         f"（参考 0.291）"),
        ("翻页位移是行高的整数倍（不会错位露半行）",
         (d.get("paged") or {}).get("isMultipleOfRow") is True,
         f"位移 {(d.get('paged') or {}).get('shift')}px / 行高 {d.get('navRowVar')}"),
        ("翻页后窗口内仍无半行",
         d.get("pagedHalfRows") == 0, f"半行 {d.get('pagedHalfRows')} 个"),
        ("边缘缩短打在窗口内首行上（不是 DOM 首行）",
         (d.get("edge") or {}).get("truncTop") is False
         or (d.get("edge") or {}).get("topClass") == (d.get("edge") or {}).get("wantTop"),
         f"标记在 {((d.get('edge') or {}).get('topClass'))}，可见首行是 {((d.get('edge') or {}).get('wantTop'))}，贴顶={((d.get('edge') or {}).get('truncTop'))}"),
        ("边缘缩短打在窗口内末行上",
         (d.get("edge") or {}).get("truncBottom") is False
         or (d.get("edge") or {}).get("bottomClass") == (d.get("edge") or {}).get("wantBottom"),
         f"标记在 {((d.get('edge') or {}).get('bottomClass'))}，可见末行是 {((d.get('edge') or {}).get('wantBottom'))}，贴底={((d.get('edge') or {}).get('truncBottom'))}"),
        ("窗口首末行确实更短更淡（仅窗口两侧都有更多时）",
         (d.get("edge") or {}).get("truncTop") is False
         or (d.get("edge") or {}).get("truncBottom") is False
         or ((((d.get("edge") or {}).get("dashTop") or {}).get("w", 99) < ((d.get("edge") or {}).get("dashInner") or {}).get("w", 0))
             and (((d.get("edge") or {}).get("dashBottom") or {}).get("w", 99) < ((d.get("edge") or {}).get("dashInner") or {}).get("w", 0))),
         f"首 {((d.get('edge') or {}).get('dashTop') or {}).get('w')} / 内 {((d.get('edge') or {}).get('dashInner') or {}).get('w')} / 末 {((d.get('edge') or {}).get('dashBottom') or {}).get('w')}"),
        # ⚠️ 这两条原先写 `>= 16`，而恰好那一行是 active（17px 宽），
        # 于是"未缩短"和"因为高亮而变宽"分不清 —— 断言永远成立、什么也没验。
        # 现在拿**非高亮普通行**的宽度做基准（dashInner 是窗口内第二条）。
        ("贴顶时首行不缩短（上面没有更多了）",
         ((d.get("atTop") or {}).get("dash") or {}).get("w", 0)
         >= ((d.get("edge") or {}).get("dashInner") or {}).get("w", 99),
         f"首行宽 {((d.get('atTop') or {}).get('dash') or {}).get('w')}"
         f" / 普通行 {((d.get('edge') or {}).get('dashInner') or {}).get('w')}"),
        ("贴底时末行不缩短（下面没有更多了）",
         ((d.get("atBottom") or {}).get("dash") or {}).get("w", 0)
         >= ((d.get("edge") or {}).get("dashInner") or {}).get("w", 99),
         f"末行宽 {((d.get('atBottom') or {}).get('dash') or {}).get('w')}"
         f" / 普通行 {((d.get('edge') or {}).get('dashInner') or {}).get('w')}"),
        # 尺寸对齐 DeepSeek 截图实测（普通 12×3、高亮 17×4）。
        # 用区间而不是等值：DPR 取整会让 12 变成 11 或 13。
        ("普通横线尺寸对齐参考图（12×3，不再是 16×4）",
         (lambda w: w is not None and 10 <= w <= 14)(
             ((d.get("edge") or {}).get("dashInner") or {}).get("w"))
         and (lambda h: h is not None and 2 <= h <= 4)(
             ((d.get("edge") or {}).get("dashInner") or {}).get("h")),
         f"实测 {((d.get('edge') or {}).get('dashInner') or {}).get('w')}"
         f"×{((d.get('edge') or {}).get('dashInner') or {}).get('h')}"),
        ("高亮横线尺寸对齐参考图（17×4，不再是 22×6）",
         (lambda w: w is not None and 15 <= w <= 19)(
             ((d.get("atTop") or {}).get("dash") or {}).get("w"))
         and (lambda h: h is not None and 3 <= h <= 5)(
             ((d.get("atTop") or {}).get("dash") or {}).get("h")),
         f"实测 {((d.get('atTop') or {}).get('dash') or {}).get('w')}"
         f"×{((d.get('atTop') or {}).get('dash') or {}).get('h')}"),
        ("高亮覆盖到最后一条提问（曾永远停在倒数第 3 条）",
         d.get("highlightFinalIdx") == d.get("highlightLastItem"),
         f"停在 {d.get('highlightFinalIdx')}，最后一条是 {d.get('highlightLastItem')}"),
        ("滚动过程中每一档高亮都出现过（无跳档）",
         d.get("highlightCoversAll") is True,
         f"覆盖 {d.get('highlightCovered')}/{d.get('highlightTotal')} 档"),
        ("高亮随滚动单调前进（不来回跳）", d.get("highlightMonotonic") is True, ""),
        ("面板层级高于浮标", int(d.get("navZ") or 0) >= 40, f"z={d.get('navZ')}"),
        ("面板展开时浮标让位（不压在面板上）",
         float(d.get("fabOpacityWhenPanelOpen") or 1) < 0.1,
         f"opacity={d.get('fabOpacityWhenPanelOpen')}"),
        ("面板贴聊天区右边缘（≤16px，不往里收）",
         (d.get("navRightGap") if d.get("navRightGap") is not None else 999) <= 16,
         f"右边距 {d.get('navRightGap')}px"),
        ("浮标落在输入卡宽度范围内（不飘到卡片外）",
         d.get("fabInsideComposer") is True,
         f"卡片右缘 {d.get('composerBox')} / 浮标 {d.get('fabBox')}"),
        ("浮标贴着输入卡右上角（内缩 0~16px）",
         d.get("fabInsetFromCardRight") is not None
         and 0 <= d.get("fabInsetFromCardRight") <= 16,
         f"距卡片右缘 {d.get('fabInsetFromCardRight')}px"),
        ("浮标浮在输入卡上沿（间距 0~16px，不悬空）",
         d.get("fabAboveCard") is not None and 0 <= d.get("fabAboveCard") <= 16,
         f"间距 {d.get('fabAboveCard')}px"),
        ("面板展开时点条目仍能跳转", abs(d.get("jump4Offset", 999)) <= 40,
         f"偏离 {d.get('jump4Offset')}px"),
        # ---- 展开时机与定位（用户两次反馈后新增）----
        ("路过窄条不弹面板（用户「弹窗出来的很快」）",
         d.get("passByExpanded0") is False and d.get("passByExpanded220") is False,
         f"0ms {d.get('passByExpanded0')} / 220ms {d.get('passByExpanded220')}"),
        ("进窄条瞬间不弹（先排队确认意图）", d.get("stayExpanded0") is False, ""),
        ("指针停住后展开", d.get("stayExpanded220") is True, ""),
        ("展开后指针指的那条完整可见（不被面板边缘切掉）",
         d.get("openHoveredVisible") is True, ""),
        # 核心：指针下必须还是同一条。旧的"可见"判据太宽松 ——
        # 实测偏移 115px（指针下换成了第 12 条）时仍然通过。
        ("展开后指针下仍是同一条（瞄准的没跑掉）",
         d.get("openSameRow") is True,
         f"展开前 {d.get('openUnderBefore')} → 展开后 {d.get('openUnderAfter')}"),
        ("展开后该行贴着指针高度（偏移 ≤20px）",
         d.get("openDist") is not None and abs(d.get("openDist")) <= 20,
         f"偏移 {d.get('openDist')}px"),
        ("面板需要滚动时，指针指的那条仍留在指针下",
         d.get("locateWithScroll") is not None
         and abs((d.get("locateWithScroll") or {}).get("distToPointer", 999)) <= 30,
         f"偏差 {((d.get('locateWithScroll') or {}).get('distToPointer'))}px"),
        # 面板为对齐而纵向让位时不能滑出聊天区。
        # 曾两次夹取各自合法、合起来把面板推出上沿。
        ("面板让位后仍完整落在聊天区内",
         d.get("panelWithinArea") is True,
         f"纵向位移 {d.get('panelShift')}px"),
        # ---- 收起后翻页不被残留 scrollTop 叠加 ----
        ("面板里滚过后收起，scrollTop 归零",
         d.get("afterCollapseScrollTop") == 0,
         f"残留 {d.get('afterCollapseScrollTop')}px"),
        ("收起后位移算出的首行 == 实际可见首行（翻页不错位）",
         d.get("afterCollapseFirstFromShift") == d.get("afterCollapseFirstVisible"),
         f"位移算 {d.get('afterCollapseFirstFromShift')} / 实际 {d.get('afterCollapseFirstVisible')}"),
        ("面板内部移动不闪退", d.get("hoverInsideKeeps") is True, ""),
        ("纵向滑出面板仍保持展开（读被盖住的正文）",
         d.get("hoverVerticalKeeps") is True, ""),
        ("横向退到正文时宽限期内不闪退", d.get("hoverLeaveGrace") is True, ""),
        ("宽限期过后才收起", d.get("hoverLeaveCollapsed") is True, ""),
    ]
    fails = 0
    for name, ok, extra in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({extra})" if extra and not ok else ""))
        if not ok:
            fails += 1
    print()
    print(f"{len(checks) - fails}/{len(checks)} 通过")
    return (0 if fails == 0 else 1), d


LIVE_PROBE_JS = r"""
<script>
document.addEventListener('DOMContentLoaded', function () { (async function () {
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  var out = {};
  var wrap = document.getElementById('chat-messages');
  var host = document.createElement('div');
  host.className = 'session-messages';
  host.innerHTML = @@MSGS@@.join('');
  var old = wrap.querySelector('.welcome-message'); if (old) old.remove();
  wrap.appendChild(host);
  state.currentSessionId = 'sess-live';
  state.sessionContainers['sess-live'] = { element: host, stream: null };
  refreshMsgNav();
  wrap.scrollTop = Math.round((wrap.scrollHeight - wrap.clientHeight) * 0.45);
  updateMsgNavActive();
  await sleep(150);
  document.documentElement.classList.remove('dark');
  document.body.classList.remove('dark');
  await sleep(80);

  var nav = document.getElementById('msg-nav');
  var rowPx = parseFloat(getComputedStyle(nav).getPropertyValue('--nav-row')) || 40;

  function underY(y) {
    var r = null;
    nav.querySelectorAll('.msg-nav-item').forEach(function (b) {
      var br = b.getBoundingClientRect();
      if (br.top <= y && br.bottom > y) r = Number(b.dataset.navIdx);
    });
    return r;
  }
  function reset() {
    nav.classList.remove('expanded');
    nav.style.setProperty('--nav-panel-shift', '0px');
    nav.scrollTop = 0;
    _syncNavWindow();
  }

  /* ---- ① 过渡开着时，7 个指针位置都必须仍指向同一条 ----
     这一条是之前所有测试的**盲区**：run() 里屏蔽了 transition，
     几何瞬间到位，所以「联立求解」看起来 35/35 全对。
     真实浏览器里 .expanded 带 0.2s 过渡，JS 在加类的**同一帧**量 rect，
     量到的是过渡起点 —— 算出来的位移全错。
     实测（未修时）：7 个位置只对 1 个，偏差 17~110px。 */
  var frs = [0.02, 0.10, 0.30, 0.50, 0.70, 0.90, 0.98];
  var same = 0, worst = 0, rows = [];
  for (var k = 0; k < frs.length; k++) {
    reset(); await sleep(320);              // 等收起过渡走完
    var r0 = nav.getBoundingClientRect();
    var X = r0.right - 7, Y = r0.top + r0.height * frs[k];
    var before = underY(Y);
    nav.dispatchEvent(new PointerEvent('pointerenter', { clientX: X, clientY: Y }));
    document.dispatchEvent(new PointerEvent('pointermove',
      { clientX: X, clientY: Y, bubbles: true }));
    await sleep(360);                        // 等展开过渡走完 = 用户看到的时刻
    var after = underY(Y);
    var ok = before != null && after === before;
    if (ok) same++;
    var el = before != null
      ? nav.querySelector('.msg-nav-item[data-nav-idx="' + before + '"]') : null;
    var dist = null;
    if (el) {
      var er = el.getBoundingClientRect();
      dist = Math.round(er.top + er.height / 2 - Y);
      if (Math.abs(dist) > worst) worst = Math.abs(dist);
    }
    rows.push({ frac: frs[k], before: before, after: after,
                same: ok, dist: dist });
  }
  out.liveSameRow = same;
  out.liveTotal = frs.length;
  out.liveWorstDist = worst;
  out.liveRows = rows;

  /* ---- ② 指针静止不动 4 秒，不能反复展开/收起 ----
     这是用户报的「鼠标移至上方的刻度，会触发弹窗频繁出现和消失」。
     根因：窄条 440px 高、面板 430px 高，都垂直居中 —— **面板更矮，
     上沿下移**，把没动过的指针甩到外面；浏览器对静止指针重新命中测试，
     补发 pointerleave → 收起 → 指针又落回窄条 → enter → 展开 → …
     实测 4 秒内 12 个完整循环。

     模拟要点：之后**不再发 pointermove**（鼠标没动就没有 move），
     只按 elementFromPoint 的结果补 enter/leave —— 与浏览器一致。 */
  reset(); await sleep(320);
  var r1 = nav.getBoundingClientRect();
  var PX = r1.right - 7, PY = r1.top + r1.height * 0.02;   // 最容易出事的上沿
  nav.dispatchEvent(new PointerEvent('pointerenter', { clientX: PX, clientY: PY }));
  document.dispatchEvent(new PointerEvent('pointermove',
    { clientX: PX, clientY: PY, bubbles: true }));

  var flips = 0, expandedTimes = 0, wasOver = true;
  var lastExp = nav.classList.contains('expanded');
  var t0 = performance.now();
  while (performance.now() - t0 < 4000) {
    await sleep(20);
    var hit = document.elementFromPoint(PX, PY);
    var over = !!(hit && (hit === nav || nav.contains(hit)));
    if (!over && wasOver) {
      nav.dispatchEvent(new PointerEvent('pointerleave',
        { clientX: PX, clientY: PY }));
    } else if (over && !wasOver) {
      nav.dispatchEvent(new PointerEvent('pointerenter',
        { clientX: PX, clientY: PY }));
    }
    wasOver = over;
    var exp = nav.classList.contains('expanded');
    if (exp !== lastExp) { flips++; lastExp = exp; if (exp) expandedTimes++; }
  }
  out.stillFlips = flips;
  out.stillCycles = Math.max(0, expandedTimes - 1);
  out.stillExpandedAtEnd = nav.classList.contains('expanded');

  /* ---- ③ 真的走开仍然要收起（不能把功能修死）---- */
  reset(); await sleep(320);
  var r2 = nav.getBoundingClientRect();
  var Lx = r2.right - 7, Ly = r2.top + r2.height * 0.5;
  nav.dispatchEvent(new PointerEvent('pointerenter', { clientX: Lx, clientY: Ly }));
  await sleep(320);
  out.realEnterExpanded = nav.classList.contains('expanded');
  nav.dispatchEvent(new PointerEvent('pointerleave', { clientX: Lx, clientY: Ly }));
  nav.dispatchEvent(new PointerEvent('pointerleave',
    { clientX: Lx - 220, clientY: Ly - 220 }));
  await sleep(320);
  out.realLeaveCollapsed = !nav.classList.contains('expanded');

  var p = document.createElement('pre'); p.id = 'probe';
  p.textContent = JSON.stringify(out);
  document.body.appendChild(p);
})(); });
</script>
"""


def run_live():
    """**开启 CSS 过渡**跑一遍关键交互。

    为什么必须单独一套：run() 为了读稳定值屏蔽了 transition，而真实浏览器
    里 .expanded 是带过渡生效的。屏蔽掉就变成了"测另一套几何"——
    展开定位那几条断言曾因此长期假绿（实测真实环境 7 个位置只对 1 个）。
    """
    print("\n[实时态：CSS 过渡开启，模拟真实浏览器]")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', f'href="file://{ROOT}/static/')
    html = html.replace('src="/static/', f'src="file://{ROOT}/static/')
    html = html.replace("</body>",
                        LIVE_PROBE_JS.replace("@@MSGS@@", build_messages()) + "</body>", 1)
    tmp = Path(tempfile.mkdtemp()) / "nav_live.html"
    tmp.write_text(html, encoding="utf-8")

    with tempfile.TemporaryDirectory() as profile:
        base = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                "--hide-scrollbars", f"--user-data-dir={profile}",
                "--window-size=1280,860", "--allow-file-access-from-files",
                "--virtual-time-budget=40000",
                "--run-all-compositor-stages-before-draw",
                "--force-device-scale-factor=1"]
        r = subprocess.run(base + ["--dump-dom", tmp.as_uri()],
                           capture_output=True, text=True, timeout=200)

    dom = r.stdout
    i = dom.find('<pre id="probe">')
    if i < 0:
        print("  ✗ 探针未输出（页面脚本异常）")
        return 1, {}
    s = dom.find(">", i) + 1
    e = dom.find("</pre>", s)
    d = json.loads(dom[s:e].replace("&quot;", '"').replace("&amp;", "&"))

    checks = [
        ("过渡开启时，7 个指针位置都仍指向同一条（曾是 1/7）",
         d.get("liveSameRow") == d.get("liveTotal"),
         f"{d.get('liveSameRow')}/{d.get('liveTotal')} 对准"),
        ("过渡开启时，目标行仍贴着指针高度（≤20px）",
         (d.get("liveWorstDist") or 999) <= 20,
         f"最大偏移 {d.get('liveWorstDist')}px"),
        ("指针静止 4 秒不反复展开收起（用户「频繁出现和消失」，曾是 12 轮）",
         d.get("stillCycles") == 0,
         f"{d.get('stillCycles')} 轮循环 / {d.get('stillFlips')} 次翻转"),
        ("指针静止期间保持展开",
         d.get("stillExpandedAtEnd") is True, ""),
        ("真走开后仍然收起（不能把功能修死）",
         d.get("realLeaveCollapsed") is True, ""),
        ("真的进入后确实展开",
         d.get("realEnterExpanded") is True, ""),
    ]
    fails = 0
    for name, ok, extra in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({extra})" if extra and not ok else ""))
        if not ok:
            fails += 1
    print()
    print(f"{len(checks) - fails}/{len(checks)} 通过")
    return (0 if fails == 0 else 1), d


if __name__ == "__main__":
    if not Path(CHROME).exists():
        print("跳过：未找到 Chrome")
        sys.exit(0)
    code, data = run()
    print()
    code2, data2 = run_live()
    if "--verbose" in sys.argv:
        print(json.dumps({**data, **data2}, ensure_ascii=False, indent=1))
    sys.exit(code or code2)
