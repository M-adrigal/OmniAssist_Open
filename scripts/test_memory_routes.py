"""记忆接口的路由级验证（需 fastapi —— 与 test_model_config 同一套搭法）。

为什么在 `test_memory.py` 之外再单开一个路由级用例：
纯逻辑测的是「函数对不对」，路由级测的是「接口契约对不对」 ——
状态码、谁能看谁的、模型整理失败时用户看到什么。这几条恰恰是
「逻辑全对但用户体验全错」的高发区。

搭法（抄 `test_model_config.py`）：按文件加载真实模块 + 临时库 + auth stub
+ chat 模块 stub（这样不用真把整个对话栈拉起来就能测四条模型路径）。

运行：
    /usr/local/bin/python3 scripts/test_memory_routes.py
"""
import importlib.util
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="memory_routes_"))

# ===== 搭模块加载环境（绕过 server/__init__.py 的 app 装配）=====
server_pkg = types.ModuleType("server")
server_pkg.__path__ = [str(ROOT / "server")]
sys.modules["server"] = server_pkg

routes_pkg = types.ModuleType("server.routes")
routes_pkg.__path__ = [str(ROOT / "server" / "routes")]
sys.modules["server.routes"] = routes_pkg

_current = {"db_id": 1, "id": "u_1", "user_type": "user", "public_id": "u_1"}


def set_current(db_id, user_type="user"):
    global _current
    _current = {"db_id": db_id, "id": f"u_{db_id}", "user_type": user_type,
                "public_id": f"u_{db_id}"}


auth_stub = types.ModuleType("server.routes.auth")
auth_stub.require_permission = lambda request, perm=None, action=None: _current
auth_stub.require_login = lambda request: _current
auth_stub.get_current_user = lambda request: _current
auth_stub.set_current = set_current
sys.modules["server.routes.auth"] = auth_stub

# chat stub：只提供记忆接口用到的那一个函数，免得拉起整个对话栈
# （chat.py 会连带 agent / 沙箱 / 技能注册表一起进来）
_model_state = {"mode": "none"}  # none=无模型 / ok=正常 / boom=调用抛异常


class _FakeLLM:
    usage_sink = None

    def __init__(self, reply=""):
        self._reply = reply

    def chat(self, messages, tools=None, source="chat"):
        if _model_state["mode"] == "boom":
            raise RuntimeError("上游连接超时")
        return {"content": self._reply}


chat_stub = types.ModuleType("server.routes.chat")


def _resolve_llm_client(user_id, session_id=None):
    if _model_state["mode"] == "none":
        return None, {"provider_name": "p", "entry_owner": "platform"}
    return _FakeLLM(_model_state.get("reply", "")), {
        "provider_name": "p", "entry_owner": "platform"}


_SESSIONS = {}   # session_id -> messages，给 /evolve 用


def _load_session_messages(session_id):
    return _SESSIONS.get(session_id) or []


chat_stub._resolve_llm_client = _resolve_llm_client
chat_stub._load_session_messages = _load_session_messages
sys.modules["server.routes.chat"] = chat_stub


def load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


database = load("server.database", "server/database.py")
memory_store = load("server.memory_store", "server/memory_store.py")
memory_route = load("server.routes.memory", "server/routes/memory.py")

database.DB_DIR = str(TMP)
database.DB_PATH = str(TMP / "users.db")
database.init_db()

# 第二个用户：用来验证「管理员也看不到别人的记忆」
_conn = sqlite3.connect(str(TMP / "users.db"))
_conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, user_type) "
              "VALUES (2, 'bob', 'x', 'user')")
_conn.commit()
_conn.close()

from fastapi import HTTPException  # noqa: E402

PASS = FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}")
        print(f"  ✗ {name}  {detail}")


def expect_http(name, fn, status, contains=""):
    try:
        fn()
        check(name, False, "（没抛 HTTPException）")
        return None
    except HTTPException as e:
        got = e.status_code == status and (not contains or contains in str(e.detail))
        check(name, got, f"status={e.status_code} detail={e.detail}")
        return e


class _Req:  # 路由只从 request 取登录态，这里是空壳
    pass


REQ = _Req()

# ============ 一、读写与归属 ============
print("[1] 读写：存得进、读得出、别人看不到")

set_current(1)
def four(m):
    """取四段成 dict —— pydantic 响应对象不能下标，逐个取属性。"""
    return {"work": m.work, "personal": m.personal, "focus": m.focus, "recent": m.recent}


empty = memory_route.get_memory(REQ)
check("初始四段为空", all(v == "" for v in four(empty).values()), four(empty))
check("初始默认开启", empty.enabled == 1, empty.enabled)
check("上限随响应返回", empty.limits["total_chars"] == 4000, empty.limits)

saved = memory_route.put_memory(
    types.SimpleNamespace(sections=None, work="负责数据集成", personal="", focus="", recent=""),
    REQ)
