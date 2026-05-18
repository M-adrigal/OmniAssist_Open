import json
import os
import re
from datetime import datetime


class ToolRegistry:
    """工具注册管理类，用于注册、管理和执行工具"""

    def __init__(self, manifest_path: str = None):
        """初始化 ToolRegistry，创建空的工具字典

        Args:
            manifest_path: 工具清单文件路径，为 None 时自动检测
        """
        self.tools = {}
        self._manifest = None
        self._manifest_path = manifest_path

    def _get_manifest_path(self) -> str:
        if self._manifest_path:
            return self._manifest_path
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_dir, "tools_manifest.json")

    def load_manifest(self) -> dict:
        """加载工具清单文件

        Returns:
            dict: 清单数据，包含 version、tools 等字段
        """
        manifest_path = self._get_manifest_path()
        if not os.path.isfile(manifest_path):
            print(f"[清单] 工具清单文件不存在: {manifest_path}")
            self._manifest = {"version": 1, "tools": []}
            return self._manifest

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                self._manifest = json.load(f)
            print(f"[清单] 已加载工具清单，共 {len(self._manifest.get('tools', []))} 个工具")
            return self._manifest
        except (json.JSONDecodeError, IOError) as e:
            print(f"[清单] 加载失败: {e}")
            self._manifest = {"version": 1, "tools": []}
            return self._manifest

    def _save_manifest(self):
        manifest_path = self._get_manifest_path()
        if self._manifest is None:
            return
        self._manifest["updated_at"] = datetime.now().isoformat()
        try:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(self._manifest, f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"[清单] 保存失败: {e}")

    def add_to_manifest(self, name: str, description: str, category: str = "custom",
                        keywords: list = None, file: str = None):
        """向工具清单中添加一个工具条目

        Args:
            name: 工具名称
            description: 工具描述
            category: 工具分类
            keywords: 关键词列表
            file: JSON 文件名
        """
        if self._manifest is None:
            self.load_manifest()

        if keywords is None:
            keywords = []
        if file is None:
            file = f"{name}.json"

        tools = self._manifest.get("tools", [])
        for tool in tools:
            if tool["name"] == name:
                tool["description"] = description
                tool["category"] = category
                tool["keywords"] = keywords
                tool["file"] = file
                self._save_manifest()
                return

        tools.append({
            "name": name,
            "description": description,
            "category": category,
            "keywords": keywords,
            "file": file,
        })
        self._manifest["tools"] = tools
        self._save_manifest()

    def remove_from_manifest(self, name: str):
        """从工具清单中移除一个工具条目

        Args:
            name: 工具名称
        """
        if self._manifest is None:
            self.load_manifest()

        tools = self._manifest.get("tools", [])
        self._manifest["tools"] = [t for t in tools if t["name"] != name]
        self._save_manifest()

    def sync_manifest(self, tools_dir: str):
        """将工具目录与清单同步，自动发现新增/删除的工具

        Args:
            tools_dir: 工具 JSON 文件目录
        """
        if self._manifest is None:
            self.load_manifest()

        if not os.path.isdir(tools_dir):
            return

        existing_files = set()
        for filename in os.listdir(tools_dir):
            if filename.endswith(".json"):
                existing_files.add(filename)

        manifest_tools = self._manifest.get("tools", [])
        manifest_names = {t["name"] for t in manifest_tools}
        manifest_files = {t.get("file", f"{t['name']}.json") for t in manifest_tools}

        removed_files = manifest_files - existing_files
        if removed_files:
            self._manifest["tools"] = [
                t for t in manifest_tools
                if t.get("file", f"{t['name']}.json") in existing_files
            ]
            print(f"[清单] 自动清理 {len(removed_files)} 个已删除的工具")

        new_files = existing_files - manifest_files
        if new_files:
            for filename in sorted(new_files):
                filepath = os.path.join(tools_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        tool_data = json.load(f)
                    name = tool_data.get("name", "")
                    description = tool_data.get("description", "")
                    if name:
                        self._manifest["tools"].append({
                            "name": name,
                            "description": description,
                            "category": "custom",
                            "keywords": [],
                            "file": filename,
                        })
                except Exception:
                    pass
            print(f"[清单] 自动发现 {len(new_files)} 个新工具")

        self._save_manifest()

    def needs_tools(self, user_query: str) -> bool:
        """轻量预判：用户问题是否需要工具参与

        汇总所有工具的关键词，只要查询中命中任意一个关键词，就认为可能需要工具。
        这是比 filter_tools_by_query 更宽松的前置检查，目的是快速排除"纯对话"场景。

        Args:
            user_query: 用户输入的问题

        Returns:
            bool: True 表示可能需要工具，False 表示几乎不需要
        """
        if self._manifest is None:
            self.load_manifest()

        tools = self._manifest.get("tools", [])
        if not tools:
            return False

        query_lower = user_query.lower()

        global_keywords = set()
        for tool in tools:
            for kw in tool.get("keywords", []):
                global_keywords.add(kw.lower())
            for part in tool.get("description", "").split():
                if len(part) >= 2:
                    global_keywords.add(part.lower())

        for kw in global_keywords:
            kw_len = len(kw)
            if kw_len < 1:
                continue
            if kw_len == 1:
                if '\u4e00' <= kw <= '\u9fff' and kw in user_query:
                    return True
            elif kw in query_lower:
                return True

        return False

    def filter_tools_by_query(self, user_query: str, min_score: int = 2) -> list:
        """根据用户查询智能过滤工具，只返回相关的工具名称列表

        使用多维度评分策略：
        1. 关键词匹配：查询分词后与工具关键词匹配（含单字中文关键词）
        2. 分类匹配：根据查询特征推断所需工具类别
        3. 描述匹配：查询词出现在工具描述中
        4. 类别联动：某类别有工具高分匹配时，自动纳入同类的其他工具

        Args:
            user_query: 用户输入的问题
            min_score: 最低匹配分数，达到此分数的工具才会被包含

        Returns:
            list: 匹配的工具名称列表，如果无匹配则返回 None（表示全量发送）
        """
        if self._manifest is None:
            self.load_manifest()

        tools = self._manifest.get("tools", [])
        if not tools:
            return None

        query_lower = user_query.lower()

        query_tokens = set()
        for char in user_query:
            if '\u4e00' <= char <= '\u9fff' or '\u3400' <= char <= '\u4dbf':
                query_tokens.add(char)
        for word in re.findall(r'[a-zA-Z]+', query_lower):
            query_tokens.add(word)
        for i in range(len(user_query) - 1):
            c1, c2 = user_query[i], user_query[i + 1]
            if ('\u4e00' <= c1 <= '\u9fff' or '\u3400' <= c1 <= '\u4dbf') and \
               ('\u4e00' <= c2 <= '\u9fff' or '\u3400' <= c2 <= '\u4dbf'):
                query_tokens.add(c1 + c2)

        category_hints = {
            "document": ["保存", "生成", "导出", "制作", "创建", "写入", "输出", "文档",
                        "word", "excel", "pdf", "ppt", "docx", "xlsx", "pptx",
                        "报告", "表格", "演示", "幻灯片", "报表"],
            "weather": ["天气", "气温", "温度", "下雨", "刮风", "雾霾", "预报",
                       "明天", "后天", "这周", "下周", "未来", "近日"],
            "web": ["网页", "网站", "链接", "url", "http", "抓取", "爬取", "访问"],
            "utility": ["时间", "日期", "今天", "现在", "几点", "计算", "算", "农历", "阴历", "转换"],
        }

        category_bonus = {}
        for cat, hints in category_hints.items():
            for hint in hints:
                if hint in query_lower:
                    category_bonus[cat] = category_bonus.get(cat, 0) + 1

        scored = []
        for tool in tools:
            score = 0
            tool_name = tool["name"]
            tool_desc = tool.get("description", "").lower()
            tool_category = tool.get("category", "")
            tool_keywords = tool.get("keywords", [])

            for token in query_tokens:
                if len(token) >= 2:
                    for kw in tool_keywords:
                        if token in kw or kw in token:
                            score += 2
                            break
                elif len(token) == 1 and '\u4e00' <= token <= '\u9fff':
                    for kw in tool_keywords:
                        if len(kw) == 1 and kw == token:
                            score += 2
                            break

            for token in query_tokens:
                if len(token) >= 2 and token in tool_desc:
                    score += 1

            if tool_category in category_bonus:
                score += category_bonus[tool_category]

            scored.append((tool_name, tool_category, score))

        category_boost = set()
        scored_names = set()
        for name, cat, s in scored:
            scored_names.add(name)
            if s >= 5:
                category_boost.add(cat)

        if category_boost:
            for tool in tools:
                if tool["name"] not in scored_names and tool.get("category", "") in category_boost:
                    scored.append((tool["name"], tool.get("category", ""), min_score))

        result = [(name, s) for name, _, s in scored if s >= min_score]
        if not result:
            return None

        result.sort(key=lambda x: x[1], reverse=True)
        result = [name for name, _ in result]

        if len(result) > 8:
            result = result[:8]

        return result

    def get_tool_specs_by_names(self, tool_names: list) -> list:
        """根据工具名称列表获取 OpenAI 格式的工具规格

        Args:
            tool_names: 工具名称列表

        Returns:
            list: OpenAI function calling 格式的工具列表
        """
        specs = []
        for name in tool_names:
            tool = self.tools.get(name)
            if tool:
                specs.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"]
                    }
                })
        return specs

    def get_filtered_specs(self, user_query: str) -> list:
        """智能过滤：根据用户查询返回相关工具的 OpenAI 规格

        三层判断：
        1. needs_tools() 预判 → 不需要则返回空列表，零 token 浪费
        2. filter_tools_by_query() 智能匹配 → 有匹配则返回部分工具
        3. 全量兜底 → 无匹配但可能需要工具时发送全部

        Args:
            user_query: 用户输入的问题

        Returns:
            list: OpenAI function calling 格式的工具列表
        """
        if not self.needs_tools(user_query):
            return []
        filtered_names = self.filter_tools_by_query(user_query)
        if filtered_names is None:
            return self.get_all_openai_specs()
        return self.get_tool_specs_by_names(filtered_names)

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
