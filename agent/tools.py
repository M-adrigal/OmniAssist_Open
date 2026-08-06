import json
import os


class ToolRegistry:
    """工具注册管理类，用于注册、管理和执行工具"""

    def __init__(self):
        """初始化 ToolRegistry，创建空的工具字典"""
        self.tools = {}

    def register_tool(self, name, description, parameters, func):
        """注册一个工具

        Args:
            name (str): 工具名称
            description (str): 工具描述
            parameters (dict): JSON Schema 格式的参数定义
            func (callable): 可调用的函数对象
        """
        self.tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "function": func
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


if __name__ == "__main__":
    pass
