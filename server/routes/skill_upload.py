"""用户自助上传 Skill 安装路由。

支持两种输入：
  1. zip 压缩包（≤10MB，解压后 ≤30MB，≤200 个文件）—— Anthropic 规范的技能文件夹打包；
  2. 单个 SKILL.md（≤1MB）—— 纯指令类技能（无脚本）。

安装链路（与前端三态一一对应）：
  上传 → 结构校验（zip 安全 / frontmatter 规范） → 风险扫描（agent/skill_risk）
   → P0 拦截 / P1 待确认（acknowledge=true 才装）/ P2 直接装 → 写入 user_skills 表。

**存储决策**：上传的技能只写 `user_skills` 表（skill_content=SKILL.md 原文，
skill_scripts=从 zip 内 .py 解析出的脚本摘要），不落 agent/skills/user/ 磁盘目录。
理由：与「设置里手工创建的技能」完全同构 —— 执行、预览、启停、上下文注入
全部复用现有链路，不存在「磁盘一份、表一份」的两处不一致。

**软删除**：卸载不删行，把整行序列化到 agent/skills/user/{user_id}/.trash/
下的 JSON 备份（保留 30 天，可恢复），与 skill_editor 的目录级软删同目录同时效。
"""

import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import zipfile

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile

from server.routes.auth import get_current_user
from agent.logger import get_logger

logger = get_logger("skill_upload")

router = APIRouter(prefix="/api/skills", tags=["skill_upload"])

# ===== 限额（用户 2026-10-24 拍板：zip ≤10MB；其余为配套默认值）=====
MAX_ZIP_SIZE = 10 * 1024 * 1024
MAX_MD_SIZE = 1 * 1024 * 1024
MAX_FILES = 200                 # zip 内文件数上限（含目录则只数文件）
MAX_UNCOMPRESSED = 30 * 1024 * 1024   # 解压后总大小上限（防压缩炸弹）
MAX_SINGLE_FILE = 5 * 1024 * 1024     # zip 内单文件上限
TRASH_RETENTION_DAYS = 30       # 卸载备份保留天数（与 skill_editor 一致）

# 放行的文件扩展名（小写）：文本/代码/文档/常见资源。
# 不在名单里的一律拒 —— 技能包不该携带陌生二进制。
ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".py", ".json", ".yaml", ".yml", ".csv", ".tsv",
    ".html", ".htm", ".css", ".js", ".ts", ".xml", ".ini", ".cfg",
    ".toml", ".sql", ".r", ".rb", ".go", ".rs", ".java", ".c", ".cpp",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv",
}

# 明确拒绝的二进制/可执行/脚本壳（宁可误伤也不放过）。
BLOCKED_EXTENSIONS = {
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".jar",
    ".sh", ".bash", ".zsh", ".command", ".app", ".msi", ".deb", ".rpm",
    ".pyc", ".pyo", ".whl",
}

# Anthropic 规范的 name：小写字母/数字/连字符，≤64 字符。
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LEN = 64
MAX_DESC_LEN = 1024

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


class UploadError(Exception):
    """上传校验失败。code 用于前端区分展示（结构错误 / 风险拦截）。"""

    def __init__(self, message: str, code: str = "invalid", findings: list = None):
        super().__init__(message)
        self.code = code
        self.findings = findings or []


# ===== 临时目录管理 =====

def _make_tmp_dir() -> str:
    return tempfile.mkdtemp(prefix="skill_upload_")


def _cleanup_tmp(path: str):
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ===== zip 安全校验与解压 =====

def _check_member_name(name: str) -> str:
    """校验 zip 条目名，返回规范化后的相对路径；不合法直接抛。"""
    # 统一分隔符后拆段，逐段拒绝 . / ..
    norm = name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise UploadError(f"压缩包含绝对路径条目: {name}", code="zip_unsafe")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UploadError(f"压缩包含路径穿越条目: {name}", code="zip_unsafe")
    return "/".join(parts)


