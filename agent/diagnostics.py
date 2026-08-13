"""
诊断与管理工具集 — 给主 Agent 开放"读多给、写克制"的运维能力

设计约束（必须遵守）：
- 所有工具函数第一个业务参数之后都带 `_user_id`，内部一律先查角色，非 admin 直接拒绝。
- 只读工具：日志读取、服务状态、只读环境检查、DB SELECT、文件读取、生成文件列表。
- 写类工具（删/改名/重启）带审计日志，且限定在白名单路径内。
- 所有输出强制截断（OUTPUT_CAP），避免把大文件/大表整个灌进 LLM 上下文。
- 密钥文件（.tool_secrets / .agent_config / .db_web_password 等）一律禁止读取。
- 本模块为"加法式"能力，不修改沙箱或其他安全逻辑。

注意：这些工具在【服务进程内】直接执行（不走沙箱），因此必须靠下面的白名单与角色网关保证安全。
"""

import os
import re
import json
import sqlite3
import subprocess
import sys
import time
from agent.logger import get_logger

logger = get_logger("diagnostics")

# ==================== 常量与白名单 ====================
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS_DIR = os.path.join(_PROJECT_ROOT, "logs")
_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "users.db")
_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

# 服务监听端口（用于状态查询/重启）
_SERVICE_PORTS = (17520, 17521, 17523)

# 日志文件名白名单（防止路径穿越）
_ALLOWED_LOG_FILES = {
    "app": "app.log",
    "error": "error.log",
}
# 带日期的日志：logs/app.log.2026-08-06 之类
_LOG_DATE_RE = re.compile(r"^(app|error)\.log\.\d{4}-\d{2}-\d{2}$")

# 密钥 / 敏感文件（任何读取工具都禁止访问）
_SECRET_NAMES = {
    ".tool_secrets", ".tool_secrets.bak",
    ".agent_config", ".agent_config.bak",
    ".agent_salt", ".db_web_password", ".db_web_password.bak",
    ".db_web_password.old",
}
_SECRET_DIRS = {_DATA_DIR}

# 文件读取白名单目录（真实路径前缀）
_READ_ALLOWED_DIRS = [
    os.path.join(_PROJECT_ROOT, "agent"),
    os.path.join(_PROJECT_ROOT, "server"),
    os.path.join(_PROJECT_ROOT, "static"),
    os.path.join(_PROJECT_ROOT, "document_output"),
    os.path.join(_PROJECT_ROOT, "tests"),
    os.path.join(_PROJECT_ROOT, "logs"),
    os.path.join(_PROJECT_ROOT, "requirements.txt"),
]
# 文件读取允许的扩展名
_READ_ALLOWED_EXT = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".html", ".css", ".js", ".csv", ".log", ".sh", ".md",
}

# DB 只读允许的表（白名单，防止随便读敏感表）。
# 注意：表名需与数据库实际表名完全一致——真实表名为 search_config（单数），
# 旧代码误写为 search_configs（复数），会导致查询该表被拦截。
_DB_READ_TABLES = {
    "user_skills", "sessions", "users", "permissions",
    "model_configs", "search_config",
}

OUTPUT_CAP = 8000          # 单个工具返回的最大字符数
DB_ROWS_CAP = 200          # DB 查询最大返回行数
AUDIT_LOG = os.path.join(_LOGS_DIR, "diagnostics_audit.log")


