from openai import OpenAI


class LLMClient:
    """大模型调用客户端，封装 OpenAI API 调用"""

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

        self.client = OpenAI(
            api_key=_api_key,
            base_url=_base_url
        )
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

        self.client = OpenAI(
            api_key=_api_key,
            base_url=_base_url
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
