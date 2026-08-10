import json
import os


class ToolRegistry:
    """工具注册管理类，用于注册、管理和执行工具"""

    def __init__(self):
        """初始化 ToolRegistry，创建空的工具字典"""
        self.tools = {}

    def register_tool(self, name, description, parameters, func,
                      risk_level: str = "safe", risk_description: str = None,
                      require_approval: bool = None):
        """注册一个工具

        Args:
            name (str): 工具名称
            description (str): 工具描述
            parameters (dict): JSON Schema 格式的参数定义
            func (callable): 可调用的函数对象
            risk_level (str): 风险等级 safe/read/write/exec/admin，决定是否需要审批
            risk_description (str): 人类可读的风险说明模板，可用 {arg} 占位填充参数
            require_approval (bool|None): 显式覆盖是否需审批；None 时按 risk_level 推断
                （write/exec/admin 默认需审批，safe/read 不需）
        """
        self.tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "function": func,
            "risk_level": risk_level,
            "risk_description": risk_description,
            "require_approval": require_approval,
        }

    def get_all_openai_specs(self):
        """获取符合 OpenAI 要求的工具列表

        Returns:
            list: 每个工具格式为 {"type": "function", "function": {...}}
        """
        specs = []
        for tool in self.tools.values():
            specs.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"]
                }
            })
        return specs

    def execute(self, name, arguments: dict, user_id: int = None) -> str:
        """根据工具名和参数执行对应的函数

        Args:
            name (str): 工具名称
            arguments (dict): 参数字典
            user_id (int): 可选，当前用户ID，用于文件输出隔离

        Returns:
            str: 执行结果的字符串形式，出错时返回错误信息
        """
        if name not in self.tools:
            return f"Error: Tool '{name}' not found"
        
        try:
            if user_id is not None:
                arguments['_user_id'] = user_id
            result = self.tools[name]["function"](**arguments)
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        except Exception as e:
            return f"Error executing tool '{name}': {str(e)}"

    def load_from_json(self, filepath, func_mapping):
        """从 JSON 文件加载工具定义并注册

        Args:
            filepath (str): JSON 文件路径
            func_mapping (dict): 函数名到函数对象的映射字典
        """
        with open(filepath, 'r', encoding='utf-8') as f:
            tools_data = json.load(f)
        
        for tool_data in tools_data:
            name = tool_data["name"]
            if name in func_mapping:
                self.register_tool(
                    name=name,
                    description=tool_data["description"],
                    parameters=tool_data["parameters"],
                    func=func_mapping[name]
                )

    def unregister_tool(self, name: str) -> bool:
        """从注册中心移除一个工具

        Args:
            name: 工具名称

        Returns:
            bool: 移除成功返回 True，工具不存在返回 False
        """
        if name in self.tools:
            del self.tools[name]
            return True
        return False

    def list_tools(self) -> dict:
        """列出所有已注册的工具及其描述

        Returns:
            dict: {tool_name: {"description": str, "parameters": dict, ...}}
        """
        return {
            name: {
                "description": info.get("description", ""),
                "parameters": info.get("parameters", {}),
            }
            for name, info in self.tools.items()
        }

    # ===== 风险 / 审批 相关 =====

    def needs_approval(self, name: str, args: dict = None) -> bool:
        """该工具执行前是否需要用户确认。"""
        t = self.tools.get(name)
        if not t:
            return False
        ra = t.get("require_approval")
        if ra is not None:
            return bool(ra)
        return t.get("risk_level", "safe") in ("write", "exec", "admin")

    def get_risk_level(self, name: str) -> str:
        return self.tools.get(name, {}).get("risk_level", "safe")

    def describe_tool_risk(self, name: str, args: dict) -> str:
        """生成人类可读的风险说明（用参数填充模板，失败回退到工具描述）。"""
        t = self.tools.get(name, {})
        template = t.get("risk_description")
        if template and isinstance(args, dict):
            try:
                return template.format(**args)
            except Exception:
                return template
        return t.get("description", name)


if __name__ == "__main__":
    pass
