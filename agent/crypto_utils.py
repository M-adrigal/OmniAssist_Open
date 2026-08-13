"""强加密工具（替代旧的逐字节 XOR）。

设计要点：
- 不依赖任何第三方库（仅标准库），保证在服务器现有 Python 环境直接可用。
- 采用「HMAC-SHA256 作为 PRF 的 CTR 流密码 + HMAC 认证标签」结构：
  * 每条密文绑定一个随机 16 字节 nonce（盐），keystream 永不重复，
    彻底消除旧 XOR（32 字节重复密钥流、可已知明文攻击、可篡改）的弱点。
  * 末尾附带 HMAC-SHA256 认证标签，任何篡改都会被检测并拒绝（AEAD 语义）。
- 密文格式：b"v2:" + base64(nonce[16] || ciphertext || tag[32])。
- 兼容旧格式：以 v2: 开头走新路径；否则尝试旧 XOR 解密（legacy 兜底），
  业务层在保存时会自动以新格式回写，实现平滑迁移。

密钥来源由调用方提供（config 用 salt 派生的 32 字节，database 用 .db_secret）。
"""

import os
import hmac
import hashlib
import base64

_PREFIX = b"v2:"
_NONCE_LEN = 16
_TAG_LEN = 32


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """由 HMAC-SHA256(key, nonce || counter) 生成指定长度的密钥流（CTR 模式）。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out += block
        counter += 1
    return bytes(out[:length])


def secure_encrypt(plaintext: str, key: bytes) -> str:
    """加密明文，返回可安全存储的字符串（空明文返回空串）。"""
    if not plaintext:
        return ""
    pt = plaintext.encode("utf-8")
    nonce = os.urandom(_NONCE_LEN)
    ks = _keystream(key, nonce, len(pt))
    ct = bytes(p ^ k for p, k in zip(pt, ks))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    blob = _PREFIX + base64.b64encode(nonce + ct + tag)
    return blob.decode("ascii")


def secure_decrypt(ciphertext: str, key: bytes) -> str:
    """解密；新格式验证认证标签，否则回退旧 XOR 格式。

    解密失败（密钥错误 / 篡改 / 格式损坏）统一抛 ValueError。
    """
    if not ciphertext:
        return ""
    raw = ciphertext.encode("ascii")
    if raw.startswith(_PREFIX):
        payload = base64.b64decode(raw[len(_PREFIX):])
        if len(payload) < _NONCE_LEN + _TAG_LEN:
            raise ValueError("密文长度不足")
        nonce = payload[:_NONCE_LEN]
        ct = payload[_NONCE_LEN:-_TAG_LEN]
        tag = payload[-_TAG_LEN:]
        expected = hmac.new(key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, tag):
            raise ValueError("认证失败：密文可能被篡改或密钥不匹配")
        ks = _keystream(key, nonce, len(ct))
        pt = bytes(c ^ k for c, k in zip(ct, ks))
        return pt.decode("utf-8")
    # 旧格式：逐字节 XOR（与历史 _encrypt/_decrypt 兼容）
    enc = base64.b64decode(ciphertext)
    return bytes(e ^ key[i % len(key)] for i, e in enumerate(enc)).decode("utf-8")
