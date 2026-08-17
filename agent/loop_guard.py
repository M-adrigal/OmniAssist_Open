"""迭代级循环防护：防止 Agent 陷入「调用-失败/诊断-再调用」死循环。

抽成纯函数，便于单测覆盖真实生产逻辑。决策在「每一轮迭代末尾」统一做，
而不是按工具名分别计数 —— 这正是此前 bug 的根因（模型轮着调不同 diag_*
工具时每个名字只出现一次，按名计数永远到不了阈值）。

三道防护：
  1. 任意工具连续失败计数（跨工具，不按名字）：连续失败 N 次即停；
  2. 权限不足硬停：工具返回「权限不足」立即停，绝不重试权限错误；
  3. 空转/重复检测：最近 K 轮只调诊断/检查类工具且未产生新产出文件 → 提前结束。
"""

from __future__ import annotations

# 诊断/检查类工具前缀：这些工具本身不产出用户成果，反复调用即视为空转
DIAGNOSTIC_PREFIXES = ("diag_", "run_command", "read_file", "list_files")
MAX_CONSECUTIVE_FAILURES = 3   # 防护 1：任意工具连续失败达此值即停
STALL_STREAK_LIMIT = 3         # 防护 3：连续 K 轮纯诊断无进展即停


def new_loop_state(seen_files=None) -> dict:
    """创建跨迭代防护状态。seen_files 为迭代开始前已存在的产出文件集合。"""
    return {
        "consecutive_failures": 0,
        "nonproductive_streak": 0,
        "seen_files": set(seen_files or ()),
        "diag_prefixes": DIAGNOSTIC_PREFIXES,
        "max_consecutive_failures": MAX_CONSECUTIVE_FAILURES,
        "stall_streak_limit": STALL_STREAK_LIMIT,
    }


def evaluate(state: dict, list_files_fn, iter_failed: bool,
             iter_perm_denied: bool, iter_tool_names) -> tuple:
    """评估本轮迭代后是否应提前结束。

    参数：
      state            - new_loop_state() 返回的（可变）状态字典
      list_files_fn    - 无参调用，返回当前用户产出文件集合（相对路径，用于进展判定）
      iter_failed      - 本轮是否有任意工具报错
      iter_perm_denied - 本轮是否有工具返回「权限不足」
      iter_tool_names  - 本轮调用的工具名集合（iterable）

    返回 (stop_msg, stop_answer)：
      - 均为 None  → 继续下一轮
      - 非 None    → 应提前结束，stop_msg 推送给前端，stop_answer 存为助手回复
    """
    # 防护 1：任意工具连续失败计数（跨工具，不按名字）
    state["consecutive_failures"] = (state["consecutive_failures"] + 1) if iter_failed else 0

    # 防护 2：权限不足 —— 立即硬停，重试权限错误无意义
    if iter_perm_denied:
        return ("工具返回「权限不足」，已停止重试。请检查账号权限。",
                "工具返回权限不足，已停止执行。")

    # 防护 1（续）：任意工具连续失败达上限
    if state["consecutive_failures"] >= state["max_consecutive_failures"]:
        n = state["consecutive_failures"]
        return (f"工具连续执行失败 {n} 次，已停止重试。请检查相关功能是否正常。",
                f"工具连续执行失败 {n} 次，已停止重试。")

    # 防护 3：空转/重复检测 —— 最近 K 轮只调诊断类工具且无新产出文件
    cur = set(list_files_fn())
    made_progress = len(cur - state["seen_files"]) > 0
    state["seen_files"] = cur
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
