"""Skill 安装风险扫描器 —— 用户上传技能链路的第一道门。

分级（与前端三态一一对应）：

| 级别 | 常量 | 安装行为 |
|---|---|---|
| P0 | BLOCK | 命中即拒绝安装；用户点「确认」也绕不过去 |
| P1 | CONFIRM | 需用户在安装弹窗里明确勾选确认后才写入 |
| P2 | PASS | 无命中，直接安装 |

**定位说明（改规则前必读）**：扫描只是第一道门，不是安全边界本身。
所有上传技能的脚本最终仍在沙箱内执行（模块黑名单 / 危险函数删除 /
目录白名单 / 子进程隔离，见 agent/sandbox.py）。扫描的价值在于
① 安装前把「明显没打算好好用」的包挡在门外；② 把可疑项摊开给用户看，
让「我了解风险」这个动作有依据。**不要试图在这里做到 100% 判别** ——
AST 与字符串规则拦得住脚本小子的顺手包，拦不住认真写的恶意包。

规则实现方式：AST 拿 import / 调用名（稳），正则拿字符串常量里的
路径与域名（必须的补充，因为路径往往是拼出来的）。
"""

import ast
import re
from dataclasses import dataclass, field
from typing import List, Optional

# ===== 分级常量 =====
BLOCK = "block"        # P0 重大风险：直接拦截
CONFIRM = "confirm"    # P1 敏感操作：需用户确认
PASS = "pass"          # P2 低风险：放行

# ===== 词表 =====

# 主进程执行元数据（内部约定关键字）：含这两类的脚本不走沙箱，
# 在服务主进程里执行 = 任意代码执行 + 以服务身份发请求。
# 历史约定见 server/routes/skills.py::_requires_admin_privilege。
_MAIN_PROCESS_MARKERS = ("http_config", "response_formatter")

# 高危路径/密钥字样：出现在「脚本代码」里即视为窃取意图。
# 注意不含宽泛的 api_key/secret —— 这两个词在正常说明文档里太常见。
_SECRET_TOKENS = (
    "data/users.db", "users.db", "password_hash", "/etc/shadow",
    "/etc/passwd", "id_rsa", "authorized_keys", ".ssh/",
    ".env", "private_key", "BEGIN RSA PRIVATE KEY",
)

# 窄词表：出现在 SKILL.md（给模型的指令）里也算高危 —— 指令同样能
# 诱导模型用文件工具去读这些位置，不只是脚本自己能读。
_SECRET_TOKENS_INSTRUCTIONS = (
    "data/users.db", "password_hash", "/etc/shadow", "id_rsa",
    "authorized_keys", "BEGIN RSA PRIVATE KEY",
)

# 持久化/开机自启：装个技能不该往系统里扎根。
_PERSIST_TOKENS = (
    "crontab", "launchctl", "LaunchAgents", "LaunchDaemons",
    "systemctl", "~/.zshrc", "~/.bashrc", "~/.bash_profile",
    "/etc/rc.local", "autorun",
)

# 网络库：单独出现是 P1（确认），与敏感字样组合出现升级为 P0。
_NET_MODULES = {
    "requests", "urllib", "urllib2", "urllib3", "http", "httpx",
    "socket", "ftplib", "smtplib", "telnetlib", "paramiko",
    "aiohttp", "websocket", "websockets", "scapy", "curl",
}

# 子进程/命令执行库：P1。
_SUBPROCESS_MODULES = {"subprocess", "commands", "pty", "multiprocessing"}
_SUBPROCESS_CALLS = {"system", "popen", "execv", "execve", "execvp", "spawnl", "spawnv"}

# 动态执行：P1。
_DYNAMIC_CALLS = {"eval", "exec", "compile", "__import__"}

# 写文件相关调用：P1。
_WRITE_CALLS = {
    "remove", "unlink", "rmdir", "removedirs", "rename", "replace",
    "makedirs", "mkdir", "rmtree", "move", "copyfile", "copytree",
    "chmod", "chown", "truncate", "touch",
}

# 内置/常用只读库白名单：命中这些的 import 不提级。
# 设计原则：能算数、能解析、能读文件，但不能联网、不能起进程、不能删东西。
_SAFE_MODULES = {
    "json", "math", "re", "regex", "datetime", "time", "calendar",
    "collections", "itertools", "functools", "operator", "random",
    "string", "textwrap", "unicodedata", "hashlib", "base64", "binascii",
    "csv", "statistics", "typing", "dataclasses", "enum", "copy", "decimal",
    "fractions", "difflib", "pprint", "uuid", "zoneinfo", "locale",
    "numbers", "ast", "pathlib", "io", "os", "sys", "path",
    "argparse", "logging", "traceback", "inspect", "abc", "contextlib",
    "warnings", "weakref", "graphlib", "secrets", "tempfile", "glob",
    "fnmatch", "linecache", "shlex", "struct", "codecs", "html",
    "xml", "configparser", "tomllib", "gzip", "zipfile", "tarfile",
    "sqlite3", "numpy", "pandas", "matplotlib", "pillow", "PIL",
    "openpyxl", "docx", "pptx", "fitz", "pytesseract", "lxml",
    "bs4", "yaml", "markdown", "dateutil", "pytz",
}

