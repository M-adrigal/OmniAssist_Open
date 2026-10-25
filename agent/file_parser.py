"""文件解析引擎

支持解析多种格式文件并提取可读内容，用于对话上下文注入。

支持的格式:
  - 纯文本: .txt .md .csv .json .xml .html .css .js .py .log .yaml .yml
  - PDF: .pdf (PyPDF2)
  - Word: .docx (python-docx)
  - Excel: .xlsx (openpyxl)
  - PowerPoint: .pptx (python-pptx)

策略:
  - 小文件 (< 8KB): 返回完整内容
  - 大文件 (>= 8KB): 返回摘要 + 前 4KB 预览，标注可按需请求原文
"""

import os
import json
import csv as csv_module
import io

SMALL_FILE_THRESHOLD = 8 * 1024
LARGE_PREVIEW_SIZE = 4 * 1024

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js",
    ".py", ".log", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".sh",
    ".bash", ".zsh", ".sql", ".r", ".rb", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".ts", ".tsx", ".jsx", ".vue",
    ".conf", ".env", ".properties",
}

# 图片：不能按文本解析，需以多模态内容块（image_url）送入模型。
# 格式以实际文件内容判定为准，这里仅按扩展名做初筛。
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 图片 -> data URL 的 MIME 类型
IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def is_image_file(file_path: str) -> bool:
    """判断文件是否为受支持的图片（按扩展名初筛）"""
    return os.path.splitext(file_path)[1].lower() in IMAGE_EXTENSIONS


def image_mime_type(file_path: str) -> str:
    """返回图片的 MIME 类型，未知扩展名兜底为 image/png"""
    return IMAGE_MIME_TYPES.get(os.path.splitext(file_path)[1].lower(), "image/png")


def get_file_type(file_path: str) -> str:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext == ".xlsx":
        return "xlsx"
    if ext == ".pptx":
        return "pptx"
    if ext == ".csv":
        return "csv"
    return "unknown"


def parse_file(file_path: str) -> dict:
    """解析单个文件，返回结构化结果

    Args:
        file_path: 文件绝对路径

    Returns:
        {
            "filename": str,
            "file_type": str,
            "file_size": int,
            "is_large": bool,
            "content": str,         # 全量或摘要 + 预览
            "full_content": str,     # 完整内容（仅小文件）
        }
    """
    filename = os.path.basename(file_path)
    file_type = get_file_type(file_path)

    if not os.path.isfile(file_path):
        return {
            "filename": filename,
            "file_type": file_type,
            "file_size": 0,
            "is_large": False,
            "content": f"[文件不存在: {filename}]",
            "full_content": "",
        }

    file_size = os.path.getsize(file_path)

    # 图片：绝不按文本读取（二进制当文本解码必然报错或产生乱码），
    # 只登记元信息 + 磁盘路径，由上下文装配层决定如何以多模态块送入模型。
    if file_type == "image":
        placeholder = f"[图片文件: {filename}（{_format_size(file_size)}）]"
        return {
            "filename": filename,
            "file_type": "image",
            "file_size": file_size,
            "is_large": False,
            "content": placeholder,
            "full_content": placeholder,
            "path": file_path,
        }

    try:
        if file_type in ("text", "csv"):
            with open(file_path, "r", encoding="utf-8") as f:
                full = f.read()
        elif file_type == "pdf":
            full = _parse_pdf(file_path)
        elif file_type == "docx":
            full = _parse_docx(file_path)
        elif file_type == "xlsx":
            full = _parse_xlsx(file_path)
        elif file_type == "pptx":
            full = _parse_pptx(file_path)
        else:
            full = f"[不支持的文件格式: .{os.path.splitext(filename)[1]}]"
    except Exception as e:
        return {
            "filename": filename,
            "file_type": file_type,
            "file_size": file_size,
            "is_large": False,
            "content": f"[解析失败: {filename}, 错误: {str(e)}]",
            "full_content": "",
        }

    is_large = len(full.encode("utf-8")) >= SMALL_FILE_THRESHOLD if full else False

    if is_large:
        content = _build_large_file_content(filename, file_type, full, file_size)
    else:
        content = _build_small_file_content(filename, file_type, full)

    return {
        "filename": filename,
        "file_type": file_type,
        "file_size": file_size,
        "is_large": is_large,
        "content": content,
        "full_content": full if not is_large else "",
    }


def parse_files(file_paths: list[str]) -> list[dict]:
    """批量解析文件"""
    return [parse_file(p) for p in file_paths]


