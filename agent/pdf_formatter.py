"""
PDF 文档格式化引擎

将结构化的排版描述（JSON）转换为 reportlab PDF 文档。
支持页面属性、字体嵌入、段落样式、列表、表格、页眉页脚、超链接、书签、图像。

设计原则：
- 所有尺寸单位统一使用厘米(cm)或磅(pt)，内部自动转换
- 中文字体自动查找系统字体并嵌入，有后备方案
- 未指定的属性使用合理默认值
- 使用 Platypus 流式构建，避免长文档内存爆炸
"""

import os
import re
import copy
import io
import tempfile

from reportlab.lib.pagesizes import A4, A3, A5, B5, LETTER, LEGAL
from reportlab.lib.units import cm, mm, inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.lib.colors import HexColor, black, white, Color
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, ListFlowable, ListItem, KeepTogether,
    Image as RLImage, Flowable
)
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.platypus.doctemplate import PageTemplate, BaseDocTemplate, NextPageTemplate
from reportlab.platypus.frames import Frame
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

try:
    from PIL import Image as PILImage
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

PAPER_SIZES = {
    "A4": A4,
    "A3": A3,
    "A5": A5,
    "B5": B5,
    "Letter": LETTER,
    "Legal": LEGAL,
}

PAPER_SIZE_NAMES = {
    "A4": (21.0, 29.7),
    "A3": (29.7, 42.0),
    "A5": (14.8, 21.0),
    "B5": (17.6, 25.0),
    "Letter": (21.59, 27.94),
    "Legal": (21.59, 35.56),
}

ALIGNMENT_MAP = {
    "left": TA_LEFT,
    "center": TA_CENTER,
    "right": TA_RIGHT,
    "justify": TA_JUSTIFY,
    "左对齐": TA_LEFT,
    "居中": TA_CENTER,
    "右对齐": TA_RIGHT,
    "两端对齐": TA_JUSTIFY,
}

FONT_FALLBACK_MAP = {
    "宋体": ["SimSun", "Songti SC", "STSong", "Noto Serif CJK SC", "AR PL UMing"],
    "simsun": ["SimSun", "Songti SC", "STSong", "Noto Serif CJK SC"],
    "黑体": ["SimHei", "Heiti SC", "STHeiti", "Noto Sans CJK SC", "WenQuanYi Micro Hei"],
    "simhei": ["SimHei", "Heiti SC", "STHeiti", "Noto Sans CJK SC"],
    "楷体": ["KaiTi", "Kaiti SC", "STKaiti", "AR PL UKai"],
    "kaiti": ["KaiTi", "Kaiti SC", "STKaiti", "AR PL UKai"],
    "仿宋": ["FangSong", "FangSong_GB2312", "STFangsong"],
    "fangsong": ["FangSong", "FangSong_GB2312", "STFangsong"],
    "微软雅黑": ["Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC"],
    "microsoft yahei": ["Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC"],
}

FONT_SEARCH_PATHS = [
    "/System/Library/Fonts/",
    "/Library/Fonts/",
    "/System/Library/Fonts/Supplemental/",
    os.path.expanduser("~/Library/Fonts/"),
    "C:/Windows/Fonts/",
    "/usr/share/fonts/",
    "/usr/local/share/fonts/",
    "/usr/share/fonts/truetype/",
    "/usr/share/fonts/opentype/",
    "/usr/share/fonts/truetype/droid/",
    "/usr/share/fonts/truetype/noto/",
    "/usr/share/fonts/truetype/wqy/",
    "/usr/share/fonts/truetype/arphic/",
    "/usr/share/fonts/truetype/liberation/",
    "/usr/share/fonts/truetype/dejavu/",
]

FONT_FILE_PATTERNS = {
    "SimSun": ["SimSun.ttf", "simsun.ttf", "SimSun.ttc", "Songti.ttc"],
    "Songti SC": ["Songti.ttc", "Songti SC.ttc"],
    "STSong": ["STSong.ttf", "STSong.ttc"],
    "SimHei": ["SimHei.ttf", "simhei.ttf", "SimHei.ttc"],
    "Heiti SC": ["Heiti.ttc", "Heiti SC.ttc"],
    "STHeiti": ["STHeiti.ttf", "STHeiti.ttc"],
    "KaiTi": ["KaiTi.ttf", "kaiti.ttf", "KaiTi.ttc"],
    "Kaiti SC": ["Kaiti.ttc", "Kaiti SC.ttc"],
    "STKaiti": ["STKaiti.ttf", "STKaiti.ttc"],
    "FangSong": ["FangSong.ttf", "FangSong_GB2312.ttf"],
    "STFangsong": ["STFangsong.ttf", "STFangsong.ttc"],
    "Microsoft YaHei": ["msyh.ttf", "msyh.ttc", "Microsoft YaHei.ttf"],
    "PingFang SC": ["PingFang.ttc", "PingFang SC.ttc"],
    "Hiragino Sans GB": ["Hiragino Sans GB.ttc", "Hiragino Sans GB W3.ttc"],
    "Arial Unicode": ["Arial Unicode.ttf", "ArialUnicodeMS.ttf"],
    "Noto Sans CJK SC": ["NotoSansCJKsc-Regular.otf", "NotoSansCJK-Regular.ttc"],
    "Noto Serif CJK SC": ["NotoSerifCJKsc-Regular.otf", "NotoSerifCJK-Regular.ttc"],
    "WenQuanYi Micro Hei": ["wqy-microhei.ttc", "wqy-microhei.ttf"],
    "WenQuanYi Zen Hei": ["wqy-zenhei.ttc", "wqy-zenhei.ttf"],
    "Droid Sans Fallback": ["DroidSansFallbackFull.ttf", "DroidSansFallback.ttf"],
    "AR PL UMing": ["uming.ttc", "AR-PL-UMing.ttf"],
    "AR PL UKai": ["ukai.ttc", "AR-PL-UKai.ttf"],
}

