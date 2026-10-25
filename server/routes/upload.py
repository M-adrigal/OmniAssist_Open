import os
import re
import uuid
import shutil
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Response
from server.models import FileItem
from server.routes.auth import get_current_user

router = APIRouter(prefix="/api/files", tags=["files_upload"])

MAX_UPLOAD_COUNT = 5
MAX_FILE_SIZE = 20 * 1024 * 1024

# 图片单独限流：图片会以 base64（体积 ×4/3）随对话请求发给上游模型，
# 而上游请求体存在上限（如 DeepSeek 48MiB）。单图压到 10MB、总量在装配时
# 另有 20MB 上限，避免「一次上传多张大图」直接把请求撑爆。
MAX_IMAGE_SIZE = 10 * 1024 * 1024

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

UPLOAD_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js",
    ".py", ".log", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".sh",
    ".bash", ".zsh", ".sql", ".r", ".rb", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".ts", ".tsx", ".jsx", ".vue",
    ".conf", ".env", ".properties",
    ".pdf", ".docx", ".xlsx", ".pptx",
} | IMAGE_EXTENSIONS


def _get_project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_upload_dir(user_id: int, session_id: str) -> str:
    project_root = _get_project_root()
    return os.path.join(project_root, "document_output", str(user_id), "uploads", session_id)


# session_id 只允许这一组字符：真实会话 id 是 uuid4，不会含分隔符或点号。
# 目的：堵死 `../../static`、`../../{他人id}/uploads/{sid}` 这类构造路径。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")


def _resolve_session_dir(user: dict, session_id: str, *, must_own: bool = True) -> str:
    """解析「当前用户在某会话下的上传目录」，是全部上传接口的唯一入口。

    三层校验，缺一不可（历史上这里完全不校验，导致任意目录读写删）：
      ① 格式：只允许 [A-Za-z0-9-]，拒绝 .. / \\ 等路径分隔符；
      ② 边界：realpath 后必须落在本人 uploads 目录内（含 os.sep 防兄弟目录前缀）；
      ③ 归属：会话若存在于库中，必须属于本人（管理员也不例外）。

    会话不存在时放行——前端存在「先上传再发首条消息」的时序，且路径已被
    ② 限制在本人目录内，最多污染自己的空间。
    """
    sid = (session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    if not _SESSION_ID_RE.fullmatch(sid):
        raise HTTPException(status_code=400, detail="session_id 格式不合法")

    project_root = _get_project_root()
    uploads_root = os.path.realpath(
        os.path.join(project_root, "document_output", str(user["id"]), "uploads")
    )
    target = os.path.realpath(os.path.join(uploads_root, sid))
    if target != uploads_root and not target.startswith(uploads_root + os.sep):
        raise HTTPException(status_code=400, detail="session_id 格式不合法")

    if must_own:
        # 会话存在但不属于本人 → 拒绝（杜绝跨租户读写）。
        # 与 sessions.py 的口径一致：不存在与非本人统一 404，不泄露存在性。
        try:
            from server.database import get_session
            sess = get_session(sid)
        except Exception:
            sess = None
        if sess is not None and sess.get("user_id") != user.get("db_id"):
            raise HTTPException(status_code=404, detail="会话不存在")

    return target


def _list_uploaded_files(upload_dir: str) -> list[dict]:
    if not os.path.isdir(upload_dir):
        return []

    from agent.file_parser import parse_file, generate_file_summary

    result = []
    for fname in sorted(os.listdir(upload_dir)):
        fpath = os.path.join(upload_dir, fname)
        if os.path.isfile(fpath) and not fname.startswith("."):
            parsed = parse_file(fpath)
            result.append({
                "filename": fname,
                "path": os.path.relpath(fpath, _get_project_root()),
                "size": os.path.getsize(fpath),
                "type": parsed["file_type"],
                "is_large": parsed["is_large"],
                "summary": generate_file_summary(parsed.get("full_content", "") or parsed.get("content", ""), fname),
                "content_preview": parsed["full_content"][:300] if parsed.get("full_content") else parsed.get("content", "")[:300],
            })
    return result


@router.post("/upload")
async def upload_files(
    request: Request,
    files: list[UploadFile] = File(...),
    session_id: str = Query(...),
):
    user = get_current_user(request)

    if not files:
        raise HTTPException(status_code=400, detail="请选择至少一个文件")

    if len(files) > MAX_UPLOAD_COUNT:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_UPLOAD_COUNT} 个文件")

    upload_dir = _resolve_session_dir(user, session_id)
    os.makedirs(upload_dir, exist_ok=True)

    # 统计当前已上传文件数
    existing_count = len([f for f in os.listdir(upload_dir)
                          if os.path.isfile(os.path.join(upload_dir, f)) and not f.startswith(".")])

    if existing_count + len(files) > MAX_UPLOAD_COUNT:
        raise HTTPException(status_code=400, detail=f"当前会话已有 {existing_count} 个文件，最多保留 {MAX_UPLOAD_COUNT} 个")

    uploaded = []
    errors = []

    for file in files:
        if not file.filename:
            continue

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in UPLOAD_EXTENSIONS:
            errors.append(f"{file.filename}: 不支持的文件格式")
            continue

        safe_filename = _safe_filename(file.filename)
        dest_path = os.path.join(upload_dir, safe_filename)

        content = await file.read()

        if ext in IMAGE_EXTENSIONS:
            if len(content) > MAX_IMAGE_SIZE:
                errors.append(f"{file.filename}: 图片超过 {MAX_IMAGE_SIZE // (1024*1024)}MB 限制")
                continue
            # 不信任扩展名：按魔数校验真实格式，避免任意二进制/脚本改名成 .png 混入，
            # 后续被当图片透传给模型或以图片类型内联返回给浏览器。
            if not _is_valid_image(content, ext):
                errors.append(f"{file.filename}: 文件内容不是有效的图片")
                continue
        elif len(content) > MAX_FILE_SIZE:
            errors.append(f"{file.filename}: 文件超过 {MAX_FILE_SIZE // (1024*1024)}MB 限制")
            continue

        with open(dest_path, "wb") as f:
            f.write(content)

        from agent.file_parser import parse_file, generate_file_summary

        parsed = parse_file(dest_path)
        uploaded.append({
            "filename": safe_filename,
            "path": os.path.relpath(dest_path, _get_project_root()),
            "size": len(content),
            "type": parsed["file_type"],
            "is_large": parsed["is_large"],
            "summary": generate_file_summary(parsed.get("full_content", "") or parsed.get("content", ""), safe_filename),
            "content_preview": parsed["full_content"][:300] if parsed.get("full_content") else parsed.get("content", "")[:300],
        })

    return {"uploaded": uploaded, "errors": errors, "existing_count": existing_count + len(uploaded)}


