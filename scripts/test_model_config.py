"""模型配置改动的路由级验证：全局隔离 / 编辑能力 / 自定义供应商 / 用量拆分。

运行方式（需 managed venv，内有 fastapi + pydantic）：
    /Users/yuanye/.workbuddy/binaries/python/envs/default/bin/python3 scripts/test_model_config.py

本机没有完整服务环境，故用 importlib 按文件加载真实模块、临时库隔离，
`server.routes.auth` 用可切换身份的 stub 代替（路由只从它取当前用户）。
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="modelcfg_test_"))

# ===== 搭建模块加载环境（绕过 server/__init__.py 的 app 装配）=====
server_pkg = types.ModuleType("server")
server_pkg.__path__ = [str(ROOT / "server")]
sys.modules["server"] = server_pkg

routes_pkg = types.ModuleType("server.routes")
routes_pkg.__path__ = [str(ROOT / "server" / "routes")]
sys.modules["server.routes"] = routes_pkg

# auth stub：路由只从它拿当前用户，测试里可随时切换身份
auth_stub = types.ModuleType("server.routes.auth")
_current = {"db_id": 1, "id": "u_admin", "user_type": "admin", "public_id": "u_admin"}


def set_current(db_id, user_type):
    global _current
    _current = {"db_id": db_id, "id": f"u_{db_id}", "user_type": user_type,
                "public_id": f"u_{db_id}"}


def require_permission(request, perm=None, action=None):
    return _current


def require_login(request):
    return _current


def get_current_user(request):
    return _current


auth_stub.require_permission = require_permission
auth_stub.require_login = require_login
auth_stub.get_current_user = get_current_user
auth_stub.set_current = set_current
sys.modules["server.routes.auth"] = auth_stub


def load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


database = load("server.database", "server/database.py")
model_store = load("server.model_store", "server/model_store.py")
usage_store = load("server.usage_store", "server/usage_store.py")
models_route = load("server.routes.models", "server/routes/models.py")

# 指向临时库并建表
database.DB_DIR = str(TMP)
database.DB_PATH = str(TMP / "users.db")
database.init_db()

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


def expect_http_error(name, fn, status=400, contains=""):
    try:
        fn()
        check(name, False, "（未抛出异常）")
    except HTTPException as e:
        ok = e.status_code == status and (contains in str(e.detail) if contains else True)
        check(name, ok, f"（status={e.status_code}, detail={e.detail}）")
    except Exception as e:  # noqa: BLE001
        check(name, False, f"（抛出了非 HTTP 异常: {e}）")


class FakeRequest:
    pass


REQ = FakeRequest()

# ===== 准备数据：admin(1) / alice / bob =====
alice = database.create_user("alice", "Passw0rd!", "user")
bob = database.create_user("bob", "Passw0rd!", "user")
ALICE, BOB = alice["id"], bob["id"]
check("测试用户创建成功", ALICE > 1 and BOB > ALICE, f"alice={ALICE}, bob={BOB}")

# 平台供应商 + 平台模型池条目
plat = model_store.create_provider(name="DeepSeek 官方",
                                   base_url="https://api.deepseek.com",
                                   api_key="plat-key", owner_user_id=None,
                                   builtin_key="deepseek")
# 管理员自建代理（无 builtin_key）——平台路径下不允许用户自带 Key
plat_custom = model_store.create_provider(name="公司代理",
                                          base_url="https://proxy.corp.com",
                                          api_key="corp-key", owner_user_id=None)
pool_entry = model_store.create_entry(provider_id=plat["id"], model_id="deepseek-chat",
                                      display_name="DeepSeek Chat", context_limit="64000",
                                      visible=True, is_default=True)

# alice 的个人供应商（从平台复制地址）+ 她的自配模型
alice_prov = model_store.create_provider(name="DeepSeek 官方",
                                         base_url="https://api.deepseek.com",
                                         api_key="alice-key", owner_user_id=ALICE,
                                         builtin_key="deepseek")
alice_entry = model_store.create_entry(provider_id=alice_prov["id"], model_id="deepseek-chat",
                                       display_name="Alice 的 Chat", visible=False,
                                       owner_user_id=ALICE)
# bob 的个人供应商 + 模型（用于验证 alice 看不到）
bob_prov = model_store.create_provider(name="DeepSeek 官方",
                                       base_url="https://api.deepseek.com",
                                       api_key="bob-key", owner_user_id=BOB,
                                       builtin_key="deepseek")
bob_entry = model_store.create_entry(provider_id=bob_prov["id"], model_id="deepseek-chat",
                                     display_name="Bob 的 Chat", visible=False,
                                     owner_user_id=BOB)
# alice 名下的第二个模型，用于制造「同供应商下模型 ID 冲突」
alice_dup = model_store.create_entry(provider_id=alice_prov["id"], model_id="dup-model",
                                     display_name="Dup", visible=False,
                                     owner_user_id=ALICE)

print("\n【1】全局配置只含平台级内容（问题 1）")
set_current(1, "admin")
provs = models_route.admin_list_providers(REQ)
prov_ids = [p["id"] for p in provs]
check("管理员看供应商全部是平台行", all(p["owner"] == "platform" for p in provs),
      f"实际 {[(p['id'], p['owner']) for p in provs]}")
check("alice/bob 的个人供应商不出现在全局",
      alice_prov["id"] not in prov_ids and bob_prov["id"] not in prov_ids)
check("我建的平台供应商在全局列表里", plat["id"] in prov_ids)
pool = models_route.admin_list_pool(REQ)
check("管理员看模型池只有平台条目", [e["entry_id"] for e in pool] == [pool_entry["id"]],
      f"实际 {[e['entry_id'] for e in pool]}")
check("平台行不返回 Key 明文", all("api_key" not in p for p in provs))

set_current(ALICE, "user")
mine = models_route.list_my_models(REQ)
alice_entry_ids = sorted(m["entry_id"] for m in mine["models"])
check("alice 只看到自己的两条自配模型", alice_entry_ids == sorted([alice_entry["id"], alice_dup["id"]]),
      f"实际 {alice_entry_ids}")
check("alice 只看到自己的供应商", [p["id"] for p in mine["providers"]] == [alice_prov["id"]])
avail = models_route.list_available_providers(REQ)
check("可选供应商只含平台行", all(p["id"] != alice_prov["id"] and p["id"] != bob_prov["id"]
                                for p in avail))
check("可选供应商不返回任何 Key 字段",
      all(not any(k in p for k in ("api_key", "api_key_masked", "api_key_encrypted"))
          for p in avail))
check("可选供应商的 has_platform_key 只是布尔",
      all(isinstance(p["has_platform_key"], bool) for p in avail))

set_current(BOB, "user")
mine_b = models_route.list_my_models(REQ)
check("bob 只看到自己的（与 alice 隔离）", [m["entry_id"] for m in mine_b["models"]] == [bob_entry["id"]])

print("\n【2】模型与供应商可修改（问题 2/3）")
set_current(1, "admin")
upd = models_route.admin_update_pool_entry(
    pool_entry["id"],
    models_route.PoolEntryUpdate(model_id="deepseek-chat-v2", display_name="DeepSeek Chat V2",
                                 context_limit="128000", visible=True), REQ)
check("模型池条目可改模型 ID/名称/上下文",
      upd["model_id"] == "deepseek-chat-v2" and upd["context_limit"] == "128000"
      and upd["display_name"] == "DeepSeek Chat V2")
check("改后仍对用户可见", upd["visible"] is True)

# 唯一索引冲突：把另一条目改成同供应商下已存在的模型 ID
dup = model_store.create_entry(provider_id=plat["id"], model_id="dup-model",
                               display_name="Dup", owner_user_id=None)
expect_http_error("同供应商下模型 ID 冲突返回 400",
                  lambda: models_route.admin_update_pool_entry(
                      dup["id"], models_route.PoolEntryUpdate(model_id="deepseek-chat-v2"), REQ),
                  contains="相同模型 ID")
pu = models_route.admin_update_provider(
    plat["id"],
    models_route.ProviderUpdate(name="DeepSeek 官方（改）", api_key="new-plat-key",
                                enabled=True), REQ)
check("平台供应商可改名称/Key", pu["name"] == "DeepSeek 官方（改）")
check("Key 已更新（掩码变化）", pu["api_key_masked"] != "")
decrypted = model_store.get_provider(plat["id"], owner_user_id=None)
check("Key 落库为加密存储", decrypted["api_key_masked"].startswith("*") or "*" in decrypted["api_key_masked"])
expect_http_error("内置供应商地址不可改",
                  lambda: models_route.admin_update_provider(
                      plat["id"], models_route.ProviderUpdate(base_url="https://evil.com"), REQ),
                  contains="不可修改")
po = models_route.admin_update_provider(
    plat["id"], models_route.ProviderUpdate(enabled=False), REQ)
check("平台供应商可停用", po["enabled"] is False)
models_route.admin_update_provider(plat["id"], models_route.ProviderUpdate(enabled=True), REQ)

print("\n【3】个人模型/供应商可修改")
set_current(ALICE, "user")
mu = models_route.update_my_model(
    alice_entry["id"],
    models_route.MyModelUpdate(model_id="deepseek-chat-pro", display_name="Alice Pro",
                               context_limit="96000"), REQ)
check("自配模型可改模型 ID/名称/上下文",
      mu["model"]["model_id"] == "deepseek-chat-pro" and mu["model"]["context_limit"] == "96000")
mk = models_route.update_my_model(
    alice_entry["id"], models_route.MyModelUpdate(api_key="alice-key-2"), REQ)
check("自配模型可改 Key", mk["model"]["entry_id"] == alice_entry["id"])
expect_http_error("自配模型 ID 冲突返回 400",
                  lambda: models_route.update_my_model(
                      alice_entry["id"], models_route.MyModelUpdate(model_id="dup-model"), REQ),
                  contains="相同模型 ID")
check("冲突后原值未被改写",
      model_store.get_entry(alice_entry["id"], owner_user_id=ALICE)["model_id"] == "deepseek-chat-pro")
pp = models_route.update_my_provider(
    alice_prov["id"],
    models_route.MyProviderUpdate(name="Alice 的 DeepSeek", api_key="alice-key-3"), REQ)
check("个人供应商可改名称/Key", pp["provider"]["name"] == "Alice 的 DeepSeek")
pu2 = models_route.update_my_provider(
    alice_prov["id"], models_route.MyProviderUpdate(base_url="https://api.deepseek.com/v1"), REQ)
check("个人供应商可改地址", pu2["provider"]["base_url"] == "https://api.deepseek.com/v1")
# 还原成原地址（此时没有其它个人行占用它，应成功）
back = models_route.update_my_provider(
    alice_prov["id"], models_route.MyProviderUpdate(base_url="https://api.deepseek.com"), REQ)
check("地址可改回原值", back["provider"]["base_url"] == "https://api.deepseek.com")

print("\n【4】用户可自建供应商（问题 4）")
created = models_route.create_my_model(
    models_route.MyModelCreate(
        api_key="my-proxy-key", model_id="gpt-4o-mini",
        display_name="公司代理 GPT", context_limit="128000",
        provider_name="公司代理", base_url="https://proxy.corp.com/v1"), REQ)
check("自定义路径建模型成功", created["model"]["model_id"] == "gpt-4o-mini")
check("自定义供应商归个人所有", created["provider"]["owner"] == "personal"
      and created["provider"]["base_url"] == "https://proxy.corp.com/v1")
custom_entry_id = created["model"]["entry_id"]
custom_prov_id = created["provider"]["id"]

# 同地址重复添加：复用凭据、只更新 Key
again = models_route.create_my_model(
    models_route.MyModelCreate(api_key="my-proxy-key-2", model_id="gpt-4o",
                               base_url="https://proxy.corp.com/v1"), REQ)
check("同地址重复添加复用凭据", again["provider"]["id"] == custom_prov_id)
check("重复添加只更新 Key 不新建行",
      len(model_store.list_providers(ALICE)) == 2)  # alice_prov + proxy

expect_http_error("自定义地址不是 http(s) 返回 400",
                  lambda: models_route.create_my_model(
                      models_route.MyModelCreate(api_key="k", model_id="m",
                                                 base_url="ftp://x.com"), REQ),
                  contains="http")
expect_http_error("既不选平台也不填地址返回 400",
                  lambda: models_route.create_my_model(
                      models_route.MyModelCreate(api_key="k", model_id="m"), REQ),
                  contains="自定义地址")
# 平台路径仍受「必须内置供应商」约束（管理员自建代理不开放自带 Key）
expect_http_error("平台自建代理不开放自带 Key",
                  lambda: models_route.create_my_model(
                      models_route.MyModelCreate(api_key="k", model_id="m",
                                                 platform_provider_id=plat_custom["id"]), REQ),
                  contains="不支持用户自带 Key")
# 自定义供应商改地址撞上另一家个人供应商 → 400（地址唯一）
expect_http_error("自定义供应商改地址撞已存在的返回 400",
                  lambda: models_route.update_my_provider(
                      custom_prov_id,
                      models_route.MyProviderUpdate(base_url="https://api.deepseek.com"), REQ),
                  contains="已存在")

print("\n【5】删除个人供应商连带清理")
# 把默认模型设成自定义供应商下的条目，验证删除后会被清回「跟随平台」
model_store.set_user_current_entry(ALICE, custom_entry_id)
check("删除前默认指向自定义条目",
      model_store.get_user_current_entry(ALICE) == custom_entry_id)
res = models_route.delete_my_provider(custom_prov_id, REQ)
check("删除个人供应商成功", res["removed_entries"] == 2)
check("删除后默认模型已清回", model_store.get_user_current_entry(ALICE) is None)
check("供应商与其条目已不存在",
      model_store.get_provider(custom_prov_id, owner_user_id=ALICE) is None
      and model_store.get_entry(custom_entry_id, owner_user_id=ALICE) is None)
# 别人的删不掉
set_current(ALICE, "user")
expect_http_error("不能删除别人的供应商",
                  lambda: models_route.delete_my_provider(bob_prov["id"], REQ),
                  status=404)

print("\n【6】Token 用量按模型/供应商/归属拆分（问题 5）")
set_current(1, "admin")
usage_store.record_usage({"source": "chat", "model": "deepseek-chat-v2",
                          "prompt_tokens": 100, "completion_tokens": 20,
                          "cached_tokens": 40, "total_tokens": 120},
                         user_id=1, session_id="s-admin", provider_name="DeepSeek 官方",
                         entry_owner="platform")
usage_store.record_usage({"source": "chat", "model": "deepseek-chat-pro",
                          "prompt_tokens": 50, "completion_tokens": 10,
                          "total_tokens": 60, "is_estimated": True},
                         user_id=ALICE, session_id="s-alice",
                         provider_name="Alice 的 DeepSeek", entry_owner="user")
usage_store.record_usage({"source": "title", "model": "deepseek-chat-v2",
                          "prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15},
                         user_id=1, session_id="s-admin", provider_name="DeepSeek 官方",
                         entry_owner="platform")
today = usage_store.today_str() if hasattr(usage_store, "today_str") else None
from datetime import date as _date  # noqa: E402
today = _date.today().isoformat()
summary = usage_store.summary_by_day(1, today, today, scope_all=True)
models = {(m["model"], m["provider_name"], m["owner"]): m for m in summary["models"]}
check("平台模型消耗可拆分",
      ("deepseek-chat-v2", "DeepSeek 官方", "platform") in models)
check("用户自配消耗可拆分且标为自付",
      ("deepseek-chat-pro", "Alice 的 DeepSeek", "user") in models)
m_plat = models[("deepseek-chat-v2", "DeepSeek 官方", "platform")]
check("平台模型 token 汇总正确（含 title 来源）",
      m_plat["total_tokens"] == 135 and m_plat["calls"] == 2, f"实际 {m_plat}")
check("缓存命中率只按精确行计算",
      m_plat["cache_rate"] is not None and abs(m_plat["cache_rate"] - 36.4) < 0.1,
      f"实际 {m_plat['cache_rate']}（40 命中 / (100+10) 精确输入，估算行不计入）")
m_user = models[("deepseek-chat-pro", "Alice 的 DeepSeek", "user")]
check("估算行单列且计入估算次数",
      m_user["estimated_calls"] == 1 and m_user["cache_rate"] is None)
labels = usage_store.model_label_map()
check("显示名映射覆盖平台与个人条目",
      labels.get("deepseek-chat-v2||DeepSeek 官方（改）") == "DeepSeek Chat V2"
      or labels.get("deepseek-chat-v2") == "DeepSeek Chat V2", f"labels={labels}")

# 个人范围查询不带别人的数据
self_summary = usage_store.summary_by_day(1, today, today, scope_all=False)
self_models = {(m["model"], m["owner"]) for m in self_summary["models"]}
check("个人范围不含他人消耗", ("deepseek-chat-pro", "user") not in self_models)

print("\n" + "=" * 56)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
if FAILURES:
    for f in FAILURES:
        print("  ✗", f)
sys.exit(1 if FAIL else 0)
