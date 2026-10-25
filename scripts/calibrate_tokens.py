"""Token 用量「体检」诊断脚本（**只读**，不写任何数据）。

回答三个问题：
  1. 我的用量里，有多大比例是「估算」而不是「服务商真值」？
  2. 估算到底偏多少？——用 tiktoken 当标尺量化（有则用，无则跳过）。
  3. 值不值得为校准投入工程？——给出结论而不是让读者自己猜。

用法:
    python scripts/calibrate_tokens.py              # 体检当前数据库
    python scripts/calibrate_tokens.py --days 7     # 只看最近 7 天
    python scripts/calibrate_tokens.py --samples    # 额外跑估算器的样本误差测试
"""

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "users.db")

# 偏差在这个范围内视为「够用」，不必为它投入工程
ACCEPTABLE_TOTAL_ERROR = 0.01  # 1%


def _open():
    if not os.path.exists(DB):
        print(f"[!] 找不到数据库: {DB}")
        return None
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def rail_split(conn, days=None):
    """精确轨 vs 估算轨的占比（按调用次数与 token 量两个口径）。"""
    where, params = "", []
    if days:
        where = "WHERE created_at >= datetime('now','localtime',?)"
        params = [f"-{int(days)} days"]

    row = conn.execute(
        "SELECT COUNT(*) AS calls, "
        "SUM(is_estimated) AS est_calls, "
        "SUM(total_tokens) AS total, "
        "SUM(CASE WHEN is_estimated=1 THEN total_tokens ELSE 0 END) AS est_total, "
        "SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, "
        "SUM(cached_tokens) AS cached, "
        "SUM(CASE WHEN is_estimated=0 THEN prompt_tokens ELSE 0 END) AS p_exact, "
        "SUM(CASE WHEN is_estimated=0 THEN cached_tokens ELSE 0 END) AS c_exact "
        f"FROM token_usage {where}", params).fetchone()

    calls = int(row["calls"] or 0)
    if not calls:
        return None
    total = int(row["total"] or 0)
    est_total = int(row["est_total"] or 0)
    p_exact = int(row["p_exact"] or 0)
    c_exact = int(row["c_exact"] or 0)
    return {
        "calls": calls,
        "est_calls": int(row["est_calls"] or 0),
        "total": total,
        "est_total": est_total,
        "est_call_rate": int(row["est_calls"] or 0) / calls * 100,
        "est_token_rate": est_total / total * 100 if total else 0.0,
        "p": int(row["p"] or 0), "c": int(row["c"] or 0),
        "cached": int(row["cached"] or 0),
        "cache_rate": c_exact / p_exact * 100 if p_exact else 0.0,
    }


