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

    def execute(self, name, arguments: dict) -> str:
        """根据工具名和参数执行对应的函数

        Args:
            name (str): 工具名称
            arguments (dict): 参数字典

        Returns:
            str: 执行结果的字符串形式，出错时返回错误信息
        """
        if name not in self.tools:
            return f"Error: Tool '{name}' not found"
        
        try:
            result = self.tools[name]["function"](**arguments)
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

    def load_tools_from_dir(self, tools_dir: str, tool_names: list = None,
                            func_factory=None) -> list:
        """从指定目录加载 Tool JSON 文件并注册为工具

        扫描目录下所有 .json 文件，解析为 Tool 定义并注册。
        func_factory 签名为 (tool_name, execution_prompt, execution_mode, execution_code, http_config, dependencies) -> callable。

        Args:
            tools_dir: Tool JSON 文件所在目录路径
            tool_names: 指定要加载的 Tool 名称列表，为 None 则加载全部
            func_factory: 可选的执行函数工厂。
                          如果提供，将为每个 Tool 创建执行函数；否则 Tool 仅注册定义不含执行函数。

        Returns:
            list: 成功加载的 Tool 名称列表
        """
        if not os.path.isdir(tools_dir):
            print(f"[警告] Tool 目录不存在：{tools_dir}")
            return []

        loaded = []
        for filename in sorted(os.listdir(tools_dir)):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(tools_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    tool_data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[警告] 无法读取 Tool 文件 {filename}：{e}")
                continue

            tool_name = tool_data.get("name")
            if not tool_name:
                print(f"[警告] Tool 文件 {filename} 缺少 'name' 字段，跳过")
                continue

            if tool_names is not None and tool_name not in tool_names:
                continue

            executor = None
            if func_factory is not None:
                execution_prompt = tool_data.get("execution_prompt", "")
                execution_mode = tool_data.get("execution_mode", "llm_simulated")
                execution_code = tool_data.get("execution_code", "")
                http_config = tool_data.get("http_config", {})
                dependencies = tool_data.get("dependencies")
                response_formatter = tool_data.get("response_formatter")
                executor = func_factory(tool_name, execution_prompt, execution_mode, execution_code, http_config, dependencies, response_formatter)

            self.register_tool(
                name=tool_name,
                description=tool_data.get("description", ""),
                parameters=tool_data.get("parameters", {}),
                func=executor
            )
            loaded.append(tool_name)

        return loaded

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

    @staticmethod
    def list_available_tools(tools_dir: str) -> list:
        """列出指定目录下所有可用的 Tool 名称

        Args:
            tools_dir: Tool JSON 文件所在目录路径

        Returns:
            list: 可用的 Tool 名称列表，每个元素为 (name, description) 元组
        """
        if not os.path.isdir(tools_dir):
            return []

        available = []
        for filename in sorted(os.listdir(tools_dir)):
            if not filename.endswith(".json"):
                continue

            filepath = os.path.join(tools_dir, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    tool_data = json.load(f)
                name = tool_data.get("name", "")
                description = tool_data.get("description", "")
                if name:
                    available.append((name, description))
            except (json.JSONDecodeError, IOError):
                continue

        return available

    @staticmethod
    def delete_tool_file(tools_dir: str, tool_name: str) -> bool:
        """删除指定 Tool 的 JSON 文件

        Args:
            tools_dir: Tool JSON 文件所在目录路径
            tool_name: 要删除的工具名称

        Returns:
            bool: 删除成功返回 True，文件不存在返回 False
        """
        filepath = os.path.join(tools_dir, f"{tool_name}.json")
        if os.path.isfile(filepath):
            os.remove(filepath)
            return True
        return False


if __name__ == "__main__":
    pass
