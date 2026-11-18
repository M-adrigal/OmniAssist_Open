"""
用户 Skill 编辑器 — 提供创建/修改/删除用户级 Skill 的工具

基于 OpenClaw/Hermes 思路：每个用户有独立的 Skill 仓库，
模型可以按需复盘优化 Skill 内容，实现自我进化。

目录结构：
  agent/skills/user/{user_id}/
    my_skill/
      SKILL.md          # 技能描述（Markdown）
      scripts/
        tool_name.py    # 工具脚本（完整 Python 源码，必须含 execute 函数）

工具 schema 由 AST 从 execute() 的签名与 docstring 自动解析生成（与系统技能
同构），**不要**手写 JSON Schema —— 2026-10-27 事故：create_user_skill 的参数
说明曾让模型传「工具定义 JSON」，落盘成 .json 后没有任何可执行代码，
运行时直接 NameError: name 'execute' is not defined。

用户 Skill 与系统 Skill 同名时会覆盖系统 Skill。
"""

import ast
import json
import os
import re
import shutil
import time

from agent.logger import get_logger
logger = get_logger("skill_editor")

# 技能根目录
_SKILLS_ROOT = os.path.join(os.path.dirname(__file__), "skills")
_USER_SKILLS_DIR = os.path.join(_SKILLS_ROOT, "user")


def _get_user_dir(user_id: int) -> str:
    """获取用户 Skill 目录"""
    return os.path.join(_USER_SKILLS_DIR, str(user_id))


def _ensure_user_dir(user_id: int) -> str:
    """确保用户 Skill 目录存在，返回路径"""
    d = _get_user_dir(user_id)
    os.makedirs(d, exist_ok=True)
    return d


def _sanitize_name(name: str) -> str:
    """清理 Skill 名称，只保留字母数字和下划线"""
    name = name.strip().lower()
    name = re.sub(r'[^a-z0-9_]', '_', name)
    name = re.sub(r'_+', '_', name)
    return name.strip('_')


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def validate_skill_md(content: str):
    """校验 SKILL.md 格式是否合法、字段是否完整（保存前调用）。

    Returns:
        (ok: bool, message: str)
    """
    if not isinstance(content, str) or not content.strip():
        return False, "SKILL.md 内容为空"

    m = _FRONTMATTER_RE.match(content)
    if not m:
        return False, "缺少 YAML frontmatter（应以 --- 开头并以 --- 结束）"

    fm_text, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()

    if "name" not in fm or not fm["name"]:
        return False, "frontmatter 缺少必填字段: name"
    if "description" not in fm or not fm["description"]:
        return False, "frontmatter 缺少必填字段: description"

    name = fm["name"]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]*$", name):
        return False, f"name 必须是合法标识（字母开头，仅含字母数字下划线），当前为: {name}"

    if not body.strip():
        return False, "SKILL.md 正文（指令部分）不能为空"

    return True, "SKILL.md 格式合法"


# ==================== 工具执行函数 ====================

def _validate_script_code(t_name: str, code) -> tuple:
    """校验用户技能脚本：必须是含 execute() 的完整 Python 源码。

    2026-10-27 事故的守门员：曾经 tools 参数说明让模型传「工具定义 JSON」，
    落盘后没有任何可执行代码，运行时报 NameError: name 'execute' is not defined。
    校验必须发生在**写盘之前**，且任何一个脚本不合法就整体拒绝 ——
    半成品技能（SKILL.md 在、脚本缺失）比创建失败更难排查。
    """
    if isinstance(code, dict):
        return False, (
            f"脚本 {t_name} 传的是工具定义 JSON（只有 schema，没有代码）。"
            f"请把**完整的 Python 源码**（含 def execute(...) 函数）放在 code 字段；"
            f"工具的参数 schema 不需要手写，系统会从 execute 的签名自动解析")
    if not isinstance(code, str) or not code.strip():
        return False, (
            f"脚本 {t_name} 缺少代码。code 必须是完整的 Python 源码文本"
            f"（含 def execute(...) 函数，返回字符串结果）")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"脚本 {t_name} 语法错误（第 {e.lineno} 行）: {e.msg}"
    has_exec = any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute"
        for n in ast.walk(tree)
    )
    if not has_exec:
        return False, (
            f"脚本 {t_name} 缺少 execute 函数：工具入口必须是 "
            f"def execute(...)，函数参数就是工具参数，返回字符串结果")
    return True, ""


