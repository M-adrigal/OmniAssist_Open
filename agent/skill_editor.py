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
    }


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

    return {"success": True, "message": f"Skill '{safe_name}' 已更新：{', '.join(updated)}"}


def delete_user_skill(user_id: int, name: str) -> dict:
    """删除用户 Skill

    Args:
        user_id: 用户 ID
        name: Skill 名称

    Returns:
        {"success": True/False, "message": "..."}
    """
    safe_name = _sanitize_name(name)
    if not safe_name:
        return {"success": False, "message": "Skill 名称无效"}

    skill_dir = os.path.join(_get_user_dir(user_id), safe_name)
    if not os.path.isdir(skill_dir):
        return {"success": False, "message": f"Skill '{safe_name}' 不存在"}

    shutil.rmtree(skill_dir)
    return {"success": True, "message": f"Skill '{safe_name}' 已删除"}


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