DEFAULT_FONT = {
    "name": "Helvetica",
    "size_pt": 11,
    "color": "#000000",
    "bold": False,
    "italic": False,
    "underline": False,
}

DEFAULT_PARAGRAPH = {
    "alignment": "left",
    "first_line_indent_cm": 0,
    "hanging_indent_cm": 0,
    "line_spacing": {"mode": "multiple", "value": 1.5},
    "space_before_pt": 0,
    "space_after_pt": 6,
    "keep_with_next": False,
    "page_break_before": False,
    "widow_control": True,
}

DEFAULT_PAGE = {
    "paper_size": "A4",
    "orientation": "portrait",
    "margin_top_cm": 2.54,
    "margin_bottom_cm": 2.54,
    "margin_left_cm": 2.54,
    "margin_right_cm": 2.54,
}

DEFAULT_TABLE = {
    "border_width": 0.5,
    "border_color": "#000000",
    "cell_padding_top": 3,
    "cell_padding_bottom": 3,
    "cell_padding_left": 6,
    "cell_padding_right": 6,
    "allow_split": True,
    "header_bg_color": "#4472C4",
    "header_font_color": "#FFFFFF",
    "header_font_bold": True,
    "stripe_bg_color": "#F2F2F2",
}

DEFAULT_HEADER_FOOTER = {
    "font_name": "Helvetica",
    "font_size": 9,
    "font_color": "#666666",
    "show_line": True,
    "line_color": "#CCCCCC",
    "line_width": 0.5,
}

_registered_fonts = {}
_available_font_name = "Helvetica"
_available_font_bold = "Helvetica-Bold"
_available_font_italic = "Helvetica-Oblique"
_available_font_bold_italic = "Helvetica-BoldOblique"


def _find_font_file(font_name):
    """在系统字体目录中查找字体文件"""
    patterns = FONT_FILE_PATTERNS.get(font_name, [font_name + ".ttf", font_name + ".ttc", font_name + ".otf"])
    for search_path in FONT_SEARCH_PATHS:
        if not os.path.isdir(search_path):
            continue
        for pattern in patterns:
            full_path = os.path.join(search_path, pattern)
            if os.path.isfile(full_path):
                return full_path
            try:
                for f in os.listdir(search_path):
                    if f.lower() == pattern.lower():
                        return os.path.join(search_path, f)
            except (PermissionError, OSError):
                pass
    return None


def _register_font(font_name, warnings):
    """注册字体，支持中文字体名映射"""
    global _available_font_name, _available_font_bold, _available_font_italic, _available_font_bold_italic

    if font_name in _registered_fonts:
        return _registered_fonts[font_name]

    search_names = [font_name]
    if font_name in FONT_FALLBACK_MAP:
        search_names.extend(FONT_FALLBACK_MAP[font_name])
    if font_name.lower() in FONT_FALLBACK_MAP:
        search_names.extend(FONT_FALLBACK_MAP[font_name.lower()])

    for name in search_names:
        if name in _registered_fonts:
            return _registered_fonts[name]

        font_path = _find_font_file(name)
        if font_path:
            try:
                pdfmetrics.registerFont(TTFont(name, font_path))
                _registered_fonts[name] = name
                _registered_fonts[font_name] = name
                return name
            except Exception as e:
                warnings.append(f"字体 '{name}' 注册失败: {str(e)}")

    warnings.append(f"未找到字体 '{font_name}'，使用默认字体 Helvetica")
    return "Helvetica"


