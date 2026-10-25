"""任务中断/异常对账测试（usage_pending 表，2026-10-24）。

覆盖：任务登记/终结生命周期、启动对账判中断、判异常（终结但无账）、
统计口径（近 7 天）、时间边界（未超时不得判中断）。

运行：
  /Users/yuanye/.workbuddy/binaries/python/envs/default/bin/python scripts/test_usage_pending.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ===== 隔离临时 DB =====
_TMP = tempfile.mkdtemp(prefix="usage_pending_test_")
import server.database as database  # noqa: E402

database.DB_PATH = os.path.join(_TMP, "test.db")
database._local.conn = None
database.init_db()
conn = database._get_connection()
conn.execute(
    "INSERT INTO users (username, password_hash, user_type) VALUES (?, ?, ?)",
    ("pending_tester", "x", "user"),
)
conn.commit()
TEST_UID = conn.execute("SELECT id FROM users WHERE username='pending_tester'").fetchone()["id"]

from server import usage_store  # noqa: E402

PASS = FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name}  {extra}")


def old_ts(minutes_ago: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


print("=" * 60)
print("用量 pending 对账测试")
print("=" * 60)

# 场景 1：生命周期（登记 → 终结）
print("\n[1] 生命周期")
check("登记成功", usage_store.mark_pending("s1", TEST_UID) is True)
row = conn.execute("SELECT status FROM usage_pending WHERE session_id='s1'").fetchone()
check("初始 status=0（未终结）", row["status"] == 0, row["status"])
check("终结成功", usage_store.finalize_pending("s1") is True)
row = conn.execute("SELECT status, finished_at FROM usage_pending WHERE session_id='s1'").fetchone()
check("终结后 status=1 且有 finished_at", row["status"] == 1 and row["finished_at"])
# 重复终结不炸（幂等）
check("重复终结不报错", usage_store.finalize_pending("s1") is True)

# 场景 2：启动对账 —— 未终结且超时 → 中断
print("\n[2] 对账判中断")
usage_store.mark_pending("s2", TEST_UID)
conn.execute("UPDATE usage_pending SET created_at=? WHERE session_id='s2'", (old_ts(30),))
conn.commit()
res = usage_store.reconcile_pending(stale_minutes=15)
check("超时未终结被判中断", "s2" in res["interrupted"], res)
row = conn.execute("SELECT status FROM usage_pending WHERE session_id='s2'").fetchone()
check("中断 status=2", row["status"] == 2, row["status"])
# 未超时不得判中断（防误判：任务只是跑得慢）
usage_store.mark_pending("s3", TEST_UID)
conn.execute("UPDATE usage_pending SET created_at=? WHERE session_id='s3'", (old_ts(5),))
conn.commit()
res = usage_store.reconcile_pending(stale_minutes=15)
check("未超时不得判中断", "s3" not in res["interrupted"], res)
row = conn.execute("SELECT status FROM usage_pending WHERE session_id='s3'").fetchone()
check("未超时仍 status=0", row["status"] == 0, row["status"])

# 场景 3：对账判异常 —— 已终结但账本无行
print("\n[3] 对账判异常（终结但无账）")
usage_store.mark_pending("s4", TEST_UID)
usage_store.finalize_pending("s4")   # 正常终结，但故意不记 usage
res = usage_store.reconcile_pending(stale_minutes=15)
check("终结但无账被判异常", "s4" in res["orphan"], res)
row = conn.execute("SELECT status FROM usage_pending WHERE session_id='s4'").fetchone()
check("异常 status=3", row["status"] == 3, row["status"])
# 终结且有账 → 不判异常
usage_store.mark_pending("s5", TEST_UID)
usage_store.finalize_pending("s5")
conn.execute(
    "INSERT INTO token_usage (user_id, session_id, source, total_tokens, created_at) "
    "VALUES (?, ?, 'chat', 100, ?)",
    (TEST_UID, "s5", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
)
conn.commit()
res = usage_store.reconcile_pending(stale_minutes=15)
check("终结且有账不判异常", "s5" not in res["orphan"], res)
# 重复对账不重复计（幂等）
res2 = usage_store.reconcile_pending(stale_minutes=15)
check("重复对账幂等（s2 不再计入）", "s2" not in res2["interrupted"], res2)
check("重复对账幂等（s4 不再计入）", "s4" not in res2["orphan"], res2)

# 场景 4：统计（近 7 天窗口）
print("\n[4] pending_stats 统计")
stats = usage_store.pending_stats(TEST_UID, days=7)
check("中断计数含 s2", stats["interrupted"] >= 1, stats)
check("异常计数含 s4", stats["orphan"] >= 1, stats)
check("总数含全部登记", stats["total"] >= 5, stats)
# 范围外（10 天前）不计入
usage_store.mark_pending("s6", TEST_UID)
conn.execute("UPDATE usage_pending SET created_at=?, status=? WHERE session_id='s6'",
             (old_ts(10 * 24 * 60), 2))
conn.commit()
stats2 = usage_store.pending_stats(TEST_UID, days=7)
check("10 天前的中断不计入近 7 天", stats2["interrupted"] == stats["interrupted"], stats2)

# 场景 5：用户隔离
print("\n[5] 用户隔离")
conn.execute(
    "INSERT INTO users (username, password_hash, user_type) VALUES ('other', 'x', 'user')")
conn.commit()
OTHER = conn.execute("SELECT id FROM users WHERE username='other'").fetchone()["id"]
usage_store.mark_pending("s7", OTHER)
conn.execute("UPDATE usage_pending SET created_at=?, status=? WHERE session_id='s7'",
             (old_ts(30), 2))
conn.commit()
stats_other = usage_store.pending_stats(TEST_UID, days=7)
check("看不到他人中断", stats_other["interrupted"] == stats["interrupted"], stats_other)
stats_all = usage_store.pending_stats(TEST_UID, days=7, scope_all=True)
check("scope_all 可见全部", stats_all["interrupted"] >= stats_other["interrupted"] + 1, stats_all)

# 场景 6：边界
print("\n[6] 边界")
check("空 session_id 登记失败", usage_store.mark_pending("", TEST_UID) is False)
check("空 user_id 登记失败", usage_store.mark_pending("sx", None) is False)
check("终结不存在的会话不炸", usage_store.finalize_pending("no-such") is True)
check("空 session_id 终结不炸", usage_store.finalize_pending("") is False)

print("\n" + "=" * 60)
print(f"结果: {PASS} 通过, {FAIL} 失败")
if FAILED:
    print("失败项:", FAILED)
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