def _prepare_scripts(tools: list) -> tuple:
    """把 tools 列表整体校验并规整为 [(name, code), ...]。

    全部合法才返回；任何一个不合法立即返回 (None, 错误消息)。
    """
    prepared = []
    for t in tools or []:
        if not isinstance(t, dict):
            return None, "工具脚本列表的每一项都必须是对象（含 name 与 code 字段）"
        t_name = _sanitize_name(t.get("name", ""))
        if not t_name:
            continue
        code = t.get("code")
        if code is None and isinstance(t.get("content"), str):
            code = t["content"]   # 兼容：content 直接给了源码字符串
        ok, msg = _validate_script_code(t_name, code)
        if not ok:
            return None, msg
        prepared.append((t_name, code))
    return prepared, ""


def create_user_skill(user_id: int, name: str, skill_md: str, tools: list = None) -> dict:
    """创建用户 Skill

    Args:
        user_id: 用户 ID
        name: Skill 名称（英文标识）
        skill_md: SKILL.md 内容（Markdown 格式的技能描述）
        tools: 工具脚本列表 [{"name": "tool_name", "code": "<完整 Python 源码>"}]。
               code 必须是含 def execute(...) 的**完整 Python 源码**；
               工具参数 schema 由系统从 execute 签名自动解析，不要手写 JSON Schema。

    Returns:
        {"success": True/False, "message": "...", "path": "..."}
    """
    safe_name = _sanitize_name(name)
    if not safe_name:
        return {"success": False, "message": "Skill 名称无效，请使用英文标识"}

    # 保存前校验 SKILL.md 格式（不合法则不落盘）
    valid, vmsg = validate_skill_md(skill_md)
    if not valid:
        logger.warning(f"创建 Skill 校验未通过 (user={user_id}, name={safe_name}): {vmsg}")
        return {"success": False, "message": f"SKILL.md 校验未通过: {vmsg}"}

    # 脚本先整体校验，全部合法才动磁盘（避免半成品技能）
    prepared, err = _prepare_scripts(tools)
    if err:
        logger.warning(f"创建 Skill 脚本校验未通过 (user={user_id}, name={safe_name}): {err}")
        return {"success": False, "message": err}

    user_dir = _ensure_user_dir(user_id)
    skill_dir = os.path.join(user_dir, safe_name)

    if os.path.exists(skill_dir):
        return {"success": False, "message": f"Skill '{safe_name}' 已存在，请使用 update_user_skill 修改"}

    os.makedirs(skill_dir, exist_ok=True)
    scripts_dir = os.path.join(skill_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)

    # 写入 SKILL.md
    with open(os.path.join(skill_dir, "SKILL.md"), 'w', encoding='utf-8') as f:
        f.write(skill_md)

    # 写入工具脚本（.py 源码；工具 schema 由 AST 自动解析生成）
    for t_name, code in prepared:
        with open(os.path.join(scripts_dir, f"{t_name}.py"), 'w', encoding='utf-8') as f:
            f.write(code)

    return {
        "success": True,
        "message": f"Skill '{safe_name}' 创建成功，包含 {len(prepared)} 个工具",
        "path": skill_dir,
        # 表同步结果（磁盘是真相源，表是索引；同步失败不影响创建本身）
        "db_sync": _sync_to_db(user_id, safe_name),
    }


def _sync_to_db(user_id: int, safe_name: str) -> dict:
    """把刚写盘的技能同步进 user_skills 表（索引）。

    同步失败不能影响主流程 —— 磁盘才是真相源，表丢了还能对账补回。
    """
    try:
        from agent.skill_sync import sync_user_skill
        return sync_user_skill(user_id, safe_name)
    except Exception as e:
        logger.warning(f"技能表同步失败 (user={user_id}, name={safe_name}): {e}")
        return {"ok": False, "reason": str(e)}


