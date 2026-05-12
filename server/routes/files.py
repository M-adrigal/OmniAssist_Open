import os
import json
import mimetypes
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from server.models import FileItem

router = APIRouter(prefix="/api/files", tags=["files"])


def get_project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


OUTPUT_ROOT = "document_output"


def _get_output_dirs():
    project_root = get_project_root()
    output_root = os.path.join(project_root, OUTPUT_ROOT)
    if not os.path.isdir(output_root):
        return []
    output_dirs = []
    for entry in os.listdir(output_root):
        entry_path = os.path.join(output_root, entry)
        if os.path.isdir(entry_path) and not entry.startswith("."):
            output_dirs.append(entry)
    return sorted(output_dirs)


@router.get("", response_model=list[FileItem])
def list_files():
    project_root = get_project_root()
    output_root = os.path.join(project_root, OUTPUT_ROOT)
    result = []

    for dir_name in _get_output_dirs():
        dir_path = os.path.join(output_root, dir_name)
        children = []
        for fname in sorted(os.listdir(dir_path)):
            fpath = os.path.join(dir_path, fname)
            if os.path.isfile(fpath) and not fname.startswith("."):
                children.append(FileItem(
                    name=fname,
                    path=os.path.join(OUTPUT_ROOT, dir_name, fname),
                    type="file",
                    size=os.path.getsize(fpath),
                ))

        result.append(FileItem(
            name=dir_name,
            path=os.path.join(OUTPUT_ROOT, dir_name),
            type="directory",
            size=0,
            children=children,
        ))

    return result


@router.get("/download")
def download_file(path: str = Query(...)):
    project_root = get_project_root()
    full_path = os.path.join(project_root, path)

    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        raise HTTPException(status_code=403, detail="禁止访问项目外的文件")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type, _ = mimetypes.guess_type(full_path)
    if media_type is None:
        media_type = "application/octet-stream"

    return FileResponse(
        full_path,
        media_type=media_type,
        filename=os.path.basename(full_path),
    )


@router.delete("")
def delete_file(path: str = Query(...)):
    project_root = get_project_root()
    full_path = os.path.join(project_root, path)

    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        raise HTTPException(status_code=403, detail="禁止删除项目外的文件")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    os.remove(full_path)
    return {"success": True, "message": f"文件 '{os.path.basename(full_path)}' 已删除"}