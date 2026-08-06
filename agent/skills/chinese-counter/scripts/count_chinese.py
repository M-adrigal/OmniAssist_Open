"""
统计一段文本中汉字的个数，排除标点符号、数字、英文字母等其他字符

Args:
    text: 输入的文本

Returns:
    str: JSON格式的统计结果
"""
import json


def execute(text: str) -> str:
    count = 0
    for char in text:
        if '\u4e00' <= char <= '\u9fff' or '\u3400' <= char <= '\u4dbf':
            count += 1

    return json.dumps({
        "total_chars": len(text),
        "chinese_chars": count,
        "non_chinese_chars": len(text) - count
    }, ensure_ascii=False)