# ==================== 内部工具 ====================
def _audit(user_id, action, detail):
    """写审计日志（谁、何时、做了什么），同步一条摘要到主日志 app.log"""
    try:
        os.makedirs(_LOGS_DIR, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        line = f"[{ts}] user={user_id} action={action} | {detail}\n"
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        # 同步到主日志，便于在 app.log 中统一观察诊断工具调用轨迹
        logger.info(f"诊断工具调用 user={user_id} action={action} | {detail}")
    except Exception:
        pass  # 审计失败不应影响主流程


def _require_admin(user_id):
    """角色网关：非 admin 拒绝。返回 (ok, msg)。"""
    try:
        from server.database import get_user_role
        role = get_user_role(user_id) if user_id else "user"
    except Exception:
        role = "user"
    if role != "admin":
        return False, "权限不足：该诊断/管理工具仅限管理员使用"
    return True, ""


def _truncate(text):
    if not isinstance(text, str):
        text = str(text)
    if len(text) > OUTPUT_CAP:
        return text[:OUTPUT_CAP] + f"\n...[输出已截断，共 {len(text)} 字符，超过上限 {OUTPUT_CAP}]"
    return text


def _realpath(p):
    return os.path.realpath(os.path.normpath(p))


def _is_secret_path(real_path):
    """是否命中密钥/敏感路径"""
    for d in _SECRET_DIRS:
        if real_path == d or real_path.startswith(d + os.sep):
            # 仅 data 目录下的密钥文件算敏感；document_output 不在 _SECRET_DIRS 中
            base = os.path.basename(real_path)
            if base in _SECRET_NAMES:
                return True
    return False


# ==================== 1) 日志读取 ====================
def diag_read_logs(log_name="app", lines=200, user_id=None):
    """读取应用运行日志 / 错误日志（只读、防穿越、截断）。

    Args:
        log_name: 'app' / 'error' / 'app.log.2026-08-06' 等
        lines: 返回尾部行数，默认 200，上限 2000
        user_id: 调用者（内部注入）
    """
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "read_logs", f"log={log_name} lines={lines}")

    # 校验文件名
    if log_name in _ALLOWED_LOG_FILES:
        fname = _ALLOWED_LOG_FILES[log_name]
    elif _LOG_DATE_RE.match(log_name):
        fname = log_name
    else:
        return _err(f"不允许的日志名: {log_name}（仅支持 app / error 及其带日期的归档）")

    path = _realpath(os.path.join(_LOGS_DIR, fname))
    if not path.startswith(_realpath(_LOGS_DIR) + os.sep) and path != _realpath(os.path.join(_LOGS_DIR, "app.log")):
        return _err("非法日志路径")
    if not os.path.isfile(path):
        return _err(f"日志文件不存在: {fname}")

    try:
        lines = max(1, min(int(lines), 2000))
    except (TypeError, ValueError):
        lines = 200

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read().splitlines()[-lines:]
        return _ok({
            "file": fname,
            "lines_returned": len(content),
            "content": _truncate("\n".join(content)),
        })
    except Exception as e:
        return _err(f"读取日志失败: {e}")


# ==================== 2) 服务状态 ====================
def diag_service_status(user_id=None):
    """查询服务运行端口的监听状态（只读，不做任何修改）。"""
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "service_status", "query")

    result = {"ports": {}, "python": _py_version(), "cwd": _PROJECT_ROOT}
    for port in _SERVICE_PORTS:
        pids = _pids_on_port(port)
        result["ports"][str(port)] = {
            "listening": bool(pids),
            "pids": pids,
        }
    return _ok(result)


def _py_version():
    return f"{sys.version.split()[0]} ({sys.executable})"


def _pids_on_port(port):
    """返回监听该端口的进程 PID 列表（macOS/Linux 通用）。"""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return [int(x) for x in out.stdout.strip().split("\n") if x.strip()]
    except Exception:
        pass
    return []


