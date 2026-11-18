#!/usr/bin/env python3
"""技能上下文注入的回归测试（零依赖，临时目录，不碰真实数据库）。

守三件事：
1. 用户技能从磁盘加载时，`is_system` 必须传染到**脚本**上 —— 否则
   `_classify_skill_risk` 会把用户上传的代码当成系统技能（read 级、免审批）。
2. 单个技能说明有注入上限，一份长 SKILL.md 不能吃掉整个上下文。
3. 本轮已勾选的技能不会在通用技能段里再注入一次。

用法：python3 scripts/test_skill_context.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("skill_registry", ROOT / "agent" / "skill_registry.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)


def make_skill(base: Path, name: str, body: str, with_script: bool = False):
    d = base / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试用技能\n---\n\n{body}", encoding="utf-8"
    )
    if with_script:
        (d / "scripts" / "do_thing.py").write_text(
            "def execute() -> str:\n    \"\"\"干点事\"\"\"\n    return 'ok'\n", encoding="utf-8"
        )
    return d


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)

        print("\n[1] 来源标记必须传染到脚本")
        d = make_skill(base, "user_skill", "正文", with_script=True)
        user_sk = sr.Skill.from_folder(str(d), is_system=False)
        sys_sk = sr.Skill.from_folder(str(d))
        check("Skill 自身标记正确", user_sk.is_system is False and sys_sk.is_system is True)
        check("用户技能的脚本 is_system=False（否则会被当系统技能免审批）",
              all(s.is_system is False for s in user_sk.scripts),
              [(s.name, s.is_system) for s in user_sk.scripts])
        check("系统技能的脚本仍是 True",
              all(s.is_system is True for s in sys_sk.scripts),
              [(s.name, s.is_system) for s in sys_sk.scripts])

        print("\n[2] 注入长度上限")
        long_body = "细" * (sr.SkillRegistry.MAX_SKILL_CHARS + 500)
        dl = make_skill(base, "long_skill", long_body)
        reg = sr.SkillRegistry()
        reg.skills = {"long_skill": sr.Skill.from_folder(str(dl))}
        out = reg.build_context()
        check("长技能说明被截断", "已截断" in out, out[-60:])
        check("总长度受控（上限 + 少量包裹文本）",
              len(out) < sr.SkillRegistry.MAX_SKILL_CHARS + 400, len(out))

        print("\n[3] 勾选过的技能不重复注入")
        d2 = make_skill(base, "picked_skill", "这是被勾选的技能正文")
        reg2 = sr.SkillRegistry()
        reg2.skills = {"picked_skill": sr.Skill.from_folder(str(d2))}
        with_pick = reg2.build_context(skip_names={"picked_skill"})
        without = reg2.build_context()
        check("跳过勾选技能后整段为空（没有重复内容）", with_pick == "", with_pick[:60])
        check("不跳过时能看到该技能", "被勾选的技能正文" in without)

        print("\n[4] 技能说明被标为参考资料，不覆盖上层规则")
        d3 = make_skill(base, "n_skill", "随便的正文")
        reg3 = sr.SkillRegistry()
        reg3.skills = {"n_skill": sr.Skill.from_folder(str(d3))}
        out3 = reg3.build_context()
        check("包含「不能修改或推翻」的边界声明", "不能修改或推翻" in out3, out3[:120])

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
