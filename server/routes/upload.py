import os
import uuid
import shutil
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File
from server.models import FileItem
from server.routes.auth import get_current_user

router = APIRouter(prefix="/api/files", tags=["files_upload"])

MAX_UPLOAD_COUNT = 5
MAX_FILE_SIZE = 20 * 1024 * 1024

UPLOAD_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js",
    ".py", ".log", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".sh",
    ".bash", ".zsh", ".sql", ".r", ".rb", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".ts", ".tsx", ".jsx", ".vue",
    ".conf", ".env", ".properties",
    ".pdf", ".docx", ".xlsx", ".pptx",
}


def _get_project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_upload_dir(user_id: int, session_id: str) -> str:
    project_root = _get_project_root()
    return os.path.join(project_root, "document_output", str(user_id), "uploads", session_id)


def _list_uploaded_files(user_id: int, session_id: str) -> list[dict]:
    upload_dir = _get_upload_dir(user_id, session_id)
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

    if not session_id.strip():
        raise HTTPException(status_code=400, detail="session_id 不能为空")

    upload_dir = _get_upload_dir(user["id"], session_id)
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
        if len(content) > MAX_FILE_SIZE:
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
    files = _list_uploaded_files(user["id"], session_id)
    return {"files": files, "count": len(files)}


@router.delete("/upload")
def delete_upload(request: Request, session_id: str = Query(...), filename: str = Query(...)):
    user = get_current_user(request)
    upload_dir = _get_upload_dir(user["id"], session_id)
    file_path = os.path.join(upload_dir, filename)

    full_path = os.path.realpath(file_path)
    if not full_path.startswith(os.path.realpath(upload_dir)):
        raise HTTPException(status_code=403, detail="禁止访问")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    os.remove(full_path)
    return {"success": True, "message": f"文件 '{filename}' 已删除"}


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
    if not full_path.startswith(os.path.realpath(uploads_root)):
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
    target_dir = os.path.join(uploads_root, target_session_id)
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
        if not src_path.startswith(os.path.realpath(uploads_root)):
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