def fit_coefficients(conn, min_samples=20):
    """拿精确行当样本，回归「本项目真实的 tokens/字符」系数。

    原理：精确行的 prompt_tokens 是服务商认定的真值，prompt_chars 是我们自己数的
    字符数，两者配对就是一条训练样本。累积够多就能替代 estimate_tokens 里那个
    通用经验值（中文 1.0 / 其余 0.25）—— 用自己数据的规律，而不是别人的平均数。

    排除带图片的行：图片按张数计费，与字符数无关，混进来会污染回归。

    Returns:
        dict 或 None（样本不足时返回 None，绝不返回不可信的系数）
    """
    rows = conn.execute(
        "SELECT prompt_chars, completion_chars, prompt_tokens, completion_tokens, model "
        "FROM token_usage WHERE is_estimated = 0 AND prompt_chars > 0 "
        "AND COALESCE(image_count, 0) = 0 AND prompt_tokens > 0"
    ).fetchall()

    if len(rows) < min_samples:
        return None, len(rows)

    def _fit(xs, ys):
        n = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        # 含截距的最小二乘：y = k*x + b
        denom = n * sxx - sx * sx
        if denom == 0:
            return None
        k = (n * sxy - sx * sy) / denom
        b = (sy - k * sx) / n
        # R²：拟合优度，低于 0.6 说明单变量解释力不足
        ybar = sy / n
        ss_tot = sum((y - ybar) ** 2 for y in ys)
        ss_res = sum((y - (k * x + b)) ** 2 for x, y in zip(xs, ys))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
        # 过原点的总体系率：sum(y)/sum(x)，物理意义最直观，也最稳
        ratio = sy / sx if sx else 0.0
        return {"k": k, "b": b, "r2": r2, "ratio": ratio, "n": n}

    pxs = [int(r["prompt_chars"]) for r in rows]
    pys = [int(r["prompt_tokens"]) for r in rows]
    # 输出侧只取有输出的行（纯工具调用可能 completion=0）
    cxs = [int(r["completion_chars"]) for r in rows
           if int(r["completion_chars"] or 0) > 0 and int(r["completion_tokens"] or 0) > 0]
    cys = [int(r["completion_tokens"]) for r in rows
           if int(r["completion_chars"] or 0) > 0 and int(r["completion_tokens"] or 0) > 0]

    prompt_fit = _fit(pxs, pys)
    completion_fit = _fit(cxs, cys) if len(cxs) >= min_samples else None

    # 按模型拆：不同厂商的 tokenizer 不一样，混在一起回归会互相拉偏
    by_model = {}
    buckets = {}
    for r in rows:
        buckets.setdefault(r["model"] or "(unknown)", []).append(r)
    for model, rs in buckets.items():
        if len(rs) < min_samples:
            continue
        f = _fit([int(x["prompt_chars"]) for x in rs],
                 [int(x["prompt_tokens"]) for x in rs])
        if f:
            by_model[model] = f

    return {"prompt": prompt_fit, "completion": completion_fit,
            "by_model": by_model, "total": len(rows)}, len(rows)


def estimator_error():
    """用 tiktoken 当标尺，量化 estimate_tokens 的偏差。

    注意：cl100k_base 是 OpenAI 的 tokenizer，与 DeepSeek 自有 tokenizer 不完全一致，
    所以本测试只用于判断「数量级」，不足以要求估算收敛到 1% 以内。
    """
    try:
        import tiktoken
    except Exception:
        print("  （未安装 tiktoken，跳过样本误差测试）")
        return None
    try:
        from agent.llm import estimate_tokens
    except Exception as e:
        print(f"  （无法导入 estimate_tokens: {e}）")
        return None

    enc = tiktoken.get_encoding("cl100k_base")
    samples = {
        "纯中文短句": "请帮我分析一下这份数据，看看里面有没有异常的地方，如果有请指出具体行数。",
        "中英混合": "请用 Python 写一个 def parse_file(path) 函数，处理 UTF-8 编码的文件，返回 dict。",
        "工具定义JSON": '{"name":"run_python","description":"在沙箱中执行 Python 代码",'
                        '"parameters":{"type":"object","properties":{"code":{"type":"string"}}}}',
        "代码块": "import os\nimport json\n\ndef main():\n    out=[]\n"
                  "    for r,d,f in os.walk('.'):\n        out+=f\n    return json.dumps(out)\n",
        "工具调用JSON": '[{"id":"call_1","type":"function","function":'
                        '{"name":"read_file","arguments":"{\\"path\\":\\"/a/b.txt\\"}"}}]',
        "系统提示词": "你是一个智能助手，可以使用工具完成任务。\n\n注意事项：\n"
                     "1. 先思考再行动\n2. 不要重复调用相同工具\n3. 输出简洁",
        "中文长段落": "由于历史原因，系统在设计之初没有考虑多租户隔离，"
                     "后续迭代中需要补齐这块能力，避免用户数据互串。" * 5,
    }
    errs = []
    print(f"  {'样本':<12} {'估算':>7} {'真值':>7} {'偏差':>9}")
    print("  " + "-" * 40)
    for name, text in samples.items():
        est = estimate_tokens(text)
        real = len(enc.encode(text))
        err = (est - real) / real * 100 if real else 0.0
        errs.append(abs(err))
        print(f"  {name:<12} {est:>7} {real:>7} {err:>+8.1f}%")
    return sum(errs) / len(errs) if errs else None


