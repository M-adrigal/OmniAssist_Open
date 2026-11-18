"""把用户技能的脚本注册成可调用的工具（按用户隔离）。

## 为什么需要这个模块

工具清单只在服务启动时由 `get_all_scripts()`（**不传 user_id**）构建，用户技能
（`agent/skills/user/{db_id}/`、DB 里的 user_skills）的脚本从来没进过清单。
结果是：用户建了技能，只有 SKILL.md 的说明进了提示词，脚本根本调不到 ——
"技能"对用户是半个空承诺。

## 隔离怎么做（这是本模块的重点）

三层，缺一不可：

1. **注册表键带用户前缀**：`u{db_id}__{脚本名}`。两个用户有同名脚本也不会互相覆盖，
   也不会撞上系统工具（更不会有人用 `web_search` 当脚本名把真工具顶掉）。
2. **下发清单时改写**：`_filter_disabled_tools` 只保留当前用户前缀的项，并把名字
   改回干净形式（`fetch_prices`）。**模型看到的是自然名字**，前缀只存在于注册表内部。
3. **执行前解析 + 归属校验**：模型传来的干净名字要映射回带前缀的键；
   执行器内部再校验一次调用者是不是所有者，防"猜到别人的名字直接调"。

## 风险等级

用户代码一律按 `_classify_skill_risk` 判（来源已修好会传染到脚本），
即 local_execution → exec + require_approval；不是系统技能那种免审批。
联网权限也不给（`allow_network=False`），要联网得管理员配 SANDBOX_NETWORK_ALLOWLIST。
"""
import re

from agent.logger import get_logger

logger = get_logger(__name__)

PREFIX_SEP = "__"
_MAX_TOOL_NAME = 64


def _sanitize(name: str) -> str:
    """OpenAI 工具名只允许字母数字下划线和短横线，其余替换掉。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", (name or "").strip())[:40]


def tool_name_for(user_id: int, script_name: str) -> str:
    """注册表里的键：u{uid}__{脚本名}，并截断到长度上限。"""
    prefix = f"u{int(user_id)}{PREFIX_SEP}"
    return (prefix + _sanitize(script_name))[:_MAX_TOOL_NAME]


def strip_prefix(name: str) -> str:
    """把注册表键还原成给模型/界面看的名字。非用户工具原样返回。"""
    if not name or PREFIX_SEP not in name:
        return name
    head, _, tail = name.partition(PREFIX_SEP)
    if head.startswith("u") and head[1:].isdigit():
        return tail
    return name


def is_foreign_user_tool(name: str, user_id) -> bool:
    """是不是"别的用户"的工具。用于下发清单时剔除。"""
    if not name or PREFIX_SEP not in name:
        return False
    head, _, _ = name.partition(PREFIX_SEP)
    if not (head.startswith("u") and head[1:].isdigit()):
        return False
    try:
        return int(head[1:]) != int(user_id)
    except (TypeError, ValueError):
        return True


def resolve_tool_name(registry, name: str, user_id) -> str:
    """模型传来的干净名字 → 注册表键。找不到就返回原名（交给上层报 not found）。"""
    if not name or registry is None or not user_id:
        return name
    try:
        tools = registry.list_tools()
    except Exception:
        return name
    if name in tools:
        return name
    candidate = tool_name_for(user_id, name)
    return candidate if candidate in tools else name


def _wrap_owner(owner_id, inner):
    """归属校验：只有所有者能调。第二层防线（第一层是清单过滤）。"""

    def _call(**kwargs):
        caller = kwargs.pop("_user_id", None)
        if caller is not None and int(caller) != int(owner_id):
            logger.warning(
                f"拒绝跨用户调用用户技能工具: owner={owner_id} caller={caller}"
            )
            return "Error: 无权调用该工具（它属于其他用户的技能）"
        return inner(**kwargs)

    return _call


def ensure_user_tools(registry, skill_registry, user_id, disabled_names=None,
                      sandbox_pool=None, llm_client=None,
                      executor_factory=None, classify_risk=None) -> set:
    """把当前用户技能里的脚本注册成工具（懒注册，已注册的跳过）。

    Args:
        registry: ToolRegistry 全局实例。
        skill_registry: SkillRegistry 实例（需已加载该用户的技能）。
        user_id: 用户内部整数 ID。
        disabled_names: 被禁用的技能名集合（这些不注册）。
        sandbox_pool / llm_client: 交给执行器。
        executor_factory / classify_risk: 可注入，便于测试（生产用默认）。

    Returns:
        set: 本次涉及的注册表键（含已存在的）。
    """
    names = set()
    if registry is None or skill_registry is None or not user_id:
        return names

    if executor_factory is None:
        from agent.main import _create_executor as executor_factory
    if classify_risk is None:
        from server.main import _classify_skill_risk as classify_risk

    try:
        skills = skill_registry.get_enabled_skills(user_id, disabled_names)
    except Exception as e:
        logger.warning(f"读取用户技能失败（不影响对话）: {e}")
        return names

    for skill in skills.values():
        # 只管用户技能；系统技能的脚本启动时已注册
        if getattr(skill, "is_system", True):
            continue
        for script in getattr(skill, "scripts", []) or []:
            key = tool_name_for(user_id, script.name)
            names.add(key)
            try:
                if key in registry.list_tools():
                    continue
                inner = executor_factory(
                    script.name, script.description, script.execution_mode,
                    script.source, script.http_config, llm_client,
                    script.dependencies, script.response_formatter,
                    sandbox_pool=sandbox_pool,
                    allow_network=False,   # 用户代码不给联网，需要就配白名单
                )
                risk, need_approval, risk_desc = classify_risk(script)
                registry.register_tool(
                    name=key,
                    description=f"[{skill.name}] {script.description}",
                    parameters=script.parameters,
                    func=_wrap_owner(user_id, inner),
                    risk_level=risk,
                    risk_description=risk_desc,
                    require_approval=need_approval,
                )
                logger.info(
                    f"已注册用户技能工具: {key} (skill={skill.name}, risk={risk}, "
                    f"approval={need_approval})"
                )
            except Exception as e:
                logger.warning(f"注册用户技能工具失败 {key}: {e}")
    return names
