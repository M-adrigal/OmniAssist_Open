import os
import json
import base64
import hashlib
import secrets


def _get_salt_file(config_dir: str) -> str:
    return os.path.join(config_dir, ".agent_salt")


def _load_or_create_salt(config_dir: str) -> bytes:
    salt_file = _get_salt_file(config_dir)
    if os.path.isfile(salt_file):
        with open(salt_file, "rb") as f:
            return f.read()
    salt = secrets.token_bytes(32)
    with open(salt_file, "wb") as f:
        f.write(salt)
    try:
        os.chmod(salt_file, 0o600)
    except Exception:
        pass
    return salt


def _derive_key(config_dir: str = None) -> bytes:
    if config_dir is None:
        config_dir = os.path.dirname(os.path.abspath(__file__))
    salt = _load_or_create_salt(config_dir)
    return hashlib.sha256(salt + b":agent_config_v1").digest()


def _encrypt(plaintext: str, config_dir: str = None) -> str:
    key = _derive_key(config_dir)
    plain_bytes = plaintext.encode('utf-8')
    encrypted = bytes(p ^ key[i % len(key)] for i, p in enumerate(plain_bytes))
    return base64.b64encode(encrypted).decode('ascii')


def _decrypt(ciphertext: str, config_dir: str = None) -> str:
    key = _derive_key(config_dir)
    encrypted = base64.b64decode(ciphertext)
    decrypted = bytes(e ^ key[i % len(key)] for i, e in enumerate(encrypted))
    return decrypted.decode('utf-8')


class AgentConfig:
    """Agent 配置管理，支持加密存储敏感信息"""

    def __init__(self, config_path: str = None):
        if config_path is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(base_dir, ".agent_config")
        self.config_path = config_path
        self.config_dir = os.path.dirname(os.path.abspath(config_path))
        self._data = {}
        self._load()

    def _load(self):
        if os.path.isfile(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._data = {}

    def _save(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(self.config_path, 0o600)
        except Exception:
            pass

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value
        self._save()

    def set_api_key(self, api_key: str):
        """加密存储 API Key"""
        encrypted = _encrypt(api_key, self.config_dir)
        self._data['api_key_encrypted'] = encrypted
        self._save()

    def get_api_key(self) -> str:
        """解密获取 API Key"""
        encrypted = self._data.get('api_key_encrypted', '')
        if not encrypted:
            return ''
        try:
            return _decrypt(encrypted, self.config_dir)
        except Exception:
            legacy_key = self._try_legacy_decrypt(encrypted)
            if legacy_key:
                self.set_api_key(legacy_key)
                return legacy_key
            return ''

    def _try_legacy_decrypt(self, encrypted: str) -> str:
        import uuid
        import socket
        machine_id = f"{uuid.getnode()}:{socket.gethostname()}:agent_config_v1"
        key = hashlib.sha256(machine_id.encode()).digest()
        try:
            enc_bytes = base64.b64decode(encrypted)
            decrypted = bytes(e ^ key[i % len(key)] for i, e in enumerate(enc_bytes))
            return decrypted.decode('utf-8')
        except Exception:
            return ''

    def get_masked_api_key(self) -> str:
        """获取脱敏后的 API Key，仅显示前4位和后4位"""
        key = self.get_api_key()
        if not key:
            return '(未设置)'
        if len(key) <= 8:
            return '*' * len(key)
        return key[:4] + '*' * (len(key) - 8) + key[-4:]

    def set_model(self, api_key: str, base_url: str, model_name: str):
        """一次性设置模型相关配置

        Args:
            api_key: API 密钥（将加密存储）
            base_url: API 基础地址
            model_name: 模型名称
        """
        self.set_api_key(api_key)
        self.set('base_url', base_url)
        self.set('model_name', model_name)

    def show_config(self) -> dict:
        """返回当前配置信息（API Key 脱敏）"""
        return {
            'model_name': self.get('model_name', '(未设置)'),
            'base_url': self.get('base_url', '(未设置)'),
            'api_key': self.get_masked_api_key(),
            'show_thought': self.get('show_thought', False),
            'context_limit': self.get('context_limit', ''),
        }

    def toggle_thought(self) -> bool:
        """切换思考过程显示开关

        Returns:
            bool: 切换后的状态
        """
        current = self.get('show_thought', False)
        new_state = not current
        self.set('show_thought', new_state)
        return new_state
