/* 14-msg-nav.js
 * 消息导航：右侧问题刻度条 + 回到底部浮标
 *
 * 解决的问题：一段对话滚到中段之后，两个问题同时成立 ——
 *   ① 右侧一列「我问过什么」完全看不见，想跳回某个早先的问题只能一路滚；
 *   ② 想回到最新回复要一直往下滚（流式输出时甚至追不上）。
 *
 * 刻度条取的是**每条用户消息在滚动容器里的相对偏移**，不是固定等分：
 * 一条短问题和一条长回答的刻度间距不同，那正是它们在对话里的真实占比。
 * 位置每次都从 DOM 现算（recompute），不缓存 —— 流式输出会持续改变高度，
 * 缓存一次就等于刻度条从第二秒开始全是错的。
 */

// 一次扫描最多画多少条：超过之后一列 2px 的线会连成一片灰，
// 而且面板要滚，不如直接截断并提示「还有更早的」。
const MSG_NAV_MAX = 40;
const MSG_NAV_DASH_MIN_ITEMS = 2;   // 只有 1 条提问时没有导航意义，不显示

/* 静止态是「11 行窗口」，不是「把 N 条压进固定长度」。

   两版都错过，记下来免得再试：
     第一版：由内容撑高。14 条 350px、25 条 625px，超过 max-height 被
     overflow:hidden 静默硬切 —— 长度不可控。
     第二版：固定 200px 轨道 + flex:1 均分。长度可控了，但 40 条时
     行距被压到 4.6px，横线糊成一片灰带，数不清也点不准。

   现在按参考图做：**行高固定（--nav-row，当前 40px）、只开 11 行的窗口**，
   上下各留一行缩短淡出暗示还有更多，超出窗口的部分靠 track 平移来"翻页"。
   40 条和 11 条看起来一样长，但密度不变、可点性不变。
   DeepSeek 参考图实测：相邻间距 39.5px、横线 11.5×3、高亮 17×4、
   整列占视口高 37%。这里取 40px × 11 = 440px（40 是 8 的倍数，
   常见 DPR 下不出半像素；且与横线 12px 的密度比 0.300 贴近参考图的 0.291）。
   ⚠️ 行距与横线长度联动，改一个必须改另一个，理由见 19-msg-nav.css
   「横线本体」上方那段。 */
const NAV_SLOTS = 11;       // 窗口能放几行（参考图是 11）
/* 行高**不在这里写死**，从 CSS 的 --nav-row 读：位移量 = 行高 × 行数，
   两边各写一份必然漂（改了 CSS 忘了改 JS，窗口就会错位到露半行）。
   见 _navRowPx()。 */
/* 窗口停在"第几行在中间"。0.5 让当前项落在正中 —— 参考图里蓝色那条
   就在正中偏上，上下各有约 5 行可看，是因为"往后还有内容"更重要：
   用户往上翻的时候最想知道的是下面还有多少。这里取 0.5，配合
   下面 _syncNavWindow 的夹取，会在开头/结尾自动贴边。 */
const NAV_ANCHOR = 0.5;

let _msgNavEl = null;
let _msgNavFab = null;
let _msgNavRefreshTimer = null;

/**
 * 取当前会话的滚动容器与消息容器。
 * 两者不是同一个元素：滚动在 #chat-messages，消息在 .session-messages 里。
 * @returns {{ wrap: HTMLElement, list: HTMLElement } | null}
 */
function _msgNavTargets() {
  const wrap = $('#chat-messages');
  if (!wrap) return null;
  const sid = state.currentSessionId || '__default__';
  const sc = state.sessionContainers[sid];
  const list = sc && sc.element;
  // 没有容器（欢迎页阶段）时没有可导航的内容
  if (!list || list.style.display === 'none') return null;
  return { wrap, list };
}

/** 取用户消息的文字：优先 .answer-area 的纯文本，附件 chip 不算标题 */
function _msgNavTitle(msgEl) {
  const area = msgEl.querySelector('.answer-area');
  const raw = (area ? area.innerText : msgEl.textContent) || '';
  const line = raw.replace(/\s+/g, ' ').trim();
  return line || '（附件）';
}

/**
 * 重建刻度条 DOM 并更新位置。幂等，可反复调用。
 * 调用时机：新消息渲染、流式 token 批量到达、切换会话、窗口尺寸变化。
 */
