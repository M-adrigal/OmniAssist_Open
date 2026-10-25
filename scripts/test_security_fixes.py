#!/usr/bin/env python3
"""本轮安全修复的回归验证脚本。

定位：「工具是内部自用」——只堵外部攻击者能碰到的面，因此本脚本只覆盖
外部入口与跨租户越权相关修复，不覆盖需要 OS 层加固的内部威胁项。

覆盖：
  ① upload.py 的 session_id 统一校验（路径穿越 / 跨租户）
  ② chat.py 的会话归属校验（跨租户读写他人对话）
  ③ files.py 的 inline MIME 白名单（存储型 XSS）
  ④ files.py 的 CSV 预览转义（存储型 XSS）
  ⑤ files.py 的 _check_file_access 前缀比较（同级前缀目录）
  ⑥ auth.py 的 X-Forwarded-For 不盲信 + 双维度锁定键
  ⑦ database.py 的默认管理员口令不再是可预测的常量
  ⑧ sessions.py 删会话时连带删图的归属与符号链接处理
  ⑨ sandbox.py：awk 移除、os 危险函数补全、importlib 后门封堵、内部别名随机化
  ⑩ main.py：全局异常不再回显内部细节

用法：
  python3 scripts/test_security_fixes.py
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK] {name}")
    else:
        FAIL.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}".strip())


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ============================================================
print("\n【1】upload.py —— session_id 统一校验（路径穿越 / 跨租户）")
# ============================================================
try:
    upload = _load("sec_upload", os.path.join(ROOT, "server", "routes", "upload.py"))
    resolve = upload._resolve_session_dir
except Exception as e:
    print(f"  无法加载 upload.py（依赖缺失）: {e}")
    upload = None
    resolve = None

if resolve:
    ME = {"id": "u_me_test", "db_id": 101}
    OTHER = {"id": "u_other_test", "db_id": 202}

    # 伪造 get_session：模拟「会话存在但属于他人」
    import server.database as real_db

    _orig_get_session = getattr(real_db, "get_session", None)
    FAKE_SESSIONS = {}

    def _fake_get_session(sid):
        return FAKE_SESSIONS.get(sid)

    real_db.get_session = _fake_get_session

    for bad in ["../../../static", "../../../data", "../../../..", "..",
                "../../u_other_test/uploads/s-victim", "/etc", "a/b",
                "..%2f..%2fstatic", "x\ny", "sess;rm -rf"]:
        try:
            resolve(ME, bad)
            check(f"拒绝穿越 session_id={bad!r}", False, "→ 未拦截")
        except Exception as e:
            code = getattr(e, "status_code", None)
            check(f"拒绝穿越 session_id={bad!r}", code in (400, 403, 404), f"→ {type(e).__name__}")

    # 跨租户：会话存在但不属于本人
    FAKE_SESSIONS["11111111-1111-1111-1111-111111111111"] = {"id": "11111111-1111-1111-1111-111111111111",
                                                             "user_id": OTHER["db_id"]}
    try:
        resolve(ME, "11111111-1111-1111-1111-111111111111")
        check("拒绝跨租户会话（会话属于他人）", False, "→ 未拦截")
    except Exception as e:
        check("拒绝跨租户会话（会话属于他人）", getattr(e, "status_code", None) == 404,
              f"→ {type(e).__name__}")

    # 本人会话必须放行，且落在本人 uploads 下
    FAKE_SESSIONS["22222222-2222-2222-2222-222222222222"] = {"id": "22222222-2222-2222-2222-222222222222",
                                                             "user_id": ME["db_id"]}
    try:
        d = resolve(ME, "22222222-2222-2222-2222-222222222222")
        expect = os.path.realpath(os.path.join(ROOT, "document_output", "u_me_test", "uploads",
                                               "22222222-2222-2222-2222-222222222222"))
        check("本人会话放行且落在本人 uploads 内", os.path.realpath(d) == expect, f"→ {d}")
    except Exception as e:
        check("本人会话放行且落在本人 uploads 内", False, f"→ {e}")

    # 会话不存在（新会话先上传）也应放行，但仍在本人目录内
    try:
        d = resolve(ME, "33333333-3333-3333-3333-333333333333")
        ok = os.path.realpath(d).startswith(
            os.path.realpath(os.path.join(ROOT, "document_output", "u_me_test", "uploads")) + os.sep)
        check("新会话（库内不存在）允许且仍在本人目录内", ok, f"→ {d}")
    except Exception as e:
        check("新会话（库内不存在）允许且仍在本人目录内", False, f"→ {e}")

    if _orig_get_session:
        real_db.get_session = _orig_get_session

# ============================================================
print("\n【2】chat.py —— /stream 会话归属校验")
# ============================================================
chat_src = _src("server/routes/chat.py")
seg = chat_src[chat_src.find("验证会话：必须存在"): chat_src.find("验证会话：必须存在") + 600] if "验证会话：必须存在" in chat_src else ""
check("stream 校验会话归属（user_id 比对）",
      'get("user_id") != user["db_id"]' in seg or 'get("user_id") != user["db_id"]' in chat_src)
check("stream 不再只判存在性",
      "if not get_session(body.session_id):" not in chat_src)

db_src = _src("server/database.py")
check("update_session_messages 兜底拒绝写他人会话",
      "拒绝写入他人会话" in db_src and 'existing["user_id"] != user_id' in db_src)

# ============================================================
print("\n【3】files.py —— inline MIME 白名单（存储型 XSS）")
# ============================================================
files_src = _src("server/routes/files.py")
check("已定义 _INLINE_MIME_ALLOWED", "_INLINE_MIME_ALLOWED" in files_src)

try:
    files = _load("sec_files", os.path.join(ROOT, "server", "routes", "files.py"))
    allowed = files._INLINE_MIME_ALLOWED
    for bad in ["text/html", "text/javascript", "application/javascript",
                "image/svg+xml", "application/xhtml+xml", "text/xml"]:
        check(f"inline 白名单不含 {bad}", bad not in allowed)
    for good in ["image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"]:
        check(f"inline 白名单含 {good}", good in allowed)
    check("inline 分支按白名单放行", "if inline and media_type in _INLINE_MIME_ALLOWED" in files_src)
except Exception as e:
    print(f"  无法加载 files.py（依赖缺失）: {e}")
    check("inline 白名单按源码判定不含 text/html",
          '"text/html"' not in files_src[files_src.find("_INLINE_MIME_ALLOWED"): files_src.find("_INLINE_MIME_ALLOWED") + 400])

# ============================================================
print("\n【4】files.py —— CSV 预览转义")
# ============================================================
check("CSV 表头已转义", "html.escape(str(h), quote=True)" in files_src)
check("CSV 单元格已转义", "html.escape(str(c), quote=True)" in files_src)
check("已导入 html 模块", "\nimport html\n" in files_src or "\nimport html" in files_src)

# ============================================================
print("\n【5】路径前缀比较补 os.sep（同级前缀目录）")
# ============================================================
check("_check_file_access 前缀比较带 os.sep",
      "full_path.startswith(_root_real + os.sep)" in files_src)
check("files.py 不再有裸 startswith(realpath(project_root))",
      files_src.count("startswith(os.path.realpath(project_root))") == 0)
up_src = _src("server/routes/upload.py")
check("upload.py 的 delete/all-uploads/reference 前缀比较带 os.sep",
      "startswith(_uploads_real + os.sep)" in up_src)

# ============================================================
print("\n【6】auth.py —— XFF 不盲信 + 双维度锁定键")
# ============================================================
auth_src = _src("server/routes/auth.py")
check("_client_ip 不再无条件返回 XFF 首段",
      'xff = request.headers.get("X-Forwarded-For")\n    if xff:\n        return xff.split(",")[0].strip()' not in auth_src)
check("_client_ip 走可信代理白名单", "TRUSTED_PROXY_IPS" in auth_src)
check("锁定键含用户名维度", 'f"u:{username}"' in auth_src)
check("锁定键含 IP 维度", 'f"ip:{ip}"' in auth_src)
check("登录失败字典有容量上限", "_LOGIN_FAILURES_MAX" in auth_src and "_prune_login_failures" in auth_src)

# ============================================================
print("\n【7】默认管理员口令不再是可预测常量")
# ============================================================
check("database.py 不再硬编码 admin123", 'admin_password = "admin123"' not in db_src)
check("初始口令随机生成", "secrets.token_urlsafe" in db_src)
main_src = _src("server/main.py")
check("日志不再明文打印初始口令",
      'logger.info("默认管理员密码: admin123")' not in main_src
      and 'logger.info(f"默认管理员密码' not in main_src)
check("初始口令只打到控制台", 'print(f"    默认管理员初始密码' in main_src)

# ============================================================
print("\n【8】sessions.py —— 删会话连带删图的归属与链接处理")
# ============================================================
ses_src = _src("server/routes/sessions.py")
check("删图目录以本人目录为界", 'user_root = os.path.realpath(os.path.join(project_root, "document_output", str(user["id"])))' in ses_src)
check("符号链接只摘除不递归删除", "if os.path.islink(upload_dir):" in ses_src and "os.unlink(upload_dir)" in ses_src)
check("rmtree 前仍校验归属", "real_dir.startswith(user_root + os.sep)" in ses_src)

# ============================================================
print("\n【9】sandbox.py —— 命令白名单 / os 名单 / importlib / 别名随机化")
# ============================================================
sb_src = _src("agent/sandbox.py")
check("awk 已移出命令白名单", '"awk"' not in sb_src and "'awk'" not in sb_src)
check("os 危险函数含 execl", "'execl'" in sb_src)
check("os 危险函数含 execvp", "'execvp'" in sb_src)
check("os 危险函数含 posix_spawn", "'posix_spawn'" in sb_src)
check("importlib 封堵 import_module", "'import_module'" in sb_src)
check("内部别名已随机化", "wrapper = wrapper.replace(_name," in sb_src)
check("随机化覆盖原始 open 句柄", '"_SB_ORIG_OPEN"' in sb_src and '"_orig_open"' in sb_src)
check("随机化覆盖 subprocess 别名", '"_sb_subprocess"' in sb_src)
check("随机化覆盖 __import__ 备份", '"_orig_import"' in sb_src)

# ============================================================
print("\n【10】main.py —— 异常不回显内部细节 + 安全响应头")
# ============================================================
check("全局异常不再回显 str(e)",
      'content={"detail": f"服务内部错误: {str(e)}"}' not in main_src)
check("新增安全响应头中间件", "security_headers_middleware" in main_src)
check("含 nosniff", '"X-Content-Type-Options", "nosniff"' in main_src)
# 2026-10-24：DENY → SAMEORIGIN。本站自己用 iframe 嵌 /usage 看板与 PDF 预览，
# DENY 会把它们打成白屏（曾导致「Token 消耗进不去」）。点击劫持防护要防的是
# 外部网站嵌入，同源放行不损失该能力，故此处断言必须是 SAMEORIGIN。
check("含 X-Frame-Options（SAMEORIGIN，同源 iframe 场景要放行）",
      '"X-Frame-Options", "SAMEORIGIN"' in main_src)
check("CSP frame-ancestors 为 self 而非 none",
      "frame-ancestors 'self'" in main_src)
check("含 CSP", "Content-Security-Policy" in main_src)

# ============================================================
print("\n【11】skills.py —— create_skill 变量先赋值再校验")
# ============================================================
sk_src = _src("server/routes/skills.py")
i_assign = sk_src.find('scripts = body.scripts or "[]"', sk_src.find("def create_skill"))
i_use = sk_src.find("if _requires_admin_privilege(scripts):")
check("scripts 在 admin 门禁之前赋值", 0 < i_assign < i_use, f"assign={i_assign} use={i_use}")

# ============================================================
print("\n" + "=" * 62)
print(f"结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("\n失败明细：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