def _init_fonts(warnings):
    """初始化中文字体"""
    global _available_font_name, _available_font_bold, _available_font_italic, _available_font_bold_italic

    chinese_fonts = [
        "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
        "SimSun", "SimHei", "KaiTi", "FangSong",
        "Songti SC", "Heiti SC", "Kaiti SC",
        "STSong", "STHeiti", "STKaiti", "STFangsong",
        "Noto Sans CJK SC", "Noto Serif CJK SC",
        "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
        "Droid Sans Fallback", "AR PL UMing", "AR PL UKai",
    ]

    for font_name in chinese_fonts:
        if font_name in _registered_fonts:
            continue
        font_path = _find_font_file(font_name)
        if font_path:
            try:
                pdfmetrics.registerFont(TTFont(font_name, font_path))
                _registered_fonts[font_name] = font_name
            except Exception:
                pass

    if "PingFang SC" in _registered_fonts:
        _available_font_name = "PingFang SC"
    elif "Hiragino Sans GB" in _registered_fonts:
        _available_font_name = "Hiragino Sans GB"
    elif "Microsoft YaHei" in _registered_fonts:
        _available_font_name = "Microsoft YaHei"
    elif "Noto Sans CJK SC" in _registered_fonts:
        _available_font_name = "Noto Sans CJK SC"
    elif "WenQuanYi Micro Hei" in _registered_fonts:
        _available_font_name = "WenQuanYi Micro Hei"
    elif "SimSun" in _registered_fonts:
        _available_font_name = "SimSun"
    elif "SimHei" in _registered_fonts:
        _available_font_name = "SimHei"
    elif "Droid Sans Fallback" in _registered_fonts:
        _available_font_name = "Droid Sans Fallback"

    _available_font_bold = _available_font_name
    _available_font_italic = _available_font_name
    _available_font_bold_italic = _available_font_name


def _parse_color(hex_color):
    """将 #RRGGBB 颜色转为 reportlab Color"""
    if not hex_color or not isinstance(hex_color, str):
        return black
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 6:
        try:
            return HexColor("#" + hex_color)
        except Exception:
            return black
    return black


def _parse_margin(value, default_unit="cm"):
    """解析边距值，支持 mm/cm/inch 单位

    Args:
        value: 数字(默认cm)或带单位的字符串如 "2cm", "20mm", "1inch"
        default_unit: 默认单位

    Returns:
        float: 厘米值
    """
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        value = value.strip().lower()
        match = re.match(r'^([\d.]+)\s*(cm|mm|inch|in|pt)?$', value)
        if match:
            num = float(match.group(1))
            unit = match.group(2) or default_unit
            if unit in ("mm",):
                return num / 10.0
            elif unit in ("inch", "in"):
                return num * 2.54
            elif unit in ("pt",):
                return num / 28.35
            else:
                return num

    return float(value) if isinstance(value, (int, float)) else 2.54


def _resolve_font_name(font_spec, default_name, warnings):
    """解析字体名称，支持中文字体映射"""
    if not font_spec or "name" not in font_spec:
        return default_name

    name = font_spec["name"]
    registered = _register_font(name, warnings)
    if registered == "Helvetica":
        return default_name
    return registered


def _build_paragraph_style(name, font_spec, para_spec, base_style=None, warnings=None):
    """构建 ParagraphStyle"""
    if warnings is None:
        warnings = []

    font = dict(DEFAULT_FONT)
    if font_spec:
        font.update({k: v for k, v in font_spec.items() if v is not None})

    para = dict(DEFAULT_PARAGRAPH)
    if para_spec:
        para.update({k: v for k, v in para_spec.items() if v is not None})

    font_name = _resolve_font_name(font_spec, _available_font_name, warnings)
    font_size = font.get("size_pt", 11)
    font_color = _parse_color(font.get("color"))

    alignment = ALIGNMENT_MAP.get(para.get("alignment"), TA_LEFT)

    line_spacing = para.get("line_spacing", {})
    if isinstance(line_spacing, dict):
        mode = line_spacing.get("mode", "multiple")
        value = line_spacing.get("value", 1.5)
        if mode == "fixed":
            leading = value
        else:
            leading = font_size * value
    else:
        leading = font_size * 1.5

    first_line_indent = para.get("first_line_indent_cm", 0) or 0
    hanging_indent = para.get("hanging_indent_cm", 0) or 0

    if first_line_indent > 0:
        left_indent = 0
        first_indent = first_line_indent * cm
    elif hanging_indent > 0:
        left_indent = hanging_indent * cm
        first_indent = -hanging_indent * cm
    else:
        left_indent = 0
        first_indent = 0

    style_kwargs = {
        "fontName": font_name,
        "fontSize": font_size,
        "textColor": font_color,
        "alignment": alignment,
        "leading": leading,
        "spaceBefore": para.get("space_before_pt", 0),
        "spaceAfter": para.get("space_after_pt", 6),
        "leftIndent": left_indent,
        "firstLineIndent": first_indent,
    }

    if font.get("bold"):
        style_kwargs["fontName"] = _available_font_bold
    if font.get("italic"):
        style_kwargs["fontName"] = _available_font_italic

    if base_style:
        return ParagraphStyle(name, parent=base_style, **style_kwargs)
    return ParagraphStyle(name, **style_kwargs)


def _build_table_style(table_spec, warnings):
    """构建 TableStyle 命令列表"""
    if warnings is None:
        warnings = []

    table_defaults = dict(DEFAULT_TABLE)
    if table_spec:
        table_defaults.update({k: v for k, v in table_spec.items() if v is not None})

    border_color = _parse_color(table_defaults["border_color"])
    border_width = table_defaults["border_width"]
    header_bg = _parse_color(table_defaults["header_bg_color"])
    header_font_color = _parse_color(table_defaults["header_font_color"])
    stripe_bg = _parse_color(table_defaults["stripe_bg_color"])

    commands = [
        ("GRID", (0, 0), (-1, -1), border_width, border_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), table_defaults["cell_padding_top"]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), table_defaults["cell_padding_bottom"]),
        ("LEFTPADDING", (0, 0), (-1, -1), table_defaults["cell_padding_left"]),
        ("RIGHTPADDING", (0, 0), (-1, -1), table_defaults["cell_padding_right"]),
    ]

    return commands, table_defaults


