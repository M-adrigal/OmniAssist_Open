"""
技能管理 API 路由

提供用户技能（自建技能）的 CRUD 接口，以及系统技能启用/禁用管理。
"""

import json
from fastapi import APIRouter, HTTPException, Request
from server.models import SkillCreate, SkillUpdate, SkillToggle
from server.routes.auth import get_current_user, require_permission
from server.database import (
    get_user_skills,
    save_user_skill,
    delete_user_skill,
    toggle_user_skill,
    update_user_skill,
    get_enabled_system_skills,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


def get_skill_registry():
    try:
        from __main__ import get_skill_registry as gsr
    except ImportError:
        from server.main import get_skill_registry as gsr
    return gsr()


@router.get("")
def list_skills(request: Request, user_only: bool = False):
    """获取技能列表

    Args:
        user_only: 是否仅返回用户技能（默认 False，返回全部）
    """
    user = get_current_user(request)
    user_id = user["id"]

    registry = get_skill_registry()

    result = {
        "system_skills": [],
        "user_skills": [],
    }

    if not user_only:
        # 系统技能
        disabled = get_enabled_system_skills()
        for name in sorted(registry.get_system_skill_names()):
            skill = registry.get_system_skill(name)
            if skill:
                result["system_skills"].append({
                    "name": skill.name,
                    "description": skill.description,
                    "is_system": True,
                    "scripts_count": len(skill.scripts),
                    "enabled": name not in disabled,
                })

    # 用户技能
    user_skills = get_user_skills(user_id)
    for s in user_skills:
        try:
            scripts = json.loads(s.get("skill_scripts", "[]"))
        except (json.JSONDecodeError, TypeError):
            scripts = []
        result["user_skills"].append({
            "id": s["id"],
            "name": s["skill_name"],
            "description": _extract_description(s.get("skill_content", "")),
            "is_system": False,
            "scripts_count": len(scripts),
            "enabled": bool(s.get("enabled", True)),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
        })

    return result


@router.get("/system/{name}")
def get_system_skill(name: str, request: Request):
    """获取系统技能详情（内容预览）"""
    user = get_current_user(request)
    registry = get_skill_registry()
    skill = registry.get_system_skill(name) if registry else None
    if not skill:
        raise HTTPException(status_code=404, detail="系统技能不存在")

    scripts = []
    for s in skill.scripts:
        scripts.append({
            "name": s.name,
            "description": s.description,
            "parameters": s.parameters if isinstance(s.parameters, dict) else {},
            "execution_code": s.source if hasattr(s, 'source') else "",
        })

    return {
        "name": skill.name,
        "description": skill.description,
        "scripts": scripts,
        "scripts_count": len(scripts),
    }


@router.get("/{skill_id}")
def get_skill(skill_id: int, request: Request):
    """获取单个技能详情"""
    user = get_current_user(request)
    user_id = user["id"]

    skills = get_user_skills(user_id)
    skill = None
    for s in skills:
        if s["id"] == skill_id:
            skill = s
            break

    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在")

    try:
        scripts = json.loads(skill.get("skill_scripts", "[]"))
    except (json.JSONDecodeError, TypeError):
        scripts = []

    return {
        "id": skill["id"],
        "name": skill["skill_name"],
        "content": skill["skill_content"],
        "scripts": scripts,
        "enabled": bool(skill.get("enabled", True)),
        "created_at": skill.get("created_at", ""),
        "updated_at": skill.get("updated_at", ""),
    }


@router.post("")
def create_skill(body: SkillCreate, request: Request):
    """创建用户技能

    用户需要提供 SKILL.md 格式的内容（含 YAML frontmatter）。
    """
    user = get_current_user(request)
    user_id = user["id"]

    name = body.name
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="技能名称不能为空")

    # 检查是否与系统技能同名
    registry = get_skill_registry()
    if registry and registry.get_system_skill(name):
        raise HTTPException(status_code=400, detail=f"技能名称 '{name}' 与系统技能冲突")

    content = body.content or ""
    scripts = body.scripts or "[]"

    try:
        saved = save_user_skill(user_id, name, content, scripts, enabled=1)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")

    # 清除用户技能缓存，下次对话时重新加载
    if registry:
        registry.clear_user_skills(user_id)

    return {
        "id": saved["id"],
        "name": saved["skill_name"],
        "message": "技能创建成功",
    }


