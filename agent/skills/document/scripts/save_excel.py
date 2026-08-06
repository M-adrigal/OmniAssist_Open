"""
将表格数据保存为Excel(.xlsx)文件，支持简单模式和排版模式

Args:
    filename: Excel文件名（无需扩展名），如"员工信息"
    headers: 简单模式下的表头，多个用逗号分隔，如"姓名,年龄,城市"；排版模式下可为空字符串
    rows: 简单模式下的数据行，每行换行分隔，行内逗号分隔；排版模式下可为空字符串
    formatting: 可选，排版配置JSON对象，控制多Sheet、单元格样式、图表等

Returns:
    str: 生成的文件路径
"""
# DEPENDENCIES: openpyxl
import sys
import os
import json

sys.path.insert(0, os.getcwd())


def execute(filename: str, headers: str = "", rows: str = "", formatting: dict = None) -> str:
    from agent.skills.document.excel_formatter import create_excel_workbook

    output_dir = os.path.join("document_output", "excel_output")

    fmt = None
    if formatting and isinstance(formatting, str):
        try:
            fmt = json.loads(formatting)
        except (json.JSONDecodeError, TypeError):
            pass
    elif formatting and isinstance(formatting, dict):
        fmt = formatting

    return create_excel_workbook(
        filename, headers=headers or "", rows=rows or "",
        formatting=fmt, output_dir=output_dir
    )