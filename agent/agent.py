import json
import re

from agent.intent_keywords import select_tools_by_intent, TOOL_CATEGORIES
from agent.model_gateway import ModelGateway


SYSTEM_PROMPT = """你是一个能使用工具的助手，可以根据情况调用工具，获得足够信息后直接给出答案。

重要：请直接给出最终回答，不要输出思考过程、分析过程或任何前置说明。"""

SYSTEM_PROMPT_WITH_THOUGHT = """你是一个能使用工具的助手，可以根据情况调用工具，获得足够信息后直接给出答案。

重要：在给出最终回答之前，请用 <thinking>...</thinking> 标签包裹你的思考过程。
思考过程请用自然流畅的独白形式书写，像自己在心里默默分析一样，不要使用列表或标签格式。
应自然覆盖以下内容：先理解用户真正想问什么，然后把问题拆解成几个小步骤，
判断需要哪些知识或工具，一步步推理出结论，最后检查一下有没有遗漏，规划好怎么组织回答。

格式示例：
<thinking>
用户想知道北京未来三天天气，应该是为了出行做准备。要回答这个问题，我需要先查到北京的地理位置ID，然后调用天气预报接口获取未来3天的数据。拿到数据后按日期整理温度、天气状况和风力，最后给一个综合的出行建议。让我确认一下：数据要覆盖未来3天，温度单位是摄氏度，天气描述要清晰易懂。回答就按日期逐日列出，最后加一句出行提醒。
</thinking>

然后给出你的正式回答。
注意：<thinking> 标签内的内容是你的内部思考，标签外的内容才是给用户的正式回答。
每次回复中只能使用一次 <thinking> 标签，放在正式回答之前。"""