check("保存后能读回", saved.work == "负责数据集成", saved.work)
check("未提交的段被清空（整份覆盖）", saved.personal == "", saved.personal)
check("响应带上导出文本", "## 工作背景" in saved.text, saved.text[:40])

set_current(2)
other = memory_route.get_memory(REQ)
check("别人读到的是自己的空记忆", other.work == "", other.work)

set_current(1, "admin")
check("管理员看到的也是自己的（不含他人数据）",
      memory_route.get_memory(REQ).work == "负责数据集成",
      memory_route.get_memory(REQ).work)

# ============ 二、上限与开关 ============
print("[2] 上限与开关：超限不写、关不等于删")

set_current(1)
# 两层上限：段级先拦（单段 2000），合计后拦（4000）。接口都要给 400 且说清是哪一层。
expect_http("超出单段上限 → 400 且点名是哪一段",
            lambda: memory_route.put_memory(
                types.SimpleNamespace(sections=None, work="甲" * 4001, personal="", focus="", recent=""),
                REQ),
            400, "超出该段上限")
expect_http("合计超出总上限 → 400（每段都在段上限内）",
            lambda: memory_route.put_memory(
                types.SimpleNamespace(sections=None, work="甲" * 2000, personal="乙" * 2000,
                                      focus="丙" * 2000, recent=""),
                REQ),
            400, "超出上限")
check("超限后原内容没被动过",
      memory_route.get_memory(REQ).work == "负责数据集成",
      memory_route.get_memory(REQ).work)

# 近期动态超条数：能存，但要说清楚挤掉了几条
many = "\n".join(f"第{i}条" for i in range(1, 24))
warn = memory_route.put_memory(
    types.SimpleNamespace(sections=None, work="", personal="", focus="", recent=many), REQ)
check("近期动态超限仍可保存", warn.recent.count("- ") == 20, warn.recent)
check("警告里说清去掉了几条", any("去掉最旧的" in w for w in warn.warnings), warn.warnings)

off = memory_route.set_memory_enabled(types.SimpleNamespace(enabled=False), REQ)
check("关掉开关状态为 0", off.enabled == 0, off.enabled)
check("关掉后内容还在（关 ≠ 删）", off.recent != "", off.recent[:20])
on = memory_route.set_memory_enabled(types.SimpleNamespace(enabled=True), REQ)
check("再打开内容仍在", on.recent != "", on.recent[:20])

# ============ 三、重置 ============
print("[3] 重置：清内容不清开关")

memory_route.set_memory_enabled(types.SimpleNamespace(enabled=True), REQ)
reset = memory_route.reset_memory(REQ)
check("重置清空四段", all(v == "" for v in four(reset).values()), four(reset))
check("重置不动开关", reset.enabled == 1, reset.enabled)

# ============ 四、导入解析 ============
print("[4] 导入：解析结果不落库，认不出的行原样返回")

parsed = memory_route.parse_memory(
    types.SimpleNamespace(text="## 工作背景\n甲\n\n## 近期动态\n- 乙\n- 丙"), REQ)
check("解析出工作背景", parsed.work == "甲", parsed.work)
check("段内多行都归那一段（延续行，不丢）", parsed.recent == "- 乙\n- 丙", parsed.recent)

# 任何段标题之前的裸行：没有任何归属 → 原样返回，不偷偷塞进某一段
orphan = memory_route.parse_memory(
    types.SimpleNamespace(text="没头没尾一句\n## 工作背景\n甲"), REQ)
check("段标题之前的裸行进 unmatched", "没头没尾一句" in orphan.unmatched, orphan.unmatched)
check("裸行不影响后面正常的段", orphan.work == "甲", orphan.work)
check("解析不落库", memory_route.get_memory(REQ).work == "",
      memory_route.get_memory(REQ).work)

# ============ 五、模型整理：只提议、不落库 ============
print("[5] 模型整理：三种结局都要有明确说法")

expect_http("空输入 → 400",
            lambda: memory_route.extract_memory(
                types.SimpleNamespace(text="   "), REQ),
            400, "先写点内容")

_model_state["mode"] = "none"
expect_http("没有可用模型 → 400 且提示去哪解决",
            lambda: memory_route.extract_memory(
                types.SimpleNamespace(text="我是产品经理"), REQ),
            400, "管理员未开放任何模型")

_model_state["mode"] = "ok"
_model_state["reply"] = ("## 工作背景\n负责数据集成\n\n## 个人背景\n\n"
                         "## 当前关注\n- NDI 映射\n\n## 近期动态\n")
got = memory_route.extract_memory(
    types.SimpleNamespace(text="我是产品经理，做数据集成"), REQ)
check("整理结果分好了四段", got.work == "负责数据集成" and got.focus == "- NDI 映射",
      {"work": got.work, "focus": got.focus})
check("整理**没有**落库（只是提议）",
      memory_route.get_memory(REQ).work == "", memory_route.get_memory(REQ).work)

