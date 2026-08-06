"""
将公历日期转换为农历日期，返回农历年、月、日、生肖、天干地支等信息

Args:
    year: 公历年份，如 2026
    month: 公历月份，如 8
    day: 公历日期，如 4

Returns:
    str: JSON格式的农历信息
"""
import datetime
import json

LUNAR_DATA = [
    0x04bd8, 0x04ae0, 0x0a570, 0x054d5, 0x0d260, 0x0d950, 0x16554, 0x056a0, 0x09ad0, 0x055d2,
    0x04ae0, 0x0a5b6, 0x0a4d0, 0x0d250, 0x1d255, 0x0b540, 0x0d6a0, 0x0ada2, 0x095b0, 0x14977,
    0x04970, 0x0a4b0, 0x0b4b5, 0x06a50, 0x06d40, 0x1ab54, 0x02b60, 0x09570, 0x052f2, 0x04970,
    0x06566, 0x0d4a0, 0x0ea50, 0x06e95, 0x05ad0, 0x02b60, 0x186e3, 0x092e0, 0x1c8d7, 0x0c950,
    0x0d4a0, 0x1d8a6, 0x0b550, 0x056a0, 0x1a5b4, 0x025d0, 0x092d0, 0x0d2b2, 0x0a950, 0x0b557,
    0x06ca0, 0x0b550, 0x15355, 0x04da0, 0x0a5b0, 0x14573, 0x052b0, 0x0a9a8, 0x0e950, 0x06aa0,
    0x0aea6, 0x0ab50, 0x04b60, 0x0aae4, 0x0a570, 0x05260, 0x0f263, 0x0d950, 0x05b57, 0x056a0,
    0x096d0, 0x04dd5, 0x04ad0, 0x0a4d0, 0x0d4d4, 0x0d250, 0x0d558, 0x0b540, 0x0b6a0, 0x195a6,
    0x095b0, 0x049b0, 0x0a974, 0x0a4b0, 0x0b27a, 0x06a50, 0x06d40, 0x0af46, 0x0ab60, 0x09570,
    0x04af5, 0x04970, 0x064b0, 0x074a3, 0x0ea50, 0x06b58, 0x055c0, 0x0ab60, 0x096d5, 0x092e0,
    0x0c960, 0x0d954, 0x0d4a0, 0x0da50, 0x07552, 0x056a0, 0x0abb7, 0x025d0, 0x092d0, 0x0cab5,
    0x0a950, 0x0b4a0, 0x0baa4, 0x0ad50, 0x055d9, 0x04ba0, 0x0a5b0, 0x15176, 0x052b0, 0x0a930,
    0x07954, 0x06aa0, 0x0ad50, 0x05b52, 0x04b60, 0x0a6e6, 0x0a4e0, 0x0d260, 0x0ea65, 0x0d530,
    0x05aa0, 0x076a3, 0x096d0, 0x04afb, 0x04ad0, 0x0a4d0, 0x1d0b6, 0x0d250, 0x0d520, 0x0dd45,
    0x0b5a0, 0x056d0, 0x055b2, 0x049b0, 0x0a577, 0x0a4b0, 0x0aa50, 0x1b255, 0x06d20, 0x0ada0,
    0x14b63, 0x09370, 0x049f8, 0x04970, 0x064b0, 0x168a6, 0x0ea50, 0x06b20, 0x1a6c4, 0x0aae0,
    0x0a2e0, 0x0d2e3, 0x0c960, 0x0d557, 0x0d4a0, 0x0da50, 0x05d55, 0x056a0, 0x0a6d0, 0x055d4,
    0x052d0, 0x0a9b8, 0x0a950, 0x0b4a0, 0x0b6a6, 0x0ad50, 0x055a0, 0x0aba4, 0x0a5b0, 0x052b0,
    0x0b273, 0x06930, 0x07337, 0x06aa0, 0x0ad50, 0x14b55, 0x04b60, 0x0a570, 0x054e4, 0x0d160,
    0x0e968, 0x0d520, 0x0daa0, 0x16aa6, 0x056d0, 0x04ae0, 0x0a9d4, 0x0a4d0, 0x0d150, 0x0f252,
    0x0d520
]

TIAN_GAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
DI_ZHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]
SHENG_XIAO = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
LUNAR_MONTH_NAMES = ["正月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "冬月", "腊月"]
LUNAR_DAY_NAMES = [
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"
]

START_YEAR = 1900
END_YEAR = 2100


def _leap_month(y):
    return LUNAR_DATA[y - START_YEAR] & 0xf


def _leap_days(y):
    if _leap_month(y):
        return 30 if LUNAR_DATA[y - START_YEAR] & 0x10000 else 29
    return 0


def _month_days(y, m):
    return 30 if LUNAR_DATA[y - START_YEAR] & (0x10000 >> m) else 29


def _lunar_year_days(y):
    days = 0
    for i in range(1, 13):
        days += _month_days(y, i)
    return days + _leap_days(y)


def _solar_day_of_lunar_new_year(y):
    base_date = datetime.date(START_YEAR, 1, 31)
    offset = 0
    for yi in range(START_YEAR, y):
        offset += _lunar_year_days(yi)
    return base_date + datetime.timedelta(days=offset)


def execute(year: int, month: int, day: int) -> str:
    if year < START_YEAR or year > END_YEAR:
        return json.dumps({"error": f"仅支持 {START_YEAR}-{END_YEAR} 年之间的日期转换"}, ensure_ascii=False)

    try:
        solar_date = datetime.date(year, month, day)
    except ValueError as e:
        return json.dumps({"error": f"日期无效: {e}"}, ensure_ascii=False)

    lunar_new_year = _solar_day_of_lunar_new_year(year)
    if solar_date < lunar_new_year:
        lunar_new_year = _solar_day_of_lunar_new_year(year - 1)
        lunar_year = year - 1
    else:
        next_new_year = _solar_day_of_lunar_new_year(year + 1)
        if solar_date >= next_new_year:
            lunar_new_year = next_new_year
            lunar_year = year + 1
        else:
            lunar_year = year

    diff_days = (solar_date - lunar_new_year).days
    leap = _leap_month(lunar_year)
    is_leap = False
    lunar_month = 0
    lunar_day = 0

    for m in range(1, 13):
        m_days = _month_days(lunar_year, m)
        if diff_days < m_days:
            lunar_month = m
            lunar_day = diff_days + 1
            break
        diff_days -= m_days
        if m == leap:
            leap_days = _leap_days(lunar_year)
            if diff_days < leap_days:
                lunar_month = m
                lunar_day = diff_days + 1
                is_leap = True
                break
            diff_days -= leap_days

    if lunar_month == 0:
        return json.dumps({"error": "农历日期计算失败"}, ensure_ascii=False)

    year_offset = (lunar_year - 4) % 60
    tg_index = year_offset % 10
    dz_index = year_offset % 12
    tiangan_dizhi = TIAN_GAN[tg_index] + DI_ZHI[dz_index]
    shengxiao = SHENG_XIAO[dz_index]

    month_str = ("闰" if is_leap else "") + LUNAR_MONTH_NAMES[lunar_month - 1]
    day_str = LUNAR_DAY_NAMES[lunar_day - 1]

    return json.dumps({
        "solar": f"{year}年{month}月{day}日",
        "lunar": f"{tiangan_dizhi}年（{shengxiao}年）{month_str}{day_str}",
        "tiangan_dizhi": tiangan_dizhi,
        "shengxiao": shengxiao,
        "lunar_year": lunar_year,
        "lunar_month": lunar_month,
        "lunar_day": lunar_day,
        "is_leap": is_leap
    }, ensure_ascii=False)