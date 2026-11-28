"""
歌词源抽象基类。

各 Provider 只负责「搜索 → 打分选优 → 取词 → 返回原文/翻译」，
限速、超时、重试、缓存与排序由插件层统一处理。
"""

from __future__ import annotations

from typing import Optional

from ..cache import TTLCache
from ..httpclient import HttpClient
from ..lrc import is_instrumental, parse_lrc
from ..log import logger
from ..match import ScoredCandidate
from ..models import LyricsQuery, LyricPayload, ProviderLyrics


class BaseProvider:
    """
    单个歌词源的公共逻辑。

    :param http: 插件共享的 HTTP 客户端
    :param cache: ``(provider, provider_id) -> LyricPayload`` 的歌词缓存
    """

    #: 源名称，写入 MusicLyrics.provider
    name = "base"
    #: 源优先级。宿主 LyricsChain 按 (quality_rank, match_score, provider_priority) 取最大值，
    #: 因此数值越大越优先；网易云带 tlyric 双语，排最高。
    priority = 100

    def __init__(self, http: HttpClient, cache: TTLCache) -> None:
        self.http = http
        self.cache = cache

    def fetch(self, query: LyricsQuery, deadline: float) -> Optional[ProviderLyrics]:
        """
        查询单个源，返回最可信的一条歌词。

        :param query: 标准化查询条件
        :param deadline: ``time.monotonic()`` 表示的单歌总预算截止点
        :return: 命中的歌词；无结果时返回 None
        :raises ProviderError: 网络或响应结构异常，由插件层隔离
        """
        raise NotImplementedError

    def _cached(self, scored: ScoredCandidate) -> Optional[ProviderLyrics]:
        """命中 ``(provider, provider_id)`` 缓存时直接返回。"""
        provider_id = scored.candidate.provider_id
        if not provider_id:
            return None
        payload = self.cache.get((self.name, provider_id))
        if not isinstance(payload, LyricPayload):
            return None
        logger.debug(
            f"【多源歌词】{self.name} 缓存命中：{payload.title}（{provider_id}）"
        )
        return ProviderLyrics(payload=payload, match_score=scored.score,
                              provider_priority=self.priority)

    def _finish(
        self,
        scored: ScoredCandidate,
        original_lrc: str,
        translation_lrc: Optional[str] = None,
    ) -> Optional[ProviderLyrics]:
        """
        校验取词结果、写缓存并包装为 ProviderLyrics。

        明确声明纯音乐或正文为空时返回 None，不伪造纯音乐候选。
        """
        candidate = scored.candidate
        original_text = str(original_lrc or "").strip()
        if not original_text:
            logger.debug(f"【多源歌词】{self.name} 无歌词：{candidate.title}")
            return None
        if is_instrumental(original_text):
            logger.debug(f"【多源歌词】{self.name} 明确为纯音乐，跳过：{candidate.title}")
            return None

        translation_text = str(translation_lrc or "").strip()
        # 有些源会返回“只有时间轴、没有译文”的占位轨（如网易云部分中文歌），
        # 这种轨不算翻译，避免日志与展示误报“有翻译”。
        if translation_text and not any(line.text for line in parse_lrc(translation_text)):
            translation_text = ""

        payload = LyricPayload(
            provider=self.name,
            provider_id=candidate.provider_id,
            title=candidate.title,
            original_lrc=original_text,
            translation_lrc=translation_text or None,
        )
        if candidate.provider_id:
            self.cache.set((self.name, candidate.provider_id), payload)
        logger.info(
            f"【多源歌词】{self.name} 命中：{candidate.title}"
            f"（{candidate.provider_id}，匹配分 {scored.score}，"
            f"翻译={'有' if payload.translation_lrc else '无'}）"
        )
        return ProviderLyrics(payload=payload, match_score=scored.score,
                              provider_priority=self.priority)
