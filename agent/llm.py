from openai import OpenAI


class LLMClient:
    """大模型调用客户端，封装 OpenAI API 调用"""

    def __init__(self, model=None, config=None):
        """初始化 LLMClient

        所有配置仅从 config 文件读取，不使用环境变量。

        Args:
            model: 模型名称，为 None 时从 config 读取
            config: AgentConfig 实例，为 None 时使用默认值（无 API Key）
        """
        api_key = ""
        base_url = ""
        model_name = ""

        if config is not None:
            api_key = config.get_api_key()
            base_url = config.get('base_url') or ""
            cfg_model = config.get('model_name')
            model_name = cfg_model if cfg_model and cfg_model != '(未设置)' else ""

        self._config = config

        if model is not None:
            model_name = model

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = model_name

    def refresh(self):
        """配置变更后刷新客户端，重新从 config 读取 API Key 等信息"""
        if self._config is None:
            return
        api_key = self._config.get_api_key()
        base_url = self._config.get('base_url') or ""
        cfg_model = self._config.get('model_name')
        model_name = cfg_model if cfg_model and cfg_model != '(未设置)' else ""
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = model_name

    def chat(self, messages, tools=None, tool_choice="auto", temperature=0):
        """调用大模型聊天接口

        Args:
            messages (list): 消息列表
            tools (list, optional): 工具列表
            tool_choice (str, optional): 工具选择策略，默认为 "auto"
            temperature (float, optional): 温度参数，默认为 0

        Returns:
            dict: 标准化的响应消息
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature
        )

        msg = response.choices[0].message

        if msg.tool_calls:
            return {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    } for tc in msg.tool_calls
                ]
            }
        else:
            return {
                "role": "assistant",
                "content": msg.content
            }


if __name__ == "__main__":
    pass
