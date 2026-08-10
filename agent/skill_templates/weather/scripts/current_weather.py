"""
查询指定城市的实时天气状况，返回温度、体感温度、天气状况、风力风向、湿度、气压、能见度等数据

Args:
    location: 城市LocationID（可通过 geo_lookup 脚本获取），如 "101010100"

Returns:
    str: Markdown格式的天气信息
"""
# HTTP_CONFIG:
#   url: https://{secret:qweather_api_host}/v7/weather/now?location={location}&key={secret:qweather_api_key}
#   method: GET
import json


def execute(location: str) -> str:
    """此脚本由沙箱通过 HTTP 请求模式执行，execute 函数为响应格式化器"""
    pass


def format_response(raw_data: str, **kwargs) -> str:
    data = json.loads(raw_data)
    if data.get("code") != "200":
        return f"查询失败，错误码：{data.get('code')}"

    now = data.get("now", {})
    loc = kwargs.get("location", "")
    unit = kwargs.get("unit", "m")
    temp_unit = "°F" if unit == "i" else "°C"
    wind_unit = "mph" if unit == "i" else "km/h"
    vis_unit = "mile" if unit == "i" else "km"
    press_unit = "inHg" if unit == "i" else "hPa"

    obs_time = now.get("obsTime", "")
    text = now.get("text", "")
    temp = now.get("temp", "")
    feels_like = now.get("feelsLike", "")
    wind_dir = now.get("windDir", "")
    wind_scale = now.get("windScale", "")
    wind_speed = now.get("windSpeed", "")
    humidity = now.get("humidity", "")
    pressure = now.get("pressure", "")
    vis = now.get("vis", "")
    precip = now.get("precip", "")

    weather_emoji = {
        "晴": "☀️", "多云": "⛅", "阴": "☁️", "雨": "🌧️", "小雨": "🌧️",
        "中雨": "🌧️", "大雨": "⛈️", "暴雨": "⛈️", "雪": "❄️", "小雪": "❄️",
        "中雪": "❄️", "大雪": "❄️", "雾": "🌫️", "沙尘": "💨", "霾": "😷"
    }
    emoji = "🌤️"
    for k, e in weather_emoji.items():
        if k in text:
            emoji = e
            break

    lines = [f"## {loc} 实时天气\n"]
    lines.append(f"> 观测时间：{obs_time}\n")
    lines.append("| 项目 | 详情 |")
    lines.append("|------|------|")
    lines.append(f"| {emoji} 天气 | {text} |")
    lines.append(f"| 🌡️ 温度 | {temp}{temp_unit}（体感 {feels_like}{temp_unit}） |")
    wind_str = f"{wind_dir} {wind_scale}级"
    if wind_speed:
        wind_str += f"（{wind_speed}{wind_unit}）"
    lines.append(f"| 💨 风力 | {wind_str} |")
    lines.append(f"| 💧 湿度 | {humidity}% |")
    lines.append(f"| 🎈 气压 | {pressure}{press_unit} |")
    lines.append(f"| 👁️ 能见度 | {vis}{vis_unit} |")
    lines.append(f"| 🌧️ 降水 | {precip}mm |")
    lines.append("")

    if "雨" in text:
        lines.append("> ⚠️ 正在下雨，出门请带伞")
    elif "雪" in text:
        lines.append("> ⚠️ 正在下雪，注意保暖防滑")
    elif "晴" in text:
        lines.append("> ☀️ 天气晴好，适合户外活动")

    return "\n".join(lines)