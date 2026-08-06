"""
将内容保存为Word(.docx)文档，支持简单模式和排版模式

Args:
    filename: Word文件名（无需扩展名），如"项目报告"
    content: 简单模式下的文本内容，排版模式下可为空字符串
    formatting: 可选，排版配置JSON对象，控制字体、段落、页面、表格等

Returns:
    str: 生成的文件路径
"""
# DEPENDENCIES: python-docx
import sys
import os
import json

sys.path.insert(0, os.getcwd())


def execute(filename: str, content: str = "", formatting: dict = None) -> str:
    from agent.skills.document.document_formatter import create_word_document

    output_dir = os.path.join("document_output", "word_output")

    fmt = None
    if formatting and isinstance(formatting, str):
        try:
            fmt = json.loads(formatting)
        except (json.JSONDecodeError, TypeError):
            pass
    elif formatting and isinstance(formatting, dict):
        fmt = formatting

    return create_word_document(filename, content=content or "", formatting=fmt, output_dir=output_dir)