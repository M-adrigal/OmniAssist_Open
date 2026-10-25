/* 12-theme-init.js
 * 主题与初始化：主题切换、DOMContentLoaded 入口（必须是最后一个加载）
 *
 * 由 static/app.js 拆分而来（原第 4254-4980 行，共 4980 行）。
 * 拆分方式：连续切分 + 按原顺序加载，行为与拆分前等价。
 * ⚠️ 本文件与其它 js/ 文件共享全局作用域（无模块系统），
 *    新增符号前请确认不与其它文件重名（可用 scripts/check_static.py 的 JS 重名检查）。
 */
function setThemeIcon(mode) {
  const btn = $('#theme-toggle');
  if (btn) btn.dataset.mode = mode;
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark') {
    document.documentElement.classList.add('dark');
    setThemeIcon('dark');
    $('#theme-toggle').title = '当前：暗色模式（点击切换）';
  } else if (saved === 'light') {
    document.documentElement.classList.remove('dark');
    setThemeIcon('light');
    $('#theme-toggle').title = '当前：亮色模式（点击切换）';
  } else {
    applySystemTheme();
    $('#theme-toggle').title = '当前：自动模式（点击切换）';
  }
}

function applySystemTheme() {
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  if (prefersDark) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }
  setThemeIcon('auto');
}

function toggleTheme() {
  const saved = localStorage.getItem('theme');
  if (!saved || saved === 'auto') {
    localStorage.setItem('theme', 'dark');
    document.documentElement.classList.add('dark');
    setThemeIcon('dark');
    $('#theme-toggle').title = '当前：暗色模式（点击切换）';
  } else if (saved === 'dark') {
    localStorage.setItem('theme', 'light');
    document.documentElement.classList.remove('dark');
    setThemeIcon('light');
    $('#theme-toggle').title = '当前：亮色模式（点击切换）';
  } else {
    localStorage.removeItem('theme');
    applySystemTheme();
    $('#theme-toggle').title = '当前：自动模式（点击切换）';
  }
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  const saved = localStorage.getItem('theme');
  if (!saved || saved === 'auto') {
    if (e.matches) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
    setThemeIcon('auto');
  }
});

