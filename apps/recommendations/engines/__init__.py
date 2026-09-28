"""推薦引擎的薄介面(design.md D7)。

目前只放額度查詢需要的最小版本:``get_engine()`` + ``is_available()``。
``recommend()``、例外階層、依 ``RECOMMENDATION_ENGINE`` 選擇引擎與
``AppConfig.ready()`` 的未知值檢查在 tasks.md 2.2 / 4.2 補上;在那之前
``get_engine()`` 固定回傳 Perplexity 引擎。
"""

from .base import RecommendationEngine
from .perplexity import PerplexityEngine

__all__ = ["RecommendationEngine", "PerplexityEngine", "get_engine"]


def get_engine() -> RecommendationEngine:
    return PerplexityEngine()