class SimpleAgent:
    """Agent 主循环类，处理多轮对话和工具调用"""

    def __init__(self, llm_client, tool_registry, context_limit='', show_thought=False,
                 skill_context="", silent=False):
        """初始化 SimpleAgent

        Args:
            llm_client: LLMClient 实例
            tool_registry: ToolRegistry 实例
            context_limit: 上下文限制，如 "32k"、"64k"、"128k"，为空则使用模型最大上下文
            show_thought: 是否显示思考过程
            skill_context: 技能上下文，注入系统提示词
            silent: 静默模式，不打印终端输出（用于子 Agent）
        """
        self.llm = llm_client
        self.tool_registry = tool_registry
        self.show_thought = show_thought
        self.context_limit = context_limit
        self.skill_context = skill_context
        self.silent = silent
        self.user_id = None  # 当前用户 ID，用于加载用户级配置
        self._system_prompt = SYSTEM_PROMPT
        self._context_limit_tokens = self._parse_context_limit(context_limit)
        self._gateway = ModelGateway(llm_client.model if hasattr(llm_client, 'model') else "")
        self.messages = []
        self._rebuild_system_message()

    @staticmethod
    def _parse_context_limit(limit_str: str) -> int:
        """解析上下文限制字符串为 token 数

        Args:
            limit_str: 如 "32k"、"64k"、"128k"，空字符串表示不限制

        Returns:
            int: token 数量，0 表示不限制
        """
        if not limit_str or not limit_str.strip():
            return 0
        limit_str = limit_str.strip().lower()
        if limit_str.endswith('k'):
            try:
                return int(float(limit_str[:-1]) * 1000)
            except ValueError:
                return 0
        try:
            return int(limit_str)
        except ValueError:
            return 0

    @staticmethod
    def _estimate_tokens(messages: list) -> int:
        """估算消息列表的 token 数量

        使用字符数/4 的粗略估算（1 token ≈ 4 英文字符）

        Args:
            messages: 消息列表

        Returns:
            int: 估算的 token 数
        """
        total = 0
        for msg in messages:
            content = msg.get('content', '') or ''
            total += len(content) // 4 + 1
        return total

    def _rebuild_system_message(self):
        """根据 show_thought 状态和网关能力重建系统消息"""
        if self.show_thought and self._gateway.needs_prompt_fallback:
            prompt = SYSTEM_PROMPT_WITH_THOUGHT
        else:
            prompt = self._system_prompt

        # 追加技能上下文
        if self.skill_context:
            prompt += "\n" + self.skill_context

        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})

    def set_show_thought(self, enabled: bool):
        """设置是否显示思考过程

        Args:
            enabled: True 开启，False 关闭
        """
        self.show_thought = enabled
        self._rebuild_system_message()

    def set_skill_context(self, context: str):
        """设置技能上下文

        Args:
            context: 技能上下文字符串
        """
        self.skill_context = context
        self._rebuild_system_message()

    def update_context_limit(self, context_limit: str):
        """更新上下文限制

        Args:
            context_limit: 如 "32k"、"64k"、"128k"，空字符串表示不限制
        """
        self.context_limit = context_limit
        self._context_limit_tokens = self._parse_context_limit(context_limit)

    def reset(self):
        """重置对话上下文"""
        self._rebuild_system_message()
        self.messages = [self.messages[0]] if self.messages else [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    def _trim_messages(self):
        """根据上下文 token 限制裁剪消息历史"""
        if self._context_limit_tokens == 0:
            return

        if len(self.messages) <= 1:
            return

        system_msg = self.messages[0]
        history_msgs = self.messages[1:]

        system_tokens = self._estimate_tokens([system_msg])
        available_tokens = self._context_limit_tokens - system_tokens
        if available_tokens <= 0:
            self.messages = [system_msg]
            return

        while history_msgs:
            estimated = self._estimate_tokens(history_msgs)
            if estimated <= available_tokens:
                break
            history_msgs = history_msgs[1:]

        self.messages = [system_msg] + history_msgs

    def _select_tools(self, user_input: str) -> list:
        """根据用户意图动态选择相关工具，减少 token 开销

        委托给 intent_keywords.select_tools_by_intent()，
        支持用户级关键词（加载自 data/intent_keywords/{user_id}.json）。

        Args:
            user_input: 用户输入文本

        Returns:
            list: 选中的工具 OpenAI spec 列表
        """
        return select_tools_by_intent(
            user_input,
            self.tool_registry.get_all_openai_specs(),
            self.user_id
        )

    @staticmethod
    def compress_messages(messages: list, llm_client, context_limit_tokens: int = 0) -> list:
        """压缩消息历史，将早期对话总结为摘要

        当消息的估算 token 数超过上下文限制的 70% 时触发压缩。
        保留最近 4 轮对话（8条消息），将更早的消息压缩为一条摘要。

        Args:
            messages: 消息列表（不含 system prompt）
            llm_client: LLMClient 实例，用于生成摘要
            context_limit_tokens: 上下文 token 限制，0 表示不限制

        Returns:
            list: 压缩后的消息列表
        """
        if context_limit_tokens == 0:
            return messages

        if len(messages) < 10:
            return messages

        estimated = SimpleAgent._estimate_tokens(messages)
        threshold = int(context_limit_tokens * 0.7)

        if estimated <= threshold:
            return messages

        keep_count = 8
        if len(messages) <= keep_count:
            return messages

        old_messages = messages[:-keep_count]
        recent_messages = messages[-keep_count:]

        summary_text = _generate_summary(old_messages, llm_client)
        compressed_metadata = _extract_compressed_metadata(old_messages)

        if summary_text:
            compressed = [{"role": "system", "content": f"[历史对话摘要] {summary_text}", "compressed_metadata": compressed_metadata}]
            compressed.extend(recent_messages)
            return compressed

        return recent_messages

    def run(self, user_input: str, max_iterations=10) -> str:
        """运行 Agent 主循环（单轮任务）

        Args:
            user_input (str): 用户输入
            max_iterations (int, optional): 最大迭代次数，默认为 10

        Returns:
            str: 最终回复
        """
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_input}
        ]

        tool_specs = self._select_tools(user_input)

        for _ in range(max_iterations):
            content = ""
            tool_calls = None

            for chunk in self.llm.chat_stream(messages, tools=tool_specs):
                if chunk.get("content"):
                    content += chunk["content"]
                    if not self.silent:
                        print(chunk["content"], end="", flush=True)

                if chunk.get("finish_reason"):
                    tool_calls = chunk.get("tool_calls")
                    content = chunk.get("full_content") or content

            if not tool_calls:
                if not self.silent:
                    print()
                return content

            if not self.silent:
                print()
            response = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls
            }
            messages.append(response)

            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_arguments = json.loads(tool_call["function"]["arguments"])

                if not self.silent:
                    print(f"[调用工具] {tool_name} 参数: {tool_arguments}")

                tool_result = self.tool_registry.execute(tool_name, tool_arguments)

                if not self.silent:
                    print(f"[工具结果] {tool_result}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_name,
                    "content": tool_result
                })

        return "已达到最大迭代次数，无法完成任务。"

    def chat(self, user_input: str, max_iterations=10) -> str:
        """多轮对话，保持上下文

        Args:
            user_input (str): 用户输入
            max_iterations (int, optional): 最大迭代次数，默认为 10

        Returns:
            str: 最终回复
        """
        self.messages.append({"role": "user", "content": user_input})

        self._trim_messages()

        tool_specs = self._select_tools(user_input)

        for _ in range(max_iterations):
            gw_cfg = self._gateway.build_params(self.show_thought, temperature=0)

            # 使用流式调用，实时打印 token
            content = ""
            reasoning_content = ""
            tool_calls = None
            first_token = True

            for chunk in self.llm.chat_stream(self.messages, tools=tool_specs, **gw_cfg["api_params"]):
                if chunk.get("reasoning_content"):
                    if first_token:
                        print("[思考] ", end="", flush=True)
                        first_token = False
                    rc = chunk["reasoning_content"]
                    reasoning_content += rc
                    print(rc, end="", flush=True)

                if chunk.get("content"):
                    c = chunk["content"]
                    # 思考结束后换行再输出回答
                    if reasoning_content and not content:
                        print("\n")
                    if first_token:
                        first_token = False
                    content += c
                    print(c, end="", flush=True)

                if chunk.get("finish_reason"):
                    tool_calls = chunk.get("tool_calls")
                    reasoning_content = chunk.get("full_reasoning_content") or reasoning_content
                    content = chunk.get("full_content") or content

            if content and not tool_calls:
                print()  # 换行
                response = {"role": "assistant", "content": content}
                if reasoning_content:
                    response["reasoning_content"] = reasoning_content
                self.messages.append(response)
                return content

            if not tool_calls:
                print()
                return content or ""

            # 有工具调用
            print()  # 换行
            response = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls
            }
            if reasoning_content:
                response["reasoning_content"] = reasoning_content
            self.messages.append(response)

            for tool_call in tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_arguments = json.loads(tool_call["function"]["arguments"])

                print(f"[调用工具] {tool_name} 参数: {tool_arguments}")

                tool_result = self.tool_registry.execute(tool_name, tool_arguments)

                print(f"[工具结果] {tool_result}")

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_name,
                    "content": tool_result
                })

        return "已达到最大迭代次数，无法完成任务。"