@router.get("/uploads")
def list_uploads(request: Request, session_id: str = Query(...)):
    user = get_current_user(request)
    upload_dir = _resolve_session_dir(user, session_id)
    files = _list_uploaded_files(upload_dir)
    return {"files": files, "count": len(files)}


@router.delete("/upload")
def delete_upload(request: Request, session_id: str = Query(...), filename: str = Query(...)):
    user = get_current_user(request)
    upload_dir = _resolve_session_dir(user, session_id)
    # 只取文件名部分，杜绝 filename 里夹带路径分隔符
    filename = os.path.basename((filename or "").strip())
    if not filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="文件名不合法")
    file_path = os.path.join(upload_dir, filename)

    full_path = os.path.realpath(file_path)
    if full_path != upload_dir and not full_path.startswith(upload_dir + os.sep):
        raise HTTPException(status_code=403, detail="禁止访问")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    os.remove(full_path)
    return {"success": True, "message": f"文件 '{filename}' 已删除"}


def _is_valid_image(content: bytes, ext: str) -> bool:
    """按魔数校验图片真实格式（不信任扩展名）。

    目的：防止把任意二进制或脚本改名成 .png 上传——否则它既会被当图片透传给
    上游模型，也可能被以图片 MIME 内联返回给浏览器。仅放行 png/jpg/gif/webp/bmp，
    **不含 svg**（svg 可内嵌脚本，内联返回存在 XSS 风险）。
    """
    if not content:
        return False
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ext == ".png"
    if content[:3] == b"\xff\xd8\xff":
        return ext in (".jpg", ".jpeg")
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ext == ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ext == ".webp"
    if content[:2] == b"BM":
        return ext == ".bmp"
    return False


def _safe_filename(filename: str) -> str:
    name, ext = os.path.splitext(filename)
    name = "".join(c for c in name if c.isalnum() or c in "._- ()（）")
    name = name.strip()[:100]
    unique = uuid.uuid4().hex[:8]
    return f"{name}_{unique}{ext}" if name else f"{unique}{ext}"


@router.get("/all-uploads")
def list_all_uploads(request: Request, search: str = Query("")):
    user = get_current_user(request)
    project_root = _get_project_root()
    uploads_root = os.path.join(project_root, "document_output", str(user["id"]), "uploads")
    if not os.path.isdir(uploads_root):
        return {"files": [], "count": 0}

    from agent.file_parser import parse_file, generate_file_summary

    result = []
    search_lower = search.strip().lower()
    for session_id in sorted(os.listdir(uploads_root)):
        session_dir = os.path.join(uploads_root, session_id)
        if not os.path.isdir(session_dir):
            continue
        for fname in sorted(os.listdir(session_dir)):
            fpath = os.path.join(session_dir, fname)
            if os.path.isfile(fpath) and not fname.startswith("."):
                if search_lower and search_lower not in fname.lower():
                    continue
                stat = os.stat(fpath)
                parsed = parse_file(fpath)
                result.append({
                    "filename": fname,
                    "path": os.path.relpath(fpath, project_root),
                    "session_id": session_id,
                    "size": stat.st_size,
                    "type": parsed["file_type"],
                    "is_large": parsed["is_large"],
                    "summary": generate_file_summary(parsed.get("full_content", "") or parsed.get("content", ""), fname),
                    "content_preview": parsed["full_content"][:500] if parsed.get("full_content") else parsed.get("content", "")[:500],
                    "upload_time": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })

    result.sort(key=lambda x: x["upload_time"], reverse=True)
    return {"files": result, "count": len(result)}