def build_context_prompt(parsed_files: list[dict], file_meta: dict = None) -> str:
    """根据解析结果构建注入对话的上下文文本

    file_meta: 可选，{文件名: mtime(秒)}，用于标注每个文件的上传时间与「最新版本」标记。
    当用户多次上传同名/不同名文件时，模型应优先依据「最新上传版本」的内容作答，
    忽略较早的旧版本，避免沿用过期文档。
    """
    if not parsed_files:
        return ""

    meta = file_meta or {}
    # 图片不参与文本上下文：二进制内容无法以文本表达，强行注入只会得到占位/乱码。
    # 图片由 build_context_parts 单独拆出，交给支持视觉的模型以多模态块送入。
    parsed_files = [pf for pf in (parsed_files or []) if pf.get("file_type") != "image"]
    if not parsed_files:
        return ""
    # 按上传时间倒序，最新版本排在前面
    ordered = sorted(parsed_files, key=lambda pf: meta.get(pf.get("filename", ""), 0), reverse=True)
    # 同名文件只保留最新版本（mtime 最大者），其余标记为旧版本不再展示
    seen = set()
    deduped = []
    for pf in ordered:
        fn = pf.get("filename", "")
        if fn in seen:
            continue
        seen.add(fn)
        deduped.append(pf)

    import datetime
    parts = ["=== 用户上传文件内容 ===\n"]
    n = len(deduped)
    for i, pf in enumerate(deduped):
        fn = pf.get("filename", "")
        mtime = meta.get(fn)
        ts = ""
        if mtime:
            ts = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        if n > 1:
            tag = "（最新上传版本）" if i == 0 else f"（较早版本 #{n - i}）"
        else:
            tag = ""
        header = f"--- 文件：{fn}"
        if ts:
            header += f"  上传于 {ts}"
        header += tag + " ---"
        parts.append(header)
        parts.append(pf["content"])
        parts.append("")

    parts.append("=== 文件内容结束 ===\n")
    parts.append("请基于以上文件内容回答用户的问题。")
    parts.append("如果用户的问题与文件无关，可以先忽略文件内容，正常回答。")
    if n > 1:
        parts.append("注意：同一文件存在多个版本时，以标注「最新上传版本」的内容为准，忽略较早的旧版本。")

    return "\n".join(parts)


def build_context_parts(parsed_files: list, file_meta: dict = None,
                        max_images: int = 5,
                        max_total_bytes: int = 20 * 1024 * 1024) -> dict:
    """把解析结果拆分为「文本上下文」与「图片元信息」两部分，供多模态装配使用。

    图片绝不进文本：二进制按文本解码必然出错。这里只登记磁盘路径与 MIME，
    由调用方（chat.py）在确认模型支持视觉后，再组装成 image_url 内容块。

    Args:
        parsed_files: parse_files 的结果
        file_meta: {文件名: mtime(秒)}，用于版本标注与「最新优先」排序
        max_images: 最多保留的图片张数（防止请求体过大被上游拒绝）
        max_total_bytes: 图片原始字节总量上限。base64 后体积约 ×4/3，
            需为上游请求体上限（如 DeepSeek 48MiB）留出余量

    Returns:
        {
            "text": str,    # 非图片文件的文本上下文（无则空串）
            "images": [     # 图片元信息，最新优先、同名去重
                {"filename": str, "path": str, "size": int, "mime": str}, ...
            ],
            "skipped": int, # 因数量/体积超限被跳过的图片数（用于提示用户）
        }
    """
    meta = file_meta or {}
    all_files = list(parsed_files or [])
    images = [pf for pf in all_files if pf.get("file_type") == "image"]

    # 按上传时间倒序 + 同名去重（与 build_context_prompt 保持同一套版本语义）
    ordered = sorted(images, key=lambda pf: meta.get(pf.get("filename", ""), 0), reverse=True)
    seen = set()
    deduped = []
    for pf in ordered:
        fn = pf.get("filename", "")
        if fn in seen:
            continue
        seen.add(fn)
        deduped.append(pf)

    kept, total, skipped = [], 0, 0
    for pf in deduped:
        path = pf.get("path") or ""
        size = int(pf.get("file_size") or 0)
        # 超限或磁盘文件已不在（如被清理）的图片一律跳过，不静默报错
        if len(kept) >= max_images or total + size > max_total_bytes or not os.path.isfile(path):
            skipped += 1
            continue
        total += size
        kept.append({
            "filename": pf.get("filename", ""),
            "path": path,
            "size": size,
            "mime": image_mime_type(path),
        })

    return {
        "text": build_context_prompt(all_files, file_meta),
        "images": kept,
        "skipped": skipped,
    }