function refreshMsgNav() {
  const nav = _msgNavEl;
  if (!nav) return;
  const t = _msgNavTargets();
  if (!t) { nav.classList.remove('show'); return; }

  const items = Array.from(t.list.querySelectorAll('.message.user'));
  if (items.length < MSG_NAV_DASH_MIN_ITEMS) {
    nav.classList.remove('show');
    nav.innerHTML = '';
    return;
  }

  // 用数据指纹判断是否需要重建：长度变了、标题变了才重建，
  // 否则流式期间每次 token 都重建一遍 DOM，滚动会卡。
  const fingerprint = items.map(_msgNavTitle).join('\u0001');
  if (nav.dataset.fp !== fingerprint) {
    nav.dataset.fp = fingerprint;
    /* 重建后按钮是全新的，都还没有 .active。必须把"上次高亮第几条"
       的缓存清掉 —— 否则 updateMsgNavActive 发现 active 没变就直接
       return，新按钮一个都不会被点亮，整列看起来"没有当前位置"。 */
    delete nav.dataset.activeIdx;
    const shown = items.slice(-MSG_NAV_MAX);
    const rows = shown.map((el, i) => {
      const idx = items.length - shown.length + i;
      return `<button type="button" class="msg-nav-item" data-nav-idx="${idx}" title="${escapeHtml(_msgNavTitle(el))}">`
        + `<span class="msg-nav-title">${escapeHtml(_msgNavTitle(el))}</span>`
        + `<span class="msg-nav-dash"></span>`
        + `</button>`;
    }).join('');
    // 刻度线全部挂进位移层（.msg-nav-track），而不是直接挂在 nav 上
    const track = nav.querySelector('.msg-nav-track');
    if (track) track.innerHTML = rows;
    else nav.innerHTML = rows;   // 兜底：老 DOM 没有 track 层
  }

  /* 窗口行数：条数不足 11 时按条数收（2 条就只有 2 行高，
     不留一大片空白），够 11 就固定 11 行。
     写进 CSS 变量而不是直接设 height：面板态要用 height:auto 覆盖它。 */
  const n = nav.querySelectorAll('.msg-nav-item').length;
  const slots = Math.min(NAV_SLOTS, n);
  if (nav.dataset.slots !== String(slots)) {
    nav.dataset.slots = String(slots);
    nav.style.setProperty('--nav-slots', slots);
  }

  nav.classList.add('show');
  _syncNavWindow();
  updateMsgNavActive();
}

/**
 * 读 CSS 里定义的行高（--nav-row），单位 px。
 *
 * 为什么不让 JS 自己写死一个常量：窗口位移量 = 行高 × 跳过行数，
 * 而"一行多高"是视觉决策、写在 CSS 里。两边各存一份的必然结果是
 * 改了 CSS 忘改 JS —— 表现是窗口错位、上下露半行，且**不报错**。
 * 读不到（异常情况）时退回 0，_syncNavWindow 会直接放弃位移。
 */
function _navRowPx(nav) {
  const v = parseFloat(getComputedStyle(nav).getPropertyValue('--nav-row'));
  return Number.isFinite(v) && v > 0 ? v : 0;
}

/**
 * 把静止态窗口平移到「让当前项可见」的位置。
 *
 * 这是用户说的「滚动内容到最上面那条，则右侧的刻度也会自动翻页滚动」——
 * 窗口是一个固定 11 行的取景框，内容滚到第 25 条时，取景框整体往上平移，
 * 让第 25 条落在窗口里。**位置反馈来自"框在动"，而不是"条目被压扁"。**
 *
 * 三个细节：
 *   · 用 transform 位移而不是改 scrollTop：overflow:hidden 元素的可编程
 *     滚动在无头环境和部分浏览器里行为不一致，transform 是确定性的。
 *   · active 定义为"窗口正中偏上那一行"（NAV_ANCHOR）。夹取到 [0, n-slots]，
 *     所以开头几行时窗口贴顶、末尾几行时贴底，不会露出空白。
 *   · 首末行是否被截断由 JS 逐行打 class（is-edge-top/bottom），
 *     CSS 靠它把那一行的横线缩短淡出 —— 这是"还有更多"的暗示。
 */
function _syncNavWindow() {
  const nav = _msgNavEl;
  if (!nav || nav.classList.contains('expanded')) return;

  const items = nav.querySelectorAll('.msg-nav-item');
  const n = items.length;
  if (!n) return;

  const active = Number(nav.dataset.activeIdx || 0);
  const slots = Math.min(NAV_SLOTS, n);

  // 首行索引：让 active 落在窗口的 NAV_ANCHOR 位置上，再夹到合法范围
  let first = Math.round(active - (slots - 1) * NAV_ANCHOR);
  first = Math.max(0, Math.min(n - slots, first));

  const rowPx = _navRowPx(nav);
  const shift = -first * rowPx;
  if (nav.dataset.shift !== String(shift)) {
    nav.dataset.shift = String(shift);
    nav.style.setProperty('--nav-shift', shift + 'px');
  }

  /* 把"窗口内首末行"精确标出来给 CSS 缩短用。
     为什么不用 :first-child / :last-child —— 那是**整个列表**的首末，
     不是**窗口里**的首末。窗口平移到第 6~16 行时，:first-child 指的是
     第 0 行（早滚出可见区了），于是缩短效果一次都不会出现：
     class、变量、位移全对，只有视觉提示永远缺席。
     真首条（first===0）/ 真末条（first+slots===n）不加 —— 那一侧
     本来就没有更多内容，不该暗示"上面还有"。 */
  items.forEach((el, i) => {
    const top = (i === first && first > 0);
    const bottom = (i === first + slots - 1 && first + slots < n);
    el.classList.toggle('is-edge-top', top);
    el.classList.toggle('is-edge-bottom', bottom);
  });
  // 上下沿状态同时留在 nav 上，方便测试与外部样式判断
  nav.classList.toggle('is-truncated-top', first > 0);
  nav.classList.toggle('is-truncated-bottom', first + slots < n);
}

