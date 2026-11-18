#!/usr/bin/env python3
"""用户技能脚本链路 + 审批记忆回归（2026-10-27 事故）。

事故链路：create_user_skill 的参数说明教模型传「工具定义 JSON」→
skill_editor 落盘 .json（无代码）→ DB skill_scripts 只有 schema →
执行时 exec 空源码 → NameError: name 'execute' is not defined。

覆盖：
  A. 正确格式：code（Python 源码）→ 落盘 .py、AST 可解析、DB 同步出 source
  B. 旧坏格式（content=dict）→ 拒绝并给出指引
  C. 缺 execute / 语法错误 → 拒绝
  D. update 校验失败不得清空已有脚本（先校验后写盘）
  E. from_db_row 跳过无代码脚本（幽灵工具不再暴露）
  F. 审批记忆：同签名放行 / 参数变化仍询问 / 用户隔离 / 签名稳定

需要 fastapi（approval_store 经 server 包导入）；用 run_tests 挑解释器执行。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        ok = False


MIN_CODE = (
    "def execute(product: str, platforms=None) -> str:\n"
    "    '''生成检索词。'''\n"
    "    return f\"{product} 价格对比\"\n"
)

BAD_NO_EXEC = "def run(x):\n    return x\n"
BAD_SYNTAX = "def execute(:\n    return 1\n"


def main():
    import server.database as database
    from agent.skill_editor import create_user_skill, update_user_skill
    from agent.skill_registry import Skill, SkillRegistry

    # ===== 隔离临时 DB 与技能目录（不隔离会把测试技能写进真实仓库！）=====
    import agent.skill_editor as _se
    _tmp = tempfile.mkdtemp(prefix="user_skill_scripts_test_")
    _se._USER_SKILLS_DIR = os.path.join(_tmp, "skills", "user")
    _se._SKILLS_ROOT = os.path.join(_tmp, "skills")
    database.DB_PATH = os.path.join(_tmp, "test.db")
    database._local.conn = None
    database.init_db()
    uid = database.create_user("script_tester", "pw123456", "user")["id"]

    print("=" * 68)
    print("用户技能脚本链路 + 审批记忆")
    print("=" * 68)

    print("\n【A】正确格式：code 源码 → 落盘 .py + AST 解析 + DB 有源码")
    r = create_user_skill(uid, "price_ok", "---\nname: price_ok\ndescription: 测试\n---\n\n正文",
                          tools=[{"name": "build_queries", "code": MIN_CODE}])
    check("创建成功", r.get("success"), r.get("message"))
    py_path = Path(_se._USER_SKILLS_DIR) / str(uid) / "price_ok/scripts/build_queries.py"
    check("落盘是 .py 源码", py_path.exists() and "def execute" in py_path.read_text(encoding="utf-8"))
    skill = Skill.from_folder(str(py_path.parent.parent), is_system=False)
    check("AST 解析出工具 schema（含 product 参数）",
          skill.scripts and "product" in json.dumps(skill.scripts[0].parameters))
    check("脚本来源标记为用户级（is_system=False）",
          skill.scripts and skill.scripts[0].is_system is False)

    print("\n【B】旧坏格式：content=dict（工具定义 JSON）→ 拒绝并指引")
    r = create_user_skill(uid, "price_bad", "---\nname: price_bad\ndescription: x\n---\n\n正文",
                          tools=[{"name": "t1", "content": {"name": "t1", "parameters": {}}}])
    check("被拒绝", r.get("success") is False)
    check("报错指向正确做法（提 code 与 execute）", "code" in r.get("message", "") and "execute" in r.get("message", ""),
          r.get("message"))

    print("\n【C】缺 execute / 语法错误 → 拒绝")
    r = create_user_skill(uid, "price_bad2", "---\nname: price_bad2\ndescription: x\n---\n\n正文",
                          tools=[{"name": "t1", "code": BAD_NO_EXEC}])
    check("缺 execute 被拒", r.get("success") is False and "execute" in r.get("message", ""))
    r = create_user_skill(uid, "price_bad3", "---\nname: price_bad3\ndescription: x\n---\n\n正文",
                          tools=[{"name": "t1", "code": BAD_SYNTAX}])
    check("语法错误被拒", r.get("success") is False and "语法错误" in r.get("message", ""))

    print("\n【D】update 校验失败不得清空已有脚本")
    r = update_user_skill(uid, "price_ok",
                          tools=[{"name": "t2", "code": BAD_NO_EXEC}])
    check("坏更新被拒", r.get("success") is False)
    check("原脚本仍在（先校验后写盘）", py_path.exists())

    print("\n【E】from_db_row 跳过无代码脚本（幽灵工具不再暴露）")
    from agent.skill_registry import ScriptDef
    row = {"id": 9, "skill_name": "ghost", "skill_content": "---\nname: ghost\ndescription: x\n---\n\n正文",
           "skill_scripts": json.dumps([
               {"name": "ghost_tool", "parameters": {"type": "object"}, "source": ""},
               {"name": "real_tool", "parameters": {"type": "object"}, "source": MIN_CODE},
           ])}
    skill = Skill.from_db_row(row)
    names = [s.name for s in skill.scripts]
    check("无源码脚本被跳过", "ghost_tool" not in names, names)
    check("有源码脚本保留", "real_tool" in names, names)

    print("\n【F】审批记忆（同签名放行 / 参数变化仍问 / 用户隔离）")
    from server.approval_store import approval_store
    from server.routes.chat import _approval_signature

    sig1 = _approval_signature("u1__compare", {"product": "iPhone"})
    sig2 = _approval_signature("u1__compare", {"product": "iPhone"})
    sig3 = _approval_signature("u1__compare", {"product": "小米15"})
    check("同工具同参数签名稳定", sig1 == sig2)
    check("参数变化签名跟着变", sig1 != sig3)

    approval_store.remember_allowed("sess-mem", 7, [sig1])
    check("已批准签名放行", approval_store.is_allowed("sess-mem", 7, sig1))
    check("参数变化不放行", not approval_store.is_allowed("sess-mem", 7, sig3))
    check("其他用户不放行", not approval_store.is_allowed("sess-mem", 8, sig1))
    check("其他会话不放行", not approval_store.is_allowed("sess-other", 7, sig1))

    print("\n【G】工具注册链路：写盘 → 表索引 → list 回报 → ensure_user_tools 注册")
    # 2026-10-27 事故（第二起，与 A~E 同源但独立）：
    # _scripts_json 只认 scripts/*.json，而落盘的是 *.py → 表里 skill_scripts 恒为 []
    # → load_user_skills 用表的空脚本版本覆盖磁盘版本 → ensure_user_tools 空转
    # → 模型看不到 build_queries / compare_prices；同时 list_user_skills 的
    # tool_count 数 .json 恒为 0、description 把整段 frontmatter 吞进去。
    from agent.skill_editor import list_user_skills, get_user_skill
    from agent.skill_sync import _scripts_json, sync_user_skill
    from agent.skill_registry import SkillRegistry

    TWO_TOOLS = [
        {"name": "build_queries", "code": MIN_CODE},
        {"name": "compare_prices",
         "code": "def execute(rows: str) -> str:\n    '''比价。'''\n    return rows\n"},
    ]
    md = "---\nname: price_chain\ndescription: 多渠道比价助手\n---\n\n这里是正文指令部分。\n"
    r = create_user_skill(uid, "price_chain", md, tools=TWO_TOOLS)
    check("创建成功", r.get("success"), r.get("message"))

    # G1. 表里的索引必须包含真实代码（否则 from_db_row 拿到的是空脚本）
    chain_dir = Path(_se._USER_SKILLS_DIR) / str(uid) / "price_chain"
    packed = json.loads(_scripts_json(str(chain_dir)))
    check("表索引包含 2 个脚本", len(packed) == 2, [p.get("name") for p in packed])
    check("表索引带真实源码（可执行）",
          all((p.get("source") or "").strip() for p in packed))

    # G2. list_user_skills 的 tool_count 与磁盘一致，description 只有一行
    listed = list_user_skills(uid)["skills"]
    row_c = next((s for s in listed if s["name"] == "price_chain"), {})
    check("tool_count = 2（不再恒为 0）", row_c.get("tool_count") == 2, row_c.get("tool_count"))
    check("description 只取 frontmatter 的值",
          row_c.get("description") == "多渠道比价助手", row_c.get("description"))
    check("description 未吞入 --- 或 name:", not str(row_c.get("description", "")).startswith("---"))
    check("tools 列出工具名",
          sorted(row_c.get("tools") or []) == ["build_queries", "compare_prices"],
          row_c.get("tools"))

    # G3. get_user_skill 能回读源码（否则模型会以为技能是空的、重复创建）
    detail = get_user_skill(uid, "price_chain")
    check("get_user_skill 回读 2 个工具", len(detail.get("tools") or []) == 2)
    check("回读内容含 execute", "def execute" in (detail["tools"][0].get("code") or ""))

    # G4. 关键回归：磁盘版与表版同名时，脚本不能被表的空版本覆盖
    sync_user_skill(uid, "price_chain")   # 先把表写成「空脚本」以复现事故现场
    import server.database as _db
    _db.save_user_skill(uid, "price_chain", md, "[]", 1)   # 人为制造表里无脚本
    reg = SkillRegistry.__new__(SkillRegistry)
    reg.skills, reg._user_skills = {}, {}
    reg.load_user_skills_from_fs(uid)                      # 先磁盘（有 2 个脚本）
    reg.load_user_skills(uid, _db.get_user_skills(uid))    # 再表（脚本为空）
    got = reg.get_user_skills(uid).get("price_chain")
    check("表覆盖后脚本仍在（磁盘为真相源）",
          got is not None and len(got.scripts) == 2,
          len(got.scripts) if got else "技能丢失")
    check("技能带 from_db 来源标记可用", hasattr(got, "from_db"))

    # G5. ensure_user_tools 能真正注册出两个工具
    try:
        from server.user_tools import ensure_user_tools, tool_name_for
        from agent.tools import ToolRegistry
        reg2 = ToolRegistry()
        reg.load_user_skills_from_fs(uid)
        names = ensure_user_tools(
            reg2, reg, uid, set(), None, None,
            executor_factory=lambda *a, **k: (lambda **kw: "stub"),
            classify_risk=lambda s: ("read", False, ""),
        )
        expect = {tool_name_for(uid, "build_queries"), tool_name_for(uid, "compare_prices")}
        check("ensure_user_tools 注册出 2 个工具", expect.issubset(names), sorted(names))
        check("注册表里能查到工具", expect.issubset(set(reg2.list_tools())), sorted(reg2.list_tools()))
    except ImportError as e:
        print(f"  - 跳过 G5（缺依赖: {e}）")

    print("\n【H】存量数据修复：表里 skill_scripts 为 [] 时对账必须补回")
    # 用户升级后最常见的状态：SKILL.md 没变、表里 skill_scripts 是空数组
    # （旧代码只认 .json 的后果）。对账若只比对 skill_content 会判成
    # "unchanged"，空索引永远修不回来 → 工具始终不出现。
    from agent.skill_sync import sync_user_from_fs
    _db.save_user_skill(uid, "price_chain", md, "[]", 1)   # 模拟升级前的旧记录
    before = _db.get_user_skills(uid, skill_name="price_chain")[0]
    check("前置：表里脚本索引为空", (before.get("skill_scripts") or "[]").strip() == "[]")
    rep = sync_user_from_fs(uid)
    after = _db.get_user_skills(uid, skill_name="price_chain")[0]
    fixed = json.loads(after.get("skill_scripts") or "[]")
    check("对账后脚本索引被补回", len(fixed) == 2, len(fixed))
    check("对账结果里有修复痕迹",
          any("price_chain" in x for x in rep.get("added", [])), rep.get("added"))

    print("\n" + "=" * 68)
    print("✓ 全部通过" if ok else "✗ 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
