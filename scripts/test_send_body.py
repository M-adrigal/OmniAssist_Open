#!/usr/bin/env python3
"""前端「发送消息」请求体回归测试（无头 Chrome，真实 index.html + 真实 JS）。

为什么要有这个：2026-10-24 给 `_runChatTurn` 的请求体加 `uploaded_files` 时，
误用了外层 `sendMessage` 的局部变量 `currentAttachments` —— 作用域错，
**每一条消息都发不出去**，界面上只显示「请求失败: currentAttachments is not defined」。
单测和 check_static 都发现不了（一个是后端、一个只查重名与 CSS 变量），
只有真正跑一遍发送路径才能抓到。这个脚本补的就是这个洞。

覆盖：
  ① sendMessage 带上附件 → 请求体 uploaded_files 等于本轮文件名
  ② _runChatTurn 不带附件（重新生成路径）→ uploaded_files 为空数组，不炸
  ③ 两条路径都不产生 JS 运行时错误（ReferenceError 会直接让断言①失败）

用法：python3 scripts/test_send_body.py
"""
import html as htmlmod
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 捕获运行时错误（ReferenceError 这类不会抛到加载期 listener，但 Promise 里会走 unhandledrejection）
HEAD = """
<script>
window.__errors = [];
window.addEventListener('error', function (e) {
  window.__errors.push('error: ' + (e.message || ''));
});
window.addEventListener('unhandledrejection', function (e) {
  window.__errors.push('rejection: ' + (e.reason && e.reason.message ? e.reason.message : e.reason));
});
</script>
"""

# 探针主体：stub fetch，走两条真实发送路径，把捕获到的请求体吐到 DOM
BODY = """
<script>
(function () {
  var out = { errors: window.__errors, bodies: [], notes: [] };
  function dump() {
    var pre = document.createElement('pre');
    pre.id = 'probe-out';
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }

  // 假 SSE 流：只回一条 token + 一条 done，够 readSSEStream 跑完
  function fakeSSE() {
    var enc = new TextEncoder();
    return new Response(new ReadableStream({
      start: function (c) {
        c.enqueue(enc.encode('data: {"type":"token","content":"ok"}\\n\\n'));
        c.enqueue(enc.encode('data: {"type":"done"}\\n\\n'));
        c.close();
      }
    }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }

  var realFetch = window.fetch;
  window.fetch = function (url, opts) {
    var u = String(url);
    if (opts && opts.body && u.indexOf('/api/chat/stream') >= 0) {
      try { out.bodies.push(JSON.parse(opts.body)); }
      catch (e) { out.notes.push('请求体不是 JSON: ' + e.message); }
      return Promise.resolve(fakeSSE());
    }
    // 其余接口（会话列表 / 归属 / 模型徽章等）一律给空结果，避免真实网络
    if (u.indexOf('/api/') >= 0) {
      return Promise.resolve(new Response('{}', {
        status: 200, headers: { 'Content-Type': 'application/json' } }));
    }
    return realFetch.apply(window, arguments);
  };

  (async function () {
    try {
      state.currentSessionId = 'probe-session';
      state.attachedFiles = [{ filename: 'a.png', type: 'image' }];
      document.getElementById('chat-input').value = '带附件的一轮';

      // ① 真实发送路径：sendMessage 会先快照附件、清空附件，再把附件传进 _runChatTurn
      await sendMessage();

      // ② 重新生成路径：没有「本轮上传」，必须传空数组而不是抛错
      await _runChatTurn('无附件的一轮');
    } catch (e) {
      out.notes.push('调用抛错: ' + (e && e.message));
    }
    out.errors = window.__errors;
    // sendMessage/_runChatTurn 内部会 try/catch 把异常写成回答区文案
    // （「请求失败: xxx」），异常本身不会冒到 window.onerror —— 必须验文案
    out.answerTexts = Array.from(document.querySelectorAll('.answer-area'))
      .map(function (el) { return (el.textContent || '').trim(); })
      .filter(Boolean);
    dump();
  })();
})();
</script>
"""


def main():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/', f'href="file://{ROOT}/static/')
    html = html.replace('src="/static/', f'src="file://{ROOT}/static/')
    html = html.replace("<head>", "<head>" + HEAD, 1)
    html = html.replace("</body>", BODY + "</body>", 1)

    tmp = Path(tempfile.mkdtemp()) / "send_body.html"
    tmp.write_text(html, encoding="utf-8")

    with tempfile.TemporaryDirectory() as profile:
        cmd = [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
               f"--user-data-dir={profile}", "--window-size=1440,1000",
               "--allow-file-access-from-files",
               "--virtual-time-budget=8000", "--dump-dom", tmp.as_uri()]
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

    bodies = data.get("bodies") or []
    print(f"捕获到 {len(bodies)} 个 /api/chat/stream 请求体")

    check("发消息确实发出了请求（作用域错误会让这一条直接不成立）", len(bodies) >= 1, bodies)
    if bodies:
        b0 = bodies[0]
        check("① 带附件：uploaded_files == ['a.png']",
              b0.get("uploaded_files") == ["a.png"], b0.get("uploaded_files"))
        check("① 消息文本透传", b0.get("message") == "带附件的一轮", b0.get("message"))
        check("① 带会话 id", b0.get("session_id") == "probe-session", b0.get("session_id"))
        check("① selected_skills 是数组",
              isinstance(b0.get("selected_skills"), list), b0.get("selected_skills"))
    if len(bodies) >= 2:
        b1 = bodies[1]
        check("② 无附件（重新生成）：uploaded_files == []",
              b1.get("uploaded_files") == [], b1.get("uploaded_files"))

    # 关键断言：这类作用域错误被内部 catch 吞成界面文案，window.onerror 抓不到
    bad = [t for t in (data.get("answerTexts") or []) if "请求失败" in t or "发送失败" in t]
    check("回答区没有「请求失败/发送失败」文案", not bad, bad)

    check("无 JS 运行时错误", not data.get("errors"), data.get("errors"))
    check("调用过程无异常", not data.get("notes"), data.get("notes"))

    print()
    print("✓ 通过" if ok else "✗ 存在问题")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