def _ensure_schema(conn):
    """补齐用量表的新列（幂等）。

    这些列本来由服务启动时的 `usage_store.ensure_table` 添加；脚本自己调一次，
    是为了在「还没重启过服务」时也能跑 --fit —— 否则会直接报列不存在。
    只 ADD COLUMN，不动任何已有数据。
    """
    added = []
    for col, decl in (("provider_name", "TEXT DEFAULT ''"),
                      ("entry_owner", "TEXT DEFAULT ''"),
                      ("prompt_chars", "INTEGER DEFAULT 0"),
                      ("completion_chars", "INTEGER DEFAULT 0"),
                      ("image_count", "INTEGER DEFAULT 0")):
        try:
            conn.execute(f"ALTER TABLE token_usage ADD COLUMN {col} {decl}")
            added.append(col)
        except sqlite3.OperationalError:
            pass  # 已存在
    if added:
        conn.commit()
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None, help="只看最近 N 天")
    ap.add_argument("--samples", action="store_true", help="跑估算器样本误差测试")
    ap.add_argument("--fit", action="store_true", help="用自身精确行回归折算系数")
    ap.add_argument("--min-samples", type=int, default=20,
                    help="回归所需的最少样本数（默认 20，低于此值不给结论）")
    args = ap.parse_args()

    conn = _open()
    if conn is None:
        return 1

    print("=" * 62)
    print("Token 用量体检")
    print("=" * 62)

    added = _ensure_schema(conn)
    if added:
        print(f"（已自动补齐用量表新列：{', '.join(added)}；不影响任何已有数据）")

    try:
        s = rail_split(conn, args.days)
    except Exception as e:
        print(f"[!] 读取失败（表还不存在？）: {e}")
        return 1

    if not s:
        print("\n还没有任何用量记录。先跑一次对话再来体检。")
        return 0

    scope = f"最近 {args.days} 天" if args.days else "全部历史"
    print(f"\n统计范围：{scope}")
    print(f"  调用次数      {s['calls']:,} 次")
    print(f"  累计 token    {s['total']:,}")
    print(f"  其中输入      {s['p']:,} / 输出 {s['c']:,}")

    print(f"\n【双轨占比】决定「估算」值不值得管")
    print(f"  精确轨（服务商回传）  {s['calls'] - s['est_calls']:,} 次  "
          f"{s['total'] - s['est_total']:,} token  "
          f"{100 - s['est_token_rate']:.1f}%")
    print(f"  估算轨（字符兜底）    {s['est_calls']:,} 次  "
          f"{s['est_total']:,} token  "
          f"{s['est_token_rate']:.1f}%")

    print(f"\n【缓存命中率】精确轨专属口径")
    print(f"  命中率 {s['cache_rate']:.1f}%  （命中部分通常按折扣价计费）")

    mae = estimator_error() if args.samples else None

    print(f"\n" + "=" * 62)
    print("结论")
    print("=" * 62)

    if mae is not None:
        impact = s["est_token_rate"] / 100 * mae / 100 * 100
        print(f"估算器平均偏差        {mae:.1f}%")
        print(f"对总用量的实际影响    ≈ {impact:.2f}%")
        verdict_ok = impact < ACCEPTABLE_TOTAL_ERROR * 100
        print(f"是否值得为它投入工程  {'否 —— 影响已可忽略' if verdict_ok else '是'}")
    else:
        print("（加 --samples 可量化估算器偏差）")

    if args.fit:
        print(f"\n" + "=" * 62)
        print("自校准：用你自己的数据回归折算系数")
        print("=" * 62)
        result, n = fit_coefficients(conn, args.min_samples)
        if result is None:
            print(f"\n  样本不足：只有 {n} 条精确记录带字符数（需要 ≥ {args.min_samples} 条）。")
            print("  这些列是本次改造才加的，之前的调用没有留下字符数。")
            print("  → 继续正常使用即可自动积累；攒够后再跑本命令就能拿到系数。")
            print("  这不是失败，是「还没有能证明规律的数据」—— 宁可不给数，也不给假的数。")
        else:
            pf = result["prompt"]
            print(f"\n  样本 {pf['n']} 条（已排除带图片的行）")
            print(f"\n  【输入侧】")
            print(f"    平均比率      {pf['ratio']:.4f} token/字符")
            print(f"    线性斜率      {pf['k']:.4f}  截距 {pf['b']:.1f}   R² {pf['r2']:.3f}")
            if pf["r2"] < 0.6:
                print(f"    ⚠ R² 偏低：单靠字符数解释力不足，建议记录更细的特征")
            eff = pf["ratio"]
            print(f"    对照现在的经验值：纯中文 1.0 / 纯英文符号 0.25")
            if eff > 0.8:
                print(f"    → 你的实际文本接近纯中文，现有估算偏高，可用 {eff:.3f} 替换")
            elif eff < 0.4:
                print(f"    → 你的实际文本偏代码/英文，可用 {eff:.3f} 替换")
            else:
                print(f"    → 混合文本，按 {eff:.3f} 折算比固定 0.25/1.0 更贴近实况")

            cf = result["completion"]
            if cf:
                print(f"\n  【输出侧】")
                print(f"    平均比率      {cf['ratio']:.4f} token/字符   R² {cf['r2']:.3f}")
            else:
                print(f"\n  【输出侧】样本不足 {args.min_samples} 条，暂不给系数")

            if len(result["by_model"]) > 1:
                print(f"\n  【按模型】不同厂商 tokenizer 不同，分开回归更准：")
                for model, f in sorted(result["by_model"].items(),
                                       key=lambda kv: -kv[1]["n"]):
                    print(f"    {model:<38} {f['ratio']:.4f} token/字符  n={f['n']}")
            elif result["by_model"]:
                for model, f in result["by_model"].items():
                    print(f"\n  仅一个模型有足够样本：{model} → {f['ratio']:.4f} token/字符")

            print(f"\n  落到代码：把 agent/llm.py::estimate_tokens 的系数换成上面的值。")

    exhibit = []
    if s["est_token_rate"] > 5:
        exhibit.append(f"估算占比 {s['est_token_rate']:.1f}% 偏高，先查成因，别急着换算法")
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(token_usage)")]
        if "prompt_chars" not in cols:
            exhibit.append("token_usage 缺 prompt_chars 列 → 无法用自身数据训练折算系数")
        else:
            # 列有了不等于样本有了：新列之前的调用没有留下字符数
            n = conn.execute(
                "SELECT COUNT(*) c FROM token_usage WHERE is_estimated = 0 "
                "AND prompt_chars > 0 AND COALESCE(image_count,0) = 0"
            ).fetchone()["c"]
            if n < args.min_samples:
                exhibit.append(
                    f"自校准样本仅 {n} 条（需 ≥{args.min_samples}）→ 正常使用即可自动积累，"
                    f"攒够后跑 --fit 拿系数")
    except Exception:
        pass

    # 「供应商名为空」的历史用量：早期记录没有沉淀供应商，只能靠 model_id 回退匹配。
    # 同一 model_id 挂在不同供应商下价钱可能不同，这部分会落到「未标价」。
    try:
        amb = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(total_tokens), 0) t FROM token_usage "
            "WHERE is_estimated = 0 AND COALESCE(provider_name, '') = ''"
        ).fetchone()
        if amb["c"]:
            exhibit.append(
                f"{amb['c']} 条精确用量没有记录供应商名（{int(amb['t']):,} token）→ "
                f"同一模型挂多家供应商时这部分算不出唯一的钱，需人工确认")
    except Exception:
        pass

    # 问「有没有模型标过价」而不是「有没有单价字段」：字段会在服务启动时自动补上，
    # 而「没标过价」才是成本算不出来的真实原因，也是用户能立刻动手解决的那件事。
    try:
        priced = conn.execute(
            "SELECT COUNT(*) c FROM model_entries "
            "WHERE price_in IS NOT NULL OR price_out IS NOT NULL"
        ).fetchone()["c"]
        if priced == 0:
            exhibit.append("还没有模型标过单价 → 折算成本会是 0，去模型池填「输入/输出单价」")
    except Exception:
        pass

    if exhibit:
        print("\n比「估算不准」更值得先做的：")
        for i, x in enumerate(exhibit, 1):
            print(f"  {i}. {x}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
