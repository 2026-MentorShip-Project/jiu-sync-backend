from django.apps import AppConfig


class RecommendationsConfig(AppConfig):
    name = 'apps.recommendations'
    label = 'recommendations'

    def ready(self):
        # 引擎設定不合法(未知 RECOMMENDATION_ENGINE、PERPLEXITY_MODEL 格式錯誤)時
        # 啟動即失敗(design.md D7/D8)。
        from .engines import check_settings

        check_settings()