def generate_file_summary(full_content: str, filename: str) -> str:
    """为文件内容生成一个人类可读的摘要

    用于前端展示上传文件卡片时显示简要信息。
    """
    # 图片没有可统计的行/字符数，单独给出可读摘要（否则会显示成 "(22B, 1行)"）
    if is_image_file(filename):
        return f"{filename} (图片文件)"

    lines = full_content.strip().split("\n")
    line_count = len(lines)
    char_count = len(full_content)
    size_str = _format_size(char_count)

    first_lines = "\n".join(lines[:3])
    if len(first_lines) > 200:
        first_lines = first_lines[:200] + "..."

    return f"{filename} ({size_str}, {line_count}行)"


def _parse_pdf(file_path: str) -> str:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return "[PDF解析需要安装 PyPDF2: pip install PyPDF2]"

    reader = PdfReader(file_path)
    parts = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text and text.strip():
            parts.append(f"--- 第{i + 1}页 ---\n{text.strip()}")
    return "\n\n".join(parts) if parts else "[PDF文件中未提取到文本内容，可能是扫描件或图片型PDF]"


def _parse_docx(file_path: str) -> str:
    try:
        from docx import Document
    except ImportError:
        return "[Word解析需要安装 python-docx: pip install python-docx]"

    doc = Document(file_path)
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    for table in doc.tables:
        table_text = []
        for row in table.rows:
            row_text = " | ".join(cell.text for cell in row.cells)
            table_text.append(row_text)
        parts.append("\n".join(table_text))

    return "\n".join(parts) if parts else "[Word文档内容为空]"


def _parse_xlsx(file_path: str) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        return "[Excel解析需要安装 openpyxl: pip install openpyxl]"

    wb = load_workbook(file_path, data_only=True)
    parts = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        parts.append(f"=== 工作表: {sheet_name} ===")

        rows_list = []
        for row in ws.iter_rows(values_only=True):
            row_values = [str(cell) if cell is not None else "" for cell in row]
            if any(v for v in row_values):
                rows_list.append(" | ".join(row_values))

        if len(rows_list) > 500:
            parts.extend(rows_list[:500])
            parts.append(f"... (共 {len(rows_list)} 行，仅显示前 500 行)")
        else:
            parts.extend(rows_list)

    wb.close()
    return "\n".join(parts) if parts else "[Excel工作簿内容为空]"


def _parse_pptx(file_path: str) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        return "[PPT解析需要安装 python-pptx: pip install python-pptx]"

    prs = Presentation(file_path)
    parts = []
    for i, slide in enumerate(prs.slides):
        slide_parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        slide_parts.append(text)
            if shape.has_table:
                table = shape.table
                for row in table.rows:
                    row_text = " | ".join(cell.text for cell in row.cells)
                    slide_parts.append(row_text)
        if slide_parts:
            parts.append(f"--- 幻灯片{i + 1} ---\n" + "\n".join(slide_parts))

    return "\n\n".join(parts) if parts else "[PPT演示文稿内容为空]"


def _build_small_file_content(filename: str, file_type: str, full: str) -> str:
    header = f"[文件: {filename} (类型: {file_type}, 大小: 小文件-完整加载)]"
    return f"{header}\n\n{full}"


def _build_large_file_content(filename: str, file_type: str, full: str, file_size: int) -> str:
    preview = full[:LARGE_PREVIEW_SIZE]
    remaining = len(full) - LARGE_PREVIEW_SIZE
    size_str = _format_size(file_size)

    lines = [
        f"[文件: {filename} (类型: {file_type}, 大小: {size_str})]",
        "",
        "【文件摘要】",
    ]

    content_lines = full.strip().split("\n")
    line_count = len(content_lines)
    char_count = len(full)

    lines.append(f"- 总行数: {line_count}")
    lines.append(f"- 总字符数: {char_count}")
    if file_type == "xlsx":
        lines.append(f"- 包含工作表，已提取文本内容")

    lines.append("")
    lines.append("【前段预览】")
    lines.append(preview)

    if remaining > 0:
        lines.append("")
        lines.append(f"... (剩余 {_format_size(remaining)} 未显示)")
        lines.append("如需查看完整内容，请告知需要哪部分信息。")

    return "\n".join(lines)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"