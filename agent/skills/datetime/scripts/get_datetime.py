"""
获取当前日期和时间，返回包含日期、时间、星期、时间戳的完整信息

Args:
    format: 输出格式，可选 "full"（完整）、"date"（仅日期）、"time"（仅时间）、"timestamp"（时间戳），默认 "full"

Returns:
    str: JSON格式的时间信息
"""
from datetime import datetime
import json


def execute(format: str = "full") -> str:
    now = datetime.now()

    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

    result = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekdays[now.weekday()],
        "timestamp": int(now.timestamp()),
        "iso": now.isoformat()
    }

    if format == "date":
        output = {"date": result["date"], "weekday": result["weekday"]}
    elif format == "time":
        output = {"time": result["time"]}
    elif format == "timestamp":
        output = {"timestamp": result["timestamp"]}
    else:
        output = result

    return json.dumps(output, ensure_ascii=False, indent=2)