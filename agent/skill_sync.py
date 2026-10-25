"""用户技能「磁盘 ↔ 数据库」同步。

背景（2026-10-23 事故）：
`agent/skill_editor.py` 的 create/update/delete **只写磁盘**，从不写 `user_skills` 表；
而 Agent 排查问题时通过 `diag_db_query` 查这张表 → 得到 0 行 → 误判「技能被删除了」
→ 进而反推出「当前登录的不是管理员」这种荒谬结论。**问题不在模型，在于它查的数据源是空的。**

`user_skills` 表的两个真实用途：
  1. **禁用标记**：enabled=0 且 skill_content 为空的占位记录，表示「该用户关闭了某个技能」
     （系统技能与文件系统技能共用这套机制，见 server/routes/skills.py 的 _is_disable_marker）。
  2. **技能内容索引**：由 skills.py 的 Web 管理接口写入的真实技能记录。

设计原则：**磁盘是真相源，表是索引**。
  · 磁盘上有的技能，表里必须有对应记录（否则查表的调用方会看到空）；
  · 表里磁盘已不存在的「真实技能记录」应当清除；
  · **禁用标记一律保留**（它不是技能内容，是开关状态，清掉会让用户的禁用设置丢失）；
  · **用户上传的技能（source='uploaded'）一律保留** —— 它按设计只存表、不落磁盘，
    不能拿「磁盘没有」当幽灵判据，否则每次服务启动都会删掉用户上传的技能。

对外提供：
  · sync_user_skill(user_id, name)        —— 单个技能写入/更新到表
  · remove_user_skill_row(user_id, name)  —— 从表移除单个真实技能记录
  · sync_user_from_fs(user_id)            —— 全量对账（扫磁盘 → 补缺 → 清幽灵）
  · reconcile_all_users()                 —— 所有用户对账（启动时/CLI 用）
"""
import json
import os

from agent.logger import get_logger

logger = get_logger("skill_sync")

# 用户技能根目录（与 skill_editor 保持一致）
_SKILLS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "agent", "skills")
_USER_SKILLS_DIR = os.path.join(_SKILLS_ROOT, "user")


def _user_dir(user_id) -> str:
    # 动态从 skill_editor 取，保证测试里改了 se._USER_SKILLS_DIR 也生效
    try:
        from agent.skill_editor import _USER_SKILLS_DIR as _live_dir
        return os.path.join(_live_dir, str(user_id))
    except Exception:
        return os.path.join(_USER_SKILLS_DIR, str(user_id))


def _sanitize_name(name: str) -> str:
    import re
    name = (name or "").strip().lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


