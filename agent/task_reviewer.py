"""
任务复盘系统 — 记录任务执行、提供复盘数据、支持自动优化 Skill

基于 OpenClaw/Hermes 思路：
1. 每次任务执行后记录日志（成功/失败/工具使用/错误）
2. 模型可按需复盘，分析失败原因，优化 Skill 或关键词
3. 支持生成优化建议，自动更新 Skill 和意图关键词
"""

import json
import os
import time

# 存储目录
_REVIEWS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "task_reviews")


def _get_reviews_path(user_id: int) -> str:
    """获取用户任务复盘日志路径"""
    return os.path.join(_REVIEWS_DIR, f"{user_id}.jsonl")


def log_task_execution(user_id: int, task: str, success: bool, **kwargs) -> dict:
    """记录一次任务执行

    Args:
        user_id: 用户 ID
        task: 任务描述
        success: 是否成功
        **kwargs: 额外信息（tools_used, tools_failed, error_message, iterations, duration_ms 等）

    Returns:
        {"logged": True, "entry": {...}}
    """
    os.makedirs(_REVIEWS_DIR, exist_ok=True)

    entry = {
        "timestamp": time.time(),
        "task": task[:500],  # 截断超长任务描述
        "success": success,
        "tools_used": kwargs.get("tools_used", []),
        "tools_failed": kwargs.get("tools_failed", []),
        "error_message": kwargs.get("error_message", ""),
        "iterations": kwargs.get("iterations", 0),
        "duration_ms": kwargs.get("duration_ms", 0),
    }

    path = _get_reviews_path(user_id)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {"logged": True, "entry": entry}


def review_recent_tasks(user_id: int, limit: int = 20) -> dict:
    """读取最近的执行记录

    Args:
        user_id: 用户 ID
        limit: 最多返回条数

    Returns:
        {"tasks": [...], "total": N, "success_rate": 0.85, "failure_patterns": [...]}
    """
    path = _get_reviews_path(user_id)
    if not os.path.isfile(path):
        return {"tasks": [], "total": 0, "success_rate": 1.0, "failure_patterns": []}

    tasks = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    tasks.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    total = len(tasks)
    if total == 0:
        return {"tasks": [], "total": 0, "success_rate": 1.0, "failure_patterns": []}

    recent = tasks[-limit:]
    successes = sum(1 for t in recent if t.get("success"))
    success_rate = successes / len(recent) if recent else 1.0

    # 分析失败模式
    failure_patterns = []
    for t in recent:
        if not t.get("success"):
            failure_patterns.append({
                "task": t.get("task", ""),
                "error": t.get("error_message", ""),
                "failed_tools": t.get("tools_failed", []),
                "timestamp": t.get("timestamp", 0),
            })

    return {
        "tasks": recent,
        "total": total,
        "success_rate": round(success_rate, 3),
        "failure_patterns": failure_patterns,
    }


def clear_reviews(user_id: int) -> dict:
    """清空用户的任务复盘日志

    Args:
        user_id: 用户 ID

    Returns:
        {"success": True, "message": "..."}
    """
    path = _get_reviews_path(user_id)
    if os.path.isfile(path):
        os.remove(path)
    return {"success": True, "message": "复盘日志已清空"}


def analyze_and_suggest(user_id: int) -> dict:
    """分析任务日志，生成优化建议

    复盘失败任务，提取共性错误，生成 Skill 和关键词的优化建议。

    Args:
        user_id: 用户 ID

    Returns:
        {"suggestions": [...], "keyword_suggestions": {...}}
    """
    result = review_recent_tasks(user_id, limit=50)
    failures = result.get("failure_patterns", [])

    if not failures:
        return {
            "suggestions": ["最近没有失败任务，Skill 运行良好"],
            "keyword_suggestions": {},
        }

    suggestions = []
    failed_tools = {}
    error_types = {}

    for f in failures:
        for tool in f.get("failed_tools", []):
            failed_tools[tool] = failed_tools.get(tool, 0) + 1
        error = f.get("error", "")
        if error:
            # 提取错误类型关键词
            if "timeout" in error.lower():
                error_types["timeout"] = error_types.get("timeout", 0) + 1
            elif "not found" in error.lower() or "不存在" in error:
                error_types["not_found"] = error_types.get("not_found", 0) + 1
            elif "permission" in error.lower() or "权限" in error:
                error_types["permission"] = error_types.get("permission", 0) + 1
            else:
                error_types["other"] = error_types.get("other", 0) + 1

    # 工具失败频次
    for tool_name, count in sorted(failed_tools.items(), key=lambda x: -x[1]):
        suggestions.append(f"工具 `{tool_name}` 失败 {count} 次，建议检查工具脚本或更新 SKILL.md 中的使用说明")

    # 错误类型分析
    if error_types.get("timeout", 0) > 2:
        suggestions.append("多个任务超时，建议在 SKILL.md 中增加超时保护策略或拆分大任务")
    if error_types.get("not_found", 0) > 2:
        suggestions.append("多个任务遇到资源未找到错误，建议在 SKILL.md 中增加前置检查步骤")

    # 关键词建议
    from agent.intent_keywords import DEFAULT_KEYWORDS, get_user_keywords
    keyword_suggestions = {}
    user_kw = get_user_keywords(user_id)

    # 如果某个类别的工具频繁失败，建议增加该类别关键词
    for tool_name in failed_tools:
        for cat, names in DEFAULT_KEYWORDS.items():
            if tool_name in str(names) and cat not in user_kw:
                keyword_suggestions[cat] = f"工具 `{tool_name}` 频繁失败，建议为该类别添加更精确的关键词"

    return {
        "suggestions": suggestions if suggestions else ["未发现明显模式，建议逐条检查失败任务"],
        "keyword_suggestions": keyword_suggestions,
        "total_failures": len(failures),
        "error_types": error_types,
    }