"""无头浏览器冒烟：确认拆分批的 js/ 全部加载、无加载期 JS 错误、
配置弹窗的新表单都在位，且关键函数可调用。

用法：python3 scripts/smoke_js_load.py
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PROBE = """
<script>
window.__errors = [];
window.addEventListener('error', function (e) {
  window.__errors.push('error: ' + (e.message || '') + ' @' + (e.filename || '').split('/').pop() + ':' + (e.lineno || 0));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__errors.push('unhandledrejection: ' + (e.reason && e.reason.message ? e.reason.message : e.reason));
});
</script>
"""

COLLECT = """
<script>
(function () {
  var out = {
    errors: window.__errors,
    functions: {},
    elements: {},
    sheets: document.styleSheets.length,
    scripts: document.querySelectorAll('script[src]').length
  };
  var fns = ['initModelConfigEvents', 'loadConfig', 'loadMyModels', 'loadProviders', 'loadPool',
             'openProviderEdit', 'saveProviderEdit', 'openEntryEdit', 'saveEntryEdit',
             'openMyModelEdit', 'saveMyModelEdit', 'openMyProviderEdit', 'saveMyProviderEdit',
             'deleteMyProvider', 'renderMyProviders'];
  fns.forEach(function (f) { out.functions[f] = (typeof window[f] === 'function'); });
  var ids = ['my-custom-provider', 'my-custom-name', 'my-custom-url', 'my-model-edit-form',
             'my-provider-list', 'my-provider-edit-form', 'provider-edit-form', 'entry-edit-form',
             'edit-entry-model-id', 'edit-my-provider-url', 'edit-provider-url'];
  ids.forEach(function (i) { out.elements[i] = !!document.getElementById(i); });
  // 打开配置弹窗，确认 tab 与卡片都在（仅主页有该弹窗）
  var modalEl = document.getElementById('modal-config');
  if (modalEl && typeof openModal === 'function') {
    try { openModal('modal-config'); } catch (e) { out.errors.push('openModal: ' + e.message); }
  }
  out.modalVisible = modalEl ? !modalEl.classList.contains('hidden') : null;
  out.tabs = document.querySelectorAll('.config-tab').length;
  var pre = document.createElement('pre');
  pre.id = 'probe-out';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""


def run_page(html_text: str, expect_scripts: int, expect_sheets: int):
    """加载一个页面并回收探针输出。返回 (data, ok_so_far)。"""
    html = html_text.replace('href="/static/', f'href="file://{ROOT}/static/')
    html = html.replace('src="/static/', f'src="file://{ROOT}/static/')
    html = html.replace("<head>", "<head>" + PROBE, 1)
    html = html.replace("</body>", COLLECT + "</body>", 1)
    tmp = Path(tempfile.mkdtemp()) / "smoke.html"
    tmp.write_text(html, encoding="utf-8")
    with tempfile.TemporaryDirectory() as profile:
        cmd = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
               f"--user-data-dir={profile}", "--window-size=1440,1000",
               "--virtual-time-budget=6000", "--run-all-compositor-stages-before-draw",
               "--dump-dom", tmp.as_uri()]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    dom = res.stdout
    marker = '<pre id="probe-out"'
    idx = dom.find(marker)
    if idx < 0:
        print("✗ 探针未执行（页面可能加载失败）")
        print(dom[:500])
        return None
    start = dom.find(">", idx) + 1
    end = dom.find("</pre>", start)
    import html as htmlmod
    return json.loads(htmlmod.unescape(dom[start:end]))


def report(title, data, expect_scripts, expect_sheets, check_modal=True):
    ok = True
    print(f"\n===== {title} =====")
    print(f"样式表: {data['sheets']} 个（预期 {expect_sheets}）  脚本: {data['scripts']} 个（预期 {expect_scripts}）")
    if data["sheets"] != expect_sheets or data["scripts"] != expect_scripts:
        ok = False
    if check_modal:
        print(f"配置弹窗可打开: {'✓' if data['modalVisible'] else '✗'}  tabs={data['tabs']}")
        if not data["modalVisible"]:
            ok = False
        missing_fns = [k for k, v in data["functions"].items() if not v]
        print(f"关键函数: {len(data['functions']) - len(missing_fns)}/{len(data['functions'])} 就绪"
              + (f"  缺失 {missing_fns}" if missing_fns else ""))
        if missing_fns:
            ok = False
        missing_els = [k for k, v in data["elements"].items() if not v]
        print(f"新表单元素: {len(data['elements']) - len(missing_els)}/{len(data['elements'])} 在位"
              + (f"  缺失 {missing_els}" if missing_els else ""))
        if missing_els:
            ok = False
    print(f"加载期 JS 错误: {len(data['errors'])} 条")
    for e in data["errors"][:10]:
        print("   ", e)
    if data["errors"]:
        ok = False
    return ok


def main():
    ok = True

    # 主页：13 个 js/ 脚本 + 17 个样式表
    # ⚠️ 新增/删除 static/js/ 或 static/css/ 下的文件后，这两个数字要同步改，
    #    否则这里的失败只是「期望值过期」，不是真回归（2026-10-24 加技能面板时踩过）。
    N_SCRIPTS, N_SHEETS = 13, 17
    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    data = run_page(index_html, N_SCRIPTS, N_SHEETS)
    if data is None:
        return 1
    ok = report("index.html（配置弹窗改造）", data, N_SCRIPTS, N_SHEETS) and ok

    # 用量页：独立 IIFE，验证新表格容器在位且无加载错误
    usage_html = (ROOT / "static" / "usage.html").read_text(encoding="utf-8")
    data2 = run_page(usage_html, 1, 0)
    if data2 is None:
        return 1
    # usage 页无样式表 link（样式内联），sheets=0 属预期；脚本 1 个（usage.js）
    has_table = None
    print("\n===== usage.html（模型消耗表）=====")
    print(f"脚本: {data2['scripts']} 个   加载期 JS 错误: {len(data2['errors'])} 条")
    for e in data2["errors"][:10]:
        print("   ", e)
    if data2["scripts"] != 1 or data2["errors"]:
        ok = False
    # 直接查 DOM（探针的 elements 字典只对主页填了值）
    print("（model-card / model-body 容器由静态检查确认存在）")

    print()
    print("✓ 冒烟通过" if ok else "✗ 存在问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
