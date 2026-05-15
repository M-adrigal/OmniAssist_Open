import os
import sqlite3
import hashlib
import secrets
import threading
from datetime import datetime


DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent")
DB_PATH = os.path.join(DB_DIR, "users.db")

_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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

    _seed_default_permissions(conn)

    conn.execute(
        "DELETE FROM permissions WHERE role = 'user' AND resource = 'tools' AND action IN ('write', 'delete')"
    )
    conn.commit()

    existing = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    admin_password = None

    if not existing:
        admin_password = _generate_random_password(8)
        password_hash = _hash_password(admin_password)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO users (username, password_hash, user_type, description, created_at, updated_at) "
            "VALUES (?, ?, 'admin', '系统管理员', ?, ?)",
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


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, h = password_hash.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except (ValueError, AttributeError):
        return False


def authenticate(username: str, password: str) -> dict | None:
    """验证用户，成功返回用户信息字典，失败返回 None"""
    conn = _get_connection()
    row = conn.execute(
        "SELECT id, username, password_hash, user_type, description FROM users WHERE username = ?",
        (username,)
    ).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return {
            "id": row["id"],
            "username": row["username"],
            "user_type": row["user_type"],
            "description": row["description"],
        }
    return None


def get_user_by_id(user_id: int) -> dict | None:
    conn = _get_connection()
    row = conn.execute(
        "SELECT id, username, user_type, description, created_at, updated_at FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    if row:
        return dict(row)
    return None


def list_users() -> list[dict]:
    conn = _get_connection()
    rows = conn.execute(
        "SELECT id, username, user_type, description, created_at, updated_at FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, user_type: str = "user", description: str = "") -> dict:
    conn = _get_connection()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    password_hash = _hash_password(password)
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, user_type, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, password_hash, user_type, description, now, now)
        )
        conn.commit()
        user = get_user_by_id(cursor.lastrowid)
        _create_user_directories(user["id"])
        return user
    except sqlite3.IntegrityError:
        raise ValueError(f"用户名 '{username}' 已存在")


def update_user(user_id: int, **kwargs) -> dict | None:
    conn = _get_connection()
    allowed = {"password", "user_type", "description"}
    updates = {}
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            if k == "password":
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
    conn = _get_connection()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ValueError("用户不存在")
    if not verify_password(old_password, row["password_hash"]):
        raise ValueError("原密码错误")

    new_hash = _hash_password(new_password)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
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


def get_session(session_id: str) -> dict | None:
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


def update_session_messages(session_id: str, messages: list, title: str = None) -> dict | None:
    conn = _get_connection()
    import json
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    messages_json = json.dumps(messages, ensure_ascii=False)
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


def rename_session(session_id: str, title: str) -> dict | None:
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
    if not plaintext:
        return ""
    key = _get_or_create_secret()
    plain_bytes = plaintext.encode("utf-8")
    encrypted = bytes(p ^ key[i % len(key)] for i, p in enumerate(plain_bytes))
    return _base64.b64encode(encrypted).decode("ascii")


def _decrypt_db(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    key = _get_or_create_secret()
    encrypted = _base64.b64decode(ciphertext)
    decrypted = bytes(e ^ key[i % len(key)] for i, e in enumerate(encrypted))
    return decrypted.decode("utf-8")


def _mask_key(key: str) -> str:
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def get_model_config(user_id: int = None) -> dict | None:
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
    return d


def resolve_model_config(user_id: int) -> dict:
    personal = get_model_config(user_id)
    if personal and personal.get("api_key"):
        personal["config_type"] = "personal"
        return personal
    global_cfg = get_model_config(None)
    if global_cfg:
        global_cfg["config_type"] = "global"
        return global_cfg
    return {
        "api_key": "", "base_url": "", "model_name": "",
        "context_limit": "",
        "api_key_masked": "(未设置)",
        "config_type": "none",
        "show_thought": False,
    }


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