#!/usr/bin/env python3
"""图片（多模态）支持改造的验证脚本。

覆盖本次改造新增的纯逻辑，不依赖网络与真实模型：
  ① 模型视觉能力判定（supports_vision）
  ② file_parser 的图片识别 / 摘要 / 多模态拆分（build_context_parts）
  ③ chat.py 的 _content_to_text（多模态 content 落库时不能压成字符串）

用法：
  python3 scripts/test_image_support.py
"""
import importlib.util
import os
import re
import sys
import tempfile

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
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- ① 视觉能力判定 ----------
print("\n① 模型视觉能力判定（agent/model_gateway.py）")
gw = _load("mgw", os.path.join(ROOT, "agent", "model_gateway.py"))

vision_cases = [
    ("deepseek-v4-flash", True),      # 官方已路由到 V4.1 Flash（原生多模态）
    ("deepseek-v4-pro", True),        # 同上
    ("deepseek-flash", True),         # 规范 ID
    ("deepseek-reasoner", False),     # 纯文本推理模型
    ("deepseek-chat", False),         # 未知 deepseek 走安全降级
    ("gpt-5-mini", True),
    ("gpt-4o", True),
    ("claude-sonnet-4-5", True),
    ("qwen2.5-vl-72b-instruct", True),
    ("qwen-max", False),              # 无 vl 标记 → 降级
    ("", False),
    ("totally-unknown-model", False), # 未知模型一律不支持（安全降级）
]
for model, expected in vision_cases:
    got = gw.supports_vision(model)
    check(f"supports_vision({model!r}) == {expected}", got == expected, f"实际={got}")

# ModelGateway 属性也要能读到
check("ModelGateway('deepseek-v4-flash').supports_vision 为真",
      gw.ModelGateway("deepseek-v4-flash").supports_vision is True)

# ---------- ② file_parser 图片处理 ----------
print("\n② 图片解析与多模态拆分（agent/file_parser.py）")
fp = _load("fparser", os.path.join(ROOT, "agent", "file_parser.py"))

check("is_image_file('a.png')", fp.is_image_file("a.png") is True)
check("is_image_file('a.txt')", fp.is_image_file("a.txt") is False)
check("image_mime_type('a.jpg') == image/jpeg", fp.image_mime_type("a.jpg") == "image/jpeg")
check("get_file_type('x.PNG') == image", fp.get_file_type("x.PNG") == "image")
check("图片摘要不含误导性的 '(xB, 1行)'",
      fp.generate_file_summary("", "photo.png") == "photo.png (图片文件)")

tmp = tempfile.mkdtemp(prefix="imgtest_")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# 造 3 张「图片」与 1 个文本文件
img_paths = []
for i in range(3):
    p = os.path.join(tmp, f"pic{i}.png")
    with open(p, "wb") as f:
        f.write(PNG_MAGIC + b"0" * (100 * (i + 1)))
    img_paths.append(p)
txt_path = os.path.join(tmp, "note.txt")
with open(txt_path, "w", encoding="utf-8") as f:
    f.write("这是一份说明文档\n第二行")

parsed = fp.parse_files(img_paths + [txt_path])
check("parse_file 对图片返回 file_type=image",
      sum(1 for p in parsed if p["file_type"] == "image") == 3)
check("图片 parse_file 带出磁盘 path（供后续 base64 读取）",
      all(p.get("path") for p in parsed if p["file_type"] == "image"))
check("文本文件仍按文本解析",
      any(p["file_type"] == "text" and "说明文档" in p["full_content"] for p in parsed))

meta = {os.path.basename(p): os.path.getmtime(p) for p in img_paths + [txt_path]}
parts = fp.build_context_parts(parsed, meta)
check("build_context_parts 拆出 3 张图片", len(parts["images"]) == 3, f"实际={len(parts['images'])}")
check("文本内容进入 text 字段", "说明文档" in parts["text"])
check("图片占位文本不混入 text（二进制不进提示词）",
      "图片文件" not in parts["text"])
check("图片元信息含 mime/path/size",
      all({"mime", "path", "size"} <= set(im) for im in parts["images"]))
check("图片按上传时间倒序（最新优先）",
      [im["filename"] for im in parts["images"]] == ["pic2.png", "pic1.png", "pic0.png"],
      f"实际={[im['filename'] for im in parts['images']]}")

