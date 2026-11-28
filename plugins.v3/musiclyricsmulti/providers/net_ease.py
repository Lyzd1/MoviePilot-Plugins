"""
网易云音乐歌词源（匿名 weapi）。

接口与加密按 ZonyLrcToolsX 的接口清单重写：
- 搜索：POST ``weapi/search/get``
- 取词：POST ``weapi/song/lyric``，取 ``lrc.lyric`` + ``tlyric.lyric``
- 加密：AES-128-CBC 两次 + RSA 裸模幂（无 padding）

另外增加一个“歌手歌曲兜底”：当关键词搜索因版权/地区原因不返回原曲时，
用歌手 ID 拉取该歌手歌曲列表（``weapi/v1/artist/songs``）再打分。
"""

from __future__ import annotations

import base64
import json
import secrets
import string
from typing import Any, Optional

from ..httpclient import DeadlineExceeded, ProviderError
from ..log import logger
from ..match import (
    normalize_text,
    rank_candidates,
    to_optional_int,
)
from ..models import Candidate, LyricsQuery, ProviderLyrics
from .base import BaseProvider

try:  # 宿主已内置 pycryptodome；缺失时只让本源失败，不影响插件加载
    from Crypto.Cipher import AES  # type: ignore
except Exception:  # pragma: no cover - 依赖缺失分支
    AES = None  # type: ignore


# weapi 公共 RSA 模数（公开算法参数，不是密钥/凭据）
_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
    "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ec"
    "bda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d81"
    "3cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
    16,
)
_IV = b"0102030405060708"
_PRESET_KEY = "0CoJUm6Qyw8W8jud"
_RSA_EXPONENT = 0x10001
_ALNUM = string.ascii_letters + string.digits

_BASE_URL = "https://music.163.com"
_SEARCH_URL = f"{_BASE_URL}/weapi/search/get"
_LYRIC_URL = f"{_BASE_URL}/weapi/song/lyric?csrf_token="
_ARTIST_SONGS_URL = f"{_BASE_URL}/weapi/v1/artist/songs"
_HEADERS = {
    "Referer": "https://music.163.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}

_SEARCH_LIMIT = 20
_ARTIST_SONG_LIMIT = 100
_MAX_ATTEMPTS = 3


def _pkcs7_pad(data: bytes) -> bytes:
    """PKCS#7 填充到 16 字节块。"""
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def _aes_base64(text: str, key: str) -> str:
    """AES-128-CBC 加密并返回 base64 字符串。"""
    if AES is None:
        raise ProviderError("缺少 pycryptodome，无法调用网易 weapi")
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, _IV)
    encrypted = cipher.encrypt(_pkcs7_pad(text.encode("utf-8")))
    return base64.b64encode(encrypted).decode("ascii")


def _weapi_form(payload: dict[str, Any]) -> dict[str, str]:
    """
    构造 weapi 的 ``params`` 与 ``encSecKey``。

    ``params = Base64(AES(Base64(AES(json, preset)), secret))``；
    ``encSecKey = hex(pow(reverse(secret), 65537, N))`` 左补零到 256 位。
    """
    secret_key = "".join(secrets.choice(_ALNUM) for _ in range(16))
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    first = _aes_base64(plaintext, _PRESET_KEY)
    params = _aes_base64(first, secret_key)
    reversed_key = int(secret_key[::-1].encode("utf-8").hex(), 16)
    enc_sec_key = format(pow(reversed_key, _RSA_EXPONENT, _MODULUS), "x").zfill(256)
    return {"params": params, "encSecKey": enc_sec_key}


def _duration_seconds(value: Any) -> Optional[int]:
    """把网易的毫秒时长转成整数秒；已是秒级时原样返回。"""
    raw = to_optional_int(value)
    if raw is None:
        return None
    return round(raw / 1000) if raw > 1000 else raw


