import os
import json
import subprocess
import sys
import tempfile
import threading
from agent.logger import get_logger

logger = get_logger("sandbox")


class ToolSandbox:
    """工具执行沙箱

    为工具提供隔离的执行环境：
    - 独立的虚拟环境（venv），所有依赖安装在其中，与宿主环境完全隔离
    - 工具代码在子进程中执行，崩溃不影响主服务
    - 超时保护，防止死循环
    - 参数通过 stdin 传入，结果通过 stdout 返回
    """

    def __init__(self, sandbox_dir: str = None):
        if sandbox_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            sandbox_dir = os.path.join(os.path.dirname(base_dir), "tool_sandbox")
        self.sandbox_dir = sandbox_dir
        self.venv_dir = os.path.join(sandbox_dir, "venv")
        self.venv_python = os.path.join(self.venv_dir, "bin", "python3")
        self._deps_installed = set()
        self._deps_file = os.path.join(sandbox_dir, ".installed_deps")
        self._ensure_venv()
        self._load_installed_deps()

    def _ensure_venv(self):
        os.makedirs(self.sandbox_dir, exist_ok=True)
        if os.path.exists(self.venv_python):
            return True
        try:
            subprocess.check_call(
                [sys.executable, "-m", "venv", self.venv_dir],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60
            )
            return True
        except subprocess.TimeoutExpired:
            logger.error(f"创建虚拟环境超时，请检查系统资源")
            logger.warning("工具执行功能将不可用，但对话功能不受影响")
            return False
        except Exception as e:
            logger.error(f"创建虚拟环境失败: {e}")
            logger.warning("提示：请确保 Python 已安装 venv 模块（python3 -m venv）")
            logger.warning("工具执行功能将不可用，但对话功能不受影响")
            return False

    def _load_installed_deps(self):
        if os.path.isfile(self._deps_file):
            try:
                with open(self._deps_file, "r") as f:
                    for line in f:
                        pkg = line.strip()
                        if pkg:
                            self._deps_installed.add(pkg)
            except Exception:
                pass

    def _save_installed_deps(self):
        try:
            with open(self._deps_file, "w") as f:
                for pkg in sorted(self._deps_installed):
                    f.write(pkg + "\n")
        except Exception:
            pass

    def _get_venv_installed_packages(self):
        if not os.path.exists(self.venv_python):
            return set()
        try:
            result = subprocess.run(
                [self.venv_python, "-m", "pip", "list", "--format", "freeze"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                return set()
            packages = set()
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                pkg_name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("!=")[0].strip().lower()
                if pkg_name:
                    packages.add(pkg_name)
            return packages
        except Exception:
            return set()

    def install(self, packages: list):
        if not packages:
            return True
        to_install = [p for p in packages if p not in self._deps_installed]
        if not to_install:
            return True

        venv_packages = self._get_venv_installed_packages()
        if venv_packages:
            for pkg in list(to_install):
                if pkg.lower() in venv_packages:
                    self._deps_installed.add(pkg)
                    to_install.remove(pkg)
            self._save_installed_deps()

        if not to_install:
            return True

        if not os.path.exists(self.venv_python):
            if not self._ensure_venv():
                logger.error(f"虚拟环境不可用，无法安装依赖")
                return False
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "install", "--no-cache-dir", "-q"] + to_install,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120
            )
        except subprocess.TimeoutExpired:
            logger.error(f"pip install 超时: {to_install}")
            return False
        except Exception as e:
            logger.error(f"pip install 失败: {e}")
            return False
        for p in to_install:
            self._deps_installed.add(p)
        self._save_installed_deps()
        return True

    def install_verbose(self, packages: list):
        if not packages:
            return
        to_install = [p for p in packages if p not in self._deps_installed]
        if not to_install:
            return

        venv_packages = self._get_venv_installed_packages()
        pre_existing = set()
        if venv_packages:
            for pkg in list(to_install):
                if pkg.lower() in venv_packages:
                    self._deps_installed.add(pkg)
                    pre_existing.add(pkg)
                    to_install.remove(pkg)
            self._save_installed_deps()

        if not to_install:
            return

        if not os.path.exists(self.venv_python):
            if not self._ensure_venv():
                logger.error("虚拟环境不可用，无法安装依赖")
                return
        logger.info(f"安装依赖: {', '.join(to_install)}")
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "install", "--no-cache-dir"] + to_install,
                timeout=120
            )
        except subprocess.TimeoutExpired:
            logger.error(f"pip install 超时: {to_install}")
            self._cleanup_failed_install(to_install, pre_existing)
            raise
        except Exception as e:
            logger.error(f"pip install 失败: {e}")
            self._cleanup_failed_install(to_install, pre_existing)
            raise
        logger.info("依赖安装完成")
        for p in to_install:
            self._deps_installed.add(p)
        self._save_installed_deps()

    def uninstall(self, packages: list):
        if not packages:
            return
        to_remove = [p for p in packages if p in self._deps_installed]
        if not to_remove:
            return
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "uninstall", "-y", "-q"] + to_remove,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30
            )
        except Exception:
            pass
        for p in to_remove:
            self._deps_installed.discard(p)
        self._save_installed_deps()

    def _cleanup_failed_install(self, packages: list, pre_existing: set = None):
        if pre_existing:
            packages = [p for p in packages if p not in pre_existing]
        if not packages:
            return
        logger.warning(f"清理安装失败的依赖: {', '.join(packages)}")
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "uninstall", "-y", "-q"] + packages,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30
            )
            logger.info("清理完成")
        except Exception:
            logger.warning("清理失败，可能需要手动清理")

    def execute(self, code: str, params: dict, timeout: int = 30, user_id: int = None) -> str:
        if user_id is not None:
            # 替换所有引用 document_output 的路径，在 document_output 后插入 /{user_id}
            code = code.replace("'document_output/'", f"'document_output/{user_id}/'")
            code = code.replace('"document_output/"', f'"document_output/{user_id}/"')
            code = code.replace("'document_output'", f"'document_output/{user_id}'")
            code = code.replace('"document_output"', f'"document_output/{user_id}"')
            # 处理带文件名的路径：'document_output/xxx' → 'document_output/{user_id}/xxx'
            import re as _re
            code = _re.sub(r"('document_output)/([^'])", rf"'\1/{user_id}/\2", code)
            code = _re.sub(r'("document_output)/([^"])', rf'"\1/{user_id}/\2', code)
        wrapper = self._build_wrapper(code, params)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(base_dir)

        subprocess_env = {
            "PATH": os.path.dirname(self.venv_python) + ":" + os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", tempfile.gettempdir()),
            "TMPDIR": tempfile.gettempdir(),
            "TEMP": tempfile.gettempdir(),
            "TMP": tempfile.gettempdir(),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        }
        for key in ("USER", "LOGNAME", "SHELL"):
            if key in os.environ:
                subprocess_env[key] = os.environ[key]

        try:
            proc = subprocess.run(
                [self.venv_python, "-c", wrapper],
                capture_output=True, text=True, timeout=timeout,
                cwd=project_root,
                env=subprocess_env,
            )
            if proc.returncode != 0:
                stdout = proc.stdout.strip()
                stderr = proc.stderr.strip()
                parts = []
                if stdout:
                    parts.append(stdout[:800])
                if stderr:
                    parts.append(f"[系统错误] {stderr[:300]}")
                if not parts:
                    parts.append(f"退出码: {proc.returncode}")
                return f"[沙箱执行失败] {' | '.join(parts)}"
            return proc.stdout.strip()
        except subprocess.TimeoutExpired:
            return f"[沙箱执行超时] 工具执行超过 {timeout} 秒，已强制终止"
        except Exception as e:
            return f"[沙箱异常] {str(e)}"

    _SAFE_MODULES = {
        "json", "math", "datetime", "re", "base64", "hashlib",
        "tempfile", "csv", "io", "zipfile", "random", "string",
        "itertools", "functools", "collections", "typing", "copy",
        "textwrap", "uuid", "html", "xml", "struct", "binascii",
        "decimal", "fractions", "statistics",
    }
    _BLOCKED_MODULES = {
        "subprocess", "shutil", "ctypes", "socket",
        "http", "requests", "popen", "signal", "pty",
        "fcntl", "posix", "grp", "pwd", "spwd", "crypt",
        "multiprocessing", "asyncio", "select", "selectors", "ssl",
        "smtplib", "imaplib", "poplib", "ftplib", "telnetlib",
        "shelve", "marshal",
    }

    @staticmethod
    def _split_code_lines(code: str) -> list:
        lines = []
        current = []
        i = 0
        n = len(code)
        while i < n:
            ch = code[i]
            if ch == '\n':
                lines.append(''.join(current))
                current = []
                i += 1
                continue
            if ch in ('"', "'"):
                quote = ch
                current.append(ch)
                i += 1
                if i + 1 < n and code[i] == quote and code[i+1] == quote:
                    current.append(code[i])
                    current.append(code[i+1])
                    i += 2
                    while i + 2 < n and not (code[i] == quote and code[i+1] == quote and code[i+2] == quote):
                        if code[i] == '\\':
                            current.append(code[i])
                            i += 1
                            if i < n:
                                current.append(code[i])
                                i += 1
                        else:
                            current.append(code[i])
                            i += 1
                    if i + 2 < n:
                        current.append(code[i])
                        current.append(code[i+1])
                        current.append(code[i+2])
                        i += 3
                else:
                    while i < n and code[i] != quote:
                        if code[i] == '\\':
                            current.append(code[i])
                            i += 1
                            if i < n:
                                current.append(code[i])
                                i += 1
                        else:
                            current.append(code[i])
                            i += 1
                    if i < n:
                        current.append(code[i])
                        i += 1
            elif ch == '#':
                while i < n and code[i] != '\n':
                    current.append(code[i])
                    i += 1
            else:
                current.append(ch)
                i += 1
        if current:
            lines.append(''.join(current))
        return lines

    def _build_wrapper(self, code: str, params: dict) -> str:
        safe_imports = ", ".join(sorted(self._SAFE_MODULES))
        blocked_list = json.dumps(sorted(self._BLOCKED_MODULES))
        params_json = json.dumps(params, ensure_ascii=False)
        tmpdir = os.path.realpath(tempfile.gettempdir())

        wrapper = (
            f"import {safe_imports}\n"
            "import os as _os\n"
            f"_params = json.loads({json.dumps(params_json)})\n"
        )
        for key in params:
            wrapper += f"{key} = _params[{json.dumps(key)}]\n"

        from agent.tool_secrets import get_tool_secrets
        _secrets = get_tool_secrets()
        secrets_json = json.dumps(_secrets.get_all_raw(), ensure_ascii=False)
        wrapper += f"_TOOL_SECRETS = json.loads({json.dumps(secrets_json)})\n"

        wrapper += (
            f"_BLOCKED = set({blocked_list})\n"
            "import builtins\n"
            "_orig_import = builtins.__import__\n"
            "def _safe_import(name, *args, **kwargs):\n"
            "    root = name.split('.')[0]\n"
            "    if root in _BLOCKED:\n"
            "        raise ImportError(f'模块 {name} 已被沙箱禁用')\n"
            "    return _orig_import(name, *args, **kwargs)\n"
            "builtins.__import__ = _safe_import\n"
            # 预置 urllib.request mock，防止报告生成库(reportlab)导入时触发 http/socket 等网络模块加载
            "import sys as _sys\n"
            "class _MockUrllibRequest:\n"
            "    urlopen = None\n"
            "    Request = None\n"
            "    OpenerDirector = None\n"
            "    build_opener = None\n"
            "    install_opener = None\n"
            "    pathname2url = None\n"
            "    url2pathname = None\n"
            "    getproxies = None\n"
            "_sys.modules['urllib.request'] = _MockUrllibRequest()\n"
            "_orig_unlink = _os.unlink\n"
            "_orig_remove = _os.remove\n"
            "_DANGEROUS_OS = ['system', 'popen', 'execv', 'execve', 'spawnl', 'spawnle', 'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe', 'remove', 'rmdir', 'removedirs', 'renames', 'chmod', 'chown', 'link', 'symlink', 'kill', 'killpg', 'setuid', 'setgid', 'fork', 'forkpty', 'unlink']\n"
            "for _func in _DANGEROUS_OS:\n"
            "    if hasattr(_os, _func):\n"
            "        delattr(_os, _func)\n"
            f"_SAFE_UNLINK_DIRS = [\n"
            f"    _os.path.realpath(_os.path.join(_os.getcwd(), 'document_output')),\n"
            f"    _os.path.realpath({json.dumps(tmpdir)}),\n"
            "]\n"
            "def _safe_unlink(path):\n"
            "    real = _os.path.realpath(path)\n"
            "    for _allowed in _SAFE_UNLINK_DIRS:\n"
            "        if real == _allowed or real.startswith(_allowed + _os.sep):\n"
            "            return _orig_unlink(path)\n"
            "    raise PermissionError(f'[沙箱] 禁止删除此路径的文件: {path}')\n"
            "_os.unlink = _safe_unlink\n"
            "_os.remove = _safe_unlink\n"
            "os = _os\n"
        )

        indented_code = "\n".join(
            "    " + line if line.strip() else ""
            for line in self._split_code_lines(code.strip())
        )

        # 构建 execute() 调用参数
        call_args = ", ".join(f"{k}={k}" for k in params)
        execute_call = f"    result = execute({call_args})" if call_args else "    result = execute()"

        wrapper += (
            "try:\n"
            f"{indented_code}\n"
            f"{execute_call}\n"
            "    _out = str(locals().get('result', '代码执行完成但未找到 result 变量'))\n"
            "except Exception as _e:\n"
            "    import traceback as _tb\n"
            "    _tb_str = _tb.format_exc()\n"
            "    _tb_lines = _tb_str.strip().split('\\n')\n"
            "    _out = f'[工具执行异常] {type(_e).__name__}: {str(_e)}\\n\\n详细追踪:\\n' + '\\n'.join(_tb_lines[-6:])\n"
            "print(_out)\n"
        )
        return wrapper


class SandboxPool:
    """每用户独立沙箱池

    为每个用户创建独立的 ToolSandbox 实例，依赖环境完全隔离：
    - 目录结构：tool_sandbox/user_{user_id}/venv/
    - 懒加载：首次使用时创建，后续复用
    - 线程安全：加锁保护 _sandboxes 字典
    """

    def __init__(self, base_dir: str = None):
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "tool_sandbox")
            base_dir = os.path.abspath(base_dir)
        self.base_dir = base_dir
        self._sandboxes: dict[int, ToolSandbox] = {}
        self._lock = threading.Lock()

    def get(self, user_id: int) -> ToolSandbox:
        """获取或创建用户专属沙箱

        Args:
            user_id: 用户 ID

        Returns:
            ToolSandbox: 用户专属沙箱实例
        """
        with self._lock:
            if user_id not in self._sandboxes:
                sandbox_dir = os.path.join(self.base_dir, f"user_{user_id}")
                self._sandboxes[user_id] = ToolSandbox(sandbox_dir)
            return self._sandboxes[user_id]

    def remove(self, user_id: int):
        """移除用户沙箱（不删除磁盘文件）

        Args:
            user_id: 用户 ID
        """
        with self._lock:
            self._sandboxes.pop(user_id, None)