#!/usr/bin/env python3
"""技能备份清理测试：30 天自动清理 + 手动清理。

用法：python3 scripts/test_trash_cleanup.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.skill_editor as se  # noqa: E402


def make_backup(trash_dir: str, name: str, age_days: float):
    """造一个 age_days 天前的备份。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - age_days * 86400))
    p = os.path.join(trash_dir, f"{name}.{stamp}")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: x\ndescription: d\n---\n\nbody")
    return os.path.basename(p)


def main():
    all_ok = True

    def check(name, cond, extra=""):
        nonlocal all_ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            all_ok = False

    print("=" * 62)
    print("技能备份清理测试")
    print("=" * 62)

    tmp = Path(tempfile.mkdtemp())
    se._USER_SKILLS_DIR = str(tmp / "user")
    uid = 1
    ud = se._ensure_user_dir(uid)
    trash = Path(ud) / ".trash"
    trash.mkdir(parents=True, exist_ok=True)

    print("\n【准备】造 3 份备份：5 天前 / 29 天前 / 31 天前")
    b5 = make_backup(str(trash), "skill_a", 5)
    b29 = make_backup(str(trash), "skill_b", 29)
    b31 = make_backup(str(trash), "skill_c", 31)
    print(f"  {b5} / {b29} / {b31}")

    print("\n【1】默认 30 天保留 → 只删 31 天前那份")
    r = se.cleanup_trashed_skills(uid)
    check("删了 1 份", r["count"] == 1, r)
    check("删的是最老那份", r["removed"] and r["removed"][0]["backup"] == b31, r)
    check("5 天前的还在", (trash / b5).exists())
    check("29 天前的还在", (trash / b29).exists())
    check("31 天前的没了", not (trash / b31).exists())
    check("统计保留了 2 份", r["kept"] == 2, r)

    print("\n【2】dry_run 只统计不删")
    make_backup(str(trash), "skill_d", 40)
    r = se.cleanup_trashed_skills(uid, dry_run=True)
    check("统计到 1 份待删", r["count"] == 1, r)
    check("实际没删", (trash / "skill_d.20260101-000000").exists()
          or any(d.startswith("skill_d") for d in os.listdir(trash)))

    print("\n【3】手动清理：retention_days=0 → 全清")
    r = se.cleanup_trashed_skills(uid, retention_days=0)
    check("清掉了多份", r["count"] >= 3, r)
    check("备份区空了", not any(os.listdir(trash)), os.listdir(trash))

    print("\n【4】无备份时清理不报错")
    r = se.cleanup_trashed_skills(uid)
    check("返回 0", r["count"] == 0, r)

    print("\n【5】名字里没有时间戳的备份 → 保守保留（不误删）")
    trash.mkdir(parents=True, exist_ok=True)
    weird = trash / "manual_backup"
    weird.mkdir(exist_ok=True)
    r = se.cleanup_trashed_skills(uid, retention_days=0)
    check("未识别名字的没被删", weird.exists(), r)

    print("\n【6】cleanup_all_trashed_skills 跨用户")
    tmp2 = Path(tempfile.mkdtemp())
    se._USER_SKILLS_DIR = str(tmp2 / "user")
    for u in (1, 2):
        d = se._ensure_user_dir(u)
        t = Path(d) / ".trash"
        t.mkdir(parents=True, exist_ok=True)
        make_backup(str(t), "old_skill", 40)
    r = se.cleanup_all_trashed_skills(retention_days=30)
    check("扫到 2 个用户", r["users"] == 2, r)
    check("删了 2 份", r["removed"] == 2, r)

    print("\n【7】时间戳解析")
    st = se._parse_backup_stamp("my_skill.20260115-143022")
    check("能解析出时间", st is not None)
    check("不含时间戳的返回 None", se._parse_backup_stamp("plainname") is None)
    check("技能名含点也能解析", se._parse_backup_stamp("a.b.20260115-143022") is not None)

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(tmp2, ignore_errors=True)
    print("\n" + "=" * 62)
    print("✓ 全部通过" if all_ok else "✗ 存在失败用例")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
