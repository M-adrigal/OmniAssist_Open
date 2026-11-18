"""DeepSeek 官方联网搜索 / 网页抓取客户端。

接口 2026-10-17 上线，规格见 https://api-docs.deepseek.com/zh-cn/guides/search
  POST https://api.deepseek.com/v1/tools/search
  POST https://api.deepseek.com/v1/tools/fetch

要点（改代码前先读）：
- **与 DeepSeek 模型 API 同一把 Bearer key**，不是另一套凭据。
- 计费按**成功请求次数**：¥5 / 1000 次。Search 必须返回非空 results 才计费，
  Fetch 必须取到正文才计费。失败 / 无结果 / 超时都不计费 → 所以「搜不到就返回空」
  在成本上是安全的，不需要为了省钱做额外收敛。
- 限速按**账号**：Search 100 QPS、Fetch 25 QPS，超了回 429。
- 只用标准库（urllib），不新增依赖；`transport` 可注入，测试不联网。
"""
import json
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

# 错误码 → 给用户看的中文说明（4xx 里只有 408/429 值得重试）
_ERROR_HINTS = {
    400: "请求不合法（检查 query 长度 1~128、count 在 1~50）",
    401: "DeepSeek API Key 缺失或无效",
    402: "DeepSeek 账号余额不足",
    403: "抓取被目标站点安全策略拒绝",
    404: "未能取到可用网页正文（站点拦截 / 内容类型不支持 / 无可提取正文）",
    408: "读取请求超时，可重试",
    413: "请求体超过大小限制",
    429: "超过 QPS 限制，稍后重试",
    500: "DeepSeek 服务内部错误",
    504: "下游请求超时",
}


class SearchError(Exception):
    """DeepSeek 搜索/抓取调用失败。`status` 是 HTTP 状态码（网络层失败时为 None）。"""

    def __init__(self, status, message, code=""):
        self.status = status
        self.code = code
        super().__init__(message)

    @property
    def retryable(self):
        """408 / 429 / 5xx 才值得重试；其它 4xx 重试也没用。"""
        return self.status in (408, 429) or (self.status is not None and self.status >= 500)


def _default_transport(url, payload, api_key, timeout):
    """真实 HTTP 调用。测试里换成桩，避免联网。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # DeepSeek 用 OpenAI 兼容的错误体：{"error": {"message","type","code"}}
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
            err = json.loads(body).get("error") or {}
            message = err.get("message") or ""
            code = err.get("code") or ""
        except Exception:
            message, code = body[:200], ""
        hint = _ERROR_HINTS.get(e.code, "")
        detail = f"{hint}" if hint else (message or f"HTTP {e.code}")
        if message and hint:
            detail = f"{hint}（{message}）"
        raise SearchError(e.code, detail, code) from None
    except urllib.error.URLError as e:
        raise SearchError(None, f"网络不可达：{e.reason}") from None
    except json.JSONDecodeError:
        raise SearchError(None, "返回内容不是合法 JSON") from None


def search(query, api_key, count=10, need_content=False, ori_question=None,
           localization="", timeout=20, base=DEFAULT_BASE, transport=None):
    """按 query 搜索网页。

    Args:
        query: 搜索词，1~128 字符。支持 site: / -site: / "精确短语" / lang: / after: 等语法。
        api_key: DeepSeek API Key。
        count: 返回条数 1~50，默认 10。只影响条数，不影响计费。
        need_content: 是否要网页正文（content 字段）。正文很占 token，默认不要。
        ori_question: 用户的原始问题，有助于理解搜索意图，可省。
        localization: 用于优先返回本地化结果的 IP，留空即可。
        timeout: 超时秒数。
        base: 便于自建代理 / 测试替换。
        transport: 注入的传输函数。

    Returns:
        dict: 原始响应，`results` 为结果列表（可能为空数组）。
    """
    payload = {"query": query, "count": int(count), "need_content": bool(need_content)}
    if ori_question:
        payload["ori_question"] = ori_question
    if localization:
        payload["localization"] = localization
    fn = transport or _default_transport
    data = fn(f"{base}{SEARCH_PATH}", payload, api_key, timeout)
    if not isinstance(data, dict):
        raise SearchError(None, "返回结构异常（不是对象）")
    return data


def fetch(url, api_key, timeout=20, base=DEFAULT_BASE, transport=None):
    """抓取单个网页，返回 {title, content}，content 是 Markdown。

    Args:
        url: 必须是含 host 的 http/https URL，不能带用户名密码。
        api_key: DeepSeek API Key。

    Returns:
        dict: {"title": str, "content": str}
    """
    fn = transport or _default_transport
    data = fn(f"{base}{FETCH_PATH}", {"url": url}, api_key, timeout)
    if not isinstance(data, dict):
        raise SearchError(None, "返回结构异常（不是对象）")
    return {"title": data.get("title") or "", "content": data.get("content") or ""}


def format_results(results, snippet_limit=300):
    """把搜索结果拼成给模型读的文本块。

    格式与原先 Tavily 的输出保持一致（编号 / 标题 / 来源 / 摘要），
    因为下游 system prompt 里已经写死了「回答末尾用『信息来源：』列出来源」，
    换搜索引擎不该让模型看到另一种结构。

    Args:
        results: search() 返回的 results 列表。
        snippet_limit: 单条摘要截断长度，防止 50 条正文把上下文吃光。

    Returns:
        str: 拼接好的文本；无结果时为空串。
    """
    if not results:
        return ""
    parts = []
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "无标题").strip()
        url = r.get("url") or ""
        # summary 是给模型读的长摘要（600~800 字），snippet 只有 100~200 字；
        # need_content=True 时 content 更长，但那是有意要的，不截断。
        text = (r.get("summary") or r.get("snippet") or r.get("content") or "").strip()
        if snippet_limit and len(text) > snippet_limit:
            text = text[:snippet_limit] + "..."
        authority = r.get("authority")
        date = (r.get("date") or "")[:10]
        meta = " · ".join(
            x for x in (
                r.get("site_name") or "",
                date,
                f"权威性 {authority}" if isinstance(authority, int) and authority >= 3 else "",
            ) if x
        )
        head = f"{i}. {title}"
        if meta:
            head += f"（{meta}）"
        parts.append(f"{head}\n   来源: {url}\n   {text}")
    return "\n\n".join(parts)
