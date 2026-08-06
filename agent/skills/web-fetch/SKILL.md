---
name: web-fetch
description: 获取网页内容，支持提取文本、HTML源码和按CSS选择器抽取特定内容。当用户需要获取URL内容、抓取网页、提取网页信息时使用此技能。
---

# 网页获取

获取公开网页内容。调用 `fetch_url` 脚本，参数：url（完整 http/https 地址）, mode（"text"/"html"/"selector"，默认 "text"）, selector（CSS选择器，mode="selector" 时使用）。