# 路径逃逸：脚本里出现即视为意图读写技能目录之外（P1）。
_ESCAPE_TOKENS = ("../", "/Users/", "/etc/", "/var/", "/tmp/", "/private/",
                  "/Library/", "/System/", "/root/", "/home/")


@dataclass
class Finding:
    """一条扫描命中。file/line 可能为空（如 SKILL.md 级规则）。"""
    rule: str
    severity: str            # BLOCK / CONFIRM
    message: str             # 给用户看的一句话（中文）
    file: str = ""
    line: int = 0
    detail: str = ""         # 命中内容摘要（截断，防泄露整文件）

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "severity": self.severity,
            "message": self.message, "file": self.file,
            "line": self.line, "detail": self.detail,
        }


@dataclass
class ScanResult:
    level: str = PASS
    findings: List[Finding] = field(default_factory=list)

    def add(self, f: Finding):
        self.findings.append(f)

    def to_dict(self) -> dict:
        blocked = [f for f in self.findings if f.severity == BLOCK]
        confirmed = [f for f in self.findings if f.severity == CONFIRM]
        level = BLOCK if blocked else (CONFIRM if confirmed else PASS)
        summary = {
            BLOCK: "该技能包含高风险操作，已阻止安装",
            CONFIRM: "该技能包含敏感操作，需要你确认后安装",
            PASS: "未发现敏感操作",
        }[level]
        return {
            "level": level,
            "summary": summary,
            "blocked_count": len(blocked),
            "confirm_count": len(confirmed),
            "findings": [f.to_dict() for f in self.findings],
        }


