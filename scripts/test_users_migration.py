#!/usr/bin/env python3
"""users 表迁移：全新安装 / 存量升级 / 幂等 / 外键存活。

守三件在部署当天才会暴露、事后极难收拾的事：

  1. **全新安装能起来**。建表语句写了 `public_id NOT NULL`，但 `init_db()`
     里创建默认 admin 的 INSERT 若没带这一列，新部署第一次启动就直接
     IntegrityError —— 而且只在"没有存量库"的机器上复现，开发机永远看不到。
  2. **存量库升级不丢 id**。9 张表以 `users.id` 为外键。重建表时若让 id
     重新自增，等于把每个用户的历史数据指给别人，不报错、只是张冠李戴。
  3. **幂等**。迁移函数每次启动都跑，第二次必须什么都不做。

用临时目录跑，不碰 data/users.db。
"""
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["OMNIASSIST_TEST_LOG"] = "1"

import server.database as database  # noqa: E402

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


def fresh_db():
    """切到全新的空库并跑 init_db。"""
    tmp = tempfile.mkdtemp(prefix="mig_fresh_")
    database.DB_PATH = os.path.join(tmp, "users.db")
    database._local.conn = None
    database.init_db()
    return database._get_connection()


def notnull_of(conn, table, col):
    for r in conn.execute(f"PRAGMA table_info({table})"):
        if r[1] == col:
            return r[3]
    return None


# ---------------------------------------------------------------- 1 全新安装
print("\n[1] 全新安装：默认 admin 必须带 public_id")
conn = fresh_db()
admins = [dict(r) for r in conn.execute("SELECT * FROM users WHERE username='admin'")]
check("admin 已创建", len(admins) == 1, f"→ {len(admins)} 个")
if admins:
    a = admins[0]
    check("admin 的 public_id 非空", bool(a.get("public_id")), f"→ {a.get('public_id')!r}")
    check("admin 的 public_id 形态合法", database._get_connection() is not None
          and str(a.get("public_id", "")).startswith("u_")
          and len(a.get("public_id", "")) == 22, f"→ {a.get('public_id')!r}")
    check("admin 的 id 是整数", isinstance(a.get("id"), int))
check("public_id 列已是 NOT NULL", notnull_of(conn, "users", "public_id") == 1)
check("全新库外键无违规", list(conn.execute("PRAGMA foreign_key_check")) == [])

# ---------------------------------------------------------------- 2 幂等
print("\n[2] 幂等：再跑一次 init_db 不应改动任何东西")
before = [dict(r) for r in conn.execute("SELECT id, username, public_id FROM users ORDER BY id")]
conn.close()
database._local.conn = None
database.init_db()
conn = database._get_connection()
after = [dict(r) for r in conn.execute("SELECT id, username, public_id FROM users ORDER BY id")]
check("用户列表完全不变", before == after, f"→\n before={before}\n after={after}")
check("仍未重复创建 admin", len(after) == 1)

# ---------------------------------------------------------------- 3 存量升级
print("\n[3] 存量库升级：旧表（public_id 可空、无 NOT NULL）→ 保留 id 与外键")
tmp = tempfile.mkdtemp(prefix="mig_old_")
old_path = os.path.join(tmp, "users.db")
raw = sqlite3.connect(old_path)
raw.row_factory = sqlite3.Row

# 先让 init_db 把"今天的完整 schema"建好，再把 users 表退化成老形态。
# 为什么要这样绕：手工写一份老 schema 必然与真实表结构漂移（少个 updated_at
# 就够 init_db 里的 CREATE INDEX 报错）。退化只改 users 一处，其余表都是真的。
database.DB_PATH = old_path
database._local.conn = None
database.init_db()
raw.executescript("""
DROP TABLE users;
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    user_type TEXT NOT NULL DEFAULT 'user',
    description TEXT,
    must_change_password INTEGER DEFAULT 0,
    public_id TEXT UNIQUE,          -- 老形态：可空、无 NOT NULL
    created_at TEXT,
    updated_at TEXT
);
""")
# 故意让 id 不连续（真实库经历过多轮建号删号，id 从来不是 1..N）
raw.execute("INSERT INTO users (id, username, password_hash, user_type, public_id)"
            " VALUES (1, 'admin', 'x', 'admin', 'u_aaaaaaaaaaaaaaaaaaaa')")
raw.execute("INSERT INTO users (id, username, password_hash, user_type, public_id)"
            " VALUES (7, 'haspid', 'x', 'user', 'u_bbbbbbbbbbbbbbbbbbbb')")
raw.execute("INSERT INTO users (id, username, password_hash, user_type, public_id)"
            " VALUES (42, 'nopid', 'x', 'user', NULL)")
raw.execute("INSERT INTO user_secrets (user_id, key_name, encrypted_value) VALUES (7, 'k1', 'v1')")
raw.execute("INSERT INTO user_secrets (user_id, key_name, encrypted_value) VALUES (42, 'k2', 'v2')")
raw.execute("INSERT INTO sessions (id, user_id, title) VALUES ('s1', 7, 't7')")
raw.execute("INSERT INTO sessions (id, user_id, title) VALUES ('s42', 42, 't42')")
raw.commit()
old_users = [dict(r) for r in raw.execute("SELECT id, username, public_id FROM users ORDER BY id")]
check("退化成功：老形态 public_id 可空", notnull_of(raw, "users", "public_id") == 0)
old_secrets = raw.execute("SELECT COUNT(*) FROM user_secrets").fetchone()[0]
old_sessions = raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
raw.close()

