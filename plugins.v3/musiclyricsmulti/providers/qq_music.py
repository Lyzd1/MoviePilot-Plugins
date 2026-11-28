"""
QQ 音乐歌词源。

- 搜索：``c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg``（搜索联想）
- 取词：``c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg``
- 响应是 JSONP（``MusicJsonCallback({...})``）；``lyric`` / ``trans``
  需要连续两次 HTML 反转义，且 ``trans`` 可能缺失或为 null。
"""

from __future__ import annotations

import html
import json
from typing import Any, Optional

from ..httpclient import DeadlineExceeded, ProviderError
from ..log import logger
from ..match import rank_candidates, split_artists
from ..models import Candidate, LyricsQuery, ProviderLyrics
from .base import BaseProvider

_SMARTBOX_URL = "https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg"
_LYRIC_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
_HEADERS = {"Referer": "https://y.qq.com/"}
_SEARCH_HEADERS = {**_HEADERS, "Accept": "application/json, text/plain, */*"}
_SEARCH_LIMIT = 10
_MAX_ATTEMPTS = 3


class QQMusicProvider(BaseProvider):
    """QQ 音乐（qqmusic）。"""

    name = "qqmusic"
    priority = 20

    def fetch(self, query: LyricsQuery, deadline: float) -> Optional[ProviderLyrics]:
        """搜索 songmid，按候选打分取词。"""
        candidates = self._search(query, deadline)
        if not candidates:
            # 搜索联想对「歌名+歌手」偶尔为空，退化为只搜歌名
            candidates = self._search(query, deadline, title_only=True)
        if not candidates:
            # 双语标题（「中文（English）」）逐个别名重试
            for variant in query.title_variants:
                if variant == query.title:
                    continue
                candidates = self._search_title(variant, deadline)
                if candidates:
                    break
        ranked = rank_candidates(query, candidates)

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
            logger.debug(f"【多源歌词】qqmusic 无合适候选：{query.title}")
        return None

    def _search_title(self, title: str, deadline: float) -> list[Candidate]:
        """用指定关键词调用搜索联想（供双语标题的别名重试使用）。"""
        return self._search_keyword(title, deadline)

    def _search_keyword(self, keyword: str, deadline: float) -> list[Candidate]:
        """按关键词请求 smartbox 并投影候选。"""
        query = LyricsQuery(title=keyword)
        return self._search(query, deadline, title_only=True)

    def _search(
        self,
        query: LyricsQuery,
        deadline: float,
        title_only: bool = False,
    ) -> list[Candidate]:
        """调用 smartbox 搜索联想并转换为候选。"""
        term = query.title
        if not title_only and query.primary_artist:
            term = f"{query.title} {query.primary_artist}"
        try:
            payload = self.http.get_json(
                _SMARTBOX_URL,
                params={
                    "format": "json",
                    "inCharset": "utf-8",
                    "outCharset": "utf-8",
                    "platform": "yqq.json",
                    "key": term,
                },
                headers=_SEARCH_HEADERS,
                deadline=deadline,
            )
        except DeadlineExceeded:
            raise
        except ProviderError as error:
            logger.warning(f"【多源歌词】qqmusic 搜索失败：{error}")
            return []
        if not isinstance(payload, dict):
            logger.warning("【多源歌词】qqmusic 搜索响应结构异常")
            return []
        if payload.get("code") not in (0, None):
            logger.warning(f"【多源歌词】qqmusic 搜索失败：code={payload.get('code')}")
            return []
        songs = ((((payload.get("data") or {}).get("song") or {}).get("itemlist")) or [])
        candidates: list[Candidate] = []
        for song in songs[:_SEARCH_LIMIT]:
            if not isinstance(song, dict):
                continue
            mid = str(song.get("mid") or "").strip()
            title = str(song.get("name") or "").strip()
            if not mid or not title:
                continue
            candidates.append(
                Candidate(
                    title=title,
                    artists=split_artists(song.get("singer")),
                    album="",
                    duration=None,
                    provider_id=mid,
                    extra={"songmid": mid},
                )
            )
        logger.debug(f"【多源歌词】qqmusic 搜索 {term!r} 返回 {len(candidates)} 条")
        return candidates

    def _fetch_lyric(
        self,
        songmid: str,
        deadline: float,
    ) -> Optional[tuple[str, Optional[str]]]:
        """按 songmid 取歌词，处理 JSONP 与双重 HTML 转义。"""
        try:
            text = self.http.get_text(
                _LYRIC_URL,
                params={
                    "nobase64": 1,
                    "songmid": songmid,
                    "platform": "yqq",
                    "inCharset": "utf8",
                    "outCharset": "utf-8",
                    "g_tk": 5381,
                },
                headers=_HEADERS,
                deadline=deadline,
            )
        except DeadlineExceeded:
            raise
        except ProviderError as error:
            logger.warning(f"【多源歌词】qqmusic 取词失败 mid={songmid}：{error}")
            return None
        try:
            payload = _parse_jsonp(text)
        except ProviderError as error:
            logger.warning(f"【多源歌词】qqmusic 响应解析失败 mid={songmid}：{error}")
            return None
        if not isinstance(payload, dict):
            return None

        retcode = payload.get("retcode")
        code = payload.get("code")
        if retcode not in (0, None) or code not in (0, None):
            logger.warning(
                f"【多源歌词】qqmusic 取词失败 mid={songmid}："
                f"retcode={retcode} code={code}"
            )
            return None

        lyric = _html_unescape_twice(payload.get("lyric"))
        trans = _html_unescape_twice(payload.get("trans"))
        if not lyric.strip():
            logger.debug(f"【多源歌词】qqmusic 无歌词 mid={songmid}")
            return None
        return lyric, (trans if trans.strip() else None)


def _parse_jsonp(text: str) -> Any:
    """
    解析 JSONP 响应。

    兼容 ``MusicJsonCallback({...})`` 与直接返回 JSON 两种情况。
    """
    content = str(text or "").strip()
    if not content:
        raise ProviderError("响应为空")
    if content.startswith("{"):
        try:
            return json.loads(content)
        except ValueError as error:
            raise ProviderError("JSON 解析失败") from error
    start = content.find("(")
    end = content.rfind(")")
    if start < 0 or end <= start:
        raise ProviderError("不是合法的 JSONP")
    try:
        return json.loads(content[start + 1:end])
    except ValueError as error:
        raise ProviderError("JSONP 内 JSON 解析失败") from error


def _html_unescape_twice(value: Any) -> str:
    """QQ 的歌词字段通常被 HTML 转义两次（``&amp;#39;``）。"""
    if value is None:
        return ""
    text = str(value)
    return html.unescape(html.unescape(text))
