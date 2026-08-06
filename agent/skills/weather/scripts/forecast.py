"""
查询指定城市未来3-30天天气预报，返回每日温度、天气状况、风力风向等数据

Args:
    days: 预报天数，可选 "3d"、"7d"、"10d"、"15d"、"30d"
    location: 城市LocationID（可通过 geo_lookup 脚本获取），如 "101010100"

Returns:
    str: Markdown格式的天气预报
"""
# HTTP_CONFIG:
#   url: https://np6heyjn2u.re.qweatherapi.com/v7/weather/{days}?location={location}&key={secret:qweather_api_key}
#   method: GET
import json
from datetime import date


def execute(days: str, location: str) -> str:
    """此脚本由沙箱通过 HTTP 请求模式执行，execute 函数为响应格式化器"""
    pass


def format_response(raw_data: str, **kwargs) -> str:
    data = json.loads(raw_data)
    if data.get("code") != "200":
        return f"查询失败，错误码：{data.get('code')}"

    daily = data.get("daily", [])
    days_map = {"3d": "3", "7d": "7", "10d": "10", "15d": "15", "30d": "30"}
    n = days_map.get(kwargs.get("days", "3d"), "3")
    loc = kwargs.get("location", "")
    unit = kwargs.get("unit", "m")
    temp_unit = "°F" if unit == "i" else "°C"

    lines = [f"## {loc} 未来{n}天天气预报\n"]
    lines.append("| 日期 | 白天 | 夜间 | 温度(高/低) | 风力风向 | 湿度 | 紫外线 | 降水 | 日出/日落 |")
    lines.append("|:-----|:-----|:-----|:------------|:---------|:-----|:-------|:-----|:----------|")

    weather_emoji = {
        "晴": "☀️", "多云": "⛅", "阴": "☁️", "雨": "🌧️", "小雨": "🌧️",
        "中雨": "🌧️", "大雨": "⛈️", "暴雨": "⛈️", "雪": "❄️", "小雪": "❄️",
        "中雪": "❄️", "大雪": "❄️", "雾": "🌫️", "沙尘": "💨", "霾": "😷"
    }
    uv_levels = [(0, "弱"), (3, "中等"), (6, "强"), (8, "很强"), (11, "极强")]

    def get_uv(uv_val):
        try:
            u = int(uv_val)
            for th, label in reversed(uv_levels):
                if u >= th:
                    return f"{label}({u})"
        except (ValueError, TypeError):
            return str(uv_val)
        return str(uv_val)

    def get_emoji(text):
        for k, e in weather_emoji.items():
            if k in text:
                return e
        return "🌤️"

    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    has_rain = False
    has_high_uv = False

    for d in daily:
        date_str = d.get("fxDate", "")
        try:
            parts = date_str.split("-")
            dt = date(int(parts[0]), int(parts[1]), int(parts[2]))
            wd = weekdays[dt.weekday()]
            date_show = f"{dt.month}月{dt.day}日 {wd}"
        except (ValueError, IndexError):
            date_show = date_str

        text_day = d.get("textDay", "")
        text_night = d.get("textNight", "")
        temp_max = d.get("tempMax", "")
        temp_min = d.get("tempMin", "")
        wind_dir = d.get("windDirDay", "")
        wind_scale = d.get("windScaleDay", "")
        wind = f"{wind_dir}{wind_scale}级" if wind_dir else ""
        humidity = d.get("humidity", "")
        uv = d.get("uvIndex", "")
        precip = d.get("precip", "")
        sunrise = d.get("sunrise", "")
        sunset = d.get("sunset", "")

        day_icon = get_emoji(text_day)
        night_icon = get_emoji(text_night)
        day_cell = f"{day_icon}{text_day} {temp_max}{temp_unit}"
        night_cell = f"{night_icon}{text_night} {temp_min}{temp_unit}"
        temp_cell = f"{temp_max}/{temp_min}{temp_unit}"
        uv_cell = get_uv(uv)

        try:
            p = float(precip)
            precip_cell = "无" if p == 0 else f"{p}mm"
            if p > 0:
                has_rain = True
        except (ValueError, TypeError):
            precip_cell = str(precip)

        try:
            if int(uv) >= 6:
                has_high_uv = True
        except (ValueError, TypeError):
            pass

        sr_ss = f"{sunrise}/{sunset}" if sunrise else ""
        lines.append(f"| {date_show} | {day_cell} | {night_cell} | {temp_cell} | {wind} | {humidity}% | {uv_cell} | {precip_cell} | {sr_ss} |")

    lines.append("")
    tips = []
    if has_rain:
        tips.append("有降雨，出门请带伞")
    if has_high_uv:
        tips.append("紫外线较强，注意防晒")
    if tips:
        lines.append("> ⚠️ " + "；".join(tips))

    return "\n".join(lines)