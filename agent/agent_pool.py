"""
多 Agent 池 — 管理子 Agent 的创建、调度和生命周期

支持功能：
- 从 YAML 配置文件加载 Agent 定义
- 子 Agent 懒加载（按需创建，复用实例）
- 同步委派和并行委派
- 自动注册为工具（Agent-as-Tool 模式）
- 子 Agent 上下文隔离（各分配独立技能子集）

用法：
    pool = AgentPool(llm_client, sandbox, skill_registry)
    pool.load_profiles("agent/profiles")
    pool.register_as_tools(tool_registry)  # 注册到主 Agent 的工具列表
"""

import json
import os
import yaml
import concurrent.futures
import threading
import uuid

from agent.agent import SimpleAgent
from agent.tools import ToolRegistry
from agent.logger import get_logger

logger = get_logger("pool")


class AgentPool:
    """多 Agent 池，管理子 Agent 的创建、调度和生命周期"""

    def __init__(self, llm_client, sandbox_pool, skill_registry):
        """初始化 AgentPool

        Args:
            llm_client: LLMClient 实例（子 Agent 共享同一个 LLM 客户端）
            sandbox_pool: SandboxPool 实例（每用户独立沙箱）
            skill_registry: SkillRegistry 实例
        """
        self.llm = llm_client
        self.sandbox_pool = sandbox_pool
        self.skill_registry = skill_registry
        self._profiles: dict[str, dict] = {}       # name → profile dict
        self._agents: dict[str, SimpleAgent] = {}   # name → SimpleAgent 实例
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._lock = threading.Lock()                # 保护 _profiles 和 _agents 的并发操作

    # ---- 注册 ----

    def load_profiles(self, profiles_dir: str) -> list:
        """从目录加载所有 Agent 配置文件

        Args:
            profiles_dir: 配置文件目录路径

        Returns:
            list: 成功加载的 Agent 名称列表
        """
        loaded = []
        if not os.path.isdir(profiles_dir):
            logger.warning(f"配置文件目录不存在: {profiles_dir}")
            return loaded

        for fname in sorted(os.listdir(profiles_dir)):
            if not fname.endswith(('.yaml', '.yml')):
                continue
            filepath = os.path.join(profiles_dir, fname)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    profile = yaml.safe_load(f)
                if not profile or 'name' not in profile:
                    logger.warning(f"跳过无效配置: {fname}")
                    continue
                self._profiles[profile['name']] = profile
                loaded.append(profile['name'])
                logger.info(f"已加载 Agent 配置: {profile['name']}")
            except Exception as e:
                logger.error(f"加载配置失败 {fname}: {e}")

        return loaded

    def register(self, profile: dict):
        """注册一个 Agent 配置（可从数据库或动态创建后调用）

        Args:
            profile: Agent 配置字典，格式与 YAML 文件一致
        """
        with self._lock:
            self._profiles[profile['name']] = profile

    def list_agents(self) -> list:
        """列出所有已注册的 Agent 名称"""
        return sorted(self._profiles.keys())

    # ---- Agent 创建（懒加载） ----

    def _get_or_create(self, name: str) -> SimpleAgent:
        """懒加载获取子 Agent 实例

        首次调用时创建 SimpleAgent 实例，之后复用。
        每个子 Agent 拥有独立的 ToolRegistry 和 SkillContext，
        只包含其配置文件中 assigned_skills 指定的技能。

        Args:
            name: Agent 名称

        Returns:
            SimpleAgent: 子 Agent 实例
        """
        if name in self._agents:
            return self._agents[name]

        with self._lock:
            # 双重检查，避免重复创建
            if name in self._agents:
                return self._agents[name]

            profile = self._profiles[name]

            # 创建独立的 ToolRegistry，只注册该 Agent 分配到的技能
            tool_registry = ToolRegistry()
            assigned_skills = profile.get('assigned_skills', [])
            for skill_name in assigned_skills:
                skill = self.skill_registry.get_system_skill(skill_name)
                if skill:
                    for script in skill.scripts:
                        tool_registry.register_tool(
                            name=script.name,
                            description=script.description,
                            parameters=script.parameters,
                            func=self._make_tool_func(script)
                        )

            # 构建技能上下文（只注入分配的技能）
            skill_context = self._build_skill_context(assigned_skills)

            agent = SimpleAgent(
                llm_client=self.llm,
                tool_registry=tool_registry,
                thinking_mode="low",       # 子 Agent 轻量思考、不展示
                skill_context=skill_context,
                silent=True                # 子 Agent 静默模式，不打印终端输出
            )
            # 覆盖系统提示词为 Agent 专属提示词
            agent._system_prompt = profile['system_prompt']
            agent._rebuild_system_message()

            self._agents[name] = agent
            return agent

    def _make_tool_func(self, script):
        """为脚本创建沙箱执行器

        Args:
            script: ScriptDef 实例

        Returns:
            callable: 工具执行函数
        """
        def executor(**kwargs):
            user_id = kwargs.pop('_user_id', None)
            if user_id is None:
                user_id = 0
            sandbox = self.sandbox_pool.get(user_id)
            if script.dependencies:
                sandbox.install(script.dependencies)
            return sandbox.execute(script.source, kwargs, user_id=user_id, tool_name=script.name)
        return executor

    def _build_skill_context(self, skill_names: list) -> str:
        """为子 Agent 构建精简的技能上下文

        Args:
            skill_names: 技能名称列表

        Returns:
            str: 技能上下文字符串
        """
        if not skill_names:
            return ""

        parts = ["\n# 可用技能\n"]
        parts.append("以下是你可以使用的专业技能。当用户任务匹配某技能描述时，"
                     "请遵循该技能的指令执行。\n")
        for name in skill_names:
            skill = self.skill_registry.get_system_skill(name)
            if skill:
                parts.append(skill.build_context())
                parts.append("")

        return "\n".join(parts)

    # ---- 委派 ----

    def delegate(self, agent_name: str, task: str, user_id: int = None) -> str:
        """委派任务给子 Agent，同步返回结果

        Args:
            agent_name: 目标 Agent 名称
            task: 任务描述
            user_id: 用户 ID，用于文件输出隔离

        Returns:
            str: 子 Agent 的最终回复
        """
        if agent_name not in self._profiles:
            return f"[委派失败] Agent '{agent_name}' 不存在，可用 Agent: {self.list_agents()}"

        agent = self._get_or_create(agent_name)
        agent.reset()
        profile = self._profiles[agent_name]
        max_iter = profile.get('max_iterations', 5)

        try:
            result = agent.run(task, max_iterations=max_iter)
            return result
        except Exception as e:
            return f"[子Agent {agent_name} 执行异常] {type(e).__name__}: {str(e)}"

    def delegate_parallel(self, tasks: list, user_id: int = None) -> dict:
        """并行委派多个任务给不同 Agent

        Args:
            tasks: [{"agent": "analyst", "task": "分析销售数据"}, ...]
            user_id: 用户 ID

        Returns:
            dict: {agent_name: result}
        """
        results = {}
        futures = {}
        for t in tasks:
            name = t['agent']
            task = t['task']
            timeout = self._profiles.get(name, {}).get('timeout', 60)
            f = self._executor.submit(self.delegate, name, task, user_id)
            futures[f] = (name, timeout)

        for f in concurrent.futures.as_completed(futures):
            name, timeout = futures[f]
            try:
                results[name] = f.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                results[name] = f"[子Agent {name} 执行超时] 超过 {timeout} 秒"
            except Exception as e:
                results[name] = f"[子Agent {name} 执行失败] {type(e).__name__}: {str(e)}"

        return results

    # ---- 动态 Agent ----

    def release_agent(self, name: str):
        """释放 Agent 实例和配置

        Args:
            name: Agent 名称
        """
        with self._lock:
            self._agents.pop(name, None)
            self._profiles.pop(name, None)

    def delegate_dynamic(self, system_prompt: str, task: str,
                         skills: list = None, user_id: int = None,
                         max_iterations: int = 5, timeout: int = 60) -> str:
        """动态创建临时 Agent 执行任务，完成后自动释放

        适用于无对应固定 Agent 的一次性任务，Agent 即用即弃。

        Args:
            system_prompt: 临时 Agent 的系统提示词，定义角色、职责和规则
            task: 要执行的具体任务描述
            skills: 分配给临时 Agent 的技能列表，如 ["calculator", "weather"]
            user_id: 用户 ID，用于文件输出隔离
            max_iterations: 最大迭代次数
            timeout: 超时时间（秒）

        Returns:
            str: 临时 Agent 的执行结果
        """
        temp_name = f"temp_{uuid.uuid4().hex[:8]}"

        profile = {
            'name': temp_name,
            'description': f'动态临时Agent',
            'system_prompt': system_prompt,
            'assigned_skills': skills or [],
            'max_iterations': max_iterations,
            'timeout': timeout,
        }
        self.register(profile)
        try:
            return self.delegate(temp_name, task, user_id)
        finally:
            self.release_agent(temp_name)

    # ---- 工具注册 ----

    def register_as_tools(self, target_registry: ToolRegistry):
        """将所有子 Agent 注册为工具，供主 Agent 调用

        注册两个工具：
        - delegate_{name}: 委派给单个 Agent
        - delegate_parallel: 并行委派给多个 Agent

        Args:
            target_registry: 主 Agent 的 ToolRegistry 实例
        """
        for name, profile in self._profiles.items():
            desc = profile.get('description', f'委派任务给{name}专家')
            # 闭包绑定 name，避免循环变量延迟绑定问题
            def make_func(n):
                return lambda task, _user_id=None: self.delegate(n, task, _user_id)

            target_registry.register_tool(
                name=f"delegate_{name}",
                description=desc,
                parameters={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": f"委派给{name}的具体任务描述，越详细越好"
                        }
                    },
                    "required": ["task"]
                },
                func=make_func(name)
            )

        # 注册并行委派工具
        if len(self._profiles) >= 2:
            target_registry.register_tool(
                name="delegate_parallel",
                description="并行委派多个任务给不同专家，适用于互不依赖的子任务。"
                            "可用 Agent 列表：" + "、".join(self._profiles.keys()),
                parameters={
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "string",
                            "description": (
                                "JSON 数组，每个元素包含 agent 和 task 字段。"
                                "示例：[{\"agent\":\"analyst\",\"task\":\"分析数据\"},"
                                "{\"agent\":\"document\",\"task\":\"生成Word\"}]"
                            )
                        }
                    },
                    "required": ["tasks"]
                },
                func=self._delegate_parallel_tool
            )

        # 注册动态委派工具
        target_registry.register_tool(
            name="delegate_dynamic",
            description="动态创建临时Agent执行特定任务，完成后自动释放。"
                        "适用于：需要特定领域知识但无对应固定Agent的一次性任务。"
                        "通过 system_prompt 定义Agent的角色、职责和规则。",
            parameters={
                "type": "object",
                "properties": {
                    "system_prompt": {
                        "type": "string",
                        "description": "临时Agent的系统提示词，定义其角色、职责和规则。例如：'你是一位古典诗词专家，擅长创作七言绝句'"
                    },
                    "task": {
                        "type": "string",
                        "description": "要执行的具体任务描述，越详细越好"
                    },
                    "skills": {
                        "type": "string",
                        "description": "分配给临时Agent的技能列表（JSON数组），如[\"calculator\",\"weather\"]。留空或\"[]\"则不给技能"
                    }
                },
                "required": ["system_prompt", "task"]
            },
            func=self._delegate_dynamic_tool
        )

    def _delegate_parallel_tool(self, tasks: str, _user_id: int = None) -> str:
        """并行委派工具的执行函数，解析 JSON 并委派

        Args:
            tasks: JSON 字符串格式的任务列表
            _user_id: 用户 ID

        Returns:
            str: JSON 格式的委派结果
        """
        try:
            task_list = json.loads(tasks)
        except json.JSONDecodeError as e:
            return f"错误：tasks 参数必须是有效的 JSON 数组。{str(e)}"

        if not isinstance(task_list, list):
            return "错误：tasks 参数必须是 JSON 数组格式"

        # 验证每个任务
        for t in task_list:
            if not isinstance(t, dict) or 'agent' not in t or 'task' not in t:
                return f"错误：每个任务必须包含 agent 和 task 字段，当前值: {t}"

        results = self.delegate_parallel(task_list, _user_id)
        return json.dumps(results, ensure_ascii=False, indent=2)

    def _delegate_dynamic_tool(self, system_prompt: str, task: str,
                                skills: str = None, _user_id: int = None) -> str:
        """动态委派工具的执行函数，解析参数并委派

        Args:
            system_prompt: 临时 Agent 的系统提示词
            task: 任务描述
            skills: JSON 字符串格式的技能列表
            _user_id: 用户 ID

        Returns:
            str: 临时 Agent 的执行结果
        """
        skill_list = []
        if skills and skills.strip():
            try:
                skill_list = json.loads(skills)
            except json.JSONDecodeError:
                return f"错误：skills 参数必须是有效的 JSON 数组。当前值: {skills}"

        return self.delegate_dynamic(
            system_prompt=system_prompt,
            task=task,
            skills=skill_list,
            user_id=_user_id,
        )

    # ---- 生命周期 ----

    def shutdown(self):
        """关闭线程池，释放资源"""
        self._executor.shutdown(wait=True)
        self._agents.clear()