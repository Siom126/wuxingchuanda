#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lunar_python import Lunar, Solar

# 从同级目录的规则文件导入全部常量
from plate_arrangement_rules import (
    DEFAULT_RULESET,
    DEFAULT_TIMEZONE,
    CHINA_NAMES,
    JIAZI,
    YANG_TERMS,
    JU_TABLE,
    EARTH_STEM_ORDER,
    ROTATION_RING,
    STAR_RING,
    DOOR_RING,
    GOD_RING_YANG,
    GOD_RING_YIN,
    XUNSHOU_TO_HIDDEN_YI,
    BRANCH_TO_PALACE,
    ZHI_ORDER,
    GAN_ORDER,
    PALACE_INFO,
    ORIGINAL_STAR_DOOR,
    GRID_ORDER,
)


def _parse_bool(value: Any) -> bool:
    """兼容常见布尔拼写错误（如 ture）"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "ture", "1", "yes", "是"}
    return bool(value)


def get_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)
        if name.upper() == "UTC":
            return timezone.utc
        raise


@dataclass
class NormalizedInput:
    question_type: str
    question_goal: str
    calendar_type: str
    timezone: str
    country: str
    city: str
    use_now: bool
    leap_month: bool
    solar_dt: datetime
    lunar_input: dict[str, Any] | None
    original_time_input: Any
    warnings: list[str]


def rotate_to_start(seq: list[Any], start: Any) -> list[Any]:
    idx = seq.index(start)
    return seq[idx:] + seq[:idx]


def parse_datetime_string(value: str) -> dict[str, int]:
    normalized = value.strip().replace("T", " ").replace("/", "-")
    normalized = normalized.replace("年", "-").replace("月", "-").replace("日", " ")
    normalized = normalized.replace("时", ":").replace("點", ":").replace("点", ":").replace("分", "").replace("秒", "")
    # 合并连续空白，防止中文替换后产生碎片
    normalized = " ".join(normalized.split())
    parts = normalized.split()
    if not parts:
        raise ValueError("time 不能为空")

    # 重新组合日期部分，过滤掉由中文替换产生的孤立 "-"
    date_parts: list[str] = []
    time_parts: list[str] = []
    found_time = False
    for part in parts:
        if ":" in part:
            found_time = True
            time_parts.append(part)
        elif not found_time and part != "-":
            date_parts.append(part)

    if not date_parts:
        raise ValueError("日期格式需为 YYYY-MM-DD")
    date_str = "-".join(date_parts)
    date_bits = [int(bit) for bit in date_str.split("-") if bit]
    if len(date_bits) != 3:
        raise ValueError("日期格式需为 YYYY-MM-DD")

    time_bits = [0, 0, 0]
    if time_parts:
        raw_time = time_parts[0]
        tmp = [int(bit) for bit in raw_time.split(":") if bit]
        if len(tmp) >= 2:
            time_bits[0] = tmp[0]
            time_bits[1] = tmp[1]
        if len(tmp) >= 3:
            time_bits[2] = tmp[2]
    return {
        "year": date_bits[0],
        "month": date_bits[1],
        "day": date_bits[2],
        "hour": time_bits[0],
        "minute": time_bits[1],
        "second": time_bits[2],
    }


def _get_location_field(payload: dict[str, Any], key: str) -> str:
    """优先读取扁平键 location.xxx，其次读取嵌套 location["xxx"]"""
    flat_key = f"location.{key}"
    if flat_key in payload:
        return str(payload[flat_key] or "").strip()
    location = payload.get("location") or {}
    return str(location.get(key) or "").strip()


def resolve_timezone(payload: dict[str, Any], warnings: list[str]) -> str:
    tz = _get_location_field(payload, "timezone")
    if tz:
        return tz
    country = _get_location_field(payload, "country").lower()
    city = _get_location_field(payload, "city")
    if country in CHINA_NAMES or not country:
        return DEFAULT_TIMEZONE
    warnings.append(f"未提供海外时区，脚本暂按 {DEFAULT_TIMEZONE} 计算，请在访谈中先补齐时区。")
    if city:
        warnings.append(f"已收到海外城市 {city}，但仍缺少明确时区。")
    return DEFAULT_TIMEZONE


def normalize_input(payload: dict[str, Any]) -> NormalizedInput:
    warnings: list[str] = []
    timezone = resolve_timezone(payload, warnings)
    try:
        tz = get_timezone(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无法识别时区 {timezone}: {exc}") from exc

    calendar_type = str(payload.get("calendar_type") or "solar").strip().lower()
    # 支持 time 字段（新格式）或 time_input 字段（旧格式兼容）
    original_time_input = payload.get("time") if "time" in payload else payload.get("time_input")
    leap_month = _parse_bool(payload.get("is_leap_month", False))
    lunar_input: dict[str, Any] | None = None

    if calendar_type == "now":
        solar_dt = datetime.now(tz).replace(microsecond=0)
        use_now = True
    elif calendar_type == "solar":
        use_now = False
        if isinstance(original_time_input, dict):
            raw = {
                "year": int(original_time_input["year"]),
                "month": int(original_time_input["month"]),
                "day": int(original_time_input["day"]),
                "hour": int(original_time_input.get("hour", 0)),
                "minute": int(original_time_input.get("minute", 0)),
                "second": int(original_time_input.get("second", 0)),
            }
        else:
            raw = parse_datetime_string(str(original_time_input))
        solar_dt = datetime(raw["year"], raw["month"], raw["day"], raw["hour"], raw["minute"], raw["second"], tzinfo=tz)
    elif calendar_type == "lunar":
        use_now = False
        if isinstance(original_time_input, dict):
            raw = {
                "year": int(original_time_input["year"]),
                "month": int(original_time_input["month"]),
                "day": int(original_time_input["day"]),
                "hour": int(original_time_input.get("hour", 0)),
                "minute": int(original_time_input.get("minute", 0)),
                "second": int(original_time_input.get("second", 0)),
                "is_leap_month": leap_month,
            }
        else:
            raw = parse_datetime_string(str(original_time_input))
            raw["is_leap_month"] = leap_month
        lunar_month = -raw["month"] if leap_month else raw["month"]
        lunar_obj = Lunar.fromYmdHms(raw["year"], lunar_month, raw["day"], raw["hour"], raw["minute"], raw["second"])
        solar_obj = lunar_obj.getSolar()
        solar_dt = datetime(
            solar_obj.getYear(),
            solar_obj.getMonth(),
            solar_obj.getDay(),
            solar_obj.getHour(),
            solar_obj.getMinute(),
            solar_obj.getSecond(),
            tzinfo=tz,
        )
        lunar_input = raw
    else:
        raise ValueError(f"不支持的 calendar_type: {calendar_type}")

    return NormalizedInput(
        question_type=str(payload.get("question_type") or "").strip(),
        question_goal=str(payload.get("question_goal") or "").strip(),
        calendar_type=calendar_type,
        timezone=timezone,
        country=_get_location_field(payload, "country"),
        city=_get_location_field(payload, "city"),
        use_now=use_now,
        leap_month=leap_month,
        solar_dt=solar_dt,
        lunar_input=lunar_input,
        original_time_input=original_time_input,
        warnings=warnings,
    )


def build_solar_and_lunar(normalized: NormalizedInput) -> tuple[Solar, Any]:
    solar = Solar.fromYmdHms(
        normalized.solar_dt.year,
        normalized.solar_dt.month,
        normalized.solar_dt.day,
        normalized.solar_dt.hour,
        normalized.solar_dt.minute,
        normalized.solar_dt.second,
    )
    lunar = solar.getLunar()
    return solar, lunar


def active_jie(lunar: Any) -> tuple[str, Any, Any]:
    prev_jie = lunar.getPrevJie(False)
    next_jie = lunar.getNextJie(False)
    if prev_jie is None:
        raise ValueError("无法确定当前节令")
    return prev_jie.getName(), prev_jie, next_jie


def compute_yuan(day_ganzhi: str) -> str:
    """按符头（甲或己开头的日子）地支确定三元"""
    idx = JIAZI.index(day_ganzhi)
    mod = idx % 10
    if mod == 0:
        futou_idx = idx
    elif mod < 5:
        futou_idx = idx - mod
    elif mod == 5:
        futou_idx = idx
    else:
        futou_idx = idx - mod + 5

    futou_ganzhi = JIAZI[futou_idx]
    futou_zhi = futou_ganzhi[1]

    if futou_zhi in {"子", "午", "卯", "酉"}:
        return "上元"
    elif futou_zhi in {"寅", "申", "巳", "亥"}:
        return "中元"
    else:
        return "下元"


def compute_earth_plate(dun_type: str, ju_number: int) -> dict[int, str]:
    palaces = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    rotated = rotate_to_start(palaces, ju_number)
    stems = EARTH_STEM_ORDER[dun_type]
    return dict(zip(rotated, stems))


def find_stem_palace(earth_plate: dict[int, str], stem: str) -> int:
    for palace, palace_stem in earth_plate.items():
        if palace_stem == stem:
            return palace
    raise ValueError(f"地盘中未找到天干 {stem}")


def hosted_palace(palace: int) -> int:
    return 2 if palace == 5 else palace


def split_branch_pair(text: str) -> list[str]:
    return [text[i : i + 1] for i in range(0, len(text), 1) if text[i : i + 1]]


def compute_zhishi_palace(dun_type: str, xunshou_palace: int, xunshou_zhi: str, time_zhi: str) -> int:
    """值使随时宫：从旬首宫开始，数到时支所在宫（阳顺阴逆）"""
    steps = (ZHI_ORDER.index(time_zhi) - ZHI_ORDER.index(xunshou_zhi)) % 12
    if dun_type == "阳遁":
        order = ROTATION_RING
    else:
        order = list(reversed(ROTATION_RING))
    start_idx = order.index(xunshou_palace)
    target_idx = (start_idx + steps) % len(order)
    return order[target_idx]


def compute_horse_star(time_zhi: str) -> str:
    """马星：申子辰马在寅，亥卯未马在巳，寅午戌马在申，巳酉丑马在亥"""
    if time_zhi in {"申", "子", "辰"}:
        return "寅"
    elif time_zhi in {"亥", "卯", "未"}:
        return "巳"
    elif time_zhi in {"寅", "午", "戌"}:
        return "申"
    else:
        return "亥"


def compute_hidden_stems(time_gan: str, dun_type: str) -> dict[int, str]:
    """暗干：以时干为基准，阳顺阴逆布于九宫"""
    idx = GAN_ORDER.index(time_gan)
    if dun_type == "阳遁":
        order = ROTATION_RING
    else:
        order = list(reversed(ROTATION_RING))
    result: dict[int, str] = {}
    for i, palace in enumerate(order):
        gan_idx = (idx + i) % 10
        result[palace] = GAN_ORDER[gan_idx]
    return result


def insert_center(seq: list[Any], center_value: Any) -> list[Any]:
    """在坤二宫位置之后插入中五宫元素，形成9元素列表"""
    k2_idx = ROTATION_RING.index(2)
    return seq[:k2_idx + 1] + [center_value] + seq[k2_idx + 1:]


def build_chart(normalized: NormalizedInput, solar: Solar, lunar: Any) -> dict[str, Any]:
    warnings = list(normalized.warnings)

    current_jie_name, prev_jie, next_jie = active_jie(lunar)
    dun_type = "阳遁" if current_jie_name in YANG_TERMS else "阴遁"
    day_ganzhi = lunar.getDayInGanZhiExact()
    yuan = compute_yuan(day_ganzhi)
    ju_number = JU_TABLE[dun_type][current_jie_name][yuan]
    earth_plate = compute_earth_plate(dun_type, ju_number)

    time_ganzhi = lunar.getTimeInGanZhi()
    time_gan = lunar.getTimeGan()
    time_zhi = lunar.getTimeZhi()
    time_xun = lunar.getTimeXun()
    time_xunkong = lunar.getTimeXunKong()
    hidden_yi = XUNSHOU_TO_HIDDEN_YI[time_xun]
    visible_time_gan = hidden_yi if time_gan == "甲" else time_gan
    if time_gan == "甲":
        warnings.append(f"时干为甲，按旬首所遁之仪 {hidden_yi} 入盘。")

    xunshou_raw_palace = find_stem_palace(earth_plate, hidden_yi)
    time_raw_palace = find_stem_palace(earth_plate, visible_time_gan)
    xunshou_palace = hosted_palace(xunshou_raw_palace)
    time_palace = hosted_palace(time_raw_palace)

    if xunshou_raw_palace == 5 or time_raw_palace == 5:
        warnings.append("本规则集中宫相关判断一律寄坤处理。")

    # 值符星与值使门（按旬首所在寄宫后宫位）
    zhifu_star = ORIGINAL_STAR_DOOR[xunshou_palace]["star"]
    zhishi_door = ORIGINAL_STAR_DOOR[xunshou_palace]["door"]

    # 旬首地支（用于值使门计算）
    xunshou_zhi = time_xun[1]

    # 天盘：值符随时干
    if dun_type == "阳遁":
        base_palace_order = ROTATION_RING
        base_star_ring = STAR_RING
        god_order = GOD_RING_YANG
    else:
        base_palace_order = list(reversed(ROTATION_RING))
        base_star_ring = list(reversed(STAR_RING))
        god_order = GOD_RING_YIN

    palace_order = rotate_to_start(base_palace_order, time_palace)
    star_order = rotate_to_start(base_star_ring, STAR_RING[ROTATION_RING.index(xunshou_palace)])

    outer_earth = [earth_plate[palace] for palace in base_palace_order]
    sky_start_stem = hidden_yi if xunshou_raw_palace != 5 else earth_plate[5]

    # 若旬首干在中五宫，需在列表中插入中五宫干，避免 rotate_to_start 找不到
    if xunshou_raw_palace == 5:
        outer_earth = insert_center(outer_earth, earth_plate[5])
        palace_order = insert_center(palace_order, 5)
        star_order = insert_center(star_order, "天禽")
        god_order = insert_center(god_order, None)

    sky_order = rotate_to_start(outer_earth, sky_start_stem)

    star_map = dict(zip(palace_order, star_order))
    god_map = dict(zip(palace_order, god_order))
    sky_map = dict(zip(palace_order, sky_order))

    zhifu = {"star": zhifu_star, "palace": time_palace}
    zhishi = {"door": zhishi_door, "palace": time_palace}

    # 人盘（八门）：值使随时宫
    zhishi_target_palace = compute_zhishi_palace(dun_type, xunshou_palace, xunshou_zhi, time_zhi)
    door_start_idx = DOOR_RING.index(zhishi_door)
    door_sequence = DOOR_RING[door_start_idx:] + DOOR_RING[:door_start_idx]

    if dun_type == "阳遁":
        door_palace_order = rotate_to_start(ROTATION_RING, zhishi_target_palace)
    else:
        door_palace_order = rotate_to_start(list(reversed(ROTATION_RING)), zhishi_target_palace)

    door_map: dict[int, str | None] = {}
    for i, palace in enumerate(door_palace_order):
        door_map[palace] = door_sequence[i]
    door_map[5] = None

    active_jie_dt = datetime.fromisoformat(prev_jie.getSolar().toYmdHms().replace(" ", "T")).replace(tzinfo=normalized.solar_dt.tzinfo)
    next_jie_dt = None
    if next_jie is not None:
        next_jie_dt = datetime.fromisoformat(next_jie.getSolar().toYmdHms().replace(" ", "T")).replace(tzinfo=normalized.solar_dt.tzinfo)
    if abs(normalized.solar_dt - active_jie_dt) <= timedelta(hours=24):
        warnings.append("当前时间距离节令起点较近，属于节气边界附近。")
    if next_jie_dt and abs(next_jie_dt - normalized.solar_dt) <= timedelta(hours=24):
        warnings.append("当前时间距离下一个节令较近，属于节气边界附近。")

    kongwang_branches = split_branch_pair(time_xunkong)
    kongwang_palaces = sorted({BRANCH_TO_PALACE[branch] for branch in kongwang_branches if branch in BRANCH_TO_PALACE})

    # 马星
    horse_star = compute_horse_star(time_zhi)
    horse_star_palace = BRANCH_TO_PALACE.get(horse_star)

    # 暗干
    hidden_stems = compute_hidden_stems(time_gan, dun_type)

    palaces: list[dict[str, Any]] = []
    for palace_no in sorted(PALACE_INFO):
        info = PALACE_INFO[palace_no]
        palace_entry = {
            "palace": palace_no,
            "name": info["name"],
            "direction": info["direction"],
            "trigram": info["trigram"],
            "element": info["element"],
            "earth_stem": earth_plate.get(palace_no),
            "sky_stem": sky_map.get(palace_no),
            "star": star_map.get(palace_no),
            "door": door_map.get(palace_no),
            "god": god_map.get(palace_no),
            "hidden_stem": hidden_stems.get(palace_no),
            "is_center": palace_no == 5,
            "hosts_center": palace_no == 2,
            "hosting_note": "中宫寄坤" if palace_no in {2, 5} else None,
        }
        palaces.append(palace_entry)

    return {
        "dun_type": dun_type,
        "yuan": yuan,
        "ju_number": ju_number,
        "xunshou": time_xun,
        "hidden_yi": hidden_yi,
        "kongwang": kongwang_branches,
        "kongwang_palaces": kongwang_palaces,
        "time_stem_visible": visible_time_gan,
        "zhifu": zhifu,
        "zhishi": zhishi,
        "active_jie": current_jie_name,
        "active_jie_started_at": prev_jie.getSolar().toYmdHms(),
        "next_jie": next_jie.getName() if next_jie else None,
        "next_jie_at": next_jie.getSolar().toYmdHms() if next_jie else None,
        "horse_star": horse_star,
        "horse_star_palace": horse_star_palace,
        "grid_order": GRID_ORDER,
        "palaces": palaces,
        "warnings": warnings,
    }


def build_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_input(payload)
    solar, lunar = build_solar_and_lunar(normalized)
    chart = build_chart(normalized, solar, lunar)

    return {
        "normalized_input": {
            "question_type": normalized.question_type,
            "question_goal": normalized.question_goal,
            "calendar_type": normalized.calendar_type,
            "ruleset": str(payload.get("ruleset") or DEFAULT_RULESET),
            "timezone": normalized.timezone,
            "country": normalized.country,
            "city": normalized.city,
            "used_now": normalized.use_now,
            "original_time_input": normalized.original_time_input,
        },
        "calendar": {
            "solar": {
                "ymd_hms": solar.toYmdHms(),
                "timezone": normalized.timezone,
            },
            "lunar": {
                "year": lunar.getYear(),
                "month": abs(lunar.getMonth()),
                "day": lunar.getDay(),
                "month_text": lunar.getMonthInChinese(),
                "day_text": lunar.getDayInChinese(),
                "is_leap_month": lunar.getMonth() < 0,
            },
            "jieqi": {
                "active_jie": chart["active_jie"],
                "active_jie_started_at": chart["active_jie_started_at"],
                "next_jie": chart["next_jie"],
                "next_jie_at": chart["next_jie_at"],
            },
        },
        "ganzhi": {
            "year": lunar.getYearInGanZhiExact(),
            "month": lunar.getMonthInGanZhiExact(),
            "day": lunar.getDayInGanZhiExact(),
            "time": lunar.getTimeInGanZhi(),
            "day_xun_exact": lunar.getDayXunExact(),
            "day_xunkong_exact": lunar.getDayXunKongExact(),
            "time_xun": lunar.getTimeXun(),
            "time_xunkong": lunar.getTimeXunKong(),
        },
        "ruleset": {
            "id": str(payload.get("ruleset") or DEFAULT_RULESET),
            "name": "时家转盘奇门（大陆默认）",
            "timezone_default": DEFAULT_TIMEZONE,
            "dun_type_rule": "冬至到芒种用阳遁，夏至到大雪用阴遁，按当前节令判定。",
            "yuan_rule": "按日干支符头（甲或己日）地支确定三元：子午卯酉为上元，寅申巳亥为中元，辰戌丑未为下元。",
            "ju_rule": "按当前节令和三元，从固定定局表取局数。",
            "center_hosting_rule": "中宫相关判断一律寄坤处理。",
            "zhifu_rule": "值符随时干：旬首所在宫原始九星为值符，带旬首天干落时干宫。",
            "zhishi_rule": "值使随时支：从旬首宫开始数到时支宫，阳顺阴逆，该宫落值使门。",
            "god_rule": "八神值符与天盘值符星同宫，阳遁顺时针、阴遁逆时针排布。",
            "horse_star_rule": "申子辰马在寅，亥卯未马在巳，寅午戌马在申，巳酉丑马在亥。",
            "hidden_stem_rule": "暗干以时干为基准，阳顺阴逆布于九宫。",
        },
        "chart": {
            "dun_type": chart["dun_type"],
            "yuan": chart["yuan"],
            "ju_number": chart["ju_number"],
            "xunshou": chart["xunshou"],
            "hidden_yi": chart["hidden_yi"],
            "kongwang": chart["kongwang"],
            "kongwang_palaces": chart["kongwang_palaces"],
            "time_stem_visible": chart["time_stem_visible"],
            "zhifu": chart["zhifu"],
            "zhishi": chart["zhishi"],
            "horse_star": chart["horse_star"],
            "horse_star_palace": chart["horse_star_palace"],
            "grid_order": chart["grid_order"],
            "palaces": chart["palaces"],
        },
        "warnings": chart["warnings"],
    }


# ──────────────────────────────────────────────
# 自动保存到 ret 目录（新增）
# ──────────────────────────────────────────────

def _sanitize_filename(text: str, max_len: int = 50) -> str:
    """清理非法字符，截断长度，添加 .json 后缀"""
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = text.strip()
    if not text:
        text = "qimen_result"
    if len(text) > max_len:
        text = text[:max_len]
    return text + ".json"


def _get_ret_dir() -> Path:
    """获取脚本同级目录下的 ret 文件夹路径，不存在则创建"""
    script_dir = Path(__file__).resolve().parent
    ret_dir = script_dir / "ret"
    ret_dir.mkdir(exist_ok=True)
    return ret_dir


def build_and_save(payload: dict[str, Any]) -> dict[str, Any]:
    """
    排盘并自动保存结果到同目录 ret/ 下，
    文件名以 question_goal 命名。
    """
    result = build_output(payload)
    ret_dir = _get_ret_dir()
    filename = _sanitize_filename(result["normalized_input"]["question_goal"])
    output_path = ret_dir / filename
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute a structured qimen dunjia chart payload.")
    parser.add_argument("--input", required=True, help="Path to input JSON")
    args = parser.parse_args()

    input_path = Path(args.input)

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
        result = build_and_save(payload)
        print(f"结果已保存到: {Path(__file__).resolve().parent / 'ret' / _sanitize_filename(result['normalized_input']['question_goal'])}")
        return 0
    except Exception as exc:
        error_payload = {"error": str(exc)}
        ret_dir = _get_ret_dir()
        error_path = ret_dir / "error.json"
        error_path.write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"排盘失败，错误信息已保存到: {error_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())