/**
 * 按滚动位置高亮「当前读到哪条提问」。
 *
 * ⚠️ 这个函数修过一次真 bug：原先判据是「顶部已滚过容器上沿的那批里最后一条」，
 * 实测滚到最底时高亮停在第 11 条（共 14 条），**最后 3 条永远点不亮**。
 *
 * 根因不是阈值没调好，而是几何上不可能：实测最大滚动距离 1838px，
 * 而第 12、13 条提问的绝对位置是 1968 / 2129 —— **它们根本滚不到顶部**。
 * 滚到底时它们还在视口下方 130 / 291px 处，所以"顶部越过上沿"这个条件
 * 对它们永远不成立。只要判据挂着这一条，无论怎么调容差都点不亮。
 *
 * 做法分两段：
 *   ① 前段按自然位置激活 —— 「顶部越过上沿」对应"我正在读这一条下面的回答"，
 *      这是最贴合阅读直觉的定义，且保留了"短问答间距小、长回答间距大"的真实占比，
 *      不能换成纯滚动百分比（那会把所有提问当成等距，失去信息）。
 *   ② 尾段压缩 —— 找出最后一个"还能滚到顶部"的条目，它之后的那些在
 *      剩余滚动距离里**均分**。这样滚到底时高亮正好落在最后一条。
 *
 * 用户提的方案「持续向上滚动，右边的定位标志也同时切换」正是这个效果：
 * 高亮在整个滚动过程里连续往前推，而不是"滚到某条才跳一下"。
 */
function updateMsgNavActive() {
  const nav = _msgNavEl;
  if (!nav) return;
  const t = _msgNavTargets();
  if (!t) return;
  const btns = nav.querySelectorAll('.msg-nav-item');
  if (!btns.length) return;

  const items = Array.from(t.list.querySelectorAll('.message.user'));
  if (!items.length) return;

  const ACTIVE_SLACK = 80;   // 刚滚到标题上方时也算到了
  const wrapRect = t.wrap.getBoundingClientRect();
  const scrollTop = t.wrap.scrollTop;
  const maxScroll = t.wrap.scrollHeight - t.wrap.clientHeight;

  // 每条提问的绝对位置 → 它"该被点亮时的 scrollTop"
  const naturals = items.map(el => Math.max(
    0, el.getBoundingClientRect().top - wrapRect.top + scrollTop - ACTIVE_SLACK
  ));

  let active = 0;
  if (maxScroll <= 0) {
    // 内容不足一屏，没有"读到哪"可言
    active = 0;
  } else {
    // 最后一个自然激活点仍在可达范围内的条目
    let lastReachable = 0;
    for (let i = 0; i < naturals.length; i++) {
      if (naturals[i] <= maxScroll) lastReachable = i;
    }

    // ① 前段：自然激活
    for (let i = 0; i <= lastReachable; i++) {
      if (naturals[i] <= scrollTop) active = i;
    }

    /* ② 尾段：把滚不到顶的条目均分到剩余距离上。
       span 是"最后一个可达条目激活后，还能往下滚多少"；
       分成 tailCnt + 1 份，滚到底时正好走满 tailCnt 份 = 停在最后一条。 */
    const tailCnt = items.length - 1 - lastReachable;
    if (tailCnt > 0) {
      const span = maxScroll - naturals[lastReachable];
      if (span > 0) {
        const per = span / (tailCnt + 1);
        const passed = scrollTop - naturals[lastReachable];
        if (passed > 0) {
          active = lastReachable + Math.min(tailCnt, Math.floor(passed / per));
        }
      } else {
        // 没有剩余距离可分（极端窄的视口），直接给最后一条，至少能到头
        active = items.length - 1;
      }
    }
  }

  // 不变就早退：滚动时每帧只改两个 class，不扫全部 40 个按钮
  if (nav.dataset.activeIdx === String(active)) return;
  nav.dataset.activeIdx = String(active);
  btns.forEach(b => {
    b.classList.toggle('active', Number(b.dataset.navIdx) === active);
  });
  /* 高亮变了，11 行取景框就要跟着平移 —— 这是"翻页"的触发点。
     放在早退之后：没变就不必重算窗口，滚动的每一帧基本都走这条捷径。 */
  _syncNavWindow();
}

