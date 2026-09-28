"""推薦引擎的薄介面(design.md D7)。

``get_engine()`` 依 ``settings.RECOMMENDATION_ENGINE`` 回傳引擎實例;目前只有
``perplexity``。未知值(含舊的 ``google_places_gemini``)丟 ``ImproperlyConfigured``,
並由 ``RecommendationsConfig.ready()`` 呼叫 ``check_settings()`` 在啟動時就失敗,
不靜默 fallback。
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .base import (
    EngineError,
    NoUsableResults,
    RecommendationContext,
    RecommendationEngine,
    RecommendationResult,
    UpstreamConnectionError,
    UpstreamHTTPError,
    UpstreamInvalidResponse,
    UpstreamTimeout,
)
from .perplexity import PerplexityEngine, parse_model_setting

__all__ = [
    "EngineError",
    "NoUsableResults",
    "PerplexityEngine",
    "RecommendationContext",
    "RecommendationEngine",
    "RecommendationResult",
    "UpstreamConnectionError",
    "UpstreamHTTPError",
    "UpstreamInvalidResponse",
    "UpstreamTimeout",
    "check_settings",
    "get_engine",
]


_ENGINES = {"perplexity": PerplexityEngine}


def _engine_class():
    name = settings.RECOMMENDATION_ENGINE
    try:
        return _ENGINES[name]
    except (KeyError, TypeError):
        raise ImproperlyConfigured(
            f"RECOMMENDATION_ENGINE must be one of {sorted(_ENGINES)}, got {name!r}"
        ) from None


def get_engine() -> RecommendationEngine:
    return _engine_class()()


def check_settings():
    """啟動時檢查引擎設定(D7/D8)。API key 未設定不在此檢查(D12:回 503,不影響啟動)。"""
    if _engine_class() is PerplexityEngine:
        parse_model_setting(settings.PERPLEXITY_MODEL)
