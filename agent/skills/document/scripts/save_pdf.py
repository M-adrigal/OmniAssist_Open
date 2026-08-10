"""
将内容保存为PDF文档，支持简单模式和排版模式

Args:
    filename: PDF文件名（无需扩展名），如"报告"
    content: 简单模式下的文本内容，排版模式下可为空字符串
    formatting: 可选，排版配置JSON对象，控制页面、字体、段落、表格等

Returns:
    str: 生成的文件路径
"""
# DEPENDENCIES: reportlab
import sys
import os
import json

sys.path.insert(0, os.getcwd())


def _find_cjk_font():
    """跨平台查找可用的中文字体文件，优先使用系统已安装的 CJK 字体。

    返回的字体文件需能被 reportlab 直接注册（TTF/OTF/TTC）。
    找不到时返回 None，调用方回退到默认字体（Helvetica，中文会显示为方块）。

    Linux 服务器需在系统层安装中文字体，例如：
        sudo apt install fonts-noto-cjk
    安装后字体通常位于 /usr/share/fonts/opentype/noto/ 或 /usr/share/fonts/truetype/noto/
    """
    import glob

    candidates = [
        # macOS
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        # Windows
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        # Linux (Debian/Ubuntu: apt install fonts-noto-cjk)
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            return fp
    # 兜底：在常见字体目录下模糊匹配 CJK 字体文件名
    patterns = [
        "/usr/share/fonts/**/NotoSansCJK*.ttc",
        "/usr/share/fonts/**/NotoSansCJK*.otf",
        "/usr/share/fonts/**/NotoSansSC*.otf",
        "/usr/share/fonts/**/wqy*.ttc",
        "/usr/share/fonts/**/DroidSansFallback*.ttf",
        "/usr/share/fonts/**/SourceHanSans*.otf",
    ]
    for pat in patterns:
        for hit in glob.glob(pat, recursive=True):
            if os.path.isfile(hit):
                return hit
    return None


def execute(filename: str, content: str = "", formatting: dict = None) -> str:
    output_dir = os.path.join("document_output", "pdf_output")
    os.makedirs(output_dir, exist_ok=True)

    fmt = None
    if formatting and isinstance(formatting, str):
        try:
            fmt = json.loads(formatting)
        except (json.JSONDecodeError, TypeError):
            pass
    elif formatting and isinstance(formatting, dict):
        fmt = formatting

    if fmt and isinstance(fmt, dict):
        from agent.skills.document.pdf_formatter import create_pdf_document
        return create_pdf_document(filename, content=content, formatting=fmt, output_dir=output_dir)

    # 简单模式
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    filepath = os.path.join(output_dir, f"{filename}.pdf")

    font_name = "Helvetica"
    cjk_font = _find_cjk_font()
    if cjk_font:
        try:
            pdfmetrics.registerFont(TTFont("ChineseFont", cjk_font))
            font_name = "ChineseFont"
        except Exception:
            pass

    doc = SimpleDocTemplate(filepath, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("CustomTitle", parent=styles["Heading1"], fontName=font_name, fontSize=18, spaceAfter=12)
    body_style = ParagraphStyle("CustomBody", parent=styles["Normal"], fontName=font_name, fontSize=11, leading=18)

    story = []
    story.append(Paragraph(filename, title_style))
    story.append(Spacer(1, 0.5*cm))

    for para in [p.strip() for p in content.replace("\r", "").split("\n") if p.strip()]:
        story.append(Paragraph(para, body_style))
        story.append(Spacer(1, 0.2*cm))

    doc.build(story)
    return f"PDF文档已成功保存至: {filepath}"