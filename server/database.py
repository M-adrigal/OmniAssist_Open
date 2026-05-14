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
        return get_user_by_id(cursor.lastrowid)
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


def delete_user(user_id: int) -> bool:
    conn = _get_connection()
    cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    return cursor.rowcount > 0


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