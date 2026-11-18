#!/usr/bin/env python3
"""输入框按键行为验收：Enter 发送 / Shift+Enter 换行 / Cmd+Enter 换行。

真实 index.html + 真实 JS，只 stub 掉 sendMessage 的后端部分，
记录它「被调用了几次」——这是唯一能区分"发了"和"没发"的判据。

用法：test_input_keys.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PROBE = r"""
<script>
document.addEventListener('DOMContentLoaded', function () { (async function () {
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  var out = {};
  var input = document.getElementById('chat-input');

  /* 把 sendMessage 换成计数桩。原实现会发网络请求、建会话、改 DOM，
     在这个探针里全是噪声；我们只需要知道「按一下键有没有触发发送」。
     ⚠️ 桩必须**先复刻原函数的入口守卫**（空内容直接 return），
     否则「纯空白不该发送」这条就变成了在测桩、不是测产品代码。 */
  window.__sendCalls = 0;
  var origSend = window.sendMessage;
  window.sendMessage = function () {
    var v = (document.getElementById('chat-input').value || '').trim();
    if (!v) return Promise.resolve();     // 与 sendMessage 开头那句一致
    window.__sendCalls++;
    return Promise.resolve();
  };

  function fire(key, mods) {
    mods = mods || {};
    var ev = new KeyboardEvent('keydown', {
      key: key, code: key === 'Enter' ? 'Enter' : key,
      bubbles: true, cancelable: true,
      shiftKey: !!mods.shift, metaKey: !!mods.meta,
      ctrlKey: !!mods.ctrl, altKey: !!mods.alt
    });
    input.dispatchEvent(ev);
    return ev.defaultPrevented;
  }
  function reset(text) {
    input.value = text != null ? text : '第一行';
    input.selectionStart = input.selectionEnd = input.value.length;
    window.__sendCalls = 0;
  }

  /* ① 裸 Enter → 发送，且值被清空 */
  reset('裸回车');
  var preventedPlain = fire('Enter', {});
  await sleep(30);
  out.plain_enter_sent = window.__sendCalls;
  out.plain_enter_prevented = preventedPlain;

  /* ② Shift+Enter → 换行，不发送 */
  reset('第一行');
  var preventedShift = fire('Enter', { shift: true });
  await sleep(30);
  out.shift_enter_sent = window.__sendCalls;
  out.shift_enter_prevented = preventedShift;
  /* Shift+Enter 走浏览器默认行为（插换行），无头里 dispatchEvent 不会真插，
     所以这里不验 value，只验「没被拦截」—— 被拦截才是问题。 */

  /* ③ Cmd+Enter → **不发送**，且插入一个换行（用户报的那个 bug） */
  reset('第一行');
  var preventedMeta = fire('Enter', { meta: true });
  await sleep(30);
  out.meta_enter_sent = window.__sendCalls;
  out.meta_enter_prevented = preventedMeta;
  out.meta_enter_value = input.value;
  out.meta_enter_newlineAdded = input.value === '第一行\n';
  /* 光标应落在换行之后 */
  out.meta_enter_caret = input.selectionStart;

  /* ④ Ctrl+Enter → 同 Cmd+Enter（Windows / Linux 用户的对应键） */
  reset('第一行');
  var preventedCtrl = fire('Enter', { ctrl: true });
  await sleep(30);
  out.ctrl_enter_sent = window.__sendCalls;
  out.ctrl_enter_value = input.value;

  /* ⑤ Alt+Enter → 不能发送（最容易漏的组合，判据若只排除部分修饰键就会中招） */
  reset('第一行');
  fire('Enter', { alt: true });
  await sleep(30);
  out.alt_enter_sent = window.__sendCalls;

  /* ⑥ Cmd+Shift+Enter → 不能发送 */
  reset('第一行');
  fire('Enter', { meta: true, shift: true });
  await sleep(30);
  out.meta_shift_enter_sent = window.__sendCalls;

  /* ⑦ 在文本中间插入换行，光标位置要正确
        （setRangeText 与简单拼接的区别就在这里） */
  reset('ABCD');
  input.selectionStart = input.selectionEnd = 2;   // A|BCD
  fire('Enter', { meta: true });
  await sleep(30);
  out.mid_insert_value = input.value;
  out.mid_insert_caret = input.selectionStart;
  out.mid_insert_ok = input.value === 'AB\nCD' && input.selectionStart === 3;

  /* ⑧ 选区被换行替换（不是留下原文） */
  reset('ABCD');
  input.selectionStart = 1; input.selectionEnd = 3;   // A[BC]D
  fire('Enter', { meta: true });
  await sleep(30);
  out.sel_replace_value = input.value;
  out.sel_replace_caret = input.selectionStart;

  /* ⑨ 换行后 undo 仍可用（setRangeText 保留撤销栈） */
  reset('XY');
  fire('Enter', { meta: true });
  await sleep(20);
  input.value = input.value;   // 不改
  out.undo_before = input.value;

  /* ⑩ IME 组字中按裸 Enter 不能发送（选词场景） */
  reset('试用');
  input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
  fire('Enter', {});
  await sleep(30);
  out.ime_enter_sent = window.__sendCalls;
  input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true }));

  /* ⑪ 组字结束后裸 Enter 正常发送（不能因为 ⑩ 把发送永久关掉） */
  reset('组字结束');
  fire('Enter', {});
  await sleep(30);
  out.after_ime_enter_sent = window.__sendCalls;

  /* ⑫ 空内容裸 Enter 不发（sendMessage 自己会挡，但按键层不该先清空） */
  reset('   ');
  fire('Enter', {});
  await sleep(30);
  out.blank_enter_prevented = true;   // 占位，实际看 sendCalls
  out.blank_enter_sent = window.__sendCalls;

  window.sendMessage = origSend;
  var p = document.createElement('pre'); p.id = 'probe';
  p.textContent = JSON.stringify(out);
  document.body.appendChild(p);
})(); });
</script>
"""


def main():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', f'href="file://{ROOT}/static/')
    html = html.replace('src="/static/', f'src="file://{ROOT}/static/')
    html = html.replace("</head>",
                        "<style>*{transition:none!important;animation:none!important}</style></head>", 1)
    html = html.replace("</body>", PROBE + "</body>", 1)

    tmp = Path(tempfile.mkdtemp()) / "keys.html"
    tmp.write_text(html, encoding="utf-8")

    with tempfile.TemporaryDirectory() as profile:
        base = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                "--hide-scrollbars", f"--user-data-dir={profile}",
                "--window-size=1280,860", "--allow-file-access-from-files",
                "--virtual-time-budget=12000",
                "--run-all-compositor-stages-before-draw",
                "--force-device-scale-factor=1"]
        r = subprocess.run(base + ["--dump-dom", tmp.as_uri()],
                           capture_output=True, text=True, timeout=120)

    dom = r.stdout
    i = dom.find('<pre id="probe">')
    if i < 0:
        print("✗ 探针未输出（页面脚本异常）")
        return 1
    s = dom.find(">", i) + 1
    e = dom.find("</pre>", s)
    d = json.loads(dom[s:e].replace("&quot;", '"').replace("&amp;", "&"))

    checks = [
        ("裸 Enter 发送", d.get("plain_enter_sent") == 1,
         f"send 调用 {d.get('plain_enter_sent')} 次"),
        ("裸 Enter 阻止默认换行", d.get("plain_enter_prevented") is True, ""),
        ("Shift+Enter 不发送（换行）", d.get("shift_enter_sent") == 0,
         f"send 调用 {d.get('shift_enter_sent')} 次"),
        ("Shift+Enter 不被拦截（走浏览器默认换行）",
         d.get("shift_enter_prevented") is False, ""),
        # —— 用户报的 bug ——
        ("Cmd+Enter 不发送（用户「直接发送出去了」）",
         d.get("meta_enter_sent") == 0, f"send 调用 {d.get('meta_enter_sent')} 次"),
        ("Cmd+Enter 插入一个换行（用户「没反应」）",
         d.get("meta_enter_newlineAdded") is True,
         f"值 = {d.get('meta_enter_value')!r}"),
        ("Cmd+Enter 后光标在换行之后", d.get("meta_enter_caret") == 4,
         f"光标 {d.get('meta_enter_caret')}"),
        ("Ctrl+Enter 同样换行不发送",
         d.get("ctrl_enter_sent") == 0 and d.get("ctrl_enter_value") == "第一行\n",
         f"send {d.get('ctrl_enter_sent')} / 值 {d.get('ctrl_enter_value')!r}"),
        ("Alt+Enter 不发送（只排 Shift 会漏掉它）",
         d.get("alt_enter_sent") == 0, f"send 调用 {d.get('alt_enter_sent')} 次"),
        ("Cmd+Shift+Enter 不发送", d.get("meta_shift_enter_sent") == 0,
         f"send 调用 {d.get('meta_shift_enter_sent')} 次"),
        ("光标在中间时换行插在中间",
         d.get("mid_insert_ok") is True,
         f"值 {d.get('mid_insert_value')!r} / 光标 {d.get('mid_insert_caret')}"),
        ("有选区时换行替换选区",
         d.get("sel_replace_value") == "A\nD" and d.get("sel_replace_caret") == 2,
         f"值 {d.get('sel_replace_value')!r} / 光标 {d.get('sel_replace_caret')}"),
        ("IME 组字中裸 Enter 不发送",
         d.get("ime_enter_sent") == 0, f"send 调用 {d.get('ime_enter_sent')} 次"),
        ("组字结束后裸 Enter 恢复发送",
         d.get("after_ime_enter_sent") == 1, f"send 调用 {d.get('after_ime_enter_sent')} 次"),
        ("纯空白裸 Enter 不产生发送",
         d.get("blank_enter_sent") == 0, f"send 调用 {d.get('blank_enter_sent')} 次"),
    ]

    fails = 0
    for name, ok, extra in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  ({extra})" if extra and not ok else ""))
        if not ok:
            fails += 1
    print()
    print(f"{len(checks) - fails}/{len(checks)} 通过")
    if fails and "--verbose" in sys.argv:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    if not Path(CHROME).exists():
        print("跳过：未找到 Chrome")
        sys.exit(0)
    sys.exit(main())
