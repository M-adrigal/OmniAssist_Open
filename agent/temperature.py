"""动态温度策略：将温度从写死常量改为按上下文动态计算。

策略在每次 LLM 调用前由 SimpleAgent 根据「迭代轮次 / 任务类型 / 模型能力」
解析出一个温度值，再交给 ModelGateway 做最终的模型能力门控。

设计要点：
- 策略返回 float 或 None。返回 None 表示「该模型不支持温度」，交由网关决定，
  避免对 o3/o1 等不支持温度的模型硬传参导致报错。
- 模型能力门控（supports_temperature / 思考时禁用温度）完全在 ModelGateway
  侧，本模块只负责"算出想要的温度"，互不耦合。
- 不引入任何额外 LLM 调用：任务类型用轻量启发式判定。
"""

from abc import ABC, abstractmethod
from typing import Optional

VALID_MODES = ("static", "auto")

# 温度合法区间
TEMP_MIN = 0.0
TEMP_MAX = 2.0


def clamp(v: float, lo: float = TEMP_MIN, hi: float = TEMP_MAX) -> float:
    """把温度裁剪到合法区间"""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        fv = 0.0
    return max(lo, min(hi, fv))


class TemperaturePolicy(ABC):
    """温度策略接口

    resolve() 在每次 LLM 调用前被调用，返回本次调用应使用的温度。

    Args:
        iteration: 当前迭代轮次（从 0 开始），用于衰减/收敛。
        phase: 调用阶段（如 "tool" / "final" / ""），预留扩展。
        task_type: 轻量启发式判定的任务类型（"code"/"writing"/"brainstorm"/
            "analysis"/None），由调用方传入。
        model_cap: ModelGateway 解析出的模型能力字典，至少含
            supports_temperature 键。
    """

    mode = "static"

    @abstractmethod
    def resolve(self, *, iteration: int = 0, phase: str = "",
                task_type: Optional[str] = None,
                model_cap: Optional[dict] = None) -> Optional[float]:
        ...

    @staticmethod
    def _supports(model_cap: Optional[dict]) -> bool:
        return bool((model_cap or {}).get("supports_temperature", True))


class StaticPolicy(TemperaturePolicy):
    """固定温度：始终返回设定基准值（模型不支持时返回 None）"""

    mode = "static"

    def __init__(self, base: float = 0.7):
        self.base = clamp(base)

    def resolve(self, *, iteration=0, phase="", task_type=None, model_cap=None):
        if not self._supports(model_cap):
            return None
        return self.base


class TaskTypePolicy(TemperaturePolicy):
    """按任务类型选基准温度，不做衰减

    base 作为「分析/未知」类任务的兜底基准。
    """

    BASE_BY_TYPE = {
        "brainstorm": 1.0,
        "writing": 0.9,
        "code": 0.3,
        "analysis": 0.2,
    }
    DEFAULT_BASE = 0.7

    mode = "task_type"

    def __init__(self, base: float = 0.7):
        self.default_base = clamp(base)

    def _base_for(self, task_type: Optional[str]) -> float:
        if task_type in self.BASE_BY_TYPE:
            return self.BASE_BY_TYPE[task_type]
        return self.default_base

    def resolve(self, *, iteration=0, phase="", task_type=None, model_cap=None):
        if not self._supports(model_cap):
            return None
        return clamp(self._base_for(task_type))


class DecayPolicy(TemperaturePolicy):
    """随迭代轮次收敛：首轮发散，后续轮次线性向 floor 收敛

    适用于「先发散探索、后聚焦收敛」的场景；base 与 floor 之间的插值
    由迭代进度驱动。
    """

    mode = "decay"

    def __init__(self, base: float = 0.7, floor: float = 0.2, max_rounds: int = 5):
        self.base = clamp(base)
        self.floor = clamp(floor)
        self.max_rounds = max(1, int(max_rounds))

    def resolve(self, *, iteration=0, phase="", task_type=None, model_cap=None):
        if not self._supports(model_cap):
            return None
        t = max(0, min(int(iteration), self.max_rounds))
        temp = self.base + (self.floor - self.base) * (t / self.max_rounds)
        return clamp(temp)


class AutoPolicy(TemperaturePolicy):
    """自动策略（推荐默认）：任务类型选基准 + 迭代衰减收敛

    - 基准温度由任务类型决定（脑暴最高、代码/分析最低）。
    - 随迭代轮次从基准线性收敛到 FLOOR，避免长链路后期过度发散。
    """

    BASE_BY_TYPE = {
        "brainstorm": 1.0,
        "writing": 0.9,
        "code": 0.3,
        "analysis": 0.2,
    }
    DEFAULT_BASE = 0.7
    FLOOR = 0.2
    MAX_ROUNDS = 5

    mode = "auto"

    def __init__(self, base: float = 0.7):
        self.default_base = clamp(base)

    def _base_for(self, task_type: Optional[str]) -> float:
        if task_type in self.BASE_BY_TYPE:
            return self.BASE_BY_TYPE[task_type]
        return self.default_base

    def resolve(self, *, iteration=0, phase="", task_type=None, model_cap=None):
        if not self._supports(model_cap):
            return None
        base = self._base_for(task_type)
        t = max(0, min(int(iteration), self.MAX_ROUNDS))
        temp = base + (self.FLOOR - base) * (t / self.MAX_ROUNDS)
        return clamp(temp)


def build_policy(mode: str, base: float = 0.7) -> TemperaturePolicy:
    """根据配置字符串构建对应策略实例

    Args:
        mode: "static" / "auto"（其它/非法值按 "auto" 处理）
        base: 基准温度（自动模式下作为分析/未知类任务的兜底基准）
    """
    mode = (mode or "auto").strip().lower()
    if mode not in VALID_MODES:
        mode = "auto"
    base = clamp(base if base is not None else 0.7)
    if mode == "auto":
        return AutoPolicy(base=base)
    return StaticPolicy(base=base)


# 代码/执行类工具名（用于任务类型启发式）
_CODE_TOOLS = {
    "run_command", "create_local_executor", "execute_code",
    "sandbox", "write_file", "edit_file", "read_file",
    "python_exec", "bash_exec",
}


def classify_task_type(message: str, tool_names: Optional[list] = None) -> Optional[str]:
    """轻量启发式任务分类（不引入额外 LLM 调用）

    优先级：代码类工具命中 → code；否则按消息关键词判定写作/脑暴/分析；
    都不命中返回 None（交由策略使用默认基准）。

    Args:
        message: 用户输入文本
        tool_names: 本轮回选中的工具名列表（可选）

    Returns:
        str | None: "code"/"writing"/"brainstorm"/"analysis"/None
    """
    if tool_names:
        if _CODE_TOOLS & set(tool_names):
            return "code"

    msg = (message or "").lower()
    if any(k in msg for k in (
        "写", "创作", "文案", "文章", "诗", "润色", "翻译", "总结", "起草",
        "write", "draft", "compose", "polish",
    )):
        return "writing"
    if any(k in msg for k in (
        "点子", "脑暴", "方案", "创意", "灵感", "起名", "slogan",
        "brainstorm", "idea", "naming",
    )):
        return "brainstorm"
    if any(k in msg for k in (
        "分析", "为什么", "对比", "原因", "解释", "查", "如何", "怎么",
        "analyze", "why", "compare", "explain", "how",
    )):
        return "analysis"
    return None
