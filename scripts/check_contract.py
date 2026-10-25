#!/usr/bin/env python3
"""前后端契约对账：端点 / SSE 事件 / 请求体字段。

为什么需要：这个项目没有类型化的客户端，前端直接 `fetch('/api/xxx')`、
后端直接 `yield {'type': 'xxx'}` —— 名字对不上时**不会报错**，
只会静静失效（用户看到的是"没反应"）。2026-10-24 就踩过两次：
① `_runChatTurn` 引用了外层局部变量导致发消息全挂；
② 我自己写的评估跑器把 `token` 事件当成 `content`、把 `name` 字段当成 `filename`，
   导致 20 条用例全部"回答为空"。

三项检查：
  [1] 前端调用的接口路径，后端是否都有对应路由（含路径参数归一化）
  [2] 后端 SSE 事件类型，前端是否都处理（未处理 = 静默丢弃）
  [3] 前端请求体字段，后端模型是否认识（不认识 = 被静默忽略）

用法：
  python3 scripts/check_contract.py
  python3 scripts/check_contract.py --strict   # 有 ALERT 时退出码 1
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "static" / "js"
STATIC = ROOT / "static"


# ===== 后端 =====

def backend_routes() -> dict:
    """返回 {(METHOD, norm_path): file}。路径参数统一成 {} 。"""
    out = {}
    for p in sorted((ROOT / "server" / "routes").glob("*.py")):
        s = p.read_text(encoding="utf-8")
        prefix = ""
        m = re.search(r'APIRouter\(\s*prefix\s*=\s*["\']([^"\']+)["\']', s)
        if m:
            prefix = m.group(1)
        for m in re.finditer(
                r'@router\.(get|post|put|delete|patch|websocket)\(\s*["\']([^"\']*)["\']', s):
            method = m.group(1).upper()
            path = prefix + m.group(2)
            out[(method, _norm(path))] = p.name
    # main.py 里直接挂的（@app.xxx）
    main = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
    for m in re.finditer(r'@app\.(get|post|put|delete)\(\s*["\']([^"\']*)["\']', main):
        out[(m.group(1).upper(), _norm(m.group(2)))] = "main.py"
    return out


def _norm(path: str) -> str:
    """把路径参数与查询串归一：/api/skills/{k} → /api/skills/#，去掉尾部 / 。

    同时把纯数字段也归成 #：前端有时写死 `/api/skills/0/toggle`（id 由后端从
    body 取名忽略），后端定义是 `/api/skills/{skill_id}/toggle` —— 两者等价，
    不做这一步会误报「后端无此路由」。
    """
    path = path.split("?")[0].rstrip("/") or "/"
    path = re.sub(r"\{[^}]+\}", "#", path)
    return re.sub(r"/\d+(?=/|$)", "/#", path)


# ===== 前端 =====

# 捕获「谁被调用」，方法直接由被调函数决定（API.put 就是 PUT，别再去猜 method 字段）
_CALL_RE = re.compile(
    r"""\b(fetch|API\.(?:get|post|put|delete)|apiGet|apiPost|apiPut|apiDelete)"""
    r"""\s*\(\s*[`'"]([^`'"]+)[`'"]""")
_METHOD_RE = re.compile(r"method\s*:\s*['\"](\w+)['\"]")
_CALLEE_METHOD = {"API.get": "GET", "API.post": "POST",
                  "API.put": "PUT", "API.delete": "DELETE",
                  # usage.html 自带的轻量助手（独立页面，不共用 API 对象）
                  "apiGet": "GET", "apiPost": "POST",
                  "apiPut": "PUT", "apiDelete": "DELETE"}


def frontend_calls() -> list:
    """返回 [(file, method, path, raw)]。fetch 无 method 时按 GET。"""
    calls = []
    for p in sorted(set(list(JS_DIR.glob("*.js")) + list(STATIC.glob("*.js")))):
        text = p.read_text(encoding="utf-8")
        for m in _CALL_RE.finditer(text):
            callee, raw = m.group(1), m.group(2)
            if "${" in raw:          # 模板串拼出来的，跳过（无法静态判定）
                continue
            if not raw.startswith("/api"):
                continue
            if raw.endswith("/"):     # '/api/users/' + id 这种字符串拼接，同样跳过
                continue
            if callee in _CALLEE_METHOD:
                method = _CALLEE_METHOD[callee]
            else:
                mm = _METHOD_RE.search(text[m.start(): m.start() + 400])
                method = (mm.group(1) if mm else "GET").upper()
            calls.append((p.name, method, _norm(raw), raw))
    return calls


# ===== SSE =====

def backend_sse_types() -> dict:
    """只认真正通过 SSE 发出去的事件：yield 行里出现的 type。

    不能全文扫 `'type': 'xxx'` —— 会把审计日志记录类型（agent_audit 的
    turn/tool/approval）、OpenAI 多模态内容块（chat.py 的 image_url）
    一起算进来，全是误报。
    """
    out = {}
    for p in sorted((ROOT / "server").rglob("*.py")):
        for line in p.read_text(encoding="utf-8").split("\n"):
            if "yield" not in line:
                continue
            for m in re.finditer(r"""['"]type['"]\s*:\s*['"]([a-z_]+)['"]""", line):
                out.setdefault(m.group(1), set()).add(p.name)
    return out


def frontend_sse_types() -> set:
    """只取 handleStreamEvent 里判断过的类型（parsed.type === 'x' / case 'x'）。"""
    s = (JS_DIR / "04-stream.js").read_text(encoding="utf-8")
    return set(re.findall(r"parsed\.type\s*===\s*'([a-z_]+)'", s)) | \
        set(re.findall(r"case\s+'([a-z_]+)'\s*:", s))


# 只关心「会话流」事件：文件预览类（text/image/pdf/csv_table/…）走 files.py 的预览接口，
# 由 04-stream.js 的 renderPreview 单独处理，不属同一套事件。
PREVIEW_TYPES = {"text", "csv_table", "docx_text", "xlsx_preview", "image", "pdf", "unsupported"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()
    alerts = 0

    print("=" * 72)
    print("前后端契约对账")
    print("=" * 72)

    # [1] 端点
    routes = backend_routes()
    calls = frontend_calls()
    print(f"\n[1] 接口端点  （后端 {len(routes)} 条路由 / 前端 {len(calls)} 处调用）")
    missing = []
    for fname, method, norm, raw in calls:
        if (method, norm) in routes:
            continue
        # 同路径换方法也算可疑（前端写错 method 同样 405）
        same_path = [m for (m, pth) in routes if pth == norm]
        missing.append((fname, method, raw, same_path))
    if not missing:
        print("    ✓ 前端调用的接口后端都有")
    for fname, method, raw, same_path in missing:
        if same_path:
            print(f"    ⚠ {fname}: {method} {raw} —— 路径存在但方法不符，后端有 {same_path}")
        else:
            print(f"    ✗ {fname}: {method} {raw} —— 后端无此路由")
        alerts += 1

    # 前端是否用了 /api 但没走上面的正则（模板串），单独提示
    tpl = []
    for p in sorted(JS_DIR.glob("*.js")):
        for m in re.finditer(r"""fetch\(\s*`([^`]*\$\{[^`]*)`""", p.read_text(encoding="utf-8")):
            tpl.append((p.name, m.group(1)))
    if tpl:
        print(f"    · 另有 {len(tpl)} 处模板字符串接口（静态不可判，人工确认）：")
        for fname, raw in tpl[:8]:
            print(f"        {fname}: {raw}")

    # [2] SSE 事件
    be = backend_sse_types()
    fe = frontend_sse_types()
    stream_be = {t for t in be if t not in PREVIEW_TYPES} - {"array", "boolean", "integer",
                                                             "object", "string"}
    print(f"\n[2] SSE 事件  （后端 {len(stream_be)} 种 / 前端处理 {len(fe)} 种）")
    unhandled = sorted(stream_be - fe)
    if not unhandled:
        print("    ✓ 后端发的事件前端都处理了")
    else:
        for t in unhandled:
            print(f"    ⚠ 后端发 '{t}' 但前端未处理（静默丢弃）—— 来源 {sorted(be[t])}")
            alerts += 1
    extra = sorted(fe - stream_be)
    if extra:
        print(f"    · 前端处理但当前后端未发：{extra}（可能是历史兼容或从别处发）")

    # [3] 请求体字段
    print("\n[3] 请求体字段")
    alerts += check_chat_body()

    print("\n" + "=" * 72)
    print(f"{'✗ 发现 ' + str(alerts) + ' 处契约不一致' if alerts else '✓ 契约一致'}")
    print("=" * 72)
    return 1 if (args.strict and alerts) else 0


def check_chat_body() -> int:
    """前端 /api/chat/stream 请求体 vs server/models.py 的 ChatRequest。"""
    body_src = (JS_DIR / "05-chat-actions.js").read_text(encoding="utf-8")
    m = re.search(r"fetch\('/api/chat/stream'.*?body:\s*JSON\.stringify\(\{(.*?)\}\),",
                  body_src, re.S)
    if not m:
        print("    ✗ 没在 05-chat-actions.js 里找到 /api/chat/stream 的请求体")
        return 1
    fe_keys = set(re.findall(r"^\s*([a-zA-Z_]\w*)\s*:", m.group(1), re.M))

    models = (ROOT / "server" / "models.py").read_text(encoding="utf-8")
    mc = re.search(r"class ChatRequest\(BaseModel\):(.*?)(?=\nclass )", models, re.S)
    if not mc:
        print("    ✗ 没在 server/models.py 里找到 ChatRequest")
        return 1
    be_keys = set(re.findall(r"^\s{4}([a-zA-Z_]\w*)\s*:", mc.group(1), re.M))

    n = 0
    unknown = sorted(fe_keys - be_keys)
    if unknown:
        print(f"    ⚠ 前端发但 ChatRequest 不认识（会被 pydantic 丢弃）：{unknown}")
        n += 1
    unused = sorted(be_keys - fe_keys)
    if unused:
        print(f"    · ChatRequest 有但前端不发（可能靠默认值）：{unused}")
    if not unknown:
        print(f"    ✓ 前端 {len(fe_keys)} 个字段后端全部认识")
    return n


if __name__ == "__main__":
    sys.exit(main())