_model_state["reply"] = "## 工作背景\n甲\n\n## 不存在的段\n乙"
got2 = memory_route.extract_memory(types.SimpleNamespace(text="随便"), REQ)
check("模型输出里认不出的部分会还给用户", "## 不存在的段" in got2.unmatched, got2.unmatched)

_model_state["mode"] = "boom"
expect_http("模型调用失败 → 502 且告知原文没丢",
            lambda: memory_route.extract_memory(
                types.SimpleNamespace(text="我是产品经理"), REQ),
            502, "原文没有改动")

_model_state["mode"] = "none"
# ============ 六、进化通道：从对话里提炼新条目 ============
print("[6] 进化：提议不落库、读不到别人的会话、失败有明确说法")

_sessions_tbl = sqlite3.connect(str(TMP / "users.db"))
_sessions_tbl.execute(
    "INSERT INTO sessions (id, user_id, title, created_at, updated_at, messages) "
    "VALUES ('s1', 1, '甲会话', '2026-01-01 00:00:00', '2026-01-02 00:00:00', ?)",
    ("[]",))
_sessions_tbl.execute(
    "INSERT INTO sessions (id, user_id, title, created_at, updated_at, messages) "
    "VALUES ('s2', 2, '乙的会话', '2026-01-01 00:00:00', '2026-01-01 00:00:00', ?)",
    ("[]",))
_sessions_tbl.commit()
_sessions_tbl.close()
_SESSIONS["s1"] = [{"role": "user", "content": "我下周要去宜春出差"}]
_SESSIONS["s2"] = [{"role": "user", "content": "乙的秘密"}]

set_current(1)
# 别人的会话：必须 404，否则就是越权读对话
expect_http("读别人的会话 → 404",
            lambda: memory_route.evolve_memory(
                types.SimpleNamespace(session_id="s2", max_messages=None), REQ),
            404, "不存在或不属于")

# 没有会话可提炼
_SESSIONS["s_empty"] = [{"role": "user", "content": "/reset"}]
expect_http("会话里没有可提炼内容 → 400",
            lambda: memory_route.evolve_memory(
                types.SimpleNamespace(session_id="s1", max_messages=None), REQ),
            400, "")  # 无模型时会先撞 400（无可用模型），见下方顺序说明

_model_state["mode"] = "none"
expect_http("没有可用模型 → 400 且提示去哪解决",
            lambda: memory_route.evolve_memory(
                types.SimpleNamespace(session_id="s1", max_messages=None), REQ),
            400, "没有可用的模型")

_model_state["mode"] = "boom"
expect_http("模型调用失败 → 502 且告知记忆没动",
            lambda: memory_route.evolve_memory(
                types.SimpleNamespace(session_id="s1", max_messages=None), REQ),
            502, "记忆没有改动")

# 正常：解析出提议、去重、不落库
_model_state["mode"] = "ok"
_model_state["reply"] = ("工作背景｜负责经管平台数据集成\n"
                         "近期动态｜去宜春出差\n"
                         "工作背景｜负责经管平台 数据集成！\n"
                         "乱七八糟一行")
# 第三条带空白和标点，第二条完全一致 —— 用来验证去重是「归一化后比对」，
# 不是字符串相等（用户手打时多一个空格不该变成两条）。
ev = memory_route.evolve_memory(
    types.SimpleNamespace(session_id="s1", max_messages=None), REQ)
check("提炼出提议", len(ev.proposals) >= 1, ev.proposals)
check("提议带段名与内容", ev.proposals[0].section_key == "work"
      and "数据集成" in ev.proposals[0].content, ev.proposals[0])
check("认不出的行被丢弃并计数", ev.dropped >= 1, ev.dropped)
check("回了会话信息（用户知道提炼自哪里）",
      ev.session_id == "s1" and ev.message_count >= 1, (ev.session_id, ev.message_count))
check("提炼不落库（只是提议）",
      memory_route.get_memory(REQ).work == "", memory_route.get_memory(REQ).work)

# 已有的记忆不会被重复提议
memory_route.put_memory(
    types.SimpleNamespace(sections=None, work="负责经管平台数据集成", personal="", focus="", recent=""),
    REQ)
ev2 = memory_route.evolve_memory(
    types.SimpleNamespace(session_id="s1", max_messages=None), REQ)
check("不重复提议已经记住的内容（完全一致的）",
      all(p.content != "负责经管平台数据集成" for p in ev2.proposals),
      [p.content for p in ev2.proposals])
check("差一个空格/标点也算重复（归一化后比对）",
      all("数据集成" not in p.content for p in ev2.proposals), [p.content for p in ev2.proposals])
check("重复条数如实上报", ev2.duplicate_skipped == 2, ev2.duplicate_skipped)

print()
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILURES:
    print("失败清单：" + " | ".join(FAILURES))
sys.exit(1 if FAIL else 0)
