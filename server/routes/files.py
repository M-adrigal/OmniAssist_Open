import os
import re
import json
import zipfile
import mimetypes
import csv as csv_mod
import io
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


def _check_file_access(full_path: str, project_root: str, user: dict) -> bool:
    """校验用户对文件的访问权限：仅允许访问自己 document_output/{uid}/ 下的文件。

    管理员不再拥有跨用户访问权限，与普通用户一致按 uid 隔离。
    """
    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        return False

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

    for entry in sorted(os.listdir(output_root)):
        entry_path = os.path.join(output_root, entry)
        if not os.path.isdir(entry_path) or entry.startswith("."):
            continue

        try:
            dir_user_id = int(entry)
        except ValueError:
            continue

        # 仅返回当前用户自己的文件目录（管理员亦不例外）
        if dir_user_id != user["id"]:
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
def list_library(request: Request, search: str = Query("")):
    """列出当前用户的统一文件库（合并生成文件与上传文件），支持模糊搜索。

    仅返回当前登录用户自己的文件，管理员亦只能查看自身文件，实现用户隔离。

    模糊匹配规则：查询词按空格拆分为多个关键词，文件名、内容摘要、文档类型、
    扩展名、来源（上传/生成）中全部命中即视为匹配（不区分大小写、支持中英文子串）。
    """
    user = get_current_user(request)
    project_root = get_project_root()

    files = _recursive_list_library(user["id"], project_root)
    if search and search.strip():
        tokens = [t for t in search.strip().lower().split() if t]
        if tokens:
            def _match(f):
                haystack = " ".join([
                    f.get("name", ""),
                    f.get("content_preview", ""),
                    f.get("category", ""),
                    f.get("ext", ""),
                    "上传" if f.get("source") == "upload" else "生成",
                ]).lower()
                return all(tok in haystack for tok in tokens)
            files = [f for f in files if _match(f)]

    return {
        "files": files,
        "count": len(files),
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


# ===== 预览格式解析辅助函数 =====

def _extract_docx_text(full_path: str, max_chars: int = 15000) -> str:
    """从 docx 文件（ZIP 格式）中提取纯文本内容。"""
    try:
        with zipfile.ZipFile(full_path, 'r') as z:
            xml_path = 'word/document.xml'
            if xml_path not in z.namelist():
                return "[无法读取文档内容]"
            content = z.read(xml_path).decode('utf-8', errors='ignore')
        import xml.etree.ElementTree as ET
        root = ET.fromstring(content)
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        paragraphs = root.findall('.//w:p', ns)
        texts = []
        for p in paragraphs:
            runs = p.findall('.//w:t', ns)
            line = ''.join(r.text or '' for r in runs)
            texts.append(line)
        result = '\n'.join(texts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n\n... (内容已截断，共 {} 字符)'.format(len(result))
        return result
    except Exception as e:
        return "[文档解析失败: {}]".format(str(e)[:100])


def _render_csv_table(full_path: str, max_rows: int = 200) -> dict:
    """将 CSV 文件渲染为 HTML 表格数据返回。"""
    try:
        with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv_mod.reader(f)
            rows = list(reader)
        if not rows:
            return {"type": "csv_table", "html": "<p style='color:#999'>空文件</p>",
                    "filename": os.path.basename(full_path), "rows": 0, "cols": 0}
        headers = rows[0]
        data_rows = rows[1:max_rows+1]
        truncated = len(rows) - 1 > max_rows
        th_html = ''.join('<th>{}</th>'.format(h) for h in headers)
        trs = []
        for r in data_rows:
            tds = ''.join('<td>{}</td>'.format(c) for c in r)
            trs.append('<tr>{}</tr>'.format(tds))
        table_html = (
            "<table class='preview-csv-table' style='width:100%;border-collapse:collapse;font-size:13px;'>"
            "<thead><tr>{}</tr></thead>"
            "<tbody>{}</tbody></table>"
        ).format(th_html, ''.join(trs))
        if truncated:
            table_html += '<p style="color:#999;font-size:12px;margin-top:8px;">仅显示前 {} 行，共 {} 行数据</p>'.format(max_rows, len(rows)-1)
        return {
            "type": "csv_table",
            "html": table_html,
            "filename": os.path.basename(full_path),
            "rows": len(rows) - 1,
            "cols": len(headers),
        }
    except Exception as e:
        return {"type": "text", "content": "[CSV 解析失败: {}]".format(e),
                "filename": os.path.basename(full_path)}


@router.get("/preview")
def preview_file(path: str = Query(...), request: Request = None):
    user = get_current_user(request) if request else None
    project_root = get_project_root()
    full_path = os.path.join(project_root, path)

    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(project_root)):
        raise HTTPException(status_code=403, detail="禁止访问项目外的文件")

    if user and not _check_file_access(full_path, project_root, user):
        raise HTTPException(status_code=403, detail="无权操作此文件")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    ext = os.path.splitext(full_path)[1].lower()
    basename = os.path.basename(full_path)

    # ---- 文本类（直接读取，扩展支持更多编程语言） ----
    text_extensions = {'.txt', '.md', '.json', '.xml', '.html', '.css', '.js', '.py',
                       '.log', '.yaml', '.yml', '.ini', '.cfg', '.toml', '.sh', '.sql',
                       '.ts', '.jsx', '.tsx', '.vue', '.java', '.c', '.cpp', '.h', '.go',
                       '.rs', '.rb', '.php', '.swift', '.kt', '.scala', '.r', '.m'}
    image_extensions = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.webp', '.ico'}

    # CSV 单独处理：渲染为表格
    if ext == '.csv':
        return _render_csv_table(full_path)

    if ext in text_extensions:
        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if len(content) > 50000:
                content = content[:50000] + '\n\n... (文件过大，已截断，共 {} 字符)'.format(len(content))
            return {"type": "text", "content": content, "filename": basename}
        except Exception:
            return {"type": "unsupported", "filename": basename, "hint": "无法读取文本内容"}

    if ext in image_extensions:
        return {"type": "image", "path": path, "filename": basename}

    # docx：提取纯文本（ZIP 内 XML 解析）
    if ext == '.docx':
        text = _extract_docx_text(full_path)
        return {"type": "docx_text", "content": text, "filename": basename}

    # xlsx：提取共享字符串作为预览
    if ext == '.xlsx':
        try:
            with zipfile.ZipFile(full_path, 'r') as z:
                ss_path = 'xl/sharedStrings.xml'
                if ss_path in z.namelist():
                    xml_content = z.read(ss_path).decode('utf-8', errors='ignore')
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(xml_content)
                    ns = {'main': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                    strings = [si.text or '' for si in root.findall('.//main:t', ns)]
                    preview = ' | '.join(strings[:100])
                    if len(strings) > 100:
                        preview += ' ... (共 {} 个字符串)'.format(len(strings))
                    return {"type": "xlsx_preview", "content": preview, "filename": basename}
                else:
                    return {"type": "unsupported", "filename": basename,
                            "hint": "Excel 文件暂无在线预览，可下载后打开"}
        except Exception:
            return {"type": "unsupported", "filename": basename, "hint": "Excel 解析失败"}

    if ext == '.pdf':
        return {"type": "pdf", "path": path, "filename": basename}

    if ext == '.pptx':
        return {"type": "unsupported", "filename": basename,
                "hint": "PPT 暂无在线预览，可下载后用 PowerPoint 或 WPS 打开"}

    return {"type": "unsupported", "filename": basename,
            "hint": "暂不支持 .{} 格式的在线预览，可下载后用对应软件打开".format(ext)}
