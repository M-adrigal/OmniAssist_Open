"""图片叠加修复测试 —— 会话上传目录跨轮累积，图片只应发本轮上传的。

背景（用户实测反馈）：一个会话分三轮各传一张图，第三轮起每轮请求都把
前两轮的图一起带上（会话上传目录不清空 + 后端整包扫描所致）。

修复：前端在上传文件随消息上报本轮文件名，后端
`_select_round_images` 只放行本轮上传的图片；未上报 = 本轮无新图，
历史图一律不发（图片从不写会话历史，重发只会叠加与浪费）。
文档文本仍走全目录（跨轮引用是文档场景的既定特性）。

运行：
  /Users/yuanye/.workbuddy/binaries/python/envs/default/bin/python scripts/test_image_round_isolation.py
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


from server.routes.chat import _select_round_images  # noqa: E402


def img(name):
    return {"filename": name, "path": f"/tmp/{name}", "size": 1000, "mime": "image/png"}


def names(images):
    return [im["filename"] for im in images]


print("=" * 60)
print("图片「只发本轮」测试")
print("=" * 60)

# 场景 1：三轮各传一张图（复现用户反馈的实锤场景）
print("\n[1] 三轮对话，每轮一张图")
dir3 = [img("image_a.png"), img("image_b.png"), img("image_c.png")]  # 目录累积 3 张
r1, s1 = _select_round_images(dir3, ["image_a.png"])
check("第 1 轮只带 a", names(r1) == ["image_a.png"], names(r1))
r2, s2 = _select_round_images(dir3, ["image_b.png"])
check("第 2 轮只带 b（不带 a）", names(r2) == ["image_b.png"], names(r2))
r3, s3 = _select_round_images(dir3, ["image_c.png"])
check("第 3 轮只带 c（不带 a/b）", names(r3) == ["image_c.png"], names(r3))

# 场景 2：一轮传多张（全部放行）
print("\n[2] 一轮传多张")
r, s = _select_round_images(dir3, ["image_a.png", "image_c.png"])
check("本轮 2 张全带", sorted(names(r)) == ["image_a.png", "image_c.png"], names(r))

# 场景 3：未上报本轮文件（旧客户端 / 重新生成 / 粘贴失败）
print("\n[3] 未上报本轮文件")
r, s = _select_round_images(dir3, [])
check("空列表 = 不发任何图", r == [] and s == 0, (names(r), s))
r, s = _select_round_images(dir3, None)
check("None = 不发任何图", r == [] and s == 0)

# 场景 4：上报了但这轮传的是文档（目录里只有历史图片）
print("\n[4] 本轮只传文档")
r, s = _select_round_images(dir3, ["report.docx"])
check("文档轮不发历史图", r == [], names(r))

# 场景 5：skipped 按本轮口径 —— 历史图超限不错报成本轮
print("\n[5] skipped 计数（本轮口径）")
# 本轮传 1 张且成功放行 → 无跳过
r, s = _select_round_images(dir3, ["image_a.png"])
check("本轮 1 张全放行 → skipped=0", s == 0, s)
# 本轮传 2 张都放行 → 无跳过
r, s = _select_round_images(dir3, ["image_a.png", "image_b.png"])
check("本轮 2 张全放行 → skipped=0", s == 0, s)
# 本轮传 6 张但目录里只放行 5 张（第 6 张被 build_context_parts 限额剔除）
big = [img(f"i{i}.png") for i in range(5)]
r, s = _select_round_images(big, [f"i{i}.png" for i in range(6)])
check("本轮 6 张放行 5 张 → skipped=1", s == 1, s)
check("放行的确实是 5 张", len(r) == 5, len(r))
# 本轮传文档轮 → 不计图片跳过
r, s = _select_round_images(dir3, ["a.docx", "b.pdf"])
check("文档轮 skipped=0", s == 0, s)

# 场景 6：边界 —— 空目录、文件名带空白、 None 元素
print("\n[6] 边界")
r, s = _select_round_images([], ["x.png"])
check("空目录", r == [])
r, s = _select_round_images([img(" spaced.png ")], [" spaced.png "])
check("文件名两侧空白被 strip", names(r) == [" spaced.png "], names(r))
r, s = _select_round_images(dir3, [None, "", "image_b.png"])
check("None/空字符串元素被忽略", names(r) == ["image_b.png"], names(r))

# 场景 7：上传同名新版本（目录里去重后剩一份，本轮应放行）
print("\n[7] 同名再上传")
r, s = _select_round_images([img("photo.png")], ["photo.png"])
check("同名文件本轮放行", names(r) == ["photo.png"], names(r))

print("\n" + "=" * 60)
print(f"结果: {PASS} 通过, {FAIL} 失败")
if FAILED:
    print("失败项:", FAILED)
print("=" * 60)
sys.exit(0 if FAIL == 0 else 1)
