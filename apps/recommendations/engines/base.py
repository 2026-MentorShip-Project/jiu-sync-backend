class RecommendationEngine:
    """所有推薦引擎的共同介面(D7)。``recommend()`` 在 tasks.md 2.2 補上。"""

    def is_available(self) -> bool:
        raise NotImplementedError