// ===== 事件绑定 =====
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initCapCards();
  // 消息「重新生成」：只挂在最后一条回答上，事件委托到 document，后补进来的按钮也生效
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.msg-act[data-regenerate]');
    if (!btn) return;
    regenerateLastTurn();
  });

  // 消息「复制」：clipboard API 只在 https / localhost 可用，其余场景走 textarea 兜底
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.msg-act[data-copy]');
    if (!btn) return;
    const area = btn.closest('.message') && btn.closest('.message').querySelector('.answer-area');
    if (!area) return;
    const text = (area.innerText || '').trim();
    if (!text) return;

    const done = () => {
      // 按钮里是图标而不是文字，所以用类切换：对勾替换复制图标，1.4s 后还原
      btn.classList.add('copied');
      btn.title = '已复制';
      setTimeout(() => { btn.classList.remove('copied'); btn.title = '复制'; }, 1400);
    };
    const fallback = () => {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;top:-9999px;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        done();
      } catch (err) {
        showToast('复制失败，请手动选择文本', 'error');
      }
    };

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else {
      fallback();
    }
  });
  // 刷新前已查看过的完成任务，恢复已读标记，避免完成气泡重复弹出
  _loadAcknowledged();

  // 文件库预览「返回文件列表」按钮（事件委托，兼容动态生成的按钮）
  document.addEventListener('click', (e) => {
    if (e.target.closest('#preview-back-btn')) {
      e.preventDefault();
      returnToFileList();
    }
  });

  $('#theme-toggle').addEventListener('click', toggleTheme);

  // ===== 侧栏折叠 =====
  // 折叠＝给 #sidebar 一个等于自身宽度的负 margin，让它整块滑出视口。
  // 不用 display:none / width:0：前者没有过渡，后者会把里面的文字一路挤变形。
  const SIDEBAR_KEY = 'sidebarCollapsed';
  const appEl = $('#app');
  const sidebarEl = $('#sidebar');
  const collapseBtn = $('#btn-collapse-sidebar');

  function setSidebarCollapsed(collapsed, persist) {
    appEl.classList.toggle('sidebar-collapsed', collapsed);
    // 侧栏只是移出视口，里面的按钮仍然能被 Tab 命中，必须同时移出焦点顺序。
    // 先手动 blur：对"内部还有焦点元素"的子树设 inert / aria-hidden 会被浏览器拒绝或报警告。
    if (collapsed) {
      if (document.activeElement && sidebarEl.contains(document.activeElement)) document.activeElement.blur();
      sidebarEl.setAttribute('inert', '');
      sidebarEl.setAttribute('aria-hidden', 'true');
    } else {
      sidebarEl.removeAttribute('inert');
      sidebarEl.removeAttribute('aria-hidden');
    }
    collapseBtn.setAttribute('aria-expanded', String(!collapsed));
    if (persist) {
      try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0'); } catch (e) { /* 隐私模式下写不了，忽略 */ }
    }
  }

  // 记住上次的状态：每次刷新都重新展开一遍会很烦
  let _sidebarCollapsed = false;
  try { _sidebarCollapsed = localStorage.getItem(SIDEBAR_KEY) === '1'; } catch (e) { /* ignore */ }
  // 首帧不许有过渡：否则恢复"已收起"时会先滑一遍再到位。等两帧后恢复动画；
  // 后台标签页里 rAF 会被节流甚至不触发，补一个 setTimeout 兜底，别让过渡永久失效。
  appEl.classList.add('no-sidebar-anim');
  setSidebarCollapsed(_sidebarCollapsed, false);
  requestAnimationFrame(() => requestAnimationFrame(() => appEl.classList.remove('no-sidebar-anim')));
  setTimeout(() => appEl.classList.remove('no-sidebar-anim'), 120);

  collapseBtn.addEventListener('click', (e) => {
    setSidebarCollapsed(true, true);
    // 只有键盘触发（Enter / Space，detail 为 0）才搬焦点：按钮马上要变成 inert，
    // 不搬的话焦点会掉回文档开头。鼠标点击不搬，否则会留一个像"选中态"的蓝圈。
    if (e.detail === 0) $('#btn-expand-sidebar').focus();
  });
  $('#btn-expand-sidebar').addEventListener('click', (e) => {
    setSidebarCollapsed(false, true);
    if (e.detail === 0) collapseBtn.focus();
  });
  $('#btn-rail-new').addEventListener('click', createSession);

  if (localStorage.getItem('thinkingMode')) {
    state.thinkingMode = localStorage.getItem('thinkingMode') || 'low';
  }
  updateThinkingModeUI();

  loadSessions();

  // 恢复上次活跃的会话（刷新后不再丢失当前会话）
  const _savedSid = _loadCurrentSessionId();
  if (_savedSid) {
    // 延迟一帧等待 loadSessions 渲染完成
    requestAnimationFrame(() => {
      const exists = state.sessions.some(s => s.id === _savedSid);
      if (exists) { switchSession(_savedSid); }
      else { localStorage.removeItem(_SID_KEY); }  // session 已被删除，清除残留
    });
  }

  $('#btn-new-session').addEventListener('click', createSession);
  $('#btn-settings').addEventListener('click', toggleDrawer);
  $('#btn-close-drawer').addEventListener('click', () => $('#settings-drawer').classList.add('hidden'));

  // 侧栏本地过滤：只筛当前列表的标题，不发请求
  const sessionFilter = $('#session-filter');
  if (sessionFilter) {
    sessionFilter.addEventListener('input', () => {
      state.sessionFilter = sessionFilter.value;
      renderSessions();
    });
    sessionFilter.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        sessionFilter.value = '';
        state.sessionFilter = '';
        renderSessions();
        sessionFilter.blur();
      }
    });
  }

  // 搜索对话弹窗
  const searchDialog = $('#modal-search-dialog');
  const searchDialogInput = $('#search-dialog-input');
  const searchDialogResults = $('#search-dialog-results');
  let _searchDialogTimer = null;

  // 侧栏里的搜索按钮和折叠后胶囊里的搜索按钮走同一段逻辑
  function openSearchDialog() {
    searchDialog.classList.remove('hidden');
    setTimeout(() => searchDialogInput.focus(), 100);
    searchDialogInput.value = '';
    searchDialogResults.innerHTML = '<p class="search-dialog-hint">输入关键词搜索历史对话</p>';
  }

  $('#btn-search-dialog').addEventListener('click', openSearchDialog);
  $('#btn-rail-search').addEventListener('click', openSearchDialog);

  $('#btn-search-dialog-close').addEventListener('click', () => {
    searchDialog.classList.add('hidden');
  });

  searchDialog.querySelector('.modal-overlay').addEventListener('click', () => {
    searchDialog.classList.add('hidden');
  });

  searchDialogInput.addEventListener('input', () => {
    clearTimeout(_searchDialogTimer);
    const query = searchDialogInput.value.trim();
    if (!query) {
      searchDialogResults.innerHTML = '<p class="search-dialog-hint">输入关键词搜索历史对话</p>';
      return;
    }
    _searchDialogTimer = setTimeout(() => searchSessionsDialog(query), 300);
  });

  async function searchSessionsDialog(query) {
    try {
      const sessions = await API.get(`/api/sessions/search?q=${encodeURIComponent(query)}`);
      if (sessions.length === 0) {
        searchDialogResults.innerHTML = '<p class="search-dialog-empty">未找到匹配的对话</p>';
        return;
      }
      searchDialogResults.innerHTML = sessions.map(s => {
        const date = new Date(s.created_at * 1000);
        const timeStr = date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
        return `
          <div class="search-dialog-item" data-id="${s.id}">
            <div class="item-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            </div>
            <div class="item-info">
              <div class="item-title">${escapeHtml(s.title || '新对话')}</div>
              <div class="item-meta">${timeStr} · ${s.message_count || 0} 条消息</div>
            </div>
            <div class="item-enter">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            </div>
          </div>
        `;
      }).join('');

      searchDialogResults.querySelectorAll('.search-dialog-item').forEach(el => {
        el.addEventListener('click', () => {
          searchDialog.classList.add('hidden');
          switchSession(el.dataset.id);
        });
      });
    } catch (e) {
      searchDialogResults.innerHTML = '<p class="search-dialog-empty">搜索失败: ' + escapeHtml(e.message) + '</p>';
    }
  }

  // 文件库搜索（事件委托：骨架重建后旧 input 会被销毁，绑定在 document 上可持久生效）
  let cloudSearchTimer;
  document.addEventListener('input', (e) => {
    if (e.target && e.target.id === 'cloud-search') {
      clearTimeout(cloudSearchTimer);
      cloudSearchTimer = setTimeout(() => loadCloudFiles(), 300);
    }
  });

  // 文件库「文档类型」「来源方式」筛选（事件委托，同上原因）
  document.addEventListener('change', (e) => {
    if (e.target && (e.target.id === 'cloud-type-filter' || e.target.id === 'cloud-source-filter')) {
      _applyCloudFilters();
    }
  });

  const pickerSearch = $('#picker-search');
  if (pickerSearch) {
    let pickerSearchTimer;
    pickerSearch.addEventListener('input', () => {
      clearTimeout(pickerSearchTimer);
      pickerSearchTimer = setTimeout(() => loadPickerFiles(pickerSearch.value), 300);
    });
  }

  const btnPickerConfirm = $('#btn-picker-confirm');
  if (btnPickerConfirm) {
    btnPickerConfirm.addEventListener('click', confirmPickerSelection);
  }

  const btnPickerCancel = $('#btn-picker-cancel');
  if (btnPickerCancel) {
    btnPickerCancel.addEventListener('click', () => {
      closeModal('modal-file-picker');
    });
  }

  $('#btn-send').addEventListener('click', () => {
    if (state.isStreaming || (state.subscribingSessionId === state.currentSessionId)) {
      stopGeneration();
    } else if (state.subscribingSessionId) {
      showToast('后台任务正在执行中，请等待完成', 'info');
    } else {
      sendMessage();
    }
  });

  $('#btn-attach').addEventListener('click', (e) => {
    e.stopPropagation();
    const menu = $('#attach-menu');
    if (!menu.classList.contains('hidden')) {
      menu.classList.add('hidden');
      if (typeof closeSkillPanel === 'function') closeSkillPanel();
      return;
    }
    const btn = $('#btn-attach');
    const rect = btn.getBoundingClientRect();
    menu.style.left = rect.left + 'px';
    menu.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
    menu.style.position = 'fixed';
    menu.classList.remove('hidden');
  });

  $$('.attach-menu-item').forEach(item => {
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      const action = item.dataset.action;
      // 「技能」是二级联动项：点它不关一级菜单，改为在菜单右缘展开技能面板；
      // 再点一次则收起（面板开关由 openSkillPanel/closeSkillPanel 内部判断）。
      if (action === 'skill') {
        if (typeof _skillPanelOpen !== 'undefined' && _skillPanelOpen) {
          closeSkillPanel();
        } else {
          openSkillPanel();
        }
        return;
      }
      // 其他项：关菜单，同时收起可能开着的技能二级面板
      $('#attach-menu').classList.add('hidden');
      if (typeof closeSkillPanel === 'function') closeSkillPanel();
      if (action === 'upload') {
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.accept = '.txt,.md,.csv,.json,.xml,.html,.css,.js,.py,.log,.yaml,.yml,.ini,.cfg,.toml,.sh,.bash,.zsh,.sql,.r,.rb,.go,.rs,.java,.c,.cpp,.h,.hpp,.ts,.tsx,.jsx,.vue,.conf,.env,.properties,.pdf,.docx,.xlsx,.pptx,.png,.jpg,.jpeg,.gif,.webp,.bmp';
        input.onchange = () => {
          if (input.files.length > 0) {
            uploadFiles(input.files);
          }
        };
        input.click();
      } else if (action === 'cloud') {
        openFilePicker();
      }
    });
  });

  // ===== 拖拽上传：拖文件到页面任意位置即弹出整屏提示，任意位置松手均可上传 =====
  (function initDropZone() {
    const overlay = $('#drop-overlay');
    if (!overlay) return;
    let dragDepth = 0;  // 进入/离开子元素会触发 dragenter/leave，用计数器避免闪烁

    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');

    const show = () => overlay.classList.remove('hidden');
    const hide = () => { dragDepth = 0; overlay.classList.add('hidden'); };

    // 在 window 级别拦截，确保拖到页面任意位置都能响应
    window.addEventListener('dragenter', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth++;
      show();
    });
    window.addEventListener('dragover', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', (e) => {
      if (!hasFiles(e)) return;
      // 离开浏览器窗口（拖出到外部）时 relatedTarget 为空，直接隐藏
      if (e.relatedTarget === null) { hide(); return; }
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) hide();
    });
    window.addEventListener('drop', (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      hide();
      const files = e.dataTransfer.files;
      if (files && files.length > 0) {
        uploadFiles(files);
      }
    });
  })();

  const input = $('#chat-input');
  let isComposing = false;

  input.addEventListener('compositionstart', () => { isComposing = true; });
  input.addEventListener('compositionend', () => { isComposing = false; });

  // ===== 粘贴图片：截图后直接在输入框 Ctrl/Cmd+V 即可作为附件上传 =====
  // 只拦截「剪贴板里含图片文件」的粘贴；纯文本粘贴放行，不干扰正常输入。
  input.addEventListener('paste', (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    const imageFiles = [];
    for (const item of items) {
      if (item.kind === 'file' && String(item.type || '').indexOf('image/') === 0) {
        const f = item.getAsFile();
        if (f) imageFiles.push(f);
      }
    }
    if (imageFiles.length === 0) return;
    e.preventDefault();
    uploadFiles(imageFiles);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (isComposing) return;
      e.preventDefault();
      if (state.commandMode && state.selectedCommandIdx >= 0) {
        const container = $('#command-suggestions');
        const selected = container.querySelector('.command-item.selected');
        if (selected) {
          const idx = parseInt(selected.dataset.idx);
          const filter = state.commandFilter.toLowerCase();
          const filtered = COMMANDS.filter(c => c.command.toLowerCase().includes(filter));
          if (filtered[idx]) selectCommand(filtered[idx]);
          return;
        }
      }
      sendMessage();
      return;
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); navigateCommand(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); navigateCommand(-1); return; }
    if (e.key === 'Escape') { hideCommandSuggestions(); return; }
  });

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
    handleCommandInput(input.value);
  });

  input.addEventListener('blur', () => {
    setTimeout(() => hideCommandSuggestions(), 150);
  });

  input.addEventListener('focus', () => {
    if (input.value.startsWith('/')) {
      state.commandMode = true;
      state.commandFilter = input.value;
      state.selectedCommandIdx = 0;
      showCommandSuggestions();
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.chat-input-container') && !e.target.closest('#command-suggestions')) {
      hideCommandSuggestions();
    }
    // 点空白处：一级菜单与技能二级面板一起收（点在面板内不关，面板有自己的 mask）
    if (!e.target.closest('#attach-menu') && !e.target.closest('#btn-attach')
        && !e.target.closest('#skill-panel')) {
      $('#attach-menu').classList.add('hidden');
      if (typeof closeSkillPanel === 'function') closeSkillPanel();
    }
  });

  // ===== Token 消耗看板浮层 =====
  // 覆盖在当前页面上，不开新标签、不做页面跳转：关闭后聊天状态与滚动位置完全不丢
  let usageOverlayOpen = false;

  function showUsageOverlay() {
    const overlay = $('#usage-overlay');
    const frame = $('#usage-frame');
    if (!overlay || !frame) return;
    if (frame.dataset.loaded !== '1') {
      frame.src = '/usage';
      frame.dataset.loaded = '1';
    }
    overlay.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    usageOverlayOpen = true;
    // 主题跟随主页面（iframe 只在首次加载时读过一次 localStorage）
    try {
      const idoc = frame.contentDocument;
      if (idoc && idoc.documentElement) {
        idoc.documentElement.classList.toggle('dark', document.documentElement.classList.contains('dark'));
      }
    } catch (e) { /* 跨域时忽略 */ }
  }

  function hideUsageOverlay() {
    const overlay = $('#usage-overlay');
    if (!overlay || overlay.classList.contains('hidden')) return;
    overlay.classList.add('hidden');
    document.body.style.overflow = '';
    usageOverlayOpen = false;
  }

  function openUsageOverlay() {
    if (usageOverlayOpen) return;
    showUsageOverlay();
    // 记一条历史：浏览器后退键是关闭看板，而不是退出当前对话
    history.pushState({ usageOverlay: true }, '');
  }

  function closeUsageOverlay() {
    if (!usageOverlayOpen) return;
    if (history.state && history.state.usageOverlay) {
      history.back(); // 由 popstate 统一收口，避免历史栈越堆越多
    } else {
      hideUsageOverlay();
    }
  }

  window.addEventListener('popstate', () => {
    if (usageOverlayOpen) hideUsageOverlay();
  });

  window.addEventListener('message', (e) => {
    if (e.origin !== window.location.origin) return;
    if (e.data && e.data.type === 'usage:close') closeUsageOverlay();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeUsageOverlay();
  });

  // 设置抽屉菜单
  $$('.drawer-item').forEach(item => {
    item.addEventListener('click', () => {
      const action = item.dataset.action;
      $('#settings-drawer').classList.add('hidden');
      if (action === 'config') { loadConfig(); openModal('modal-config'); }
      if (action === 'files') { loadFiles(); openModal('modal-files'); }
      if (action === 'cloud-files') { loadCloudFiles(); openModal('modal-cloud-files'); }
      if (action === 'skills') { loadSkills(); openModal('modal-skills'); }
      if (action === 'memory') { memLoad(); openModal('modal-memory'); }
      if (action === 'usage') { openUsageOverlay(); }
      if (action === 'change-password') { openChangePassword(); }
      if (action === 'user-management') { loadUsers(); openModal('modal-user-management'); }
      if (action === 'logout') {
        // 通知服务端吊销令牌（fire-and-forget），再清 cookie 跳转
        fetch('/api/auth/logout', { method: 'POST' }).catch(() => {});
        document.cookie = 'auth_token=; path=/; max-age=0';
        window.location.href = '/login.html';
      }
    });
  });

  // 模态框关闭
  // 关闭一律走 memTryClose：记忆弹窗有未保存改动时它会先问一句，
  // 其余弹窗直接关（保持原来的行为）。
  $$('.modal-close').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = btn.closest('.modal');
      memTryClose(m, () => m.classList.add('hidden'));
    });
  });

  $$('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', () => {
      const m = overlay.closest('.modal');
      memTryClose(m, () => m.classList.add('hidden'));
    });
  });

  // 配置保存
  $('#btn-save-config').addEventListener('click', saveConfig);

  // 记忆（四段 + 开关 + 整理 + 导入导出）
  initMemoryEvents();

  // 模型配置（供应商 / 模型池 / 自配模型）与聊天页模型切换
  initModelConfigEvents();
  initModelSwitchEvents();

  // 修改密码
  $('#btn-save-password').addEventListener('click', savePassword);

  // 用户管理
  $('#btn-add-user').addEventListener('click', openAddUser);
  $('#btn-submit-user').addEventListener('click', submitUserForm);
  $('#btn-confirm-delete').addEventListener('click', confirmDeleteUser);
  $('#user-search').addEventListener('input', filterUsers);

  // 密码显示切换
  $('#btn-toggle-user-password').addEventListener('click', () => {
    const input = $('#user-form-password');
    const btn = $('#btn-toggle-user-password');
    if (input.type === 'password') {
      input.type = 'text';
      btn.textContent = '🙈';
    } else {
      input.type = 'password';
      btn.textContent = '👁';
    }
  });

  // 加载当前用户信息
  loadCurrentUser();

  // 模型标识（失败不阻塞启动：拿不到就显示「模型」占位）
  refreshChatModelBadge();

  // 启动任务状态轮询（检测后台任务完成，显示气泡通知）
  startTaskPolling();

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('must_change_password') === '1') {
    setTimeout(() => {
      openChangePassword();
      showToast('首次登录，请修改默认密码', 'info');
      if (window.history && window.history.replaceState) {
        window.history.replaceState({}, '', '/');
      }
    }, 500);
  }

  // 联网搜索两态切换: off -> auto -> off
  const WEB_SEARCH_MODES = ['off', 'auto'];
  const WEB_SEARCH_LABELS = {
    off: '联网搜索',
    auto: '联网搜索·自动',
  };
  $('#btn-web-search').addEventListener('click', () => {
    const btn = $('#btn-web-search');
    const currentIdx = WEB_SEARCH_MODES.indexOf(state.webSearch);
    const nextIdx = (currentIdx + 1) % WEB_SEARCH_MODES.length;
    state.webSearch = WEB_SEARCH_MODES[nextIdx];
    btn.dataset.mode = state.webSearch;
    btn.querySelector('span').textContent = WEB_SEARCH_LABELS[state.webSearch];
    btn.classList.remove('active', 'auto');
    if (state.webSearch === 'auto') btn.classList.add('auto');
    btn.title = WEB_SEARCH_LABELS[state.webSearch];
  });

  // 思考强度：模型下拉里的三档分段（关闭 / 低 / 高），只在改变档位时写后端
  const thinkSeg = $('#mm-think-seg');
  if (thinkSeg) {
    thinkSeg.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-think]');
      if (!btn) return;
      const next = btn.dataset.think;
      if (next === state.thinkingMode) return;
      const prev = state.thinkingMode;
      state.thinkingMode = next;
      updateThinkingModeUI();
      try {
        await API.put('/api/config', { thinking_mode: next });
      } catch (err) {
        showToast('保存思考设置失败: ' + err.message, 'error');
        state.thinkingMode = prev;
        updateThinkingModeUI();
      }
    });
  }

  // 会话权限模式（请求批准 / 完全访问权限）：事件绑定
  // 注意：updateTrustUI / refreshTrustState 定义在顶层作用域，
  // 因为 switchSession 等顶层函数需要调用它们。
  const btnPerm = $('#btn-perm');

  // 权限按钮点击 → 切换模式（切到 full 时弹确认窗）
  if (btnPerm) {
    btnPerm.addEventListener('click', async () => {
      // 未在任何 session 中：权限模式需绑定到具体会话，
      // 先发送一条消息创建会话后再切换（符合「输入发送后创建新对话」的逻辑）
      if (!state.currentSessionId) {
        showToast('请先在下方输入并发送一条消息创建会话，再切换权限模式', 'info');
        return;
      }
      const currentMode = btnPerm.dataset.mode || 'request';

      // 当前是 full → 直接切回 request（安全方向，无需确认）
      if (currentMode === 'full') {
        try {
          const res = await API.post(`/api/chat/${state.currentSessionId}/trust`, { mode: 'request' });
          if (!res || res.success !== true) throw new Error('返回异常');
          updateTrustUI('request');
          showToast('已切回「请求批准」：敏感操作前将再次需要你确认', 'info');
        } catch (e) {
          showToast('切换权限模式失败: ' + (e.message || e), 'error');
        }
        return;
      }

      // 当前是 request → 弹确认窗
      openModal('modal-perm-confirm');
    });

    // 确认弹窗：确认按钮
    const permConfirmOk = $('#perm-confirm-ok');
    if (permConfirmOk) {
      permConfirmOk.addEventListener('click', async () => {
        closeModal('modal-perm-confirm');
        btnPerm.disabled = true;
        try {
          const res = await API.post(`/api/chat/${state.currentSessionId}/trust`, { mode: 'full' });
          if (!res || res.success !== true) throw new Error('返回异常');
          updateTrustUI('full');
          showToast('已切换为「完全访问权限」：敏感操作将直接执行，不再确认', 'info');
        } catch (e) {
          showToast('切换权限模式失败: ' + (e.message || e), 'error');
        } finally {
          btnPerm.disabled = false;
        }
      });
    }

    // 确认弹窗：取消按钮
    const permConfirmCancel = $('#perm-confirm-cancel');
    if (permConfirmCancel) {
      permConfirmCancel.addEventListener('click', () => closeModal('modal-perm-confirm'));
    }
  }

  // 权限范围说明已统一放入切换确认弹窗（modal-perm-confirm），不再单独维护 ⓘ 气泡

  // 保存搜索配置
  // 配置标签页切换
  document.querySelectorAll('.config-tab').forEach(tab => {
    tab.addEventListener('click', () => switchConfigTab(tab.dataset.tab));
  });

  // 快捷键
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const memModal = $('#modal-memory');
      const memOpen = memModal && !memModal.classList.contains('hidden');
      if (memOpen) {
        memTryClose(memModal, () => closeAllModals());
      } else {
        closeAllModals();
      }
      hideCommandSuggestions();
      $('#settings-drawer').classList.add('hidden');
    }
  });

  // 点击聊天区域关闭抽屉
  $('#chat-area').addEventListener('click', () => {
    $('#settings-drawer').classList.add('hidden');
  });
});
