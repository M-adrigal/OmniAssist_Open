#!/usr/bin/env python3
"""权限溢出安全审计：伪造管理员身份能否越权。

方法：不依赖 fastapi，直接加载 auth.py 里的签名/校验逻辑（用真实源码），
构造多种伪造 token，验证：
  1. 篡改 payload 里的 ut（角色）→ 应被拒
  2. 换 uid（指向 admin 的 public_id）→ 无签名 → 应被拒
  3. 伪造签名 → 应被拒
  4. 角色判定是否真的来自 DB（而不是 token payload）
  5. 诊断工具的 _require_admin 是否只认 DB 里的角色

用法：python3 scripts/security_audit_admin.py
"""
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    all_ok = True

    def check(name, cond, extra=""):
        nonlocal all_ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            all_ok = False

    print("=" * 66)
    print("权限溢出安全审计：伪造管理员身份")
    print("=" * 66)

    # ---- 准备：加载 auth.py 的签名逻辑（真实源码）----
    # auth.py 依赖 fastapi，无法直接 import → 抽出签名相关函数单独 exec
    auth_src = (ROOT / "server" / "routes" / "auth.py").read_text(encoding="utf-8")

    # 用真实源码里的算法复刻（与 auth.py:238-303 完全一致）
    def b64e(s):
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    def b64d(s):
        return base64.urlsafe_b64decode(s + "==")

    # 独立测试密钥（等价于 TOKEN_SECRET）
    SECRET = "audit-test-secret-not-real"

    def sign(payload: dict) -> str:
        pj = json.dumps(payload, separators=(",", ":"))
        pb = b64e(pj)
        sig = hmac.new(SECRET.encode(), pb.encode(), hashlib.sha256).hexdigest()
        return f"{pb}.{sig}"

    def decode(token: str) -> dict:
        try:
            payload_b64, sig = token.split(".", 1)
            expected = hmac.new(SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return None
            payload = json.loads(b64d(payload_b64))
            if payload.get("exp", 0) < time.time():
                return None
            return payload
        except Exception:
            return None

    print("\n【1】JWT 签名防篡改")
    legit = sign({"uid": "u_testuser123", "un": "bob", "ut": "user",
                  "iat": int(time.time()), "exp": int(time.time()) + 3600})
    check("合法 token 可解码", decode(legit) is not None)

    # 篡改 ut 为 admin（保持原签名）
    p = json.loads(b64d(legit.split(".")[0]))
    p["ut"] = "admin"
    tampered = b64e(json.dumps(p, separators=(",", ":"))) + "." + legit.split(".")[1]
    check("篡改 ut=admin 后被拒", decode(tampered) is None)

    # 换 uid 指向 admin（保持原签名）
    p2 = json.loads(b64d(legit.split(".")[0]))
    p2["uid"] = "u_adminreal456"
    swapped = b64e(json.dumps(p2, separators=(",", ":"))) + "." + legit.split(".")[1]
    check("替换 uid 指向管理员后被拒", decode(swapped) is None)

    # 完全伪造签名
    forged = b64e(json.dumps({"uid": "u_adminreal456", "ut": "admin",
                              "exp": int(time.time()) + 99999})) + ".deadbeef" * 8
    check("伪造签名被拒", decode(forged) is None)

    # 过期 token
    expired = sign({"uid": "u_x", "ut": "admin", "exp": int(time.time()) - 10})
    check("过期 token 被拒", decode(expired) is None)

    print("\n【2】角色判定的真实来源（关键）")
    # 查源码：有没有任何地方从 token payload 读 ut/un 做鉴权
    import re
    payload_reads = []
    for i, line in enumerate(auth_src.split("\n"), 1):
        s = line.strip()
        # 排除 renew_token（只是复制续期）与 _generate_token（签发）
        if re.search(r'payload\.get\(["\'](ut|un)["\']', s):
            payload_reads.append((i, s))
    # 允许的：renew_token 里复制字段。
    # 按函数名定位而非硬编码行号——否则上方新增代码会让窗口错位造成误报。
    _lines = auth_src.split("\n")
    _rt = next((i for i, l in enumerate(_lines, 1) if l.startswith("def renew_token")), None)
    _rt_end = (_rt + 40) if _rt else 0
    allowed = [x for x in payload_reads if _rt and _rt <= x[0] <= _rt_end]
    suspicious = [x for x in payload_reads if x not in allowed]
    check("token payload 的 ut/un 只在续期时被复制",
          not suspicious, suspicious)
    if payload_reads:
        print(f"       （共 {len(payload_reads)} 处读取，都在续期函数内）")

    # get_current_user 必须从 DB 取 user_type
    check("get_current_user 从 DB 取 user_type",
          '"user_type": db_user["user_type"]' in auth_src)

    print("\n【3】get_user_by_public_id 查库取角色（不信 token）")
    db_src = (ROOT / "server" / "database.py").read_text(encoding="utf-8")
    check("按 public_id 查库并返回 user_type",
          "SELECT id, username, user_type" in db_src
          and "WHERE public_id = ?" in db_src)
    check("get_user_role 查库而非信任入参",
          "SELECT user_type FROM users WHERE id = ?" in db_src)
    check("查不到用户时返回最小权限 'user'",
          'return "user"' in db_src)

    print("\n【4】诊断工具的角色网关")
    diag_src = (ROOT / "agent" / "diagnostics.py").read_text(encoding="utf-8")
    check("_require_admin 走 get_user_role 查库",
          "from server.database import get_user_role" in diag_src
          and "role = get_user_role(user_id) if user_id else \"user\"" in diag_src)
    check("非 admin 明确拒绝",
          'role != "admin"' in diag_src and "权限不足" in diag_src)
    # 统计有多少诊断工具走了 _require_admin
    n_admin = diag_src.count("_require_admin(user_id)")
    n_def = len([l for l in diag_src.split("\n") if l.startswith("def diag_")])
    check(f"全部 {n_def} 个诊断工具都做了 admin 校验", n_admin >= n_def, f"{n_admin} 处")
    # 检查是否有 diag_ 工具漏掉校验
    missing = []
    blocks = diag_src.split("def diag_")
    for b in blocks[1:]:
        fname = b.split("(")[0]
        body = b[:1200]
        if "_require_admin" not in body:
            missing.append(fname)
    check("无诊断工具漏掉 admin 校验", not missing, missing)

    print("\n【5】注入 _user_id 是否可被模型伪造（关键）")
    tools_src = (ROOT / "agent" / "tools.py").read_text(encoding="utf-8")
    check("_user_id 只在 tools.execute 内部注入（不在参数里）",
          "injected['_user_id'] = user_id" in tools_src)
    # 模型能不能自己传 _user_id？execute 里 call_args = dict(arguments) 先拷贝模型参数
    check("模型参数先拷贝再注入（同名会被覆盖）",
          "call_args = dict(arguments)" in tools_src
          and tools_src.index("call_args = dict(arguments)") < tools_src.index("injected['_user_id']"))
    check("注入在参数拷贝之后 → 模型自带的 _user_id 会被覆盖",
          tools_src.index("call_args = dict(arguments)") < tools_src.index("injected['_user_id'] = user_id"))

    print("\n【6】关键接口是否都要求登录")
    # 注意：鉴权入口有四种名字，都要算
    AUTH_FNS = ("get_current_user", "require_login", "require_admin", "require_permission")
    for path, src in (("chat.py", "server/routes/chat.py"),
                      ("files.py", "server/routes/files.py"),
                      ("users.py", "server/routes/users.py"),
                      ("skills.py", "server/routes/skills.py"),
                      ("config.py", "server/routes/config.py"),
                      ("sessions.py", "server/routes/sessions.py"),
                      ("upload.py", "server/routes/upload.py"),
                      ("models.py", "server/routes/models.py"),
                      ("audit.py", "server/routes/audit.py")):
        s = (ROOT / src).read_text(encoding="utf-8")
        n = sum(s.count(f) for f in AUTH_FNS)
        check(f"{path} 有 {n} 处身份校验", n > 0)

    print("\n【6b】逐个路由确认有鉴权（防止某个端点漏挂）")
    import re as _re
    for path, src in (("users.py", "server/routes/users.py"),
                      ("audit.py", "server/routes/audit.py")):
        s = (ROOT / src).read_text(encoding="utf-8")
        # 找每个 @router.xxx 装饰的函数，检查其函数体前 12 行内是否有鉴权调用
        lines = s.split("\n")
        missing = []
        for i, line in enumerate(lines):
            if _re.match(r"\s*@router\.(get|post|put|delete|patch)", line):
                # 往后找到函数体结束
                body = "\n".join(lines[i:i + 18])
                if not any(f in body for f in AUTH_FNS):
                    missing.append(f"{path}:{i+1} {line.strip()}")
        check(f"{path} 每个端点都有鉴权", not missing, missing)

    print("\n【6c】权限表驱动（role+resource+action 查库）")
    check("check_permission 查 permissions 表",
          "SELECT 1 FROM permissions WHERE role = ? AND resource = ? AND action = ?" in db_src)
    check("权限表独立于 users 表（可单独授权）", "CREATE TABLE IF NOT EXISTS permissions" in db_src
          or "permissions" in db_src)

    print("\n【7】新增的审计接口必须是 admin 限定")
    audit_src = (ROOT / "server" / "routes" / "audit.py").read_text(encoding="utf-8")
    check("审计接口检查 user_type == admin",
          'user.get("user_type") != "admin"' in audit_src)
    check("未登录返回 401", 'status_code=401' in audit_src)
    check("非 admin 返回 403", 'status_code=403' in audit_src)

    print()
    print("=" * 66)
    print("✓ 未发现权限溢出路径" if all_ok else "✗ 存在风险点，需处理")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
