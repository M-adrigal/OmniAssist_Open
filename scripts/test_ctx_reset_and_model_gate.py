#!/usr/bin/env python3
"""`/reset` 上下文边界 + 模型可见性门禁 —— 两个「界面看着正常、实际没生效」的问题。

背景（2026-10-24 用户反馈）：
  1. `/reset` 之前是直接 `messages = []` —— 对话记录跟着上下文一起被删，翻不回去。
     现在改成「记录保留、记忆断开」：在最后一条 /reset 上打 `context_reset` 标记，
     送模型时只取标记之后的消息（`_context_messages`）。
  2. 管理员把模型全部隐藏后，用户**仍然能用**：隐藏只作用于下拉列表，
     请求解析（`resolve_active_model`）不看 `visible`。

运行：
  /usr/local/bin/python3 scripts/test_ctx_reset_and_model_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name} — {extra}")


# ============ 一、/reset 上下文边界 ============
print("[1] _context_messages：/reset 之后断上下文、但历史仍保留")
from server.routes.chat import _context_messages  # noqa: E402

MSGS = [
    {"role": "user", "content": "第一问"},
    {"role": "assistant", "content": "第一答"},
    {"role": "user", "content": "/reset"},
    {"role": "assistant", "content": "(本轮执行中断，未产生回复)"},
    {"role": "user", "content": "我刚刚说了什么"},
]

# 没有标记 → 全带上（只是把斜杠命令剔掉）
out = _context_messages(MSGS)
check("无标记时不裁剪", len(out) == 4, [m.get("content") for m in out])
check("斜杠命令本身不送模型",
      all(m.get("content") != "/reset" for m in out),
      [m.get("content") for m in out])

# 标记在 /reset 上 → 只取之后的内容
MSGS_MARKED = [dict(m) for m in MSGS]
MSGS_MARKED[2]["context_reset"] = True
out2 = _context_messages(MSGS_MARKED)
check("/reset 之前的一律不送", [m["content"] for m in out2] ==
      ["(本轮执行中断，未产生回复)", "我刚刚说了什么"],
      [m.get("content") for m in out2])
check("历史本身没被删（原列表长度不变）", len(MSGS_MARKED) == 5, len(MSGS_MARKED))

# 多次重置 → 以最后一次为界
MSGS_TWICE = [
    {"role": "user", "content": "旧问题"},
    {"role": "user", "content": "/reset", "context_reset": True},
    {"role": "user", "content": "中间问题"},
    {"role": "user", "content": "/reset", "context_reset": True},
    {"role": "user", "content": "新问题"},
]
check("多次重置以最后一次为界",
      [m["content"] for m in _context_messages(MSGS_TWICE)] == ["新问题"],
      [m["content"] for m in _context_messages(MSGS_TWICE)])

# 边界标记是 system 兜底消息（/reset 没来得及落库时）
FALLBACK = [
    {"role": "user", "content": "旧问题"},
    {"role": "system", "content": "上下文已重置", "context_reset": True},
    {"role": "user", "content": "新问题"},
]
check("system 兜底标记同样生效",
      [m["content"] for m in _context_messages(FALLBACK)] == ["新问题"],
      [m["content"] for m in _context_messages(FALLBACK)])

# 边界/命令的边界情况
check("空列表", _context_messages([]) == [])
check("None 安全", _context_messages(None) == [])
check("大小写与空格（RESET / 带空格）",
      [m["content"] for m in _context_messages(
          [{"role": "user", "content": "  RESET "}, {"role": "user", "content": "x"}])] == ["x"])
check("非 dict 元素原样保留",
      _context_messages(["纯字符串", {"role": "user", "content": "x"}]) ==
      ["纯字符串", {"role": "user", "content": "x"}])
check("help 也不送模型",
      [m["content"] for m in _context_messages(
          [{"role": "user", "content": "/help"}, {"role": "user", "content": "x"}])] == ["x"])


# ============ 二、模型可见性门禁 ============
print("\n[2] _entry_usable：隐藏/停用的判定")
from server import model_store as ms  # noqa: E402

PLATFORM_VISIBLE = {"id": 1, "enabled": True, "visible": True, "provider_owner": None}
PLATFORM_HIDDEN = {"id": 2, "enabled": True, "visible": False, "provider_owner": None}
PLATFORM_DISABLED = {"id": 3, "enabled": False, "visible": True, "provider_owner": None}
USER_OWN_HIDDEN = {"id": 4, "enabled": True, "visible": False, "provider_owner": 7}

check("平台可见 → 可用", ms._entry_usable(PLATFORM_VISIBLE) is True)
check("平台隐藏 → 普通用户不可用", ms._entry_usable(PLATFORM_HIDDEN) is False)
check("平台隐藏 → 管理员放行", ms._entry_usable(PLATFORM_HIDDEN, include_hidden=True) is True)
check("停用 → 谁都不行（管理员也不行）",
      ms._entry_usable(PLATFORM_DISABLED) is False
      and ms._entry_usable(PLATFORM_DISABLED, include_hidden=True) is False)
check("用户自配条目不受 visible 影响（那是平台侧开关）",
      ms._entry_usable(USER_OWN_HIDDEN) is True)


print("\n[3] resolve_active_model：隐藏模型不能靠 entry_id 绕过")


class _Fake:
    """把 model_store 的取数函数换成可控的，只测解析逻辑本身。"""

    def __init__(self, entries, providers, default=None, user_entry=None):
        self.entries = entries
        self.providers = providers
        self.default = default
        self.user_entry = user_entry

    def __enter__(self):
        self._orig = (ms.get_entry, ms.get_provider, ms.get_user_current_entry,
                      ms.get_default_entry, ms._compose)
        ms.get_entry = lambda eid, owner_user_id=None: self.entries.get(eid)
        ms.get_provider = lambda pid, owner_user_id=None: self.providers.get(pid)
        ms.get_user_current_entry = lambda uid: self.user_entry
        ms.get_default_entry = lambda: self.default
        ms._compose = lambda entry, provider, source: {
            "entry_id": entry["id"], "provider_id": provider["id"],
            "resolved_from": source, "model_id": entry.get("model_id", ""),
        }
        return self

    def __exit__(self, *a):
        (ms.get_entry, ms.get_provider, ms.get_user_current_entry,
         ms.get_default_entry, ms._compose) = self._orig


PROV = {10: {"id": 10, "enabled": True}}

# 用户默认就选中了那条被隐藏的模型
with _Fake({2: dict(PLATFORM_HIDDEN, provider_id=10)}, PROV, user_entry=2):
    check("隐藏模型即便被用户选中也不解析",
          ms.resolve_active_model(1) is None, ms.resolve_active_model(1))
    got = ms.resolve_active_model(1, include_hidden=True)
    check("管理员仍能解析同一模型", got and got["entry_id"] == 2, got)

# 前端显式指定 entry_id 也不行（否则改个请求体就能越权用）
with _Fake({2: dict(PLATFORM_HIDDEN, provider_id=10)}, PROV, user_entry=None):
    check("显式 entry_id 也拦得住",
          ms.resolve_active_model(1, entry_id=2) is None,
          ms.resolve_active_model(1, entry_id=2))

# 隐藏的模型不能被当作平台默认回落
with _Fake({}, PROV, default=dict(PLATFORM_HIDDEN, provider_id=10)):
    check("隐藏项不会成为平台默认回落",
          ms.resolve_active_model(1) is None, ms.resolve_active_model(1))

# 供应商被停用同样不可用
with _Fake({1: dict(PLATFORM_VISIBLE, provider_id=11)}, {11: {"id": 11, "enabled": False}},
           user_entry=1):
    check("供应商停用则不可用", ms.resolve_active_model(1) is None)

# 正常情况（回归：别把可见的也拦掉了）
with _Fake({1: dict(PLATFORM_VISIBLE, provider_id=10)}, PROV, user_entry=1):
    got = ms.resolve_active_model(1)
    check("可见模型正常解析", got and got["entry_id"] == 1, got)


print("\n" + "=" * 60)
print(f"结果: {PASS} 通过, {FAIL} 失败")
if FAILED:
    print("失败项:", FAILED)
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
