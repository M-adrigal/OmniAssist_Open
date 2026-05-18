"""模型网关：统一多模型参数适配层

根据模型名称自动匹配能力配置，将通用的 thinking/temperature 参数
翻译为各模型原生的 API 参数，并归一化流式响应中的推理内容。
"""
from fnmatch import fnmatch


MODEL_CAPABILITIES = {
    "gpt-5-mini*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": True,
    },
    "gpt-5-nano*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": True,
    },
    "gpt-5*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": True,
    },
    "o3*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": False,
    },
    "o4*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": False,
    },
    "o1*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": False,
    },
    "deepseek-reasoner*": {
        "provider": "deepseek",
        "thinking_param": "always_on",
        "thinking_on": {},
        "thinking_off": {},
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
        "temperature_unsupported_when_thinking": True,
    },
    "deepseek-v4-pro*": {
        "provider": "deepseek",
        "thinking_param": "extra_body",
        "thinking_on": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
        },
        "thinking_off": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
        "temperature_unsupported_when_thinking": True,
    },
    "deepseek-v4-flash*": {
        "provider": "deepseek",
        "thinking_param": "extra_body",
        "thinking_on": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
        },
        "thinking_off": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
        "temperature_unsupported_when_thinking": True,
    },
    "deepseek*": {
        "provider": "deepseek",
        "thinking_param": "extra_body",
        "thinking_on": {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
        },
        "thinking_off": {
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
        "temperature_unsupported_when_thinking": True,
    },
    "qwq*": {
        "provider": "qwen",
        "thinking_param": "always_on",
        "thinking_on": {},
        "thinking_off": {},
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
    },
    "qwen3-next*thinking*": {
        "provider": "qwen",
        "thinking_param": "always_on",
        "thinking_on": {},
        "thinking_off": {},
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
    },
    "qwen*": {
        "provider": "qwen",
        "thinking_param": "extra_body",
        "thinking_on": {"extra_body": {"enable_thinking": True}},
        "thinking_off": {"extra_body": {"enable_thinking": False}},
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
    },
    "claude*": {
        "provider": "anthropic",
        "thinking_param": "extra_body",
        "thinking_on": {
            "extra_body": {
                "thinking": {"type": "enabled", "budget_tokens": 4096}
            },
        },
        "thinking_off": {},
        "reasoning_field": "reasoning",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
    },
}

DEFAULT_CAPABILITY = {
    "provider": "unknown",
    "thinking_param": "prompt",
    "thinking_on": {},
    "thinking_off": {},
    "reasoning_field": None,
    "needs_prompt_fallback": False,
    "supports_temperature": True,
}


class ModelGateway:
    """模型网关：检测模型能力，翻译通用参数为模型特定参数"""

    def __init__(self, model_name: str = ""):
        self.model_name = model_name
        self.cap = self._detect(model_name)

    def _detect(self, model_name: str) -> dict:
        for pattern, cap in MODEL_CAPABILITIES.items():
            if fnmatch(model_name.lower(), pattern):
                return cap
        return DEFAULT_CAPABILITY

    def build_params(self, show_thought: bool, temperature: float = 0) -> dict:
        """根据思考开关和温度，构建模型特定的 API 参数

        Args:
            show_thought: 是否开启思考模式
            temperature: 温度参数（0-2）

        Returns:
            dict: {
                "api_params": 传给 OpenAI SDK 的额外参数,
                "needs_prompt_fallback": 是否需要 <thinking> 提示词兜底,
                "reasoning_field": 流式 delta 中推理内容的字段名（None 表示不在流中输出）,
                "thinking_param": 思考参数类型（native/extra_body/always_on/prompt）,
            }
        """
        params = {}
        result = {
            "api_params": {},
            "needs_prompt_fallback": self.cap.get("needs_prompt_fallback", False),
            "reasoning_field": self.cap.get("reasoning_field"),
            "thinking_param": self.cap["thinking_param"],
        }

        if self.cap.get("supports_temperature", True):
            skip_temp = (
                show_thought
                and self.cap.get("temperature_unsupported_when_thinking", False)
            )
            if not skip_temp:
                params["temperature"] = temperature

        thinking_type = self.cap["thinking_param"]
        if thinking_type == "native":
            source = self.cap["thinking_on"] if show_thought else self.cap["thinking_off"]
            params.update(source)
        elif thinking_type == "extra_body":
            source = self.cap["thinking_on"] if show_thought else self.cap["thinking_off"]
            if "extra_body" in source:
                params["extra_body"] = source["extra_body"]
            if "reasoning_effort" in source:
                params["reasoning_effort"] = source["reasoning_effort"]
        elif thinking_type == "always_on":
            pass
        elif thinking_type == "prompt":
            pass

        result["api_params"] = params
        return result

    def extract_reasoning(self, delta) -> str | None:
        """从流式响应的 delta 对象中提取原生推理内容

        Args:
            delta: OpenAI SDK 流式 chunk 的 delta 对象

        Returns:
            str | None: 推理内容文本，没有则返回 None
        """
        field = self.cap.get("reasoning_field")
        if field and hasattr(delta, field):
            val = getattr(delta, field)
            if val:
                return val
        return None

    @property
    def provider(self) -> str:
        return self.cap.get("provider", "unknown")

    @property
    def needs_prompt_fallback(self) -> bool:
        return self.cap.get("needs_prompt_fallback", False)

    @property
    def reasoning_field(self) -> str | None:
        return self.cap.get("reasoning_field")