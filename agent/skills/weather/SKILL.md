---
name: weather
description: 查询天气信息，包括实时天气、天气预报和城市地理位置查询。当用户询问天气、温度、空气质量等天气相关问题时使用此技能。
---

# 天气查询

查询实时天气或天气预报。工作流程：先用 `geo_lookup` 查城市 ID，再调用 `current_weather` 或 `forecast`。

脚本：
- `geo_lookup` — 城市搜索，参数：location（城市名）
- `current_weather` — 实时天气，参数：location（城市ID）
- `forecast` — 天气预报，参数：days（3d/7d/10d/15d/30d）, location（城市ID）