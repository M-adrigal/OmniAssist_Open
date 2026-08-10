"""
按用户隔离的加密密钥管理。

用于存储「用户自带 API Key / 私有 host」等敏感信息，按 user_id 维度加密存储
（复用 agent.config 的加解密，密钥来自 data/.agent_config 的 salt）。

与全局 ToolSecrets（data/.tool_secrets）的本质区别：
- 每个用户有独立命名空间，多用户场景下 A 用户设置的 qweather_api_key 不会
  泄露给 B 用户；
- HTTP 执行器在请求时按「当前调用者的 user_id」解析 {secret:xxx} 占位符，
  因此同一套技能模板，不同用户可用各自申请的 Key/Host。

存储：SQLite 表 user_secrets（server/database.py 中建表），每次操作独立连接，
避免与请求线程的数据库连接耦合。
"""

import os
import re
import sqlite3
from datetime import datetime

from agent.config import _encrypt, _decrypt
from agent.logger import get_logger

logger = get_logger("user_secrets")

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_DB_PATH = os.path.join(_DATA_DIR, "users.db")

_SECRET_RE = re.compile(r"\{secret:([^}]+)\}")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def set_user_secret(user_id: int, key_name: str, value: str) -> bool:
    """设置（加密）用户密钥；已存在则更新。

    Args:
        user_id: 用户 ID
        key_name: 密钥名称，如 'qweather_api_key'
        value: 密钥明文
    Returns:
        bool: 是否成功
    """
    if not key_name or value is None:
        return False
    try:
        encrypted = _encrypt(value, _DATA_DIR)
    except Exception as e:
        logger.error(f"加密用户密钥失败(user={user_id}, key={key_name}): {e}")
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO user_secrets (user_id, key_name, encrypted_value, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, key_name) DO UPDATE SET "
            "encrypted_value=excluded.encrypted_value, updated_at=excluded.updated_at",
            (user_id, key_name, encrypted, now, now),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"写入用户密钥失败(user={user_id}, key={key_name}): {e}")
        return False
    finally:
        conn.close()


def get_user_secret(user_id: int, key_name: str, default: str = "") -> str:
    """获取用户密钥明文；不存在返回 default。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT encrypted_value FROM user_secrets WHERE user_id=? AND key_name=?",
            (user_id, key_name),
        ).fetchone()
        if not row:
            return default
        return _decrypt(row["encrypted_value"], _DATA_DIR)
    except Exception as e:
        logger.error(f"读取用户密钥失败(user={user_id}, key={key_name}): {e}")
        return default
    finally:
        conn.close()


def has_user_secret(user_id: int, key_name: str) -> bool:
    """该用户是否设置过该密钥（与值是否为空区分）"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM user_secrets WHERE user_id=? AND key_name=?",
            (user_id, key_name),
        ).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        conn.close()


def delete_user_secret(user_id: int, key_name: str) -> bool:
    """删除用户密钥。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM user_secrets WHERE user_id=? AND key_name=?",
            (user_id, key_name),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"删除用户密钥失败(user={user_id}, key={key_name}): {e}")
        return False
    finally:
        conn.close()


def _mask(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def list_user_secrets_masked(user_id: int) -> dict:
    """列出用户所有密钥名称及脱敏值。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT key_name, encrypted_value FROM user_secrets WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return {r["key_name"]: _mask(_decrypt(r["encrypted_value"], _DATA_DIR)) for r in rows}
    except Exception as e:
        logger.error(f"列出用户密钥失败(user={user_id}): {e}")
        return {}
    finally:
        conn.close()


def resolve_user_secrets_in_template(text: str, user_id: int = None) -> str:
    """按用户解析 {secret:key_name} 占位符。

    解析顺序：
    1. 若给定 user_id 且该用户设置了该密钥 → 用用户密钥；
    2. 否则回退到全局 ToolSecrets（data/.tool_secrets），保持向后兼容；
    3. 都无 → 替换为空串（与原全局行为一致）。

    Args:
        text: 含占位符的模板（URL / Header / Body）
        user_id: 当前调用者用户 ID；为 None 时直接走全局解析
    """
    if user_id is None:
        from agent.tool_secrets import resolve_secrets_in_template
        return resolve_secrets_in_template(text)

    def replacer(match):
        key_name = match.group(1)
        if has_user_secret(user_id, key_name):
            return get_user_secret(user_id, key_name, "")
        # 回退全局
        from agent.tool_secrets import get_tool_secrets
        return get_tool_secrets().get(key_name, "")

    return _SECRET_RE.sub(replacer, text)
