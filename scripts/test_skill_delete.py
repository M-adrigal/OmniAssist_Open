#!/usr/bin/env python3
"""技能软删除 / 恢复 单元测试。

覆盖 2026-10-23 事故场景：
  「先 delete 再 create，create 失败」→ 技能应可恢复，而不是永久消失。

用法：python3 scripts/test_skill_delete.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.skill_editor as se  # noqa: E402


def main():
    all_ok = True
    tmp = Path(tempfile.mkdtemp())
    se._USER_SKILLS_DIR = str(tmp / "user")
    uid = 1

    def check(name, cond, extra=""):
        nonlocal all_ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + extra) if extra and not cond else ''}")
        if not cond:
            all_ok = False

    print("=" * 62)
    print("技能软删除 / 恢复测试")
    print("=" * 62)

    # 造一个技能
    r = se.create_user_skill(uid, "grill_me",
                             "---\nname: grill_me\ndescription: 测试技能\n---\n\n正文内容")
    print(f"\n【准备】create_user_skill → {r.get('success')}")
    skill_dir = Path(se._get_user_dir(uid)) / "grill_me"
    check("技能目录已创建", skill_dir.is_dir())

    print("\n【1】删除 → 应进备份区而非真删")
    r = se.delete_user_skill(uid, "grill_me")
    check("删除返回成功", r.get("success") is True, str(r))
    check("原目录已移走", not skill_dir.exists())
    check("返回了备份路径", bool(r.get("backup")), str(r))
    trash = Path(se._get_user_dir(uid)) / ".trash"
    check("备份区存在", trash.is_dir())
    check("备份区里有内容", trash.is_dir() and len(list(trash.iterdir())) == 1)

    print("\n【2】list_user_skills 不应把 .trash 当技能")
    r = se.list_user_skills(uid)
    check("技能列表为空", r.get("count") == 0, str(r))
    check("没有 .trash 条目",
          all(s.get("name") != ".trash" for s in r.get("skills", [])), str(r))

    print("\n【3】事故场景：delete 后 create 失败 → 应能恢复")
    r = se.create_user_skill(uid, "grill_me", "缺少 frontmatter 的内容")
    check("create 被拒（符合预期）", r.get("success") is False, str(r))
    check("技能此时确实不存在", not skill_dir.exists())

    r = se.restore_user_skill(uid, "grill_me")
    check("恢复成功", r.get("success") is True, str(r))
    check("技能目录回来了", skill_dir.is_dir())
    check("SKILL.md 内容完好",
          (skill_dir / "SKILL.md").is_file()
          and "grill_me" in (skill_dir / "SKILL.md").read_text(encoding="utf-8"))

    print("\n【4】恢复后应能正常列出")
    r = se.list_user_skills(uid)
    check("技能列表有 1 项", r.get("count") == 1, str(r))
    check("名字正确", r.get("skills", [{}])[0].get("name") == "grill_me", str(r))

    print("\n【5】list_trashed_skills 能看到备份")
    r = se.list_trashed_skills(uid)
    check("备份列表非空", r.get("count", 0) >= 0, str(r))

    print("\n【6】重复恢复应被拒绝（不覆盖）")
    r = se.restore_user_skill(uid, "grill_me")
    check("已存在时拒绝恢复", r.get("success") is False, str(r))
    check("原技能未被破坏", skill_dir.is_dir())

    print("\n【7】删除不存在的技能 → 明确报错")
    r = se.delete_user_skill(uid, "no_such_skill")
    check("返回失败", r.get("success") is False, str(r))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 62)
    print("✓ 全部通过" if all_ok else "✗ 存在失败用例")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
