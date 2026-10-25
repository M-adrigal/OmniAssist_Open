import contextlib
import json
import threading
import time

from openai import OpenAI
from agent.logger import get_logger

logger = get_logger("llm")


# 是否请求上游在流式响应中回传 usage（OpenAI 协议扩展参数）。
# 已被火山方舟实测支持；个别网关不认识该参数时会自动降级重试一次。
_STREAM_INCLUDE_USAGE = True


# ==================== 用量归因的“作用域” ====================
# 全局 LLM 客户端（服务启动时创建的那个）被子 Agent、技能模拟执行器、HTTP 结果整理
# 共用。它没有 per-request 的 usage_sink，导致这些调用的 token 被直接丢弃。
# 这里用一个「线程本地」的作用域把当前请求的 sink 传下去：
#   工具执行线程 → （子 Agent / 技能执行器） → 全局客户端._report_usage → 读到本线程的 sink
# 用线程本地而不是实例属性，是为了让并发用户的归因互不串号（各在自己线程里）。
_usage_local = threading.local()


def current_usage_sink():
    """返回当前线程绑定的用量回传钩子（无则 None）。"""
    return getattr(_usage_local, "sink", None)


@contextlib.contextmanager
def usage_scope(sink):
    """在 with 块内把 sink 绑定到当前线程；退出时恢复原值（支持嵌套）。"""
    prev = getattr(_usage_local, "sink", None)
    _usage_local.sink = sink
    try:
        yield
    finally:
        _usage_local.sink = prev


def _get_any(obj, *names):
    """从对象或字典中按候选名取值（兼容 SDK 对象 / raw dict 两种形态）。"""
    if obj is None:
        return None
    for n in names:
        val = getattr(obj, n, None)
        if val is not None:
            return val
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return None


def extract_usage(u) -> dict | None:
    """把上游返回的 usage 规范化为统一字段。

    返回 None 表示上游未回传 usage（此时调用方应使用估算兜底）。
    字段含义：
      prompt_tokens     输入 token 数
      completion_tokens 输出 token 数（含推理模型的思考 token）
      cached_tokens     命中前缀缓存的输入 token 数（单独计价，通常打折）
      reasoning_tokens  思考 token 数（是 completion 的子集，不重复相加）
    """
    if u is None:
        return None
    prompt = _get_any(u, "prompt_tokens") or 0
    completion = _get_any(u, "completion_tokens") or 0
    total = _get_any(u, "total_tokens") or (prompt + completion)
    cached = _get_any(_get_any(u, "prompt_tokens_details"), "cached_tokens") or 0
    reasoning = _get_any(_get_any(u, "completion_tokens_details"), "reasoning_tokens") or 0
    try:
        return {
            "prompt_tokens": int(prompt),
            "completion_tokens": int(completion),
            "total_tokens": int(total),
            "cached_tokens": int(cached),
            "reasoning_tokens": int(reasoning),
        }
    except (TypeError, ValueError):
        return None


