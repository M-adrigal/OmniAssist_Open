"""
技能注册中心 — 管理 Skill 的加载、脚本发现和上下文构建

Skill 是比 Tool 更高层的能力抽象，每个 Skill 是一个文件夹：
  skill-name/
    SKILL.md          # 必需：YAML frontmatter + Markdown 指令
    scripts/          # 可选：可执行 Python 脚本
      *.py

脚本通过 AST 自动解析，从 execute() 函数的签名和 docstring 生成工具定义。
"""

import ast
import json
import os
import re
from agent.logger import get_logger

logger = get_logger("skill")


class ScriptDef:
    """脚本定义 — 从 Python 文件自动解析，替代 JSON 工具定义

    通过 AST 解析 .py 文件，自动提取：
    - execute() 函数签名（参数名、类型、是否必填）
    - 模块 docstring（工具描述）
    - 依赖的第三方库（import 语句分析）
    - 特殊注释：HTTP_CONFIG、DEPENDENCIES 等元数据
    """

    def __init__(self, name, description, source, parameters,
                 execution_mode="local_execution", dependencies=None,
                 http_config=None, response_formatter=None, path=""):
        self.name = name
        self.description = description
        self.source = source
        self.parameters = parameters
        self.execution_mode = execution_mode
        self.dependencies = dependencies or []
        self.http_config = http_config or {}
        self.response_formatter = response_formatter
        self.path = path

    @classmethod
    def from_file(cls, script_path: str) -> "ScriptDef":
        """从 .py 文件自动解析脚本定义

        Args:
            script_path: Python 脚本文件路径

        Returns:
            ScriptDef: 解析后的脚本定义
        """
        name = os.path.splitext(os.path.basename(script_path))[0]

        with open(script_path, 'r', encoding='utf-8') as f:
            source = f.read()

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            raise ValueError(f"脚本语法错误 {script_path}: {e}")

        docstring = ast.get_docstring(tree) or ""

        # 解析特殊注释元数据
        http_config = cls._parse_http_config(source)
        comment_deps = cls._parse_comment_deps(source)
        execution_mode = "http_request" if http_config else "local_execution"

        # 解析 execute 函数的参数签名
        params = {"type": "object", "properties": {}, "required": []}
        has_execute = False
        response_formatter = None

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "execute":
                has_execute = True
                for arg in node.args.args:
                    arg_name = arg.arg
                    arg_type = "string"
                    if arg.annotation:
                        type_str = ast.unparse(arg.annotation)
                        arg_type = cls._map_type(type_str)
                    params["properties"][arg_name] = {
                        "type": arg_type,
                        "description": ""
                    }
                    # 没有默认值的参数为必填
                    defaults_count = len(node.args.defaults)
                    required_count = len(node.args.args) - defaults_count
                    if node.args.args.index(arg) < required_count:
                        params["required"].append(arg_name)
                break
            elif isinstance(node, ast.FunctionDef) and node.name == "format_response":
                # 提取 response_formatter 函数源码
                response_formatter = ast.get_source_segment(source, node)

        if not has_execute:
            raise ValueError(f"脚本 {script_path} 缺少 execute() 函数")

        # 自动提取第三方库依赖
        auto_deps = cls._extract_deps(tree)
        all_deps = sorted(set(auto_deps + comment_deps))

        return cls(
            name=name,
            description=docstring.strip(),
            source=source,
            parameters=params,
            execution_mode=execution_mode,
            dependencies=all_deps,
            http_config=http_config,
            response_formatter=response_formatter,
            path=script_path
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ScriptDef":
        """从字典创建（用于从数据库加载用户脚本）"""
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            source=data.get("source", ""),
            parameters=data.get("parameters", {"type": "object", "properties": {}, "required": []}),
            execution_mode=data.get("execution_mode", "local_execution"),
            dependencies=data.get("dependencies", []),
            http_config=data.get("http_config", {}),
            response_formatter=data.get("response_formatter"),
            path=""
        )

    @staticmethod
    def _parse_http_config(source: str) -> dict:
        """从脚本注释中解析 HTTP_CONFIG 元数据

        格式：
        # HTTP_CONFIG:
        #   url: https://api.example.com/endpoint?param={param}
        #   method: GET
        """
        config = {}
        in_config = False
        for line in source.split('\n'):
            stripped = line.strip()
            if stripped.startswith('# HTTP_CONFIG:'):
                in_config = True
                continue
            if in_config:
                if stripped.startswith('# ') and ':' in stripped:
                    kv = stripped[2:].split(':', 1)
                    key = kv[0].strip()
                    value = kv[1].strip() if len(kv) > 1 else ""
                    config[key] = value
                elif not stripped.startswith('#'):
                    break
        return config

    @staticmethod
    def _parse_comment_deps(source: str) -> list:
        """从脚本注释中解析 DEPENDENCIES 元数据

        格式：
        # DEPENDENCIES: openpyxl, python-docx
        """
        for line in source.split('\n'):
            stripped = line.strip()
            if stripped.startswith('# DEPENDENCIES:'):
                deps_str = stripped.split(':', 1)[1].strip()
                return [d.strip() for d in deps_str.split(',') if d.strip()]
        return []

    @staticmethod
    def _map_type(type_str: str) -> str:
        """Python 类型映射到 JSON Schema 类型"""
        type_map = {
            "str": "string", "int": "integer", "float": "number",
            "bool": "boolean", "list": "array", "dict": "object",
            "None": "null"
        }
        return type_map.get(type_str, "string")

    @staticmethod
    def _extract_deps(tree: ast.AST) -> list:
        """从 AST 中提取第三方库依赖（排除标准库）"""
        stdlib = {
            "json", "os", "sys", "re", "math", "datetime", "io", "base64",
            "hashlib", "csv", "random", "string", "urllib", "tempfile",
            "zipfile", "itertools", "functools", "collections", "typing",
            "copy", "textwrap", "uuid", "html", "xml", "struct", "binascii",
            "decimal", "fractions", "statistics", "pathlib", "enum", "time",
            "contextlib", "dataclasses", "pprint", "traceback", "warnings",
            "inspect", "abc", "atexit", "gc", "logging", "subprocess",
            "threading", "multiprocessing", "asyncio", "socket", "ssl",
            "email", "http", "unittest", "doctest", "argparse", "getopt",
            "getpass", "curses", "platform", "signal", "mmap", "glob",
            "fnmatch", "linecache", "pickle", "shelve", "marshal", "dbm",
            "sqlite3", "bz2", "gzip", "lzma", "tarfile", "configparser",
            "netrc", "plistlib", "shlex", "hmac", "secrets", "ipaddress",
            "wave", "colorsys", "difflib", "unicodedata", "stringprep",
            "codecs", "dis", "symtable", "token", "keyword", "tokenize",
            "tabnanny", "py_compile", "compileall", "pkgutil", "modulefinder",
            "runpy", "importlib", "pydoc", "bdb", "faulthandler", "pdb",
            "profile", "timeit", "trace", "tracemalloc", "venv", "zipapp",
            "zipimport", "webbrowser", "calendar", "gettext", "locale",
        }
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    pkg = alias.name.split('.')[0]
                    if pkg not in stdlib:
                        deps.add(pkg)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    pkg = node.module.split('.')[0]
                    if pkg not in stdlib:
                        deps.add(pkg)
        return sorted(deps)

    def to_openai_tool(self) -> dict:
        """转为 OpenAI function calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }

    def to_dict(self) -> dict:
        """转为可序列化的字典（用于数据库存储）"""
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "parameters": self.parameters,
            "execution_mode": self.execution_mode,
            "dependencies": self.dependencies,
            "http_config": self.http_config,
            "response_formatter": self.response_formatter,
        }


class Skill:
    """单个技能定义

    一个 Skill 对应一个文件夹，包含：
    - SKILL.md：YAML frontmatter（name, description）+ Markdown 指令
    - scripts/：可执行 Python 脚本
    """

    def __init__(self, name, description, instructions, path="",
                 scripts=None, is_system=True, skill_id=None):
        self.name = name
        self.description = description
        self.instructions = instructions  # SKILL.md 正文（不含 frontmatter）
        self.path = path
        self.scripts = scripts or []
        self.is_system = is_system  # 系统内置 vs 用户创建
        self.skill_id = skill_id   # 数据库 ID（用户技能）

    @classmethod
    def from_folder(cls, skill_path: str) -> "Skill":
        """从文件夹加载技能

        Args:
            skill_path: 技能文件夹路径

        Returns:
            Skill: 加载后的技能定义
        """
        skill_md = os.path.join(skill_path, "SKILL.md")
        if not os.path.isfile(skill_md):
            raise ValueError(f"技能文件夹缺少 SKILL.md: {skill_path}")

        with open(skill_md, 'r', encoding='utf-8') as f:
            content = f.read()

        # 解析 YAML frontmatter
        name = os.path.basename(skill_path)
        description = ""
        fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if fm_match:
            fm = fm_match.group(1)
            for line in fm.split('\n'):
                line = line.strip()
                if line.startswith('name:'):
                    name = line.split(':', 1)[1].strip()
                elif line.startswith('description:'):
                    description = line.split(':', 1)[1].strip()
            instructions = content[fm_match.end():].strip()
        else:
            instructions = content.strip()

        # 发现脚本
        scripts = []
        scripts_dir = os.path.join(skill_path, "scripts")
        if os.path.isdir(scripts_dir):
            for fname in sorted(os.listdir(scripts_dir)):
                if fname.endswith(".py") and not fname.startswith("_"):
                    try:
                        script = ScriptDef.from_file(
                            os.path.join(scripts_dir, fname)
                        )
                        scripts.append(script)
                    except ValueError as e:
                        logger.warning(f"跳过脚本 {fname}: {e}")

        return cls(
            name=name, description=description,
            instructions=instructions, path=skill_path,
            scripts=scripts, is_system=True
        )

    @classmethod
    def from_db_row(cls, row: dict) -> "Skill":
        """从数据库行加载用户技能"""
        name = row["skill_name"]
        content = row["skill_content"]

        # 解析 YAML frontmatter
        description = ""
        fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if fm_match:
            fm = fm_match.group(1)
            for line in fm.split('\n'):
                line = line.strip()
                if line.startswith('description:'):
                    description = line.split(':', 1)[1].strip()
            instructions = content[fm_match.end():].strip()
        else:
            instructions = content.strip()

        # 解析用户脚本
        scripts = []
        try:
            user_scripts = json.loads(row.get("skill_scripts", "[]"))
            for s in user_scripts:
                scripts.append(ScriptDef.from_dict(s))
        except (json.JSONDecodeError, TypeError):
            pass

        return cls(
            name=name, description=description,
            instructions=instructions, path="",
            scripts=scripts, is_system=False,
            skill_id=row.get("id")
        )

    def build_context(self) -> str:
        """构建技能上下文（注入系统提示词）"""
        lines = [f"## {self.name}"]
        if self.description:
            lines.append(f"*{self.description}*")
        lines.append("")
        lines.append(self.instructions)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """转为可序列化的字典"""
        return {
            "id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "is_system": self.is_system,
            "scripts_count": len(self.scripts),
            "script_names": [s.name for s in self.scripts],
        }


class SkillRegistry:
    """技能注册中心

    管理所有技能（系统内置 + 用户创建），负责：
    - 加载技能文件夹
    - 管理用户技能
    - 构建技能上下文（注入系统提示词）
    - 获取所有脚本（注册为工具）
    """

    def __init__(self):
        self.skills = {}          # name → Skill（系统技能）
        self._user_skills = {}    # user_id → {name → Skill}（用户技能）

    # ---- 加载 ----

    def load_system_skills(self, skills_dir: str) -> list:
        """加载系统内置技能

        Args:
            skills_dir: 技能文件夹根目录

        Returns:
            list: 成功加载的技能名称列表
        """
        loaded = []
        if not os.path.isdir(skills_dir):
            logger.warning(f"技能目录不存在: {skills_dir}")
            return loaded

        for folder in sorted(os.listdir(skills_dir)):
            skill_path = os.path.join(skills_dir, folder)
            if not os.path.isdir(skill_path) or folder.startswith("__") or folder == "user":
                continue
            try:
                skill = Skill.from_folder(skill_path)
                self.skills[skill.name] = skill
                loaded.append(skill.name)
                logger.info(f"已加载系统技能: {skill.name} ({len(skill.scripts)} 个脚本)")
            except ValueError as e:
                logger.warning(f"跳过 {folder}: {e}")

        return loaded

    def load_user_skills(self, user_id: int, skills_data: list):
        """加载用户自建技能（从数据库）

        Args:
            user_id: 用户 ID
            skills_data: 数据库查询结果列表
        """
        user_skills = {}
        for row in skills_data:
            if not row.get("enabled", True):
                continue
            try:
                skill = Skill.from_db_row(row)
                user_skills[skill.name] = skill
            except Exception as e:
                logger.warning(f"加载用户技能失败: {e}")

        # 合并已有的文件系统技能
        if user_id in self._user_skills:
            existing = self._user_skills[user_id]
            existing.update(user_skills)
            self._user_skills[user_id] = existing
        else:
            self._user_skills[user_id] = user_skills

    def load_user_skills_from_fs(self, user_id: int) -> list:
        """从文件系统加载用户自定义 Skill（agent/skills/user/{user_id}/）

        用户可直接编辑目录下的 SKILL.md 和 scripts/ 文件，
        系统启动时或在 Skill 变更后通过此方法加载。

        Args:
            user_id: 用户 ID

        Returns:
            list: 成功加载的技能名称列表
        """
        user_dir = os.path.join(os.path.dirname(__file__), "skills", "user", str(user_id))
        if not os.path.isdir(user_dir):
            return []

        loaded = []
        for folder in sorted(os.listdir(user_dir)):
            skill_path = os.path.join(user_dir, folder)
            if not os.path.isdir(skill_path):
                continue
            try:
                skill = Skill.from_folder(skill_path)
                skill.is_system = False  # 用户技能标记

                if user_id not in self._user_skills:
                    self._user_skills[user_id] = {}
                self._user_skills[user_id][skill.name] = skill
                loaded.append(skill.name)
                logger.info(f"已加载用户技能(user={user_id}): {skill.name} ({len(skill.scripts)} 个脚本)")
            except ValueError as e:
                logger.warning(f"跳过用户技能 {folder}: {e}")

        return loaded

    # ---- 查询 ----

    def get_system_skill_names(self) -> list:
        """获取所有系统技能名称"""
        return sorted(self.skills.keys())

    def get_system_skill(self, name: str) -> Skill:
        """获取指定系统技能"""
        return self.skills.get(name)

    def get_user_skills(self, user_id: int) -> dict:
        """获取指定用户的所有技能"""
        return self._user_skills.get(user_id, {})

    def get_enabled_skills(self, user_id: int = None,
                           enabled_system: set = None) -> dict:
        """获取所有启用的技能（系统 + 用户）

        Args:
            user_id: 用户 ID，用于加载用户技能
            enabled_system: 启用的系统技能名称集合，None 表示全部启用

        Returns:
            dict: {name: Skill}
        """
        result = {}

        # 系统技能
        for name, skill in self.skills.items():
            if enabled_system is None or name in enabled_system:
                result[name] = skill

        # 用户技能
        if user_id and user_id in self._user_skills:
            result.update(self._user_skills[user_id])

        return result

    def get_all_scripts(self, user_id: int = None,
                        enabled_system: set = None) -> list:
        """获取所有启用技能的脚本列表

        Args:
            user_id: 用户 ID
            enabled_system: 启用的系统技能名称集合

        Returns:
            list: ScriptDef 列表
        """
        scripts = []
        skills = self.get_enabled_skills(user_id, enabled_system)
        for skill in skills.values():
            for script in skill.scripts:
                scripts.append(script)
        return scripts

    # ---- 上下文构建 ----

    def build_context(self, user_id: int = None,
                      enabled_system: set = None) -> str:
        """构建技能上下文，用于注入系统提示词

        Args:
            user_id: 用户 ID
            enabled_system: 启用的系统技能名称集合

        Returns:
            str: 技能上下文字符串
        """
        skills = self.get_enabled_skills(user_id, enabled_system)
        if not skills:
            return ""

        parts = ["\n# 可用技能\n"]
        parts.append("以下是你可以使用的专业技能。当用户任务匹配某技能描述时，"
                     "请遵循该技能的指令执行。\n")
        for skill in skills.values():
            parts.append(skill.build_context())
            parts.append("")

        return "\n".join(parts)

    # ---- 管理 ----

    def unregister(self, name: str) -> bool:
        """注销一个系统技能"""
        if name in self.skills:
            del self.skills[name]
            return True
        return False

    def clear_user_skills(self, user_id: int):
        """清除用户技能缓存"""
        self._user_skills.pop(user_id, None)