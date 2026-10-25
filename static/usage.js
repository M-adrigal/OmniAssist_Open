/* Token 消耗看板
 * 数据口径：以服务商回传的 usage 为准；is_estimated=1 的行是估算兜底，界面单独标注。
 */
(function () {
  'use strict';

  var state = {
    range: '30',          // today | 7 | 30 | custom
    start: '',
    end: '',
    scope: 'self',        // self | all（管理员）
    isAdmin: false,
    page: 1,
    size: 20,
    q: '',                // 会话名称搜索关键字
    summary: null,
    sessions: null,
    loading: false,
  };

  function $(id) { return document.getElementById(id); }

  // ===== 主题：与主站保持同一份偏好 =====
  function initTheme() {
    var saved = localStorage.getItem('theme');
    var dark = saved === 'dark' ||
      (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.classList.toggle('dark', dark);
  }

  // ===== 格式化 =====
  function fmt(n) {
    n = Number(n || 0);
    return n.toLocaleString('zh-CN');
  }
  function fmtShort(n) {
    n = Number(n || 0);
    if (n >= 1e8) return (n / 1e8).toFixed(1) + '亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
    return String(n);
  }
  // KPI 主数值：位数多时降一档字号，避免千亿级数字撑破卡片
  function setKpiNum(id, n) {
    var el = $(id);
    if (!el) return;
    var s = fmt(n);
    el.textContent = s;
    el.title = s;
    el.style.fontSize = s.length >= 14 ? '16px' : (s.length >= 11 ? '19px' : '');
  }
  function fmtTime(s) {
    if (!s) return '—';
    // 'YYYY-MM-DD HH:MM:SS' -> 'MM-DD HH:MM'
    var parts = String(s).split(' ');
    if (parts.length < 2) return s;
    return parts[0].slice(5) + ' ' + parts[1].slice(0, 5);
  }
  // 缓存命中率：整数百分比够大就取整，个位数保留一位小数，避免 0% 与 <1% 混淆
  function fmtRate(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (!isFinite(n)) return '—';
    return (n >= 10 || n === 0 ? Math.round(n) : n.toFixed(1)) + '%';
  }
  function dateLabel(d) { return d ? d.slice(5) : ''; }

  // ===== 请求 =====
  function apiGet(url) {
    return fetch(url, { credentials: 'same-origin' }).then(function (res) {
      if (res.status === 401) {
        // 在浮层 iframe 内登录态失效时，让整个页面去登录，避免 iframe 里又套一层应用
        if (window.parent && window.parent !== window) {
          try { window.parent.location.href = '/login.html'; } catch (e) { /* 跨域受限 */ }
        }
        window.location.href = '/login.html';
        throw new Error('未登录');
      }
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.detail || ('请求失败 ' + res.status));
        });
      }
      return res.json();
    });
  }

  function rangeParams() {
    if (state.range === 'custom') {
      return 'start=' + encodeURIComponent(state.start) + '&end=' + encodeURIComponent(state.end);
    }
    if (state.range === 'today') {
      var t = new Date();
      var ds = ymd(t);
      return 'start=' + ds + '&end=' + ds;
    }
    return 'days=' + state.range;
  }

  function ymd(d) {
    var m = ('0' + (d.getMonth() + 1)).slice(-2);
    var dd = ('0' + d.getDate()).slice(-2);
    return d.getFullYear() + '-' + m + '-' + dd;
  }

  function scopeParam() {
    return (state.scope === 'all' && state.isAdmin) ? '&all_users=1' : '';
  }

  // ===== 加载数据 =====
  function loadAll() {
    if (state.loading) return;
    state.loading = true;
    $('btn-refresh').disabled = true;

    var rp = rangeParams();
    var sp = scopeParam();
    var qp = state.q ? '&q=' + encodeURIComponent(state.q) : '';

    Promise.all([
      apiGet('/api/usage/summary?' + rp + sp),
      apiGet('/api/usage/sessions?' + rp + sp + '&page=' + state.page + '&size=' + state.size + qp),
    ]).then(function (out) {
      state.summary = out[0];
      state.sessions = out[1];
      _modelLabels = out[1].model_labels || {};
      renderSummary(out[0]);
      renderChart(out[0]);
      renderModelBreakdown(out[0]);
      renderTable(out[1]);
      renderPager(out[1]);
    }).catch(function (e) {
      $('table-body').innerHTML = '<tr><td colspan="9" class="err">加载失败：' +
        escapeHtml(e.message) + '</td></tr>';
    }).then(function () {
      state.loading = false;
      $('btn-refresh').disabled = false;
    });
  }

  function renderSummary(d) {
    var t = d.total || {};
    var calls = t.calls || 0;
    var est = t.estimated_calls || 0;

    // ============================================================
    // 双轨分离：主数字只显示「服务商认定的量」
    // ============================================================
    // 以前这里显示的是 精确+估算 的合计数，读者无法判断哪个数字能拿去对账。
    // 现在主推精确值，估算的部分挪到副标注 —— 两件事分得清，不必猜。
    var totalAll = t.total_tokens || 0;
    var totalExact = (t.total_exact === null || t.total_exact === undefined)
      ? totalAll : t.total_exact;
    var estTokens = Math.max(0, totalAll - totalExact);

    setKpiNum('kpi-total', totalExact);
    setKpiNum('kpi-prompt', t.prompt_tokens || 0);
    setKpiNum('kpi-completion', t.completion_tokens || 0);
    setKpiNum('kpi-calls', calls);

    // 日均 + 区间
    var dayCount = (d.days || []).length || 1;
    var meta = '日均 ' + fmtShort(Math.round(totalExact / dayCount));
    if (estTokens > 0) meta += ' · 另有 ' + fmtShort(estTokens) + ' 属估算';
    $('kpi-total-meta').textContent = meta;
    $('kpi-cached').textContent = '缓存命中 ' + fmtShort(t.cached_tokens || 0);
    $('kpi-reasoning').textContent = '含思考 ' + fmtShort(t.reasoning_tokens || 0);

    // 缓存命中率 = 命中缓存的输入 token / 服务商回传的输入 token
    var rate = t.cache_rate;
    if (rate === null || rate === undefined) {
      $('kpi-cache-rate').textContent = '—';
      $('kpi-cache-meta').textContent = '窗口内没有可统计的输入';
    } else {
      $('kpi-cache-rate').textContent = fmtRate(rate);
      $('kpi-cache-meta').textContent = '命中 ' + fmtShort(t.cached_exact || 0) +
        ' / 输入 ' + fmtShort(t.prompt_exact || 0);
    }

    if (est > 0) {
      var pct = Math.round(est / calls * 100);
      var pctText = pct === 0 ? '<1' : String(pct);
      $('kpi-est').innerHTML = '<span class="tag-est">其中 ' + est + ' 次为估算（' + pctText + '%）</span>';
    } else {
      $('kpi-est').innerHTML = '&nbsp;';
    }

    var label = (d.start && d.end)
      ? (d.start === d.end ? d.start : d.start + ' ~ ' + d.end)
      : '';
    $('range-label').textContent = label;

    // 中断/异常任务提示（近 7 天，与查询范围解耦）。全为 0 时不显示 —— 不制造噪音。
    var pending = d.pending || {};
    var note = $('pending-note');
    if (note) {
      var interrupted = pending.interrupted || 0;
      var orphan = pending.orphan || 0;
      if (interrupted > 0 || orphan > 0) {
        var parts = [];
        if (interrupted > 0) parts.push(interrupted + ' 次任务中断（token 可能未入账）');
        if (orphan > 0) parts.push(orphan + ' 次未记账（任务完成但无用量记录）');
        note.textContent = '近 7 天：' + parts.join('，');
        note.classList.remove('hidden');
      } else {
        note.textContent = '';
        note.classList.add('hidden');
      }
    }
  }

  // ===== 模型消耗拆分 =====
  // 回答「token 花在哪家模型、走的是谁的 Key」：平台 Key = 平台成本，
  // 用户自带 Key = 自付。同名模型可能平台池一份、用户自配一份，故按「模型+供应商」分行。
  function renderModelBreakdown(d) {
    var body = $('model-body');
    var card = $('model-card');
    if (!body) return;
    var models = (d && d.models) || [];
    if (card) card.classList.toggle('hidden', models.length === 0);
    if (models.length === 0) {
      body.innerHTML = '<tr><td colspan="7" class="empty">窗口内暂无消耗记录</td></tr>';
      return;
    }
    body.innerHTML = models.map(function (m) {
      var label = modelLabel(m.model, m.provider_name);
      var keyTag = m.owner === 'user'
        ? '<span class="key-tag self">用户自付</span>'
        : (m.owner === 'platform'
            ? '<span class="key-tag">平台 Key</span>'
            : '<span class="key-tag">未知</span>');
      var rate = (m.cache_rate === null || m.cache_rate === undefined) ? '—' : fmtRate(m.cache_rate);
      var estNote = m.estimated_calls
        ? '<div class="m-id">含 ' + m.estimated_calls + ' 次估算</div>' : '';
      return '<tr>'
        + '<td><div class="m-name">' + escapeHtml(label) + '</div>'
        + '<div class="m-id">' + escapeHtml(m.model) + '</div></td>'
        + '<td>' + escapeHtml(m.provider_name || '—') + '</td>'
        + '<td>' + keyTag + '</td>'
        + '<td class="num">' + fmt(m.calls) + estNote + '</td>'
        + '<td class="num">' + fmtShort(m.total_tokens) + '</td>'
        + '<td class="num">' + fmtShort(m.prompt_tokens) + ' / ' + fmtShort(m.completion_tokens) + '</td>'
        + '<td class="num">' + rate + '</td>'
        + '</tr>';
    }).join('');
  }

  // ===== 柱状图（原生 SVG，无外部依赖）=====
  function renderChart(d) {
    var svg = $('chart');
    var days = d.days || [];
    var W = Math.max(320, svg.parentNode.clientWidth - 36);
    var H = 260;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.removeAttribute('preserveAspectRatio');
    svg.style.height = H + 'px';

    var padL = 54, padR = 14, padT = 14, padB = 28;
    var innerW = W - padL - padR;
    var innerH = H - padT - padB;

    var keys = null;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    if (!days.length) {
      svg.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2) +
        '" text-anchor="middle" class="axis-text">暂无数据</text>';
      return;
    }
    var hasData = false;
    for (var k = 0; k < days.length; k++) {
      if ((days[k].total_tokens || 0) > 0) { hasData = true; break; }
    }
    if (!hasData) {
      svg.innerHTML = '<text x="' + (W / 2) + '" y="' + (H / 2 - 8) +
        '" text-anchor="middle" class="axis-text">这段时间还没有消耗记录</text>' +
        '<text x="' + (W / 2) + '" y="' + (H / 2 + 14) +
        '" text-anchor="middle" class="axis-text" style="font-size:11px;opacity:.75">发起一次对话后，这里会自动出现数据</text>';
      return;
    }

    var maxV = 0;
    days.forEach(function (x) { maxV = Math.max(maxV, x.total_tokens || 0); });
    if (maxV <= 0) maxV = 1;

    // 先定「刻度步长」，再让顶格 = 4 步，保证刻度是整数档（避免出现 263万 这种刻度）
    var ticks = 4;
    var step = niceStep(maxV / ticks);
    maxV = step * ticks;

    var NS = 'http://www.w3.org/2000/svg';
    function el(name, attrs, cls) {
      var n = document.createElementNS(NS, name);
      for (var k in attrs) if (attrs.hasOwnProperty(k)) n.setAttribute(k, attrs[k]);
      if (cls) n.setAttribute('class', cls);
      return n;
    }

    // 横向网格 + Y 轴刻度
    for (var i = 0; i <= ticks; i++) {
      var v = maxV * i / ticks;
      var y = padT + innerH - (innerH * i / ticks);
      svg.appendChild(el('line', { x1: padL, y1: y, x2: W - padR, y2: y }, 'grid'));
      var tx = el('text', {
        x: padL - 8, y: y + 4, 'text-anchor': 'end'
      }, 'axis-text');
      tx.textContent = fmtShort(Math.round(v));
      svg.appendChild(tx);
    }

    var n = days.length;
    var slot = innerW / n;
    // 上限 56px：否则「今天 / 近 7 天」只有几根柱子时会摊成很宽的大色块
    var barW = Math.max(2, Math.min(56, slot - Math.min(8, slot * 0.28)));
    var labelEvery = Math.ceil(n / 12);

    days.forEach(function (day, idx) {
      var x = padL + slot * idx + (slot - barW) / 2;
      // 两段堆叠：输出在下（深蓝细条），输入在上（浅蓝大段）
      var inH = innerH * (day.prompt_tokens || 0) / maxV;
      var outH = innerH * (day.completion_tokens || 0) / maxV;
      var baseY = padT + innerH;
      var yOut = baseY - outH;
      var yIn = yOut - inH;

      var g = el('g', {});
      // 透明热区，便于 hover
      g.appendChild(el('rect', {
        x: padL + slot * idx, y: padT, width: slot, height: innerH,
        fill: 'transparent'
      }, 'hit'));

      if (inH > 0) {
        g.appendChild(el('rect', {
          x: x, y: yIn, width: barW, height: inH, rx: Math.min(2, inH / 2)
        }, 'bar bar-in'));
      }
      if (outH > 0) {
        g.appendChild(el('rect', {
          x: x, y: yOut, width: barW, height: outH, rx: Math.min(2, outH / 2)
        }, 'bar bar-out'));
      }

      g.addEventListener('mouseenter', function (ev) {
        showTip(ev, day);
      });
      g.addEventListener('mousemove', function (ev) { moveTip(ev); });
      g.addEventListener('mouseleave', hideTip);
      svg.appendChild(g);

      if (idx % labelEvery === 0 || idx === n - 1) {
        var lb = el('text', {
          x: padL + slot * idx + slot / 2,
          y: H - 8,
          'text-anchor': 'middle'
        }, 'axis-text');
        lb.textContent = dateLabel(day.date);
        svg.appendChild(lb);
      }
    });
  }

  function niceStep(v) {
    if (!(v > 0)) return 1;
    var mag = Math.pow(10, Math.floor(Math.log10(v)));
    var n = v / mag;
    var ladder = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < ladder.length; i++) {
      if (n <= ladder[i] + 1e-9) return ladder[i] * mag;
    }
    return 10 * mag;
  }

  var _tipDay = null;
  function showTip(ev, day) {
    _tipDay = day;
    var tip = $('tip');
    tip.innerHTML = '<b>' + day.date + '</b><br>' +
      '输入 ' + fmt(day.prompt_tokens) + '<br>' +
      '输出 ' + fmt(day.completion_tokens) + '<br>' +
      '缓存命中 ' + fmt(day.cached_tokens) +
        (day.cache_rate === null || day.cache_rate === undefined ? '' : '（' + fmtRate(day.cache_rate) + '）') + '<br>' +
      '合计 ' + fmt(day.total_tokens) + '（' + day.calls + ' 次调用）';
    tip.style.opacity = '1';
    moveTip(ev);
  }
  function moveTip(ev) {
    var tip = $('tip');
    var card = $('chart-card');
    var r = card.getBoundingClientRect();
    var x = ev.clientX - r.left + 14;
    var y = ev.clientY - r.top + 14;
    if (x + tip.offsetWidth > r.width - 6) x = ev.clientX - r.left - tip.offsetWidth - 14;
    tip.style.left = Math.max(6, x) + 'px';
    tip.style.top = Math.max(6, y) + 'px';
  }
  function hideTip() {
    _tipDay = null;
    $('tip').style.opacity = '0';
  }

  // ===== 表格 =====
  function renderTable(d) {
    var body = $('table-body');
    body.innerHTML = '';
    var items = (d && d.items) || [];

    if (!items.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">' +
        (state.q ? '没有标题包含「' + escapeHtml(state.q) + '」的会话' : '这段时间还没有消耗记录') +
        '</td></tr>';
      return;
    }

    items.forEach(function (it) {
      var tr = document.createElement('tr');

      var tdTime = document.createElement('td');
      tdTime.className = 'time-cell';
      tdTime.textContent = fmtTime(it.last_at);
      tr.appendChild(tdTime);

      var tdTitle = document.createElement('td');
      tdTitle.className = 'sess-title';
      var sid = it.session_id || '';
      var shortId = sid ? sid.slice(-8) : '';
      var hasTitle = !!(it.title && it.title.trim()) &&
        it.title !== '（已删除的会话）' && it.title !== '（无会话）';
      var btn = document.createElement('button');
      // 标题是人能对应的名字，优先；没有标题时才退回短 ID，避免多行「未命名会话」无法区分
      btn.textContent = hasTitle ? it.title : ('会话 ' + (shortId || '—'));
      btn.title = (hasTitle ? it.title + '\n' : '') +
        (sid ? 'ID：' + sid + '\n' : '') + '点击查看逐次调用';
      btn.addEventListener('click', function () { openDetail(it); });
      tdTitle.appendChild(btn);
      if (hasTitle && shortId) {
        var idLine = document.createElement('div');
        idLine.className = 'sess-id';
        idLine.textContent = shortId;
        tdTitle.appendChild(idLine);
      }
      tr.appendChild(tdTitle);

      // 模型：一个会话里可能切换过多个模型，按消耗降序列出；
      // 超过 2 个时只显示前两个，其余折叠成 +N（悬停看完整清单）
      var tdModel = document.createElement('td');
      var mchips = document.createElement('div');
      mchips.className = 'model-chips';
      var models = (it.models || []).filter(function (m) { return m && m.model; });
      if (!models.length && it.model) models = [{ model: it.model, calls: it.calls, total_tokens: it.total_tokens }];
      if (!models.length) {
        mchips.textContent = '—';
      } else {
        models.slice(0, 2).forEach(function (m) {
          var c = document.createElement('span');
          c.className = 'model-chip' + (m.owner === 'user' ? ' self-pay' : '');
          var label = modelLabel(m.model, m.provider_name);
          c.textContent = label + (m.owner === 'user' ? ' · 自付' : '');
          var tip = label;
          if (m.provider_name) tip += '（' + m.provider_name + '）';
          if (m.owner === 'user') tip += ' · 使用用户自带 Key，费用不由平台承担';
          else if (m.owner === 'platform') tip += ' · 平台 Key';
          if (m.calls) tip += '：' + m.calls + ' 次调用，' + fmt(m.total_tokens) + ' token';
          c.title = tip;
          mchips.appendChild(c);
        });
        if (models.length > 2) {
          var more = document.createElement('span');
          more.className = 'model-chip';
          more.textContent = '+' + (models.length - 2);
          more.title = models.map(function (m) {
            return modelLabel(m.model, m.provider_name) + (m.owner === 'user' ? '（自付）' : '');
          }).join('\n');
          mchips.appendChild(more);
        }
      }
      tdModel.appendChild(mchips);
      tr.appendChild(tdModel);

      var tdSrc = document.createElement('td');
      var chips = document.createElement('div');
      chips.className = 'src-chips';
      (it.sources || []).forEach(function (s) {
        var c = document.createElement('span');
        c.className = 'chip ' + s.source;
        c.textContent = s.label + ' ' + fmtShort(s.total_tokens);
        var chipTip = s.label + '：' + fmt(s.total_tokens) + ' token，' + s.calls + ' 次调用';
        if (s.cache_rate !== null && s.cache_rate !== undefined) {
          chipTip += '，缓存命中率 ' + fmtRate(s.cache_rate);
        }
        c.title = chipTip;
        chips.appendChild(c);
      });
      if (!(it.sources || []).length) chips.textContent = '—';
      tdSrc.appendChild(chips);
      tr.appendChild(tdSrc);

      tr.appendChild(numCell(it.prompt_tokens));

      var tdRate = document.createElement('td');
      tdRate.className = 'num';
      tdRate.textContent = fmtRate(it.cache_rate);
      tdRate.title = '命中 ' + fmt(it.cached_exact || 0) + ' / 输入 ' + fmt(it.prompt_exact || 0);
      tr.appendChild(tdRate);

      tr.appendChild(numCell(it.completion_tokens));

      var tdTotal = numCell(it.total_tokens);
      if (it.estimated_calls > 0) {
        var tag = document.createElement('span');
        tag.className = 'tag-est';
        tag.textContent = '估算';
        tag.title = '该会话有 ' + it.estimated_calls + ' 次调用未拿到服务商 usage';
        tag.style.marginLeft = '6px';
        tdTotal.appendChild(tag);
      }
      tr.appendChild(tdTotal);

      tr.appendChild(numCell(it.calls));
      body.appendChild(tr);
    });
  }

  function numCell(v) {
    var td = document.createElement('td');
    td.className = 'num';
    td.textContent = fmt(v);
    return td;
  }

  function renderPager(d) {
    var total = d.total || 0;
    var pages = d.pages || 1;
    var page = d.page || 1;

    $('page-info').textContent = total
      ? '共 ' + total + ' 个会话，第 ' + page + ' / ' + pages + ' 页'
      : '共 0 条';

    var wrap = $('page-buttons');
    wrap.innerHTML = '';

    function mkBtn(label, target, disabled, active) {
      var b = document.createElement('button');
      b.textContent = label;
      if (disabled) b.disabled = true;
      if (active) b.className = 'active';
      b.addEventListener('click', function () {
        if (disabled || active) return;
        state.page = target;
        loadAll();
      });
      wrap.appendChild(b);
    }

    mkBtn('上一页', Math.max(1, page - 1), page <= 1, false);

    var start = Math.max(1, page - 2);
    var end = Math.min(pages, start + 4);
    start = Math.max(1, end - 4);
    for (var p = start; p <= end; p++) {
      mkBtn(String(p), p, false, p === page);
    }

    mkBtn('下一页', Math.min(pages, page + 1), page >= pages, false);
  }

  // ===== 会话明细弹窗 =====
  var _detailRows = [];
  var _detailFilter = '';

  function openDetail(it) {
    var modal = $('detail-modal');
    var body = $('detail-body');
    $('detail-title').textContent = '调用明细 · ' + (it.title || '未命名会话');
    body.innerHTML = '<div class="loading">加载中…</div>';
    modal.classList.remove('hidden');
    _detailRows = [];
    _detailFilter = '';

    var url = '/api/usage/session/' + encodeURIComponent(it.session_id || '') + '?' + scopeParam().replace(/^&/, '');
    apiGet(url).then(function (d) {
      var rows = d.items || [];
      _detailRows = rows;
      if (!rows.length) {
        body.innerHTML = '<div class="empty">没有明细记录</div>';
        return;
      }
      renderDetail();
    }).catch(function (e) {
      body.innerHTML = '<div class="err">加载失败：' + escapeHtml(e.message) + '</div>';
    });
  }

  // 模型 ID → 可读显示名（后端随会话列表一起给出）。
  // 账本永远记 model ID，显示名只用于展示：显示名改了，历史账单也不该跟着变。
  var _modelLabels = {};

  function modelLabel(modelId, providerName) {
    if (!modelId) return '（未知）';
    var k = modelId + '||' + (providerName || '');
    if (_modelLabels[k]) return _modelLabels[k];
    if (_modelLabels[modelId]) return _modelLabels[modelId];
    return modelId;
  }

  function detailModelKey(r) {
    return (r.model || '') + '||' + (r.provider_name || '');
  }

  function detailModelText(r) {
    return modelLabel(r.model, r.provider_name);
  }

  function renderDetail() {
    var body = $('detail-body');
    var rows = _detailRows;

    // 会话内可能切换过多个模型 → 用下拉列表按模型筛选
    // 筛选值用「模型 ID + 供应商」组合键：不同供应商可能有同名模型，只按 ID 会混在一起
    var models = [];
    rows.forEach(function (r) {
      var k = detailModelKey(r);
      if (models.indexOf(k) < 0) models.push(k);
    });

    var bar = '<div class="detail-bar">' +
      '<label for="detail-model">模型</label>' +
      '<select id="detail-model"><option value="">全部模型</option>';
    models.forEach(function (k) {
      var n = rows.filter(function (r) { return detailModelKey(r) === k; }).length;
      var sample = rows.filter(function (r) { return detailModelKey(r) === k; })[0] || {};
      bar += '<option value="' + escapeHtml(k) + '"' +
        (k === _detailFilter ? ' selected' : '') + '>' +
        escapeHtml(detailModelText(sample)) + '（' + n + ' 次）</option>';
    });
    bar += '</select></div>';

    var shown = rows.filter(function (r) {
      return !_detailFilter || detailModelKey(r) === _detailFilter;
    });

    var sum = 0;
    shown.forEach(function (r) { sum += Number(r.total_tokens || 0); });

    var html = bar + '<div class="table-wrap"><table><thead><tr>' +
      '<th>时间</th><th>来源</th><th>模型</th><th class="num">输入</th><th class="num">输出</th>' +
      '<th class="num">缓存命中</th><th class="num">思考</th><th class="num">合计</th><th class="num">耗时</th>' +
      '</tr></thead><tbody>';
    shown.forEach(function (r) {
      var est = r.is_estimated ? ' <span class="tag-est">估算</span>' : '';
      // 估算行没有缓存概念，不显示 0%，避免误读成「缓存完全没命中」
      var rateTxt = (r.is_estimated || !r.prompt_tokens)
        ? ''
        : '<span class="rate-sub"> · ' + fmtRate(r.cached_tokens / r.prompt_tokens * 100) + '</span>';
      html += '<tr>' +
        '<td class="time-cell">' + escapeHtml(r.created_at || '') + '</td>' +
        '<td><span class="chip ' + escapeHtml(r.source) + '">' +
          escapeHtml(r.source_label || r.source) + est + '</span></td>' +
        '<td class="model-cell">' + escapeHtml(detailModelText(r)) + '</td>' +
        '<td class="num">' + fmt(r.prompt_tokens) + '</td>' +
        '<td class="num">' + fmt(r.completion_tokens) + '</td>' +
        '<td class="num">' + fmt(r.cached_tokens) + rateTxt + '</td>' +
        '<td class="num">' + fmt(r.reasoning_tokens) + '</td>' +
        '<td class="num">' + fmt(r.total_tokens) + '</td>' +
        '<td class="num">' + (r.duration_ms ? (r.duration_ms / 1000).toFixed(1) + 's' : '—') + '</td>' +
        '</tr>';
    });
    html += '</tbody></table></div>' +
      '<div class="detail-foot">显示 ' + shown.length + ' / ' + rows.length +
      ' 次调用，合计 ' + fmt(sum) + ' token</div>';
    body.innerHTML = html;

    var sel = $('detail-model');
    if (sel) {
      sel.addEventListener('change', function () {
        _detailFilter = this.value;
        renderDetail();
      });
    }
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // ===== 返回 =====
  // 在对话页的浮层 iframe 里打开时：只通知父页面收起浮层，父页面状态与滚动位置完全保留
  // 独立打开（直接访问 /usage）时：回到来源页，没有来源就回对话页
  function goBack() {
    if (window.parent && window.parent !== window) {
      try {
        window.parent.postMessage({ type: 'usage:close' }, window.location.origin);
        return;
      } catch (e) { /* 跨域受限时降级为普通跳转 */ }
    }
    var sameOrigin = document.referrer && document.referrer.indexOf(window.location.origin + '/') === 0;
    if (sameOrigin && window.history.length > 1) {
      window.history.back();
    } else {
      window.location.href = '/';
    }
  }

  // ===== 事件绑定 =====
  function bind() {
    $('btn-back').addEventListener('click', goBack);

    // 会话名称搜索：输入停顿后才查，避免每敲一个字打一次库
    var qTimer = null;
    var qInput = $('sess-q');
    if (qInput) {
      qInput.addEventListener('input', function () {
        var v = this.value.trim();
        if (qTimer) clearTimeout(qTimer);
        qTimer = setTimeout(function () {
          if (v === state.q) return;
          state.q = v;
          state.page = 1;
          loadAll();
        }, 300);
      });
      qInput.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        if (qTimer) clearTimeout(qTimer);
        var v = this.value.trim();
        if (v === state.q) return;
        state.q = v;
        state.page = 1;
        loadAll();
      });
    }

    $('range-seg').addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      Array.prototype.forEach.call(this.children, function (b) {
        b.classList.toggle('active', b === btn);
      });
      var r = btn.dataset.range;
      if (r === 'custom') {
        $('custom-range').style.display = 'inline-flex';
        return;
      }
      $('custom-range').style.display = 'none';
      state.range = r;
      state.page = 1;
      loadAll();
    });

    $('btn-apply-range').addEventListener('click', function () {
      var s = $('date-start').value;
      var e = $('date-end').value;
      if (!s || !e) {
        alert('请选择开始和结束日期');
        return;
      }
      state.range = 'custom';
      state.start = s;
      state.end = e;
      state.page = 1;
      loadAll();
    });

    $('scope-seg').addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      Array.prototype.forEach.call(this.children, function (b) {
        b.classList.toggle('active', b === btn);
      });
      state.scope = btn.dataset.scope;
      state.page = 1;
      loadAll();
    });

    $('btn-refresh').addEventListener('click', function () {
      state.page = 1;
      loadAll();
    });

    $('detail-close').addEventListener('click', closeModal);
    $('detail-mask').addEventListener('click', closeModal);
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      // 先关明细弹窗；没有弹窗且处于浮层内时，按 Esc 直接回对话
      if (!$('detail-modal').classList.contains('hidden')) { closeModal(); return; }
      if (window.parent && window.parent !== window) goBack();
    });

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (!state.summary) return;
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () { renderChart(state.summary); }, 150);
    });
  }

  function closeModal() {
    $('detail-modal').classList.add('hidden');
  }

  // ===== 启动 =====
  function boot() {
    initTheme();
    var today = new Date();
    $('date-end').value = ymd(today);
    $('date-start').value = ymd(new Date(today.getTime() - 29 * 864e5));
    bind();

    // 管理员才能看全平台
    apiGet('/api/auth/me').then(function (me) {
      state.isAdmin = (me && me.user_type === 'admin');
      if (state.isAdmin) $('scope-wrap').style.display = 'inline-flex';
    }).catch(function () { /* 忽略：仍可看自己的数据 */ })
      .then(loadAll);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
