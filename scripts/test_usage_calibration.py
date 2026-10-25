#!/usr/bin/env python3
"""Token 校准四项改造的验证脚本。

覆盖：
  ① 图片计数——多模态请求里的图不能像改造前那样整块漏计（agent/llm.py）
  ② 压缩侧的估算必须认多模态 content（agent/agent.py）
  ③ 单价语义——「未标价(None)」与「免费(0)」必须分开，赃值要被拒（model_store）
  ④ 成本折算——缓存是 prompt 的子集、金额只走精确轨（usage_store）

**不碰真实数据库**：所有需要落库的用例都在临时文件上跑，通过替换模块的
`_conn` 拿到自己的连接。它可以随时重跑，不留副作用。

用法：
  python3 scripts/test_usage_calibration.py
"""
import importlib.util as importlib_util
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK] {name}")
    else:
        FAIL.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}".strip())


def _load(name, path):
    spec = importlib_util.spec_from_file_location(name, path)
    mod = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


llm = _load("llm_under_test", os.path.join(ROOT, "agent", "llm.py"))
agent_mod = _load("agent_under_test", os.path.join(ROOT, "agent", "agent.py"))
usage_store = _load("usage_store_under_test", os.path.join(ROOT, "server", "usage_store.py"))
model_store = _load("model_store_under_test", os.path.join(ROOT, "server", "model_store.py"))


def _img(n):
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


print("\n=== ① 图片计数：多模态不能像改造前那样漏计 ===")

msgs = [
    {"role": "system", "content": "你可以读取图片。"},
    {"role": "user", "content": [
        {"type": "text", "text": "这两张图有什么区别？"},
        _img(1), _img(2),
    ]},
]
text, images = llm._analyze_messages(msgs, tools=None)
check("识别出 2 张图片", images == 2, f"got={images}")
check("文本部分仍正常提取", "区别" in text and "image_url" not in text)

text2, images2 = llm._analyze_messages([{"role": "user", "content": "纯文本提问"}])
check("纯文本请求不计图片", images2 == 0, f"got={images2}")

# 回归：改造前 _messages_text 只会取 seg['text']，图片数恒为 0
check("旧行为已被替代（图片不再返回 0）", images != 0)

check("detail=high 每张 1024", llm.estimate_image_tokens(2) == 2048)
check("detail=low 每张 256", llm.estimate_image_tokens(2, detail="low") == 512)
check("零图返回 0", llm.estimate_image_tokens(0) == 0)
check("负数张数不炸", llm.estimate_image_tokens(-5) == 0)

# 图片必须真的加进估算结果
payload_multi = {"p_tokens": llm.estimate_tokens("看看这张图") + llm.estimate_image_tokens(2)}
check("估算包含图片预算", payload_multi["p_tokens"] > 2000,
      f"got={payload_multi['p_tokens']}")

# 工具定义仍按文本计入（tools 也是计费体积的一部分）
_t, _i = llm._analyze_messages([{"role": "user", "content": "hi"}],
                               tools=[{"name": "run_python"}])
check("工具定义按文本计入且不误判为图片", _i == 0 and "run_python" in _t)


print("\n=== ② 压缩侧估算：多模态必须认得，否则长会话永不压缩 ===")

SimpleAgent = agent_mod.SimpleAgent
multimodal = [
    {"role": "user", "content": [{"type": "text", "text": "看图说话"}, _img(1)]},
]
tok = SimpleAgent._estimate_tokens(multimodal)
check("多模态 content 不再按元素个数计算", tok >= llm.IMAGE_TOKENS_PER_IMAGE,
      f"got={tok}（改造前会是 1）")

plain = [{"role": "user", "content": "a" * 400}]
check("纯文本估算维持原口径", SimpleAgent._estimate_tokens(plain) == 400 // 4 + 1,
      f"got={SimpleAgent._estimate_tokens(plain)}")

with_tools = [{"role": "assistant", "content": "ok",
               "tool_calls": [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]}]
check("工具调用参数计入体积", SimpleAgent._estimate_tokens(with_tools) > 1)


print("\n=== ③ 单价语义：None(未标价) 与 0(免费) 必须分开 ===")

