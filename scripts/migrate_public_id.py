#!/usr/bin/env python3
"""
迁移脚本：为用户表引入对外不透明 public_id（方案 A）。

为什么要先跑这个脚本、再重启服务：
- 现有代码已改为「对外只暴露 public_id、JWT/前端/文件目录均用 public_id」，
  但存量用户的 public_id 为 NULL。若先重启服务，get_current_user 会因
  get_user_by_public_id 解析不到而把所有人（含 admin）判为未登录 → 全员 401 锁死。
- 因此本脚本在重启前完成三件事：
    (1) 确保 users 表存在 public_id 列（无约束加列，避免 UNIQUE 非空表限制）；
    (2) 为所有 public_id 为 NULL 的用户生成唯一 public_id 并写回；
    (3) 将 document_output/{整数id}/ 重命名为 document_output/{public_id}/（含子目录）。
        同样处理 uploads/{整数id}/（若存在）。

可重复执行（幂等）：已存在 public_id 的用户跳过；目标目录已存在则跳过重命名。

用法：
    python3 scripts/migrate_public_id.py
"""

import os
import sqlite3
import sys
import uuid

# 项目根目录（脚本位于 scripts/ 下）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "users.db")

OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "document_output")
UPLOAD_ROOT = os.path.join(PROJECT_ROOT, "uploads")

GEN_PREFIX = "u_"


def gen_public_id() -> str:
    """与 database._gen_public_id 保持一致：u_ + uuid4 前 20 位十六进制。"""
    return GEN_PREFIX + uuid.uuid4().hex[:20]


def main() -> int:
    if not os.path.isfile(DB_PATH):
        print(f"[错误] 找不到数据库文件: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")

    # (1) 确保列存在
    try:
        conn.execute("ALTER TABLE users ADD COLUMN public_id TEXT")
        print("[1/3] 已新增 public_id 列")
    except sqlite3.OperationalError as e:
        print(f"[1/3] public_id 列已存在，跳过 ({e})")

    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id ON users(public_id)")
        print("[1/3] 唯一索引 idx_users_public_id 就绪")
    except sqlite3.OperationalError as e:
        print(f"[1/3] 唯一索引跳过 ({e})", file=sys.stderr)

    # (2) 回填 public_id
    rows = conn.execute("SELECT id, username, public_id FROM users ORDER BY id").fetchall()
    backfilled = 0
    for r in rows:
        uid, username, pid = r["id"], r["username"], r["public_id"]
        if pid:
            continue
        # 生成不冲突的 public_id
        while True:
            cand = gen_public_id()
            exists = conn.execute(
                "SELECT 1 FROM users WHERE public_id = ?", (cand,)
            ).fetchone()
            if not exists:
                break
        conn.execute("UPDATE users SET public_id = ? WHERE id = ?", (cand, uid))
        backfilled += 1
        print(f"[2/3] 用户 id={uid} ({username}) -> public_id={cand}")

    conn.commit()
    if backfilled == 0:
        print("[2/3] 无需回填，所有用户已有 public_id")

    # 回填后重新读取（上面的 rows 是回填前的快照，public_id 仍是 NULL），
    # 否则目录重命名循环会因 public_id 为空而全部跳过。
    rows = conn.execute("SELECT id, public_id FROM users ORDER BY id").fetchall()

    # (3) 重命名目录
    renamed = 0
    for r in rows:
        uid, pid = r["id"], r["public_id"]
        if not pid:
            continue
        for root in (OUTPUT_ROOT, UPLOAD_ROOT):
            old = os.path.join(root, str(uid))
            new = os.path.join(root, pid)
            if os.path.isdir(old) and not os.path.exists(new):
                try:
                    os.rename(old, new)
                    renamed += 1
                    print(f"[3/3] 目录已重命名: {old} -> {new}")
                except OSError as e:
                    print(f"[3/3] 重命名失败 {old}: {e}", file=sys.stderr)
            elif not os.path.exists(old):
                # 该用户无产出/上传目录，跳过（无需创建，运行时按需创建）
                pass
            elif os.path.exists(new):
                print(f"[3/3] 目标已存在，跳过: {old} (已存在 {new})")
    print(f"[3/3] 共重命名 {renamed} 个目录")

    # (3.1) 确保当前每个用户的产出目录存在（含子目录）。
    # 若某用户此前从未产出文件（如仅做对话测试），其 public_id 目录可能不存在，
    # 沙箱写入 document_output/{public_id}/ 时会失败，因此此处补齐。
    sub_dirs = ["word_output", "excel_output", "pdf_output", "ppt_output", "csv_output", "image_output"]
    for r in rows:
        pid = r["public_id"]
        if not pid:
            continue
        user_dir = os.path.join(OUTPUT_ROOT, pid)
        if not os.path.isdir(user_dir):
            os.makedirs(user_dir, exist_ok=True)
            for sub in sub_dirs:
                os.makedirs(os.path.join(user_dir, sub), exist_ok=True)
            print(f"[3.1] 已创建产出目录: {user_dir}")

    conn.close()

    # 校验：所有用户都必须有 public_id，否则重启后会锁死
    conn2 = sqlite3.connect(DB_PATH)
    conn2.row_factory = sqlite3.Row
    nulls = conn2.execute("SELECT COUNT(*) AS c FROM users WHERE public_id IS NULL OR public_id = ''").fetchone()["c"]
    conn2.close()
    if nulls > 0:
        print(f"[校验] 仍有 {nulls} 个用户缺少 public_id，禁止重启服务！", file=sys.stderr)
        return 2
    print("[校验] 所有用户均已具备 public_id，可安全重启服务。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
