#!/usr/bin/env python3
"""时间基准统一迁移：把库里残留的非本地时间修成与全库一致的本地时间。

背景（2026-10-27 数据审计结论）：
- 全库口径是**本地时间**（所有写入路径都显式传 datetime.now()），存量数据
  实测也是本地时间 —— 之前担心的「CURRENT_TIMESTAMP 默认值混入 UTC」并未发生。
- 仅两处例外：
  1. `search_usage.updated_at`：该表写入时显式用了 CURRENT_TIMESTAMP（UTC），
     与同一行的 day（本地日期）差 8 小时 —— 真实发生（tavily 行 10:48 vs 18:48）
  2. `users` 表有个别**数字型时间戳**（time.time() 的浮点值，测试造号写入），
     前端按字符串展示会直接显示数字

本脚本做三件事（幂等，靠 PRAGMA user_version 标记，重复执行直接跳过）：
  1. users.created_at / updated_at：数字型 → 本地时间字符串
  2. search_usage.updated_at：UTC → 本地（+8 小时；服务器按本地时区换算）
  3. 写 PRAGMA user_version = 1 作为「已迁移」标记

用法：
  python3 scripts/migrate_timezone.py --dry-run   # 只看要改什么，不写
  python3 scripts/migrate_timezone.py             # 备份后执行

服务器部署：git pull 后、重启服务前执行一次（见 deploy.md）。
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "users.db"
TZ_MARKER = 1          # user_version=1 表示「时区统一迁移已完成」
UTC_OFFSET_HOURS = 8   # 服务器部署在东八区；改部署时区要同步改这里


def _fmt_ts(v) -> str:
    """数字型时间戳 → 本地时间字符串。"""
    ts = float(v)
    if ts > 1e11:          # 毫秒级
        ts /= 1000
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def migrate(conn: sqlite3.Connection, dry_run: bool = False) -> list:
    """执行迁移，返回改动摘要。幂等：user_version >= 1 直接跳过。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= TZ_MARKER:
        return [f"user_version={version}，时区迁移已做过，跳过"]

    changes = []

    # ---- 1) users 表数字型时间戳 → 本地字符串 ----
    rows = conn.execute("SELECT id, created_at, updated_at FROM users").fetchall()
    fixed = 0
    for r in rows:
        sets, params = [], []
        for col in ("created_at", "updated_at"):
            v = r[col]
            if isinstance(v, (int, float)):
                sets.append(f"{col} = ?")
                params.append(_fmt_ts(v))
        if sets:
            if not dry_run:
                conn.execute(
                    f"UPDATE users SET {', '.join(sets)} WHERE id = ?",
                    params + [r["id"]],
                )
            changes.append(
                f"users.id={r['id']}: 数字时间戳 → 本地字符串 "
                f"({_fmt_ts(r['created_at']) if isinstance(r['created_at'], (int, float)) else 'created_at 原样'})")
            fixed += 1
    if not fixed:
        changes.append("users: 无数字型时间戳，无需修")

    # ---- 2) search_usage.updated_at：UTC → 本地 ----
    try:
        cur = conn.execute(
            "UPDATE search_usage "
            f"SET updated_at = datetime(updated_at, '+{UTC_OFFSET_HOURS} hours') "
            "WHERE updated_at IS NOT NULL AND typeof(updated_at) = 'text'")
        if cur.rowcount:
            changes.append(f"search_usage.updated_at: {cur.rowcount} 行 UTC → 本地（+{UTC_OFFSET_HOURS}h）")
        else:
            changes.append("search_usage: 无需修")
    except sqlite3.OperationalError as e:
        changes.append(f"search_usage: 表不存在，跳过（{e}）")

    # ---- 3) 打标记 ----
    if not dry_run:
        conn.execute(f"PRAGMA user_version = {TZ_MARKER}")
        conn.commit()

    return changes


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="只打印要改什么，不写库")
    args = ap.parse_args()

    if not DB.exists():
        print(f"数据库不存在: {DB}")
        return 1

    # 备份用 sqlite backup API（WAL 模式下直接 cp 文件会丢未 checkpoint 的内容）
    if not args.dry_run:
        bak = DB.with_suffix(".db.bak-tz")
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(bak)
        src.backup(dst)
        dst.close()
        src.close()
        print(f"已备份 → {bak}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    changes = migrate(conn, dry_run=args.dry_run)
    print("迁移内容：")
    for c in changes:
        print(f"  - {c}")
    conn.close()
    print("完成" if not args.dry_run else "（dry-run，未写库）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
