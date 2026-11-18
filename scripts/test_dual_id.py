#!/usr/bin/env python3
"""双 ID 体系：形态 / 边界 / 混用失败模式。

守的是「一个对内、一个对外」这条线不被悄悄抹平：

  1. `get_current_user()` 返回 `public_id` + `db_id`，**没有 `"id"` 键**。
     历史上键名叫 "id" 而值是 public_id，于是 `user["id"]` 在每条路径上都
     语义不明 —— 有人当字符串拼目录，有人当整数查外键，后者静默返回 0 条。
  2. `_generate_token()` 只收 public_id。旧实现形参叫 user_id、内部又查一次库，
     传整数和传 public_id **都能跑通**，结果对错靠运气；现在传整数直接 ValueError。
  3. 整数外键接口拿 public_id 去查会静默返回空 —— 把这条失败模式写成断言，
     以后谁再混用，这里先红。
  4. 跨用户隔离：拿 A 的 token 看不到 B 的数据。

不需要网络，全部用临时库，20 秒内跑完。
"""
import json
import base64
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["OMNIASSIST_TEST_LOG"] = "1"

PASS = FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name} {extra}")


# ---------------------------------------------------------------- 临时库
TMP = tempfile.mkdtemp(prefix="dual_id_")
import server.database as database  # noqa: E402

database.DB_PATH = os.path.join(TMP, "users.db")
database._local.conn = None
database.init_db()

conn = database._get_connection()
now = database.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
for _name in ("alice", "bob"):
    conn.execute(
        "INSERT INTO users (username, password_hash, user_type, public_id, created_at, updated_at)"
        " VALUES (?, 'x', 'user', ?, ?, ?)",
        (_name, database._gen_public_id(), now, now),
    )
conn.commit()
alice = dict(conn.execute("SELECT * FROM users WHERE username='alice'").fetchone())
bob = dict(conn.execute("SELECT * FROM users WHERE username='bob'").fetchone())
# alice 的一条会话：user_id 是 INTEGER 外键
conn.execute("INSERT INTO sessions (id, user_id, title) VALUES ('s-alice', ?, 'alice 的会话')",
             (alice["id"],))
conn.commit()

from server.routes.auth import (  # noqa: E402
    _generate_token, _looks_like_public_id, _sign_payload, get_current_user,
)


class _FakeReq:
    """只带 token 的最小 Request 替身（get_current_user 只读这两处）。"""

    def __init__(self, tok):
        self.cookies = {"auth_token": tok}
        self.headers = {}

    def __getattr__(self, n):
        return None


# ---------------------------------------------------------------- 1 形态校验
print("\n[1] public_id 形态校验（宽松匹配等于没校验）")
for _ in range(3):
    pid = database._gen_public_id()
    check(f"生成的 {pid} 通过", _looks_like_public_id(pid))
for bad, why in [(1, "整数"), ("1", "数字字符串"), ("u_abc", "位数不足"),
                 ("U_" + "a" * 20, "大写前缀"),
                 ("u_" + "A" * 20, "大写 hex"), (None, "None"), ("", "空串"),
                 ("u_" + "g" * 20, "非 hex 字符")]:
    check(f"拒绝 {why} {bad!r}", _looks_like_public_id(bad) is False)

# ---------------------------------------------------------------- 2 返回结构
print("\n[2] get_current_user 返回两个 ID，且没有歧义的 'id' 键")
tok_a = _generate_token(alice["public_id"], "alice", "user")
tok_b = _generate_token(bob["public_id"], "bob", "user")
me = get_current_user(_FakeReq(tok_a))
check("能解析出 alice", me is not None)
check("public_id 正确", me and me.get("public_id") == alice["public_id"])
check("db_id 正确", me and me.get("db_id") == alice["id"])
check("没有 'id' 键", me is not None and "id" not in me,
      f"→ 实际键: {sorted(me) if me else None}")

# ---------------------------------------------------------------- 3 签发收窄
print("\n[3] _generate_token 只接受 public_id（消灭'碰巧能跑'）")
payload_b64 = tok_a.split(".")[0]
payload_b64 += "=" * (-len(payload_b64) % 4)
_pl = json.loads(base64.urlsafe_b64decode(payload_b64))
check("载荷 uid == public_id", _pl.get("uid") == alice["public_id"], f"→ {_pl.get('uid')}")
check("载荷 uid 不是纯数字", not str(_pl.get("uid", "")).isdigit())
for bad in (alice["id"], str(alice["id"]), 0, "0", None, "u_short",
            alice["public_id"].upper(), ""):
    try:
        _generate_token(bad, "x", "user")
        check(f"拒绝签发给 {bad!r}", False, "→ 竟然签发了")
    except ValueError:
        check(f"拒绝签发给 {bad!r}", True)
    except Exception as e:
        check(f"拒绝签发给 {bad!r}", False, f"→ 抛 {type(e).__name__} 而非 ValueError")

