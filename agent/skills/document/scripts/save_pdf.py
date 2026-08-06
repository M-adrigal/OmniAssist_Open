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
import os
import json


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
    font_paths = [
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont("ChineseFont", fp))
                font_name = "ChineseFont"
                break
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