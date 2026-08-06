"""
通过和风天气GeoAPI查询城市地理信息，返回城市ID、经纬度、行政区划等信息

Args:
    location: 城市名称（中文或英文），如"北京"、"beijing"

Returns:
    str: JSON格式的城市信息列表
"""
# HTTP_CONFIG:
#   url: https://np6heyjn2u.re.qweatherapi.com/geo/v2/city/lookup?location={location}&key={secret:qweather_api_key}
#   method: GET
import json


def execute(location: str) -> str:
    """此脚本由沙箱通过 HTTP 请求模式执行，execute 函数为响应格式化器"""
    pass


def format_response(raw_data: str, **kwargs) -> str:
    data = json.loads(raw_data)
    if data.get("code") != "200":
        return f"查询失败，错误码：{data.get('code')}"

    locations = data.get("location", [])
    if not locations:
        return "未查询到匹配的城市信息"

    count = len(locations)
    lines = [f"查询到 {count} 个匹配城市："]
    for i, loc in enumerate(locations[:10], 1):
        name = loc.get("name", "")
        loc_id = loc.get("id", "")
        lat = loc.get("lat", "")
        lon = loc.get("lon", "")
        adm1 = loc.get("adm1", "")
        adm2 = loc.get("adm2", "")
        country = loc.get("country", "")
        tz = loc.get("tz", "")
        region = adm1 if adm1 else country
        if adm2 and adm2 != name:
            region = f"{adm1} {adm2}" if adm1 else adm2
        lines.append(f"{i}. {name} | ID: {loc_id} | 坐标: {lat},{lon} | 所属: {region} | 时区: {tz}")
    if count > 10:
        lines.append(f"... 还有 {count - 10} 个结果未显示")
    return "\n".join(lines)