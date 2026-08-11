import os
import re
import json
import mimetypes
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from server.models import FileItem
from server.routes.auth import get_current_user

router = APIRouter(prefix="/api/files", tags=["files"])


def get_project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


OUTPUT_ROOT = "document_output"


def _get_user_display_name(user_id: int) -> str:
    from server.database import get_user_by_id
    user = get_user_by_id(user_id)
    if user:
        return f"{user['username']} (ID:{user_id})"
    return f"用户{user_id}"


def _is_admin(user: dict) -> bool:
    return user.get("user_type") == "admin"


def _check_file_access(full_path: str, project_root: str, user: dict) -> bool:
    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        return False

    if _is_admin(user):
        return True

    rel = os.path.relpath(full_path, os.path.join(project_root, OUTPUT_ROOT))
    parts = rel.split(os.sep)
    if len(parts) < 2:
        return False
    try:
        file_user_id = int(parts[0])
    except ValueError:
        return False
    return file_user_id == user["id"]


# 文件库类别标签：立即子目录名 -> 中文标签
LIBRARY_CATEGORY_LABELS = {
    "uploads": "上传文件",
    "word_output": "Word 文档",
    "excel_output": "Excel 表格",
    "pdf_output": "PDF 文档",
    "ppt_output": "PPT 演示",
    "csv_output": "CSV 文件",
    "image_output": "图片文件",
    "text_output": "文本文件",
    "generated": "生成文件",
}

_LIBRARY_TEXT_EXTS = {
    '.txt', '.md', '.csv', '.json', '.xml', '.html', '.css', '.js', '.py',
    '.log', '.yaml', '.yml', '.ini', '.cfg', '.toml', '.sh', '.sql',
}


def _recursive_list_library(user_id, project_root):
    """递归扫描 document_output/{user_id}/ 下所有文件（合并生成文件与上传文件）。"""
    root = os.path.join(project_root, OUTPUT_ROOT, str(user_id))
    result = []
    if not os.path.isdir(root):
        return result
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            parts = rel.split(os.sep)
            top_dir = parts[0] if len(parts) > 1 else "其他"
            # 来源：uploads/ 下为用户上传，其余（word_output 等）为平台生成
            source = "upload" if top_dir == "uploads" else "generated"
            category = LIBRARY_CATEGORY_LABELS.get(top_dir, top_dir)
            ext = os.path.splitext(fn)[1].lower().lstrip(".")
            stat = os.stat(full)
            entry = {
                "name": fn,
                "path": os.path.join(OUTPUT_ROOT, str(user_id), rel),
                "size": stat.st_size,
                "ext": ext,
                "source": source,
                "category": category,
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            }
            if os.path.splitext(fn)[1].lower() in _LIBRARY_TEXT_EXTS:
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        entry["content_preview"] = f.read(300)
                except Exception:
                    entry["content_preview"] = ""
            result.append(entry)
    result.sort(key=lambda x: x["mtime"], reverse=True)
    return result


TYPE_LABELS = {
    "word_output": "Word 文档",
    "excel_output": "Excel 表格",
    "pdf_output": "PDF 文档",
    "ppt_output": "PPT 演示",
    "csv_output": "CSV 文件",
    "image_output": "图片文件",
}


@router.get("", response_model=list[FileItem])
def list_files(request: Request):
    user = get_current_user(request)
    project_root = get_project_root()
    output_root = os.path.join(project_root, OUTPUT_ROOT)
    result = []

    if not os.path.isdir(output_root):
        return result

    is_admin = _is_admin(user)

    for entry in sorted(os.listdir(output_root)):
        entry_path = os.path.join(output_root, entry)
        if not os.path.isdir(entry_path) or entry.startswith("."):
            continue

        try:
            dir_user_id = int(entry)
        except ValueError:
            continue

        if not is_admin and dir_user_id != user["id"]:
            continue

        user_label = _get_user_display_name(dir_user_id)

        type_dirs = []
        for sub_entry in sorted(os.listdir(entry_path)):
            sub_path = os.path.join(entry_path, sub_entry)
            if not os.path.isdir(sub_path) or sub_entry.startswith("."):
                continue

            children = []
            for fname in sorted(os.listdir(sub_path)):
                fpath = os.path.join(sub_path, fname)
                if os.path.isfile(fpath) and not fname.startswith("."):
                    children.append(FileItem(
                        name=fname,
                        path=os.path.join(OUTPUT_ROOT, entry, sub_entry, fname),
                        type="file",
                        size=os.path.getsize(fpath),
                    ))

            if not children:
                continue

            display_name = TYPE_LABELS.get(sub_entry, sub_entry)
            type_dirs.append(FileItem(
                name=display_name,
                path=os.path.join(OUTPUT_ROOT, entry, sub_entry),
                type="directory",
                size=0,
                children=children,
            ))

        if not type_dirs:
            continue

        result.append(FileItem(
            name=user_label,
            path=os.path.join(OUTPUT_ROOT, entry),
            type="directory",
            size=0,
            children=type_dirs,
        ))

    return result