def _extract_compressed_metadata(messages: list) -> dict:
    """从被压缩的旧消息中提取思考过程、工具调用和搜索元数据

    将被压缩的消息中的 thought/tools/search 字段提取出来，
    存储到压缩消息的 compressed_metadata 字段中，供历史记录渲染使用。

    Args:
        messages: 被压缩的消息列表（role: user/assistant）

    Returns:
        dict: {"rounds": [{"thought": ..., "tools": [...], "search": {...}}, ...]}
    """
    rounds = []
    i = 0
    while i < len(messages):
        if messages[i].get("role") == "user":
            user_msg = messages[i]
            assistant_msg = messages[i + 1] if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant" else None
            round_data = {"user": user_msg.get("content", "")[:100]}
            if assistant_msg:
                meta = {}
                if assistant_msg.get("thought"):
                    meta["thought"] = assistant_msg["thought"]
                if assistant_msg.get("tools"):
                    meta["tools"] = assistant_msg["tools"]
                if assistant_msg.get("search"):
                    meta["search"] = assistant_msg["search"]
                if meta:
                    round_data["meta"] = meta
            if "meta" in round_data:
                rounds.append(round_data)
            i += 2 if assistant_msg else 1
        else:
            # 跳过非user开头的消息（如system summary）
            i += 1
    return {"rounds": rounds} if rounds else {}


def _generate_summary(messages: list, llm_client) -> str:
    """使用 LLM 生成对话摘要"""
    conversation_text = ""
    for msg in messages:
        role = "用户" if msg["role"] == "user" else "助手"
        content = msg.get("content", "") or ""
        if len(content) > 500:
            content = content[:500] + "..."
        conversation_text += f"{role}: {content}\n"

    summary_prompt = (
        "请用一段简洁的文字（不超过200字）总结以下对话的核心内容和关键信息，"
        "包括用户的主要问题和助手给出的重要结论：\n\n"
        f"{conversation_text}"
    )

    try:
        response = llm_client.chat(
            [{"role": "user", "content": summary_prompt}],
            tools=None
        )
        return response.get("content", "").strip()
    except Exception:
        return ""


if __name__ == "__main__":
    pass