check("空值归一化为 None", model_store._norm_price(None) is None)
check("空字符串归一化为 None", model_store._norm_price("") is None)
check("0 保留为免费而非未知", model_store._norm_price(0) == 0.0)
check("正常数字通过", model_store._norm_price("2.5") == 2.5)

for bad, label in [(float("nan"), "NaN"), (-1, "负数"), (-0.5, "负小数"),
                   (2_000_000, "超出范围"), ("abc", "非数字")]:
    try:
        model_store._norm_price(bad)
        check(f"拒绝 {label}", False, "未被拦截")
    except ValueError:
        check(f"拒绝 {label}", True)

# 注意 `a == a is False` 在 Python 里是链式比较，会写错意思；NaN 的判断要直写 !=
_nan = float("nan")
check("NaN 是合法 float 且不等于自身（普通比较拦不住）", _nan != _nan)
check("所以 _norm_price 必须专门拦 NaN", model_store._norm_price.__doc__ is not None)


print("\n=== ④ 成本折算：缓存是子集，金额只认精确轨 ===")

# 口径 1：cached 是 prompt 的子集，不能全额按输入价算
# prompt=10000, cached=8000, completion=100；输入价 2 / 输出价 8 / 缓存价 0.2（元每百万）
# 未命中 2000 → 2000/1e6*2   = 0.0040
# 命中   8000 → 8000/1e6*0.2 = 0.0016
# 输出    100 → 100/1e6*8    = 0.0008
# 合计 0.0064
c1 = usage_store.cost_of(10000, 100, 8000, (2.0, 8.0, 0.2))
check("未命中按输入价、命中按缓存价、输出另计", abs(c1 - 0.0064) < 1e-9, f"got={c1}")

# 缓存价留空时回落到输入价：0.0040 + 0.016 + 0.0008 = 0.0208
c_full = usage_store.cost_of(10000, 100, 8000, (2.0, 8.0, None))
check("缓存价未标定则回落输入价（不是按免费）", abs(c_full - 0.0208) < 1e-9, f"got={c_full}")
check("回落后的金额高于打折情形", c_full > c1)

check("无单价返回 None 而不是 0", usage_store.cost_of(100, 10, 0, None) is None)
check("全 0 用量成本为 0", usage_store.cost_of(0, 0, 0, (2.0, 8.0, 0.2)) == 0.0)

# 口径 2：cached 超过 prompt 时不能算出负数（-= fire）
c_neg = usage_store.cost_of(100, 10, 5000, (2.0, 8.0, 0.2))
check("脏数据（缓存>输入）不产生负金额", c_neg is not None and c_neg >= 0, f"got={c_neg}")


print("\n=== ⑤ 端到端：真实 SQLite 上的双轨分离与金额 ===")

_tmp_db = os.path.join(ROOT, "data", "_test_calibration.db")
if os.path.exists(_tmp_db):
    os.remove(_tmp_db)
_tmp_conn = sqlite3.connect(_tmp_db)
_tmp_conn.row_factory = sqlite3.Row
usage_store._conn = lambda: _tmp_conn
usage_store.ensure_table(_tmp_conn)

# 暂存真实实现，用完还原 —— 否则后面几节拿不到被测代码
_real_price_map = usage_store.price_map
fake_prices = {
    "ds-flash||平台A": (2.0, 8.0, 0.2),
    "ds-flash": (2.0, 8.0, 0.2),
}
usage_store.price_map = lambda: fake_prices

# 三行数据：2 条精确（其中 1 条命中缓存）、1 条估算
rows = [
    (1, "2026-10-20 10:00:00", "chat", "ds-flash", "平台A", 10000, 100, 8000, 10100, 0, 500, 300, 0),
    (1, "2026-10-20 11:00:00", "chat", "ds-flash", "平台A", 20000, 200, 0, 20200, 0, 900, 400, 0),
    (1, "2026-10-20 12:00:00", "chat", "ds-flash", "平台A", 8000, 50, 0, 8050, 1, 700, 100, 0),
]
for user_id, ts, src, model, pv, p, c, ca, t, est, pc, cc, ic in rows:
    _tmp_conn.execute(
        "INSERT INTO token_usage (user_id, session_id, run_id, source, model, provider_name, "
        "entry_owner, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, "
        "total_tokens, is_estimated, duration_ms, prompt_chars, completion_chars, "
        "image_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, "s1", "r1", src, model, pv, "platform",
         p, c, ca, 0, t, est, 1000, pc, cc, ic, ts))