@router.delete("/all-uploads")
def delete_all_upload(request: Request, path: str = Query(...)):
    user = get_current_user(request)
    project_root = _get_project_root()
    uploads_root = os.path.join(project_root, "document_output", str(user["id"]), "uploads")

    full_path = os.path.realpath(os.path.join(project_root, path))
    _uploads_real = os.path.realpath(uploads_root)
    if full_path != _uploads_real and not full_path.startswith(_uploads_real + os.sep):
        raise HTTPException(status_code=403, detail="禁止访问")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    os.remove(full_path)
    return {"success": True, "message": "文件已删除"}


@router.post("/reference-files")
async def reference_files(request: Request):
    body = await request.json()
    paths: list[str] = body.get("paths", [])
    target_session_id: str = body.get("session_id", "")

    user = get_current_user(request)

    if not paths:
        raise HTTPException(status_code=400, detail="请选择至少一个文件")
    if not target_session_id.strip():
        raise HTTPException(status_code=400, detail="session_id 不能为空")
    if len(paths) > MAX_UPLOAD_COUNT:
        raise HTTPException(status_code=400, detail=f"一次最多引用 {MAX_UPLOAD_COUNT} 个文件")

    project_root = _get_project_root()
    uploads_root = os.path.join(project_root, "document_output", str(user["id"]), "uploads")
    target_dir = _resolve_session_dir(user, target_session_id)
    os.makedirs(target_dir, exist_ok=True)

    existing_count = len([f for f in os.listdir(target_dir)
                          if os.path.isfile(os.path.join(target_dir, f)) and not f.startswith(".")])

    if existing_count + len(paths) > MAX_UPLOAD_COUNT:
        raise HTTPException(status_code=400, detail=f"当前会话已有 {existing_count} 个文件，最多保留 {MAX_UPLOAD_COUNT} 个")

    from agent.file_parser import parse_file, generate_file_summary

    referenced = []
    errors = []

    for rel_path in paths:
        src_path = os.path.realpath(os.path.join(project_root, rel_path))
        _uploads_real = os.path.realpath(uploads_root)
        if src_path != _uploads_real and not src_path.startswith(_uploads_real + os.sep):
            errors.append(f"{rel_path}: 路径不在允许范围内")
            continue
        if not os.path.isfile(src_path):
            errors.append(f"{rel_path}: 文件不存在")
            continue

        basename = os.path.basename(src_path)
        dest_path = os.path.join(target_dir, basename)
        if os.path.exists(dest_path):
            name, ext = os.path.splitext(basename)
            unique = uuid.uuid4().hex[:6]
            dest_path = os.path.join(target_dir, f"{name}_{unique}{ext}")

        shutil.copy2(src_path, dest_path)

        parsed = parse_file(dest_path)
        size = os.path.getsize(dest_path)
        referenced.append({
            "filename": os.path.basename(dest_path),
            "path": os.path.relpath(dest_path, project_root),
            "size": size,
            "type": parsed["file_type"],
            "is_large": parsed["is_large"],
            "summary": generate_file_summary(parsed.get("full_content", "") or parsed.get("content", ""), os.path.basename(dest_path)),
            "content_preview": parsed["full_content"][:300] if parsed.get("full_content") else parsed.get("content", "")[:300],
        })

    return {"referenced": referenced, "errors": errors, "existing_count": existing_count + len(referenced)}


@router.get("/upload-thumb")
def upload_thumb(session_id: str = Query(...), filename: str = Query(...),
                 request: Request = None):
    """按「会话 + 文件名」取一张已上传图片的原始字节，供聊天气泡里显示缩略图。

    为什么不用 `/api/files/preview?path=`：会话历史里只存了文件名（没存完整路径），
    而前端拿不到 public_id，拼不出 `document_output/{public_id}/uploads/{sid}/...`。
    这里用 `_resolve_session_dir` 做同样的三层校验（格式 / 边界 / 归属），
    把「文件名」限制在本人该会话的上传目录内。
    """
    user = get_current_user(request)
    name = os.path.basename(filename or "")
    if not name or name != (filename or ""):
        raise HTTPException(status_code=400, detail="文件名不合法")

    session_dir = _resolve_session_dir(user, session_id)
    full = os.path.realpath(os.path.join(session_dir, name))
    root = os.path.realpath(session_dir)
    if not full.startswith(root + os.sep):
        raise HTTPException(status_code=400, detail="文件名不合法")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")

    from agent.file_parser import image_mime_type
    ext = os.path.splitext(full)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        raise HTTPException(status_code=415, detail="只有图片支持此接口")

    with open(full, "rb") as f:
        data = f.read()
    return Response(content=data, media_type=image_mime_type(full),
                    headers={"Cache-Control": "private, max-age=3600"})
