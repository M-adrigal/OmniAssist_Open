#!/usr/bin/env python3
"""Agent 效果评估：自动跑批采集。

做什么：
    按 eval_samples.py 的题目逐条发给 Agent，把「调了哪些工具、传了什么参数、
    工具返回什么、用了几步、耗时多少」记录下来，并按规则自动判定过程类指标。
    **「任务达成」与「幻觉」两列不自动判** —— 这两项要人看语义，机器判会误导。

为什么不用手工：75 题 × 2 轮 = 150 次对话，手工发一遍两小时；而且人会疲劳，
后 30 题判得比前 30 题松，指标就没法比了。

用法：
    # 先起服务（本地或服务器），确认端口
    /usr/bin/python3 scripts/eval_agent.py --rounds 2 --concurrency 3

    # 只跑指定的几条（调试用）
    /usr/bin/python3 scripts/eval_agent.py --only S001,S062 --rounds 1

    # 用服务器地址跑
    /usr/bin/python3 scripts/eval_agent.py --base http://1.2.3.4:17520

    # 跑完清理测试会话（会在会话列表里留一堆 [Eval] 标题）
    /usr/bin/python3 scripts/eval_agent.py --clean

输出：
    data/eval_results_<时间戳>.json    完整记录（含每题完整回答，便于复核）
    控制台打印进度与汇总

成本提示：每次对话都会真实消耗 token。75 题 × 2 轮 ≈ 150 次调用。
"""
import argparse
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from eval_samples import SAMPLES  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:17520"

# 纯查询、无副作用的工具：任何任务里多调一下都不算「滥用」。
# 尤其开了联网搜索后，模型会先取当天日期再搜（搜索要判断"今天"是哪天）——
# 这是合理行为，不该记成多余调用。只放无副作用的，run_command/fetch_url 不放。
HARMLESS_TOOLS = {"get_datetime", "calculate"}

# 工具返回里出现这些字样 = 该次调用失败（用于自动判定「执行成功」）
FAIL_MARKS = ("[抓取失败]", "[搜索失败]", "[沙箱执行失败]", "[沙箱执行超时]",
              "[沙箱异常]", "[工具执行异常]", "[工具执行失败]")
# 守卫按预期拒绝 ≠ 执行失败：SSRF 守卫拦下内网/假域名是「正确行为」——
# 工具本身跑通并返回了受控的拒绝说明。不算进失败，否则会把「安全边界
# 工作正常」误判成执行失败（2026-10-26 实测把 7 次守卫拒绝记成失败，
# 执行成功率被从真实水平拉低 5 个百分点）。
GUARD_MARKS = ("只允许访问公网地址",)


def make_token():
    """自签一个 admin token。

    服务端的白名单是「软」的（签名对 + 未过期即接受），所以本地跑批不需要
    知道管理员密码。签名密钥就是服务自己用的 data/.token_secret。
    """
    secret_file = ROOT / "data" / ".token_secret"
    if not secret_file.exists():
        sys.exit(f"找不到签名密钥 {secret_file} —— 先启动一次服务生成它")
    secret = secret_file.read_text().strip()

    conn = sqlite3.connect(str(ROOT / "data" / "users.db"))
    row = conn.execute(
        "SELECT public_id, username, user_type FROM users WHERE user_type='admin' LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        sys.exit("数据库里没有管理员账号")
    pid, uname, utype = row

    payload = {"uid": pid, "un": uname, "ut": utype,
               "iat": int(time.time()), "exp": int(time.time()) + 6 * 3600}
    pj = json.dumps(payload, separators=(",", ":"))
    p64 = base64.urlsafe_b64encode(pj.encode()).decode().rstrip("=")
    sig = hmac.new(secret.encode(), p64.encode(), hashlib.sha256).hexdigest()
    return f"{p64}.{sig}"


class Client:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _post(self, path, body, timeout=180):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers=self.h, method="POST")
        return urllib.request.urlopen(req, timeout=timeout)

    def create_session(self, title):
        with self._post("/api/sessions", {"title": title}) as r:
            return json.loads(r.read())["id"]

    def delete_session(self, sid):
        req = urllib.request.Request(f"{self.base}/api/sessions/{sid}",
                                     headers=self.h, method="DELETE")
        with urllib.request.urlopen(req, timeout=20):
            pass

    def stream_once(self, sid, message, web_search="auto"):
        """跑一轮对话，返回 (工具调用列表, 工具结果列表, 回答文本, 耗时, 事件统计)。"""
        tools, results, answer, events = [], [], [], {}
        t0 = time.time()
        body = {"message": message, "session_id": sid, "web_search": web_search}
        with self._post("/api/chat/stream", body) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except Exception:
                    continue
                et = ev.get("type")
                events[et] = events.get(et, 0) + 1
                if et == "tool_call":
                    tools.append({"name": ev.get("name"), "args": ev.get("arguments")})
                elif et == "tool_result":
                    results.append({"name": ev.get("name"), "content": str(ev.get("content") or "")})
                elif et in ("content", "token"):
                    answer.append(ev.get("content") or "")
                elif et == "done":
                    break
        return tools, results, "".join(answer), time.time() - t0, events