/** 滚到第 idx 条用户消息。留 12px 余量，别让气泡紧贴顶栏 */
function scrollToUserMessage(idx) {
  const t = _msgNavTargets();
  if (!t) return;
  const items = t.list.querySelectorAll('.message.user');
  const el = items[idx];
  if (!el) return;
  const wrapTop = t.wrap.getBoundingClientRect().top;
  const elTop = el.getBoundingClientRect().top;
  t.wrap.scrollTo({ top: t.wrap.scrollTop + (elTop - wrapTop) - 12, behavior: 'smooth' });

  /* 跳完立刻把高亮落到位，不等 scroll 事件。
     为什么必须补这一下：smooth 动画要几百毫秒，这期间 scroll 事件会把
     active 一帧帧推向目标 —— 于是刻度条一边翻页一边亮，中途会闪过好几档。
     更糟的是用户点了"第 20 条"，眼光扫回右侧时看到的还是旧位置。
     直接按目标索引写死一次，再让后续 scroll 事件接管即可。 */
  if (_msgNavEl && _msgNavEl.dataset.activeIdx !== String(idx)) {
    _msgNavEl.dataset.activeIdx = String(idx);
    _msgNavEl.querySelectorAll('.msg-nav-item').forEach(b => {
      b.classList.toggle('active', Number(b.dataset.navIdx) === idx);
    });
    _syncNavWindow();
  }
}

// ===== 回到底部浮标 =====
const FAB_ICON_ARROW = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M6 13l6 6 6-6"/></svg>';

/** 是否已经贴着底部（容差 40px：等宽字体行高取整后很难精确相等） */
function _isNearBottom(wrap) {
  return wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight <= 40;
}

/**
 * 刷新浮标的显隐与忙碌态。幂等。
 *
 * 忙碌判据看四条，缺一条都会漏掉一种「明明在跑却显示成静止」：
 *   - 当前会话正在流式（state.isStreaming && streamingSessionId 相同）
 *   - 当前会话被当成后台任务在订阅（subscribingSessionId）
 *   - 当前会话的流对象还在（切换会话回来时 isStreaming 可能已归 false）
 *   - 后端轮询说这个会话有 running 任务（最权威：用户翻历史消息期间
 *     前端所有的 streaming 标记都可能已经被清掉了，只有它还在动）
 */
function refreshScrollFab() {
  const fab = _msgNavFab;
  const wrap = $('#chat-messages');
  if (!fab || !wrap) return;

  const nearBottom = _isNearBottom(wrap);
  fab.classList.toggle('show', !nearBottom);

  /* 「当前会话」判断必须带非空前提。
     没有当前会话时 currentSessionId / streamingSessionId / subscribingSessionId
     三者都是 null，`null === null` 成立 —— 于是「正在流式」「正在订阅」
     两个判据会永久为真，浮标上的 loading 圈永远不摘。
     这类缺陷特别难查：四条判据里另外两条都正常，看起来"逻辑没问题"，
     真凶是两个"都没值"被当成了"是同一个"。 */
  const curSid = state.currentSessionId;
  const hasSid = !!curSid;
  const sc = state.sessionContainers[curSid || '__default__'];
  const taskInfo = state.taskStatuses && hasSid && state.taskStatuses[curSid];
  const busy = !!(
    (hasSid && state.isStreaming && state.streamingSessionId === curSid)
    || (hasSid && state.subscribingSessionId === curSid)
    || (sc && sc.stream && sc.stream.container && !sc.stream.finished)
    || (taskInfo && taskInfo.status === 'running')
  );
  fab.classList.toggle('busy', busy);
  fab.title = busy ? '回到最新（还有内容在输出）' : '回到最新';
}

// ===== 面板展开/收起 =====
/* 为什么不用纯 :hover：
   面板展开后有 300px 宽，必然盖住正文右侧。纯 :hover 的后果是
   —— 鼠标从刻度线移进面板去点某一条，指针一旦经过面板边缘之外的
   空隙，面板就瞬间收起，点击目标消失。用户看到的是"鼠标一碰就没了"。

   所以改成 JS 控制在两个区域：静止态的窄条 + 展开后的面板本身，
   两者拼成一块"热区"。鼠标在热区里任意位置都保持展开；
   真正离开（横向退到正文里、或纵向离开面板上下沿超过 120px）才收起。
   纵向留 120px 余量是因为面板很高，用户读被盖住的那部分正文时
   指针可能滑出面板上下边界，那不代表他不想再看了。 */
