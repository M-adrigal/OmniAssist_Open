#!/usr/bin/env python3
"""网页抓取工具的 SSRF 守卫回归测试（零依赖，不联网）。

背景：`fetch_url` 在沙箱里被「按工具名放行网络」（net_allow="*"），
意味着它能访问任意主机 —— 包括 127.0.0.1、内网段、以及云厂商元数据服务
169.254.169.254。那不是"抓网页"，是把沙箱变成内网探测器。

判据：只允许公网目标；域名解析出的 IP 也要判（防"域名指向内网"绕过）。

用法：python3 scripts/test_fetch_guard.py
"""
import importlib.util
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "fetch_url", ROOT / "agent" / "skills" / "web-fetch" / "scripts" / "fetch_url.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    print("\n[1] 内网 / 本机 / 元数据服务一律拒绝")
    for host in ("127.0.0.1", "127.0.0.2", "10.1.2.3", "172.16.0.9",
                 "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"):
        check(f"{host} 被拒", fx._is_public_host(host) is False, fx._is_public_host(host))

    print("\n[2] 公网地址放行")
    for host in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
        check(f"{host} 放行", fx._is_public_host(host) is True, fx._is_public_host(host))

    print("\n[3] 空值 / 非法值不放过")
    check("空字符串被拒", fx._is_public_host("") is False)
    check("None 被拒", fx._is_public_host(None) is False)
    # 注意：本机开 fake-ip 代理时，**任何域名**（含不存在的）都会被解析成
    # 198.18.x.x，DNS 层无法区分真假。这种情况下会放行（见 [5] 的说明），
    # 因此这条只在没有 fake-ip 干扰时才断言。
    if not fx._is_fake_ip((socket.getaddrinfo("no-such-host.invalid", None)[0][4][0])):
        check("解析不了的域名被拒（宁可抓不到，不可猜着放行）",
              fx._is_public_host("no-such-host.invalid") is False)
    else:
        print("  - 跳过「解析不了的域名被拒」：本机 fake-ip 代理会把不存在的域名也解析成功，"
              "该场景下无法在 DNS 层判定（已记录为已知局限，见 _is_public_host 注释）")

    print("\n[4] 域名要走解析结果，不能因为「不是 IP」就放行")
    _orig = socket.getaddrinfo
    try:
        socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))]
        check("域名解析到内网 → 拒绝", fx._is_public_host("evil.example") is False)
        socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
        check("域名解析到公网 → 放行", fx._is_public_host("good.example") is True)
        socket.getaddrinfo = lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 0)), (2, 1, 6, "", ("127.0.0.1", 0))]
        check("多记录里有一条内网 → 也拒绝（防轮询绕过）",
              fx._is_public_host("mixed.example") is False)
    finally:
        socket.getaddrinfo = _orig

    print("\n[5] 代理 fake-ip 下必须仍能抓公网（2026-10-28 事故）")
    # Clash / Surge / Mihomo 开 fake-ip 时，DNS 会把**所有域名**解析到
    # 198.18.0.0/15（RFC 2544 保留段），真连接由 TUN 转发。
    # 原实现把它当私有地址 → 只要本机开着代理，example.com 都报
    # "只允许访问公网地址"，工具等于全程不可用。
    # 这个用例之前缺失，是因为 [2][4] 都用字面量 IP 或伪造的干净解析结果，
    # 永远走不到 fake-ip 这条路 —— 真实的解析形态没被覆盖到。
    _orig2 = socket.getaddrinfo
    try:
        # 真实形态：fake-ip A 记录 + ISATAP 形式的 AAAA（ipv4_mapped 为 None）
        socket.getaddrinfo = lambda *a, **k: [
            (2, 1, 6, "", ("198.18.0.221", 0)),
            (10, 1, 6, "", ("::ffff:0:c612:dd", 0)),
        ]
        check("整组都是 fake-ip → 放行（真连接由代理转发）",
              fx._is_public_host("example.com") is True, fx._is_public_host("example.com"))

        # fake-ip 混真实内网 → 不能因为"有 fake-ip"就整体放过
        socket.getaddrinfo = lambda *a, **k: [
            (2, 1, 6, "", ("198.18.0.221", 0)),
            (2, 1, 6, "", ("10.0.0.5", 0)),
        ]
        check("fake-ip 混真实内网 → 拒绝",
              fx._is_public_host("mixed2.example") is False, fx._is_public_host("mixed2.example"))

        # fake-ip 混真实公网 → 放行
        socket.getaddrinfo = lambda *a, **k: [
            (2, 1, 6, "", ("198.18.0.221", 0)),
            (2, 1, 6, "", ("93.184.216.34", 0)),
        ]
        check("fake-ip 混真实公网 → 放行",
              fx._is_public_host("mixed3.example") is True, fx._is_public_host("mixed3.example"))
    finally:
        socket.getaddrinfo = _orig2

    print("\n[6] 用户直接写 fake-ip 字面量不等同于公网")
    # 直接请求 198.18.x.x 是在探测这个保留段本身，必须拒绝；
    # 与"域名被代理解析成 fake-ip"区分开。
    for h in ("198.18.0.5", "198.19.255.1"):
        check(f"{h} 字面量被拒", fx._is_public_host(h) is False, fx._is_public_host(h))

    print("\n[7] execute 层真的会拦")
    out = fx.execute("http://169.254.169.254/latest/meta-data/")
    check("元数据服务地址被 execute 拒掉", "[抓取失败]" in out, out[:80])
    out2 = fx.execute("file:///etc/passwd")
    check("非 http(s) 协议被拒", "[抓取失败]" in out2, out2[:80])

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