# 数量上限：造 7 张图，应只留 5 张并报告跳过 2 张
many = []
for i in range(7):
    p = os.path.join(tmp, f"m{i}.png")
    with open(p, "wb") as f:
        f.write(PNG_MAGIC + b"0" * 10)
    many.append(p)
parts_many = fp.build_context_parts(fp.parse_files(many), {os.path.basename(p): 0 for p in many})
check("图片数量超限时截断到 5 张", len(parts_many["images"]) == 5, f"实际={len(parts_many['images'])}")
check("被截断的图片计入 skipped（用于提示用户）", parts_many["skipped"] == 2, f"实际={parts_many['skipped']}")

# 体积上限：单图超限应被跳过
big = os.path.join(tmp, "big.png")
with open(big, "wb") as f:
    f.write(PNG_MAGIC + b"0" * (1024 * 1024))  # 1MB
parts_big = fp.build_context_parts(fp.parse_files([big]),
                                   {os.path.basename(big): 0}, max_total_bytes=1024)
check("超出体积上限的图片被跳过而不是撑爆请求体",
      len(parts_big["images"]) == 0 and parts_big["skipped"] == 1)

# 磁盘文件已不存在（如被清理）不应报错
gone = os.path.join(tmp, "gone.png")
with open(gone, "wb") as f:
    f.write(PNG_MAGIC + b"0" * 10)
pg = fp.parse_file(gone)
os.remove(gone)
parts_gone = fp.build_context_parts([pg], {"gone.png": 0})
check("图片已被删除时静默跳过（不崩溃、计入 skipped）",
      len(parts_gone["images"]) == 0 and parts_gone["skipped"] == 1)

# ---------- ③ 多模态 content 落库脱敏 ----------
print("\n③ 多模态 content 落库（server/routes/chat.py::_content_to_text）")
chat_src = open(os.path.join(ROOT, "server", "routes", "chat.py"), encoding="utf-8").read()
m = re.search(r"^def _content_to_text\(.*?(?=^\ndef |\Z)", chat_src, re.S | re.M)
check("能定位到 _content_to_text 源码", m is not None)
ns = {}
exec(m.group(0), ns)  # 该函数为纯函数，无外部依赖，可独立执行
_content_to_text = ns["_content_to_text"]

check("纯字符串原样返回", _content_to_text("你好") == "你好")
mixed = [
    {"type": "text", "text": "这张图里有什么？"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAAAAAAAA"}},
]
out = _content_to_text(mixed)
check("多模态 content 只保留文本块", out == "这张图里有什么？", f"实际={out!r}")
check("图片 base64 不会落入会话历史（守数据份数）", "base64" not in out and "AAAA" not in out)
check("与旧行为对比：不再把整个列表 str() 化", not out.startswith("["))

# ---------- ④ 上传安全：不信任扩展名，按魔数校验 ----------
print("\n④ 上传安全（server/routes/upload.py::_is_valid_image）")
up_src = open(os.path.join(ROOT, "server", "routes", "upload.py"), encoding="utf-8").read()
m2 = re.search(r"^def _is_valid_image\(.*?(?=^def |\Z)", up_src, re.S | re.M)
check("能定位到 _is_valid_image 源码", m2 is not None)
ns2 = {}
exec(m2.group(0), ns2)
_is_valid_image = ns2["_is_valid_image"]

check("真实 PNG 通过", _is_valid_image(b"\x89PNG\r\n\x1a\n" + b"0" * 32, ".png") is True)
check("真实 JPEG 通过", _is_valid_image(b"\xff\xd8\xff\xe0" + b"0" * 32, ".jpg") is True)
check("真实 GIF 通过", _is_valid_image(b"GIF89a" + b"0" * 32, ".gif") is True)
check("真实 WEBP 通过", _is_valid_image(b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 32, ".webp") is True)
check("真实 BMP 通过", _is_valid_image(b"BM" + b"0" * 32, ".bmp") is True)
check("脚本伪装成 .png 被拒绝", _is_valid_image(b"<script>alert(1)</script>", ".png") is False)
check("PNG 内容但改名 .jpg 被拒绝（扩展名与内容须一致）",
      _is_valid_image(b"\x89PNG\r\n\x1a\n" + b"0" * 32, ".jpg") is False)
check("空文件被拒绝", _is_valid_image(b"", ".png") is False)
check("svg 不在放行格式内（防内联 XSS）",
      ".svg" not in fp.IMAGE_EXTENSIONS)

# ---------- 汇总 ----------
print("\n" + "=" * 60)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
