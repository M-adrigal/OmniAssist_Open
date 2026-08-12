"""
查询黄金价格，包括国际现货黄金、国内期货黄金和银行实时金价

Args:
    type: 品种，可选 "all"（全部）、"international"（国际）、"domestic"（国内），默认 "all"

Returns:
    str: JSON格式的黄金价格信息
"""
# HTTP_CONFIG:
#   url: https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1d&range=1d
#   method: GET
import json
import re
import urllib.request
import urllib.error

# 强制直连外网，忽略 HTTP_PROXY/HTTPS_PROXY 环境变量（与沙箱行为对齐）。
# 系统技能在主进程执行会继承部署环境代理，若代理不可达则联网失败；
# 此处强制直连，确保无论服务器是否带代理都能稳定联网。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)


def execute(type: str = "all") -> str:
    result = {}

    # 国际金价 (Yahoo Finance)
    try:
        url = "https://api.gold-api.com/price/XAU"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            result["international"] = {
                "price": data.get("price", "N/A"),
                "currency": data.get("currency", "USD"),
                "unit": "美元/盎司",
                "timestamp": data.get("timestamp", "")
            }
    except Exception as e:
        result["international"] = {"error": f"查询失败: {e}"}

    # 国内金价（上海黄金交易所）
    try:
        url = "https://api.gold-api.com/price/AU9999"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            result["domestic"] = {
                "price": data.get("price", "N/A"),
                "currency": data.get("currency", "CNY"),
                "unit": "元/克",
                "timestamp": data.get("timestamp", ""),
                "name": "黄金9999"
            }
    except Exception as e:
        result["domestic"] = {"error": f"查询失败: {e}"}

    return json.dumps(result, ensure_ascii=False, indent=2)