def update_user_skill(user_id: int, name: str, skill_md: str = None, tools: list = None) -> dict:
    """更新用户 Skill

    Args:
        user_id: 用户 ID
        name: Skill 名称
        skill_md: 新的 SKILL.md 内容（不传则不更新）
        tools: 新的工具列表（不传则不更新），传入空列表则清空所有工具

    Returns:
        {"success": True/False, "message": "..."}
    """
    safe_name = _sanitize_name(name)
    if not safe_name:
        return {"success": False, "message": "Skill 名称无效"}

    skill_dir = os.path.join(_get_user_dir(user_id), safe_name)
    if not os.path.isdir(skill_dir):
        return {"success": False, "message": f"Skill '{safe_name}' 不存在，请使用 create_user_skill 创建"}

    # 若更新 SKILL.md，先校验格式
    if skill_md is not None:
        valid, vmsg = validate_skill_md(skill_md)
        if not valid:
            logger.warning(f"更新 Skill 校验未通过 (user={user_id}, name={safe_name}): {vmsg}")
            return {"success": False, "message": f"SKILL.md 校验未通过: {vmsg}"}

    updated = []

    if skill_md is not None:
        with open(os.path.join(skill_dir, "SKILL.md"), 'w', encoding='utf-8') as f:
            f.write(skill_md)
        updated.append("SKILL.md")

    if tools is not None:
        # 先整体校验再清目录：写盘失败留下「脚本被清空」的半成品比更新失败更难查
        # （2026-10-23 删除事故的同款教训：先破坏、后失败 = 数据丢失）
        prepared, err = _prepare_scripts(tools)
        if err:
            logger.warning(f"更新 Skill 脚本校验未通过 (user={user_id}, name={safe_name}): {err}")
            return {"success": False, "message": err}
        scripts_dir = os.path.join(skill_dir, "scripts")
        if os.path.isdir(scripts_dir):
            shutil.rmtree(scripts_dir)
        os.makedirs(scripts_dir, exist_ok=True)

        for t_name, code in prepared:
            with open(os.path.join(scripts_dir, f"{t_name}.py"), 'w', encoding='utf-8') as f:
                f.write(code)
        updated.append(f"{len(prepared)} 个工具脚本")

    if not updated:
        return {"success": False, "message": "未提供任何更新内容"}

    return {"success": True, "message": f"Skill '{safe_name}' 已更新：{', '.join(updated)}",
            "db_sync": _sync_to_db(user_id, safe_name)}


