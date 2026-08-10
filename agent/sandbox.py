import os
import json
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse
from agent.logger import get_logger

logger = get_logger("sandbox")

# pip 安装配置（可通过环境变量覆盖）
# SANDBOX_PIP_INDEX 设为空串即使用 pypi 官方源
PIP_INDEX_URL = os.environ.get(
    "SANDBOX_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple"
)
PIP_INSTALL_TIMEOUT = int(os.environ.get("SANDBOX_PIP_TIMEOUT", "300"))


class ToolSandbox:
    """工具执行沙箱

    为工具提供隔离的执行环境：
    - 独立的虚拟环境（venv），所有依赖安装在其中，与宿主环境完全隔离
    - 工具代码在子进程中执行，崩溃不影响主服务
    - 超时保护，防止死循环
    - 参数通过 stdin 传入，结果通过 stdout 返回
    - 每用户独立日志文件（sandbox.log）
    """

    def __init__(self, sandbox_dir: str = None, user_id: int = 0):
        if sandbox_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            sandbox_dir = os.path.join(os.path.dirname(base_dir), "tool_sandbox")
        self.sandbox_dir = sandbox_dir
        self.user_id = user_id
        self.venv_dir = os.path.join(sandbox_dir, "venv")
        self.venv_python = os.path.join(self.venv_dir, "bin", "python3")
        self._deps_installed = set()
        self._deps_file = os.path.join(sandbox_dir, ".installed_deps")
        self._exec_log_path = os.path.join(sandbox_dir, "sandbox.log")

        # 共享基础环境：用户沙箱（tool_sandbox/user_N）通过 PYTHONPATH 继承
        # tool_sandbox/venv 的依赖，避免每个用户重复 pip 安装常用库
        _norm = os.path.normpath(sandbox_dir)
        if os.path.basename(_norm).startswith("user_"):
            self.shared_venv_dir = os.path.join(os.path.dirname(_norm), "venv")
        else:
            self.shared_venv_dir = self.venv_dir
        self._shared_site_packages = None   # 懒加载缓存
        self._shared_packages = None        # 懒加载缓存

        self._ensure_venv()
        self._load_installed_deps()

    def get_shared_site_packages(self) -> str:
        """返回共享基础环境的 site-packages 路径（用户沙箱专用），无则返回空串"""
        if self._shared_site_packages is not None:
            return self._shared_site_packages
        path = ""
        if self.shared_venv_dir != self.venv_dir:
            import glob as _glob
            hits = _glob.glob(os.path.join(self.shared_venv_dir, "lib", "python*", "site-packages"))
            if hits:
                path = hits[0]
        self._shared_site_packages = path
        return path

    def _get_shared_packages(self) -> set:
        """列出共享基础环境中已安装的包名（小写），用于跳过重复安装"""
        if self._shared_packages is not None:
            return self._shared_packages
        packages = set()
        shared_python = os.path.join(self.shared_venv_dir, "bin", "python3")
        if self.shared_venv_dir != self.venv_dir and os.path.exists(shared_python):
            try:
                result = subprocess.run(
                    [shared_python, "-m", "pip", "list", "--format", "freeze"],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        name = line.strip().split("==")[0].strip().lower()
                        if name:
                            packages.add(name)
                            packages.add(name.replace("-", "_"))
            except Exception:
                pass
        self._shared_packages = packages
        return packages

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

        # 1) 共享基础环境已有的包，通过 PYTHONPATH 继承，无需安装
        shared_packages = self._get_shared_packages()
        if shared_packages:
            for pkg in list(to_install):
                if pkg.lower() in shared_packages:
                    self._deps_installed.add(pkg)
                    to_install.remove(pkg)
            self._save_installed_deps()

        if not to_install:
            return True

        # 2) 本沙箱 venv 已有的包
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

        logger.info(f"安装依赖到用户沙箱(user={self.user_id}): {', '.join(to_install)}")
        try:
            proc = subprocess.run(
                self._build_pip_command(to_install),
                capture_output=True, text=True,
                timeout=PIP_INSTALL_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.error(
                f"pip install 超时({PIP_INSTALL_TIMEOUT}s): {to_install} | "
                f"index={PIP_INDEX_URL or '默认'}"
            )
            return False
        except Exception as e:
            logger.error(f"pip install 异常: {to_install} | {e}")
            return False

        if proc.returncode != 0:
            # 关键：把 pip 的真实错误写进日志，便于定位（旧实现丢弃了 stderr）
            detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
            logger.error(f"pip install 失败: {to_install} | 退出码={proc.returncode} | {detail[:500]}")
            return False

        for p in to_install:
            self._deps_installed.add(p)
        self._save_installed_deps()
        return True

    def _build_pip_command(self, packages: list) -> list:
        """构建 pip install 命令（含镜像源与重试参数）

        镜像源可通过环境变量 SANDBOX_PIP_INDEX 覆盖，设为空串则使用 pypi 官方源。
        """
        cmd = [
            self.venv_python, "-m", "pip", "install",
            "--no-cache-dir", "-q", "--disable-pip-version-check",
            "--timeout", "30", "--retries", "2",
        ]
        if PIP_INDEX_URL:
            cmd += ["-i", PIP_INDEX_URL]
            host = urlparse(PIP_INDEX_URL).hostname
            if host:
                cmd += ["--trusted-host", host]
        return cmd + list(packages)

    def install_verbose(self, packages: list):
        if not packages:
            return
        to_install = [p for p in packages if p not in self._deps_installed]
        if not to_install:
            return

        pre_existing = set()
        # 共享基础环境已有的包，通过 PYTHONPATH 继承
        shared_packages = self._get_shared_packages()
        if shared_packages:
            for pkg in list(to_install):
                if pkg.lower() in shared_packages:
                    self._deps_installed.add(pkg)
                    pre_existing.add(pkg)
                    to_install.remove(pkg)
            self._save_installed_deps()

        venv_packages = self._get_venv_installed_packages()
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
                self._build_pip_command(to_install),
                timeout=PIP_INSTALL_TIMEOUT
            )
        except subprocess.TimeoutExpired:
            logger.error(f"pip install 超时({PIP_INSTALL_TIMEOUT}s): {to_install}")
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

    def _write_exec_log(self, tool_name: str, success: bool, duration_ms: float,
                        code_preview: str, result_preview: str, error_detail: str = ""):
        """写入每用户独立的沙箱执行日志

        Args:
            tool_name: 工具名称
            success: 是否执行成功
            duration_ms: 执行耗时（毫秒）
            code_preview: 代码预览（前100字符）
            result_preview: 结果预览（前200字符）
            error_detail: 错误详情（失败时）
        """
        import time as _time
        try:
            timestamp = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime())
            status = "SUCCESS" if success else "FAILED"
            entry = (
                f"[{timestamp}] [{status}] tool={tool_name} | "
                f"user={self.user_id} | duration={duration_ms:.0f}ms | "
                f"code={code_preview}\n"
            )
            if not success:
                entry += f"  error: {error_detail[:500]}\n"
            if result_preview:
                entry += f"  result: {result_preview[:300]}\n"
            entry += "-" * 60 + "\n"
            with open(self._exec_log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass  # 日志写入失败不应影响主流程

    @staticmethod
    def _rewrite_user_paths(code: str, user_id: int, tool_name: str = "") -> str:
        """将代码中的 document_output 路径重写为用户专属目录 document_output/{user_id}。

        实现要点：
        1. 单次正则替换，只匹配紧跟在引号后的 document_output，不会误伤变量名/注释。
        2. 幂等：通过负向前瞻跳过已含 /{user_id} 的路径，避免重复注入。
        3. 安全网：重写后做语法校验，若破坏了原本合法的代码则回退原始代码。

        Args:
            code: 待执行的工具源码
            user_id: 用户 ID
            tool_name: 工具名（仅用于日志）

        Returns:
            str: 重写后的代码；重写失败时返回原始代码
        """
        import re as _re

        # (?P<q>['"])  引号
        # (?!/{uid}(?:/|(?P=q)))  已重写过则跳过
        # (?P<tail>/|(?P=q))  后接路径分隔符或闭合引号，确保是完整的目录名
        pattern = _re.compile(
            rf"(?P<q>['\"])document_output(?!/{user_id}(?:/|(?P=q)))(?P<tail>/|(?P=q))"
        )
        rewritten = pattern.sub(
            lambda m: f"{m.group('q')}document_output/{user_id}{m.group('tail')}",
            code,
        )
        if rewritten == code:
            return code

        # 安全网：原本能编译却被改坏了，说明重写有问题，回退并告警
        try:
            compile(code, "<sandbox-origin>", "exec")
        except SyntaxError:
            return rewritten  # 原代码本身就有语法问题，交给后续流程报错
        try:
            compile(rewritten, "<sandbox-rewritten>", "exec")
        except SyntaxError as e:
            logger.error(
                f"用户路径重写破坏了代码语法，已回退: tool={tool_name or '未知'} | "
                f"user={user_id} | line={e.lineno} | msg={e.msg}"
            )
            return code
        return rewritten

    def execute(self, code: str, params: dict, timeout: int = 30, user_id: int = None, tool_name: str = "") -> str:
        import time as _time
        _start = _time.time()
        code_preview = code[:100].replace("\n", " ") + ("..." if len(code) > 100 else "")
        logger.info(f"执行工具: {tool_name or '未知'} | user={user_id} | code={code_preview}")
        if user_id is not None:
            code = self._rewrite_user_paths(code, user_id, tool_name)
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
        # 用户沙箱继承共享基础环境的依赖（只读共享，避免每用户重复安装）
        _shared_sp = self.get_shared_site_packages()
        if _shared_sp:
            subprocess_env["PYTHONPATH"] = _shared_sp
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
                    parts.append(f"[系统错误] {stderr[:500]}")
                if not parts:
                    parts.append(f"退出码: {proc.returncode}")
                result = f"[沙箱执行失败] {' | '.join(parts)}"
                logger.error(
                    f"沙箱执行失败: tool={tool_name or '未知'} | user={user_id} | "
                    f"returncode={proc.returncode} | stderr={stderr[:200]} | "
                    f"code={code[:200]}"
                )
                _elapsed = (_time.time() - _start) * 1000
                self._write_exec_log(tool_name or '未知', False, _elapsed,
                                     code_preview, result[:200], error_detail=stderr[:500])
                return result
            result = proc.stdout.strip()
            logger.debug(f"沙箱执行成功: tool={tool_name or '未知'} | user={user_id}")
            _elapsed = (_time.time() - _start) * 1000
            self._write_exec_log(tool_name or '未知', True, _elapsed,
                                 code_preview, result[:200])
            return result
        except subprocess.TimeoutExpired:
            result = f"[沙箱执行超时] 工具执行超过 {timeout} 秒，已强制终止"
            logger.warning(f"沙箱执行超时: tool={tool_name or '未知'} | user={user_id} | timeout={timeout}s")
            _elapsed = timeout * 1000
            self._write_exec_log(tool_name or '未知', False, _elapsed,
                                 code_preview, result[:200], error_detail=f"超时({timeout}s)")
            return result
        except Exception as e:
            result = f"[沙箱异常] {str(e)}"
            logger.error(f"沙箱异常: tool={tool_name or '未知'} | user={user_id} | error={str(e)}")
            _elapsed = (_time.time() - _start) * 1000
            self._write_exec_log(tool_name or '未知', False, _elapsed,
                                 code_preview, result[:200], error_detail=str(e))
            return result

    _SAFE_MODULES = {
        "json", "math", "datetime", "re", "base64", "hashlib",
        "tempfile", "csv", "io", "zipfile", "random", "string",
        "itertools", "functools", "collections", "typing", "copy",
        "textwrap", "uuid", "html", "xml", "struct", "binascii",
        "decimal", "fractions", "statistics",
    }
    _BLOCKED_MODULES = {
        "subprocess", "shutil", "ctypes",
        "requests", "popen", "signal", "pty",
        "fcntl", "posix", "grp", "pwd", "spwd", "crypt",
        "multiprocessing", "asyncio", "select", "selectors",
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
            # 注意：eval/exec/compile 是 Python 标准库和 import 系统的内部依赖，不可移除
            # 沙箱安全由其他 9 层防护保证（进程隔离、模块阻断、OS函数禁用、文件删除控制、内存限制、超时等）
            # 内存限制（Unix 系统，512MB）
            "try:\n"
            "    import resource as _resource\n"
            "    _limit = 512 * 1024 * 1024\n"
            "    _resource.setrlimit(_resource.RLIMIT_AS, (_limit, _limit))\n"
            "except (ImportError, ValueError):\n"
            "    pass\n"
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
                self._sandboxes[user_id] = ToolSandbox(sandbox_dir, user_id=user_id)
            return self._sandboxes[user_id]

    def remove(self, user_id: int):
        """移除用户沙箱（不删除磁盘文件）

        Args:
            user_id: 用户 ID
        """
        with self._lock:
            self._sandboxes.pop(user_id, None)