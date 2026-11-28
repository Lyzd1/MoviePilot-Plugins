"""内嵌歌词写入实现（只依赖 mutagen 与标准库，便于脱离宿主单独测试）。

MoviePilot 在整理完成后会把本插件返回的歌词写成同目录旁挂文件
（``.lrc`` / ``.txt`` / ``.lyricsfile.yaml``）。本模块负责把已生成的旁挂
歌词写进 FLAC / OGG 音频标签，并在成功后删除旁挂文件。

调用方是插件的事件处理器：影视文件走 ``TransferComplete``
（``transfer.complete``），音频文件由宿主发布 ``AudioTransferComplete``
（``transfer.audio.complete``）；两者载荷结构一致，本模块不感知事件类型。

写入规则：
- 有时间轴：同时写三个字段——``LYRICS`` = LRC 全文（主流播放器/标签
  编辑器读这个键，必须带时间轴）、``UNSYNCEDLYRICS`` = 去掉时间轴的纯文本、
  ``SYNCEDLYRICS`` = LRC 全文（兼容 MusicBee 等只认该键的播放器）；
- 没有时间轴：只写 ``LYRICS`` = 纯文本；
- 原文件已有内嵌歌词时直接跳过，不做任何修改。

为保证原子性，先把原文件复制到同目录临时文件，在临时文件上写标签，
成功后再用 ``os.replace`` 替换原文件。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import mutagen

# 只处理这两种容器后缀
SUPPORTED_EXTENSIONS = (".flac", ".ogg")
# 需要清理的旁挂歌词后缀
SIDECAR_EXTENSIONS = (".lrc", ".txt", ".lyricsfile.yaml")

# 视为「已有内嵌歌词」的标签键（大小写不敏感）
_LYRIC_TAG_KEYS = frozenset(
    {
        "lyrics",
        "syncedlyrics",
        "unsyncedlyrics",
        "lyricsfile",
        "lyricsfile.yaml",
        "synced lyrics",
        "unsynced lyrics",
        "©lyr",
        "\xa9lyr",
    }
)

# LRC 行内的时间轴/元数据标签
_LRC_TAG_RE = re.compile(r"\[[^\]]*\]")


@dataclass
class EmbedOutcome:
    """一次内嵌操作的结果。

    status 取值：
    - ``embedded``：已写入并删除旁挂文件；
    - ``already``：原文件已有内嵌歌词，已跳过；
    - ``no_sidecar``：没有可用的旁挂歌词或内容为空，已跳过；
    - ``unsupported`` / ``missing``：后缀不符或文件不可用，已跳过；
    - ``error``：发生异常，未完成写入。
    """

    status: str
    deleted: List[str] = field(default_factory=list)
    failed_deletes: List[str] = field(default_factory=list)
    detail: str = ""


def _value_has_text(value: object) -> bool:
    """判断标签值（字符串、列表或 mutagen 帧对象）是否含有非空文本。"""
    if isinstance(value, (list, tuple)):
        return any(_value_has_text(item) for item in value)
    text = getattr(value, "text", value)
    if isinstance(text, (list, tuple)):
        return any(str(item or "").strip() for item in text)
    return bool(str(text or "").strip())


def has_embedded_lyrics(audio_path: Path) -> bool:
    """判断音频是否已带有非空的内嵌歌词标签。"""
    audio = mutagen.File(audio_path)
    if audio is None:
        return False
    tags = getattr(audio, "tags", None)
    if not tags:
        return False
    try:
        items = list(tags.items())
    except Exception:
        items = []
    for key, value in items:
        if str(key).casefold() in _LYRIC_TAG_KEYS and _value_has_text(value):
            return True
    # ID3 的 SYLT/USLT 帧不会以普通键出现在 items 里
    getall = getattr(tags, "getall", None)
    if callable(getall):
        for frame_id in ("SYLT", "USLT"):
            try:
                frames = getall(frame_id)
            except Exception:
                frames = []
            if any(_value_has_text(frame) for frame in frames):
                return True
    return False


def _read_text(path: Path) -> str:
    """读取文本旁挂文件，兼容 BOM 与少量编码问题。"""
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""


def lrc_to_plain(lrc: str) -> Optional[str]:
    """去掉 LRC 行首时间轴与元数据标签，得到纯文本。"""
    lines: List[str] = []
    for raw_line in lrc.splitlines():
        text = _LRC_TAG_RE.sub("", raw_line).strip()
        if text:
            lines.append(text)
    return "\n".join(lines) or None


def _yaml_to_lyrics(content: str) -> Tuple[Optional[str], Optional[str]]:
    """把 Lyricsfile YAML 的 lines/start_ms 转成 LRC，并生成纯文本。"""
    try:
        import yaml  # 宿主环境自带 PyYAML，按需导入便于单独测试
    except ImportError:
        return None, None
    try:
        payload = yaml.safe_load(content)
    except Exception:
        return None, None
    if not isinstance(payload, dict):
        return None, None

    synced_lines: List[str] = []
    plain_lines: List[str] = []
    for line in payload.get("lines") or []:
        if not isinstance(line, dict):
            continue
        text_value = line.get("text")
        text = text_value.strip() if isinstance(text_value, str) else ""
        if not text and isinstance(line.get("words"), list):
            text = "".join(
                str(word.get("text") or "")
                for word in line["words"]
                if isinstance(word, dict)
            ).strip()
        if not text:
            continue
        plain_lines.append(text)
        start_ms = line.get("start_ms")
        if isinstance(start_ms, bool) or not isinstance(start_ms, int):
            continue
        minutes, remainder = divmod(max(start_ms, 0), 60000)
        synced_lines.append(f"[{minutes:02d}:{remainder / 1000:05.2f}]{text}")

    synced = "\n".join(synced_lines) or None
    plain = "\n".join(plain_lines) or None
    if not synced and not plain:
        plain_field = payload.get("plain")
        if isinstance(plain_field, str) and plain_field.strip():
            plain = plain_field.strip()
    return synced, plain


def read_sidecar_lyrics(audio_path: Path) -> Optional[Tuple[Optional[str], Optional[str], Path]]:
    """读取同目录旁挂歌词。

    优先级：``.lrc``（带时间轴） > ``.txt``（纯文本） > ``.lyricsfile.yaml``。
    返回 ``(synced, plain, sidecar_path)``；没有可用歌词时返回 ``None``。
    """
    audio_path = Path(audio_path)

    lrc_path = audio_path.with_suffix(".lrc")
    if lrc_path.is_file():
        content = _read_text(lrc_path)
        if content:
            return content, lrc_to_plain(content), lrc_path

    txt_path = audio_path.with_suffix(".txt")
    if txt_path.is_file():
        content = _read_text(txt_path)
        if content:
            return None, content, txt_path

    yaml_path = audio_path.with_suffix(".lyricsfile.yaml")
    if yaml_path.is_file():
        content = _read_text(yaml_path)
        if content:
            synced, plain = _yaml_to_lyrics(content)
            if synced or plain:
                return synced, plain, yaml_path

    return None


def _write_tags_atomic(audio_path: Path, synced: Optional[str], plain: Optional[str]) -> None:
    """同目录复制出临时文件、写入标签后原子替换原文件。"""
    parent = audio_path.parent
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{audio_path.stem[:40]}.",
        suffix=audio_path.suffix,
        dir=str(parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(audio_path, temp_path)
        audio = mutagen.File(temp_path)
        if audio is None:
            raise ValueError("无法识别的音频容器")
        if getattr(audio, "tags", None) is None:
            audio.add_tags()
        if synced:
            # 三写：LYRICS 带时间轴供主流播放器读取，UNSYNCEDLYRICS 纯文本兜底，
            # SYNCEDLYRICS 兼容只认该键的播放器（如 MusicBee）。
            audio["LYRICS"] = synced
            if plain:
                audio["UNSYNCEDLYRICS"] = plain
            audio["SYNCEDLYRICS"] = synced
        else:
            audio["LYRICS"] = plain
        audio.save()
        os.replace(temp_path, audio_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def write_lyrics_direct(
    audio_path: Path,
    synced: Optional[str],
    plain: Optional[str],
) -> EmbedOutcome:
    """把插件自己取到的歌词直接写入 FLAC/OGG 内嵌标签。

    与 ``embed_sidecar_lyrics`` 不同，本函数不读取也不删除任何旁挂文件，
    供插件接管歌词刮削后直接落盘使用。

    :param audio_path: 目标音轨路径
    :param synced: 带时间轴的 LRC；没有时间轴时为 ``None``
    :param plain: 纯文本歌词；为空且有时间轴时自动从 LRC 推导
    :return: 操作结果
    """
    path = Path(audio_path)
    if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        return EmbedOutcome("unsupported", detail=f"不支持的内嵌格式：{path.suffix}")
    if not path.is_file():
        return EmbedOutcome("missing", detail="文件不存在")

    synced = (synced or "").strip() or None
    plain = (plain or "").strip() or None
    if synced and not plain:
        # 保证写入 UNSYNCEDLYRICS，三写策略完整
        plain = lrc_to_plain(synced)
    if not synced and not plain:
        return EmbedOutcome("no_lyrics", detail="没有可写入的歌词")

    try:
        _write_tags_atomic(path, synced, plain)
    except Exception as error:
        return EmbedOutcome("error", detail=f"写入内嵌歌词失败：{error}")
    return EmbedOutcome("embedded", detail=f"时间轴={'有' if synced else '无'}")


def _remove_sidecars(audio_path: Path) -> Tuple[List[str], List[str]]:
    """删除该音轨存在的全部旁挂歌词，返回 (成功列表, 失败列表)。"""
    deleted: List[str] = []
    failed: List[str] = []
    for suffix in SIDECAR_EXTENSIONS:
        sidecar = audio_path.with_suffix(suffix)
        if not sidecar.is_file():
            continue
        try:
            sidecar.unlink()
            deleted.append(str(sidecar))
        except OSError:
            failed.append(str(sidecar))
    return deleted, failed


def embed_sidecar_lyrics(audio_path: Path) -> EmbedOutcome:
    """把同目录旁挂歌词写入 FLAC/OGG 内嵌标签，成功后删除旁挂文件。

    :param audio_path: 已整理到本地、位于最终目录的音轨路径
    :return: 操作结果
    """
    path = Path(audio_path)
    if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        return EmbedOutcome("unsupported", detail=f"不支持的内嵌格式：{path.suffix}")
    if not path.is_file():
        return EmbedOutcome("missing", detail="文件不存在")

    try:
        if has_embedded_lyrics(path):
            return EmbedOutcome("already", detail="原文件已有内嵌歌词")
    except Exception as error:
        return EmbedOutcome("error", detail=f"读取内嵌歌词失败：{error}")

    sidecar = read_sidecar_lyrics(path)
    if sidecar is None:
        return EmbedOutcome("no_sidecar", detail="没有可用的旁挂歌词")
    synced, plain, sidecar_path = sidecar
    if not synced and not plain:
        return EmbedOutcome("no_sidecar", detail="旁挂歌词内容为空")

    try:
        _write_tags_atomic(path, synced, plain)
    except Exception as error:
        return EmbedOutcome("error", detail=f"写入内嵌歌词失败：{error}")

    deleted, failed = _remove_sidecars(path)
    return EmbedOutcome(
        "embedded",
        deleted=deleted,
        failed_deletes=failed,
        detail=f"来源={sidecar_path.name}，时间轴={'有' if synced else '无'}",
    )
