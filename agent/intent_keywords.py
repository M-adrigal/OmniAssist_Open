"""
用户级意图关键词管理 — 支持按用户存储和自优化

关键词用于 _select_tools() 中按需注入工具，减少 LLM 每次请求的 token 开销。
每个用户有独立的关键词配置，可与系统默认关键词合并使用。

存储格式（data/intent_keywords/{user_id}.json）：
{
  "weather": ["天气", "温度", "下雨"],
  "document": ["Excel", "Word", "生成报告"],
  ...
}
"""

import json
import os
import re

# 系统默认关键词，所有用户共享兜底
DEFAULT_KEYWORDS = {
    "weather": [r"天气", r"温度", r"下雨", r"刮风", r"空气质量", r"湿度", r"风力", r"降水"],
    "document": [r"生成.*文档", r"生成.*报告", r"Excel", r"Word", r"PDF", r"PPT", r"表格", r"导出"],
    "web": [r"网页", r"抓取", r"http", r"链接", r"金价", r"黄金", r"URL", r"网站"],
    "lunar": [r"农历", r"阴历", r"生肖", r"天干地支", r"八字"],
    "agent": [r"分析.*数据", r"委派", r"统计.*分析", r"多.*任务"],
}

# 工具分类：按功能领域分组
TOOL_CATEGORIES = {
    "basic": ["calculate", "count_chinese", "get_datetime"],
    "weather": ["geo_lookup", "current_weather", "forecast"],
    "document": ["save_excel", "save_pdf", "save_word", "save_ppt"],
    "web": ["fetch_url", "gold_price"],
    "lunar": ["convert_lunar"],
    "agent": ["delegate_analyst", "delegate_document", "delegate_researcher", "delegate_parallel"],
}

# 存储目录
_KEYWORDS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "intent_keywords")


def _get_keywords_path(user_id: int) -> str:
    """获取用户关键词文件路径"""
    return os.path.join(_KEYWORDS_DIR, f"{user_id}.json")


def get_user_keywords(user_id: int) -> dict:
    """获取用户级关键词，合并系统默认

    Args:
        user_id: 用户 ID

    Returns:
        dict: {category: [pattern, ...]}
    """
    user_kw = _load_user_keywords(user_id)
    merged = {}

    # 以系统默认为基础
    for cat, patterns in DEFAULT_KEYWORDS.items():
        merged[cat] = list(patterns)

    # 用户自定义覆盖/追加
    for cat, patterns in user_kw.items():
        if cat in merged:
            # 用户模式追加到系统模式后面
            merged[cat].extend(patterns)
        else:
            merged[cat] = list(patterns)

    return merged


def _load_user_keywords(user_id: int) -> dict:
    """从文件加载用户关键词"""
    path = _get_keywords_path(user_id)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def update_user_keywords(user_id: int, keywords: dict) -> bool:
    """更新用户关键词（覆盖写）

    Args:
        user_id: 用户 ID
        keywords: {category: [pattern, ...]}

    Returns:
        bool: 是否成功
    """
    os.makedirs(_KEYWORDS_DIR, exist_ok=True)
    path = _get_keywords_path(user_id)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(keywords, f, ensure_ascii=False, indent=2)
        return True
    except IOError:
        return False


def select_tools_by_intent(user_input: str, all_specs: list, user_id: int = None) -> list:
    """根据用户意图动态选择相关工具

    basic 类始终注入，其他类按意图关键词匹配。
    用户级关键词优先匹配，系统默认兜底。

    Args:
        user_input: 用户输入文本
        all_specs: 所有工具的 OpenAI spec 列表
        user_id: 用户 ID，用于加载用户级关键词

    Returns:
        list: 选中的工具 OpenAI spec 列表
    """
    if len(all_specs) <= 5:
        return all_specs

    all_names = [s["function"]["name"] for s in all_specs]

    # 收集所有已知工具名
    all_tool_names = set()
    for names in TOOL_CATEGORIES.values():
        all_tool_names.update(names)

    # 未知分类的工具始终包含
    unknown_tools = [n for n in all_names if n not in all_tool_names]

    selected_names = set(TOOL_CATEGORIES["basic"])
    selected_names.update(unknown_tools)

    # 获取合并后的关键词
    keywords = get_user_keywords(user_id) if user_id else DEFAULT_KEYWORDS

    # 按关键词匹配意图
    matched_any = False
    for category, patterns in keywords.items():
        for pattern in patterns:
            if re.search(pattern, user_input, re.IGNORECASE):
                selected_names.update(TOOL_CATEGORIES.get(category, []))
                matched_any = True
                break

    # 兜底：未匹配任何类别时回退全量工具
    if not matched_any:
        return all_specs

    return [s for s in all_specs if s["function"]["name"] in selected_names]