class NetEaseProvider(BaseProvider):
    """网易云音乐（netease）。"""

    name = "netease"
    priority = 30

    def fetch(self, query: LyricsQuery, deadline: float) -> Optional[ProviderLyrics]:
        """搜索候选、择优取词；搜索无合适结果时用歌手歌曲兜底。"""
        candidates = self._search(query, deadline)
        ranked = rank_candidates(query, candidates)
        if not ranked or ranked[0].score < 60:
            fallback = self._artist_fallback(query, deadline)
            if fallback:
                merged = rank_candidates(query, candidates + fallback)
                if merged:
                    ranked = merged
                    logger.debug(
                        f"【多源歌词】netease 歌手歌曲兜底生效：{query.title}"
                        f"（候选 {len(merged)} 条）"
                    )

        for scored in ranked[:_MAX_ATTEMPTS]:
            cached = self._cached(scored)
            if cached:
                return cached
            payload = self._fetch_lyric(scored.candidate.provider_id, deadline)
            if payload is None:
                continue
            original, translation = payload
            result = self._finish(scored, original, translation)
            if result:
                return result
        if not ranked:
            logger.debug(f"【多源歌词】netease 无合适候选：{query.title}")
        return None

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def _weapi_post(self, url: str, payload: dict[str, Any], deadline: float) -> Any:
        """发送一次 weapi POST 并返回 JSON。"""
        form = _weapi_form(payload)
        return self.http.post_form_json(
            url, data=form, headers=_HEADERS, deadline=deadline
        )

    def _search(self, query: LyricsQuery, deadline: float) -> list[Candidate]:
        """按「歌名+歌手」搜索歌曲；零结果时退回只搜歌名。

        网易的检索对「中文歌手 + 英文歌名」组合并不稳定，实测同一请求在不同出口 IP
        下会返回 0 条，而只搜歌名能命中，因此保留一次降级检索。
        """
        terms = []
        for variant in query.title_variants:
            if query.primary_artist:
                terms.append(f"{variant}+{query.primary_artist}")
            terms.append(variant)
        for term in dict.fromkeys(terms):
            candidates = self._search_term(term, deadline)
            if candidates:
                return candidates
        return []

    def _search_term(self, term: str, deadline: float) -> list[Candidate]:
        """用单个检索词请求网易搜索并投影候选。"""
        try:
            payload = self._weapi_post(
                _SEARCH_URL,
                {
                    "s": term,
                    "type": 1,
                    "offset": 0,
                    "total": True,
                    "limit": _SEARCH_LIMIT,
                    "csrf_token": "",
                    "crypto": "weapi",
                },
                deadline,
            )
        except DeadlineExceeded:
            raise
        if not isinstance(payload, dict):
            logger.warning("【多源歌词】netease 搜索响应结构异常")
            return []
        code = payload.get("code")
        if code != 200:
            logger.warning(f"【多源歌词】netease 搜索失败：code={code}")
            return []
        songs = ((payload.get("result") or {}).get("songs")) or []
        candidates = [item for item in (self._candidate_from_song(song) for song in songs) if item]
        logger.debug(f"【多源歌词】netease 搜索 {term!r} 返回 {len(candidates)} 条")
        return candidates

    @staticmethod
    def _candidate_from_song(song: Any) -> Optional[Candidate]:
        """把网易歌曲对象转换为统一候选。"""
        if not isinstance(song, dict):
            return None
        song_id = song.get("id")
        title = str(song.get("name") or "").strip()
        if not song_id or not title:
            return None
        artists = tuple(
            str(artist.get("name") or "").strip()
            for artist in (song.get("artists") or [])
            if isinstance(artist, dict) and str(artist.get("name") or "").strip()
        )
        album_obj = song.get("album") or {}
        album = str(album_obj.get("name") or "").strip() if isinstance(album_obj, dict) else ""
        duration = _duration_seconds(song.get("duration") or song.get("dt"))
        return Candidate(
            title=title,
            artists=artists,
            album=album,
            duration=duration,
            provider_id=str(song_id),
            extra={"song_id": song_id},
        )

    def _artist_fallback(self, query: LyricsQuery, deadline: float) -> list[Candidate]:
        """
        关键词搜索找不到原曲时，按歌手 ID 拉取该歌手歌曲列表。

        只在有艺人信息时启用；任何失败都降级为空列表，不阻断主流程。
        """
        artist = query.primary_artist
        if not artist:
            return []
        try:
            artist_id = self._resolve_artist_id(artist, deadline)
            if not artist_id:
                return []
            payload = self._weapi_post(
                _ARTIST_SONGS_URL,
                {
                    "id": artist_id,
                    "offset": 0,
                    "limit": _ARTIST_SONG_LIMIT,
                    "total": True,
                    "csrf_token": "",
                    "crypto": "weapi",
                },
                deadline,
            )
        except DeadlineExceeded:
            raise
        except ProviderError as error:
            logger.debug(f"【多源歌词】netease 歌手兜底失败：{error}")
            return []
        if not isinstance(payload, dict) or payload.get("code") != 200:
            logger.debug(
                f"【多源歌词】netease 歌手兜底无结果：code={payload.get('code') if isinstance(payload, dict) else '?'}"
            )
            return []
        songs = payload.get("songs") or []
        candidates = [item for item in (self._candidate_from_song(song) for song in songs) if item]
        logger.debug(
            f"【多源歌词】netease 歌手 {artist}（{artist_id}）歌曲 {len(candidates)} 条"
        )
        return candidates

    def _resolve_artist_id(self, artist: str, deadline: float) -> Optional[str]:
        """用网易搜索的歌手类型（type=100）解析艺术家 ID。"""
        payload = self._weapi_post(
            _SEARCH_URL,
            {
                "s": artist,
                "type": 100,
                "offset": 0,
                "total": True,
                "limit": 10,
                "csrf_token": "",
                "crypto": "weapi",
            },
            deadline,
        )
        if not isinstance(payload, dict) or payload.get("code") != 200:
            return None
        artists = ((payload.get("result") or {}).get("artists")) or []
        expected = normalize_text(artist)
        exact: Optional[dict[str, Any]] = None
        contains: Optional[dict[str, Any]] = None
        first: Optional[dict[str, Any]] = None
        for item in artists:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            if first is None:
                first = item
            name = normalize_text(item.get("name"))
            if name == expected:
                exact = item
                break
            if contains is None and expected and (expected in name or name in expected):
                contains = item
        chosen = exact or contains or first
        return str(chosen["id"]) if chosen else None

    # ------------------------------------------------------------------ #
    # 取词
    # ------------------------------------------------------------------ #
    def _fetch_lyric(self, song_id: str, deadline: float) -> Optional[tuple[str, Optional[str]]]:
        """按歌曲 ID 取原文与翻译轨。"""
        try:
            payload = self._weapi_post(
                _LYRIC_URL,
                {
                    "os": "pc",
                    "id": int(song_id),
                    "lv": -1,
                    "kv": -1,
                    "tv": -1,
                    "rv": -1,
                    "crypto": "api",
                },
                deadline,
            )
        except DeadlineExceeded:
            raise
        except ProviderError as error:
            logger.warning(f"【多源歌词】netease 取词失败 id={song_id}：{error}")
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("code") != 200:
            logger.warning(
                f"【多源歌词】netease 取词失败 id={song_id}：code={payload.get('code')}"
            )
            return None
        lrc = ((payload.get("lrc") or {}).get("lyric")) or ""
        tlyric = ((payload.get("tlyric") or {}).get("lyric")) or ""
        if not str(lrc).strip():
            logger.debug(f"【多源歌词】netease 无歌词 id={song_id}")
            return None
        return str(lrc), (str(tlyric) if str(tlyric).strip() else None)
