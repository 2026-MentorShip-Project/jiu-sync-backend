"""推薦引擎的薄介面(design.md D7)。

``get_engine()`` 目前固定回傳 Perplexity 引擎;依 ``RECOMMENDATION_ENGINE`` 選擇
與 ``AppConfig.ready()`` 的未知值檢查在 tasks.md 4.2 補上。
"""

from .base import (
    EngineError,
    NoUsableResults,
    RecommendationContext,
    RecommendationEngine,
    RecommendationResult,
    UpstreamHTTPError,
    UpstreamInvalidResponse,
    UpstreamTimeout,
)
from .perplexity import PerplexityEngine

__all__ = [
    "EngineError",
    "NoUsableResults",
    "PerplexityEngine",
    "RecommendationContext",
    "RecommendationEngine",
    "RecommendationResult",
    "UpstreamHTTPError",
    "UpstreamInvalidResponse",
    "UpstreamTimeout",
    "get_engine",
]


def get_engine() -> RecommendationEngine:
    return PerplexityEngine()
