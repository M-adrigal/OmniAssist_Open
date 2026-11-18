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
import ipaddress
import re
import socket
from urllib.parse import urlparse

# 强制直连外网，忽略 HTTP_PROXY/HTTPS_PROXY 环境变量。
# 原因：系统技能在主进程执行，会继承部署环境的代理变量；
# 若代理指向不可达地址（如本机端口）会导致联网失败，而沙箱子进程剥离了代理反而直连成功。
# 此处与沙箱行为对齐，确保联网稳定（与 main.py 启动逻辑无关）。
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

# 技能脚本会在沙箱里执行，不能强依赖 agent.* 模块（那里可能被隔离）。
# 拿不到 logger 就退化成静默，不影响主流程。
try:
    from agent.logger import get_logger
    logger = get_logger("fetch")
except Exception:                                    # pragma: no cover
    import logging as _logging
    logger = _logging.getLogger("agent.fetch")


# RFC 2544 保留给网络设备基准测试，真实业务地址不会用它。
# Clash / Surge / Mihomo 的 fake-ip 模式默认就用这段做占位。
_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")
# ISATAP 前缀 ::ffff:0:0:0/96 —— 低 32 位内嵌 IPv4。
# fake-ip 的 AAAA 记录常以这种形式出现（如 ::ffff:0:c612:dd = 198.18.0.221），
# 而它的 ipv4_mapped 是 None，不走 IPv4-mapped 那条路，必须单独解出来。
_ISATAP_NET = ipaddress.ip_network("::ffff:0:0:0/96")


def _embedded_ipv4(ip):
    """取出 IPv6 里内嵌的 IPv4（IPv4-mapped 或 ISATAP），取不到返回 None。"""
    if ip.version != 6:
        return None
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip in _ISATAP_NET:
        try:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        except ValueError:
            return None
    return None


def _is_fake_ip(ip_str: str) -> bool:
    """识别代理软件的 fake-ip 占位地址（RFC 2544 基准测试网段 198.18.0.0/15）。

    代理开启 fake-ip 模式时，DNS 会把**任何域名**都解析到 198.18.x.x，
    真连接由 TUN / 透明代理在系统层截获再转发到真实公网。
    这个网段不可路由，本身到不了任何内网，所以它是"占位符"而非真实目标。

    不识别它会导致：只要本机开着代理，所有域名都被判成"内网地址"而拒绝抓取
    —— 表现为工具对 example.com 这种站点也报"只允许访问公网地址"。
    """
    try:
        ip = ipaddress.ip_address(ip_str.strip("[]"))
    except ValueError:
        return False
    v4 = _embedded_ipv4(ip)
    if v4 is not None:
        return v4 in _FAKE_IP_NET
    return ip.version == 4 and ip in _FAKE_IP_NET



def _is_public_host(host: str) -> bool:
    """只允许公网目标。

    这个工具在沙箱里被「按工具名放行网络」（net_allow="*"），即能访问任意主机 ——
    包括 127.0.0.1、内网 10.x/172.16-31.x/192.168.x、以及云厂商元数据服务
    169.254.169.254。那不叫"抓网页"，等于把沙箱变成内网探测器。
    这里按**解析后的 IP** 判断，所以「域名指向内网」同样会被拦住。
    """
    host = (host or "").strip().strip("[]")
    if not host:
        return False

    def _ok(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        # IPv4-mapped / ISATAP 先把内嵌的 IPv4 解出来再判：
        # 直接按 v6 判会因 ::ffff:0:0:0/96 属 reserved 而被一律拒掉。
        v4 = _embedded_ipv4(ip)
        if v4 is not None:
            ip = v4
        return (ip.is_global and not ip.is_private and not ip.is_loopback
                and not ip.is_link_local and not ip.is_reserved and not ip.is_multicast)

    # 关键：先判断"是不是显式 IP"，是才直接判。不能直接 return _ok(host) ——
    # 那样域名会因为 ip_address() 解析失败而被判成非法，把正常网址全拦死。
    try:
        ipaddress.ip_address(host)
        # 用户直接写 198.18.x.x —— 那是真的在探测这个保留段，不放行
        return _ok(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    if not infos:
        return False

    addrs = {i[4][0] for i in infos}
    real = {a for a in addrs if not _is_fake_ip(a)}

    # 整组都是 fake-ip → 本机代理在接管解析，真实目标是公网由代理决定。
    #
    # ⚠️ 这里必须承认一个局限：fake-ip 是**占位符**，代理会把任何域名
    # （包括不存在的 no-such-host.invalid）都解析成 198.18.x.x。
    # 也就是说在这个模式下，DNS 层**无法**区分"真实公网域名"和"根本不存在的域名"，
    # 更无法看到真实的解析结果。要么放行（SSRF 校验降级，但工具能用），
    # 要么拒绝（校验严格，但所有装代理的开发机上都彻底不可用）——二者不可兼得。
    #
    # 选择放行 + 告警：目标若是内网域名，最终能否访问由代理规则决定，
    # 不在本工具可控范围内；不放行则等于让工具对最常见的本地开发环境完全失效。
    # 不存在的域名会在真实连接阶段失败，用户看到的是明确的"[抓取失败]"。
    if addrs and not real:
        logger.warning(
            f"检测到 fake-ip 代理（{sorted(addrs)[:2]}），"
            f"{host} 的公网校验降级为「交给代理」——DNS 层无法看到真实目标")
        return True

    # 混有真实地址 → 逐个校验；此时出现 fake-ip 说明解析结果不可信，一律按真实地址判
    return all(_ok(a) for a in real)




def execute(url: str, mode: str = "text", selector: str = "") -> str:
    if not url.startswith(("http://", "https://")):
        return f"[抓取失败] URL 必须以 http:// 或 https:// 开头，当前值: {url}"
    if not _is_public_host(urlparse(url).hostname or ""):
        return f"[抓取失败] 只允许访问公网地址（内网 / 本机 / 元数据服务一律拒绝）: {url}"

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
        return f"[抓取失败] HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return f"[抓取失败] {e.reason}"
    except Exception as e:
        return f"[抓取失败] {e}"

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