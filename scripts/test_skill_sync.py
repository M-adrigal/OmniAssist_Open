#!/usr/bin/env python3
"""用户技能「磁盘 ↔ 表」同步测试。

覆盖 2026-10-23 事故场景：磁盘上有技能但表里空 → Agent 查表误判。

不依赖 fastapi（按文件加载模块）。

用法：python3 scripts/test_skill_sync.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent.skill_editor as se  # noqa: E402


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    all_ok = True

    def check(name, cond, extra=""):
        nonlocal all_ok
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            all_ok = False

    print("=" * 66)
    print("用户技能「磁盘 ↔ 表」同步测试")
    print("=" * 66)

    tmp = Path(tempfile.mkdtemp())
    se._USER_SKILLS_DIR = str(tmp / "user")

    # 隔离的数据库 + database 模块
    db_path = tmp / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # ⚠️ 表结构必须与 server/database.py 的 init_db 保持一致：
    # 2026-10-24 起新增 source / risk_level / confirmed_at 三列（用户上传技能用），
    # get_user_skills 的 SELECT 会查这三列，夹具缺列会报 "no such column"。
    conn.execute("""CREATE TABLE user_skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, skill_name TEXT NOT NULL,
        skill_content TEXT NOT NULL DEFAULT '',
        skill_scripts TEXT NOT NULL DEFAULT '[]',
        enabled INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        source TEXT DEFAULT 'editor',
        risk_level TEXT DEFAULT 'low',
        confirmed_at TIMESTAMP DEFAULT NULL)""")
    conn.commit()

    # 加载 database.py 里 user_skills 相关函数，替换其 _get_connection
    db = load_mod("test_db", "server/database.py")
    db._get_connection = lambda: conn

    sync = load_mod("test_sync", "agent/skill_sync.py")
    # skill_sync 内部 from server.database import ... 会失败（fastapi），
    # 这里把模块级的导入目标换成测试库
    class FakeDB:
        get_user_skills = staticmethod(db.get_user_skills)
        save_user_skill = staticmethod(db.save_user_skill)
        delete_user_skill = staticmethod(db.delete_user_skill)
    sync.__dict__["server"] = None
    # 直接注入假模块：让 from server.database import X 拿到 FakeDB
    import types
    fake_server = types.ModuleType("server")
    fake_database = types.ModuleType("server.database")
    for fn in ("get_user_skills", "save_user_skill", "delete_user_skill"):
        setattr(fake_database, fn, getattr(FakeDB, fn))
    fake_server.database = fake_database
    sys.modules["server"] = fake_server
    sys.modules["server.database"] = fake_database

    uid = 1
    ud = se._ensure_user_dir(uid)

    print("\n【1】事故场景：磁盘有技能，表是空的")
    # 通过真实 skill_editor 创建（它会同步写表）
    r = se.create_user_skill(uid, "grill_me", "---\nname: grill_me\ndescription: 测试\n---\n\n正文")
    check("创建成功", r.get("success") is True, r)
    check("db_sync 成功", r.get("db_sync", {}).get("ok") is True, r.get("db_sync"))
    rows = db.get_user_skills(uid)
    check("表里有 1 条记录", len(rows) == 1, rows)
    check("skill_name 正确", rows and rows[0]["skill_name"] == "grill_me", rows)
    check("skill_content 有内容", rows and "grill_me" in (rows[0]["skill_content"] or ""), rows)
    check("enabled=1", rows and rows[0]["enabled"] == 1, rows)

    print("\n【2】Agent 查表（模拟 diag_db_query 的场景）")
    rows = db.get_user_skills(uid)
    check("现在能查到了（不再是 0 行）", len(rows) > 0, rows)

    print("\n【3】update 后表内容跟着更新")
    r = se.update_user_skill(uid, "grill_me", "---\nname: grill_me\ndescription: 改过的描述\n---\n\n新正文")
    check("更新成功", r.get("success") is True, r)
    rows = db.get_user_skills(uid)
    check("仍只有 1 条（不是重复插入）", len(rows) == 1, rows)
    check("内容已刷新", "新正文" in (rows[0]["skill_content"] or ""), rows)

    print("\n【4】delete 后表记录移除（软删除）")
    r = se.delete_user_skill(uid, "grill_me")
    check("删除成功", r.get("success") is True, r)
    rows = db.get_user_skills(uid)
    check("表记录已移除", len(rows) == 0, rows)
    check("磁盘备份存在", (Path(ud) / ".trash").is_dir())

    print("\n【5】恢复后表记录回来")
    r = se.restore_user_skill(uid, "grill_me")
    check("恢复成功", r.get("success") is True, r)
    rows = db.get_user_skills(uid)
    check("表记录回来了", len(rows) == 1, rows)

    print("\n【6】对账：表被清空时能从磁盘补回（兜底）")
    conn.execute("DELETE FROM user_skills")
    conn.commit()
    r = sync.sync_user_from_fs(uid)
    check("识别出磁盘上的技能", r["on_disk"] == ["grill_me"], r)
    check("补充了表记录", "grill_me" in r["added"], r)
    rows = db.get_user_skills(uid)
    check("表里补回来了", len(rows) == 1 and rows[0]["skill_name"] == "grill_me", rows)

    print("\n【7】对账：表里的幽灵记录（磁盘已没有）被清除")
    db.save_user_skill(uid, "ghost_skill", "幽灵内容", "[]", 1)
    check("先造一条幽灵", len(db.get_user_skills(uid)) == 2)
    r = sync.sync_user_from_fs(uid)
    check("幽灵被清除", "ghost_skill" in r["removed"], r)
    rows = db.get_user_skills(uid)
    check("只剩真实技能", len(rows) == 1 and rows[0]["skill_name"] == "grill_me", rows)

    print("\n【8】对账：禁用标记必须保留（不能当幽灵删掉）")
    db.save_user_skill(uid, "some_system_skill", "", "[]", 0)  # 禁用标记
    check("禁用标记已插入", any(r["enabled"] == 0 for r in db.get_user_skills(uid)))
    r = sync.sync_user_from_fs(uid)
    check("禁用标记未被清除", not any("some_system_skill" in x for x in r["removed"]), r)
    check("kept_markers=1", r["kept_markers"] == 1, r)
    rows = db.get_user_skills(uid)
    check("禁用标记还在", any(r["skill_name"] == "some_system_skill" and r["enabled"] == 0
                             for r in rows), rows)

    print("\n【9】对账：用户的禁用状态在同步时不被打回启用")
    db.save_user_skill(uid, "grill_me", "---\nname: grill_me\n---\n\nbody", "[]", 0)  # 用户禁用了 grill_me
    r = sync.sync_user_from_fs(uid)
    rows = [x for x in db.get_user_skills(uid) if x["skill_name"] == "grill_me"]
    check("仍是禁用状态", rows and rows[0]["enabled"] == 0, rows)

    print("\n【10】多次对账是幂等的")
    a = sync.sync_user_from_fs(uid)
    b = sync.sync_user_from_fs(uid)
    check("第二次无新增", not b["added"], b)
    check("第二次无清除", not b["removed"], b)

    # 2026-10-24 回归：上传技能只存表、不落磁盘（source='uploaded'）。
    # 曾经的 bug：按「磁盘没有就是幽灵」处理 → 服务每次启动的对账
    # 都会把用户上传的技能删光（数据丢失，且用户完全无感）。
    print("\n【11】对账不得清除「只存表」的上传技能")
    db.save_user_skill(uid, "my_uploaded", "# 上传的技能\n\n正文", "[]", source="uploaded")
    db.save_user_skill(uid, "ghost_editor", "# 磁盘已没有的编辑器技能", "[]", source="editor")
    r = sync.sync_user_from_fs(uid)
    names = {x["skill_name"] for x in db.get_user_skills(uid)}
    check("上传技能保留", "my_uploaded" in names, r)
    check("真幽灵仍被清除", "ghost_editor" not in names, r)
    check("结果里标注了保留的上传技能", "my_uploaded" in (r.get("kept_uploaded") or []), r)

    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 66)
    print("✓ 全部通过" if all_ok else "✗ 存在失败用例")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
