"""
技能管理 API 路由

提供用户技能（自建技能）的 CRUD 接口，以及系统技能启用/禁用管理。
"""

import json
import re
from fastapi import APIRouter, HTTPException, Request
from server.models import SkillCreate, SkillUpdate, SkillToggle
from server.routes.auth import get_current_user, require_permission
from server.database import (
    get_user_skills,
    save_user_skill,
    delete_user_skill,
    toggle_user_skill,
    update_user_skill,
    get_disabled_system_skills,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


def get_skill_registry():
    try:
        from __main__ import get_skill_registry as gsr
    except ImportError:
        from server.main import get_skill_registry as gsr
    return gsr()


def _is_disable_marker(row: dict) -> bool:
    """判断一条 user_skills 记录是否只是"禁用标记"而非真实的用户技能

    禁用系统技能 / 文件系统技能时，我们往 user_skills 插入一条
    内容为空、enabled=0 的占位记录来表示"该用户关闭了这个技能"。
    这类记录不能出现在用户技能列表里，也不能被当作技能加载。
    """
    if row.get("enabled", 1):
        return False
    content = (row.get("skill_content") or "").strip()
    scripts = (row.get("skill_scripts") or "[]").strip()
    return not content and scripts in ("", "[]")


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

    # 当前用户禁用的技能（含系统技能与文件系统技能）
    try:
        disabled = get_disabled_system_skills(user_id)
    except Exception:
        disabled = set()

    if not user_only:
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

    system_names = set(registry.get_system_skill_names()) if registry else set()

    # 用户技能（从数据库）
    db_skill_names = set()
    for s in get_user_skills(user_id):
        # 跳过纯禁用标记：它只是开关状态，不是一个真实技能
        if _is_disable_marker(s):
            continue
        # 跳过与系统技能同名的历史脏数据，避免在用户技能页出现幽灵条目
        if s["skill_name"] in system_names:
            continue
        try:
            scripts = json.loads(s.get("skill_scripts", "[]"))
        except (json.JSONDecodeError, TypeError):
            scripts = []
        db_skill_names.add(s["skill_name"])
        result["user_skills"].append({
            "id": s["id"],
            "name": s["skill_name"],
            "description": _extract_description(s.get("skill_content", "")),
            "is_system": False,
            "scripts_count": len(scripts),
            "enabled": bool(s.get("enabled", True)),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "source": "database",
        })

    # 用户技能（从文件系统，补充数据库中没有的技能）
    try:
        registry.load_user_skills_from_fs(user_id)
        for name, skill in registry.get_user_skills(user_id).items():
            if name in db_skill_names or getattr(skill, "skill_id", None) is not None:
                continue
            result["user_skills"].append({
                "id": None,
                "name": skill.name,
                "description": skill.description,
                "is_system": False,
                "scripts_count": len(skill.scripts),
                # 文件系统技能同样支持开关，状态来自禁用标记
                "enabled": name not in disabled,
                "created_at": "",
                "updated_at": "",
                "source": "filesystem",
            })
    except Exception:
        pass

    result["user_skills"].sort(key=lambda x: x["name"])
    return result


def _serialize_skill_scripts(skill) -> list:
    """把 Skill 对象的脚本序列化为预览用结构"""
    scripts = []
    for s in skill.scripts:
        scripts.append({
            "name": s.name,
            "description": s.description,
            "parameters": s.parameters if isinstance(s.parameters, dict) else {},
            "execution_code": getattr(s, "source", "") or "",
        })
    return scripts


@router.get("/system/{name}")
def get_system_skill(name: str, request: Request):
    """获取系统技能详情（内容预览）"""
    get_current_user(request)
    registry = get_skill_registry()
    skill = registry.get_system_skill(name) if registry else None
    if not skill:
        raise HTTPException(status_code=404, detail="系统技能不存在")

    scripts = _serialize_skill_scripts(skill)
    return {
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.instructions,
        "is_system": True,
        "source": "filesystem",
        "scripts": scripts,
        "scripts_count": len(scripts),
    }


@router.get("/user/{name}")
def get_user_skill_by_name(name: str, request: Request):
    """按名称获取用户技能详情

    数据库技能有自增 id，可以走 /api/skills/{skill_id}；
    但直接放在 agent/skills/user/{user_id}/ 下的文件系统技能没有 id，
    只能按名称查，否则前端预览会一直报"技能未找到"。
    """
    user = get_current_user(request)
    user_id = user["id"]

    # 优先查数据库
    rows = get_user_skills(user_id, skill_name=name)
    for row in rows:
        if _is_disable_marker(row):
            continue
        return _db_skill_detail(row)

    # 再查文件系统（registry 缓存）
    registry = get_skill_registry()
    skill = None
    if registry:
        try:
            registry.load_user_skills_from_fs(user_id)
        except Exception:
            pass
        skill = registry.get_user_skills(user_id).get(name)

    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在")

    try:
        disabled = get_disabled_system_skills(user_id)
    except Exception:
        disabled = set()

    scripts = _serialize_skill_scripts(skill)
    return {
        "id": None,
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.instructions,
        "content": skill.instructions,
        "is_system": False,
        "source": "filesystem",
        "enabled": name not in disabled,
        "scripts": scripts,
        "scripts_count": len(scripts),
    }


def _db_skill_detail(skill: dict) -> dict:
    """把数据库技能记录转成预览结构"""
    try:
        scripts = json.loads(skill.get("skill_scripts", "[]"))
    except (json.JSONDecodeError, TypeError):
        scripts = []

    content = skill.get("skill_content", "") or ""
    return {
        "id": skill["id"],
        "name": skill["skill_name"],
        "description": _extract_description(content),
        "instructions": _strip_frontmatter(content),
        "content": content,
        "is_system": False,
        "source": "database",
        "scripts": scripts,
        "scripts_count": len(scripts),
        "enabled": bool(skill.get("enabled", True)),
        "created_at": skill.get("created_at", ""),
        "updated_at": skill.get("updated_at", ""),
    }


@router.get("/{skill_id}")
def get_skill(skill_id: int, request: Request):
    """获取单个技能详情"""
    user = get_current_user(request)
    user_id = user["id"]

    for s in get_user_skills(user_id):
        if s["id"] == skill_id:
            return _db_skill_detail(s)

    raise HTTPException(status_code=404, detail="技能不存在")


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


def _clear_disable_marker(user_id: int, name: str):
    """删除某个技能的禁用标记（只删标记，绝不碰真实技能记录）"""
    from server.database import _get_connection
    conn = _get_connection()
    conn.execute(
        "DELETE FROM user_skills WHERE user_id = ? AND skill_name = ? "
        "AND enabled = 0 AND COALESCE(skill_content, '') = '' "
        "AND COALESCE(skill_scripts, '[]') IN ('', '[]')",
        (user_id, name)
    )
    conn.commit()


@router.put("/{skill_id}/toggle")
def toggle_skill(skill_id: int, body: SkillToggle, request: Request):
    """启用/禁用技能

    三种情况：
    - 系统技能：用 user_skills 里的"禁用标记"记录状态（按用户隔离）
    - 数据库用户技能：直接改 enabled 字段
    - 文件系统用户技能：没有数据库记录，同样用禁用标记
    """
    user = get_current_user(request)
    user_id = user["id"]

    registry = get_skill_registry()
    name = body.name

    if not name:
        raise HTTPException(status_code=400, detail="技能名称不能为空")

    action = "启用" if body.enabled else "禁用"

    def _finish(msg: str):
        # 状态变了就清缓存，下一轮对话重新加载技能与上下文
        if registry:
            registry.clear_user_skills(user_id)
        return {"message": msg, "name": name, "enabled": bool(body.enabled)}

    # 1) 系统技能 —— 用禁用标记
    if registry and registry.get_system_skill(name):
        if body.enabled:
            _clear_disable_marker(user_id, name)
        else:
            save_user_skill(user_id, name, "", "[]", enabled=0)
        return _finish(f"系统技能 '{name}' 已{action}")

    # 2) 数据库用户技能 —— 直接改 enabled
    rows = [r for r in get_user_skills(user_id, skill_name=name)
            if not _is_disable_marker(r)]
    if rows:
        if not toggle_user_skill(rows[0]["id"], 1 if body.enabled else 0):
            raise HTTPException(status_code=500, detail="操作失败")
        return _finish(f"技能 '{name}' 已{action}")

    # 3) 文件系统用户技能 —— 没有数据库记录，同样用禁用标记
    fs_skill = None
    if registry:
        try:
            registry.load_user_skills_from_fs(user_id)
        except Exception:
            pass
        fs_skill = registry.get_user_skills(user_id).get(name)

    if fs_skill is None:
        raise HTTPException(status_code=404, detail="技能不存在")

    if body.enabled:
        _clear_disable_marker(user_id, name)
    else:
        save_user_skill(user_id, name, "", "[]", enabled=0)
    return _finish(f"技能 '{name}' 已{action}")


_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL)


def _extract_description(content: str) -> str:
    """从 SKILL.md 内容中提取 description"""
    if not content:
        return ""
    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        for line in fm_match.group(1).split('\n'):
            line = line.strip()
            if line.startswith('description:'):
                return line.split(':', 1)[1].strip()
    return ""


def _strip_frontmatter(content: str) -> str:
    """去掉 YAML frontmatter，返回 SKILL.md 正文"""
    if not content:
        return ""
    fm_match = _FRONTMATTER_RE.match(content)
    return content[fm_match.end():].strip() if fm_match else content.strip()