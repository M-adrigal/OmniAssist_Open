#!/usr/bin/env python3
"""OmniAssist 质量评估运行器 —— 黄金问答 + 双层判定 + LLM 评委。

用法：
  # 1. 拿登录态：浏览器打开应用 → F12 → Application/存储 → Cookies → 复制 auth_token 的值
  python3 scripts/eval/eval_runner.py --cookie <auth_token值>
  # 只跑某一类 / 某几条
  python3 scripts/eval/eval_runner.py --cookie <token> --only 计算
  python3 scripts/eval/eval_runner.py --cookie <token> --ids calc-01,instr-01
  # 启用 LLM 评委（按 rubric 打 1-5 分，需要本机模型配置可用）
  python3 scripts/eval/eval_runner.py --cookie <token> --judge
  # 自定义服务地址（默认 http://127.0.0.1:17520）
  python3 scripts/eval/eval_runner.py --cookie <token> --base-url http://127.0.0.1:8000
  # 跑「文件生成」类用例前先清掉同名历史产出（否则一定是假阴性，见 _clean_output_files）
  python3 scripts/eval/eval_runner.py --cookie <token> --ids doc-01,doc-02 --clean-output

判定两层：
  L1 硬断言（expect，全部 case 都跑）：contains / contains_all / regex /
     max_chars / tool_called / file_ext / none（none=只交评委）
  L2 LLM 评委（--judge）：把 prompt + rubric + 实际回答交给模型按 1-5 打分。
     评委不走 /api/chat（那条链路带工具与技能，不纯净），而是本地直连 LLMClient。

输出：scripts/eval/reports/<时间戳>.md（人读）+ .json（机器读），控制台打印摘要。
退出码：L1 全过且（未开评委）为 0；开了评委则要求均分 ≥ JUDGE_PASS_LINE。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(EVAL_DIR))
sys.path.insert(0, ROOT)

DEFAULT_BASE = "http://127.0.0.1:17520"
JUDGE_PASS_LINE = 4.0          # 评委均分通过线
JUDGE_SYSTEM = """你是严格的大模型回答质量评审。给定「问题、评分标准、待评回答」，
按 1-5 的整数打分：
5 = 完全正确且严格遵守了全部格式/长度要求
4 = 基本正确，仅有不影响使用的小瑕疵
3 = 部分正确或漏了一半要求
2 = 大部分不正确或严重偏题
1 = 完全错误、答非所问或包含有害内容
只输出一行 JSON，不要任何其他文字：{"score": 4, "reason": "一句话理由"}"""


# ===== HTTP / SSE =====

# 本机若设了 http_proxy，urllib 会把 127.0.0.1 也走代理 → 假 502。
# 评估只打本机，统一禁用代理。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(base: str, cookie: str, path: str, payload: dict, timeout: int = 30):
    """POST 一个 JSON 接口，返回 dict。失败抛异常。"""
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Cookie": f"auth_token={cookie}"},
        method="POST",
    )
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _new_session(base: str, cookie: str, title: str = "") -> str:
    """建一个新会话，返回 session_id（前端发首条消息前也是先建会话）。"""
    data = _http_json(base, cookie, "/api/sessions", {"title": title or "eval"})
    return data.get("id") or (data.get("session") or {}).get("id") or ""


def _http_get(base: str, cookie: str, path: str, timeout: int = 30):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Cookie": f"auth_token={cookie}"},
        method="GET",
    )
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _clean_output_files(public_id: str, exts, dry: bool = False) -> int:
    """把该用户产出目录里指定扩展名的历史文件**挪走**（不删除）。

    为什么挪：后端 `files_created` 是「本轮新增」的基线 diff
    （`chat.py::_collect_new_output_files`），上一轮已存在的同名文件不会
    被算作新增 —— 文件生成类用例重跑必然假阴性。

    为什么挪而不是删：这些目录里有用户的真实产出，评估脚本不该承担销毁责任。
    移动到 `document_output/{uid}/.eval_trash/<时间戳>/`，随时可手动取回。
    仅在 --clean-output 时调用。
    """
    root = os.path.join(ROOT, "document_output", str(public_id))
    if not os.path.isdir(root):
        return 0
    n = 0
    for dirpath, _dirs, files in os.walk(root):
        # 别把回收站自己再挪一遍
        if ".eval_trash" in dirpath.split(os.sep):
            continue
        for fn in files:
            if not any(fn.lower().endswith(e.lower()) for e in exts):
                continue
            n += 1
            if dry:
                continue
            try:
                _rel = os.path.relpath(os.path.join(dirpath, fn), root)
                _dst = os.path.join(root, ".eval_trash",
                                    datetime.now().strftime("%Y%m%d-%H%M%S"), _rel)
                os.makedirs(os.path.dirname(_dst), exist_ok=True)
                os.replace(os.path.join(dirpath, fn), _dst)
            except OSError:
                pass
    return n


def _sse_collect(base: str, cookie: str, message: str, session_id: str = None,
                 timeout: int = 180) -> dict:
    """POST /api/chat/stream，解析 SSE，返回 {answer, tools, files, error}。

    服务端事件名（见 server/routes/chat.py）：
      token         → 正文增量（不是 content）
      tool_call     → 单次工具调用 {name, arguments}
      tool_summary  → 本轮工具汇总 {tools: [...]}
      files_created → 产出文件
      error / done  → 结束
    """
    url = base.rstrip("/") + "/api/chat/stream"
    body = {"message": message, "web_search": "off", "show_thought": False}
    if session_id:
        body["session_id"] = session_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Cookie": f"auth_token={cookie}"},
        method="POST",
    )
    out = {"answer": "", "tools": [], "files": [], "error": "", "session_id": session_id}
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                raw, buf = buf.split(b"\n\n", 1)
                for line in raw.decode("utf-8", "replace").splitlines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    et = ev.get("type")
                    if et == "token":
                        out["answer"] += ev.get("content") or ""
                    elif et == "tool_call":
                        if ev.get("name"):
                            out["tools"].append(ev["name"])
                    elif et == "tool_summary":
                        for t in ev.get("tools") or []:
                            _n = t.get("name") if isinstance(t, dict) else t
                            if _n and _n not in out["tools"]:
                                out["tools"].append(_n)
                    elif et == "files_created":
                        # chat.py::_collect_new_output_files 的载荷字段是 name/path/size/ext
                        for f in ev.get("files") or []:
                            out["files"].append(f.get("name") or f.get("filename") or "")
                    elif et == "error":
                        out["error"] += ev.get("content") or ""
                    elif et == "done":
                        pass
    out["session_id"] = out["session_id"] or session_id
    return out


def run_case(base: str, cookie: str, case: dict, timeout: int = 180) -> dict:
    """跑一条 case（含多轮），返回结果字典。

    每条 case 独占一个会话：先 POST /api/sessions 拿到 id，后续每轮带上，
    多轮用例才能共享上下文（服务端不会在 SSE 里回传新建会话的 id）。
    """
    turns = case.get("turns") or [case["prompt"]]
    session_id = None
    last = {"answer": "", "tools": [], "files": [], "error": ""}
    try:
        session_id = _new_session(base, cookie, (case.get("prompt") or (turns[0] if turns else ""))[:30])
    except Exception as e:
        print(f"  ! [{case['id']}] 建会话失败：{e}（将不带 session_id 直接发）")
    for i, msg in enumerate(turns):
        try:
            last = _sse_collect(base, cookie, msg, session_id=session_id, timeout=timeout)
        except Exception as e:
            last = {"answer": "", "tools": [], "files": [], "error": f"请求失败: {e}"}
        session_id = session_id or last.get("session_id")
        if last["error"] and i < len(turns) - 1:
            break
    return {
        "id": case["id"],
        "category": case.get("category", ""),
        "answer": last.get("answer", ""),
        "tools": last.get("tools", []),
        "files": last.get("files", []),
        "error": last.get("error", ""),
        "session_id": session_id or "",
    }


# ===== L1 硬断言 =====

def check_expect(case: dict, result: dict):
    """返回 (passed, detail)。无 expect 时返回 (None, '')（交评委）。"""
    exp = case.get("expect")
    if not exp:
        return None, ""
    etype = exp.get("type")
    ans = result["answer"] or ""
    if etype == "contains":
        ok = exp["value"] in ans
        return ok, f"应包含「{exp['value']}」"
    if etype == "contains_all":
        missing = [v for v in exp["values"] if v not in ans]
        return (not missing), (f"缺少: {missing}" if missing else "")
    if etype == "regex":
        ok = bool(re.search(exp["pattern"], ans, re.M))
        return ok, f"应匹配 /{exp['pattern']}/"
    if etype == "max_chars":
        n = len(ans)
        return n <= exp["value"], f"实际 {n} 字（上限 {exp['value']}）"
    if etype == "tool_called":
        ok = exp["value"] in result["tools"]
        return ok, f"应调用工具 {exp['value']}，实际 {result['tools']}"
    if etype == "file_ext":
        ok = any(f.lower().endswith(exp["value"].lower()) for f in result["files"])
        return ok, f"应生成 {exp['value']} 文件，实际 {result['files']}"
    if etype == "none":
        return None, ""
    return None, f"未知 expect 类型 {etype}"


# ===== L2 LLM 评委 =====

def judge_batch(cases: list, results: list) -> dict:
    """本地直连 LLMClient 当评委。返回 {case_id: {"score", "reason"}}。"""
    from server.database import get_model_config
    from agent.config import AgentConfig
    from agent.llm import LLMClient

    cfg_row = get_model_config(None)
    if cfg_row and cfg_row.get("api_key"):
        llm = LLMClient(
            api_key=cfg_row["api_key"],
            base_url=cfg_row.get("base_url", ""),
            model=cfg_row.get("model_name", ""),
            config=AgentConfig(os.path.join(ROOT, "config.yaml")),
        )
    else:
        llm = LLMClient(config=AgentConfig(os.path.join(ROOT, "config.yaml")))

    out = {}
    for case, result in zip(cases, results):
        prompt = (
            f"【问题】\n{case.get('prompt') or ' / '.join(case.get('turns') or [])}\n\n"
            f"【评分标准】\n{case.get('rubric', '')}\n\n"
            f"【待评回答】\n{(result['answer'] or '(空)')[:2000]}\n\n"
            f"【该轮调用的工具】{result['tools']}\n【该轮生成的文件】{result['files']}\n\n"
            "请打分，只输出一行 JSON。"
        )
        try:
            resp = llm.chat(
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                source="eval",
            )
            text = (resp.get("content") or "").strip()
            m = re.search(r'\{[^{}]*"score"\s*:\s*(\d)[^{}]*\}', text)
            if m:
                score = int(m.group(1))
                reason = ""
                rm = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
                if rm:
                    reason = rm.group(1)
                out[case["id"]] = {"score": score, "reason": reason}
            else:
                out[case["id"]] = {"score": 0, "reason": f"评委输出无法解析: {text[:80]}"}
        except Exception as e:
            out[case["id"]] = {"score": 0, "reason": f"评委调用失败: {e}"}
        time.sleep(0.5)
    return out


# ===== 报告 =====

def write_report(rows: list, judge_scores: dict, args, path_md: str, path_json: str):
    total = len(rows)
    l1_pass = sum(1 for r in rows if r["l1"] is True)
    l1_fail = sum(1 for r in rows if r["l1"] is False)
    l1_na = total - l1_pass - l1_fail
    scores = [judge_scores[r["id"]]["score"] for r in rows
              if r["id"] in judge_scores and judge_scores[r["id"]]["score"] > 0]
    avg = round(sum(scores) / len(scores), 2) if scores else None

    lines = [
        "# OmniAssist 质量评估报告",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 服务：{args.base_url}",
        f"- 用例：{total} 条（L1 通过 {l1_pass} / 失败 {l1_fail} / 交评委 {l1_na}）",
    ]
    if avg is not None:
        lines.append(f"- 评委均分：**{avg}** / 5（通过线 {JUDGE_PASS_LINE}，样本 {len(scores)}）")
    lines += ["", "| ID | 分类 | L1 | 评委 | 说明 |", "|---|---|---|---|---|"]
    for r in rows:
        l1 = {True: "✅", False: "❌", None: "—"}[r["l1"]]
        js = judge_scores.get(r["id"], {})
        jsd = f"{js.get('score')} {js.get('reason', '')[:30]}" if js else "—"
        note = r["detail"] or r["error"][:40] or ""
        lines.append(f"| {r['id']} | {r['category']} | {l1} | {jsd} | {note} |")
    lines += ["", "## 失败明细", ""]
    fails = [r for r in rows if r["l1"] is False or (r["id"] in judge_scores and judge_scores[r["id"]]["score"] < 4)]
    if not fails:
        lines.append("无。")
    for r in fails:
        js = judge_scores.get(r["id"], {})
        lines += [
            f"### {r['id']}（{r['category']}）",
            f"- 断言：{r['detail'] or '通过/未设'}",
            f"- 评委：{js.get('score', '—')} 分 —— {js.get('reason', '')}",
            f"- 实际回答：{(r['answer'] or '(空)')[:300]}",
            f"- 工具：{r['tools']}　文件：{r['files']}",
            "",
        ]
    with open(path_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(path_json, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "judge": judge_scores,
                   "avg": avg, "l1_pass": l1_pass, "l1_fail": l1_fail},
                  f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="OmniAssist 质量评估运行器")
    ap.add_argument("--cookie", required=True, help="浏览器里的 auth_token cookie 值")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--only", default=None, help="只跑某个分类（如 计算）")
    ap.add_argument("--ids", default=None, help="只跑指定 id，逗号分隔")
    ap.add_argument("--judge", action="store_true", help="启用 LLM 评委")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--clean-output", action="store_true",
                    help="跑文件生成类用例前，先删掉该用户产出目录里同扩展名的历史文件"
                         "（后端 files_created 是本轮新增 diff，不清理会假阴性）")
    args = ap.parse_args()

    with open(os.path.join(EVAL_DIR, "cases.json"), encoding="utf-8") as f:
        all_cases = json.load(f)["cases"]
    cases = all_cases
    if args.only:
        cases = [c for c in cases if c.get("category") == args.only]
    if args.ids:
        want = {i.strip() for i in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in want]
    if not cases:
        print("没有匹配的用例")
        return 2

    print(f"共 {len(cases)} 条用例，服务 {args.base_url}")
    rows = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']}（{case.get('category')}）...", flush=True)
        t0 = time.time()
        if args.clean_output and (case.get("expect") or {}).get("type") == "file_ext":
            try:
                _me = _http_get(args.base_url, args.cookie, "/api/auth/me")
                _rm = _clean_output_files(_me.get("public_id"), [case["expect"]["value"]])
                if _rm:
                    print(f"    清理历史产出 {_rm} 个 {case['expect']['value']} 文件")
            except Exception as e:
                print(f"    清理历史产出失败（忽略）: {e}")
        try:
            result = run_case(args.base_url, args.cookie, case, timeout=args.timeout)
        except Exception as e:
            result = {"id": case["id"], "category": case.get("category", ""),
                      "answer": "", "tools": [], "files": [], "error": f"请求失败: {e}"}
        l1, detail = check_expect(case, result)
        rows.append({
            "id": case["id"], "category": case.get("category", ""),
            "l1": l1, "detail": detail,
            "answer": result["answer"], "tools": result["tools"],
            "files": result["files"], "error": result["error"],
            "elapsed_s": round(time.time() - t0, 1),
        })
        mark = {True: "✅", False: "❌", None: "—"}[l1]
        print(f"    {mark} {detail or ''}（{rows[-1]['elapsed_s']}s）")

    judge_scores = {}
    if args.judge:
        print("\n评委打分中...")
        judged_cases = [c for c in cases if c.get("rubric")]
        judged_results = [r for c, r in zip(cases, rows) if c.get("rubric")]
        judge_scores = judge_batch(judged_cases, judged_results)

    os.makedirs(os.path.join(EVAL_DIR, "reports"), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md = os.path.join(EVAL_DIR, "reports", f"{stamp}.md")
    js = os.path.join(EVAL_DIR, "reports", f"{stamp}.json")
    write_report(rows, judge_scores, args, md, js)

    l1_fail = sum(1 for r in rows if r["l1"] is False)
    print("\n" + "=" * 56)
    print(f"L1：{sum(1 for r in rows if r['l1'] is True)} 通过 / {l1_fail} 失败 / "
          f"{sum(1 for r in rows if r['l1'] is None)} 交评委")
    if judge_scores:
        scores = [v["score"] for v in judge_scores.values() if v["score"] > 0]
        avg = round(sum(scores) / len(scores), 2) if scores else 0
        print(f"评委均分：{avg} / 5（通过线 {JUDGE_PASS_LINE}）")
    print(f"报告：{md}")
    print("=" * 56)

    if judge_scores:
        scores = [v["score"] for v in judge_scores.values() if v["score"] > 0]
        avg = (sum(scores) / len(scores)) if scores else 0
        return 0 if (l1_fail == 0 and avg >= JUDGE_PASS_LINE) else 1
    return 0 if l1_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
