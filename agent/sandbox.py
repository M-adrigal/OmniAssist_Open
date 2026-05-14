import os
import json
import subprocess
import sys
import tempfile


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
        self._ensure_venv()

    def _ensure_venv(self):
        os.makedirs(self.sandbox_dir, exist_ok=True)
        if os.path.exists(self.venv_python):
            return
        subprocess.check_call(
            [sys.executable, "-m", "venv", self.venv_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=60
        )

    def install(self, packages: list):
        if not packages:
            return
        to_install = [p for p in packages if p not in self._deps_installed]
        if not to_install:
            return
        if not os.path.exists(self.venv_python):
            self._ensure_venv()
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "install", "--no-cache-dir", "-q"] + to_install,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            print(f"[沙箱] pip install 超时: {to_install}")
            raise
        except Exception as e:
            print(f"[沙箱] pip install 失败: {e}")
            raise
        for p in to_install:
            self._deps_installed.add(p)

    def install_verbose(self, packages: list):
        if not packages:
            return
        to_install = [p for p in packages if p not in self._deps_installed]
        if not to_install:
            print("所有依赖已安装，无需重复安装。")
            return
        if not os.path.exists(self.venv_python):
            self._ensure_venv()
        print(f"正在安装依赖: {', '.join(to_install)}")
        print("-" * 40)
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "install", "--no-cache-dir"] + to_install,
                timeout=120
            )
        except subprocess.TimeoutExpired:
            print("-" * 40)
            print(f"[沙箱] pip install 超时: {to_install}")
            self._cleanup_failed_install(to_install)
            raise
        except Exception as e:
            print("-" * 40)
            print(f"[沙箱] pip install 失败: {e}")
            self._cleanup_failed_install(to_install)
            raise
        print("-" * 40)
        print("依赖安装完成。")
        for p in to_install:
            self._deps_installed.add(p)

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

    def _cleanup_failed_install(self, packages: list):
        print(f"正在清理安装失败的依赖: {', '.join(packages)}")
        try:
            subprocess.check_call(
                [self.venv_python, "-m", "pip", "uninstall", "-y", "-q"] + packages,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30
            )
            print("清理完成。")
        except Exception:
            print("清理失败，可能需要手动清理。")

    def execute(self, code: str, params: dict, timeout: int = 30, user_id: int = None) -> str:
        if user_id is not None:
            code = code.replace("'document_output/'", f"'document_output/{user_id}/'")
            code = code.replace('"document_output/"', f'"document_output/{user_id}/"')
            code = code.replace("'document_output'", f"'document_output/{user_id}'")
            code = code.replace('"document_output"', f'"document_output/{user_id}"')
        wrapper = self._build_wrapper(code, params)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(base_dir)
        try:
            proc = subprocess.run(
                [self.venv_python, "-c", wrapper],
                capture_output=True, text=True, timeout=timeout,
                cwd=project_root,
                env={
                    "PATH": os.path.dirname(self.venv_python) + ":" + os.environ.get("PATH", ""),
                    "HOME": os.environ.get("HOME", tempfile.gettempdir()),
                    "TMPDIR": tempfile.gettempdir(),
                    "LANG": "en_US.UTF-8",
                }
            )
            if proc.returncode != 0:
                stderr = proc.stderr.strip()
                if stderr:
                    return f"[沙箱执行失败] {stderr[:500]}"
                return f"[沙箱执行失败] 退出码: {proc.returncode}"
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
        "http", "urllib", "requests", "popen", "signal", "pty",
        "fcntl", "posix", "grp", "pwd", "spwd", "crypt",
        "multiprocessing", "threading", "concurrent",
        "asyncio", "select", "selectors", "ssl", "email",
        "smtplib", "imaplib", "poplib", "ftplib", "telnetlib",
        "pickle", "shelve", "marshal",
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
        wrapper += (
            "try:\n"
            f"{indented_code}\n"
            "    _out = str(locals().get('result', '代码执行完成但未找到 result 变量'))\n"
            "except Exception as _e:\n"
            "    _out = f'[工具执行异常] {type(_e).__name__}: {str(_e)}'\n"
            "print(_out)\n"
        )
        return wrapper