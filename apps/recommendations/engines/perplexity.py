from django.conf import settings

from .base import RecommendationEngine


class PerplexityEngine(RecommendationEngine):
    """Perplexity Agent API 引擎。目前只有 ``is_available()``(tasks.md 1.2 最小版本)。"""

    def is_available(self) -> bool:
        # key 空字串(或只有空白)→ 未設定 → 推薦回 503、額度查詢 serviceAvailable false
        # (D12),不影響啟動。
        return bool((settings.PERPLEXITY_API_KEY or "").strip())