const NAV_HIDE_DELAY = 160;      // 收起前的宽限，避免边缘抖动导致闪烁
const NAV_VERTICAL_SLACK = 120;  // 纵向离开多少像素才算真的走了
/* 展开前的"意图确认"时长。

   为什么需要它：指针从正文划到右侧、或者只是想点滚动条时，会横向扫过
   这条 34px 的窄带。`pointerenter` 是**碰到即触发**，一点延迟都没有，
   于是鼠标只是路过，一块 300px 的面板就已经盖在正文上了 —— 用户原话
   「鼠标移上去弹窗出来的很快」。快不是问题，**不请自来**才是问题。

   160ms 的判据是"停没停"：真实想用刻度条的人会在上面停住找位置，
   路过的人不会。这个时长也比人手的微抖动长（~100ms），不会漏掉真意图。 */
const NAV_EXPAND_DELAY = 160;
let _navHideTimer = null;
let _navShowTimer = null;
let _navPointerIn = false;
/* 指针最近一次的纵坐标。展开时要用它换算"你指的是哪一条"——
   pointerenter 自带 clientY，但排队 160ms 期间指针可能还在移动，
   所以每次 pointermove 都更新，展开时取最新的那个。 */
let _navLastPointerY = null;
/* 指针最近一次的真实坐标（含 x）。用于识别「几何自变引发的假离开」，
   见 NAV_BOUNCE_PX 的说明。 */
let _navLastPointerX = null;
/* 上一次 pointerleave 发生时的指针坐标。
   若紧接着收到 pointerenter 且坐标几乎一致，说明指针根本就没动过 ——
   那次 leave 是**元素自己变形**把指针甩出去的，不是用户意图。 */
let _navLeaveX = null;
let _navLeaveY = null;
/* 一次「假离开」之后允许的容忍度：在这个像素范围内回来 → 视为抖动 */
const NAV_BOUNCE_PX = 6;

function _expandMsgNav(immediate) {
  const nav = _msgNavEl;
  if (!nav || !nav.classList.contains('show')) return;
  _navPointerIn = true;
  clearTimeout(_navHideTimer);
  if (nav.classList.contains('expanded')) return;

  /* 已经在展开状态就不用再排队；否则延迟确认意图。
     键盘 focusin 走 immediate=true —— Tab 进来是明确意图，不该再等。 */
  if (immediate) {
    _openMsgNavPanel();
    return;
  }
  clearTimeout(_navShowTimer);
  _navShowTimer = setTimeout(() => {
    if (!_navPointerIn) return;   // 排队期间指针已经走了，不展开
    _openMsgNavPanel();
  }, NAV_EXPAND_DELAY);
}

/**
 * 真正把窄条展开成面板，并**把面板滚到"指针所在的那一条"**。
 *
 * 这里原本漏了一件事，用户反馈「没有定位到刻度对应的对话」：
 * 窄条是 11 行窗口（显示第 6~16 条），而展开后是**完整清单**（40 条从第 1 条
 * 开始铺）。同一块屏幕位置，展开前指针下是第 11 条，展开后变成了第 7 条 ——
 * 用户明明瞄准某一条，一展开它就跑掉了，还得重新找。
 *
 * 所以展开时必须按"指针纵向位置占窗口的比例"换算成完整清单里的行，
 * 再把面板滚到让那一行落在同一个高度上。视觉上是"窗口拉开成清单，
 * 你指着的那条留在原地"。
 */