def judge(sample, tools, results, answer):
    """自动判定过程类指标。返回值用 "" 表示「机器判不了，留给人」。

    原则：宁可留空，不要瞎判 —— 机器判错比不判更糟，因为人会以为已经判过了。
    """
    actual = []
    for t in tools:
        if t["name"] and t["name"] not in actual:
            actual.append(t["name"])

    must = sample["must"]
    allow = sample["allow"]

    # 工具调全：must 为空的题（本就不该调）天然算"没漏"
    tool_complete = "是" if all(m in actual for m in must) else "否"

    # 有多余调用：must/allow 之外的工具出现了就算
    extra = [t for t in actual
             if t not in must and t not in allow and t not in HARMLESS_TOOLS]
    has_extra = "是" if extra else "否"

    # 执行成功：有工具调用且没有任何失败标记（守卫按预期拒绝不算，见 GUARD_MARKS）
    if not results:
        exec_ok = ""
    else:
        failed = [r for r in results
                  if any(m in r["content"] for m in FAIL_MARKS)
                  and not any(g in r["content"] for g in GUARD_MARKS)]
        exec_ok = "否" if failed else "是"

    # 参数正确：只有「参数陷阱」类必须人看，其余按执行成功初判
    if sample["cat"].startswith("参数陷阱"):
        param_ok = ""          # 交给人工，因为参数错也可能"算成功"
    elif not tools:
        param_ok = ""
    else:
        param_ok = exec_ok if exec_ok else ""

    # 任务达成：只有写了 expect（确定无疑的关键内容）才敢自动判
    if sample["expect"]:
        hit = all(k in answer for k in sample["expect"])
        task_ok = "是" if hit else "否"
    else:
        task_ok = ""           # 人工判

    return {
        "actual_tools": actual,
        "extra_tools": extra,
        "tool_complete": tool_complete,
        "has_extra": has_extra,
        "param_ok": param_ok,
        "exec_ok": exec_ok,
        "task_ok": task_ok,
        "hallucination": "",   # 永远人工
        "iterations": len(tools),
    }


def run_one(client, sample, rnd, verbose, web_search="auto"):
    sid = None
    try:
        sid = client.create_session(f"[Eval] {sample['id']} #{rnd}")
        all_tools, all_results, answers, elapsed = [], [], [], 0.0
        for msg in sample["turns"]:
            tools, results, answer, sec, _ = client.stream_once(sid, msg, web_search)
            all_tools += tools
            all_results += results
            answers.append(answer)
            elapsed += sec
        answer_text = "\n---\n".join(answers)
        rec = {
            "sample": sample["id"],
            "round": rnd,
            "cat": sample["cat"],
            "session_id": sid,
            "turns": sample["turns"],
            "tools": all_tools,
            "results": all_results,
            "answer": answer_text,
            "elapsed": round(elapsed, 2),
            "judge": judge(sample, all_tools, all_results, answer_text),
            "expect": sample["expect"],
            "judge_hint": sample["judge"],
        }
        if verbose:
            print(f"  ✓ {sample['id']}#{rnd} {elapsed:.1f}s 工具={rec['judge']['actual_tools']}")
        return rec
    except Exception as e:
        if verbose:
            print(f"  ✗ {sample['id']}#{rnd} {type(e).__name__}: {e}")
        return {"sample": sample["id"], "round": rnd, "cat": sample["cat"],
                "session_id": sid, "error": f"{type(e).__name__}: {e}", "judge": {}}