@router.put("/{skill_id}")
def update_skill(skill_id: int, body: SkillUpdate, request: Request):
    """更新用户技能"""
    user = get_current_user(request)
    user_id = user["id"]

    # 验证技能属于当前用户
    skills = get_user_skills(user_id)
    owned = any(s["id"] == skill_id for s in skills)
    if not owned:
        raise HTTPException(status_code=404, detail="技能不存在")

    updates = {}
    if body.content is not None:
        updates["skill_content"] = body.content
    if body.scripts is not None:
        updates["skill_scripts"] = body.scripts
    if body.enabled is not None:
        updates["enabled"] = body.enabled

    if not updates:
        return {"message": "无变更"}

    success = update_user_skill(skill_id, **updates)
    if not success:
        raise HTTPException(status_code=500, detail="更新失败")

    # 清除缓存
    registry = get_skill_registry()
    if registry:
        registry.clear_user_skills(user_id)

    return {"message": "技能更新成功"}


@router.delete("/{skill_id}")
def delete_skill(skill_id: int, request: Request):
    """删除用户技能"""
    user = get_current_user(request)
    user_id = user["id"]

    skills = get_user_skills(user_id)
    owned = any(s["id"] == skill_id for s in skills)
    if not owned:
        raise HTTPException(status_code=404, detail="技能不存在")

    success = delete_user_skill(skill_id)
    if not success:
        raise HTTPException(status_code=500, detail="删除失败")

    registry = get_skill_registry()
    if registry:
        registry.clear_user_skills(user_id)

    return {"message": "技能已删除"}


@router.put("/{skill_id}/toggle")
def toggle_skill(skill_id: int, body: SkillToggle, request: Request):
    """启用/禁用技能

    对于系统技能：通过 user_skills 表记录禁用状态（enabled=0 表示禁用）
    对于用户技能：直接修改 enabled 字段
    """
    user = get_current_user(request)
    user_id = user["id"]

    registry = get_skill_registry()
    name = body.name

    if not name:
        raise HTTPException(status_code=400, detail="技能名称不能为空")

    # 检查是否是系统技能
    if registry and registry.get_system_skill(name):
        if body.enabled:
            # 启用：删除禁用记录
            conn = __import__("server.database", fromlist=["_get_connection"])._get_connection()
            conn.execute(
                "DELETE FROM user_skills WHERE user_id = ? AND skill_name = ?",
                (user_id, name)
            )
            conn.commit()
        else:
            # 禁用：插入禁用记录
            save_user_skill(user_id, name, "", "[]", enabled=0)
        return {"message": f"系统技能 '{name}' 已{'启用' if body.enabled else '禁用'}"}

    # 用户技能
    skills = get_user_skills(user_id, skill_name=name)
    if not skills:
        raise HTTPException(status_code=404, detail="技能不存在")

    success = toggle_user_skill(skills[0]["id"], 1 if body.enabled else 0)
    if not success:
        raise HTTPException(status_code=500, detail="操作失败")

    if registry:
        registry.clear_user_skills(user_id)

    return {"message": f"技能 '{name}' 已{'启用' if body.enabled else '禁用'}"}


def _extract_description(content: str) -> str:
    """从 SKILL.md 内容中提取 description"""
    import re
    if not content:
        return ""
    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        for line in fm.split('\n'):
            line = line.strip()
            if line.startswith('description:'):
                return line.split(':', 1)[1].strip()
    return ""