#!/usr/bin/env python3
"""清理「父行已不存在」的孤儿数据。

## 为什么需要这个脚本

`ON DELETE CASCADE` **对孤儿行不生效**：它只在「有父行被删除」时触发。
如果某行写入时父行就已经不存在（外键未开启、历史数据、直接改库），
那么父行永远不会"被删除"，级联也永远不会发生 —— 这行数据就永久残留，
界面上也删不掉（查不到、点不到），只能靠这种脚本扫。

2026-10-28 实测：`user_secrets` 有 2 行 `user_id=99`，而 users 表没有 id=99，
从 2026-08-10 一直留到被发现。

## ⚠️ 什么**不**算孤儿（这里最容易误删）

`NULL` **不是孤儿**，在多处是"平台级"的正常语义：

  - `model_providers.owner_user_id IS NULL` = 平台内置供应商（8 个内置厂商全靠它识别）
  - `model_configs.user_id IS NULL`         = 平台级模型配置

所以本脚本只处理 `IS NOT NULL 且 NOT IN users` 的行，**绝不碰 NULL**。
删掉那些 NULL 行等于删掉平台所有内置供应商 —— 服务看起来还在，但一个模型都选不了。

## 用法

  python3 scripts/clean_orphans.py            # dry-run，只报告不删（默认）
  python3 scripts/clean_orphans.py --apply    # 真正删除
  python3 scripts/clean_orphans.py --db path  # 指定库（默认 data/users.db）
"""
import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "users.db")


def candidate_columns(conn):
    """找出所有可能指向 users 的列。

    除了 user_id，还有 owner_user_id / created_by 这类命名。逐个表看列名，
    不硬编码表清单 —— 新加表自动被纳入，不会因为漏登记而扫不到。
    """
    out = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        for col in cols:
            if col == "user_id" or col.endswith("_user_id"):
                out.append((t, col))
    return out


def find_orphans(conn):
    """返回 [(表, 列, 孤儿行数)]，只统计 IS NOT NULL 且无对应 users.id 的行。"""
    found = []
    for t, col in candidate_columns(conn):
        n = conn.execute(
            f"SELECT COUNT(*) FROM {t} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN (SELECT id FROM users)"
        ).fetchone()[0]
        if n:
            found.append((t, col, n))
    return found


def main():
    ap = argparse.ArgumentParser(description="清理孤儿数据（父行已不存在的子表行）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只报告）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"库不存在: {args.db}")
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    users = [dict(r) for r in conn.execute("SELECT id, username FROM users ORDER BY id")]
    print(f"库: {args.db}")
    print(f"有效用户 {len(users)} 个: " + ", ".join(f"{u['id']}={u['username']}" for u in users))

    orphans = find_orphans(conn)
    print("\n=== 孤儿扫描（IS NOT NULL 且不在 users 表）===")
    if not orphans:
        print("  ✓ 没有孤儿行")
        print("\n注：NULL 值一律不动 —— 那是「平台级」的正常语义，不是脏数据。")
        conn.close()
        return 0

    total = 0
    for t, col, n in orphans:
        print(f"  {t}.{col}: {n} 行")
        detail = conn.execute(
            f"SELECT {col} AS uid, COUNT(*) AS n FROM {t} "
            f"WHERE {col} IS NOT NULL AND {col} NOT IN (SELECT id FROM users) "
            f"GROUP BY {col}").fetchall()
        for d in detail:
            print(f"      user_id={d['uid']} → {d['n']} 行")
        total += n
    print(f"\n合计 {total} 行")

    if not args.apply:
        print("\n[DRY-RUN] 未做任何修改。确认无误后加 --apply 执行。")
        conn.close()
        return 0

    # 先落一份一致性备份，再删
    bak = args.db + ".before_orphan_clean"
    try:
        dst = sqlite3.connect(bak)
        conn.backup(dst)
        dst.close()
        print(f"\n已备份到: {bak}")
    except Exception as e:
        print(f"\n备份失败，中止（不删任何东西）: {e}")
        conn.close()
        return 1

    conn.execute("PRAGMA foreign_keys=ON")
    removed = 0
    try:
        for t, col, _ in orphans:
            cur = conn.execute(
                f"DELETE FROM {t} WHERE {col} IS NOT NULL "
                f"AND {col} NOT IN (SELECT id FROM users)")
            removed += cur.rowcount
        conn.commit()
        print(f"已删除 {removed} 行")
    except Exception as e:
        conn.rollback()
        print(f"失败已回滚（库未改动）: {e}")
        conn.close()
        return 1

    # 复核
    left = find_orphans(conn)
    viol = list(conn.execute("PRAGMA foreign_key_check"))
    print(f"复核: 剩余孤儿 {sum(n for _, _, n in left) if left else 0} 行，"
          f"外键违规 {len(viol)} 条")
    conn.close()
    return 0 if not left and not viol else 1


if __name__ == "__main__":
    sys.exit(main())