function _openMsgNavPanel() {
  const nav = _msgNavEl;
  if (!nav) return;

  const items = nav.querySelectorAll('.msg-nav-item');
  const n = items.length;

  /* ① 先认出「指针此刻盖住的是哪一条」，以及指针在**那一行内部**的比例。

     为什么不用"指针占窗口的比例 × 总条数"：窗口显示 first..first+10
     （共 11 行），指针在正中对应 first+5，而 frac×(n−1) 给出的是另一个
     序号 —— 实测差一条，展开后你指着的那条又跑掉了。

     为什么记"行内比例"而不是"相对窗口顶部的像素距离"：
     窗口高度 528、行高 48；面板高度 430、行高 29 —— 两个状态的几何
     完全不同，像素距离换算过来必然错位。行内比例（0=行顶，1=行底）
     是唯一在两个状态下语义不变、且不需要知道面板位置就有意义的量。 */
  let hoveredIdx = null;
  let inRowFrac = 0.5;
  if (_navLastPointerY != null) {
    for (let i = 0; i < n; i++) {
      const r = items[i].getBoundingClientRect();
      if (r.top <= _navLastPointerY && r.bottom > _navLastPointerY) {
        hoveredIdx = i;
        inRowFrac = r.height > 0 ? (_navLastPointerY - r.top) / r.height : 0.5;
        break;
      }
    }
  }
  // 指针在窗口的上下余量里（不在任何行上）→ 退回到"当前正在读的那条"，居中
  if (hoveredIdx == null) hoveredIdx = Number(nav.dataset.activeIdx || 0);

  /* 展开前先清掉面板可能残留的 scrollTop 与纵向位移。
     否则下面量 rect 时已经带着旧偏移，算出来会再偏一次。
     位移必须清：getBoundingClientRect 会把 transform 算进去，
     带着上一轮的 --nav-panel-shift 去量，夹取区间也跟着错。 */
  nav.scrollTop = 0;
  nav.style.setProperty('--nav-panel-shift', '0px');
  nav.classList.add('expanded');
  if (!n) return;

  const el = items[hoveredIdx];
  if (!el) return;

  /* ② 量展开后的几何。两个 rect 相减把 padding/border 全消掉
     （offsetTop 相对 offsetParent 的 border box，会稳定多算 13px）。 */
  const navRect = nav.getBoundingClientRect();
  const elRect = el.getBoundingClientRect();
  const offsetInContent = elRect.top - navRect.top;   // scrollTop=0 时的相对位置
  const visibleH = nav.clientHeight || navRect.height;

  /* 指针的绝对纵坐标。pointerenter/pointermove 已经把它记下来了；
     万一没有（键盘 focusin 进来），退回到面板正中。 */
  const pointerY = _navLastPointerY != null ? _navLastPointerY : navRect.top + visibleH / 2;

  /* ③ 对齐：让指针所指的那一行，展开后仍在指针的高度上。

     两个状态行高不同（窗口 40px → 面板 29px），"哪一行在指针下"必然重算。
     这条修了三版，前两版都"测试全绿"却都是错的，记下来免得再试：

       一、按 `frac × (n−1)` 比例换算 —— 与窗口的 first 不对应，实测差一条。
       二、认"指针盖住那条"、只调 scrollTop —— 14 条时面板装得下
           （406 < 430），scrollTop 恒为 0，对最常见的规模完全无效；
           展开后指针下偏 115px。而当时断言写的是"指针那条仍**可见**"，
           第 12 条也在面板里，所以照样绿 —— **判据挑错了，不是算法对**。
       三、联立解（见下）。实测 4/12/14/25/40 条 × 7 个位置：35/35 同一条。

     推导在下面 ②③ 之间，这里不重复，避免两处注释以后各改各的。 */
  const wantTopAbs = pointerY - inRowFrac * elRect.height;
  const idealScroll = offsetInContent - (wantTopAbs - navRect.top);
  const maxTop = Math.max(0, nav.scrollHeight - visibleH);

  // ① scrollTop 的可行区间：元素必须整个留在面板可见区内
  const scLo = Math.max(0, offsetInContent + elRect.height - visibleH);
  const scHi = Math.max(scLo, Math.min(maxTop, offsetInContent));

  /* ② 面板位移的可行区间：留在聊天区内，上下各留 EDGE。

     ⚠️ 试过把下界收紧到"输入卡上沿"，想避免面板压住输入框 —— 不行。
     实测（1280×860）：chat-area 0~717、消息区 61~533、输入卡 539~681，
     面板 max-height 60% = 430，**垂直居中后底边 573 就已经压住输入卡 40px**。
     这是 max-height 与居中基线的几何结果，跟展开位移无关 ——
     默认位置就压着。若把输入卡上沿当硬边界，面板会被整体上推 40px，
     中间位置反而全部错位（实测偏 92px）。
     要真不压只能把 max-height 降到 48%，那 14 条（406px）就装不下、
     被迫滚动，违背"常见规模一眼看全"。
     **压住的是输入卡上部空白区（placeholder 在左侧、按钮在底部，均不受影响），
     两害相权取不动。** */
  const area = $('#chat-area');
  const areaRect = area ? area.getBoundingClientRect() : null;
  let minShift = -Infinity;
  let maxShift = Infinity;
  if (areaRect) {
    const EDGE = 8;
    minShift = (areaRect.top + EDGE) - navRect.top;
    maxShift = (areaRect.bottom - EDGE) - (navRect.top + navRect.height);
  }

  /* ③ 联立。约束是**一条等式**（不是两个独立的补偿）：
        elTop = navTop + shift + offsetInContent − sc  必须等于 wantTopAbs
     移项得  shift = sc − idealScroll。
     所以 sc 一旦定下，shift 就被唯一确定；反过来 shift 定了 sc 也定了。
     各自都有可行区间，于是 sc 必须同时落在
        [scLo, scHi]  且  [minShift + idealScroll, maxShift + idealScroll]
     取交集里**最靠近 idealScroll** 的值 —— 这样 |shift| 最小，
     也就是"能靠滚动解决就不动面板"。

     ⚠️ 先前两次修错都是因为把这条等式拆成了两段独立的夹取：
     先夹 sc，再把残差交给 shift 并单独夹一遍。两次夹取各自合法，
     合起来却不再满足等式 —— 实测就出现"中间位置反而错 92px"。
     约束是联立的就必须联立解。 */
  const lo = Math.max(scLo, minShift + idealScroll);
  const hi = Math.min(scHi, maxShift + idealScroll);
  let sc;
  if (hi >= lo) {
    sc = Math.max(lo, Math.min(hi, idealScroll));
  } else {
    /* 无解：面板高度 + 目标位置在可用空间里放不下（窄屏极端）。
       退回"保滚动、保可见"，牺牲精确对齐 —— 至少行不跑出面板。 */
    sc = Math.max(scLo, Math.min(scHi, idealScroll));
  }
  nav.scrollTop = sc;

  let shift = sc - idealScroll;
  // 无解时上面的 shift 可能越界，这里再夹一次兜底（此时对齐已不保证）
  shift = Math.max(minShift, Math.min(maxShift, shift));
  nav.style.setProperty('--nav-panel-shift', Math.round(shift) + 'px');
}

