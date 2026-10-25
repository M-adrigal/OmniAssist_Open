#!/usr/bin/env python3
"""一次性对账：把磁盘上的用户技能同步进 user_skills 索引表。

服务启动时已会自动执行（server/main.py 的 lifespan）。
本脚本用于：
  · 服务没重启时手动触发
  · 排查「Agent 查表查不到技能」时立即修复

用法：
    python3 scripts/sync_skills_db.py            # 所有用户
    python3 scripts/sync_skills_db.py <user_id>  # 指定用户
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.skill_editor as se  # noqa: E402
from agent.skill_sync import sync_user_from_fs, reconcile_all_users  # noqa: E402


def _print_one(r):
    if r.get("error"):
        print(f"  ✗ {r['error']}")
        print("  提示：本机若未安装 fastapi，无法直连数据库。")
        print("       真实服务环境下此脚本由 lifespan 自动调用，无需手动执行。")
        return 1
    print(f"  磁盘技能: {r['on_disk']}")
    print(f"  新增到表: {r['added'] or '（无）'}")
    print(f"  清除幽灵: {r['removed'] or '（无）'}")
    print(f"  未变化: {r['unchanged']}")
    print(f"  保留禁用标记: {r['kept_markers']}")
    return 0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv

    if args:
        uid = int(args[0])
        print(f"对账用户 user_id={uid}" + ("（dry-run）" if dry else ""))
        return _print_one(sync_user_from_fs(uid))

    print("对所有用户对账" + ("（dry-run）" if dry else ""))
    r = reconcile_all_users()
    print(f"  用户数: {r['users']}")
    if r["detail"]:
        for d in r["detail"]:
            print(f"  user={d['user_id']}: 新增 {d['added'] or '（无）'}, 清除 {d['removed'] or '（无）'}")
    else:
        print("  无需修正（磁盘与表一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