def estimate_tokens(text: str) -> int:
    """无 usage 时的兜底估算（CJK 感知）。

    说明：这不是精确值，仅用于上游不回传 usage 或流式中断时避免「记 0」。
    经验系数：中文约 1 字 1 token，英文/符号约 4 字符 1 token。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff":
            cjk += 1
    other = len(text) - cjk
    return int(cjk + other * 0.25) + 1


# 每张图片的视觉 token 预算。
#
# 多模态请求里图片不是文本，不能按字符折算 —— 服务端对图片有独立的计费口径：
# 按若干档缩放下采样后切成 image patch，每个 patch 占固定 token。
# OpenAI 兼容视觉接口的常见上限是 1024 个视觉 token/张（detail=high / auto），
# 降到 512×512（detail=low）时约 256/张。DeepSeek V4.1 Flash 用的也是这套数量级。
#
# 注意：这只是「估算轨」的兜底预算。**精确轨不需要它** —— 只要上游回传 usage，
# 图片消耗已经算在 prompt_tokens 里了，我们不需要也不应该自己再猜一遍。
IMAGE_TOKENS_PER_IMAGE = 1024
IMAGE_TOKENS_LOW_DETAIL = 256


def estimate_image_tokens(count: int, detail: str = "high") -> int:
    """按图片数量估算视觉 token。`detail=low` 走 512×512 的低档预算。"""
    unit = IMAGE_TOKENS_LOW_DETAIL if (detail or "").lower() == "low" else IMAGE_TOKENS_PER_IMAGE
    return max(0, int(count or 0)) * unit


def _analyze_messages(messages, tools=None) -> tuple[str, int]:
    """把请求体拆成「可估算的文本」和「图片数量」两部分。

    为什么必须拆开：`image_url` 段是一整串 base64，既不能按字符估算（会把 snapshot
    记成一个天文数字），也不能直接丢弃（会像改造前那样整块漏计）。
    正解是单独按张数计固定预算 —— 见 IMAGE_TOKENS_PER_IMAGE。

    Returns:
        (text, image_count)
    """
    parts = []
    images = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for seg in c:
                if not isinstance(seg, dict):
                    continue
                if isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
                elif seg.get("type") == "image_url":
                    images += 1
        tc = m.get("tool_calls")
        if tc:
            try:
                parts.append(json.dumps(tc, ensure_ascii=False))
            except (TypeError, ValueError):
                pass
    if tools:
        try:
            parts.append(json.dumps(tools, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return "\n".join(parts), images


def _messages_text(messages, tools=None) -> str:
    """把请求体压成纯文本，仅用于估算输入 token（不含图片，图片另算）。"""
    return _analyze_messages(messages, tools)[0]


class LLMClient:
    """大模型调用客户端，封装 OpenAI API 调用"""

    # 用量回传钩子：签名 sink(payload: dict) -> None。
    # 由调用方按需赋值（通常每次请求新建一个 client，故不会跨请求串味）。
    usage_sink = None

    def __init__(self, model=None, config=None, api_key=None, base_url=None):
        """初始化 LLMClient

        所有配置仅从 config 文件读取，不使用环境变量。
        若直接传入 api_key/base_url，则优先使用传入值。

        Args:
            model: 模型名称，为 None 时从 config 读取
            config: AgentConfig 实例，为 None 时使用默认值（无 API Key）
            api_key: 直接传入的 API Key，优先级高于 config
            base_url: 直接传入的 Base URL，优先级高于 config
        """
        _api_key = ""
        _base_url = ""
        model_name = ""

        if config is not None:
            _api_key = config.get_api_key()
            _base_url = config.get('base_url') or ""
            cfg_model = config.get('model_name')
            model_name = cfg_model if cfg_model and cfg_model != '(未设置)' else ""

        if api_key is not None:
            _api_key = api_key
        if base_url is not None:
            _base_url = base_url

        self._config = config
        self._api_key = _api_key
        self._base_url = _base_url

        if model is not None:
            model_name = model

        if _api_key:
            self.client = OpenAI(
                api_key=_api_key,
                base_url=_base_url
            )
        else:
            self.client = None
        self.model = model_name

    def refresh(self):
        """配置变更后刷新客户端，重新从 config 读取 API Key 等信息"""
        _api_key = self._api_key
        _base_url = self._base_url
        model_name = self.model

        if self._config is not None:
            cfg_api_key = self._config.get_api_key()
            if cfg_api_key:
                _api_key = cfg_api_key
            cfg_base_url = self._config.get('base_url') or ""
            if cfg_base_url:
                _base_url = cfg_base_url
            cfg_model = self._config.get('model_name')
            if cfg_model and cfg_model != '(未设置)':
                model_name = cfg_model

        if _api_key:
            self.client = OpenAI(
                api_key=_api_key,
                base_url=_base_url
            )
        else:
            self.client = None
        self.model = model_name

    def _report_usage(self, usage, source, duration_ms, prompt_text="", reply_text="",
                     image_count=0):
        """把一次调用的用量交给 usage_sink（无 sink 时直接返回，零开销）。

        上游未回传 usage 时用字符估算兜底，并标记 is_estimated=1 —— 绝不能静默记 0，
        否则「账单对不上」且无从判断是「没花钱」还是「没采到」。

        prompt_text / reply_text / image_count **无论走哪条轨都要传**：它们不参与
        精确轨的计算，但会被落进 prompt_chars / image_count，用于日后拿「精确行」
        当样本回归出本项目自己的折算系数（见 scripts/calibrate_tokens.py --fit）。
        只存数值不存原文 —— 原文可能含密钥，落库等于开了一扇侧窗。

        取值优先级：当前线程的作用域 sink（更窄，代表本次工具执行所属的请求）
        → 客户端实例自带的 sink（per-request 新建的对话客户端）。
        """
        sink = current_usage_sink() or getattr(self, "usage_sink", None)
        if sink is None:
            return
        payload = {
            "source": source or "chat",
            "model": self.model or "",
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
            "is_estimated": 0,
            # 文本长度与图片数：精确轨也记录，用作校准样本的特征值
            "prompt_chars": len(prompt_text or ""),
            "completion_chars": len(reply_text or ""),
            "image_count": int(image_count or 0),
        }
        data = extract_usage(usage)
        if data is None:
            prompt_est = estimate_tokens(prompt_text) + estimate_image_tokens(image_count)
            completion_est = estimate_tokens(reply_text)
            payload.update({
                "prompt_tokens": prompt_est,
                "completion_tokens": completion_est,
                "total_tokens": prompt_est + completion_est,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "is_estimated": 1,
            })
        else:
            payload.update(data)
        try:
            sink(payload)
        except Exception as e:  # 埋点绝不能影响主流程
            logger.debug(f"usage_sink 调用失败（已忽略）: {e}")

    def chat(self, messages, tools=None, tool_choice="auto", temperature=0,
             extra_body=None, reasoning_effort=None, source="chat"):
        """调用大模型聊天接口（非流式）

        Args:
            messages (list): 消息列表
            tools (list, optional): 工具列表
            tool_choice (str, optional): 工具选择策略，默认为 "auto"
            temperature (float, optional): 温度参数，默认为 0
            extra_body (dict, optional): 额外参数，通过 extra_body 透传
            reasoning_effort (str, optional): 推理强度，如 "minimal"/"medium"/"high"
            source (str, optional): 用量归因来源（chat/title/compress/subagent），仅用于统计

        Returns:
            dict: 标准化的响应消息
        """
        if self.client is None:
            raise RuntimeError("LLM 客户端未配置 API Key，请先在设置中配置模型参数")

        create_kwargs = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
        }
        if reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = reasoning_effort
        if extra_body is not None:
            create_kwargs["extra_body"] = extra_body

        import time as _time
        _start = _time.time()
        response = self.client.chat.completions.create(**create_kwargs)
        _elapsed = _time.time() - _start

        msg = response.choices[0].message
        result = {"role": "assistant", "content": msg.content}
        # 记录 API 调用信息
        token_info = ""
        if hasattr(response, 'usage') and response.usage:
            token_info = f", tokens={response.usage.prompt_tokens}(入)+{response.usage.completion_tokens}(出)"
        logger.info(f"API 请求: model={self.model}, messages={len(messages)}, tools={len(tools) if tools else 0}, temperature={temperature}, 耗时 {_elapsed:.1f}s{token_info}")

        # 图片单独拆出来：multimodal content 里的 image_url 段要按「每张固定预算」计，
        # 混进文本估算会把一整张图记成几十 token（漏计）或按 base64 长度记成天文数字。
        _prompt_text, _image_count = _analyze_messages(messages, tools)
        self._report_usage(
            getattr(response, "usage", None), source, _elapsed * 1000,
            prompt_text=_prompt_text, image_count=_image_count,
            reply_text=(msg.content or "") + (getattr(msg, "reasoning_content", "") or ""),
        )

        if hasattr(msg, "reasoning_content") and msg.reasoning_content:
            result["reasoning_content"] = msg.reasoning_content

        if msg.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                } for tc in msg.tool_calls
            ]
        return result

    def chat_stream(self, messages, tools=None, tool_choice="auto", temperature=0,
                 extra_body=None, reasoning_effort=None, source="chat"):
        """调用大模型聊天接口（流式），逐块返回内容，同时累积工具调用

        Args:
            messages (list): 消息列表
            tools (list, optional): 工具列表
            tool_choice (str, optional): 工具选择策略，默认为 "auto"
            temperature (float, optional): 温度参数，默认为 0
            extra_body (dict, optional): 额外参数，通过 extra_body 透传
            reasoning_effort (str, optional): 推理强度，如 "minimal"/"medium"/"high"
            source (str, optional): 用量归因来源（chat/title/compress/subagent），仅用于统计

        Yields:
            dict: {"content": "文本块"} 或 {"reasoning_content": "推理块"} 流式内容，
                  流结束时 yield {"content": None, "finish_reason": str, "tool_calls": list,
                                 "usage": {...} | None}
        """
        if self.client is None:
            raise RuntimeError("LLM 客户端未配置 API Key，请先在设置中配置模型参数")

        create_kwargs = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
            "stream": True,
            "timeout": 180,  # 防止上游无响应时无限挂起；超时将由调用方重试/降级处理
        }
        if reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = reasoning_effort
        if extra_body is not None:
            create_kwargs["extra_body"] = extra_body
        if _STREAM_INCLUDE_USAGE:
            create_kwargs["stream_options"] = {"include_usage": True}

        logger.info(f"API 流式请求: model={self.model}, messages={len(messages)}, tools={len(tools) if tools else 0}, temperature={temperature}")

        _start = time.time()
        try:
            stream = self.client.chat.completions.create(**create_kwargs)
        except Exception as e:
            # 个别网关不认识 stream_options（OpenAI 扩展参数）会直接 400。
            # 这种情况降级为不取 usage 的请求，保证对话本身不受影响（用量走估算兜底）。
            if _STREAM_INCLUDE_USAGE and "stream_options" in create_kwargs:
                logger.warning(f"上游拒绝 stream_options，降级为不含 usage 的流式请求: {e}")
                create_kwargs.pop("stream_options", None)
                stream = self.client.chat.completions.create(**create_kwargs)
            else:
                raise

        content = ""
        reasoning_content = ""
        tool_calls = []
        finish_reason = None
        usage = None          # 上游回传的精确用量（末块 choices 为空时携带）
        _reported = False

        _prompt_text, _image_count = _analyze_messages(messages, tools)

        def _finalize():
            """统一上报一次用量；提前关闭（客户端断开）时按已产出内容估算。"""
            nonlocal _reported
            if _reported:
                return
            _reported = True
            # 图片数量在请求发出时就已知，故这里沿用同一个计数值，
            # 保证「正常结束」与「客户端断连」两条路径算出来的图片预算一致。
            self._report_usage(
                usage, source, (time.time() - _start) * 1000,
                prompt_text=_prompt_text, image_count=_image_count,
                reply_text=content + reasoning_content,
            )

        try:
            _after_finish = 0
            for chunk in stream:
                # usage 出现在最后一个 choices 为空的块里，必须先取再跳过
                _u = getattr(chunk, "usage", None)
                if _u is not None:
                    usage = _u
                if finish_reason:
                    # 已收到结束标记：只再等很少几块把 usage 取回来。
                    # 之所以不一直读到流关闭，是防止个别网关结束标记后不关连接导致挂起。
                    _after_finish += 1
                    if usage is not None or _after_finish > 3:
                        break
                    continue
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_text = delta.reasoning_content
                    reasoning_content += reasoning_text
                    yield {"reasoning_content": reasoning_text}

                if delta.content:
                    content += delta.content
                    yield {"content": delta.content}

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        while len(tool_calls) <= idx:
                            tool_calls.append({
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            })
                        if tc_delta.id:
                            tool_calls[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tool_calls[idx]["function"]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tool_calls[idx]["function"]["arguments"] += tc_delta.function.arguments

                finish_reason = chunk.choices[0].finish_reason

            # 流自然结束：此时 usage 已到齐
            _finalize()
            yield {
                "content": None,
                "finish_reason": finish_reason or "stop",
                "tool_calls": tool_calls if tool_calls else None,
                "full_content": content,
                "full_reasoning_content": reasoning_content if reasoning_content else None,
                "usage": extract_usage(usage),
            }
        finally:
            # 确保 OpenAI 流式连接被关闭, 避免因调用方提前 break 导致连接泄漏
            if hasattr(stream, 'close'):
                try:
                    stream.close()
                except Exception:
                    pass
            # 兜底上报：覆盖「调用方提前 break / 客户端断连导致生成器被关闭」的情形
            try:
                _finalize()
            except Exception:
                pass


if __name__ == "__main__":
    pass
