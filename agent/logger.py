"""
日志系统 - 统一日志格式、轮转、上下文注入

零外部依赖，基于 Python logging 模块。
支持按天/按大小轮转，自动清理过期日志，线程安全的上下文注入。
"""

import logging
import logging.handlers
import os
import threading
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 线程本地存储：上下文（user_id, session_id）
# ---------------------------------------------------------------------------

_context = threading.local()


def set_context(user_id: Optional[int] = None, session_id: Optional[str] = None):
    """设置当前请求的上下文。通常在请求入口处调用。"""
    _context.user_id = user_id
    _context.session_id = session_id


def clear_context():
    """清除当前请求的上下文。通常在请求出口处调用。"""
    _context.user_id = None
    _context.session_id = None


def _get_context() -> tuple:
    return (
        getattr(_context, "user_id", None),
        getattr(_context, "session_id", None),
    )


# ---------------------------------------------------------------------------
# 自定义 Formatter
# ---------------------------------------------------------------------------

class LogFormatter(logging.Formatter):
    """统一格式：[时间戳] [级别] [模块名] [user:N] [sess:xxx] 消息"""

    # 模块名最大宽度
    MODULE_WIDTH = 8

    def format(self, record: logging.LogRecord) -> str:
        # 时间戳精确到毫秒
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        ms = int((record.created - int(record.created)) * 1000)
        timestamp = f"{ts}.{ms:03d}"

        # 级别 5 字符对齐
        level = record.levelname[:5].ljust(5)

        # 模块名左对齐
        module = record.name[: self.MODULE_WIDTH].ljust(self.MODULE_WIDTH)

        # 上下文
        user_id, session_id = _get_context()
        ctx_parts = []
        if user_id is not None:
            ctx_parts.append(f"user:{user_id}")
        if session_id:
            ctx_parts.append(f"sess:{session_id}")
        ctx = f"[{' '.join(ctx_parts)}] " if ctx_parts else ""

        # 消息
        msg = record.getMessage()

        return f"[{timestamp}] [{level}] [{module}] {ctx}{msg}"


# ---------------------------------------------------------------------------
# 日志器工厂
# ---------------------------------------------------------------------------

_log_dir: Optional[str] = None
_initialized = False
_init_lock = threading.Lock()


def _init_logging(log_dir: str, level: int = logging.DEBUG):
    """初始化日志系统（仅执行一次，线程安全）。"""
    global _log_dir, _initialized

    with _init_lock:
        if _initialized:
            return

        _log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        root_logger = logging.getLogger()
        root_logger.setLevel(level)

        formatter = LogFormatter()

        # --- 控制台输出 (INFO+) ---
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(formatter)
        root_logger.addHandler(console)

        # --- 全量文件日志 (DEBUG+)：按天轮转，保留 30 天 ---
        app_path = os.path.join(log_dir, "app.log")
        app_handler = logging.handlers.TimedRotatingFileHandler(
            app_path,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        app_handler.suffix = "%Y-%m-%d"
        app_handler.setLevel(level)
        app_handler.setFormatter(formatter)
        root_logger.addHandler(app_handler)

        # --- 错误日志 (ERROR+)：按天轮转，保留 30 天 ---
        err_path = os.path.join(log_dir, "error.log")
        err_handler = logging.handlers.TimedRotatingFileHandler(
            err_path,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        err_handler.suffix = "%Y-%m-%d"
        err_handler.setLevel(logging.ERROR)
        err_handler.setFormatter(formatter)
        root_logger.addHandler(err_handler)

        _initialized = True


def get_logger(name: str, log_dir: str = None) -> logging.Logger:
    """获取模块日志器。

    Args:
        name: 模块名，如 'chat', 'agent', 'sandbox'。自动加 'agent.' 前缀。
        log_dir: 日志目录。仅首次调用时生效，默认使用项目根目录下的 logs/。

    Returns:
        logging.Logger 实例
    """
    if log_dir is None:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "logs")
        log_dir = os.path.abspath(log_dir)

    _init_logging(log_dir)

    # 统一加前缀，避免与其他库的 logger 冲突
    full_name = f"agent.{name}" if not name.startswith("agent.") else name
    return logging.getLogger(full_name)