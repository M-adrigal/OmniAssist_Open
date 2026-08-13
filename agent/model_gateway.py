"""模型网关：统一多模型参数适配层

根据模型名称自动匹配能力配置，将通用的 thinking/temperature 参数
翻译为各模型原生的 API 参数，并归一化流式响应中的推理内容。
"""
from fnmatch import fnmatch
from typing import Optional


MODEL_CAPABILITIES = {
    "doubao*": {
        "provider": "doubao",
        "thinking_param": "extra_body",
        "thinking_on": {"extra_body": {"thinking": {"type": "enabled"}}},
        "thinking_off": {"extra_body": {"thinking": {"type": "disabled"}}},
        "reasoning_field": "reasoning_content",
        "needs_prompt_fallback": False,
        "supports_temperature": True,
    },
    "gpt-5-mini*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": True,
        "supports_reasoning_effort": True,
    },
    "gpt-5-nano*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": True,
        "supports_reasoning_effort": True,
    },
    "gpt-5*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": True,
        "supports_reasoning_effort": True,
    },
    "o3*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": False,
        "supports_reasoning_effort": True,
    },
    "o4*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": False,
        "supports_reasoning_effort": True,
    },
    "o1*": {
        "provider": "openai",
        "thinking_param": "native",
        "thinking_on": {"reasoning_effort": "medium"},
        "thinking_off": {"reasoning_effort": "minimal"},
        "reasoning_field": None,
        "needs_prompt_fallback": True,
        "supports_temperature": False,
        "supports_reasoning_effort": True,
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
        "supports_reasoning_effort": True,
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
        "supports_reasoning_effort": True,
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
        "supports_reasoning_effort": True,
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

# 思考模式三态：关（不思考）/ 低（轻思考、不展示）/ 高（强思考、展示）
THINKING_MODES = ("off", "low", "high")


def thinking_mode_to_flags(mode: str):
    """将 thinking_mode 翻译为底层参数标记。

    Args:
        mode: "off" / "low" / "high"（其它值按 "low" 处理）

    Returns:
        tuple: (thinking_enabled, reasoning_effort)
            thinking_enabled: 是否开启思考（决定 build_params 选 thinking_on/off）
            reasoning_effort: 推理强度覆盖（"low"/"high"/None），仅 supported 模型生效
    """
    m = (mode or "low").strip().lower()
    if m == "off":
        return (False, None)
    if m == "high":
        return (True, "high")
    # low（默认）
    return (True, "low")


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

    def build_params(self, thinking_enabled: bool, temperature: float = 0,
                     reasoning_effort: str = None) -> dict:
        """根据思考开关和温度，构建模型特定的 API 参数

        Args:
            thinking_enabled: 是否开启思考模式（由 thinking_mode 推导，而非展示开关）
            temperature: 温度参数（0-2）
            reasoning_effort: 推理强度覆盖（"low"/"high"）。
                仅当模型支持（supports_reasoning_effort）且开启思考时生效，
                用于在不改模型默认行为的前提下按需调低/调高 token 消耗。

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
                thinking_enabled
                and self.cap.get("temperature_unsupported_when_thinking", False)
            )
            if not skip_temp:
                params["temperature"] = temperature

        thinking_type = self.cap["thinking_param"]
        if thinking_type == "native":
            source = self.cap["thinking_on"] if thinking_enabled else self.cap["thinking_off"]
            params.update(source)
        elif thinking_type == "extra_body":
            source = self.cap["thinking_on"] if thinking_enabled else self.cap["thinking_off"]
            if "extra_body" in source:
                params["extra_body"] = source["extra_body"]
            if "reasoning_effort" in source:
                params["reasoning_effort"] = source["reasoning_effort"]
        elif thinking_type == "always_on":
            pass
        elif thinking_type == "prompt":
            pass

        # 推理强度覆盖：仅当模型支持且处于思考开启状态才覆盖（思考关闭时设
        # reasoning_effort 对多数模型无意义甚至报错）
        if (
            reasoning_effort
            and thinking_enabled
            and self.cap.get("supports_reasoning_effort", False)
        ):
            params["reasoning_effort"] = reasoning_effort

        result["api_params"] = params
        return result

    def extract_reasoning(self, delta) -> Optional[str]:
        """从流式响应的 delta 对象中提取原生推理内容

        Args:
            delta: OpenAI SDK 流式 chunk 的 delta 对象

        Returns:
            Optional[str]: 推理内容文本，没有则返回 None
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
    def reasoning_field(self) -> Optional[str]:
        return self.cap.get("reasoning_field")