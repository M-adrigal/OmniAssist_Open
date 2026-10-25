"""
用户 Skill 编辑器 — 提供创建/修改/删除用户级 Skill 的工具

基于 OpenClaw/Hermes 思路：每个用户有独立的 Skill 仓库，
模型可以按需复盘优化 Skill 内容，实现自我进化。

目录结构：
  agent/skills/user/{user_id}/
    my_skill/
      SKILL.md          # 技能描述（Markdown）
      scripts/
        tool_name.json  # 工具定义（JSON Schema 格式）

用户 Skill 与系统 Skill 同名时会覆盖系统 Skill。
"""

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

def create_user_skill(user_id: int, name: str, skill_md: str, tools: list = None) -> dict:
    """创建用户 Skill

    Args:
        user_id: 用户 ID
        name: Skill 名称（英文标识）
        skill_md: SKILL.md 内容（Markdown 格式的技能描述）
        tools: 工具脚本列表 [{"name": "tool_name", "content": {...JSON Schema...}}, ...]

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

    # 写入工具脚本
    tool_count = 0
    if tools:
        for t in tools:
            t_name = _sanitize_name(t.get("name", ""))
            if not t_name:
                continue
            t_content = t.get("content", {})
            with open(os.path.join(scripts_dir, f"{t_name}.json"), 'w', encoding='utf-8') as f:
                json.dump(t_content, f, ensure_ascii=False, indent=2)
            tool_count += 1

    return {
        "success": True,
        "message": f"Skill '{safe_name}' 创建成功，包含 {tool_count} 个工具",
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
        scripts_dir = os.path.join(skill_dir, "scripts")
        # 清空旧脚本
        if os.path.isdir(scripts_dir):
            shutil.rmtree(scripts_dir)
        os.makedirs(scripts_dir, exist_ok=True)

        tool_count = 0
        for t in tools:
            t_name = _sanitize_name(t.get("name", ""))
            if not t_name:
                continue
            t_content = t.get("content", {})
            with open(os.path.join(scripts_dir, f"{t_name}.json"), 'w', encoding='utf-8') as f:
                json.dump(t_content, f, ensure_ascii=False, indent=2)
            tool_count += 1
        updated.append(f"{tool_count} 个工具脚本")

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

        scripts_dir = os.path.join(skill_dir, "scripts")
        tool_count = 0
        if os.path.isdir(scripts_dir):
            tool_count = len([f for f in os.listdir(scripts_dir)
                              if f.endswith('.json') and not f.startswith('.')])

        skills.append({
            "name": name,
            "description": md_content[:200] if md_content else "(无描述)",
            "tool_count": tool_count,
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
        for fname in sorted(os.listdir(scripts_dir)):
            if not fname.endswith('.json') or fname.startswith('.'):
                continue
            with open(os.path.join(scripts_dir, fname), 'r', encoding='utf-8') as f:
                tools.append({
                    "name": fname[:-5],
                    "content": json.load(f),
                })

    return {"name": safe_name, "skill_md": skill_md, "tools": tools}