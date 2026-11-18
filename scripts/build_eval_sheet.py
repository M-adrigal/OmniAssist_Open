"""生成 Agent 效果评估表，并把跑批结果回填进去。

题目从 scripts/eval_samples.py 读（唯一真相源），结果从 data/eval_results_*.json 读。
不维护第二份题目，避免「表里写的」和「实际跑的」对不上。

用法：
    # 只出空表（还没跑批时）
    /usr/bin/python3 scripts/build_eval_sheet.py

    # 出表并回填最新一次跑批结果
    /usr/bin/python3 scripts/build_eval_sheet.py --results data/eval_results_xxx.json

回填规则（重要）：
    机器只填它**确实判得了**的列。「出现幻觉」永远留空，「参数正确」在参数陷阱类
    也留空 —— 这些要人看语义，机器判错比不判更糟（人会以为已经判过了）。
"""
import argparse
import json
import re
import sys
from pathlib import Path

# 工具返回可能带原始二进制（如 od/xxd 输出、文件内容），里面会有控制字符 ——
# openpyxl 对控制字符直接抛 IllegalCharacterError，整张表都生成不出来。
# 写入前统一清洗（保留换行/制表，剔除其余 C0 控制字符与非法码位）。
_ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _clean(v):
    return _ILLEGAL_XLSX.sub("", v) if isinstance(v, str) else v


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from eval_samples import SAMPLES  # noqa: E402

try:
    import openpyxl
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "openpyxl>=3.1.0"])
    import openpyxl

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.formatting.rule import CellIsRule
from openpyxl.worksheet.datavalidation import DataValidation

OUT = Path("/Users/yuanye/Desktop/OmniAssist产品设计文档/OmniAssist-v2.4.0-Agent效果评估表.xlsx")
ROUNDS_DEFAULT = 2


def xl_color(h: str) -> str:
    v = h.removeprefix("#").upper()
    if len(v) != 6:
        raise ValueError(h)
    return "FF" + v


XL_TITLE = xl_color("#1F3864")
XL_HEADER = xl_color("#4472C4")
XL_KPI_VALUE = xl_color("#D9E2F3")
XL_INPUT = xl_color("#EEF3FB")
XL_MACHINE = xl_color("#F2F7EC")        # 机器填的格子：极浅绿，和人填的浅蓝区分开
XL_BORDER = xl_color("#BFBFBF")
XL_GREEN_BG, XL_GREEN_FG = xl_color("#C6EFCE"), xl_color("#006100")
XL_RED_BG, XL_RED_FG = xl_color("#FFC7CE"), xl_color("#9C0006")
XL_YELLOW_BG = xl_color("#FFEB9C")

FONT = "微软雅黑"
thin = Side(style="thin", color=XL_BORDER)
BOX = Border(left=thin, right=thin, top=thin, bottom=thin)


def style_header(ws, row, headers, widths, height=30):
    for col, name in zip("ABCDEFGHIJKLMN", headers):
        c = ws[f"{col}{row}"]
        c.value = name
        c.font = Font(name=FONT, size=10, bold=True, color=xl_color("#FFFFFF"))
        c.fill = PatternFill("solid", fgColor=XL_HEADER)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BOX
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.row_dimensions[row].height = height


