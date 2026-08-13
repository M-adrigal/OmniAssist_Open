import os
import re
import hmac
import sqlite3
import hashlib
import secrets
import threading
from datetime import datetime
from typing import Optional
from agent.logger import get_logger

logger = get_logger("db")


DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "users.db")

_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        try:
            _local.conn = sqlite3.connect(DB_PATH)
            _local.conn.row_factory = sqlite3.Row
            _local.conn.execute("PRAGMA journal_mode=WAL")
            _local.conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as e:
            logger.error(f"数据库连接失败: {e}")
            raise
    return _local.conn


def init_db() -> str:
    """初始化数据库，创建用户表并插入默认管理员。返回管理员初始密码。"""
    os.makedirs(DB_DIR, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            user_type TEXT NOT NULL DEFAULT 'user',
            description TEXT DEFAULT '',
            must_change_password INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    try:
        conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT '新对话',
            messages TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            api_key_encrypted TEXT DEFAULT '',
            base_url TEXT DEFAULT '',
            model_name TEXT DEFAULT '',
            context_limit TEXT DEFAULT '',
            show_thought INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    try:
        conn.execute("ALTER TABLE model_configs ADD COLUMN show_thought INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute("ALTER TABLE model_configs ADD COLUMN max_iterations INTEGER DEFAULT 10")
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_configs_user_id ON model_configs(user_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            tavily_api_key_encrypted TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            resource TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(role, resource, action)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            operation TEXT NOT NULL,
            operator TEXT DEFAULT '',
            operator_id INTEGER DEFAULT 0,
            details TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_tool_name ON tool_operation_logs(tool_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_operation ON tool_operation_logs(operation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_logs_created_at ON tool_operation_logs(created_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            skill_name TEXT NOT NULL,
            skill_content TEXT NOT NULL DEFAULT '',
            skill_scripts TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_skills_user_id ON user_skills(user_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_skills_name ON user_skills(user_id, skill_name)")

    try:
        conn.execute("ALTER TABLE user_skills ADD COLUMN enabled INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass

    # 按用户隔离的加密密钥（用于用户自带 API Key / 私有 host 的技能）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_secrets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key_name TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, key_name),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_secrets_user_id ON user_secrets(user_id)")

    _seed_default_permissions(conn)

    conn.execute(
        "DELETE FROM permissions WHERE role = 'user' AND resource = 'tools' AND action IN ('write', 'delete')"
    )
    conn.commit()

    existing = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    admin_password = None

    if not existing:
        admin_password = "admin123"
        password_hash = _hash_password(admin_password)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO users (username, password_hash, user_type, description, must_change_password, created_at, updated_at) "
            "VALUES (?, ?, 'admin', '系统管理员', 1, ?, ?)",
            ("admin", password_hash, now, now)
        )
        conn.commit()

    conn.close()

    try:
        os.chmod(DB_PATH, 0o600)
    except Exception:
        pass

    return admin_password


def _generate_random_password(length: int = 8) -> str:
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return ''.join(secrets.choice(chars) for _ in range(length))


_DEFAULT_PERMISSIONS = {
    "admin": {
        "users": ["read", "write", "delete"],
        "model_config_global": ["read", "write"],
        "search_config": ["read", "write"],
        "model_config_personal": ["read", "write"],
        "tools": ["read", "write", "delete"],
        "sessions": ["read", "write", "delete"],
    },
    "user": {
        "model_config_personal": ["read", "write"],
        "tools": ["read"],
        "sessions": ["read", "write", "delete"],
    },
}


def _seed_default_permissions(conn: sqlite3.Connection):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for role, resources in _DEFAULT_PERMISSIONS.items():
        for resource, actions in resources.items():
            for action in actions:
                conn.execute(
                    "INSERT OR IGNORE INTO permissions (role, resource, action, created_at) VALUES (?, ?, ?, ?)",
                    (role, resource, action, now)
                )
    conn.commit()


def get_role_permissions(role: str) -> dict[str, list[str]]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT resource, action FROM permissions WHERE role = ? ORDER BY resource, action",
        (role,)
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        resource = row["resource"]
        action = row["action"]
        if resource not in result:
            result[resource] = []
        result[resource].append(action)
    return result


def check_permission(role: str, resource: str, action: str) -> bool:
    conn = _get_connection()
    row = conn.execute(
        "SELECT 1 FROM permissions WHERE role = ? AND resource = ? AND action = ?",
        (role, resource, action)
    ).fetchone()
    return row is not None


def set_permission(role: str, resource: str, action: str, granted: bool) -> bool:
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if granted:
        conn.execute(
            "INSERT OR IGNORE INTO permissions (role, resource, action, created_at) VALUES (?, ?, ?, ?)",
            (role, resource, action, now)
        )
    else:
        conn.execute(
            "DELETE FROM permissions WHERE role = ? AND resource = ? AND action = ?",
            (role, resource, action)
        )
    conn.commit()
    return True


def list_all_permissions() -> list[dict]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT role, resource, action FROM permissions ORDER BY role, resource, action"
    ).fetchall()
    return [dict(r) for r in rows]


# 慢哈希参数：PBKDF2-HMAC-SHA256，20 万次迭代（标准库实现，无第三方依赖）
_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 慢哈希存储口令，抵御 GPU/字典爆破。

    存储格式：pbkdf2$<iterations>$<salt_hex>$<dk_hex>
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。兼容旧版 sha256(salt+pwd) 格式（salt:hash）以便平滑迁移。"""
    if not password_hash:
        return False
    if password_hash.startswith("pbkdf2$"):
        try:
            _, it_s, salt_hex, hash_hex = password_hash.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(it_s)
            )
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    # 旧版格式：salt:hash（sha256）
    try:
        salt, h = password_hash.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except (ValueError, AttributeError):
        return False


_PASSWORD_POLICY_HINT = (
    "密码需至少 8 位，且必须同时包含：大写字母、小写字母、数字、特殊符号（如 !@#$%^&*）"
)


def validate_password_strength(password: str) -> tuple[bool, str]:
    """校验密码强度。

    规则：长度 >= 8；含小写字母、大写字母、数字、特殊符号（非字母数字）。
    返回 (是否通过, 失败原因/空)。
    """
    if not isinstance(password, str) or len(password) < 8:
        return False, "密码长度至少 8 位"
    if not re.search(r"[a-z]", password):
        return False, "密码必须包含小写字母"
    if not re.search(r"[A-Z]", password):
        return False, "密码必须包含大写字母"
    if not re.search(r"\d", password):
        return False, "密码必须包含数字"
    if not re.search(r"[^A-Za-z0-9]", password):
        return False, "密码必须包含特殊符号（非字母数字）"
    return True, ""


def get_user_must_change(user_id: int) -> bool:
    """查询用户是否处于"必须修改初始密码"状态（被强制改密拦截依赖此函数）。"""
    conn = _get_connection()
    row = conn.execute(
        "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return bool(row["must_change_password"]) if row else False


def authenticate(username: str, password: str) -> Optional[dict]:
    """验证用户，成功返回用户信息字典，失败返回 None。"""
    conn = _get_connection()
    row = conn.execute(
        "SELECT id, username, password_hash, user_type, description, must_change_password FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    if row and verify_password(password, row["password_hash"]):
        # 旧版快哈希在首次成功登录后就地升级为 PBKDF2 慢哈希
        if not row["password_hash"].startswith("pbkdf2$"):
            try:
                new_hash = _hash_password(password)
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (new_hash, row["id"]),
                )
                conn.commit()
            except Exception:
                pass
        return {
            "id": row["id"],
            "username": row["username"],
            "user_type": row["user_type"],
            "description": row["description"],
            "must_change_password": bool(row["must_change_password"]),
        }
    return None


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = _get_connection()
    row = conn.execute(
        "SELECT id, username, user_type, description, created_at, updated_at FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    if row:
        return dict(row)
    return None


def get_user_role(user_id: int) -> str:
    """获取用户角色（admin/user），用于诊断工具的权限网关。

    注意：users 表使用 user_type 字段（'admin' / 'user'）标识管理员，
    此处统一映射为 'admin' / 'user' 返回，未找到时返回 'user'（最小权限）。

    Args:
        user_id: 用户 ID

    Returns:
        str: 角色名
    """
    conn = _get_connection()
    row = conn.execute(
        "SELECT user_type FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row:
        ut = (row["user_type"] or "user")
        return "admin" if ut == "admin" else "user"
    return "user"


def list_users() -> list[dict]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT id, username, user_type, description, created_at, updated_at FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, user_type: str = "user", description: str = "") -> dict:
    ok, reason = validate_password_strength(password)
    if not ok:
        raise ValueError(f"密码强度不足：{reason}。要求：{_PASSWORD_POLICY_HINT}")
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    password_hash = _hash_password(password)
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, user_type, description, must_change_password, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (username, password_hash, user_type, description, now, now)
        )
        conn.commit()
        user = get_user_by_id(cursor.lastrowid)
        _create_user_directories(user["id"])
        return user
    except sqlite3.IntegrityError:
        raise ValueError(f"用户名 '{username}' 已存在")


def update_user(user_id: int, **kwargs) -> Optional[dict]:
    conn = _get_connection()
    allowed = {"password", "user_type", "description"}
    updates = {}
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            if k == "password":
                ok, reason = validate_password_strength(v)
                if not ok:
                    raise ValueError(f"密码强度不足：{reason}。要求：{_PASSWORD_POLICY_HINT}")
                updates["password_hash"] = _hash_password(v)
            else:
                updates[k] = v

    if not updates:
        return get_user_by_id(user_id)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates["updated_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
    conn.commit()
    return get_user_by_id(user_id)


def delete_user(user_id: int, keep_files: bool = False) -> bool:
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    if cursor.rowcount > 0 and not keep_files:
        _delete_user_files(user_id)
    return cursor.rowcount > 0


def _create_user_directories(user_id: int):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_root = os.path.join(project_root, "document_output")
    user_dir = os.path.join(output_root, str(user_id))
    sub_dirs = ["word_output", "excel_output", "pdf_output", "ppt_output", "csv_output", "image_output"]
    for sub in sub_dirs:
        os.makedirs(os.path.join(user_dir, sub), exist_ok=True)


def _delete_user_files(user_id: int):
    import shutil
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    user_dir = os.path.join(project_root, "document_output", str(user_id))
    if os.path.isdir(user_dir):
        shutil.rmtree(user_dir)


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    ok, reason = validate_password_strength(new_password)
    if not ok:
        raise ValueError(f"新密码强度不足：{reason}。要求：{_PASSWORD_POLICY_HINT}")
    conn = _get_connection()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ValueError("用户不存在")
    if not verify_password(old_password, row["password_hash"]):
        raise ValueError("原密码错误")

    new_hash = _hash_password(new_password)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
        (new_hash, now, user_id)
    )
    conn.commit()
    return True


def create_session(session_id: str, user_id: int, title: str = "新对话") -> dict:
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO sessions (id, user_id, title, messages, created_at, updated_at) VALUES (?, ?, ?, '[]', ?, ?)",
        (session_id, user_id, title, now, now)
    )
    conn.commit()
    return get_session(session_id)


def get_session(session_id: str) -> Optional[dict]:
    conn = _get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row:
        d = dict(row)
        import json
        d["messages"] = json.loads(d.get("messages", "[]"))
        return d
    return None


def list_sessions(user_id: int) -> list[dict]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT id, user_id, title, created_at, updated_at, "
        "LENGTH(messages) - LENGTH(REPLACE(messages, '\"role\"', '')) AS msg_count "
        "FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
        (user_id,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["message_count"] = max(0, d.get("msg_count", 0) // 2)
        d.pop("msg_count", None)
        result.append(d)
    return result


def update_session_messages(session_id: str, messages: list, title: str = None, user_id: int = None) -> Optional[dict]:
    conn = _get_connection()
    import json
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    messages_json = json.dumps(messages, ensure_ascii=False)

    # 检查 session 是否存在，不存在则先创建
    existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not existing:
        if user_id is None:
            logger.warning(f"会话 {session_id} 不存在且 user_id 为空，兜底使用 admin")
            user_id = 1  # 兜底使用 admin
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, messages, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, title or "新对话", messages_json, now, now)
        )
        conn.commit()
        return get_session(session_id)

    if title:
        conn.execute(
            "UPDATE sessions SET messages = ?, title = ?, updated_at = ? WHERE id = ?",
            (messages_json, title, now, session_id)
        )
    else:
        conn.execute(
            "UPDATE sessions SET messages = ?, updated_at = ? WHERE id = ?",
            (messages_json, now, session_id)
        )
    conn.commit()
    return get_session(session_id)


def rename_session(session_id: str, title: str) -> Optional[dict]:
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
        (title, now, session_id)
    )
    conn.commit()
    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    return cursor.rowcount > 0


def search_sessions(user_id: int, query: str) -> list[dict]:
    conn = _get_connection()
    import json
    like_q = f"%{query}%"
    rows = conn.execute(
        "SELECT id, user_id, title, created_at, updated_at, messages FROM sessions "
        "WHERE user_id = ? AND (title LIKE ? OR messages LIKE ?) "
        "ORDER BY updated_at DESC LIMIT 50",
        (user_id, like_q, like_q)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        messages = json.loads(d.get("messages", "[]"))
        d["message_count"] = len(messages) // 2
        d.pop("messages", None)
        result.append(d)
    return result


# ===== 模型配置加密与存储 =====

import base64 as _base64

from agent.crypto_utils import secure_encrypt, secure_decrypt

_SECRET_KEY_FILE = os.path.join(DB_DIR, ".db_secret")


def _get_or_create_secret() -> bytes:
    if os.path.isfile(_SECRET_KEY_FILE):
        with open(_SECRET_KEY_FILE, "rb") as f:
            return f.read()
    secret = secrets.token_bytes(32)
    with open(_SECRET_KEY_FILE, "wb") as f:
        f.write(secret)
    try:
        os.chmod(_SECRET_KEY_FILE, 0o600)
    except Exception:
        pass
    return secret


def _encrypt_db(plaintext: str) -> str:
    """加密明文（AEAD，见 agent.crypto_utils）；空明文返回空串。"""
    if not plaintext:
        return ""
    return secure_encrypt(plaintext, _get_or_create_secret())


def _decrypt_db(ciphertext: str) -> str:
    """解密明文；自动兼容旧 XOR 格式，损坏/密钥错误抛异常由调用方兜底。"""
    if not ciphertext:
        return ""
    return secure_decrypt(ciphertext, _get_or_create_secret())


def _mask_key(key: str) -> str:
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def get_model_config(user_id: int = None) -> Optional[dict]:
    conn = _get_connection()
    if user_id is not None:
        row = conn.execute("SELECT * FROM model_configs WHERE user_id = ?", (user_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM model_configs WHERE user_id IS NULL").fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _decrypt_db(d.get("api_key_encrypted", ""))
    d["api_key_masked"] = _mask_key(d["api_key"])
    d["show_thought"] = bool(d.get("show_thought", 0))
    try:
        d["max_iterations"] = int(d.get("max_iterations") or 10)
    except (TypeError, ValueError):
        d["max_iterations"] = 10
    return d


def resolve_model_config(user_id: int) -> dict:
    """解析用户实际生效的模型配置（个人优先、全局兜底合并）。

    策略：以全局配置为基底；若用户存在个人配置，则模型名 / BaseURL /
    上下文限制 / 思考开关 / 最大迭代次数等非凭据字段「非空即覆盖」个人值，
    API Key 缺省时回落全局。这样用户在「个人」 tab 调整模型或迭代次数，
    即使未单独填写 API Key 也能生效（旧逻辑要求必须填个人 Key 才生效）。
    """
    base = {
        "api_key": "", "base_url": "", "model_name": "",
        "context_limit": "",
        "api_key_masked": "(未设置)",
        "config_type": "global",
        "show_thought": False,
        "max_iterations": 10,
    }
    global_cfg = get_model_config(None) or {}
    personal = get_model_config(user_id) or {}

    merged = dict(base)
    merged.update({k: v for k, v in global_cfg.items() if v not in (None, "")})
    merged["config_type"] = "global"

    if personal:
        for key in ("model_name", "base_url", "context_limit", "show_thought", "max_iterations"):
            val = personal.get(key)
            if key in ("show_thought", "max_iterations"):
                if val is not None:
                    merged[key] = val
            elif val not in (None, ""):
                merged[key] = val
        if personal.get("api_key"):
            merged["api_key"] = personal["api_key"]
            merged["api_key_masked"] = personal.get("api_key_masked", "(未设置)")
        else:
            merged["api_key"] = global_cfg.get("api_key")
            merged["api_key_masked"] = global_cfg.get("api_key_masked")
        merged["config_type"] = "personal"

    return merged


def save_model_config(user_id: int = None, **kwargs) -> dict:
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    updates = {}
    if "api_key" in kwargs:
        updates["api_key_encrypted"] = _encrypt_db(kwargs["api_key"] or "")
    if "base_url" in kwargs:
        updates["base_url"] = kwargs["base_url"] or ""
    if "model_name" in kwargs:
        updates["model_name"] = kwargs["model_name"] or ""
    if "context_limit" in kwargs:
        updates["context_limit"] = kwargs["context_limit"] or ""
    if "show_thought" in kwargs:
        updates["show_thought"] = 1 if kwargs["show_thought"] else 0
    if "max_iterations" in kwargs:
        try:
            updates["max_iterations"] = max(1, int(kwargs["max_iterations"]))
        except (TypeError, ValueError):
            updates["max_iterations"] = 10

    if user_id is not None:
        existing = conn.execute("SELECT id FROM model_configs WHERE user_id = ?", (user_id,)).fetchone()
    else:
        existing = conn.execute("SELECT id FROM model_configs WHERE user_id IS NULL").fetchone()

    if existing:
        if not updates:
            return get_model_config(user_id)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE model_configs SET {set_clause}, updated_at = ? WHERE id = ?",
                     list(updates.values()) + [now, existing["id"]])
    else:
        fields = ["user_id"] + list(updates.keys()) + ["created_at", "updated_at"]
        placeholders = ", ".join("?" for _ in fields)
        values = [user_id] + list(updates.values()) + [now, now]
        conn.execute(f"INSERT INTO model_configs ({', '.join(fields)}) VALUES ({placeholders})", values)

    conn.commit()
    return get_model_config(user_id)


def delete_model_config(user_id: int) -> bool:
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM model_configs WHERE user_id = ?", (user_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_search_config() -> dict:
    conn = _get_connection()
    row = conn.execute("SELECT * FROM search_config WHERE id = 1").fetchone()
    if not row:
        return {"tavily_api_key": "", "tavily_api_key_masked": "(未设置)"}
    d = dict(row)
    d["tavily_api_key"] = _decrypt_db(d.get("tavily_api_key_encrypted", ""))
    d["tavily_api_key_masked"] = _mask_key(d["tavily_api_key"])
    return d


def save_search_config(tavily_api_key: str = None) -> dict:
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    encrypted = _encrypt_db(tavily_api_key or "")

    existing = conn.execute("SELECT id FROM search_config WHERE id = 1").fetchone()
    if existing:
        conn.execute(
            "UPDATE search_config SET tavily_api_key_encrypted = ?, updated_at = ? WHERE id = 1",
            (encrypted, now)
        )
    else:
        conn.execute(
            "INSERT INTO search_config (id, tavily_api_key_encrypted, created_at, updated_at) VALUES (1, ?, ?, ?)",
            (encrypted, now, now)
        )
    conn.commit()
    return get_search_config()


def log_tool_operation(tool_name: str, operation: str, operator: str = "",
                       operator_id: int = 0, details: str = ""):
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO tool_operation_logs (tool_name, operation, operator, operator_id, details, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tool_name, operation, operator, operator_id, details, now)
    )
    conn.commit()


def get_tool_operation_logs(tool_name: str = None, operation: str = None,
                            limit: int = 100, offset: int = 0) -> list[dict]:
    conn = _get_connection()
    conditions = []
    params = []

    if tool_name:
        conditions.append("tool_name = ?")
        params.append(tool_name)
    if operation:
        conditions.append("operation = ?")
        params.append(operation)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT id, tool_name, operation, operator, operator_id, details, created_at "
        f"FROM tool_operation_logs {where_clause} "
        f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params
    ).fetchall()
    return [dict(r) for r in rows]


# ---- 用户技能管理 ----

def get_user_skills(user_id: int, enabled_only: bool = False, skill_name: str = None) -> list[dict]:
    """获取用户自建技能列表

    Args:
        user_id: 用户 ID
        enabled_only: 是否仅返回启用的技能
        skill_name: 按名称筛选

    Returns:
        list[dict]: 技能列表
    """
    conn = _get_connection()
    conditions = ["user_id = ?"]
    params = [user_id]

    if enabled_only:
        conditions.append("enabled = 1")
    if skill_name:
        conditions.append("skill_name = ?")
        params.append(skill_name)

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT id, user_id, skill_name, skill_content, skill_scripts, enabled, "
        f"created_at, updated_at FROM user_skills WHERE {where} ORDER BY skill_name",
        params
    ).fetchall()
    return [dict(r) for r in rows]


def get_disabled_system_skills(user_id: int = None) -> set:
    """获取被禁用的技能名称集合

    从 user_skills 表中查询 enabled=0 的记录。禁用状态按用户隔离：
    传入 user_id 时只返回该用户禁用的技能；不传则返回全局（仅为兼容旧调用保留）。

    Args:
        user_id: 用户 ID

    Returns:
        set: 被禁用的技能名称集合
    """
    conn = _get_connection()
    if user_id is None:
        rows = conn.execute(
            "SELECT DISTINCT skill_name FROM user_skills WHERE enabled = 0"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT skill_name FROM user_skills "
            "WHERE enabled = 0 AND user_id = ?",
            (user_id,)
        ).fetchall()
    return {r["skill_name"] for r in rows}


def save_user_skill(user_id: int, skill_name: str, skill_content: str,
                    skill_scripts: str = "[]", enabled: int = 1) -> dict:
    """创建或更新用户技能

    Args:
        user_id: 用户 ID
        skill_name: 技能名称
        skill_content: SKILL.md 内容
        skill_scripts: 脚本 JSON 数组
        enabled: 是否启用（1=启用, 0=禁用）

    Returns:
        dict: 保存后的技能记录
    """
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing = conn.execute(
        "SELECT id FROM user_skills WHERE user_id = ? AND skill_name = ?",
        (user_id, skill_name)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE user_skills SET skill_content = ?, skill_scripts = ?, enabled = ?, "
            "updated_at = ? WHERE id = ?",
            (skill_content, skill_scripts, enabled, now, existing["id"])
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM user_skills WHERE id = ?", (existing["id"],)
        ).fetchone())
    else:
        conn.execute(
            "INSERT INTO user_skills (user_id, skill_name, skill_content, skill_scripts, "
            "enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, skill_name, skill_content, skill_scripts, enabled, now, now)
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM user_skills WHERE id = last_insert_rowid()"
        ).fetchone())


def delete_user_skill(skill_id: int) -> bool:
    """删除用户技能

    Args:
        skill_id: 技能 ID

    Returns:
        bool: 是否删除成功
    """
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM user_skills WHERE id = ?", (skill_id,))
    conn.commit()
    return cursor.rowcount > 0


def toggle_user_skill(skill_id: int, enabled: int) -> bool:
    """启用/禁用技能

    Args:
        skill_id: 技能 ID
        enabled: 1=启用, 0=禁用

    Returns:
        bool: 是否操作成功
    """
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute(
        "UPDATE user_skills SET enabled = ?, updated_at = ? WHERE id = ?",
        (enabled, now, skill_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def update_user_skill(skill_id: int, skill_content: str = None,
                      skill_scripts: str = None, enabled: int = None) -> bool:
    """更新用户技能

    Args:
        skill_id: 技能 ID
        skill_content: 新的 SKILL.md 内容
        skill_scripts: 新的脚本 JSON
        enabled: 启用状态

    Returns:
        bool: 是否更新成功
    """
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    fields = []
    params = []
    if skill_content is not None:
        fields.append("skill_content = ?")
        params.append(skill_content)
    if skill_scripts is not None:
        fields.append("skill_scripts = ?")
        params.append(skill_scripts)
    if enabled is not None:
        fields.append("enabled = ?")
        params.append(enabled)

    if not fields:
        return True

    fields.append("updated_at = ?")
    params.append(now)
    params.append(skill_id)

    cursor = conn.execute(
        f"UPDATE user_skills SET {', '.join(fields)} WHERE id = ?",
        params
    )
    conn.commit()
    return cursor.rowcount > 0