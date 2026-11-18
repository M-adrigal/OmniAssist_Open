#!/usr/bin/env python3
"""联网搜索运行时支撑的回归测试（零依赖，用内存 SQLite，不联网、不碰真实库）。

覆盖：缓存的命中/过期/键归一、计数的自增与汇总、缓存上限不炸。

用法：python3 scripts/test_search_runtime.py
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 按路径加载，绕开 server/__init__.py（它会 import main → 需要 fastapi）
_spec = importlib.util.spec_from_file_location("search_runtime", ROOT / "server" / "search_runtime.py")
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)


def main():
    ok = True
    n = 0

    def check(name, cond, extra=""):
        nonlocal ok, n
        n += 1
        print(f"  {'✓' if cond else '✗'} {name}{(' — ' + str(extra)) if extra and not cond else ''}")
        if not cond:
            ok = False

    print("\n[1] 缓存")
    rt.clear_cache()
    check("未命中返回 None", rt.cache_get("no-such-key") is None)
    rt.cache_put("k1", "v1", 60)
    check("命中返回原值", rt.cache_get("k1") == "v1")
    rt.cache_put("k2", "v2", 0)
    check("ttl=0 立刻过期", rt.cache_get("k2") is None)
    rt.cache_put("k3", "v3", 60)
    rt.cache_put("k3", "v3-new", 60)
    check("同键覆盖（后写的赢）", rt.cache_get("k3") == "v3-new")

    print("\n[2] 缓存键归一")
    check("None / 空串被忽略",
          rt.cache_key("search", "deepseek", None, "", "query") == "search|deepseek|query",
          rt.cache_key("search", "deepseek", None, "", "query"))
    check("不同供应商 → 不同键",
          rt.cache_key("search", "tavily", "q") != rt.cache_key("search", "deepseek", "q"))
    check("不同场景 → 不同键",
          rt.cache_key("search", "x", "q", "general") != rt.cache_key("search", "x", "q", "realtime"))

    print("\n[3] 缓存上限不炸")
    rt.clear_cache()
    for i in range(rt._MAX_ENTRIES + 50):
        rt.cache_put(f"bulk-{i}", i, 60)
    check(f"写入 {rt._MAX_ENTRIES + 50} 条后仍可读写",
          rt.cache_get(f"bulk-{rt._MAX_ENTRIES + 49}") == rt._MAX_ENTRIES + 49)
    check("条数不超过上限", len(rt._cache) <= rt._MAX_ENTRIES, len(rt._cache))

    print("\n[4] 计数与汇总")
    conn = sqlite3.connect(":memory:")
    rt.bump("tavily", "search", conn=conn)
    rt.bump("tavily", "search", conn=conn)
    rt.bump("deepseek", "search", conn=conn)
    rt.bump("deepseek", "fetch", conn=conn)
    s = rt.summary(conn=conn)
    check("Tavily 搜索计数 = 2", s["month_total"].get("tavily") == 2, s)
    check("DeepSeek 合计 = 2（search + fetch）", s["month_total"].get("deepseek") == 2, s)
    check("按类型汇总 search=3", s["by_kind"].get("search") == 3, s)
    check("按类型汇总 fetch=1", s["by_kind"].get("fetch") == 1, s)
    check("今日与本月一致（都是今天写的）", s["today"].get("tavily") == 2, s["today"])
    check("月份格式 YYYY-MM", len(s["month"]) == 7 and s["month"][4] == "-", s["month"])

    print("\n[5] 空库不报错")
    empty = sqlite3.connect(":memory:")
    s2 = rt.summary(conn=empty)
    check("空库返回空统计", s2["month_total"] == {} and s2["by_kind"] == {}, s2)

    print("\n" + "=" * 68)
    print(f"{'✓ 全部通过' if ok else '✗ 存在失败项'}（{n} 项检查）")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