def clean_sessions(client):
    """删掉跑批产生的会话。

    **不能靠标题识别**：服务会在首条消息之后自动把会话标题改成内容摘要，
    创建时写的 "[Eval]" 前缀会被覆盖掉（踩过）。所以从结果文件的 session_id 删 ——
    那是跑批时真实记下来的，不受标题改写影响。
    """
    ids = set()
    for f in sorted((ROOT / "data").glob("eval_results_*.json")):
        try:
            for r in json.loads(f.read_text(encoding="utf-8")):
                if r.get("session_id"):
                    ids.add(r["session_id"])
        except Exception as e:
            print(f"  跳过 {f.name}: {e}")
    if not ids:
        print("没有可清理的记录（结果文件里没有 session_id）")
        return

    ok = fail = 0
    for sid in ids:
        try:
            client.delete_session(sid)
            ok += 1
        except Exception:
            fail += 1
    print(f"已清理 {ok} 个测试会话" + (f"，失败 {fail} 个" if fail else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--only", default="", help="只跑指定编号，逗号分隔，如 S001,S062")
    ap.add_argument("--clean", action="store_true", help="只清理 [Eval] 会话，不跑题")
    # 默认 auto：不传这个参数时接口按「关」处理，所有需要搜索的题都会
    # 变成「搜索已关闭，我查不到」—— 测的就不是搜索能力了。这是踩过的坑。
    ap.add_argument("--web-search", default="auto", choices=["off", "auto"],
                    help="联网开关，默认 auto（不传会被当成 off）")
    args = ap.parse_args()

    client = Client(args.base, make_token())

    if args.clean:
        clean_sessions(client)
        return 0

    picked = SAMPLES
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        picked = [s for s in SAMPLES if s["id"].upper() in want]
        if not picked:
            sys.exit(f"没有匹配的样本：{args.only}")

    jobs = [(s, r) for s in picked for r in range(1, args.rounds + 1)]
    print(f"待跑 {len(picked)} 题 × {args.rounds} 轮 = {len(jobs)} 次调用")
    print(f"目标 {args.base}  并发 {args.concurrency}\n")

    out_dir = ROOT / "data"
    if not out_dir.exists():          # 某些沙箱环境不支持 mkdir(exist_ok=True)
        out_dir.mkdir(parents=True)
    out_file = out_dir / f"eval_results_{time.strftime('%Y%m%d_%H%M%S')}.json"

    records, t0 = [], time.time()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(run_one, client, s, r, True, args.web_search): (s, r) for s, r in jobs}
            for i, fut in enumerate(as_completed(futures), 1):
                records.append(fut.result())
                if i % 10 == 0 or i == len(jobs):
                    ok = sum(1 for x in records if "error" not in x)
                    print(f"  进度 {i}/{len(jobs)}  成功 {ok}  已用 {time.time()-t0:.0f}s")
    finally:
        try:
            out_file.write_text(json.dumps(records, ensure_ascii=False, indent=1))
            print(f"\n结果已保存：{out_file}")
        except Exception as e:
            print(f"保存失败: {e}")

    # 汇总
    ok = [r for r in records if "error" not in r]
    failed = [r for r in records if "error" in r]
    if failed:
        print(f"失败 {len(failed)} 次：" + ", ".join(f"{r['sample']}#{r['round']}" for r in failed[:10]))
    if ok:
        def rate(key):
            vals = [r["judge"].get(key) for r in ok]
            yes, no = vals.count("是"), vals.count("否")
            judged = yes + no
            return f"{yes}/{judged} ({yes/judged:.1%})" if judged else "—（全部待人工）"

        print("\n自动判定结果（仅含机器能判的部分）：")
        print("  工具调全   ", rate("tool_complete"))
        print("  有多余调用 ", rate("has_extra"))
        print("  执行成功   ", rate("exec_ok"))
        print("  参数正确   ", rate("param_ok"), "（参数陷阱类已留给人工）")
        print("  任务达成   ", rate("task_ok"), "（未写 expect 的题已留给人工）")
        avg_it = sum(r["judge"].get("iterations", 0) for r in ok) / len(ok)
        print(f"  平均工具调用 {avg_it:.2f} 次")
        print(f"  总耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