# ---------------------------------------------------------------- 4 混用失败模式
print("\n[4] 混用失败模式：public_id 查 INTEGER 列 = 静默 0 条（不报错）")
_n = conn.execute("SELECT COUNT(*) c FROM sessions WHERE user_id = ?",
                  (alice["public_id"],)).fetchone()["c"]
check("拿 public_id 查 INTEGER 外键 → 静默 0 条", _n == 0, f"→ {_n} 条（不抛异常）")
_n2 = conn.execute("SELECT COUNT(*) c FROM sessions WHERE user_id = ?",
                   (alice["db_id"] if "db_id" in alice else alice["id"],)).fetchone()["c"]
check("拿 db_id 查同一列 → 查到数据", _n2 == 1, f"→ {_n2} 条")

# 写入方向会报错（这个相对好查，但一起守）
try:
    conn.execute("INSERT INTO sessions (id, user_id, title) VALUES ('s-bad', ?, 'x')",
                 (alice["public_id"],))
    conn.commit()
    check("拿 public_id 写 INTEGER 外键 → 报错", False, "→ 竟然写进去了")
except Exception as e:
    conn.rollback()
    check("拿 public_id 写 INTEGER 外键 → 报错", "FOREIGN KEY" in str(e).upper() or "constraint" in str(e).lower(),
          f"→ {type(e).__name__}: {e}")

# ---------------------------------------------------------------- 5 伪造与越权
print("\n[5] 伪造 token / 跨用户")
forged = _sign_payload({"uid": str(bob["id"]), "un": "bob", "ut": "user",
                        "iat": 0, "exp": 4102444800})
check("拒绝：签名合法但 uid 是整数", get_current_user(_FakeReq(forged)) is None)
for name, t in [("空串", ""), ("乱码", "abc.def.ghi"), ("只改载荷不重签", "x" + tok_a[1:])]:
    check(f"拒绝：{name}", get_current_user(_FakeReq(t)) is None)

# 跨用户：alice 的会话行 user_id 是 alice 的 db_id，bob 的 db_id 取不到它
_bob_view = conn.execute("SELECT COUNT(*) c FROM sessions WHERE user_id = ?",
                         (bob["id"],)).fetchone()["c"]
check("bob 查不到 alice 的会话", _bob_view == 0, f"→ {_bob_view} 条")

# ---------------------------------------------------------------- 6 登录链路
print("\n[6] 登录链路：authenticate 的返回值必须够签 token 用")
# 这一条是踩出来的：改名时把 auth.login 写成 user["public_id"]，但
# authenticate() 的 SELECT 里根本没有 public_id 列 → 任何人登录都 KeyError 500。
# 症状是"密码明明对却报服务器错误"，而且只有真登录才触发，静态检查看不出来。
_loginuser = database.create_user("loginchk", "Abc12345!x", "user", "")
_a = database.authenticate("loginchk", "Abc12345!x")
check("authenticate 成功", _a is not None)
check("返回值含 id", _a and _a.get("id") == _loginuser["id"])
check("返回值含 public_id", _a and _a.get("public_id") == _loginuser["public_id"],
      f"→ {sorted(_a) if _a else None}")
try:
    _t = _generate_token(_a["public_id"], _a["username"], _a["user_type"])
    check("登录能拿到 token", bool(_t))
    _back = get_current_user(_FakeReq(_t))
    check("token 能反解回同一用户", _back and _back.get("db_id") == _loginuser["id"])
except KeyError as e:
    check("登录能拿到 token", False, f"→ KeyError {e}（authenticate 少返回字段）")

# ---------------------------------------------------------------- 7 约束
print("\n[7] 表约束：public_id 必须 NOT NULL + UNIQUE")
_notnull = [r[3] for r in conn.execute("PRAGMA table_info(users)") if r[1] == "public_id"]
check("public_id 是 NOT NULL", _notnull and _notnull[0] == 1, f"→ notnull={_notnull}")
try:
    conn.execute("INSERT INTO users (username, password_hash, user_type, public_id, created_at,"
                 " updated_at) VALUES ('dup', 'x', 'user', ?, ?, ?)",
                 (alice["public_id"], now, now))
    conn.commit()
    check("拒绝重复 public_id", False, "→ 竟然插入了")
except Exception as e:
    conn.rollback()
    check("拒绝重复 public_id", "UNIQUE" in str(e).upper() or "constraint" in str(e).lower(),
          f"→ {e}")
try:
    conn.execute("INSERT INTO users (username, password_hash, user_type, public_id, created_at,"
                 " updated_at) VALUES ('nullpid', 'x', 'user', NULL, ?, ?)", (now, now))
    conn.commit()
    check("拒绝 NULL public_id", False, "→ 竟然插入了")
except Exception as e:
    conn.rollback()
    check("拒绝 NULL public_id", "NOT NULL" in str(e).upper(), f"→ {e}")

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败明细：")
    for f in FAILED:
        print("  -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