function _collapseMsgNavSoon() {
  clearTimeout(_navHideTimer);
  clearTimeout(_navShowTimer);
  _navHideTimer = setTimeout(() => {
    if (!_msgNavEl) return;
    _navPointerIn = false;
    _msgNavEl.classList.remove('expanded');
    /* 收起时必须把面板里滚过的 scrollTop 清零。
       这是「翻页有些问题」的根因：overflow:hidden 不重置滚动位置，
       面板里滚了 317px 之后收起，静止态窗口是 overflow:hidden 的滚动容器，
       **同一份 scrollTop 会继续把内容往上顶**，于是 transform 位移
       算的是第 6 行开头、实际看到的是第 13 行 —— 两套位移叠加，翻页全乱。
       transform 是声明式的、scrollTop 是残留状态，两者必须只有一个生效。 */
    _msgNavEl.scrollTop = 0;
    /* 面板纵向让位也清零，让下一次展开从"垂直居中"这个已知基准起算。
       不依赖"静止态下它没被消费"这种前提 —— 一旦以后有人把它挪到
       基础类上（比如想让刻度条也跟随），残留值就会把整列顶歪。 */
    _msgNavEl.style.setProperty('--nav-panel-shift', '0px');
    _syncNavWindow();
  }, NAV_HIDE_DELAY);
}

/** 指针是否还在「窄条 + 面板」这块热区里（纵向带余量） */
function _pointerInNavZone(e) {
  const nav = _msgNavEl;
  if (!nav) return false;
  const r = nav.getBoundingClientRect();
  // 面板态与窄条态的左边界不同（面板向左展开），但 r.left 取的是当前实际值，
  // 所以这里不必区分 —— 保留变量只为提醒"两种态几何不同"。
  return e.clientX >= r.left - 4 && e.clientX <= r.right + 4
    && e.clientY >= r.top - NAV_VERTICAL_SLACK
    && e.clientY <= r.bottom + NAV_VERTICAL_SLACK;
}