def _read_skill_md(skill_dir: str) -> str:
    p = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _scripts_json(skill_dir: str) -> str:
    """把 scripts/ 下的工具定义打包成表里的 JSON 字符串。"""
    scripts_dir = os.path.join(skill_dir, "scripts")
    out = []
    if os.path.isdir(scripts_dir):
        for fn in sorted(os.listdir(scripts_dir)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            try:
                with open(os.path.join(scripts_dir, fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            out.append({"name": fn[:-5], "schema": data})
    return json.dumps(out, ensure_ascii=False)


def _is_disable_marker(row: dict) -> bool:
    """与 server/routes/skills.py 的口径一致：空内容 + enabled=0 即禁用标记。"""
    if row.get("enabled", 1):
        return False
    content = (row.get("skill_content") or "").strip()
    scripts = (row.get("skill_scripts") or "[]").strip()
    return not content and scripts in ("", "[]")


def _is_db_only_skill(row: dict) -> bool:
    """用户上传的技能：只存表、不落磁盘（`source='uploaded'`）。

    对账时**必须跳过**这类记录 —— 「磁盘是真相源」这条原则只适用于磁盘托管的
    技能（editor 手工创建 / 历史遗留 filesystem）。上传技能压根没有磁盘副本，
    按「磁盘没有就是幽灵」处理等于每次重启删除用户数据。
    """
    return (row.get("source") or "").strip().lower() == "uploaded"


def sync_user_skill(user_id: int, name: str) -> dict:
    """把磁盘上的一个技能写进表（upsert）。

    写表失败不影响磁盘（磁盘才是真相源），只记日志。
    """
    safe = _sanitize_name(name)
    if not safe:
        return {"ok": False, "reason": "invalid_name"}
    skill_dir = os.path.join(_user_dir(user_id), safe)
    if not os.path.isdir(skill_dir):
        return {"ok": False, "reason": "not_on_disk"}

    content = _read_skill_md(skill_dir)
    scripts = _scripts_json(skill_dir)

    try:
        from server.database import save_user_skill, get_user_skills
        # 保留用户已有的禁用设置：若表里已有该技能的记录且 enabled=0，同步时沿用 0
        enabled = 1
        for row in get_user_skills(user_id, skill_name=safe):
            if row.get("enabled") == 0:
                enabled = 0
        save_user_skill(user_id, safe, content, scripts, enabled)
        return {"ok": True, "skill": safe, "enabled": enabled}
    except Exception as e:
        logger.error(f"同步技能到表失败 (user={user_id}, name={safe}): {e}")
        return {"ok": False, "reason": f"db_failed: {e}"}


def remove_user_skill_row(user_id: int, name: str) -> dict:
    """从表里移除一个真实技能记录（保留禁用标记）。"""
    safe = _sanitize_name(name)
    if not safe:
        return {"ok": False, "reason": "invalid_name"}
    try:
        from server.database import get_user_skills, delete_user_skill
        removed = 0
        for row in get_user_skills(user_id, skill_name=safe):
            if _is_disable_marker(row):
                continue  # 禁用标记不删
            delete_user_skill(row["id"])
            removed += 1
        return {"ok": True, "skill": safe, "removed": removed}
    except Exception as e:
        logger.error(f"从表移除技能失败 (user={user_id}, name={safe}): {e}")
        return {"ok": False, "reason": f"db_failed: {e}"}


def sync_user_from_fs(user_id: int) -> dict:
    """全量对账：扫磁盘，补缺失记录，清掉表里磁盘已没有的幽灵记录。

    Returns:
        {"user_id": N, "on_disk": [...], "added": [...], "removed": [...],
         "kept_markers": N, "unchanged": N}
    """
    result = {"user_id": user_id, "on_disk": [], "added": [],
              "removed": [], "kept_markers": 0, "unchanged": 0}

    user_dir = _user_dir(user_id)
    if not os.path.isdir(user_dir):
        # 磁盘上什么都没有 → 只要清掉表里的幽灵记录即可
        on_disk = []
    else:
        on_disk = sorted(
            d for d in os.listdir(user_dir)
            if not d.startswith(".") and os.path.isdir(os.path.join(user_dir, d))
        )
    result["on_disk"] = on_disk

    try:
        from server.database import get_user_skills
        rows = get_user_skills(user_id)
    except Exception as e:
        # 读表失败必须显式暴露：静默返回会让调用方误以为"磁盘与表一致"，
        # 而那正是 2026-10-23 事故的根因（Agent 查表得空 → 误判技能被删）。
        logger.error(f"读取 user_skills 失败 (user={user_id}): {e}")
        result["error"] = f"read_db_failed: {e}"
        return result

    db_real = {}      # 磁盘托管下的真实技能记录（有内容）
    db_uploaded = {}  # 用户上传的技能：**只存表、不落磁盘**（见 5.21）
    markers = 0
    for r in rows:
        if _is_disable_marker(r):
            markers += 1
        elif _is_db_only_skill(r):
            db_uploaded[r["skill_name"]] = r
        else:
            db_real[r["skill_name"]] = r
    result["kept_markers"] = markers
    result["kept_uploaded"] = sorted(db_uploaded.keys())

    disk_set = set(on_disk)
    db_set = set(db_real.keys())

    # 1) 磁盘有、表里没有 → 补
    for name in sorted(disk_set - db_set):
        r = sync_user_skill(user_id, name)
        if r.get("ok"):
            result["added"].append(name)
        else:
            logger.warning(f"补充技能记录失败 (user={user_id}, name={name}): {r}")

    # 2) 表里有、磁盘没有 → 清幽灵（禁用标记与上传技能已在上面排除）
    #    ⚠️ 上传技能（source='uploaded'）**故意不落磁盘**（见 5.21 的存储决策），
    #    它本来就不在磁盘上。若把「磁盘没有」一律当幽灵，服务每次启动
    #    （lifespan 里会跑 reconcile_all_users）都会把用户上传的技能删光。
    for name in sorted(db_set - disk_set):
        r = remove_user_skill_row(user_id, name)
        if r.get("ok") and r.get("removed"):
            result["removed"].append(name)

    # 3) 两边都有 → 内容以磁盘为准（若 SKILL.md 变了，刷新表里的副本）
    for name in sorted(disk_set & db_set):
        row = db_real[name]
        content = _read_skill_md(os.path.join(user_dir, name))
        if (row.get("skill_content") or "") != content:
            try:
                from server.database import save_user_skill
                save_user_skill(user_id, name, content,
                                _scripts_json(os.path.join(user_dir, name)),
                                1 if row.get("enabled", 1) else 0)
                result["added"].append(f"{name} (内容已刷新)")
            except Exception as e:
                logger.warning(f"刷新技能内容失败 (user={user_id}, name={name}): {e}")
        else:
            result["unchanged"] += 1

    if result["added"] or result["removed"]:
        logger.info(
            f"用户技能对账完成 (user={user_id}): "
            f"磁盘 {len(on_disk)} 个，新增 {len(result['added'])}，清除幽灵 {len(result['removed'])}，"
            f"保留禁用标记 {markers} 个"
        )
    return result


def reconcile_all_users() -> dict:
    """对所有有技能目录的用户做对账（服务启动 / CLI 用）。"""
    from agent.skill_editor import _USER_SKILLS_DIR as _live_dir
    base = _live_dir
    if not os.path.isdir(base):
        return {"users": 0, "detail": []}
    detail = []
    for name in sorted(os.listdir(base)):
        if not name.isdigit():
            continue
        uid = int(name)
        r = sync_user_from_fs(uid)
        if r.get("added") or r.get("removed"):
            detail.append({
                "user_id": uid,
                "added": r.get("added", []),
                "removed": r.get("removed", []),
            })
    return {"users": len(detail) or _count_user_dirs(), "detail": detail}


def _count_user_dirs() -> int:
    try:
        from agent.skill_editor import _USER_SKILLS_DIR as _live_dir
        base = _live_dir
    except Exception:
        base = _USER_SKILLS_DIR
    if not os.path.isdir(base):
        return 0
    return sum(1 for d in os.listdir(base) if d.isdigit())
