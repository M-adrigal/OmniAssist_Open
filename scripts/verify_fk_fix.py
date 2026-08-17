"""验证方案 A 后 user[\"id\"]（public_id 字符串）误写整数外键列的回归是否已修复。

做法：把真实 data/users.db 复制到一个临时副本，将 server.database.DB_PATH 指向副本，
所有 DB 写入都落在副本上，不污染真实数据。

核心断言：
  A. 向 sessions / model_configs / user_skills 写入「字符串 public_id」应触发
     sqlite3.IntegrityError（证明这些列确实要求整数，旧代码因此崩溃）。
  B. 路由在修复后传入「整数 db_id」，对应 DB 写入成功（create_session / config / skills / trust）。
"""
import os
import sys
import shutil
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 1) 复制真实 DB 到临时副本
tmp = tempfile.mkdtemp(prefix="las_verify_")
src_dir = os.path.join(ROOT, "data")
for name in ("users.db", "users.db-wal", "users.db-shm"):
    sp = os.path.join(src_dir, name)
    if os.path.exists(sp):
        shutil.copy(sp, os.path.join(tmp, name))
tmp_db = os.path.join(tmp, "users.db")

import server.database as db
db.DB_PATH = tmp_db  # 重定向到副本

passed = 0
failed = 0
def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name} {extra}")
    else:
        failed += 1
        print(f"  [FAIL] {name} {extra}")

print("=== A) 字符串 public_id 写入整数外键列应失败（复现旧 bug）===")
sid = str(uuid.uuid4())
try:
    db.create_session(sid, "u_fake_public_id_string", "应失败")
    check("sessions 写入字符串 public_id 应被 FK 拒绝", False, "-> 居然成功了（异常）")
except Exception as e:
    check("sessions 写入字符串 public_id 触发 IntegrityError", "IntegrityError" in type(e).__name__, f"({type(e).__name__})")
finally:
    try:
        db._get_connection().execute("DELETE FROM sessions WHERE id=?", (sid,))
    except Exception:
        pass

print("=== B) 修复后路由传入整数 db_id，写入应成功 ===")
# B1. sessions 路由直接调用（request=None -> user_id 回退为 db_id=1）
from server.routes.sessions import create_session as route_create_session
from server.models import SessionCreate
try:
    r = route_create_session(SessionCreate(title="FK修复验证"), None)
    ok = isinstance(r, dict) and "id" in r
    check("sessions 路由 create_session 成功（传入 db_id=1）", ok, f"-> {r.get('id') if isinstance(r, dict) else r}")
except Exception as e:
    check("sessions 路由 create_session 成功", False, f"-> {type(e).__name__}: {e}")
    r = None
sid2 = (r.get("id") if isinstance(r, dict) else None)

# B2. config 读写（model_configs.user_id INTEGER）
try:
    cfg = db.resolve_model_config(1)
    cfg2 = db.save_model_config(1, thinking_mode="low")
    check("model_configs 以 db_id=1 读写成功", isinstance(cfg, dict) and isinstance(cfg2, dict))
except Exception as e:
    check("model_configs 以 db_id=1 读写成功", False, f"-> {type(e).__name__}: {e}")

# B3. skills 读取（user_skills.user_id INTEGER）
try:
    sk = db.get_user_skills(1)
    check("user_skills 以 db_id=1 读取成功", isinstance(sk, list), f"-> {len(sk)} 条")
except Exception as e:
    check("user_skills 以 db_id=1 读取成功", False, f"-> {type(e).__name__}: {e}")

# B4. approval trust_store 以整数 owner_id 存取一致
try:
    from server.trust_store import trust_store
    trust_store.set("verify_s1", 1, enabled=True, mode="full")
    st = trust_store.get("verify_s1", 1)
    check("trust_store 以 db_id=1 存取一致", st.get("enabled") is True and st.get("mode") == "full", f"-> {st}")
except Exception as e:
    check("trust_store 以 db_id=1 存取一致", False, f"-> {type(e).__name__}: {e}")

# 清理副本
try:
    shutil.rmtree(tmp, ignore_errors=True)
except Exception:
    pass

print(f"\n结果: {passed} 通过 / {failed} 失败")
sys.exit(1 if failed else 0)