# ==================== 3) 受控重启 ====================
def diag_restart_service(user_id=None):
    """受控重启主服务（admin 专用，带审计）。

    采用"独立守护子进程"模式：本工具只负责启动一个 detached 的重启助手，
    助手会先停掉当前监听 17520 的旧进程，再启动新服务。这样即使旧进程退出，
    助手（已 setsid 脱离会话）仍存活并完成重生，避免端口残留孤儿。
    """
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "restart_service", "requested")

    helper = os.path.join(_PROJECT_ROOT, "server", "_restart_helper.py")
    if not os.path.isfile(helper):
        return _err("重启助手脚本缺失: server/_restart_helper.py")

    try:
        # start_new_session=True 使助手脱离当前进程会话，父进程退出后仍可运行
        subprocess.Popen(
            [sys.executable, helper],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=_PROJECT_ROOT,
        )
        return _ok({
            "status": "restarting",
            "message": "已触发受控重启，助手进程将在约 3 秒内停掉旧服务并拉起新服务。"
                       "当前请求可能短暂中断，稍后刷新即可。",
        })
    except Exception as e:
        return _err(f"重启触发失败: {e}")


# ==================== 4) 只读环境检查（结构化，非自由 shell） ====================
def diag_check_env(check_type, value="", user_id=None):
    """结构化只读环境检查（不开放自由 shell，零注入风险）。

    check_type 取值：
      - python_version : 返回 Python 版本
      - pip_package    : value=包名，检查是否已安装及版本
      - file_exists    : value=相对项目的路径，检查文件是否存在
      - port_listen    : value=端口号，检查是否监听
    """
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "check_env", f"type={check_type} value={value}")

    ct = (check_type or "").strip().lower()
    try:
        if ct == "python_version":
            return _ok({"python": _py_version(),
                        "pip": _pip_version()})

        if ct == "pip_package":
            pkg = (value or "").strip().lower()
            if not pkg or not re.match(r"^[a-z0-9_.\-]+$", pkg):
                return _err("包名非法（仅允许字母数字._-）")
            return _ok({"package": pkg, **_pip_show(pkg)})

        if ct == "file_exists":
            if not value:
                return _err("请提供待检查路径")
            real = _realpath(os.path.join(_PROJECT_ROOT, value))
            if not real.startswith(_PROJECT_ROOT):
                return _err("路径超出项目根目录")
            return _ok({
                "path": os.path.relpath(real, _PROJECT_ROOT),
                "exists": os.path.exists(real),
                "is_file": os.path.isfile(real) if os.path.exists(real) else False,
                "is_dir": os.path.isdir(real) if os.path.exists(real) else False,
                "size": os.path.getsize(real) if os.path.isfile(real) else None,
            })

        if ct == "port_listen":
            try:
                port = int(value)
            except (TypeError, ValueError):
                return _err("端口必须是数字")
            pids = _pids_on_port(port)
            return _ok({"port": port, "listening": bool(pids), "pids": pids})

        return _err(f"不支持的检查类型: {check_type}（可选 python_version/pip_package/file_exists/port_listen）")
    except Exception as e:
        return _err(f"环境检查失败: {e}")


def _pip_version():
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip().split("\n")[0] if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _pip_show(pkg):
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0:
            info = {}
            for line in out.stdout.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k.strip().lower()] = v.strip()
            return {
                "installed": True,
                "version": info.get("version", ""),
                "location": info.get("location", ""),
            }
        return {"installed": False}
    except Exception:
        return {"installed": False, "error": "pip show 异常"}


