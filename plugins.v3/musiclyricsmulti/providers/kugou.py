"""
酷狗音乐歌词源。

三步链路：
1. ``songsearch.kugou.com/song_search_v2`` 搜索，拿 ``FileHash``；
2. ``lyrics.kugou.com/search`` 用 hash 换 ``id + accesskey``；
3. ``lyrics.kugou.com/download`` 取 base64 LRC，校验 ``status == 200``。

``lyrics.kugou.com`` 按文档优先 https，失败回退 http。
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from ..httpclient import DeadlineExceeded, ProviderError
from ..log import logger
from ..match import rank_candidates, split_artists, to_optional_int
from ..models import Candidate, LyricsQuery, ProviderLyrics
from .base import BaseProvider

_SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
_TOKEN_URLS = (
    "https://lyrics.kugou.com/search",
    "http://lyrics.kugou.com/search",
)
_DOWNLOAD_URLS = (
    "https://lyrics.kugou.com/download",
    "http://lyrics.kugou.com/download",
)
_SEARCH_LIMIT = 10
_MAX_ATTEMPTS = 3


def _duration_seconds(value: Any) -> Optional[int]:
    """酷狗搜索返回秒，换票候选返回毫秒；按量级归一为整数秒。"""
    raw = to_optional_int(value)
    if raw is None:
        return None
    return round(raw / 1000) if raw > 1000 else raw


class KugouProvider(BaseProvider):
    """酷狗音乐（kugou）。"""

    name = "kugou"
    priority = 10

    def fetch(self, query: LyricsQuery, deadline: float) -> Optional[ProviderLyrics]:
        """搜索候选，逐个尝试换票下载。"""
        candidates = self._search(query, deadline)
        ranked = rank_candidates(query, candidates)

        for scored in ranked[:_MAX_ATTEMPTS]:
            cached = self._cached(scored)
            if cached:
                return cached
            original = self._fetch_lyric(scored.candidate, query, deadline)
            if original is None:
                continue
            result = self._finish(scored, original, None)
            if result:
                return result
        if not ranked:
            logger.debug(f"【多源歌词】kugou 无合适候选：{query.title}")
        return None

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def _search(self, query: LyricsQuery, deadline: float) -> list[Candidate]:
        """调用 song_search_v2 并转换为候选；零结果时退回只搜歌名。

        新歌/中文歌手的「歌名 + 歌手」组合常搜不到，单独歌名反而能命中。
        """
        terms = []
        for variant in query.title_variants:
            if query.primary_artist:
                terms.append(f"{variant} {query.primary_artist}")
            terms.append(variant)
        merged: list[Candidate] = []
        seen: set[str] = set()
        for term in dict.fromkeys(terms):
            for candidate in self._search_term(term, deadline):
                if candidate.provider_id in seen:
                    continue
                seen.add(candidate.provider_id)
                merged.append(candidate)
            if merged:
                break
        return merged

    def _search_term(self, term: str, deadline: float) -> list[Candidate]:
        """用单个检索词调用 song_search_v2 并转换为候选。"""
        try:
            payload = self.http.get_json(
                _SEARCH_URL,
                params={
                    "filter": 2,
                    "platform": "WebFilter",
                    "keyword": term,
                    "pagesize": _SEARCH_LIMIT,
                    "page": 1,
                },
                deadline=deadline,
            )
        except DeadlineExceeded:
            raise
        except ProviderError as error:
            logger.warning(f"【多源歌词】kugou 搜索失败：{error}")
            return []
        if not isinstance(payload, dict):
            logger.warning("【多源歌词】kugou 搜索响应结构异常")
            return []
        if payload.get("error_code") not in (0, None):
            logger.warning(
                f"【多源歌词】kugou 搜索失败：error_code={payload.get('error_code')}"
            )
            return []

        lists = ((payload.get("data") or {}).get("lists")) or []
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for item in lists:
            if not isinstance(item, dict):
                continue
            file_hash = str(item.get("FileHash") or "").strip()
            if not file_hash or file_hash in seen:
                continue
            seen.add(file_hash)
            file_name = str(item.get("FileName") or "").strip()
            title = file_name
            artist_hint = ""
            if " - " in file_name:
                artist_hint, _, title = file_name.partition(" - ")
            title = title.strip()
            if not title:
                continue
            artists = split_artists(item.get("SingerName")) or split_artists(artist_hint)
            candidates.append(
                Candidate(
                    title=title,
                    artists=artists,
                    album=str(item.get("AlbumName") or "").strip(),
                    duration=_duration_seconds(item.get("Duration")),
                    provider_id=file_hash,
                    extra={"file_hash": file_hash},
                )
            )
        logger.debug(f"【多源歌词】kugou 搜索 {term!r} 返回 {len(candidates)} 条")
        return candidates

    # ------------------------------------------------------------------ #
    # 换票 + 下载
    # ------------------------------------------------------------------ #
    def _fetch_lyric(
        self,
        candidate: Candidate,
        query: LyricsQuery,
        deadline: float,
    ) -> Optional[str]:
        """用搜索候选的 FileHash 换票并下载 LRC。"""
        file_hash = str(candidate.extra.get("file_hash") or candidate.provider_id)
        token_payload = self._get_json_with_fallback(
            _TOKEN_URLS,
            {
                "ver": 1,
                "man": "yes",
                "client": "mobi",
                "hash": file_hash,
            },
            deadline,
            label="换票",
        )
        if token_payload is None:
            return None
        token_candidate = self._pick_token_candidate(token_payload, query)
        if token_candidate is None:
            logger.debug(f"【多源歌词】kugou 换票无候选 hash={file_hash}")
            return None

        lyric_id = str(token_candidate.provider_id)
        accesskey = str(token_candidate.extra.get("accesskey") or "")
        download_payload = self._get_json_with_fallback(
            _DOWNLOAD_URLS,
            {
                "ver": 1,
                "client": "iphone",
                "fmt": "lrc",
                "charset": "utf8",
                "id": lyric_id,
                "accesskey": accesskey,
            },
            deadline,
            label="下载",
        )
        if not isinstance(download_payload, dict):
            return None
        status = download_payload.get("status")
        if str(status) != "200":
            logger.warning(
                f"【多源歌词】kugou 下载失败 hash={file_hash} id={lyric_id}：status={status}"
            )
            return None
        content = download_payload.get("content") or ""
        if not str(content).strip():
            logger.debug(f"【多源歌词】kugou 下载内容为空 id={lyric_id}")
            return None
        try:
            decoded = base64.b64decode(str(content), validate=False).decode(
                "utf-8", errors="ignore"
            )
        except (ValueError, TypeError) as error:
            logger.warning(f"【多源歌词】kugou base64 解码失败 id={lyric_id}：{error}")
            return None
        if not decoded.strip():
            return None
        logger.debug(
            f"【多源歌词】kugou 下载成功 hash={file_hash} id={lyric_id} "
            f"长度={len(decoded)}"
        )
        return decoded

    def _get_json_with_fallback(
        self,
        urls: tuple[str, ...],
        params: dict[str, Any],
        deadline: float,
        label: str,
    ) -> Optional[Any]:
        """按顺序尝试 https/http，全部失败时记 warning 并返回 None。"""
        last_error: Optional[ProviderError] = None
        for index, url in enumerate(urls):
            try:
                return self.http.get_json(url, params=params, deadline=deadline)
            except DeadlineExceeded:
                raise
            except ProviderError as error:
                last_error = error
                logger.debug(
                    f"【多源歌词】kugou {label} {url} 失败（{index + 1}/{len(urls)}）：{error}"
                )
        if last_error is not None:
            logger.warning(f"【多源歌词】kugou {label}全部失败：{last_error}")
        return None

    @staticmethod
    def _pick_token_candidate(payload: Any, query: LyricsQuery) -> Optional[Candidate]:
        """从换票结果中挑选与查询最匹配的候选；没有更好依据时取第一条。"""
        if not isinstance(payload, dict) or str(payload.get("status")) != "200":
            return None
        raw_candidates = payload.get("candidates") or []
        token_candidates: list[Candidate] = []
        for item in raw_candidates:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            token_candidates.append(
                Candidate(
                    title=str(item.get("song") or "").strip(),
                    artists=split_artists(item.get("singer")),
                    album="",
                    duration=_duration_seconds(item.get("duration")),
                    provider_id=str(item.get("id")),
                    extra={
                        "accesskey": str(item.get("accesskey") or ""),
                        "hash": str(item.get("hash") or ""),
                    },
                )
            )
        if not token_candidates:
            return None
        ranked = rank_candidates(query, token_candidates)
        if ranked:
            return ranked[0].candidate
        # 多个换票候选但都无法与查询对齐时不做“取第一条”的猜测；
        # 只有一个候选时不存在歧义，可以继续尝试下载。
        if len(token_candidates) == 1:
            return token_candidates[0]
        logger.debug(f"【多源歌词】kugou 换票候选均未通过匹配：{query.title}")
        return None