def delete_user_skill(user_id: int, name: str) -> dict:
    """删除用户 Skill（软删除：先备份，可恢复）。

    ⚠️ 2026-10-23 事故：Agent 改技能时走了「先 delete 再 create」的组合，
    delete 把目录真删了，create 因内容格式不合法在写盘前就 return，
    结果**技能永久消失且无法恢复**（磁盘、DB、git 三处都没有）。
    现在改为：删除时把整个技能目录移到 .trash 备份区（带时间戳），
    新建失败时可以从备份恢复；备份保留 7 天后由清理逻辑处理。

    Args:
        user_id: 用户 ID
        name: Skill 名称

    Returns:
        {"success": True/False, "message": "...", "backup": "备份路径(可恢复时)"}
    """
    safe_name = _sanitize_name(name)
    if not safe_name:
        return {"success": False, "message": "Skill 名称无效"}

    skill_dir = os.path.join(_get_user_dir(user_id), safe_name)
    if not os.path.isdir(skill_dir):
        return {"success": False, "message": f"Skill '{safe_name}' 不存在"}

    # 备份到 .trash 目录（同级隐藏目录），不直接 rmtree
    trash_dir = os.path.join(_get_user_dir(user_id), ".trash")
    os.makedirs(trash_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(trash_dir, f"{safe_name}.{stamp}")

    try:
        shutil.move(skill_dir, backup_dir)
    except Exception as e:
        logger.error(f"删除 Skill 失败 (user={user_id}, name={safe_name}): {e}")
        return {"success": False, "message": f"删除失败: {e}"}

    logger.info(f"Skill 已删除并备份 (user={user_id}, name={safe_name}, backup={backup_dir})")
    return {
        "success": True,
        "message": f"Skill '{safe_name}' 已删除（备份保留 30 天，可恢复）",
        "backup": os.path.relpath(backup_dir, os.path.dirname(_SKILLS_ROOT)),
        # 同步移除表里的索引记录（保留禁用标记）
        "db_sync": _remove_from_db(user_id, safe_name),
    }


def _remove_from_db(user_id: int, safe_name: str) -> dict:
    """从 user_skills 表移除该技能的索引记录（保留禁用标记）。"""
    try:
        from agent.skill_sync import remove_user_skill_row
        return remove_user_skill_row(user_id, safe_name)
    except Exception as e:
        logger.warning(f"技能表移除同步失败 (user={user_id}, name={safe_name}): {e}")
        return {"ok": False, "reason": str(e)}


def restore_user_skill(user_id: int, name: str) -> dict:
    """从备份区恢复被删除的 Skill（取该名称最近一份备份）。

    用于「删了但没建回来」的场景兜底。
    """
    safe_name = _sanitize_name(name)
    if not safe_name:
        return {"success": False, "message": "Skill 名称无效"}

    user_dir = _get_user_dir(user_id)
    trash_dir = os.path.join(user_dir, ".trash")
    if not os.path.isdir(trash_dir):
        return {"success": False, "message": "没有可恢复的备份"}

    cands = sorted(
        (d for d in os.listdir(trash_dir) if d.startswith(safe_name + ".")),
        reverse=True,
    )
    if not cands:
        return {"success": False, "message": f"没有找到 '{safe_name}' 的备份"}

    target = os.path.join(user_dir, safe_name)
    if os.path.isdir(target):
        return {"success": False, "message": f"Skill '{safe_name}' 已存在，无需恢复"}

    src = os.path.join(trash_dir, cands[0])
    try:
        shutil.move(src, target)
    except Exception as e:
        return {"success": False, "message": f"恢复失败: {e}"}

    logger.info(f"Skill 已从备份恢复 (user={user_id}, name={safe_name}, from={cands[0]})")
    return {"success": True, "message": f"Skill '{safe_name}' 已从备份恢复",
            "db_sync": _sync_to_db(user_id, safe_name)}


def list_trashed_skills(user_id: int) -> dict:
    """列出可恢复的已删除 Skill 备份。"""
    trash_dir = os.path.join(_get_user_dir(user_id), ".trash")
    if not os.path.isdir(trash_dir):
        return {"items": [], "count": 0}
    items = []
    for d in sorted(os.listdir(trash_dir), reverse=True):
        p = os.path.join(trash_dir, d)
        if os.path.isdir(p):
            items.append({"backup": d, "skill": d.split(".")[0]})
    return {"items": items, "count": len(items)}


# 备份保留天数：超过则自动清理（30 天）
TRASH_RETENTION_DAYS = 30


def _parse_backup_stamp(backup_name: str):
    """从备份名 '{skill}.{YYYYmmdd-HHMMSS}' 里解析时间戳，解析不了返回 None。"""
    # 技能名本身可能含点，取最后一段作为时间戳
    parts = backup_name.rsplit(".", 1)
    if len(parts) != 2:
        return None
    try:
        return time.strptime(parts[1], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def cleanup_trashed_skills(user_id: int, retention_days: int = None, dry_run: bool = False) -> dict:
    """清理超过保留期的技能备份。

    Args:
        user_id: 用户 ID
        retention_days: 保留天数，默认 TRASH_RETENTION_DAYS（30 天）
        dry_run: 只统计不删除

    Returns:
        {"removed": [...], "count": N, "kept": M, "scanned": K}
    """
    days = TRASH_RETENTION_DAYS if retention_days is None else max(0, int(retention_days))
    trash_dir = os.path.join(_get_user_dir(user_id), ".trash")
    if not os.path.isdir(trash_dir):
        return {"removed": [], "count": 0, "kept": 0, "scanned": 0}

    now = time.time()
    cutoff = now - days * 86400
    removed, kept, scanned = [], 0, 0

    for d in sorted(os.listdir(trash_dir)):
        p = os.path.join(trash_dir, d)
        if not os.path.isdir(p):
            continue
        scanned += 1
        st = _parse_backup_stamp(d)
        if st is None:
            # 名字里没有可解析的时间戳 → 保守保留（不误删）
            kept += 1
            continue
        if time.mktime(st) >= cutoff:
            kept += 1
            continue
        if dry_run:
            removed.append({"backup": d, "age_days": int((now - time.mktime(st)) / 86400)})
            continue
        try:
            shutil.rmtree(p)
            removed.append({"backup": d, "age_days": int((now - time.mktime(st)) / 86400)})
        except Exception as e:
            logger.warning(f"清理技能备份失败 (user={user_id}, backup={d}): {e}")

    if removed and not dry_run:
        logger.info(f"已清理过期技能备份 (user={user_id}, count={len(removed)}, retention={days}天)")
    return {"removed": removed, "count": len(removed), "kept": kept, "scanned": scanned}


def cleanup_all_trashed_skills(retention_days: int = None, dry_run: bool = False) -> dict:
    """清理所有用户的过期技能备份（供后台定时任务调用）。"""
    if not os.path.isdir(_USER_SKILLS_DIR):
        return {"users": 0, "removed": 0, "detail": []}
    total_removed, detail = 0, []
    users = 0
    for uid_name in os.listdir(_USER_SKILLS_DIR):
        if not uid_name.isdigit():
            continue
        uid = int(uid_name)
        users += 1
        r = cleanup_trashed_skills(uid, retention_days=retention_days, dry_run=dry_run)
        if r["count"]:
            total_removed += r["count"]
            detail.append({"user_id": uid, "removed": r["count"]})
    return {"users": users, "removed": total_removed, "detail": detail}


def _parse_frontmatter(content: str) -> tuple:
    """从 SKILL.md 拆出 frontmatter 字典与正文。

    返回 (fm: dict, body: str)。没有 frontmatter 时 fm 为空字典、body 为原文。
    """
    m = _FRONTMATTER_RE.match(content or "")
    if not m:
        return {}, (content or "").strip()
    fm = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, m.group(2).strip()


def list_user_skills(user_id: int) -> dict:
    """列出用户的所有 Skill

    Args:
        user_id: 用户 ID

    Returns:
        {"skills": [...], "count": N}
    """
    user_dir = _get_user_dir(user_id)
    if not os.path.isdir(user_dir):
        return {"skills": [], "count": 0}

    skills = []
    for name in sorted(os.listdir(user_dir)):
        # 跳过隐藏目录（如 .trash 备份区）
        if name.startswith("."):
            continue
        skill_dir = os.path.join(user_dir, name)
        if not os.path.isdir(skill_dir):
            continue

        md_path = os.path.join(skill_dir, "SKILL.md")
        md_content = ""
        if os.path.isfile(md_path):
            with open(md_path, 'r', encoding='utf-8') as f:
                md_content = f.read()

        # ⚠️ 2026-10-27 事故：这里曾数 `scripts/*.json`，而落盘的是 `.py`，
        # 于是 tool_count 永远是 0 —— 用户看到「已更新 2 个工具脚本」
        # 但列表里显示 0 个，误以为注册链路坏了。二者必须同一判据：
        # 只统计真能被 ScriptDef 解析成工具的文件。
        scripts_dir = os.path.join(skill_dir, "scripts")
        tool_names = []
        if os.path.isdir(scripts_dir):
            for fn in sorted(os.listdir(scripts_dir)):
                if not fn.endswith(".py") or fn.startswith(("_", ".")):
                    continue
                try:
                    from agent.skill_registry import ScriptDef
                    ScriptDef.from_file(os.path.join(scripts_dir, fn), is_system=False)
                    tool_names.append(fn[:-3])
                except Exception as e:
                    # 解析不出来的不算工具（它运行时也确实调不到），但要留痕
                    logger.warning(f"技能 {name} 的脚本 {fn} 无法解析为工具: {e}")

        # 描述只取 frontmatter 里 description 那一行。
        # 原来截 md_content[:200] 会把 `---\nname: ...\ndescription:` 整段吞进去。
        fm, _body = _parse_frontmatter(md_content)
        description = fm.get("description") or "(无描述)"

        skills.append({
            "name": name,
            "description": description,
            "tool_count": len(tool_names),
            "tools": tool_names,
        })

    return {"skills": skills, "count": len(skills)}


def get_user_skill(user_id: int, name: str) -> dict:
    """获取用户 Skill 的完整内容

    Args:
        user_id: 用户 ID
        name: Skill 名称

    Returns:
        {"name": "...", "skill_md": "...", "tools": [...]}
    """
    safe_name = _sanitize_name(name)
    skill_dir = os.path.join(_get_user_dir(user_id), safe_name)
    if not os.path.isdir(skill_dir):
        return {"error": f"Skill '{safe_name}' 不存在"}

    md_path = os.path.join(skill_dir, "SKILL.md")
    skill_md = ""
    if os.path.isfile(md_path):
        with open(md_path, 'r', encoding='utf-8') as f:
            skill_md = f.read()

    tools = []
    scripts_dir = os.path.join(skill_dir, "scripts")
    if os.path.isdir(scripts_dir):
        # 与 create/update 的落盘格式一致：.py 源码（schema 由 AST 解析）。
        # 原来这里是读 .json，导致 get_user_skill 对任何真实技能都回 tools=[]，
        # 模型据此以为"技能里没有工具"，进而重复创建。
        for fname in sorted(os.listdir(scripts_dir)):
            if not fname.endswith('.py') or fname.startswith(('_', '.')):
                continue
            try:
                with open(os.path.join(scripts_dir, fname), 'r', encoding='utf-8') as f:
                    code = f.read()
            except Exception as e:
                logger.warning(f"读取技能脚本失败 {fname}: {e}")
                continue
            tools.append({
                "name": fname[:-3],
                "code": code,   # 字段名与 create/update 的入参对齐，便于直接回写
            })

    return {"name": safe_name, "skill_md": skill_md, "tools": tools}