# ==================== 5) DB 只读查询 ====================
def diag_db_query(sql, user_id=None):
    """数据库只读查询（SELECT-only + 表白名单 + 只读连接 + 截断）。

    Args:
        sql: 单条 SELECT 语句
    """
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "db_query", sql[:200])

    if not isinstance(sql, str) or not sql.strip():
        return _err("SQL 不能为空")
    s = sql.strip().rstrip(";").strip()
    # 必须是 SELECT（允许 WITH ... SELECT 形式）
    up = re.sub(r"\s+", " ", s, count=0).upper()
    if not (up.startswith("SELECT") or up.startswith("WITH")):
        return _err("仅允许 SELECT 查询（含 WITH 起始的只读查询）")
    # 禁止任何写/危险关键字
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "TRUNCATE", "REPLACE", "ATTACH", "PRAGMA", "VACUUM",
                 "EXEC", "EXECUTE", "GRANT", "REVOKE", "PRAGMA"]
    # 用词边界匹配，避免误伤列名
    for kw in forbidden:
        if re.search(rf"\b{kw}\b", up):
            return _err(f"检测到被禁止的关键字: {kw}")
    # 表白名单：提取所有 FROM / JOIN 后的表名（统一小写比较）
    tables = set(t.lower() for t in re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*)", up))
    bad = tables - _DB_READ_TABLES
    if bad:
        return _err(f"以下表不在只读白名单内: {', '.join(sorted(bad))}（允许: {', '.join(sorted(_DB_READ_TABLES))}）")

    if not os.path.isfile(_DB_PATH):
        return _err(f"数据库文件不存在: {_DB_PATH}")

    try:
        # 只读模式打开（mode=ro），且设置 uri
        uri = f"file:{_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(s)
        rows = cur.fetchmany(DB_ROWS_CAP + 1)
        cols = [d[0] for d in cur.description] if cur.description else []
        truncated = len(rows) > DB_ROWS_CAP
        rows = rows[:DB_ROWS_CAP]
        data = [dict(r) for r in rows]
        conn.close()
        return _ok({
            "columns": cols,
            "row_count": len(data),
            "truncated": truncated,
            "rows": data,
        })
    except Exception as e:
        return _err(f"查询执行失败: {e}")


# ==================== 6) 文件读取 ====================
def diag_read_file(path, user_id=None):
    """读取配置文件 / 技能定义 / 沙箱脚本（白名单目录 + 扩展名，排除密钥）。"""
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "read_file", path)

    if not path:
        return _err("请提供文件路径")
    real = _realpath(os.path.join(_PROJECT_ROOT, path)) if not os.path.isabs(path) else _realpath(path)
    # 必须落在白名单目录内
    in_allowed = any(
        real == d or real.startswith(d + os.sep) for d in _READ_ALLOWED_DIRS
    )
    if not in_allowed:
        return _err(f"路径不在允许读取的目录内: {path}")
    # 密钥文件禁止
    if _is_secret_path(real):
        return _err("该文件为敏感密钥文件，禁止读取")
    if not os.path.isfile(real):
        return _err(f"文件不存在: {path}")
    ext = os.path.splitext(real)[1].lower()
    if ext not in _READ_ALLOWED_EXT:
        return _err(f"扩展名 {ext} 不在允许读取范围内")
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return _ok({
            "path": path,
            "size": len(content),
            "content": _truncate(content),
        })
    except Exception as e:
        return _err(f"读取文件失败: {e}")


# ==================== 7) 生成文件管理 ====================
def diag_list_files(target_user_id=None, user_id=None):
    """列出 document_output 下已生成的文件（只读展示，支持按用户过滤）。"""
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "list_files", f"target={target_user_id}")

    root = os.path.join(_PROJECT_ROOT, "document_output")
    if not os.path.isdir(root):
        return _ok({"root": "document_output", "users": []})

    items = []
    # 若指定了 target_user_id，只看该用户；否则列出所有用户
    if target_user_id is not None:
        user_dirs = [str(target_user_id)]
    else:
        user_dirs = sorted(
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
        )

    for uid in user_dirs:
        udir = os.path.join(root, uid)
        if not os.path.isdir(udir):
            continue
        files = []
        for cat in sorted(os.listdir(udir)):
            cdir = os.path.join(udir, cat)
            if not os.path.isdir(cdir):
                continue
            for fn in sorted(os.listdir(cdir)):
                fp = os.path.join(cdir, fn)
                if os.path.isfile(fp):
                    files.append({
                        "category": cat,
                        "name": fn,
                        "size": os.path.getsize(fp),
                        "rel_path": os.path.relpath(fp, _PROJECT_ROOT),
                    })
        items.append({"user_id": uid, "file_count": len(files), "files": files})

    return _ok({"root": "document_output", "users": items})