database.DB_PATH = old_path
database._local.conn = None
database.init_db()
conn = database._get_connection()

new_users = [dict(r) for r in conn.execute("SELECT id, username, public_id FROM users ORDER BY id")]
check("用户数不变", len(new_users) == len(old_users), f"→ {len(new_users)} vs {len(old_users)}")
check("所有原 id 都还在", {u["id"] for u in new_users} == {u["id"] for u in old_users},
      f"→ {sorted(u['id'] for u in new_users)}")
check("原有 public_id 一字未改",
      all({u["username"]: u["public_id"] for u in old_users if u["public_id"]}.get(n) == pid
          for n, pid in {u["username"]: u["public_id"] for u in new_users if u["public_id"]}.items()
          if n in {x["username"] for x in old_users if x["public_id"]}),
      "→ 迁移不应重新生成已有的 public_id")
_nopid = [u for u in new_users if u["username"] == "nopid"][0]
check("NULL 的 public_id 已回填", bool(_nopid["public_id"]), f"→ {_nopid['public_id']!r}")
check("回填的 public_id 形态合法", str(_nopid["public_id"]).startswith("u_")
      and len(str(_nopid["public_id"])) == 22)
check("升级后列是 NOT NULL", notnull_of(conn, "users", "public_id") == 1)
check("外键表行数未变", conn.execute("SELECT COUNT(*) FROM user_secrets").fetchone()[0] == old_secrets)
check("会话行数未变", conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == old_sessions)
check("外键关系仍成立（user_secrets 都能找到主人）",
      list(conn.execute("PRAGMA foreign_key_check")) == [],
      f"→ {list(conn.execute('PRAGMA foreign_key_check'))}")

# ---------------------------------------------------------------- 4 升级后可用
print("\n[4] 升级后的库能正常用（不是「只迁移成功但用不了」）")
u7 = database.get_user_by_id(7)
check("get_user_by_id(7) 拿得到", u7 is not None and u7["username"] == "haspid")
check("id=7 的 public_id 未变", u7 and u7["public_id"] == "u_bbbbbbbbbbbbbbbbbbbb")
u_pid = database.get_user_by_public_id("u_bbbbbbbbbbbbbbbbbbbb")
check("按 public_id 反查得到同一个人", u_pid is not None and u_pid["id"] == 7)
new_u = database.create_user("aftermig", "Abc12345!x", "user", "")
check("迁移后仍能建新号", new_u is not None and new_u.get("public_id"))
check("新建号的 id 不与存量冲突",
      new_u["id"] not in (1, 7, 42), f"→ {new_u['id']}")

# 第二次跑仍然幂等
conn.close()
database._local.conn = None
database.init_db()
conn = database._get_connection()
check("升级路径二次执行无副作用",
      [dict(r) for r in conn.execute("SELECT id, username, public_id FROM users ORDER BY id")]
      == new_users + [{"id": new_u["id"], "username": "aftermig", "public_id": new_u["public_id"]}]
      or conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == len(new_users) + 1)

# ---------------------------------------------------------------- 5 列清单防线
print("\n[5] 重建语句的列清单不能静默丢列（将来加列时的保险）")
# 场景：有人给 users 加了新列，但某个存量库还停在"public_id 可空"的老形态。
# 重建语句里写死了一份列清单，若不核对就会把那列连同数据一起丢掉 —— 不报错。
tmp2 = tempfile.mkdtemp(prefix="mig_newcol_")
p2 = os.path.join(tmp2, "users.db")
database.DB_PATH = p2
database._local.conn = None
database.init_db()
_raw2 = sqlite3.connect(p2)
_raw2.executescript("""
DROP TABLE users;
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, user_type TEXT NOT NULL DEFAULT 'user',
    description TEXT, must_change_password INTEGER DEFAULT 0,
    public_id TEXT UNIQUE, created_at TEXT, updated_at TEXT,
    future_col TEXT DEFAULT 'must_survive'
);
""")
_raw2.execute("INSERT INTO users (id,username,password_hash,user_type,public_id,future_col)"
              " VALUES (5,'u5','x','user','u_cccccccccccccccccccc','keepme')")
_raw2.commit()
_raw2.close()
database._local.conn = None
database.init_db()
conn2 = database._get_connection()
_cols = [r[1] for r in conn2.execute("PRAGMA table_info(users)")]
check("清单外的新列没有丢", "future_col" in _cols, f"→ {_cols}")
check("新列的数据没丢",
      conn2.execute("SELECT future_col FROM users WHERE id=5").fetchone()["future_col"] == "keepme")
check("原有 public_id 没变",
      conn2.execute("SELECT public_id FROM users WHERE id=5").fetchone()["public_id"]
      == "u_cccccccccccccccccccc")
check("放弃加约束（宁愿不加，也不丢列）",
      notnull_of(conn2, "users", "public_id") == 0, "→ 应保持可空并留下告警")

print("\n" + "=" * 60)
print(f"结果：通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败明细：")
    for f in FAILED:
        print("  -", f)
print("=" * 60)
sys.exit(1 if FAIL else 0)
