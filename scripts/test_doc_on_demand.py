"""文档「按需注入」测试 —— 替代原来的全量注入。

背景：会话上传目录跨轮累积，旧实现每轮把所有历史文档全文注入系统提示词，
token 随文档数线性增长，大文档会话越来越慢。文档又不能像图片那样简单
按轮隔离（有「基于我刚上传的文档…」的跨轮引用需求），故按四类信号按需：

  1. 本轮上传/引用的文档（含 @ 引用历史文件，走同一通道）
  2. 用户消息文本提到文件名的历史文档（全名或去扩展名主名）
  3. 最近一轮上传的文档（覆盖「刚才那个文档」指代）
  4. 老会话无历史上传清单 → 全量注入（兼容优先）

另测「会话文档清单」提示词的构建。

运行：
  /Users/yuanye/.workbuddy/binaries/python/envs/default/bin/python scripts/test_doc_on_demand.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name}  {extra}")


from server.routes.chat import _select_round_documents, _build_doc_index_prompt  # noqa: E402

DIR = ["a.docx", "b.xlsx", "c.pdf", "d.md"]

print("=" * 60)
print("文档按需注入测试")
print("=" * 60)

# 场景 1：本轮上传的文档（必注入）
print("\n[1] 本轮上传")
sel, on = _select_round_documents(DIR, ["a.docx"], ["b.xlsx"], "帮我看下")
# 本轮 a + 上一轮 b（"刚才那个文档"兜底）都注入 —— 跨轮引用窗口有界（2 份）
check("本轮 + 上一轮兜底都注入", sel == {"a.docx", "b.xlsx"}, sel)
check("按需模式开启", on is True)

# 场景 2：@ 引用历史文件（走 uploaded_files 通道）
print("\n[2] @ 引用历史文件")
sel, on = _select_round_documents(DIR, ["c.pdf"], ["a.docx"], "继续")
check("引用的历史文件注入", "c.pdf" in sel, sel)

# 场景 3：消息文本提及文件名（全名 / 主名）
print("\n[3] 文本提及")
sel, _ = _select_round_documents(DIR, [], ["a.docx"], "基于 b.xlsx 生成图表")
check("全名匹配", "b.xlsx" in sel, sel)
sel, _ = _select_round_documents(["report.pdf", "a.docx"], [], ["a.docx"], "把 report 转换成 word")
check("多字符主名匹配", "report.pdf" in sel, sel)
sel, _ = _select_round_documents(DIR, [], ["a.docx"], "把 d 整理一下")
check("单字符主名不匹配（防误匹配）", "d.md" not in sel, sel)
sel, _ = _select_round_documents(DIR, [], ["a.docx"], "把 d.md 整理一下")
check("单字符主名给全名仍匹配", "d.md" in sel, sel)
sel, _ = _select_round_documents(DIR, [], ["a.docx"], "你好呀")
check("无提及不误匹配", sel == {"a.docx"}, sel)   # 只剩最近一轮
sel, _ = _select_round_documents(DIR, [], ["a.docx"], "基于 zz.docx 改一下")
check("提及不存在的文件无效果", "zz.docx" not in sel and sel == {"a.docx"}, sel)

# 场景 4：最近一轮（无名称指代兜底）
print("\n[4] 最近一轮兜底")
sel, _ = _select_round_documents(DIR, [], ["b.xlsx", "c.pdf"], "刚才那个文档帮我改下")
check("上一轮多文件全兜底", sel == {"b.xlsx", "c.pdf"}, sel)
# 最近一轮 + 文本提及另一个 → 两个都进
sel, _ = _select_round_documents(DIR, [], ["b.xlsx"], "结合 a.docx 一起看")
check("最近一轮 + 文本提及合并", sel == {"a.docx", "b.xlsx"}, sel)

# 场景 5：多轮历史但未提及 → 不注入（这是优化核心）
print("\n[5] 未提及的历史文档不注入")
sel, on = _select_round_documents(DIR, [], ["a.docx"], "今天天气怎么样")
check("只注入最近一轮，d/b/c 不注入", sel == {"a.docx"}, sel)
sel, _ = _select_round_documents(["x1.docx", "x2.docx", "x3.docx", "x4.docx"],
                                 [], ["x4.docx"], "讲个笑话")
check("4 份历史文档只注入 1 份", sel == {"x4.docx"}, sel)

# 场景 6：老会话降级（无历史上传清单）
print("\n[6] 老会话降级")
sel, on = _select_round_documents(DIR, ["a.docx"], None, "帮我看下")
check("无清单 → 全量注入", sel == set(DIR), sel)
check("非按需模式", on is False)

# 场景 7：边界
print("\n[7] 边界")
sel, _ = _select_round_documents([], [], [], "空目录")
check("空目录", sel == set())
sel, _ = _select_round_documents(DIR, [None, "", "a.docx"], [], "x")
check("None/空串元素被忽略", sel == {"a.docx"}, sel)
sel, _ = _select_round_documents(DIR, ["不存在的.docx"], ["a.docx"], "x")
check("上传列表里不存在的名字被忽略", sel == {"a.docx"}, sel)

# 场景 8：清单提示词
print("\n[8] 会话文档清单提示词")
p = _build_doc_index_prompt(DIR, {"a.docx"}, True)
check("部分注入时给出未注入引导", "内容未注入" in p and "@ 引用对话中的文件" in p, p[:80])
check("已注入的有标注", "a.docx，内容已注入" in p, p[:120])
p2 = _build_doc_index_prompt(DIR, set(DIR), True)
check("全部注入时无引导语", "@ 引用对话中的文件" not in p2, p2[:80])
check("非按需模式不出引导语", "@ 引用对话中的文件" not in
      _build_doc_index_prompt(DIR, {"a.docx"}, False))
check("空清单返回空串", _build_doc_index_prompt([], set(), True) == "")

# 场景 9：本轮文件清单必须真的落库
# 为什么单独测：整条「按需注入」都建立在这份清单上。清单如果被清洗丢掉，
# 后端就永远不知道哪份文档是哪一轮传的 —— 表现为历史消息没有附件、
# 「刚才那个文档」指代失效，且**不报错**。
print("\n[9] 本轮文件清单落库")
import server.routes.chat as _chat

_captured = {}
_orig_save = _chat._save_session_messages
_orig_load = _chat._load_session_messages
_chat._save_session_messages = lambda sid, msgs, title=None, uid=None: _captured.setdefault("m", msgs)
_chat._load_session_messages = lambda sid: []
_chat._save_user_message_immediate("s_test", "看图", 1, ["a.png", "b.docx"])
_saved = (_captured.get("m") or [{}])[-1]
check("uploaded_files 经过脱敏后仍在", _saved.get("uploaded_files") == ["a.png", "b.docx"], _saved)
check("消息本体未丢", _saved.get("content") == "看图", _saved)

_captured.clear()
_chat._save_user_message_immediate("s_test", "纯文字", 1, [])
_saved2 = (_captured.get("m") or [{}])[-1]
check("无附件时不写空字段（避免污染老逻辑）", "uploaded_files" not in _saved2, _saved2)

_chat._save_session_messages = _orig_save
_chat._load_session_messages = _orig_load

print("\n" + "=" * 60)
print(f"结果: {PASS} 通过, {FAIL} 失败")
if FAILED:
    print("失败项:", FAILED)
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