def diag_delete_file(rel_path, user_id=None):
    """删除 document_output 下的某个生成文件（写操作，带审计 + 白名单）。"""
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    if not rel_path:
        return _err("请提供文件路径")
    real = _realpath(os.path.join(_PROJECT_ROOT, rel_path))
    root = _realpath(os.path.join(_PROJECT_ROOT, "document_output"))
    if not (real == root or real.startswith(root + os.sep)):
        return _err("只能删除 document_output 下的文件")
    if not os.path.isfile(real):
        return _err(f"文件不存在: {rel_path}")
    _audit(user_id, "delete_file", rel_path)
    try:
        os.remove(real)
        return _ok({"deleted": rel_path, "status": "success"})
    except Exception as e:
        return _err(f"删除失败: {e}")


def diag_rename_file(old_rel_path, new_name, user_id=None):
    """重命名 document_output 下的某个生成文件（写操作，带审计 + 白名单）。

    new_name 仅允许文件名（不含路径），自动保持原目录。
    """
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    if not old_rel_path or not new_name:
        return _err("请提供原路径与新文件名")
    if "/" in new_name or "\\" in new_name or new_name in (".", ".."):
        return _err("新文件名不能包含路径分隔符")
    real = _realpath(os.path.join(_PROJECT_ROOT, old_rel_path))
    root = _realpath(os.path.join(_PROJECT_ROOT, "document_output"))
    if not (real == root or real.startswith(root + os.sep)):
        return _err("只能重命名 document_output 下的文件")
    if not os.path.isfile(real):
        return _err(f"文件不存在: {old_rel_path}")
    new_real = _realpath(os.path.join(os.path.dirname(real), new_name))
    if not new_real.startswith(root + os.sep):
        return _err("重命名目标超出 document_output")
    _audit(user_id, "rename_file", f"{old_rel_path} -> {new_name}")
    try:
        os.rename(real, new_real)
        return _ok({
            "renamed": os.path.relpath(real, _PROJECT_ROOT),
            "to": os.path.relpath(new_real, _PROJECT_ROOT),
            "status": "success",
        })
    except Exception as e:
        return _err(f"重命名失败: {e}")


# ==================== 8) SKILL.md 格式校验 ====================
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def validate_skill_md(content):
    """校验 SKILL.md 格式是否合法、字段是否完整。

    规则：
      - 必须以 YAML frontmatter（--- 包裹）开头
      - frontmatter 至少含 name、description 两个字段
      - name 必须为合法标识（字母数字下划线，建议小写）
      - 正文（指令部分）不能为空

    Returns:
        (ok: bool, message: str)
    """
    if not isinstance(content, str) or not content.strip():
        return False, "SKILL.md 内容为空"

    m = _FRONTMATTER_RE.match(content)
    if not m:
        return False, "缺少 YAML frontmatter（应以 --- 开头并以 --- 结束）"

    fm_text, body = m.group(1), m.group(2)
    # 解析简单 key: value（不引入 yaml 依赖）
    fm = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()

    if "name" not in fm or not fm["name"]:
        return False, "frontmatter 缺少必填字段: name"
    if "description" not in fm or not fm["description"]:
        return False, "frontmatter 缺少必填字段: description"

    name = fm["name"]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
        return False, f"name 必须是合法标识（字母开头，仅含字母数字下划线），当前为: {name}"

    if not body.strip():
        return False, "SKILL.md 正文（指令部分）不能为空"

    return True, "SKILL.md 格式合法"


def diag_validate_skill(skill_md, user_id=None):
    """供 Agent 调用的技能校验工具（只读，无副作用）。"""
    ok, msg = _require_admin(user_id)
    if not ok:
        return _err(msg)
    _audit(user_id, "validate_skill", "check")
    valid, message = validate_skill_md(skill_md)
    return _ok({"valid": valid, "message": message})


# ==================== 返回格式helper ====================
def _ok(data):
    return json.dumps({"success": True, **data}, ensure_ascii=False)


def _err(message):
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)