// ===== 事件绑定 =====
function initMsgNav() {
  _msgNavEl = $('#msg-nav');
  _msgNavFab = $('#scroll-bottom-fab');
  const wrap = $('#chat-messages');
  if (!_msgNavEl || !_msgNavFab || !wrap) return;

  _msgNavFab.innerHTML = FAB_ICON_ARROW;

  _msgNavFab.addEventListener('click', () => {
    wrap.scrollTo({ top: wrap.scrollHeight, behavior: 'smooth' });
  });

  // 刻度条点击（委托：内容是动态重建的）
  _msgNavEl.addEventListener('click', (e) => {
    const btn = evTarget(e)?.closest('.msg-nav-item');
    if (!btn) return;
    scrollToUserMessage(Number(btn.dataset.navIdx));
    _collapseMsgNavSoon();
  });

  /* 指针进窄条 → 排队展开（不是立刻）。
     立刻展开的后果：从正文横向扫到右侧、或去够滚动条时会碰到这 34px，
     面板就自己弹出来了。见 NAV_EXPAND_DELAY 的说明。 */
  _msgNavEl.addEventListener('pointerenter', (e) => {
    /* 关键判据：这次 enter 是不是「原地上一次假 leave 的反弹」。
       窄条展开时自己会变矮、上沿下移，把**没动过的**指针甩到面板外面，
       浏览器随即补发 pointerleave；等过渡走完它又落回面板里，于是再发
       enter —— 一来一回就抖一次。4 秒内曾抖出 12 个「出现-消失」循环。
       判据：离开与回来的坐标差 ≤ NAV_BOUNCE_PX = 指针没动 = 是变形。
       真的移开再移回来，坐标差会是几十上百像素，不会被误判。 */
    const bouncedBack = _navLeaveX != null
      && Math.abs(e.clientX - _navLeaveX) <= NAV_BOUNCE_PX
      && Math.abs(e.clientY - _navLeaveY) <= NAV_BOUNCE_PX;
    _navLeaveX = null;
    _navLeaveY = null;

    _navLastPointerY = e.clientY;
    _navLastPointerX = e.clientX;
    if (bouncedBack && _msgNavEl.classList.contains('expanded')) {
      /* 原样回来了：撤销那次待执行的收起，保持展开，不做任何动画 */
      clearTimeout(_navHideTimer);
      _navPointerIn = true;
      return;
    }
    _expandMsgNav(false);
  });
  /* 在面板内部／窄条上移动时：① 不断续命 ② 记住纵向位置供展开定位用。
     clientY 在 pointerenter 之后只会更准（用户还在动），所以在 move 里覆盖。 */
  _msgNavEl.addEventListener('pointermove', (e) => {
    _navLastPointerY = e.clientY;
    _navLastPointerX = e.clientX;
    if (_msgNavEl.classList.contains('expanded')) {
      clearTimeout(_navHideTimer);
    } else if (!_navShowTimer) {
      /* 已经离开又回来了，但排队已取消（比如上一步 pointerleave 清掉的）→
         重新排队。不在这里重排的话，指针在窄条上小幅抖动会永久抑制展开。 */
      _navPointerIn = true;
      _expandMsgNav(false);
    }
  });
  _msgNavEl.addEventListener('pointerleave', (e) => {
    /* 排队期间就离开 → 直接取消排队，"路过"不该弹面板。
       这是这个交互的核心：pointerenter 之后 pointerleave 先到，
       就说明指针没停住。 */
    _navPointerIn = false;
    clearTimeout(_navShowTimer);
    _navShowTimer = null;

    /* 记下离开坐标。真正收起仍照常安排 —— 不能在这里就判"是不是假离开"，
       因为此刻还看不出用户会不会马上回来。判据放在 pointerenter 里：
       若在宽限期内**几乎原地**回来，就撤销这次收起（见那边的说明）。 */
    if (e && typeof e.clientX === 'number') {
      _navLeaveX = e.clientX;
      _navLeaveY = e.clientY;
    }
    _collapseMsgNavSoon();
  });

  /* 全局指针移动：只有它能在"从面板移到正文"时正确收起。
     面板自身收不到 pointerleave（指针直接进了另一个兄弟元素），
     靠监听整体移动 + 几何判断最可靠。 */
  document.addEventListener('pointermove', (e) => {
    if (!_msgNavEl || !_msgNavEl.classList.contains('expanded')) {
      /* 还没展开时也要维护纵向位置：指针是从正文那边移进来的，
         本元素上的 pointerenter 可能早于这次 move 触发。 */
      return;
    }
    _navLastPointerX = e.clientX;
    _navLastPointerY = e.clientY;
    if (_pointerInNavZone(e)) {
      clearTimeout(_navHideTimer);
    } else {
      _collapseMsgNavSoon();
    }
  }, { passive: true });

  // 键盘可达性：Tab 进面板时展开，Esc 收起
  _msgNavEl.addEventListener('focusin', () => {
    /* Tab 进来是明确意图，不再等 160ms。
       immediate=true 时也没有指针坐标，_openMsgNavPanel 会退回 frac=0.5（正中）。 */
    _expandMsgNav(true);
  });
  _msgNavEl.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      _navPointerIn = false;
      clearTimeout(_navShowTimer);
      _navShowTimer = null;
      _msgNavEl.classList.remove('expanded');
      _msgNavEl.scrollTop = 0;   // 同 _collapseMsgNavSoon：清掉面板残留位移
      _msgNavEl.style.setProperty('--nav-panel-shift', '0px');
      _syncNavWindow();
      _msgNavEl.blur();
    }
  });

  // 滚动：节流到下一帧，避免高频 scroll 事件里反复量 DOM
  let ticking = false;
  wrap.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      updateMsgNavActive();
      refreshScrollFab();
    });
  }, { passive: true });

  // 尺寸变化：正文换行会改变每条消息的高度，刻度位置要重算
  window.addEventListener('resize', () => {
    clearTimeout(_msgNavRefreshTimer);
    _msgNavRefreshTimer = setTimeout(() => { refreshMsgNav(); refreshScrollFab(); }, 120);
  });
}

/**
 * 供渲染路径调用的统一入口：内容变了就调它。
 * 合并到下一帧，连续调用只跑一次 —— 流式 token 可能一秒几十次。
 */
function scheduleMsgNavRefresh() {
  if (_msgNavRefreshTimer) return;
  _msgNavRefreshTimer = setTimeout(() => {
    _msgNavRefreshTimer = null;
    refreshMsgNav();
    refreshScrollFab();
  }, 80);
}
