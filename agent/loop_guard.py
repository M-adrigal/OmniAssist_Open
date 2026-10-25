"""迭代级循环防护：防止 Agent 陷入「调用-失败/诊断-再调用」死循环。

抽成纯函数，便于单测覆盖真实生产逻辑。决策在「每一轮迭代末尾」统一做，
而不是按工具名分别计数 —— 这正是此前 bug 的根因（模型轮着调不同 diag_*
工具时每个名字只出现一次，按名计数永远到不了阈值）。

四道防护：
  1. 同一调用连续失败（工具名+参数签名相同）达 N 次即停 —— 真死循环；
     附带总失败宽限（默认 8 次，防「轮着换工具失败」），一轮有成功即清零；
  2. 权限不足硬停：工具返回「权限不足」立即停，绝不重试权限错误；
  3. 空转/重复检测：最近 K 轮的**工具调用签名完全相同**才判为空转；
  4. （兼容）调用方不提供失败签名时，退回「任意工具连续失败 N 次」旧判据。

⚠️ 防护 1 判据变更（2026-10-24，消除误杀）：
   原判据「任意工具连续失败 ≥3 次（跨工具、跨轮）」会误杀**合法场景**：
   · 批量处理 10 个文件，其中 3 个格式不支持 —— 每轮各失败一次即被停；
   · 模型读错误→改参数→再试的 ReAct 修正，3 次修正失败被当死循环；
   · 并行工具里个别工具失败（其余成功）也计入连续。
   现改为：**同一调用（签名相同）反复失败**才是死循环；不同工具/不同参数
   的失败只计入总失败宽限（默认 8 次），任意一轮有成功即清零。
   宁可漏判（多烧几轮 token），不可误杀（用户任务被腰斩）。

⚠️ 防护 3 的判据变更（2026-10-23）：
   原判据是「连续 K 轮只调 diag_* 类工具 且 未产生新产出文件」。
   这会误杀「查日志」这类**有进展的探索**：查日志本就不产出文件，
   但每次参数/返回内容都不同（lines=200→500→2000、换日志名、换日期），
   属于在推进排查，不是空转。
   现改为：**连续 K 轮的调用签名（工具名 + 参数）完全相同**才算重复。
   参数或工具名任一变化 → 视为有进展，不累加空转计数。

   ⚠️ 注意：run_command / read_file / list_files 是**正常干活的通用工具**，
   严禁列入此处——否则模型连续几轮调用它们（如批量跑脚本、读多个文件）且未生
   成产出文件时，会被误判为空转死循环而提前终止（实际并未陷入循环）。
   真正的死循环由「防护 1 同一调用反复失败」与「纯 diag_* 轮次无进展」两道分别兜底。
"""

from __future__ import annotations

import hashlib
import json

# 诊断/检查类工具前缀：仅管理员诊断工具 diag_* 算「诊断/检查类」。
# ⚠️ 注意：run_command / read_file / list_files 是**正常干活的通用工具**，
#    严禁列入此处——否则模型连续几轮调用它们（如批量跑脚本、读多个文件）且未生
#    成产出文件时，会被误判为空转死循环而提前终止（实际并未陷入循环）。
#    真正的死循环由「防护 1 连续失败」与「纯 diag_* 轮次无进展」两道分别兜底。
DIAGNOSTIC_PREFIXES = ("diag_",)
MAX_CONSECUTIVE_FAILURES = 3   # 兼容判据：无失败签名时，任意工具连续失败达此值即停
MAX_SAME_FAILURES = 3          # 防护 1：同一调用（签名相同）连续失败达此值即停（真死循环）
MAX_TOTAL_FAILURES = 8         # 防护 1 宽限：失败总次数上限（防轮着换工具失败），一轮有成功即清零
STALL_STREAK_LIMIT = 3         # 防护 3：连续 K 轮调用签名完全相同即停


def new_loop_state(seen_files=None) -> dict:
    """创建跨迭代防护状态。seen_files 为迭代开始前已存在的产出文件集合。"""
    return {
        "consecutive_failures": 0,
        "consecutive_same_failures": 0,
        "total_failures": 0,
        "last_fail_signatures": set(),
        "nonproductive_streak": 0,
        "seen_files": set(seen_files or ()),
        "diag_prefixes": DIAGNOSTIC_PREFIXES,
        "max_consecutive_failures": MAX_CONSECUTIVE_FAILURES,
        "max_same_failures": MAX_SAME_FAILURES,
        "max_total_failures": MAX_TOTAL_FAILURES,
        "stall_streak_limit": STALL_STREAK_LIMIT,
        # 最近几轮的调用签名（工具名 + 参数的哈希），用于判断"是否在重复同一件事"
        "recent_signatures": [],
    }


def _call_signature(tool_name: str, args) -> str:
    """一轮调用的签名：工具名 + 参数的稳定哈希。

    参数里若含时间戳/随机 id 之类每次都变的字段，会导致签名永远不同，
    退化为"永不判重复"——这是安全侧失败（宁可漏判，不可误杀）。
    """
    try:
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = str(args)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{h}"