@router.get("/library")
def list_library(request: Request, search: str = Query(""), user_id: int = Query(None)):
    """列出某用户的统一文件库（合并生成文件与上传文件），支持搜索与管理员切换用户。"""
    user = get_current_user(request)
    project_root = get_project_root()

    target_uid = user["id"]
    if user_id is not None:
        if not _is_admin(user):
            raise HTTPException(status_code=403, detail="仅管理员可查看其他用户的文件库")
        target_uid = int(user_id)

    files = _recursive_list_library(target_uid, project_root)
    if search and search.strip():
        s = search.strip().lower()
        files = [f for f in files if s in f["name"].lower()]

    return {
        "files": files,
        "count": len(files),
        "is_admin": _is_admin(user),
        "target_uid": target_uid,
    }


@router.post("/rename")
def rename_file(request: Request, body: dict):
    """在用户文件库内重命名文件（仅限同目录内，带访问校验与字符白名单）。"""
    user = get_current_user(request)
    project_root = get_project_root()

    path = (body.get("path") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    if not path or not new_name:
        raise HTTPException(status_code=400, detail="路径与新文件名均不能为空")

    full = os.path.realpath(os.path.join(project_root, path))
    out_root = os.path.realpath(os.path.join(project_root, OUTPUT_ROOT))
    if not full.startswith(out_root):
        raise HTTPException(status_code=403, detail="禁止访问项目外文件")
    if not _check_file_access(full, project_root, user):
        raise HTTPException(status_code=403, detail="无权操作此文件")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")

    # 仅允许同目录内重命名，且文件名做白名单净化
    new_name = os.path.basename(new_name)
    if not re.match(r"^[\w.\- ()（）]+$", new_name):
        raise HTTPException(status_code=400, detail="文件名含非法字符")
    if new_name == os.path.basename(full):
        return {"success": True, "new_path": path, "new_name": new_name}

    new_full = os.path.join(os.path.dirname(full), new_name)
    if os.path.exists(new_full):
        raise HTTPException(status_code=400, detail="同名文件已存在")
    os.rename(full, new_full)
    new_path = os.path.relpath(new_full, project_root)
    return {"success": True, "new_path": new_path, "new_name": new_name}


@router.get("/download")
def download_file(path: str = Query(...), inline: bool = Query(False), request: Request = None):
    user = get_current_user(request) if request else None
    project_root = get_project_root()
    full_path = os.path.join(project_root, path)

    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        raise HTTPException(status_code=403, detail="禁止访问项目外的文件")

    if user and not _check_file_access(full_path, project_root, user):
        raise HTTPException(status_code=403, detail="无权访问此文件")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type, _ = mimetypes.guess_type(full_path)
    if media_type is None:
        media_type = "application/octet-stream"

    if inline:
        return FileResponse(full_path, media_type=media_type)

    return FileResponse(
        full_path,
        media_type=media_type,
        filename=os.path.basename(full_path),
    )


@router.delete("")
def delete_file(path: str = Query(...), request: Request = None):
    user = get_current_user(request) if request else None
    project_root = get_project_root()
    full_path = os.path.join(project_root, path)

    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        raise HTTPException(status_code=403, detail="禁止删除项目外的文件")

    if user and not _check_file_access(full_path, project_root, user):
        raise HTTPException(status_code=403, detail="无权删除此文件")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    os.remove(full_path)
    return {"success": True, "message": f"文件 '{os.path.basename(full_path)}' 已删除"}


@router.get("/preview")
def preview_file(path: str = Query(...), request: Request = None):
    user = get_current_user(request) if request else None
    project_root = get_project_root()
    full_path = os.path.join(project_root, path)

    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        raise HTTPException(status_code=403, detail="禁止访问项目外的文件")

    if user and not _check_file_access(full_path, project_root, user):
        raise HTTPException(status_code=403, detail="无权访问此文件")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    ext = os.path.splitext(full_path)[1].lower()
    text_extensions = {'.txt', '.md', '.csv', '.json', '.xml', '.html', '.css', '.js', '.py', '.log', '.yaml', '.yml', '.ini', '.cfg', '.toml'}
    image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.webp', '.ico'}
    pdf_extensions = {'.pdf'}

    if ext in text_extensions:
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return {"type": "text", "content": content, "filename": os.path.basename(full_path)}
        except Exception:
            return {"type": "unsupported", "filename": os.path.basename(full_path)}

    if ext in image_extensions:
        return {"type": "image", "path": path, "filename": os.path.basename(full_path)}

    if ext in pdf_extensions:
        return {"type": "pdf", "path": path, "filename": os.path.basename(full_path)}

    return {"type": "unsupported", "filename": os.path.basename(full_path)}