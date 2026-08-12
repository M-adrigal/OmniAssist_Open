"""
获取网页内容，支持提取纯文本、HTML源码和按CSS选择器抽取特定内容

Args:
    url: 网页地址，必须以 http:// 或 https:// 开头
    mode: 提取模式，可选 "text"（纯文本）、"html"（HTML源码）、"selector"（CSS选择器），默认 "text"
    selector: CSS选择器，仅在 mode="selector" 时有效

Returns:
    str: 提取的网页内容
"""
# DEPENDENCIES: beautifulsoup4
import urllib.request
import urllib.error
import html
import re

# 强制直连外网，忽略 HTTP_PROXY/HTTPS_PROXY 环境变量。
# 原因：系统技能在主进程执行，会继承部署环境的代理变量；
# 若代理指向不可达地址（如本机端口）会导致联网失败，而沙箱子进程剥离了代理反而直连成功。
# 此处与沙箱行为对齐，确保联网稳定（与 main.py 启动逻辑无关）。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)


def execute(url: str, mode: str = "text", selector: str = "") -> str:
    if not url.startswith(("http://", "https://")):
        return f"错误：URL必须以 http:// 或 https:// 开头，当前值: {url}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return f"HTTP错误: {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"URL错误: {e.reason}"
    except Exception as e:
        return f"获取失败: {e}"

    # 如果返回的是非HTML内容，直接返回文本
    if "text/html" not in content_type and "text/plain" not in content_type and "application/json" not in content_type:
        return raw_html[:5000]

    if mode == "html":
        return raw_html[:50000]

    if mode == "selector" and selector:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw_html, "html.parser")
            elements = soup.select(selector)
            if not elements:
                return f"未找到匹配选择器 '{selector}' 的元素"
            result = []
            for el in elements:
                text = el.get_text(separator=" ", strip=True)
                if text:
                    result.append(text)
            return "\n\n".join(result)[:50000]
        except ImportError:
            return "错误：需要安装 beautifulsoup4 库来支持 CSS 选择器模式"

    # 默认 text 模式：提取纯文本
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        # 移除脚本和样式
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 清理多余空行
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        return "\n".join(lines)[:50000]
    except ImportError:
        # 简单的 HTML 标签移除（无 beautifulsoup 时回退）
        clean = re.sub(r'<script[^>]*>.*?</script>', '', raw_html, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r'<style[^>]*>.*?</style>', '', clean, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r'<[^>]+>', ' ', clean)
        clean = html.unescape(clean)
        lines = [l.strip() for l in clean.split("\n") if l.strip()]
        return "\n".join(lines)[:50000]