def load_results(path):
    """返回 {(sample_id, round): record}。"""
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {(r["sample"], r["round"]): r for r in data}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="", help="跑批结果 JSON 路径")
    ap.add_argument("--rounds", type=int, default=0, help="不填则按结果里的最大轮次")
    args = ap.parse_args()

    res = load_results(args.results) if args.results else {}
    rounds = args.rounds or (max((k[1] for k in res), default=0) or ROUNDS_DEFAULT)

    wb = Workbook()
    wb.properties.title = "OmniAssist Agent 效果评估表"

    # ==================== 评估样本 ====================
    ws_s = wb.active
    ws_s.title = "评估样本"
    ws_s.merge_cells("A1:G1")
    c = ws_s["A1"]
    c.value = "Agent 效果评估 · 评估样本集"
    c.font = Font(name=FONT, size=15, bold=True, color=XL_TITLE)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws_s.row_dimensions[1].height = 30

    ws_s.merge_cells("A2:G2")
    c = ws_s["A2"]
    c.value = ("题目与跑批脚本共用 scripts/eval_samples.py，改题请改那里再重新生成表格，"
               "不要直接改这个 sheet —— 否则表里的题和脚本跑的题会不一致，指标就失去意义。")
    c.font = Font(name=FONT, size=9, color=xl_color("#595959"))
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws_s.row_dimensions[2].height = 26

    style_header(ws_s, 4, ["编号", "类别", "用户提问", "必须调用的工具", "不要调用的工具",
                           "判定要点", "轮数"],
                 {"A": 8, "B": 16, "C": 42, "D": 26, "E": 18, "F": 48, "G": 8})

    S_FIRST = 5
    for i, s in enumerate(SAMPLES):
        r = S_FIRST + i
        vals = [s["id"], s["cat"],
                " → ".join(s["turns"]),
                "、".join(s["must"]) or "—",
                "、".join(s["forbid"]) or "—",
                s["judge"], len(s["turns"])]
        for col, val in zip("ABCDEFG", vals):
            cell = ws_s[f"{col}{r}"]
            cell.value = val
            cell.font = Font(name=FONT, size=10)
            cell.border = BOX
            if col in ("C", "D", "F"):
                cell.alignment = Alignment(vertical="top", wrap_text=True, horizontal="left")
            else:
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws_s.freeze_panes = f"A{S_FIRST}"
    ws_s.auto_filter.ref = f"A4:G{S_FIRST + len(SAMPLES) - 1}"
    ws_s.sheet_view.showGridLines = False

    # ==================== 逐次记录 ====================
    ws_r = wb.create_sheet("逐次记录")
    ws_r.merge_cells("A1:K1")
    c = ws_r["A1"]
    c.value = "Agent 效果评估 · 逐次运行记录"
    c.font = Font(name=FONT, size=15, bold=True, color=XL_TITLE)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws_r.row_dimensions[1].height = 30

    ws_r.merge_cells("A2:K2")
    c = ws_r["A2"]
    c.value = ("浅绿底＝脚本自动判的（工具调全 / 有多余调用 / 执行成功 / 迭代次数）；"
               "浅蓝底＝需要你判的。**「出现幻觉」全部留空等人工**，"
               "「参数正确」在参数陷阱类也留空 —— 参数错也可能算出结果，机器看不出来。"
               "备注列写了耗时与实际调用工具，方便复核。")
    c.font = Font(name=FONT, size=9, color=xl_color("#595959"))
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws_r.row_dimensions[2].height = 30

    R_HEADERS = ["序号", "样本编号", "提问", "工具调全", "有多余调用", "参数正确",
                 "执行成功", "任务达成", "出现幻觉", "迭代次数", "备注"]
    style_header(ws_r, 4, R_HEADERS,
                 {"A": 6, "B": 10, "C": 38, "D": 10, "E": 12, "F": 10,
                  "G": 10, "H": 10, "I": 10, "J": 10, "K": 34})

    R_FIRST = 5
    rows = []
    for s in SAMPLES:
        for k in range(1, rounds + 1):
            rows.append((s, k))
    R_LAST = R_FIRST + len(rows) - 1

    MACHINE_COLS = {"D", "E", "G", "J"}     # 完全由机器填
    for i, (s, rnd) in enumerate(rows):
        r = R_FIRST + i
        rec = res.get((s["id"], rnd))
        j = (rec or {}).get("judge") or {}

        ws_r[f"A{r}"] = i + 1
        ws_r[f"B{r}"] = f"{s['id']}#{rnd}"
        ws_r[f"C{r}"] = " → ".join(s["turns"])

        def pick(key):
            v = j.get(key)
            return v if v in ("是", "否") else None

        ws_r[f"D{r}"] = pick("tool_complete")
        ws_r[f"E{r}"] = pick("has_extra")
        ws_r[f"F{r}"] = pick("param_ok")
        ws_r[f"G{r}"] = pick("exec_ok")
        # 人工判定统一在「回答与判定」做（那里能看到回答全文），这里只做引用：
        # 表格里只给「是/否」不给原文，人是判不出幻觉的 —— 这是最初设计漏掉的一环。
        # 加 IF 防空：直接引用空单元格会得到 0，界面上就是一片 0
        ws_r[f"H{r}"] = f'=IF(\'回答与判定\'!G{r}="","",\'回答与判定\'!G{r})'
        ws_r[f"I{r}"] = f'=IF(\'回答与判定\'!H{r}="","",\'回答与判定\'!H{r})'
        if j.get("iterations") is not None:
            ws_r[f"J{r}"] = j["iterations"]

        # 备注：耗时 + 实际工具 + 待人工提示
        if rec is None:
            note = "未跑"
        elif rec.get("error"):
            note = f"跑批失败：{rec['error'][:60]}"
        else:
            tools = "、".join(j.get("actual_tools") or []) or "无工具"
            note = f"{rec.get('elapsed', '?')}s ｜ {tools}"
            if s["expect"] and pick("task_ok") is None:
                note += " ｜ 待人工判"
        ws_r[f"K{r}"] = note

        for col in "ABCDEFGHIJK":
            cell = ws_r[f"{col}{r}"]
            cell.font = Font(name=FONT, size=10)
            cell.border = BOX
            if col in ("C", "K"):
                cell.alignment = Alignment(vertical="top", wrap_text=True, horizontal="left")
            else:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            if col in MACHINE_COLS:
                cell.fill = PatternFill("solid", fgColor=XL_MACHINE)
            elif col in ("F", "H", "I"):
                cell.fill = PatternFill("solid", fgColor=XL_INPUT)

    dv_yn = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
    ws_r.add_data_validation(dv_yn)
    dv_yn.add(f"D{R_FIRST}:I{R_LAST}")

    ws_r.freeze_panes = f"A{R_FIRST}"
    ws_r.auto_filter.ref = f"A4:K{R_LAST}"
    ws_r.sheet_view.showGridLines = False

    # ==================== 回答与判定（人工判定入口） ====================
    # 为什么单独一张表：判「有没有幻觉」必须看到回答原文，而逐次记录里只放得下
    # 是/否。两条路都列在这里 —— 判定要点告诉你该看什么，回答全文让你能看。
    ws_a = wb.create_sheet("回答与判定")
    ws_a.merge_cells("A1:I1")
    c = ws_a["A1"]
    c.value = "Agent 效果评估 · 回答与判定（人工判这两列）"
    c.font = Font(name=FONT, size=15, bold=True, color=XL_TITLE)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws_a.row_dimensions[1].height = 30

    ws_a.merge_cells("A2:I2")
    c = ws_a["A2"]
    c.value = ("你只需要判 G「任务达成」与 H「出现幻觉」两列，判完「逐次记录」与「指标看板」自动更新。"
               "C 列是这道题的判定要点，照着它看即可。G 列已按标准答案预填了一部分，"
               "标「预填」的可复核，空的要你判；**H 列全部要你判**。")
    c.font = Font(name=FONT, size=9, color=xl_color("#595959"))
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws_a.row_dimensions[2].height = 26

    A_HEADERS = ["编号#轮次", "提问", "判定要点", "实际调用的工具", "工具返回（截选）",
                 "回答全文", "任务达成", "出现幻觉", "备注"]
    style_header(ws_a, 4, A_HEADERS,
                 {"A": 12, "B": 34, "C": 34, "D": 26, "E": 30, "F": 68,
                  "G": 10, "H": 10, "I": 18})

    def est_height(text, width_chars=34, cap=200):
        """按字数估算行高：openpyxl 不会自动撑开，不设的话长回答只能看到一行。"""
        if not text:
            return 16
        lines = 0
        for seg in str(text).split("\n"):
            lines += max(1, -(-len(seg) // width_chars))
        return min(15 * max(1, lines), cap)

    for i, (s, rnd) in enumerate(rows):
        r = R_FIRST + i
        rec = res.get((s["id"], rnd)) or {}
        j = rec.get("judge") or {}

        ws_a[f"A{r}"] = f"{s['id']}#{rnd}"
        ws_a[f"B{r}"] = " → ".join(s["turns"])
        ws_a[f"C{r}"] = s["judge"]

        # 实际调用：工具名 + 关键参数，一行一个，便于对照「参数陷阱」类
        if rec.get("tools"):
            ws_a[f"D{r}"] = _clean("\n".join(
                f"{t.get('name')}({json.dumps(t.get('args') or {}, ensure_ascii=False)[:90]})"
                for t in rec["tools"]))
        else:
            ws_a[f"D{r}"] = "未调用工具"

        if rec.get("results"):
            ws_a[f"E{r}"] = _clean("\n".join(str(x.get("content") or "")[:300] for x in rec["results"]))
        else:
            ws_a[f"E{r}"] = "—"

        answer = rec.get("answer") or ("（未跑 / 跑批失败）" if not rec else "（空回答）")
        ws_a[f"F{r}"] = _clean(answer)

        # 任务达成：机器判过的预填（备注标注），判不了的留空
        machine_task = j.get("task_ok")
        ws_a[f"G{r}"] = machine_task if machine_task in ("是", "否") else None
        ws_a[f"H{r}"] = None      # 幻觉：永远人工

        if rec.get("error"):
            ws_a[f"I{r}"] = f"跑批失败：{rec['error'][:50]}"
        elif machine_task in ("是", "否"):
            ws_a[f"I{r}"] = "任务达成已由机器预填（有标准答案），建议复核"
        else:
            ws_a[f"I{r}"] = "需人工判"

        for col in "ABCDEFGHI":
            cell = ws_a[f"{col}{r}"]
            cell.font = Font(name=FONT, size=10)
            cell.border = BOX
            if col in ("B", "C", "D", "E", "F", "I"):
                cell.alignment = Alignment(vertical="top", wrap_text=True, horizontal="left")
            else:
                cell.alignment = Alignment(horizontal="center", vertical="center")
            if col in ("G", "H"):
                cell.fill = PatternFill("solid", fgColor=XL_INPUT)
        ws_a.row_dimensions[r].height = est_height(answer)

    dv_a = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
    ws_a.add_data_validation(dv_a)
    dv_a.add(f"G{R_FIRST}:H{R_LAST}")

    ws_a.freeze_panes = f"C{R_FIRST}"
    ws_a.auto_filter.ref = f"A4:I{R_LAST}"
    ws_a.sheet_view.showGridLines = False

    # ==================== 指标看板 ====================
    ws_m = wb.create_sheet("指标看板", 0)
    ws_m.merge_cells("A1:F1")
    c = ws_m["A1"]
    c.value = "Agent 效果评估 · 指标看板"
    c.font = Font(name=FONT, size=16, bold=True, color=XL_TITLE)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws_m.row_dimensions[1].height = 34

    filled = sum(1 for (s, r) in rows if (s["id"], r) in res)
    judged = sum(1 for (s, r) in rows
                 if (res.get((s["id"], r), {}).get("judge") or {}).get("task_ok") in ("是", "否"))
    to_human = len(rows) - judged
    ws_m.merge_cells("A2:F2")
    c = ws_m["A2"]
    c.value = (f"题目 {len(SAMPLES)} 条 × {rounds} 轮 = {len(rows)} 次运行，已采集 {filled} 次；"
               f"其中机器已判「任务达成」{judged} 次，剩余 {to_human} 次要你在「回答与判定」里判"
               f"（幻觉列全部要在那里判，逐次记录只是引用）。"
               "指标全部由「逐次记录」自动汇总，不要手填；分母只算已判定的行，填一行就更新一次。")
    c.font = Font(name=FONT, size=9, color=xl_color("#595959"))
    c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws_m.row_dimensions[2].height = 28

    style_header(ws_m, 4, ["指标", "算法", "当前值", "目标", "判定", "它回答什么问题"],
                 {"A": 22, "B": 40, "C": 12, "D": 10, "E": 10, "F": 48})

    S3 = "'逐次记录'!"
    D = f"{S3}$D${R_FIRST}:$D${R_LAST}"
    E = f"{S3}$E${R_FIRST}:$E${R_LAST}"
    F = f"{S3}$F${R_FIRST}:$F${R_LAST}"
    G = f"{S3}$G${R_FIRST}:$G${R_LAST}"
    H = f"{S3}$H${R_FIRST}:$H${R_LAST}"
    I = f"{S3}$I${R_FIRST}:$I${R_LAST}"
    J = f"{S3}$J${R_FIRST}:$J${R_LAST}"
    B = f"{S3}$B${R_FIRST}:$B${R_LAST}"     # 样本编号#轮次，用于按轮次统计

    ALGO = {
        "已记录运行数": "已填「任务达成」的行数",
        "工具召回率": "工具调全=是 的行数 ÷ 已判定行数",
        "工具精确率": "1 − 有多余调用=是 的行数 ÷ 已判定行数",
        "参数正确率": "参数正确=是 的行数 ÷ 已判定行数",
        "执行成功率": "执行成功=是 的行数 ÷ 已判定行数",
        "任务达成率": "任务达成=是 的行数 ÷ 已判定行数",
        "首次完成率": "达成=是 且 迭代次数=1 的行数 ÷ 已判定行数",
        "幻觉率": "出现幻觉=是 的行数 ÷ 已判定行数",
        "平均迭代次数": "迭代次数列平均值",
    }

    METRICS = [
        # H 列是公式列：COUNTA 会把「公式返回空串」的单元格也算成非空，
        # 分母从 80 虚高到 150，达成率直接从 97.5% 掉到 52%（被实测抓到过）。
        # 所以统计口径一律用「是 + 否」，只数真正判过的。
        ("已记录运行数",
         f'=COUNTIF({H},"是")+COUNTIF({H},"否")', None, "0",
         "已判定的运行次数，其余指标以它为分母基础"),
        ("工具召回率", f'=IF(COUNTA({D})=0,"—",COUNTIF({D},"是")/COUNTA({D}))', 0.95, "0.0%",
         "该用的工具一个没漏的比例。漏调＝该查不查，常表现为凭记忆编答案"),
        ("工具精确率", f'=IF(COUNTA({E})=0,"—",1-COUNTIF({E},"是")/COUNTA({E}))', 0.90, "0.0%",
         "没有多余调用的比例。滥用工具更慢更贵，还会把无关信息带进回答"),
        ("参数正确率", f'=IF(COUNTA({F})=0,"—",COUNTIF({F},"是")/COUNTA({F}))', 0.95, "0.0%",
         "工具选对之后参数是否填对（参数陷阱类需人工判）"),
        ("执行成功率", f'=IF(COUNTA({G})=0,"—",COUNTIF({G},"是")/COUNTA({G}))', 0.90, "0.0%",
         "工具实际跑通的比例（排除环境问题）"),
        ("任务达成率",
         f'=IF((COUNTIF({H},"是")+COUNTIF({H},"否"))=0,"—",'
         f'COUNTIF({H},"是")/(COUNTIF({H},"是")+COUNTIF({H},"否")))', 0.85, "0.0%",
         "端到端达成用户目标的比例，最接近「用户觉得好用吗」"),
        # 按「第 1 轮」算，不能按「工具调用次数=1」——
        # 后者会把多工具任务（必然 >1 次）和不该调工具的题（0 次）全判成失败。
        ("首次完成率",
         # 分母写成「是 + 否」，不用 "<>" —— 后者在部分求值引擎里不认空值，
         # 会把分母算小，比率超过 100%（被实测抓到过一次 111.4%）
         f'=IF((COUNTIFS({B},"*#1",{H},"是")+COUNTIFS({B},"*#1",{H},"否"))=0,"—",'
         f'COUNTIFS({B},"*#1",{H},"是")/'
         f'(COUNTIFS({B},"*#1",{H},"是")+COUNTIFS({B},"*#1",{H},"否")))',
         0.70, "0.0%",
         "第 1 轮就达成的比例（分母只算第 1 轮有判定的行），用来看结果稳不稳"),
        ("幻觉率",
         f'=IF((COUNTIF({I},"是")+COUNTIF({I},"否"))=0,"—",'
         f'COUNTIF({I},"是")/(COUNTIF({I},"是")+COUNTIF({I},"否")))', 0.02, "0.0%",
         "编造事实/编造工具结果/伪造数据的比例，越低越好，超 2% 要逐个复查"),
        ("平均迭代次数", f'=IF(COUNTA({J})=0,"—",AVERAGE({J}))', 3, "0.00",
         "完成一个任务平均调几次工具，直接关系耗时与 token 成本"),
    ]

    M_FIRST = 5
    for i, (name, formula, target, numfmt, desc) in enumerate(METRICS):
        r = M_FIRST + i
        ws_m[f"A{r}"] = name
        ws_m[f"B{r}"] = ALGO[name]
        ws_m[f"C{r}"] = formula
        ws_m[f"C{r}"].number_format = numfmt
        if target is None:
            ws_m[f"D{r}"] = "—"
        else:
            ws_m[f"D{r}"] = target
            ws_m[f"D{r}"].number_format = numfmt
        ws_m[f"F{r}"] = desc

        for col in "ABCDEF":
            cell = ws_m[f"{col}{r}"]
            cell.font = Font(name=FONT, size=10, bold=(col == "A"))
            cell.border = BOX
            cell.alignment = (Alignment(vertical="center", wrap_text=True, horizontal="left")
                              if col in ("B", "F")
                              else Alignment(horizontal="center", vertical="center"))
        ws_m[f"C{r}"].font = Font(name=FONT, size=11, bold=True)
        ws_m[f"C{r}"].fill = PatternFill("solid", fgColor=XL_KPI_VALUE)

        if i == 0:
            ws_m[f"E{r}"] = "—"
        elif name in ("幻觉率", "平均迭代次数"):
            ws_m[f"E{r}"] = f'=IF(OR($C{r}="—",$C{r}=""),"—",IF($C{r}<=$D{r},"达标","未达标"))'
        else:
            ws_m[f"E{r}"] = f'=IF(OR($C{r}="—",$C{r}=""),"—",IF($C{r}>=$D{r},"达标","未达标"))'
        ws_m[f"E{r}"].font = Font(name=FONT, size=10, bold=True)
        ws_m[f"E{r}"].alignment = Alignment(horizontal="center", vertical="center")

    M_LAST = M_FIRST + len(METRICS) - 1
    jr = f"E{M_FIRST}:E{M_LAST}"
    ws_m.conditional_formatting.add(jr, CellIsRule(
        operator="equal", formula=['"达标"'],
        fill=PatternFill("solid", bgColor=XL_GREEN_BG),
        font=Font(color=XL_GREEN_FG, bold=True)))
    ws_m.conditional_formatting.add(jr, CellIsRule(
        operator="equal", formula=['"未达标"'],
        fill=PatternFill("solid", bgColor=XL_RED_BG),
        font=Font(color=XL_RED_FG, bold=True)))
    ws_m.conditional_formatting.add(f"C{M_FIRST + 7}", CellIsRule(
        operator="greaterThan", formula=["$D$" + str(M_FIRST + 7)],
        fill=PatternFill("solid", bgColor=XL_YELLOW_BG)))

    ws_m.freeze_panes = "A5"
    ws_m.sheet_view.showGridLines = False

    wb.save(str(OUT))
    print(f"已生成 {OUT}")
    print(f"样本 {len(SAMPLES)} 条 × {rounds} 轮 = {len(rows)} 行记录；已回填 {filled} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
