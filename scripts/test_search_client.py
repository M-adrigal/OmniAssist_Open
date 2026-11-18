#!/usr/bin/env python3
"""DeepSeek 搜索 / 抓取客户端回归测试（零依赖，不联网）。

覆盖：请求体构造、鉴权头、结果格式化、错误码 → 人话映射、可重试判定。
真实 HTTP 由 transport 注入替换，所以不需要 API Key，也不花钱。

用法：python3 scripts/test_search_client.py
"""
import io
import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 直接按路径加载，绕开 server/__init__.py（它会 import main → 需要 fastapi）。
# 这样本测试保持零依赖，任何解释器都能跑。
_spec = importlib.util.spec_from_file_location("search_client", ROOT / "server" / "search_client.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    def stub(payload_box, reply):
        """造一个捕获请求体的传输函数。"""
        def _fn(url, payload, api_key, timeout):
            payload_box.update({"url": url, "payload": payload, "key": api_key, "timeout": timeout})
            return reply
        return _fn

    def http_error_transport(code, body):
        def _fn(url, payload, api_key, timeout):
            raise urllib.error.HTTPError(
                url, code, "err", {}, io.BytesIO(body.encode("utf-8"))
            )
        return _fn

    print("\n[1] 请求构造")
    box = {}
    reply = {"results": [{
        "title": "DeepSeek V4 发布", "url": "https://e.com/a",
        "snippet": "短摘要", "summary": "摘" * 600,
        "content": "", "site_name": "Example", "date": "2026-06-01T00:00:00+00:00",
        "authority": 3,
    }]}
    sc.search("豆包手机 测评", "sk-test", count=5, need_content=True,
              ori_question="最近有什么豆包手机测评", transport=stub(box, reply))
    check("URL 指向 /v1/tools/search", box["url"].endswith(sc.SEARCH_PATH), box.get("url"))
    check("Bearer key 透传", box["key"] == "sk-test")
    check("query 原样传递", box["payload"]["query"] == "豆包手机 测评")
    check("count 透传", box["payload"]["count"] == 5)
    check("need_content 透传", box["payload"]["need_content"] is True)
    check("ori_question 透传", box["payload"]["ori_question"].startswith("最近"))

    print("\n[2] 可选参数不该凭空出现")
    box2 = {}
    sc.search("只给 query", "sk", transport=stub(box2, {"results": []}))
    check("未传 ori_question 时请求体里没有该字段", "ori_question" not in box2["payload"], box2["payload"])
    check("未传 localization 时请求体里没有该字段", "localization" not in box2["payload"])
    check("默认 count=10", box2["payload"]["count"] == 10)
    check("默认 need_content=False", box2["payload"]["need_content"] is False)

    print("\n[3] 结果格式化（下游 prompt 依赖这个结构）")
    text = sc.format_results(reply["results"])
    check("含编号与标题", "1. DeepSeek V4 发布" in text, text[:80])
    check("含来源链接", "https://e.com/a" in text)
    check("含站点与权威性标记", "Example" in text and "权威性 3" in text)
    check("长摘要被截断到 300 字以内", "摘" * 301 not in text and "..." in text, text[-40:])
    check("空结果 → 空串", sc.format_results([]) == "")
    check("summary 缺失时退回 snippet",
          "短摘要" in sc.format_results([{"title": "t", "url": "u", "snippet": "短摘要"}]))

    print("\n[4] 错误码 → 人话 + 是否重试")
    # 走真实传输层（桩掉 urlopen），这样测的是 _default_transport 里的映射逻辑，
    # 而不是测试自己编的错误处理。
    cases = [
        (402, "余额不足", False),
        (401, "无效", False),
        (429, "QPS", True),
        (504, "超时", True),
        (404, "正文", False),
    ]

    def _raise(code, body):
        def _f(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body.encode()))
        return _f

    _orig_err = urllib.request.urlopen
    try:
        for code, kw, retry in cases:
            urllib.request.urlopen = _raise(
                code, json.dumps({"error": {"message": "raw", "code": "x"}}))
            try:
                sc.search("q", "sk")
                check(f"{code} 应抛异常", False)
            except sc.SearchError as e:
                check(f"{code} → 提示含「{kw}」", kw in str(e), str(e))
                check(f"{code} 是否可重试 = {retry}", e.retryable == retry, e.retryable)
                check(f"{code} 带上了 HTTP 状态码", e.status == code, e.status)
    finally:
        urllib.request.urlopen = _orig_err

    print("\n[5] 抓取接口")
    fbox = {}
    fout = sc.fetch("https://e.com/a", "sk", transport=stub(fbox, {"title": "标题", "content": "# 正文"}))
    check("URL 指向 /v1/tools/fetch", fbox["url"].endswith(sc.FETCH_PATH), fbox.get("url"))
    check("请求体只有 url", set(fbox["payload"]) == {"url"}, fbox["payload"])
    check("返回 title/content", fout == {"title": "标题", "content": "# 正文"}, fout)
    fout2 = sc.fetch("https://e.com/a", "sk", transport=stub({}, {"content": "只有正文"}))
    check("title 缺失时给空串而不是 None", fout2["title"] == "", fout2)

    print("\n[6] 真实传输层：鉴权头与 JSON 体（不联网，桩掉 urlopen）")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return json.dumps({"results": []}).encode()

    _orig = urllib.request.urlopen
    try:
        def _fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode())
            captured["url"] = req.full_url
            return _Resp()
        urllib.request.urlopen = _fake_urlopen
        sc.search("q", "sk-abc", count=3)
    finally:
        urllib.request.urlopen = _orig
    hdr = {k.lower(): v for k, v in captured["headers"].items()}
    check("POST 方法", captured["method"] == "POST", captured["method"])
    check("Authorization 是 Bearer sk-abc", hdr.get("authorization") == "Bearer sk-abc", hdr.get("authorization"))
    check("Content-Type 是 application/json", hdr.get("content-type") == "application/json", hdr)
    check("base URL 正确", captured["url"] == "https://api.deepseek.com/v1/tools/search", captured["url"])
    check("body 是合法 JSON 且含 query", captured["body"].get("query") == "q", captured["body"])

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
