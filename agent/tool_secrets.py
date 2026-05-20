import os
import json
from agent.config import _encrypt, _decrypt


class ToolSecrets:
    """工具密钥管理

    为工具提供加密的敏感信息存储（API Key、Token 等）。
    密钥文件存储在 data/.tool_secrets，使用与 AgentConfig 相同的加密机制。
    此文件应在 .gitignore 中排除，防止开源时泄露。
    """

    def __init__(self, secrets_path: str = None):
        if secrets_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            data_dir = os.path.join(os.path.dirname(base_dir), "data")
            os.makedirs(data_dir, exist_ok=True)
            secrets_path = os.path.join(data_dir, ".tool_secrets")
        self.secrets_path = secrets_path
        self.config_dir = os.path.dirname(os.path.abspath(secrets_path))
        self._data = {}
        self._load()

    def _load(self):
        if os.path.isfile(self.secrets_path):
            try:
                with open(self.secrets_path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = {}

    def _save(self):
        with open(self.secrets_path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(self.secrets_path, 0o600)
        except Exception:
            pass

    def set(self, key: str, value: str):
        """设置密钥（加密存储）

        Args:
            key: 密钥名称，如 'qweather_api_key'、'gold_price_api_key'
            value: 密钥明文
        """
        encrypted = _encrypt(value, self.config_dir)
        self._data[key] = encrypted
        self._save()

    def get(self, key: str, default: str = "") -> str:
        """获取密钥明文

        Args:
            key: 密钥名称
            default: 密钥不存在时的默认值

        Returns:
            密钥明文，不存在时返回 default
        """
        encrypted = self._data.get(key, "")
        if not encrypted:
            return default
        try:
            return _decrypt(encrypted, self.config_dir)
        except Exception:
            return default

    def has(self, key: str) -> bool:
        """检查密钥是否存在"""
        return key in self._data

    def delete(self, key: str):
        """删除指定密钥"""
        if key in self._data:
            del self._data[key]
            self._save()

    def list_keys(self) -> list:
        """列出所有密钥名称"""
        return list(self._data.keys())

    def get_masked(self, key: str) -> str:
        """获取脱敏后的密钥，仅显示前4位和后4位"""
        val = self.get(key)
        if not val:
            return "(未设置)"
        if len(val) <= 8:
            return "*" * len(val)
        return val[:4] + "*" * (len(val) - 8) + val[-4:]

    def get_all_masked(self) -> dict:
        """获取所有密钥的脱敏视图

        Returns:
            dict: {key_name: masked_value}
        """
        return {k: self.get_masked(k) for k in self._data}

    def get_all_raw(self) -> dict:
        """获取所有密钥明文（仅供内部集成使用）

        Returns:
            dict: {key_name: plaintext_value}
        """
        return {k: self.get(k) for k in self._data}


_TOOL_SECRETS_INSTANCE = None


def get_tool_secrets() -> ToolSecrets:
    """获取 ToolSecrets 全局单例"""
    global _TOOL_SECRETS_INSTANCE
    if _TOOL_SECRETS_INSTANCE is None:
        _TOOL_SECRETS_INSTANCE = ToolSecrets()
    return _TOOL_SECRETS_INSTANCE


def resolve_secrets_in_template(text: str, secrets: ToolSecrets = None) -> str:
    """解析文本中的 {secret:key_name} 占位符

    用于 URL 和 Header 模板中的密钥注入。
    例如: "https://api.example.com?key={secret:my_api_key}"
          → "https://api.example.com?key=actualvalue"

    Args:
        text: 包含占位符的模板文本
        secrets: ToolSecrets 实例，为 None 时使用全局单例

    Returns:
        替换后的文本
    """
    if secrets is None:
        secrets = get_tool_secrets()
    import re
    def replacer(match):
        key_name = match.group(1)
        return secrets.get(key_name, "")
    return re.sub(r'\{secret:([^}]+)\}', replacer, text)