def evaluate(state: dict, list_files_fn, iter_failed: bool,
             iter_perm_denied: bool, iter_tool_names,
             iter_signatures=None, iter_fail_signatures=None) -> tuple:
    """评估本轮迭代后是否应提前结束。

    参数：
      state            - new_loop_state() 返回的（可变）状态字典
      list_files_fn    - 无参调用，返回当前用户产出文件集合（相对路径，用于进展判定）
      iter_failed      - 本轮是否有任意工具报错
      iter_perm_denied - 本轮是否有工具返回「权限不足」
      iter_tool_names  - 本轮调用的工具名集合（iterable）
      iter_signatures  - 本轮每次工具调用的签名列表（工具名+参数哈希）。
                        传入时用「签名完全相同」判重复；不传则退回旧的
                        「纯 diag_* 且无新文件」判据（兼容性兜底）。
      iter_fail_signatures - 本轮**失败**的调用签名列表。传入时启用防护 1 新判据
                        （同一调用反复失败才算死循环，见模块 docstring）；
                        不传则退回旧判据（任意工具连续失败 N 次）。

    返回 (stop_msg, stop_answer)：
      - 均为 None  → 继续下一轮
      - 非 None    → 应提前结束，stop_msg 推送给前端，stop_answer 存为助手回复
    """
    # 防护 2：权限不足 —— 立即硬停，重试权限错误无意义
    if iter_perm_denied:
        return ("工具返回「权限不足」，已停止重试。请检查账号权限。",
                "工具返回权限不足，已停止执行。")

    # ---- 防护 1：失败判定（新判据：同一调用反复失败才算死循环）----
    if iter_failed:
        _fail_sigs = {str(s) for s in (iter_fail_signatures or []) if s}
        if _fail_sigs:
            state["total_failures"] += 1
            if _fail_sigs == state.get("last_fail_signatures"):
                state["consecutive_same_failures"] += 1
            else:
                state["last_fail_signatures"] = _fail_sigs
                state["consecutive_same_failures"] = 1
            if state["consecutive_same_failures"] >= state["max_same_failures"]:
                n = state["consecutive_same_failures"]
                return (f"同一操作连续失败 {n} 次（参数相同），已停止重试。"
                        f"请调整请求或检查相关功能是否正常。",
                        f"同一操作连续失败 {n} 次，已停止重试。")
            if state["total_failures"] >= state["max_total_failures"]:
                n = state["total_failures"]
                return (f"工具失败次数已达 {n} 次，已停止重试。"
                        f"请检查相关功能是否正常。",
                        f"工具失败次数过多（{n} 次），已停止重试。")
        else:
            # 兼容判据（调用方未提供失败签名）：任意工具连续失败
            state["consecutive_failures"] += 1
            if state["consecutive_failures"] >= state["max_consecutive_failures"]:
                n = state["consecutive_failures"]
                return (f"工具连续执行失败 {n} 次，已停止重试。请检查相关功能是否正常。",
                        f"工具连续执行失败 {n} 次，已停止重试。")
    else:
        # 本轮无失败：失败类计数全部清零（有进展就不按失败停）
        state["consecutive_failures"] = 0
        state["consecutive_same_failures"] = 0
        state["total_failures"] = 0
        state["last_fail_signatures"] = set()

    # 防护 3：重复检测
    cur = set(list_files_fn())
    made_progress = len(cur - state["seen_files"]) > 0
    state["seen_files"] = cur

    sigs = list(iter_signatures or [])
    if sigs:
        # 新判据：连续 K 轮调用签名完全相同 → 才是真重复
        recent = state.get("recent_signatures") or []
        recent.append(tuple(sorted(sigs)))
        # 只保留最近 limit 轮
        if len(recent) > state["stall_streak_limit"]:
            recent = recent[-state["stall_streak_limit"]:]
        state["recent_signatures"] = recent

        limit = state["stall_streak_limit"]
        if len(recent) >= limit and all(s == recent[0] for s in recent):
            return ("检测到模型在重复调用同一组工具且参数完全相同，已提前结束以避免陷入循环。"
                    "请调整请求或检查相关功能。",
                    "模型重复调用相同工具（参数完全一致），已提前结束。")
        # 签名有变化 → 有进展，计数清零
        state["nonproductive_streak"] = 0
        return (None, None)

    # 旧判据（未提供签名时的兼容兜底）：连续 K 轮纯诊断且无新产出文件
    all_diag = bool(iter_tool_names) and all(
        any(name.startswith(p) for p in state["diag_prefixes"])
        for name in iter_tool_names
    )
    if all_diag and not made_progress:
        state["nonproductive_streak"] += 1
    else:
        state["nonproductive_streak"] = 0
    if state["nonproductive_streak"] >= state["stall_streak_limit"]:
        return ("检测到模型在反复调用诊断/检查类工具且未产生有效进展，"
                "已提前结束以避免陷入循环。请调整请求或检查相关功能。",
                "模型陷入工具调用循环（仅重复诊断类操作且无新产出），已提前结束。")

    return (None, None)
