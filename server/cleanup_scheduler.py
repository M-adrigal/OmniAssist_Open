"""后台清理任务：定期清理过期数据。

当前任务：
  · 技能备份区（agent/skills/user/{uid}/.trash）超过保留期的备份 → 删除
    保留期 30 天（agent/skill_editor.py 的 TRASH_RETENTION_DAYS）。
    起因：2026-10-23 技能软删除改造后备份会一直堆积。

设计：
  · 用守护线程 + threading.Event 实现「可唤醒的定时循环」，
    不用 while True + sleep（那样测试时无法停止、也无法立即触发）。
  · 服务启动后延迟一段时间再跑第一次，避免与初始化抢资源。
  · 所有异常都吞掉并记日志 —— 清理任务失败绝不能影响主服务。
"""
import threading
import time

from agent.logger import get_logger

logger = get_logger("cleanup")

# 清理间隔（秒）：默认 6 小时跑一次
CLEANUP_INTERVAL = 6 * 3600

# 首次执行的延迟（秒）：服务启动后先歇一会儿，别和初始化抢资源
FIRST_RUN_DELAY = 120

# 技能备份保留天数
TRASH_RETENTION_DAYS = 30


class CleanupScheduler:
    """可停止、可立即触发的定时清理器。"""

    def __init__(self, interval=CLEANUP_INTERVAL, first_delay=FIRST_RUN_DELAY):
        self._interval = interval
        self._first_delay = first_delay
        self._stop = threading.Event()
        self._thread = None
        self._last_result = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cleanup-scheduler", daemon=True)
        self._thread.start()
        logger.info(f"清理任务已启动 (间隔 {self._interval}s, 首次延迟 {self._first_delay}s)")

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("清理任务已停止")

    def trigger_now(self):
        """立即执行一次清理（不阻塞调用方）。"""
        threading.Thread(target=self._run_once, name="cleanup-once", daemon=True).start()

    def run_once_sync(self):
        """同步执行一次清理（测试/CLI 用）。"""
        return self._run_once()

    def _loop(self):
        # 首次延迟
        if self._stop.wait(self._first_delay):
            return
        while not self._stop.is_set():
            self._run_once()
            if self._stop.wait(self._interval):
                return

    def _run_once(self):
        try:
            from agent.skill_editor import cleanup_all_trashed_skills
            r = cleanup_all_trashed_skills(retention_days=TRASH_RETENTION_DAYS)
            self._last_result = r
            if r.get("removed"):
                logger.info(f"清理过期技能备份 {r['removed']} 个 (涉及 {len(r.get('detail', []))} 个用户)")
            else:
                logger.debug(f"清理检查完成：扫描 {r.get('users', 0)} 个用户，无过期备份")
            return r
        except Exception as e:
            logger.warning(f"清理任务执行失败（已忽略）: {e}")
            return None

    @property
    def last_result(self):
        return self._last_result


# 全局单例
cleanup_scheduler = CleanupScheduler()
