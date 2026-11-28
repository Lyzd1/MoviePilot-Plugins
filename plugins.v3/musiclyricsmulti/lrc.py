"""
LRC 解析、双语合并与序列化。

合并策略（translation_mode）：
- ``merge``：按时间戳最近邻（容差 0.3s）+ 双指针单调对齐，合成
  ``[mm:ss.xx]原文  译文`` 一行；
- ``two_lines``：同一时间戳输出两行（原文在前，译文在后）；
- ``original_only``：忽略翻译轨，原样返回原文。

时间戳始终取自原文轨，翻译行只在容差内配对；配不上的译文丢弃，
因此不会出现“整轨错位”或“唱完再唱一遍译文”的问题。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# [mm:ss]、[mm:ss.x]、[mm:ss.xx]、[mm:ss.xxx]，小数分隔符兼容 . 与 :
_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 常见 LRC 元数据标签；纯文本输出时只移除这些，保留 [Verse] 等正文标记
_META_TAG_RE = re.compile(
    r"^\s*\[(?:ti|ar|al|by|offset|length|re|ve|id|hash|sign|qq|total)\s*:[^\]]*\]\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)

MERGE = "merge"
TWO_LINES = "two_lines"
ORIGINAL_ONLY = "original_only"
TRANSLATION_MODES = (MERGE, TWO_LINES, ORIGINAL_ONLY)

# 翻译行与原文行允许的时间差（毫秒）
ALIGN_TOLERANCE_MS = 300

_INSTRUMENTAL_MARKERS = (
    "纯音乐，请欣赏",
    "纯音乐,请欣赏",
    "此歌曲为没有填词的纯音乐",
    "没有填词的纯音乐",
    "该歌曲为纯音乐",
    "请您欣赏纯音乐",
)


@dataclass(frozen=True)
class LrcLine:
    """一行带时间轴的歌词。"""

    ms: int
    text: str


def clean_text(value: object) -> str:
    """去掉 BOM、统一换行，不改写歌词正文。"""
    text = str(value or "")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def has_timestamps(value: object) -> bool:
    """判断文本是否包含至少一个有效时间轴。"""
    return bool(_TIMESTAMP_RE.search(clean_text(value)))


def is_instrumental(value: object) -> bool:
    """判断源是否明确声明为纯音乐（仅用于跳过，不伪造纯音乐候选）。"""
    lowered = clean_text(value).casefold()
    return any(marker.casefold() in lowered for marker in _INSTRUMENTAL_MARKERS)


def _fraction_to_ms(fraction: Optional[str]) -> int:
    """把时间戳小数部分转成毫秒：``5`` → 500，``25`` → 250，``250`` → 250。"""
    if not fraction:
        return 0
    if len(fraction) == 1:
        return int(fraction) * 100
    if len(fraction) == 2:
        return int(fraction) * 10
    return int(fraction[:3])


def parse_lrc(value: object) -> list[LrcLine]:
    """
    解析 LRC 文本为按时间升序的行列表。

    - 同一行有多个时间戳时，每个时间戳各生成一条记录；
    - 元数据行（``[ti:]`` / ``[ar:]`` 等）与空文本行被忽略；
    - 不改写文本内容，只去掉时间轴后的首尾空白。

    :param value: LRC 文本
    :return: 排序后的歌词行
    """
    lines: list[LrcLine] = []
    for raw_line in clean_text(value).split("\n"):
        matches = list(_TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue
        text = raw_line[matches[-1].end():].strip()
        if not text:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            ms = (minutes * 60 + seconds) * 1000 + _fraction_to_ms(match.group(3))
            lines.append(LrcLine(ms=ms, text=text))
    lines.sort(key=lambda item: item.ms)
    return lines


def _format_timestamp(ms: int) -> str:
    """把毫秒格式化为标准 ``[mm:ss.xx]``（分钟可超过两位）。"""
    safe_ms = max(int(ms), 0)
    minutes, remainder = divmod(safe_ms, 60_000)
    seconds = remainder / 1000.0
    return f"[{minutes:02d}:{seconds:05.2f}]"


def _serialize(lines: list[tuple[int, str]]) -> str:
    """把 (毫秒, 文本) 列表序列化为 LRC。"""
    return "\n".join(f"{_format_timestamp(ms)}{text}" for ms, text in lines)


def _plain_text(value: object) -> str:
    """
    返回纯文本歌词：只移除 LRC 元数据行，保留正文中的方括号标记。
    """
    text = _META_TAG_RE.sub("", clean_text(value))
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _align(
    original: list[LrcLine],
    translation: list[LrcLine],
) -> list[tuple[int, str, Optional[str]]]:
    """
    用双指针把翻译行单调对齐到原文行。

    只允许向前推进，避免同一译文被重复使用；超出容差的译文直接丢弃。

    :param original: 原文行（已按时间排序）
    :param translation: 译文行（已按时间排序，且已过滤空文本）
    :return: ``(原文毫秒, 原文文本, 译文或 None)`` 列表
    """
    aligned: list[tuple[int, str, Optional[str]]] = []
    if not translation:
        return [(line.ms, line.text, None) for line in original]

    index = 0
    for line in original:
        # 跳过明显早于当前原文行、且超出容差的译文
        while index < len(translation) and translation[index].ms < line.ms - ALIGN_TOLERANCE_MS:
            index += 1
        if index >= len(translation):
            aligned.append((line.ms, line.text, None))
            continue
        # 在单调前提下逼近与当前原文行最近的一条译文
        while (
            index + 1 < len(translation)
            and abs(translation[index + 1].ms - line.ms)
            <= abs(translation[index].ms - line.ms)
        ):
            index += 1
        if abs(translation[index].ms - line.ms) <= ALIGN_TOLERANCE_MS:
            aligned.append((line.ms, line.text, translation[index].text))
            index += 1
        else:
            aligned.append((line.ms, line.text, None))
    return aligned


def merge_translation(
    original_lrc: str,
    translation_lrc: Optional[str],
    mode: str = MERGE,
) -> str:
    """
    按配置模式合并原文与翻译轨。

    :param original_lrc: 原文 LRC
    :param translation_lrc: 独立翻译轨，可能为 None / 空串
    :param mode: merge / two_lines / original_only
    :return: 合并后的 LRC；无法合并时返回规范化后的原文
    """
    original_text = clean_text(original_lrc).strip()
    if not original_text:
        return ""

    if mode not in TRANSLATION_MODES:
        mode = MERGE
    translation_text = clean_text(translation_lrc).strip()
    if mode == ORIGINAL_ONLY or not translation_text:
        return original_text

    original_lines = parse_lrc(original_text)
    if not original_lines:
        # 原文没有时间轴，无法按时间对齐，保持原文
        return original_text

    # 空文本译文（常见于网易云“只有时间轴没有翻译”的占位行）不参与对齐
    translation_lines = [line for line in parse_lrc(translation_text) if line.text]
    if not translation_lines:
        return original_text

    aligned = _align(original_lines, translation_lines)
    merged: list[tuple[int, str]] = []
    for ms, original, translated in aligned:
        if mode == TWO_LINES:
            merged.append((ms, original))
            if translated:
                merged.append((ms, translated))
        else:
            text = f"{original}  {translated}" if translated else original
            merged.append((ms, text))
    return _serialize(merged)


def build_lyrics(
    original_lrc: str,
    translation_lrc: Optional[str],
    mode: str = MERGE,
) -> tuple[Optional[str], Optional[str]]:
    """
    构造 ``MusicLyrics`` 需要的 (synced_lyrics, plain_lyrics)。

    :param original_lrc: 源返回的原文歌词
    :param translation_lrc: 源返回的翻译轨
    :param mode: 翻译模式
    :return: 同步歌词与纯文本歌词；没有内容时对应项为 None
    """
    original_text = clean_text(original_lrc).strip()
    if not original_text:
        return None, None
    if is_instrumental(original_text):
        return None, None
    if has_timestamps(original_text):
        synced = merge_translation(original_text, translation_lrc, mode)
        return (synced or None), None
    plain = _plain_text(original_text)
    return None, (plain or None)