def _safe_extract_zip(raw: bytes, dest: str) -> None:
    """把 zip 安全解压到 dest。任何可疑形态直接抛 UploadError。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise UploadError("文件不是有效的 zip 压缩包", code="bad_zip")

    with zf:
        members = zf.infolist()
        file_count = 0
        total_uncompressed = 0

        for info in members:
            rel = _check_member_name(info.filename)

            # 符号链接：external_attr 高 16 位是 Unix st_mode
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise UploadError(
                    f"压缩包含符号链接条目: {info.filename}（技能包不允许链接）",
                    code="zip_unsafe",
                )

            if info.is_dir():
                continue

            file_count += 1
            if file_count > MAX_FILES:
                raise UploadError(f"压缩包文件数超过 {MAX_FILES} 个", code="zip_too_big")

            ext = os.path.splitext(rel)[1].lower()
            if ext in BLOCKED_EXTENSIONS:
                raise UploadError(
                    f"压缩包含不允许的文件类型 {ext}: {rel}", code="zip_blocked_ext",
                )
            if ext not in ALLOWED_EXTENSIONS:
                raise UploadError(
                    f"压缩包含未知文件类型 {ext}: {rel}（技能包只接受文本/代码/文档/图片）",
                    code="zip_blocked_ext",
                )

            if info.file_size > MAX_SINGLE_FILE:
                raise UploadError(
                    f"单文件超过 {MAX_SINGLE_FILE // (1024 * 1024)}MB: {rel}",
                    code="zip_too_big",
                )
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_UNCOMPRESSED:
                raise UploadError("解压后总大小超过 30MB（疑似压缩炸弹）", code="zip_too_big")

            target = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_skill_root(extracted: str) -> str:
    """在解压目录里定位技能根（含 SKILL.md 的目录）。

    兼容两种常见打包方式：
      a) zip 根直接是技能内容（SKILL.md 在根）；
      b) 「右键压缩 skill 文件夹」得到的 zip（根下只有一个同名目录）。
    多个顶层目录都含 SKILL.md → 歧义，拒绝。
    """
    if os.path.isfile(os.path.join(extracted, "SKILL.md")):
        return extracted

    top_dirs = [
        d for d in sorted(os.listdir(extracted))
        if os.path.isdir(os.path.join(extracted, d)) and not d.startswith(".")
    ]
    with_skill = [d for d in top_dirs
                  if os.path.isfile(os.path.join(extracted, d, "SKILL.md"))]
    if len(with_skill) == 1:
        return os.path.join(extracted, with_skill[0])
    if len(with_skill) > 1:
        raise UploadError("压缩包含多个技能文件夹，请逐个上传", code="ambiguous")
    raise UploadError("压缩包内找不到 SKILL.md（技能文件夹的必需入口文件）", code="no_skill_md")


# ===== frontmatter 解析与校验 =====

def _parse_frontmatter(content: str) -> dict:
    m = _FRONTMATTER_RE.match(content or "")
    if not m:
        raise UploadError(
            "SKILL.md 缺少 YAML frontmatter（文件应以 --- 开头，name/description 后以 --- 结束）",
            code="bad_frontmatter",
        )
    fm = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"').strip("'")
        fm[k.strip()] = v
    return fm


def _validate_frontmatter(fm: dict):
    name = (fm.get("name") or "").strip()
    desc = (fm.get("description") or "").strip()
    if not name:
        raise UploadError("frontmatter 缺少必填字段 name", code="bad_frontmatter")
    if not NAME_RE.match(name):
        raise UploadError(
            f"技能名称 '{name}' 不合法：只能使用小写字母、数字与连字符（如 my-skill）",
            code="bad_name",
        )
    if len(name) > MAX_NAME_LEN:
        raise UploadError(f"技能名称超过 {MAX_NAME_LEN} 字符", code="bad_name")
    if not desc:
        raise UploadError("frontmatter 缺少必填字段 description", code="bad_frontmatter")
    if len(desc) > MAX_DESC_LEN:
        raise UploadError(f"description 超过 {MAX_DESC_LEN} 字符", code="bad_frontmatter")
    return name, desc


# ===== 脚本摘要（供 DB skill_scripts 与执行链路复用）=====

def _collect_scripts(skill_root: str) -> list:
    """解析 scripts/ 下的 .py，产出与 skills.py::_serialize_skill_scripts 同构的摘要。"""
    from agent.skill_registry import ScriptDef
    scripts = []
    scripts_dir = os.path.join(skill_root, "scripts")
    if not os.path.isdir(scripts_dir):
        return scripts
    for dirpath, _dirs, files in os.walk(scripts_dir):
        for fn in sorted(files):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            try:
                sd = ScriptDef.from_file(os.path.join(dirpath, fn), is_system=False)
            except ValueError as e:
                logger.warning(f"上传技能脚本解析失败，已跳过: {fn}: {e}")
                continue
            scripts.append({
                "name": sd.name,
                "description": sd.description,
                "parameters": sd.parameters if isinstance(sd.parameters, dict) else {},
                "execution_code": sd.source,
            })
    return scripts


# ===== 软删除（卸载/恢复/回收站）=====

def _trash_dir(user_id: int) -> str:
    from agent.skill_editor import _get_user_dir
    return os.path.join(_get_user_dir(user_id), ".trash")


def _uninstall_skill_row(user_id: int, name: str) -> dict:
    """把一条用户技能软删：备份整行到 .trash JSON 后删除。"""
    from server.database import get_user_skills, delete_user_skill

    rows = [r for r in get_user_skills(user_id, skill_name=name)
            if not ((r.get("enabled") == 0) and not (r.get("skill_content") or "").strip())]
    if not rows:
        raise HTTPException(status_code=404, detail="技能不存在")

    row = rows[0]
    trash = _trash_dir(user_id)
    os.makedirs(trash, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(trash, f"{name}.{stamp}.json")
    payload = {
        "kind": "db_skill",     # 与目录级备份区分（cleanup 两类都清）
        "skill_name": row["skill_name"],
        "skill_content": row.get("skill_content") or "",
        "skill_scripts": row.get("skill_scripts") or "[]",
        "source": row.get("source") or "editor",
        "risk_level": row.get("risk_level") or "low",
        "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    delete_user_skill(row["id"])

    _clear_skill_cache(user_id)
    return {"ok": True, "backup": os.path.basename(backup_path),
            "message": f"技能 '{name}' 已卸载（备份保留 {TRASH_RETENTION_DAYS} 天，可恢复）"}


def _restore_skill_row(user_id: int, name: str) -> dict:
    """从 .trash 里找该技能最近一份 JSON 备份，写回 user_skills 表。"""
    from server.database import save_user_skill, get_user_skills

    trash = _trash_dir(user_id)
    if not os.path.isdir(trash):
        raise HTTPException(status_code=404, detail="没有可恢复的备份")

    cands = sorted(
        (f for f in os.listdir(trash)
         if f.startswith(name + ".") and f.endswith(".json")),
        reverse=True,
    )
    if not cands:
        raise HTTPException(status_code=404, detail=f"没有找到 '{name}' 的备份")

    with open(os.path.join(trash, cands[0]), "r", encoding="utf-8") as f:
        payload = json.load(f)

    # 已存在同名技能时不覆盖
    if any(r["skill_name"] == name for r in get_user_skills(user_id)):
        raise HTTPException(status_code=409, detail=f"技能 '{name}' 已存在，请先卸载当前版本")

    save_user_skill(
        user_id, payload["skill_name"], payload.get("skill_content") or "",
        payload.get("skill_scripts") or "[]", enabled=1,
        source=payload.get("source"), risk_level=payload.get("risk_level"),
    )
    os.remove(os.path.join(trash, cands[0]))
    _clear_skill_cache(user_id)
    return {"ok": True, "message": f"技能 '{name}' 已恢复"}


def _list_trash(user_id: int) -> list:
    """回收站列表：DB 技能 JSON 备份 + 目录级软删（skill_editor 的 .trash 目录）。"""
    items = []
    trash = _trash_dir(user_id)
    if not os.path.isdir(trash):
        return items
    for entry in sorted(os.listdir(trash), reverse=True):
        path = os.path.join(trash, entry)
        if entry.endswith(".json") and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                items.append({
                    "backup": entry, "name": payload.get("skill_name", entry),
                    "kind": "db_skill", "deleted_at": payload.get("deleted_at", ""),
                    "risk_level": payload.get("risk_level", "low"),
                })
            except Exception:
                continue
        elif os.path.isdir(path):
            skill = entry.split(".")[0]
            items.append({
                "backup": entry, "name": skill, "kind": "dir_skill",
                "deleted_at": "", "risk_level": "low",
            })
    return items


def cleanup_skill_trash(user_id: int, retention_days: int = None) -> dict:
    """清理超过保留期的卸载备份（DB JSON 与目录级两类都清）。

    与 agent/skill_editor.cleanup_trashed_skills 同目录同时效；
    那一个按目录扫，这一个按 .json 扫，互不遗漏。
    """
    days = TRASH_RETENTION_DAYS if retention_days is None else max(0, int(retention_days))
    trash = _trash_dir(user_id)
    result = {"removed": [], "count": 0, "kept": 0}
    if not os.path.isdir(trash):
        return result
    cutoff = time.time() - days * 86400
    for entry in os.listdir(trash):
        path = os.path.join(trash, entry)
        # 两类备份名都带 .{YYYYmmdd-HHMMSS} 时间戳（JSON 多一个 .json 后缀）
        stem = entry[:-5] if entry.endswith(".json") else entry
        parts = stem.rsplit(".", 1)
        if len(parts) != 2:
            continue
        try:
            ts = time.mktime(time.strptime(parts[1], "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if ts >= cutoff:
            result["kept"] += 1
            continue
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            result["removed"].append(entry)
            result["count"] += 1
        except OSError:
            continue
    return result


# ===== 缓存刷新 =====

def _clear_skill_cache(user_id: int):
    try:
        from __main__ import get_skill_registry as gsr
    except ImportError:
        from server.main import get_skill_registry as gsr
    try:
        gsr().clear_user_skills(user_id)
    except Exception as e:
        logger.warning(f"清除技能缓存失败 (user={user_id}): {e}")


def _system_skill_names() -> set:
    try:
        from __main__ import get_skill_registry as gsr
    except ImportError:
        from server.main import get_skill_registry as gsr
    try:
        return set(gsr().get_system_skill_names())
    except Exception:
        return set()


# ===== 主接口 =====

@router.post("/upload")
async def upload_skill(
    request: Request,
    file: UploadFile = File(...),
    acknowledge: bool = Query(False, description="用户已确认 P1 敏感操作"),
):
    """上传并安装技能（zip 或单个 SKILL.md）。

    返回三态：
      - installed：已安装（P2 放行，或 P1 且 acknowledge=true）
      - needs_confirm：命中 P1，前端展示清单后带 acknowledge=true 重传
      - blocked（HTTP 400）：命中 P0，附 findings 说明哪条规则命中
    """
    user = get_current_user(request)
    user_id = user["db_id"]

    filename = (file.filename or "").lower()
    raw = await file.read()

    if filename.endswith(".zip"):
        if len(raw) > MAX_ZIP_SIZE:
            raise HTTPException(status_code=400, detail=f"压缩包超过 {MAX_ZIP_SIZE // (1024 * 1024)}MB 上限")
    elif filename.endswith(".md"):
        if len(raw) > MAX_MD_SIZE:
            raise HTTPException(status_code=400, detail=f"SKILL.md 超过 {MAX_MD_SIZE // (1024 * 1024)}MB 上限")
    else:
        raise HTTPException(status_code=400, detail="只支持 .zip 压缩包或 .md 技能文件")

    tmp = _make_tmp_dir()
    try:
        if filename.endswith(".zip"):
            _safe_extract_zip(raw, tmp)
            skill_root = _find_skill_root(tmp)
        else:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise UploadError("SKILL.md 不是 UTF-8 编码的文本文件", code="bad_encoding")
            os.makedirs(tmp, exist_ok=True)
            with open(os.path.join(tmp, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(content)
            skill_root = tmp

        with open(os.path.join(skill_root, "SKILL.md"), "r", encoding="utf-8", errors="replace") as f:
            skill_md = f.read()

        # ① 结构校验（对齐 Anthropic 规范）
        fm = _parse_frontmatter(skill_md)
        name, description = _validate_frontmatter(fm)

        # ② 同名检查：系统技能与本人已有技能都不可覆盖（避免静默替换）
        if name in _system_skill_names():
            raise HTTPException(status_code=409, detail=f"'{name}' 与系统技能同名，请换个名称")
        from server.database import get_user_skills
        for r in get_user_skills(user_id):
            if r["skill_name"] == name and not _is_disable_marker(r):
                raise HTTPException(
                    status_code=409,
                    detail=f"你已有名为 '{name}' 的技能，请先卸载后再上传同名技能",
                )

        # ③ 风险扫描
        from agent.skill_risk import scan_skill_dir, BLOCK, CONFIRM
        scan = scan_skill_dir(skill_root)
        result = scan.to_dict()

        if result["level"] == BLOCK:
            logger.warning(
                f"技能上传被拦截 (user={user_id}, name={name}): "
                f"{[f['rule'] for f in result['findings']]}"
            )
            raise UploadError(
                "该技能包含高风险操作，已阻止安装",
                code="blocked", findings=result["findings"],
            )

        if result["level"] == CONFIRM and not acknowledge:
            return {
                "status": "needs_confirm",
                "name": name,
                "description": description,
                "scan": result,
            }

        # ④ 写入（只写 DB，与手工创建的技能同构）
        scripts = _collect_scripts(skill_root)
        from server.database import save_user_skill
        confirmed_at = time.strftime("%Y-%m-%d %H:%M:%S") if result["level"] == CONFIRM else None
        save_user_skill(
            user_id, name, skill_md, json.dumps(scripts, ensure_ascii=False),
            enabled=1, source="uploaded",
            risk_level="medium" if result["level"] == CONFIRM else "low",
            confirmed_at=confirmed_at,
        )
        _clear_skill_cache(user_id)

        logger.info(
            f"技能上传安装成功 (user={user_id}, name={name}, "
            f"scripts={len(scripts)}, risk={result['level']}, acknowledged={acknowledge})"
        )
        return {
            "status": "installed",
            "name": name,
            "description": description,
            "scripts_count": len(scripts),
            "risk_level": "medium" if result["level"] == CONFIRM else "low",
            "scan": result,
        }
    except UploadError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": e.code, "message": str(e), "findings": e.findings},
        )
    finally:
        _cleanup_tmp(tmp)


def _is_disable_marker(row: dict) -> bool:
    if row.get("enabled", 1):
        return False
    content = (row.get("skill_content") or "").strip()
    scripts = (row.get("skill_scripts") or "[]").strip()
    return not content and scripts in ("", "[]")


@router.delete("/upload/{name}")
def uninstall_skill(name: str, request: Request):
    """卸载技能（软删：备份到 .trash，保留 30 天可恢复）。"""
    user = get_current_user(request)
    return _uninstall_skill_row(user["db_id"], name)


@router.post("/upload/{name}/restore")
def restore_skill(name: str, request: Request):
    """从回收站恢复已卸载的技能。"""
    user = get_current_user(request)
    return _restore_skill_row(user["db_id"], name)


@router.get("/trash")
def list_trash(request: Request):
    """回收站列表（卸载备份）。"""
    user = get_current_user(request)
    return {"items": _list_trash(user["db_id"])}


# ===== 回收站：永久删除 / 技能导出 =====

# 技能名规范（与 Anthropic Agent Skills 一致，比 backup 名更严）
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _resolve_trash_entry(user_id: int, backup: str) -> str:
    """把回收站里的一个 backup 名解析成绝对路径，并确认它就在本人的 .trash 里。

    回收站条目来自磁盘文件名，但接口参数是用户可控的 —— 必须挡 `../`、
    绝对路径、以及 realpath 之后跳出 .trash 的情况，否则等于给了任意删除。
    """
    if (not backup or backup in (".", "..")
            or "/" in backup or "\\" in backup or os.sep in backup):
        raise HTTPException(status_code=400, detail="备份名不合法")
    trash = os.path.realpath(_trash_dir(user_id))
    path = os.path.realpath(os.path.join(trash, backup))
    if not path.startswith(trash + os.sep):
        raise HTTPException(status_code=400, detail="备份名不合法")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="备份不存在（可能已被清理）")
    return path


@router.delete("/trash/{backup}")
def delete_trash_item(backup: str, request: Request):
    """回收站里**永久删除**某一项（不可恢复）。

    与「卸载」的区别：卸载是把技能移进回收站（30 天可恢复），
    这里是把回收站里那份备份彻底删掉（用户主动放弃恢复）。
    """
    user = get_current_user(request)
    path = _resolve_trash_entry(user["db_id"], backup)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as e:
        logger.error(f"回收站删除失败 user={user['db_id']} backup={backup}: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")

    logger.info(f"回收站永久删除: user={user['db_id']} backup={backup}")
    return {"deleted": backup}


def _skill_disk_dirs(user_id: int, name: str) -> list:
    """技能可能的磁盘目录：系统技能 → agent/skills/{name}；用户文件技能 → user/{db_id}/{name}。"""
    from agent.skill_editor import _get_user_dir
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [
        os.path.join(root, "agent", "skills", name),
        os.path.join(_get_user_dir(user_id), name),
    ]


def _zip_disk_skill(zf: zipfile.ZipFile, skill_dir: str) -> int:
    """把磁盘上的技能目录整体打进 zip（保持相对结构，跳过缓存与回收站）。"""
    n = 0
    for dirpath, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".trash")]
        for fn in sorted(files):
            if fn.endswith((".pyc", ".pyo")) or fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            zf.write(full, os.path.relpath(full, skill_dir))
            n += 1
    return n


def _zip_db_skill(zf: zipfile.ZipFile, row: dict) -> int:
    """把只存表的技能还原成「文件夹 + SKILL.md + scripts/*.py」的规范结构。"""
    n = 0
    content = row.get("skill_content") or ""
    if content.strip():
        zf.writestr("SKILL.md", content)
        n += 1
    try:
        scripts = json.loads(row.get("skill_scripts") or "[]")
    except (json.JSONDecodeError, TypeError):
        scripts = []
    for item in scripts:
        if not isinstance(item, dict):
            continue
        sname = (item.get("name") or "tool").strip() or "tool"
        code = item.get("execution_code") or ""
        zf.writestr(f"scripts/{sname}.py", code)
        n += 1
    return n


@router.get("/export/{name}")
def export_skill(name: str, request: Request):
    """把技能导出成 zip（SKILL.md + scripts/），用于备份或分享给他人再上传。

    同时支持三种来源：
      · 系统技能        —— 读 agent/skills/{name}/
      · 用户「文件」技能 —— 读 agent/skills/user/{db_id}/{name}/
      · 只存表的技能    —— 从 user_skills 行还原成规范目录结构
    """
    user = get_current_user(request)
    uid = user["db_id"]
    if not _SKILL_NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="技能名不合法")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        disk_dir = next((d for d in _skill_disk_dirs(uid, name) if os.path.isdir(d)), None)
        if disk_dir:
            count = _zip_disk_skill(zf, disk_dir)
        else:
            from server.database import get_user_skills
            rows = [r for r in get_user_skills(uid, skill_name=name)
                    if not ((r.get("enabled") == 0) and not (r.get("skill_content") or "").strip())]
            if not rows:
                raise HTTPException(status_code=404, detail=f"技能 '{name}' 不存在")
            count = _zip_db_skill(zf, rows[0])

    if count <= 0:
        raise HTTPException(status_code=404, detail=f"技能 '{name}' 没有可导出的内容")

    data = buf.getvalue()
    logger.info(f"技能导出: user={uid} name={name} files={count} bytes={len(data)}")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )
