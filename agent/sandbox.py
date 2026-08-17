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
    - 共享基础环境按 Python 小版本分桶（tool_sandbox/shared/pyX.Y/venv），
      用户沙箱通过 PYTHONPATH 继承同版本依赖，避免跨版本注入 C 扩展导致崩溃
    - 工具代码在子进程中执行，崩溃不影响主服务
    - 超时保护，防止死循环
    - 参数通过 stdin 传入，结果通过 stdout 返回
    - 每用户独立日志文件（sandbox.log）
    """

    # 已尝试懒构建共享 venv 的 Python 版本集合（每个版本仅尝试一次，避免热路径重复构建）
    _shared_build_attempted = set()

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
        # tool_sandbox/shared/pyX.Y/venv 的依赖（按 Python 小版本分桶，
        # 避免 3.14 的 C 扩展 .so 注入到 3.9 解释器导致 ImportError）。
        # 旧布局 tool_sandbox/venv 仍兼容，但仅当 Python 小版本一致时回退。
        _norm = os.path.normpath(sandbox_dir)
        _base = os.path.dirname(_norm)
        self._py_tag = f"py{sys.version_info.major}.{sys.version_info.minor}"
        if os.path.basename(_norm).startswith("user_"):
            self.shared_venv_dir = os.path.join(_base, "shared", self._py_tag, "venv")
            if not os.path.isdir(self.shared_venv_dir):
                # 兼容旧布局：仅当 Python 小版本一致才回退，否则各自安装
                _legacy = os.path.join(_base, "venv")
                if os.path.isdir(_legacy) and self._venv_version(_legacy) == self._py_tag:
                    self.shared_venv_dir = _legacy
        else:
            self.shared_venv_dir = self.venv_dir
        self._shared_site_packages = None   # 懒加载缓存
        self._shared_packages = None        # 懒加载缓存

        self._ensure_venv()
        self._load_installed_deps()

        # 注意：沙箱子进程的网络出口由 _build_wrapper 内的 getaddrinfo 补丁统一管控
        # （依据 SANDBOX_NETWORK_ALLOWLIST，默认全阻断；web-fetch 等主进程技能不受影响）。

    @staticmethod
    def _venv_version(venv_dir: str) -> str:
        """读取 venv 的 Python 小版本标签（如 'py3.14'）；无法识别返回 ''。"""
        cfg = os.path.join(venv_dir, "pyvenv.cfg")
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line.lower().startswith("version"):
                        ver = line.split("=", 1)[1].strip()
                        parts = ver.split(".")
                        if len(parts) >= 2:
                            return f"py{parts[0]}.{parts[1]}"
        except Exception:
            pass
        return ""

    @staticmethod
    def _clean_pip_env() -> dict:
        """构造不含代理环境变量的 pip 运行环境，避免主进程 HTTP_PROXY 致 pip 失败。"""
        env = dict(os.environ)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        return env

    @staticmethod
    def _parse_requirement(pkg: str):
        """返回 (归一化包名, 版本约束串|None)。"""
        try:
            from packaging.requirements import Requirement
            r = Requirement(pkg)
            return r.name.lower(), (str(r.specifier) or None)
        except Exception:
            import re as _re
            m = _re.match(r"^([A-Za-z0-9_.\-]+)\s*(.*)$", pkg.strip())
            if not m:
                return pkg.strip().lower(), None
            return m.group(1).lower(), (m.group(2).strip() or None)

    @staticmethod
    def _spec_satisfied(spec: str, installed_ver: str) -> bool:
        """判断已装版本是否满足约束（无法解析时保守视为满足）。"""
        try:
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version
            return SpecifierSet(spec).contains(Version(installed_ver), prereleases=True)
        except Exception:
            return True

    def get_shared_site_packages(self) -> str:
        """返回共享基础环境的 site-packages 路径（用户沙箱专用），无则返回空串。

        含版本守卫：仅当共享 venv 的 Python 小版本与用户 venv 一致时才注入，
        否则返回空串（交由 per-user 安装），杜绝跨版本 C 扩展崩溃。
        """
        if self._shared_site_packages is not None:
            return self._shared_site_packages
        path = ""
        if self.shared_venv_dir != self.venv_dir:
            if self._venv_version(self.shared_venv_dir) == self._py_tag:
                # 首次按需构建共享 venv（每个 Python 版本仅尝试一次）
                if (not os.path.isdir(self.shared_venv_dir)
                        and self._py_tag not in ToolSandbox._shared_build_attempted):
                    ToolSandbox._shared_build_attempted.add(self._py_tag)
                    self.ensure_shared_venv()
                import glob as _glob
                hits = _glob.glob(os.path.join(self.shared_venv_dir, "lib", "python*", "site-packages"))
                if hits:
                    path = hits[0]
        self._shared_site_packages = path
        return path

    def _get_shared_packages(self) -> dict:
        """列出共享基础环境已装包：{归一化包名(小写): 版本号}，用于跳过重复安装。"""
        if self._shared_packages is not None:
            return self._shared_packages
        packages: dict = {}
        shared_python = os.path.join(self.shared_venv_dir, "bin", "python3")
        if (self.shared_venv_dir != self.venv_dir
                and self._venv_version(self.shared_venv_dir) == self._py_tag
                and os.path.exists(shared_python)):
            try:
                result = subprocess.run(
                    [shared_python, "-m", "pip", "list", "--format", "freeze"],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        line = line.strip()
                        if not line or "==" not in line:
                            continue
                        name, ver = line.split("==", 1)
                        name = name.strip().lower()
                        packages[name] = ver.strip()
                        packages[name.replace("-", "_")] = ver.strip()
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

    def ensure_shared_venv(self) -> bool:
        """按需构建共享基础 venv（当前 Python 版本分桶）。

        读取仓库根 requirements.sandbox.txt，创建 tool_sandbox/shared/pyX.Y/venv
        并安装依赖。失败返回 False（不阻塞主流程，回退到 per-user 安装）。
        """
        if self.shared_venv_dir == self.venv_dir:
            return True
        if os.path.isdir(self.shared_venv_dir):
            return True
        req_file = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "requirements.sandbox.txt"))
        if not os.path.isfile(req_file):
            return False
        try:
            logger.info(f"构建共享沙箱 venv: {self.shared_venv_dir}")
            subprocess.check_call(
                [sys.executable, "-m", "venv", self.shared_venv_dir],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
            )
            shared_py = os.path.join(self.shared_venv_dir, "bin", "python3")
            cmd = [shared_py, "-m", "pip", "install",
                   "--no-cache-dir", "-q", "--disable-pip-version-check",
                   "--timeout", "30", "--retries", "2"]
            if PIP_INDEX_URL:
                cmd += ["-i", PIP_INDEX_URL]
                host = urlparse(PIP_INDEX_URL).hostname
                if host:
                    cmd += ["--trusted-host", host]
            cmd += ["-r", req_file]
            subprocess.check_call(cmd, env=self._clean_pip_env(), timeout=PIP_INSTALL_TIMEOUT)
            self._shared_site_packages = None
            self._shared_packages = None
            return True
        except Exception as e:
            logger.error(f"构建共享沙箱 venv 失败: {e}")
            return False

    def install(self, packages: list):
        if not packages:
            return True
        to_install = [p for p in packages if p not in self._deps_installed]
        if not to_install:
            return True

        # 1) 共享基础环境已有的包，通过 PYTHONPATH 继承，无需安装
        #    （版本感知：若声明了版本约束，需已装版本满足才跳过）
        shared_packages = self._get_shared_packages()
        if shared_packages:
            for pkg in list(to_install):
                _name, _spec = self._parse_requirement(pkg)
                if _name not in shared_packages:
                    continue
                if _spec is None or self._spec_satisfied(_spec, shared_packages[_name]):
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
                timeout=PIP_INSTALL_TIMEOUT,
                env=self._clean_pip_env(),
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
        # 共享基础环境已有的包，通过 PYTHONPATH 继承（版本感知）
        shared_packages = self._get_shared_packages()
        if shared_packages:
            for pkg in list(to_install):
                _name, _spec = self._parse_requirement(pkg)
                if _name not in shared_packages:
                    continue
                if _spec is None or self._spec_satisfied(_spec, shared_packages[_name]):
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
                timeout=PIP_INSTALL_TIMEOUT,
                env=self._clean_pip_env(),
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

    def execute(self, code: str, params: dict, timeout: int = 30, user_id: int = None,
                 public_id: str = None, tool_name: str = "", allow_network: bool = False) -> str:
        import time as _time
        _start = _time.time()
        code_preview = code[:100].replace("\n", " ") + ("..." if len(code) > 100 else "")
        logger.info(f"执行工具: {tool_name or '未知'} | user={user_id} | code={code_preview}")
        # 文件隔离目录以对外不透明 public_id 命名；未提供时回退到整数 user_id
        _file_owner = public_id if public_id is not None else user_id
        if _file_owner is not None:
            code = self._rewrite_user_paths(code, _file_owner, tool_name)
        wrapper = self._build_wrapper(code, params, user_id=_file_owner, allow_network=allow_network)
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
        # importlib 不能整包禁用：Python 3.14 的 stdlib（logging→inspect→
        # importlib.machinery）会经 importlib.machinery 间接导入，整包禁用会导致
        # 任何依赖 logging 的模块（含 agent.logger）全部导入失败。
        # 只精确禁用真正危险的 importlib.util（reload 攻击入口），并在 wrapper 中
        # 删除 importlib.reload 属性，既保留 stdlib 可用，又堵住沙箱逃逸通道。
        "importlib.util",
    }

    # 密钥 / 敏感文件名（任何位置命中即禁止读取）
    _SECRET_NAMES = {
        ".tool_secrets", ".tool_secrets.bak", ".agent_config", ".agent_config.bak",
        ".agent_salt", ".db_web_password", ".db_web_password.bak", ".db_web_password.old",
        ".env", ".env.local", ".env.example", "id_rsa", "id_rsa.pub",
        "id_ed25519", "id_ed25519.pub", ".pypirc", ".npmrc",
        ".git-credentials", ".netrc",
    }
    # 命令白名单：仅这些命令名可经 run_command 运行（防 rm -rf / 等破坏性命令）。
    # 刻意不含 python3/python/node —— 它们是完整解释器，放行后
    # `python3 -c "任意代码"` 会起一个不受沙箱 import/file 钩子约束的新进程，
    # 等于沙箱逃逸超级通道（可读任意文件、直连数据库、出网）。
    # 执行 AI 生成的 Python 代码请走 ToolSandbox.execute() 路径（带钩子约束）。
    _ALLOWED_COMMANDS = {
        "ls", "cat", "echo", "date", "pwd",
        "wc", "sort", "head", "tail", "grep", "awk", "sed", "jq", "cut",
        "tr", "uniq", "nl", "od", "base64", "xxd", "file", "stat", "printf",
        "true", "false", "test", "expr", "tee", "diff", "comm",
    }
    # 网络出口已由 _build_wrapper 内的 getaddrinfo 补丁统一管控
    # （SANDBOX_NETWORK_ALLOWLIST，默认全阻断）。下列集合保留作参考，不再自动并入禁用清单。
    _NETWORK_BLOCK = {
        "socket", "ssl", "urllib", "http", "ftplib", "smtplib",
        "telnetlib", "poplib", "imaplib", "webbrowser",
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

    def _build_wrapper(self, code: str, params: dict, user_id: int = None, allow_network: bool = False) -> str:
        safe_imports = ", ".join(sorted(self._SAFE_MODULES))
        blocked_list = json.dumps(sorted(self._BLOCKED_MODULES))
        params_json = json.dumps(params, ensure_ascii=False)
        tmpdir = os.path.realpath(tempfile.gettempdir())

        # 计算用户专属安全路径（用于在子进程中做文件读写边界控制）
        base_dir = os.path.dirname(os.path.abspath(__file__))   # agent/
        project_root = os.path.dirname(base_dir)                # 仓库根
        doc_root = os.path.join(project_root, "document_output")
        user_doc = os.path.realpath(os.path.join(doc_root, str(user_id or 0)))
        user_skill_dir = os.path.realpath(
            os.path.join(project_root, "agent", "skills", "user", str(user_id or 0))
        )
        data_dir = os.path.realpath(os.path.join(project_root, "data"))
        workbuddy_dir = os.path.realpath(os.path.join(project_root, ".workbuddy"))
        user_workbuddy = os.path.realpath(os.path.expanduser("~/.workbuddy"))

        write_allowed = sorted({user_doc, tmpdir, user_skill_dir, os.path.realpath("/tmp")})
        home_dir = os.path.realpath(os.path.expanduser("~"))
        current_user = os.path.basename(home_dir)
        # 当前用户 home 下的敏感目录仍禁止沙箱读取（防密钥/凭证外泄）
        read_secret_dirs = sorted({data_dir, workbuddy_dir, user_workbuddy,
            os.path.join(home_dir, ".ssh"), os.path.join(home_dir, ".aws"),
            os.path.join(home_dir, ".config"), os.path.join(home_dir, ".gnupg"),
            os.path.join(home_dir, ".kube"), os.path.join(home_dir, ".netrc"),
            os.path.join(home_dir, ".env")})
        # 运维可通过环境变量追加写白名单目录
        extra_dirs = os.environ.get("SANDBOX_WRITE_DIRS", "")
        for _d in extra_dirs.split(","):
            _d = _d.strip()
            if _d:
                _rp = os.path.realpath(_d)
                if _rp not in write_allowed:
                    write_allowed.append(_rp)
        allowed_cmds = json.dumps(sorted(self._ALLOWED_COMMANDS))

        # 受限读取目录前缀：沙箱子进程禁止读取系统/敏感目录
        # （防 /etc、用户家目录、数据库目录、密钥目录、其他用户的产出目录被越权读取后外泄）。
        # 项目自身的 agent/server/static 等源码仍可读（属应用自身代码，非敏感）。
        # 注意：不能简单用 /Users、/home 前缀屏蔽整个用户目录，
        # 否则会连同项目自身的共享 venv（如 python-docx 内置模板
        # tool_sandbox/shared/.../docx/templates/default.docx）一起禁读，
        # 导致依赖资源无法加载。改为：仅屏蔽系统目录 + 其他用户 home
        # （见下方 _is_other_user_home），当前用户 home 下敏感目录由
        # read_secret_dirs 管控。
        read_deny = sorted({
            os.path.realpath(p) for p in (
                "/etc", "/root", "/var", "/proc", "/sys",
                "/boot", "/usr", "/bin", "/sbin", "/lib", "/opt",
            )
        })
        # 沙箱审计日志（集中记录命令执行与越权访问尝试）
        logs_dir = os.path.join(project_root, "logs")
        audit_log = os.path.join(logs_dir, "sandbox_audit.log")
        # 网络出口白名单（逗号分隔主机后缀）；为空则沙箱内全部外联被拒绝。
        # allow_network=True 时设为 "*"，放行该可信工具的所有外联（仍保留文件/导入隔离）。
        net_allow = "*" if allow_network else os.environ.get("SANDBOX_NETWORK_ALLOWLIST", "")

        wrapper = (
            f"import {safe_imports}\n"
            "import subprocess as _sb_subprocess\n"
            "import os as _os\n"
            "import socket as _sb_socket\n"
            "_SB_ORIG_OPEN = open\n"
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
            "    if root in _BLOCKED or name in _BLOCKED or any(name.startswith(b + '.') for b in _BLOCKED):\n"
            "        raise ImportError(f'模块 {name} 已被沙箱禁用')\n"
            "    _mod = _orig_import(name, *args, **kwargs)\n"
            "    if root == 'os':\n"
            "        for _f in _DANGEROUS_OS:\n"
            "            if hasattr(_mod, _f):\n"
            "                delattr(_mod, _f)\n"
            "    return _mod\n"
            "builtins.__import__ = _safe_import\n"
            "_orig_unlink = _os.unlink\n"
            "_orig_remove = _os.remove\n"
            "_orig_makedirs = _os.makedirs\n"
            "_orig_mkdir = _os.mkdir\n"
            "_orig_rename = _os.rename\n"
            "_orig_replace = _os.replace\n"
            "_orig_symlink = _os.symlink\n"
            "_orig_link = _os.link\n"
            "_DANGEROUS_OS = ['system', 'popen', 'execv', 'execve', 'spawnl', 'spawnle', 'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe', 'remove', 'rmdir', 'removedirs', 'renames', 'chmod', 'chown', 'kill', 'killpg', 'setuid', 'setgid', 'fork', 'forkpty', 'unlink', 'open']\n"
            "for _func in _DANGEROUS_OS:\n"
            "    if hasattr(_os, _func):\n"
            "        delattr(_os, _func)\n"
            "try:\n"
            "    import importlib as _il\n"
            "    for _b in ('reload', 'util'):\n"
            "        if hasattr(_il, _b):\n"
            "            delattr(_il, _b)\n"
            "except Exception:\n"
            "    pass\n"
            # ---- 文件 / 目录安全边界 ----
            f"_WRITE_ALLOWED_DIRS = {json.dumps(write_allowed)}\n"
            f"_READ_SECRET_DIRS = {json.dumps(read_secret_dirs)}\n"
            f"_SECRET_NAMES = {json.dumps(sorted(self._SECRET_NAMES))}\n"
            f"_READ_DENY_PREFIXES = {json.dumps(read_deny)}\n"
            f"_DOC_ROOT = {json.dumps(doc_root)}\n"
            f"_USER_DOC = {json.dumps(user_doc)}\n"
            f"_SB_AUDIT_LOG = {json.dumps(audit_log)}\n"
            f"_SB_UID = {json.dumps(str(user_id or 0))}\n"
            f"_HOME = {json.dumps(home_dir)}\n"
            f"_CURRENT_USER = {json.dumps(current_user)}\n"
            f"_SB_NET_ALLOW = {json.dumps(net_allow)}\n"
            "def _sb_audit(event, detail):\n"
            "    try:\n"
            "        import time as _t\n"
            "        _d = _os.path.dirname(_SB_AUDIT_LOG)\n"
            "        if _d and not _os.path.isdir(_d):\n"
            "            try: _os.makedirs(_d, exist_ok=True)\n"
            "            except Exception: pass\n"
            "        _ts = _t.strftime('%Y-%m-%d %H:%M:%S', _t.localtime())\n"
            "        with _SB_ORIG_OPEN(_SB_AUDIT_LOG, 'a', encoding='utf-8') as _af:\n"
            "            _af.write(f'[_ts] [{event}] user={_SB_UID} | {detail}\\n')\n"
            "    except Exception:\n"
            "        pass\n"
            "# ---- 网络出口白名单（默认全阻断，仅放行 SANDBOX_NETWORK_ALLOWLIST 中的主机）----\n"
            "_sb_orig_getaddrinfo = _sb_socket.getaddrinfo\n"
            "def _sb_getaddrinfo(host, *args, **kwargs):\n"
            "    _h = (host or '').strip().lower()\n"
            "    if _h.startswith('['):\n"
            "        _h = _h.strip('[]')\n"
            "    _host = _h.split(':')[0]\n"
            "    _allow = [x.strip().lower() for x in _SB_NET_ALLOW.split(',') if x.strip()]\n"
            "    for _a in _allow:\n"
            "        if _a == '*' or _host == _a or _host.endswith('.' + _a):\n"
            "            return _sb_orig_getaddrinfo(host, *args, **kwargs)\n"
            "    _sb_audit('net_denied', f'host={host}')\n"
            "    raise PermissionError(f'[沙箱] 禁止访问外部网络主机: {host}')\n"
            "_sb_socket.getaddrinfo = _sb_getaddrinfo\n"
            "def _in_dirs(rp, dirs):\n"
            "    for _d in dirs:\n"
            "        if rp == _d or rp.startswith(_d + _os.sep):\n"
            "            return True\n"
            "    return False\n"
            "def _under_prefix(rp, prefixes):\n"
            "    for _p in prefixes:\n"
            "        if rp == _p or rp.startswith(_p + _os.sep):\n"
            "            return True\n"
            "    return False\n"
            "def _is_other_user_home(rp):\n"
            "    _p = rp.split(_os.sep)\n"
            "    if len(_p) >= 3 and _p[1] in ('Users', 'home') and _p[2] != _CURRENT_USER:\n"
            "        return True\n"
            "    return False\n"
            "def _is_secret(rp):\n"
            "    if _os.path.basename(rp) in _SECRET_NAMES:\n"
            "        return True\n"
            "    return _in_dirs(rp, _READ_SECRET_DIRS)\n"
            # 删除控制（仅白名单目录）
            "def _safe_unlink(path):\n"
            "    real = _os.path.realpath(path)\n"
            "    if _in_dirs(real, _WRITE_ALLOWED_DIRS):\n"
            "        return _orig_unlink(path)\n"
            "    _sb_audit('file_delete_denied', f'path={path}')\n"
            "    raise PermissionError(f'[沙箱] 禁止删除此路径的文件: {path}')\n"
            "_os.unlink = _safe_unlink\n"
            "_os.remove = _safe_unlink\n"
            # 创建目录控制（仅白名单目录）
            "def _safe_makedirs(path, mode=0o777, exist_ok=False):\n"
            "    if _in_dirs(_os.path.realpath(path), _WRITE_ALLOWED_DIRS):\n"
            "        return _orig_makedirs(path, mode, exist_ok)\n"
            "    raise PermissionError(f'[沙箱] 禁止在禁区创建目录: {path}')\n"
            "_os.makedirs = _safe_makedirs\n"
            "def _safe_mkdir(path, mode=0o777):\n"
            "    if _in_dirs(_os.path.realpath(path), _WRITE_ALLOWED_DIRS):\n"
            "        return _orig_mkdir(path, mode)\n"
            "    raise PermissionError(f'[沙箱] 禁止在禁区创建目录: {path}')\n"
            "_os.mkdir = _safe_mkdir\n"
            # 移动 / 重命名 / 链接控制
            "def _safe_rename(src, dst):\n"
            "    if _is_secret(_os.path.realpath(src)):\n"
            "        _sb_audit('file_rename_denied', f'src={src}')\n"
            "        raise PermissionError('[沙箱] 禁止移动密钥文件')\n"
            "    if not _in_dirs(_os.path.realpath(dst), _WRITE_ALLOWED_DIRS):\n"
            "        _sb_audit('file_rename_denied', f'dst={dst}')\n"
            "        raise PermissionError(f'[沙箱] 禁止移动到禁区: {dst}')\n"
            "    return _orig_rename(src, dst)\n"
            "_os.rename = _safe_rename\n"
            "_os.replace = _safe_rename\n"
            "def _safe_symlink(target, link):\n"
            "    if _is_secret(_os.path.realpath(target)):\n"
            "        _sb_audit('file_symlink_denied', f'target={target}')\n"
            "        raise PermissionError('[沙箱] 禁止链接到密钥文件')\n"
            "    if not _in_dirs(_os.path.realpath(link), _WRITE_ALLOWED_DIRS):\n"
            "        _sb_audit('file_symlink_denied', f'link={link}')\n"
            "        raise PermissionError(f'[沙箱] 禁止在禁区创建链接: {link}')\n"
            "    return _orig_symlink(target, link)\n"
            "_os.symlink = _safe_symlink\n"
            "def _safe_link(src, link):\n"
            "    if _is_secret(_os.path.realpath(src)):\n"
            "        _sb_audit('file_link_denied', f'src={src}')\n"
            "        raise PermissionError('[沙箱] 禁止硬链接密钥文件')\n"
            "    if not _in_dirs(_os.path.realpath(link), _WRITE_ALLOWED_DIRS):\n"
            "        _sb_audit('file_link_denied', f'link={link}')\n"
            "        raise PermissionError(f'[沙箱] 禁止在禁区创建链接: {link}')\n"
            "    return _orig_link(src, link)\n"
            "_os.link = _safe_link\n"
            # 文件读写控制：读禁密钥区，写限白名单
            "_orig_open = open\n"
            "import io as _io\n"
            "def _safe_open(path, mode='r', *args, **kwargs):\n"
            "    _rp = _os.path.realpath(str(path))\n"
            "    _mode = mode if isinstance(mode, str) else 'r'\n"
            "    _read = ('r' in _mode) or ('+' in _mode)\n"
            "    _write = ('w' in _mode) or ('a' in _mode) or ('x' in _mode) or ('+' in _mode)\n"
            "    if _read and (_is_secret(_rp) or _under_prefix(_rp, _READ_DENY_PREFIXES) or _is_other_user_home(_rp) or (_rp.startswith(_DOC_ROOT + _os.sep) and not _rp.startswith(_USER_DOC + _os.sep))):\n"
            "        _sb_audit('file_read_denied', f'path={path}')\n"
            "        raise PermissionError(f'[沙箱] 禁止读取受限文件: {path}')\n"
            "    if _write and not _in_dirs(_rp, _WRITE_ALLOWED_DIRS):\n"
            "        _sb_audit('file_write_denied', f'path={path}')\n"
            "        raise PermissionError(f'[沙箱] 禁止写入禁区: {path}')\n"
            "    return _orig_open(path, mode, *args, **kwargs)\n"
            "builtins.open = _safe_open\n"
            "_io.open = _safe_open\n"
            # 受控命令执行（命令白名单，默认禁用 rm/sudo/dd 等危险命令）
            f"_ALLOWED_CMDS = set({allowed_cmds})\n"
            f"_DEFAULT_CWD = {json.dumps(tmpdir)}\n"
            "def run_command(cmd, cwd=None, timeout=30):\n"
            "    import shlex as _shlex\n"
            "    _parts = _shlex.split(cmd) if isinstance(cmd, str) else list(cmd)\n"
            "    if not _parts:\n"
            "        raise ValueError('空命令')\n"
            "    if _parts[0] not in _ALLOWED_CMDS:\n"
            "        _sb_audit('cmd_denied', f'cmd={cmd}')\n"
            "        raise PermissionError(f'[沙箱] 命令不在白名单: {_parts[0]}')\n"
            "    # 路径参数白名单：禁止绝对路径、家目录展开、父目录遍历、密钥文件与受限目录\n"
            "    for _arg in _parts[1:]:\n"
            "        _a = _arg.strip()\n"
            "        if not _a:\n"
            "            continue\n"
            "        if _a.startswith('/') or _a.startswith('~'):\n"
            "            _sb_audit('cmd_path_denied', f'cmd={cmd} arg={_arg}')\n"
            "            raise PermissionError(f'[沙箱] run_command 禁止访问绝对路径: {_arg}')\n"
            "        if _a == '..' or _a.startswith('../') or '/../' in _a:\n"
            "            _sb_audit('cmd_path_denied', f'cmd={cmd} arg={_arg}')\n"
            "            raise PermissionError(f'[沙箱] run_command 禁止父目录遍历: {_arg}')\n"
            "        if _os.path.basename(_a) in _SECRET_NAMES:\n"
            "            _sb_audit('cmd_secret_denied', f'cmd={cmd} arg={_arg}')\n"
            "            raise PermissionError(f'[沙箱] run_command 禁止访问密钥文件: {_arg}')\n"
            "        _rp = _os.path.realpath(_os.path.join(str(cwd or _DEFAULT_CWD), _a))\n"
            "        if _under_prefix(_rp, _READ_DENY_PREFIXES):\n"
            "            _sb_audit('cmd_path_denied', f'cmd={cmd} arg={_arg}')\n"
            "            raise PermissionError(f'[沙箱] run_command 禁止访问受限路径: {_arg}')\n"
            "    if cwd is None:\n"
            "        cwd = _DEFAULT_CWD\n"
            "    _rcwd = _os.path.realpath(str(cwd))\n"
            "    if not _in_dirs(_rcwd, _WRITE_ALLOWED_DIRS):\n"
            "        raise PermissionError(f'[沙箱] 命令工作目录不在白名单: {cwd}')\n"
            "    _sb_audit('cmd_exec', f'cmd={cmd}')\n"
            "    try:\n"
            "        _r = _sb_subprocess.run(_parts, cwd=_rcwd, capture_output=True, text=True, timeout=timeout)\n"
            "    except Exception as _e:\n"
            "        return f'[命令执行失败] {type(_e).__name__}: {_e}'\n"
            "    return ((_r.stdout or '') + (_r.stderr or ''))[:8000]\n"
            # 绑定 os 别名供工具代码使用（已阉割 + 受控）
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