_tmp_conn.commit()

out = usage_store.summary_by_day(1, "2026-10-20", "2026-10-20")
day = out["days"][0]
total = out["total"]

check("总 token 保持全量口径", total["total_tokens"] == 10100 + 20200 + 8050,
      f"got={total['total_tokens']}")
check("精确量与估算量已分离",
      total["total_exact"] == 10100 + 20200 and (total["total_tokens"] - total["total_exact"]) == 8050,
      f"exact={total['total_exact']}")
check("每天也有独立的精确量", day["total_exact"] == 30300, f"got={day['total_exact']}")

# 精确金额 = 行1(0.0056 + 输出 100/1e6*8=0.0008) + 行2(20000/1e6*2 + 200/1e6*8)
#          = 0.0064 + (0.04 + 0.0016) = 0.048
expect = round(0.0064 + 0.0416, 4)
check("金额只由精确轨折算", abs(total["cost"] - expect) < 1e-6,
      f"got={total['cost']} expect={expect}")
check("估算那次没有被算进金额", abs(day["cost"] - expect) < 1e-6, f"got={day['cost']}")
check("仍能感知存在估算调用", total["estimated_calls"] == 1)

# 换一个没有标价的模型：成本必须显示「未知」而不是 0
_tmp_conn.execute(
    "INSERT INTO token_usage (user_id, session_id, source, model, provider_name, "
    "entry_owner, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, "
    "total_tokens, is_estimated, duration_ms, prompt_chars, completion_chars, "
    "image_count, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (1, "s1", "chat", "no-price-model", "平台B", "platform",
     1000, 10, 0, 0, 1010, 0, 100, 50, 5, 0, "2026-10-20 13:00:00"))
_tmp_conn.commit()
out2 = usage_store.summary_by_day(1, "2026-10-20", "2026-10-20")
check("未标价模型的消耗被识别出来", out2["total"]["unpriced_tokens"] == 1010,
      f"got={out2['total']['unpriced_tokens']}")
check("未标价部分不会虚增金额", abs(out2["total"]["cost"] - expect) < 1e-6)

# 缓存命中率仍然只吃精确轨分母：此刻精确轨 prompt = 10000+20000+1000 = 31000，
# 命中的 8000 全部来自第一行（估算行与新插入的未标价行都不进分母）
check("缓存命中率只按精确轨计算",
      abs(out2["total"]["cache_rate"] - 8000 / 31000 * 100) < 0.05,
      f"got={out2['total']['cache_rate']}")

_tmp_conn.close()
os.remove(_tmp_db)

print("\n=== ⑥ 价格匹配：同名模型挂多家供应商时不能瞎猜 ===")

usage_store.price_map = _real_price_map


class _FakeConn:
    """最小替身：price_map 只用到 execute().fetchall()，够了。"""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, *args):
        return self

    def fetchall(self):
        return self._rows


# 同一个 model_id 在两家供应商下价钱不同 —— 自建/代理加价的常见情形
usage_store._conn = lambda: _FakeConn([
    {"model_id": "shared-model", "price_in": 1.0, "price_out": 2.0,
     "price_cached": 0.1, "pname": "供应商甲"},
    {"model_id": "shared-model", "price_in": 9.0, "price_out": 9.0,
     "price_cached": 9.0, "pname": "供应商乙"},
    {"model_id": "solo-model", "price_in": 3.0, "price_out": 4.0,
     "price_cached": 0.3, "pname": "供应商丙"},
])

pm = usage_store.price_map()
check("带供应商的精确键始终可用",
      pm["shared-model||供应商甲"][0] == 1.0 and pm["shared-model||供应商乙"][0] == 9.0)
check("同名但价不同 → 不建回退键", "shared-model" not in pm,
      "若建了回退键，同一个模型会算出两种钱，取决于表的物理顺序")
check("唯一模型 → 建回退键", pm.get("solo-model", (None,))[0] == 3.0)
check("未标价的模型不进表", "没有标价的模型" not in pm)


print("\n" + "=" * 58)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("\n失败明细：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