def _clip(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _iter_strings(tree: ast.AST):
    """遍历源码里所有字符串常量，附带行号。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _iter_calls(tree: ast.AST):
    """遍历所有函数调用名（Name 或 Attribute.attr 形态都取最后一段）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            yield node.lineno, func.id
        elif isinstance(func, ast.Attribute):
            yield node.lineno, func.attr


def _iter_imports(tree: ast.AST):
    """遍历所有 import 的模块根名（import a.b.c → a）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0], alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.lineno, node.module.split(".")[0], node.module


def scan_python(source: str, filename: str = "script.py") -> List[Finding]:
    """扫描单个 Python 脚本，返回命中列表（不含分级结论）。"""
    findings: List[Finding] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        # 语法错误不是「恶意」，但装不上也跑不起来，交给格式校验层报。
        # 这里只记一条信息，由调用方决定是否阻断（skill_registry 会跳过它）。
        return findings

    imports = list(_iter_imports(tree))
    calls = list(_iter_calls(tree))
    strings = list(_iter_strings(tree))
    imported_roots = {root for _, root, _ in imports}

    # ---- P0：主进程执行元数据 ----
    for marker in _MAIN_PROCESS_MARKERS:
        if marker in source:
            findings.append(Finding(
                rule="P0_MAIN_PROCESS_EXEC", severity=BLOCK,
                message=f"脚本含主进程执行标记「{marker}」，该能力仅限管理员使用",
                file=filename, detail=marker,
            ))

    # ---- P0：敏感路径/密钥 ----
    for lineno, text in strings:
        for token in _SECRET_TOKENS:
            if token in text:
                findings.append(Finding(
                    rule="P0_SECRET_ACCESS", severity=BLOCK,
                    message=f"脚本引用了敏感路径或凭证「{token}」",
                    file=filename, line=lineno, detail=_clip(text),
                ))
                break  # 一行报一次就够

    # ---- P0：网络 + 敏感组合（外传特征）----
    has_net = bool(imported_roots & _NET_MODULES)
    if has_net:
        for lineno, text in strings:
            if any(t in text for t in _SECRET_TOKENS):
                findings.append(Finding(
                    rule="P0_EXFIL_COMBO", severity=BLOCK,
                    message="脚本同时具备网络能力与敏感数据访问，存在数据外传特征",
                    file=filename, line=lineno, detail=_clip(text),
                ))
                break

    # ---- P0：持久化 ----
    for lineno, text in strings:
        if any(t in text for t in _PERSIST_TOKENS):
            findings.append(Finding(
                rule="P0_PERSISTENCE", severity=BLOCK,
                message="脚本包含系统持久化/开机自启相关内容",
                file=filename, line=lineno, detail=_clip(text),
            ))
            break

    # ---- P1：子进程 ----
    for lineno, root, full in imports:
        if root in _SUBPROCESS_MODULES:
            findings.append(Finding(
                rule="P1_SUBPROCESS", severity=CONFIRM,
                message=f"脚本会调用外部命令（import {full}）",
                file=filename, line=lineno,
            ))
    for lineno, name in calls:
        if name in _SUBPROCESS_CALLS:
            findings.append(Finding(
                rule="P1_SUBPROCESS", severity=CONFIRM,
                message=f"脚本会执行外部命令（{name}）",
                file=filename, line=lineno,
            ))

    # ---- P1：网络 ----
    for lineno, root, full in imports:
        if root in _NET_MODULES:
            findings.append(Finding(
                rule="P1_NETWORK", severity=CONFIRM,
                message=f"脚本会访问网络（import {full}）",
                file=filename, line=lineno,
            ))

    # ---- P1：动态执行 ----
    for lineno, name in calls:
        if name in _DYNAMIC_CALLS:
            findings.append(Finding(
                rule="P1_DYNAMIC_EXEC", severity=CONFIRM,
                message=f"脚本会动态执行代码（{name}）",
                file=filename, line=lineno,
            ))

    # ---- P1：写文件 ----
    for lineno, name in calls:
        if name in _WRITE_CALLS:
            findings.append(Finding(
                rule="P1_FILE_WRITE", severity=CONFIRM,
                message=f"脚本会创建/修改/删除文件（{name}）",
                file=filename, line=lineno,
            ))
    # open(..., 'w'/'a'/'x') 写模式
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = ""
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value or "")
            if any(m in mode for m in ("w", "a", "x", "+")):
                findings.append(Finding(
                    rule="P1_FILE_WRITE", severity=CONFIRM,
                    message=f"脚本会写文件（open 模式 {mode or '写'}）",
                    file=filename, line=node.lineno,
                ))

    # ---- P1：路径逃逸 ----
    for lineno, text in strings:
        if any(t in text for t in _ESCAPE_TOKENS):
            findings.append(Finding(
                rule="P1_PATH_ESCAPE", severity=CONFIRM,
                message="脚本引用了技能目录之外的路径",
                file=filename, line=lineno, detail=_clip(text),
            ))
            break

    # ---- P1：非常备依赖 ----
    for lineno, root, full in imports:
        if root in _SAFE_MODULES or root in _NET_MODULES or root in _SUBPROCESS_MODULES:
            continue  # 已由其它规则覆盖或在白名单内
        if root.startswith("_") or root in ("agent", "server"):
            findings.append(Finding(
                rule="P0_INTERNAL_IMPORT", severity=BLOCK,
                message=f"脚本试图引用平台内部模块 {full}",
                file=filename, line=lineno,
            ))
            continue
        findings.append(Finding(
            rule="P1_UNKNOWN_DEP", severity=CONFIRM,
            message=f"脚本依赖第三方库「{full}」，将在沙箱内安装",
            file=filename, line=lineno,
        ))

    return findings


def scan_instructions(content: str, filename: str = "SKILL.md") -> List[Finding]:
    """扫描 SKILL.md 指令正文（指令能诱导模型用工具做坏事，不只是脚本）。"""
    findings: List[Finding] = []
    for token in _SECRET_TOKENS_INSTRUCTIONS:
        if token in content:
            findings.append(Finding(
                rule="P0_SECRET_ACCESS", severity=BLOCK,
                message=f"技能指令中引用了敏感路径或凭证「{token}」",
                file=filename, detail=token,
            ))
    for marker in _MAIN_PROCESS_MARKERS:
        if marker in content:
            findings.append(Finding(
                rule="P0_MAIN_PROCESS_EXEC", severity=BLOCK,
                message=f"技能指令中引用了主进程执行标记「{marker}」",
                file=filename, detail=marker,
            ))
    return findings


def scan_skill_dir(root: str, py_files: Optional[List[str]] = None) -> ScanResult:
    """扫描整个技能目录：SKILL.md + scripts/*.py。

    Args:
        root: 技能目录（含 SKILL.md）
        py_files: 限定扫描的 .py 相对路径；None 则扫 scripts/ 下全部 .py
    """
    import os
    result = ScanResult()

    skill_md = os.path.join(root, "SKILL.md")
    if os.path.isfile(skill_md):
        try:
            with open(skill_md, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            for finding in scan_instructions(content):
                result.add(finding)
        except OSError:
            pass

    if py_files is None:
        py_files = []
        scripts_dir = os.path.join(root, "scripts")
        if os.path.isdir(scripts_dir):
            for dirpath, _dirs, files in os.walk(scripts_dir):
                for fn in files:
                    if fn.endswith(".py"):
                        rel = os.path.relpath(os.path.join(dirpath, fn), root)
                        py_files.append(rel)

    for rel in py_files:
        path = os.path.join(root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue
        for finding in scan_python(source, filename=rel.replace(os.sep, "/")):
            result.add(finding)

    # 分级结论（以 findings 为准，level 在 to_dict 时计算）
    return result
