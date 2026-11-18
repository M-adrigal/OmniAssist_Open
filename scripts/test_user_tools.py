#!/usr/bin/env python3
"""用户技能工具化 + 跨用户隔离的回归测试（零依赖，不联网、不碰真实库）。

守四件事：
1. 注册表键带用户前缀，模型看到的是干净名字；
2. 别的用户的工具不会出现在下发清单里（is_foreign_user_tool）；
3. 懒注册只发生一次（第二次调用不再构建执行器）；
4. 执行器内部还有一层归属校验——猜到名字直接调也会被拒。

用法：python3 scripts/test_user_tools.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sr = _load("skill_registry", "agent/skill_registry.py")
tools_mod = _load("tools_mod", "agent/tools.py")
ut = _load("user_tools", "server/user_tools.py")

ScriptDef = sr.ScriptDef


class FakeSkill:
    def __init__(self, name, scripts, is_system=False):
        self.name = name
        self.scripts = scripts
        self.is_system = is_system


class FakeSkillRegistry:
    def __init__(self, skills):
        self._skills = skills          # user_id -> {name: FakeSkill}
        self.disabled_seen = None

    def get_enabled_skills(self, user_id=None, disabled_names=None):
        self.disabled_seen = disabled_names
        return dict(self._skills.get(user_id, {}))


def make_script(name="fetch_prices"):
    return ScriptDef(
        name=name, description="抓价格", source="def execute():\n    return 'ok'\n",
        parameters={"type": "object", "properties": {}, "required": []},
        execution_mode="local_execution",
    )


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    print("\n[1] 命名与前缀")
    check("带用户前缀", ut.tool_name_for(1, "fetch_prices") == "u1__fetch_prices",
          ut.tool_name_for(1, "fetch_prices"))
    check("还原干净名", ut.strip_prefix("u1__fetch_prices") == "fetch_prices")
    check("普通工具名不受影响", ut.strip_prefix("web_search") == "web_search")
    check("非法字符被替换", "__" not in ut.tool_name_for(2, "a b/c")[3:], ut.tool_name_for(2, "a b/c"))
    check("超长名截断到 64", len(ut.tool_name_for(9, "x" * 200)) <= ut.tool_name_for.__globals__.get("_MAX_TOOL_NAME", 64),
          len(ut.tool_name_for(9, "x" * 200)))

    print("\n[2] 归属判定")
    check("别人的工具 → 是", ut.is_foreign_user_tool("u2__x", 1) is True)
    check("自己的工具 → 否", ut.is_foreign_user_tool("u1__x", 1) is False)
    check("系统工具 → 否", ut.is_foreign_user_tool("web_search", 1) is False)
    check("畸形前缀不误判", ut.is_foreign_user_tool("ux__x", 1) is False)

    print("\n[3] 懒注册")
    reg = tools_mod.ToolRegistry()
    skills = FakeSkillRegistry({1: {"ecommerce": FakeSkill("ecommerce", [make_script()])}})
    calls = {"factory": 0}

    def fake_factory(*a, **kw):
        calls["factory"] += 1
        def _inner(**kwargs):
            kwargs.pop("_public_id", None)
            return "executed"
        return _inner

    def fake_classify(script):
        return ("exec", True, "执行你自定义的 Python 代码")

    names = ut.ensure_user_tools(reg, skills, 1, {"whatever"}, None, None,
                                 executor_factory=fake_factory, classify_risk=fake_classify)
    check("返回注册表键", names == {"u1__fetch_prices"}, names)
    check("确实注册进注册表", "u1__fetch_prices" in reg.list_tools())
    check("风险等级按用户代码判（exec + 需审批）",
          reg.get_risk_level("u1__fetch_prices") == "exec"
          and reg.needs_approval("u1__fetch_prices") is True,
          (reg.get_risk_level("u1__fetch_prices"), reg.needs_approval("u1__fetch_prices")))

    ut.ensure_user_tools(reg, skills, 1, set(), None, None,
                         executor_factory=fake_factory, classify_risk=fake_classify)
    check("第二次不重复构建执行器", calls["factory"] == 1, calls["factory"])

    print("\n[4] 系统技能不重复注册")
    sys_skills = FakeSkillRegistry({1: {"document": FakeSkill(
        "document", [make_script("save_word")], is_system=True)}})
    before = len(reg.list_tools())
    ut.ensure_user_tools(reg, sys_skills, 1, set(), None, None,
                         executor_factory=fake_factory, classify_risk=fake_classify)
    check("系统技能被跳过", len(reg.list_tools()) == before, len(reg.list_tools()))

    print("\n[5] 执行器内的归属校验（第二层防线）")
    check("所有者调用成功", reg.execute("u1__fetch_prices", {}, user_id=1) == "executed")
    denied = reg.execute("u1__fetch_prices", {}, user_id=2)
    check("别人调用被拒", "无权调用" in str(denied), denied)

    print("\n[6] 名字解析")
    check("干净名 → 注册表键",
          ut.resolve_tool_name(reg, "fetch_prices", 1) == "u1__fetch_prices")
    check("别人的干净名解析不到（落到原名）",
          ut.resolve_tool_name(reg, "fetch_prices", 2) == "fetch_prices")
    check("已是注册表键则原样返回",
          ut.resolve_tool_name(reg, "u1__fetch_prices", 1) == "u1__fetch_prices")

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