def _parse_unit_value(value, default_unit="cm"):
    """解析带单位的值，返回厘米"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _parse_margin(value, default_unit)
    return 0


class HeaderFooterCanvas:
    """页眉页脚绘制 Canvas 混入类"""

    def __init__(self, header_spec, footer_spec, page_spec, warnings):
        self.header_spec = header_spec or {}
        self.footer_spec = footer_spec or {}
        self.page_spec = page_spec or {}
        self.warnings = warnings or []

        hf_defaults = dict(DEFAULT_HEADER_FOOTER)
        self.header_font = _resolve_font_name(
            self.header_spec.get("font"), hf_defaults["font_name"], warnings
        )
        self.footer_font = _resolve_font_name(
            self.footer_spec.get("font"), hf_defaults["font_name"], warnings
        )
        self.header_size = self.header_spec.get("font_size", hf_defaults["font_size"])
        self.footer_size = self.footer_spec.get("font_size", hf_defaults["font_size"])
        self.header_color = _parse_color(self.header_spec.get("font_color", hf_defaults["font_color"]))
        self.footer_color = _parse_color(self.footer_spec.get("font_color", hf_defaults["font_color"]))
        self.show_header_line = self.header_spec.get("show_line", hf_defaults["show_line"])
        self.show_footer_line = self.footer_spec.get("show_line", hf_defaults["show_line"])
        self.line_color = _parse_color(hf_defaults["line_color"])
        self.line_width = hf_defaults["line_width"]

        self.header_text = self.header_spec.get("text", "")
        self.header_align = ALIGNMENT_MAP.get(self.header_spec.get("alignment", "center"), TA_CENTER)
        self.footer_text = self.footer_spec.get("text", "")
        self.footer_align = ALIGNMENT_MAP.get(self.footer_spec.get("alignment", "center"), TA_CENTER)

        self.page_num_format = self.footer_spec.get("page_num_format", "page_only")

        margin_top = _parse_margin(page_spec.get("margin_top_cm", 2.54))
        margin_bottom = _parse_margin(page_spec.get("margin_bottom_cm", 2.54))
        self.header_y = page_spec.get("_page_height_cm", 29.7) - margin_top / 2
        self.footer_y = margin_bottom / 2

    def draw_header(self, canvas_obj, doc):
        """绘制页眉"""
        canvas_obj.saveState()
        canvas_obj.setFont(self.header_font, self.header_size)
        canvas_obj.setFillColor(self.header_color)

        page_width = doc.pagesize[0]
        margin_left = _parse_margin(self.page_spec.get("margin_left_cm", 2.54)) * cm
        margin_right = _parse_margin(self.page_spec.get("margin_right_cm", 2.54)) * cm
        margin_top = _parse_margin(self.page_spec.get("margin_top_cm", 2.54)) * cm

        header_y = doc.pagesize[1] - margin_top + 0.5 * cm

        text = self.header_text
        if text:
            if self.header_align == TA_CENTER:
                canvas_obj.drawCentredString(page_width / 2, header_y, text)
            elif self.header_align == TA_RIGHT:
                canvas_obj.drawRightString(page_width - margin_right, header_y, text)
            else:
                canvas_obj.drawString(margin_left, header_y, text)

        if self.show_header_line:
            canvas_obj.setStrokeColor(self.line_color)
            canvas_obj.setLineWidth(self.line_width)
            line_y = doc.pagesize[1] - margin_top + 0.2 * cm
            canvas_obj.line(margin_left, line_y, page_width - margin_right, line_y)

        canvas_obj.restoreState()

    def draw_footer(self, canvas_obj, doc):
        """绘制页脚"""
        canvas_obj.saveState()
        canvas_obj.setFont(self.footer_font, self.footer_size)
        canvas_obj.setFillColor(self.footer_color)

        page_width = doc.pagesize[0]
        margin_left = _parse_margin(self.page_spec.get("margin_left_cm", 2.54)) * cm
        margin_right = _parse_margin(self.page_spec.get("margin_right_cm", 2.54)) * cm
        margin_bottom = _parse_margin(self.page_spec.get("margin_bottom_cm", 2.54)) * cm

        footer_y = margin_bottom - 0.8 * cm

        page_num = canvas_obj.getPageNumber()

        if self.show_footer_line:
            canvas_obj.setStrokeColor(self.line_color)
            canvas_obj.setLineWidth(self.line_width)
            line_y = margin_bottom - 0.3 * cm
            canvas_obj.line(margin_left, line_y, page_width - margin_right, line_y)

        text = self.footer_text
        if self.page_num_format == "page_of_total":
            page_text = f"第 {page_num} 页 / 共 {doc.page} 页"
        elif self.page_num_format == "page_x_of_y":
            page_text = f"Page {page_num} of {doc.page}"
        elif self.page_num_format == "page_only":
            page_text = str(page_num)
        elif self.page_num_format == "none":
            page_text = ""
        else:
            page_text = str(page_num)

        if text and page_text:
            combined = f"{text}    {page_text}"
        elif text:
            combined = text
        else:
            combined = page_text

        if combined:
            if self.footer_align == TA_CENTER:
                canvas_obj.drawCentredString(page_width / 2, footer_y, combined)
            elif self.footer_align == TA_RIGHT:
                canvas_obj.drawRightString(page_width - margin_right, footer_y, combined)
            else:
                canvas_obj.drawString(margin_left, footer_y, combined)

        canvas_obj.restoreState()


class PDFDocTemplate(BaseDocTemplate):
    """支持页眉页脚和书签的文档模板"""

    def __init__(self, filename, page_spec, header_spec, footer_spec, warnings, **kwargs):
        self.page_spec = page_spec
        self.header_canvas = HeaderFooterCanvas(header_spec, footer_spec, page_spec, warnings)
        self.warnings = warnings
        self.bookmarks = []

        BaseDocTemplate.__init__(self, filename, **kwargs)

        margin_top = _parse_margin(page_spec.get("margin_top_cm", 2.54)) * cm
        margin_bottom = _parse_margin(page_spec.get("margin_bottom_cm", 2.54)) * cm
        margin_left = _parse_margin(page_spec.get("margin_left_cm", 2.54)) * cm
        margin_right = _parse_margin(page_spec.get("margin_right_cm", 2.54)) * cm

        page_w, page_h = self.pagesize
        frame = Frame(
            margin_left, margin_bottom,
            page_w - margin_left - margin_right,
            page_h - margin_top - margin_bottom,
            id='main'
        )
        self.addPageTemplates([PageTemplate(id='main', frames=frame,
                                            onPage=self._on_page,
                                            onPageEnd=self._on_page_end)])

    def _on_page(self, canvas_obj, doc):
        self.header_canvas.draw_header(canvas_obj, doc)

    def _on_page_end(self, canvas_obj, doc):
        self.header_canvas.draw_footer(canvas_obj, doc)

    def afterFlowable(self, flowable):
        if hasattr(flowable, '_bookmark_name') and flowable._bookmark_name:
            key = flowable._bookmark_name
            page_num = self.page
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable._bookmark_title or key, key, level=flowable._bookmark_level or 0)
        BaseDocTemplate.afterFlowable(self, flowable)


class BookmarkParagraph(Paragraph):
    """带书签的段落"""

    def __init__(self, text, style, bookmark_name=None, bookmark_title=None, bookmark_level=0):
        super().__init__(text, style)
        self._bookmark_name = bookmark_name
        self._bookmark_title = bookmark_title
        self._bookmark_level = bookmark_level


class HyperlinkParagraph(Paragraph):
    """带超链接的段落"""

    def __init__(self, text, style, url=None, internal_target=None):
        if url:
            linked_text = f'<a href="{url}" color="blue"><u>{text}</u></a>'
        elif internal_target:
            linked_text = f'<a href="#{internal_target}" color="blue"><u>{text}</u></a>'
        else:
            linked_text = text
        super().__init__(linked_text, style)


def _estimate_paragraph_lines(text, font_size, available_width):
    """估算段落行数，用于孤行控制"""
    if not text:
        return 1
    avg_char_width = font_size * 0.5
    chars_per_line = max(1, int(available_width / avg_char_width))
    text_len = len(text)
    return max(1, (text_len + chars_per_line - 1) // chars_per_line)


def _add_toc(story, toc_spec, warnings):
    """添加目录页

    目录通过收集文档中的书签(bookmark)来生成。
    需要在构建文档时两遍处理：第一遍收集书签，第二遍生成目录。
    这里采用简化方案：从 toc_spec 中直接提供目录项。
    """
    entries = toc_spec.get("entries", [])
    if not entries:
        story.append(Paragraph("（目录）", ParagraphStyle(
            "TOCPlaceholder",
            fontName=_available_font_name,
            fontSize=14,
            alignment=TA_CENTER,
            spaceAfter=12,
        )))
        story.append(Paragraph("（请在元素中为各级标题添加 bookmark 以自动生成目录）", ParagraphStyle(
            "TOCNote",
            fontName=_available_font_name,
            fontSize=10,
            textColor=_parse_color("#999999"),
            alignment=TA_CENTER,
            spaceAfter=12,
        )))
        story.append(Spacer(1, 0.5 * cm))
        return

    title_text = toc_spec.get("title", "目录")
    title_style = ParagraphStyle(
        "TOCTitle",
        fontName=_available_font_name,
        fontSize=18,
        alignment=TA_CENTER,
        spaceAfter=16,
    )
    story.append(Paragraph(title_text, title_style))

    for entry in entries:
        level = entry.get("level", 0)
        text = entry.get("text", "")
        page = entry.get("page", "")
        indent = level * 1.5

        entry_style = ParagraphStyle(
            f"TOC_Level{level}",
            fontName=_available_font_name,
            fontSize=12 - level,
            leftIndent=indent * cm,
            leading=18,
            spaceAfter=2,
        )

        dots = "." * max(2, 50 - len(text) - len(str(page)))
        entry_text = f"{text} {dots} {page}"
        story.append(Paragraph(entry_text, entry_style))

    story.append(PageBreak())


def _add_paragraph(story, para_spec, style, warnings):
    """添加段落到故事列表"""
    text = para_spec.get("text", "")
    bookmark = para_spec.get("bookmark")
    hyperlink = para_spec.get("hyperlink")
    keep_with_next = para_spec.get("keep_with_next", False)
    page_break_before = para_spec.get("page_break_before", False)
    widow_control = para_spec.get("widow_control", True)

    if page_break_before:
        story.append(PageBreak())

    if bookmark:
        p = BookmarkParagraph(
            text, style,
            bookmark_name=bookmark.get("name", ""),
            bookmark_title=bookmark.get("title", ""),
            bookmark_level=bookmark.get("level", 0),
        )
    elif hyperlink:
        p = HyperlinkParagraph(
            text, style,
            url=hyperlink.get("url"),
            internal_target=hyperlink.get("internal_target"),
        )
    else:
        p = Paragraph(text, style)

    if keep_with_next:
        story.append(KeepTogether([p]))
    elif widow_control and text:
        font_size = style.fontSize if hasattr(style, 'fontSize') else 11
        available_width = 14.0
        est_lines = _estimate_paragraph_lines(text, font_size, available_width)
        if est_lines <= 2:
            story.append(KeepTogether([p]))
        else:
            story.append(p)
    else:
        story.append(p)


def _add_table(story, table_spec, base_style, warnings):
    """添加表格到故事列表"""
    headers = table_spec.get("headers", [])
    rows = table_spec.get("rows", [])
    if not headers and not rows:
        warnings.append("表格缺少数据")
        return

    table_data = []
    if headers:
        table_data.append([Paragraph(h, base_style) for h in headers])

    for row in rows:
        table_data.append([Paragraph(str(c), base_style) for c in row])

    col_widths = table_spec.get("col_widths")
    table = Table(table_data, colWidths=col_widths, repeatRows=1 if headers else 0)

    style_commands, tbl_defaults = _build_table_style(table_spec, warnings)

    header_bg = _parse_color(tbl_defaults["header_bg_color"])
    header_font_color = _parse_color(tbl_defaults["header_font_color"])
    stripe_bg = _parse_color(tbl_defaults["stripe_bg_color"])

    if headers:
        style_commands.extend([
            ("BACKGROUND", (0, 0), (-1, 0), header_bg),
            ("TEXTCOLOR", (0, 0), (-1, 0), header_font_color),
        ])

    if tbl_defaults.get("stripe_rows") and len(table_data) > 1:
        for i in range(1, len(table_data)):
            if i % 2 == 0:
                style_commands.append(("BACKGROUND", (0, i), (-1, i), stripe_bg))

    allow_split = tbl_defaults.get("allow_split", True)
    if not allow_split and len(table_data) > 1:
        table = KeepTogether(table)

    table.setStyle(TableStyle(style_commands))
    story.append(table)


BULLET_TYPE_MAP = {
    "bullet": "bullet",
    "disc": "bullet",
    "circle": "bullet",
    "square": "bullet",
    "ordered": "1",
    "decimal": "1",
    "number": "1",
    "upper_letter": "A",
    "lower_letter": "a",
    "upper_roman": "I",
    "lower_roman": "i",
}


def _add_list(story, list_spec, style, warnings):
    """添加列表到故事列表"""
    items = list_spec.get("items", [])
    list_type = list_spec.get("list_type", "bullet")

    bullet_type = BULLET_TYPE_MAP.get(list_type, "bullet")
    is_ordered = bullet_type != "bullet"

    flowable_items = []
    for item in items:
        if isinstance(item, str):
            flowable_items.append(ListItem(Paragraph(item, style)))
        elif isinstance(item, dict):
            text = item.get("text", "")
            level = item.get("level", 0)
            flowable_items.append(ListItem(Paragraph(text, style), leftIndent=level * 20))

    if flowable_items:
        list_kwargs = {
            "bulletType": bullet_type,
        }
        if is_ordered:
            list_kwargs["start"] = list_spec.get("start", 1)

        list_flowable = ListFlowable(flowable_items, **list_kwargs)
        story.append(list_flowable)


def _preprocess_image(image_path, quality=85, handle_transparency=True, max_width_px=None, max_height_px=None):
    """预处理图片：压缩、透明背景处理、尺寸限制

    Args:
        image_path: 图片文件路径
        quality: JPEG 压缩质量 (1-100)，仅对 JPEG 有效
        handle_transparency: 是否将透明背景转为白色
        max_width_px: 最大宽度（像素），超过则等比缩放
        max_height_px: 最大高度（像素），超过则等比缩放

    Returns:
        str: 处理后的图片路径（可能是临时文件），如果 PIL 不可用则返回原路径
    """
    if not _HAS_PIL:
        return image_path

    try:
        img = PILImage.open(image_path)
        original_mode = img.mode

        if handle_transparency and img.mode in ("RGBA", "LA", "P"):
            if img.mode == "P":
                img = img.convert("RGBA")
            background = PILImage.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.split()[3])
            else:
                background.paste(img)
            img = background

        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        if max_width_px or max_height_px:
            w, h = img.size
            ratio = 1.0
            if max_width_px and w > max_width_px:
                ratio = min(ratio, max_width_px / w)
            if max_height_px and h > max_height_px:
                ratio = min(ratio, max_height_px / h)
            if ratio < 1.0:
                new_w = int(w * ratio)
                new_h = int(h * ratio)
                img = img.resize((new_w, new_h), PILImage.LANCZOS)

        ext = os.path.splitext(image_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            save_format = "JPEG"
            save_kwargs = {"quality": quality, "optimize": True}
        elif ext == ".png":
            save_format = "PNG"
            save_kwargs = {"optimize": True}
        elif ext == ".webp":
            save_format = "WEBP"
            save_kwargs = {"quality": quality}
        else:
            save_format = "PNG"
            save_kwargs = {"optimize": True}

        tmp = tempfile.NamedTemporaryFile(suffix=f".{save_format.lower()}", delete=False)
        tmp_path = tmp.name
        tmp.close()

        img.save(tmp_path, format=save_format, **save_kwargs)
        return tmp_path

    except Exception:
        return image_path


def _add_image(story, image_spec, warnings):
    """添加图片到故事列表"""
    path = image_spec.get("path", "")
    if not path or not os.path.exists(path):
        warnings.append(f"图片文件不存在: {path}")
        return

    try:
        width_cm = image_spec.get("width_cm")
        height_cm = image_spec.get("height_cm")
        quality = image_spec.get("quality", 85)
        handle_transparency = image_spec.get("handle_transparency", True)
        max_width_px = image_spec.get("max_width_px")
        max_height_px = image_spec.get("max_height_px")

        processed_path = _preprocess_image(
            path,
            quality=quality,
            handle_transparency=handle_transparency,
            max_width_px=max_width_px,
            max_height_px=max_height_px,
        )

        kwargs = {}
        if width_cm:
            kwargs["width"] = width_cm * cm
        if height_cm:
            kwargs["height"] = height_cm * cm

        img = RLImage(processed_path, **kwargs)

        alignment = image_spec.get("alignment", "center")
        if alignment in ("center", "居中"):
            story.append(Spacer(1, 0.2 * cm))
            story.append(img)
            story.append(Spacer(1, 0.2 * cm))
        else:
            story.append(img)

        caption = image_spec.get("caption")
        if caption:
            caption_style = ParagraphStyle(
                "ImageCaption",
                fontName=_available_font_name,
                fontSize=9,
                textColor=_parse_color("#666666"),
                alignment=TA_CENTER,
                spaceAfter=6,
            )
            story.append(Paragraph(caption, caption_style))

    except Exception as e:
        warnings.append(f"插入图片失败: {str(e)}")


def _add_spacer(story, spacer_spec):
    """添加间距"""
    height_cm = spacer_spec.get("height_cm", 0.5)
    story.append(Spacer(1, height_cm * cm))


def create_pdf_document(filename, content=None, formatting=None, output_dir=None):
    """创建格式化 PDF 文档

    Args:
        filename: 文件名（不含扩展名）
        content: 简单模式的文本内容（formatting 为 None 时使用，向后兼容）
        formatting: 排版描述 JSON 对象

    Returns:
        str: 结果消息，包含文件路径和可能的警告
    """
    if output_dir is None:
        output_dir = os.path.join("document_output", "pdf_output")
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{filename}.pdf")

    warnings = []

    _init_fonts(warnings)

    if formatting and isinstance(formatting, dict):
        return _build_formatted_pdf(filepath, filename, formatting, warnings)
    else:
        return _build_simple_pdf(filepath, filename, content or "", warnings)


def _build_formatted_pdf(filepath, filename, formatting, warnings):
    """构建排版模式 PDF"""
    page_spec = dict(DEFAULT_PAGE)
    user_page = formatting.get("page", {})
    if user_page:
        page_spec.update({k: v for k, v in user_page.items() if v is not None})

    paper_size = PAPER_SIZES.get(page_spec["paper_size"], A4)
    orientation = page_spec.get("orientation", "portrait")

    if orientation == "landscape":
        paper_size = (paper_size[1], paper_size[0])

    page_spec["_page_width_cm"] = paper_size[0] / cm
    page_spec["_page_height_cm"] = paper_size[1] / cm

    margin_top = _parse_margin(page_spec.get("margin_top_cm", 2.54)) * cm
    margin_bottom = _parse_margin(page_spec.get("margin_bottom_cm", 2.54)) * cm
    margin_left = _parse_margin(page_spec.get("margin_left_cm", 2.54)) * cm
    margin_right = _parse_margin(page_spec.get("margin_right_cm", 2.54)) * cm

    header_spec = formatting.get("header", {})
    footer_spec = formatting.get("footer", {})

    doc = PDFDocTemplate(
        filepath,
        page_spec,
        header_spec,
        footer_spec,
        warnings,
        pagesize=paper_size,
        title=formatting.get("title", filename),
        author=formatting.get("author", ""),
        subject=formatting.get("subject", ""),
    )

    default_font_spec = formatting.get("default_font", {})
    default_para_spec = formatting.get("default_paragraph", {})

    resolved_font = _resolve_font_name(default_font_spec, _available_font_name, warnings)

    base_style = ParagraphStyle(
        "BaseStyle",
        fontName=resolved_font,
        fontSize=default_font_spec.get("size_pt", 11),
        textColor=_parse_color(default_font_spec.get("color", "#000000")),
        alignment=ALIGNMENT_MAP.get(default_para_spec.get("alignment", "left"), TA_LEFT),
        leading=default_font_spec.get("size_pt", 11) * 1.5,
        spaceBefore=default_para_spec.get("space_before_pt", 0),
        spaceAfter=default_para_spec.get("space_after_pt", 6),
    )

    heading1_style = _build_paragraph_style(
        "Heading1",
        formatting.get("heading1_font", {"size_pt": 22, "bold": True}),
        formatting.get("heading1_paragraph", {"space_before_pt": 18, "space_after_pt": 12}),
        base_style,
        warnings,
    )

    heading2_style = _build_paragraph_style(
        "Heading2",
        formatting.get("heading2_font", {"size_pt": 16, "bold": True}),
        formatting.get("heading2_paragraph", {"space_before_pt": 14, "space_after_pt": 8}),
        base_style,
        warnings,
    )

    heading3_style = _build_paragraph_style(
        "Heading3",
        formatting.get("heading3_font", {"size_pt": 13, "bold": True}),
        formatting.get("heading3_paragraph", {"space_before_pt": 10, "space_after_pt": 6}),
        base_style,
        warnings,
    )

    body_style = _build_paragraph_style(
        "Body",
        default_font_spec,
        default_para_spec,
        base_style,
        warnings,
    )

    story = []

    title = formatting.get("title")
    if title:
        title_style = _build_paragraph_style(
            "DocTitle",
            formatting.get("title_font", {"size_pt": 26, "bold": True}),
            {"alignment": "center", "space_before_pt": 0, "space_after_pt": 20},
            base_style,
            warnings,
        )
        story.append(Paragraph(title, title_style))

    elements = formatting.get("elements", [])
    if not elements:
        warnings.append("未指定内容元素(elements)，生成了空白文档")

    for elem in elements:
        elem_type = elem.get("type", "paragraph")

        if elem_type == "paragraph":
            level = elem.get("level", "body")
            if level == "heading1":
                style = heading1_style
            elif level == "heading2":
                style = heading2_style
            elif level == "heading3":
                style = heading3_style
            else:
                elem_font = elem.get("font", {})
                elem_para = elem.get("paragraph", {})
                if elem_font or elem_para:
                    merged_font = dict(default_font_spec)
                    merged_font.update(elem_font)
                    merged_para = dict(default_para_spec)
                    merged_para.update(elem_para)
                    style = _build_paragraph_style(
                        f"Custom_{id(elem)}", merged_font, merged_para, base_style, warnings
                    )
                else:
                    style = body_style
            _add_paragraph(story, elem, style, warnings)

        elif elem_type == "table":
            table_style = _build_paragraph_style(
                f"Table_{id(elem)}",
                elem.get("font", {"size_pt": 10}),
                {},
                base_style,
                warnings,
            )
            _add_table(story, elem, table_style, warnings)

        elif elem_type == "list":
            list_style = _build_paragraph_style(
                f"List_{id(elem)}",
                elem.get("font", {}),
                elem.get("paragraph", {}),
                base_style,
                warnings,
            )
            _add_list(story, elem, list_style, warnings)

        elif elem_type == "image":
            _add_image(story, elem, warnings)

        elif elem_type == "spacer":
            _add_spacer(story, elem)

        elif elem_type == "page_break":
            story.append(PageBreak())

        elif elem_type == "table_of_contents":
            _add_toc(story, elem, warnings)

        else:
            warnings.append(f"未知的元素类型 '{elem_type}'，已跳过")

    try:
        doc.build(story)
    except Exception as e:
        warnings.append(f"PDF 构建失败: {str(e)}")
        return f"PDF 构建失败: {str(e)}"

    result = f"PDF文档已成功保存至: {filepath}"
    if warnings:
        result += "\n[提示] " + "; ".join(warnings)
    return result


def _build_simple_pdf(filepath, filename, content, warnings):
    """构建简单模式 PDF（向后兼容）"""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    doc = SimpleDocTemplate(
        filepath, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm
    )

    title_style = ParagraphStyle(
        'CustomTitle',
        fontName=_available_font_name,
        fontSize=18,
        spaceAfter=12,
    )
    body_style = ParagraphStyle(
        'CustomBody',
        fontName=_available_font_name,
        fontSize=11,
        leading=18,
    )

    story = []
    story.append(Paragraph(filename, title_style))
    story.append(Spacer(1, 0.5 * cm))

    for para in [p.strip() for p in content.replace("\r", "").split("\n") if p.strip()]:
        story.append(Paragraph(para, body_style))
        story.append(Spacer(1, 0.2 * cm))

    doc.build(story)

    result = f"PDF文档已成功保存至: {filepath}"
    if warnings:
        result += "\n[提示] " + "; ".join(warnings)
    return result