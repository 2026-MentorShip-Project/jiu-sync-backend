import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class RestaurantRecommendationRequest(models.Model):
    """一次 AI 餐廳推薦呼叫的紀錄——同時是額度計次來源與稽核紀錄。

    額度不另存計數器,每次由本表推導(見 quota.py 與
    openspec/changes/add-ai-restaurant-recommendation/design.md D3):
    ``used = succeeded(當月) + pending(當月, 建立未滿 5 分鐘)``。
    ``pending`` 本身就是「預留」,失敗的補償是轉成 ``failed``(不再計數)。
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        SUCCEEDED = "succeeded"
        FAILED = "failed"

    class ErrorCode(models.TextChoices):
        UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
        UPSTREAM_HTTP_ERROR = "UPSTREAM_HTTP_ERROR"
        UPSTREAM_CONNECTION_ERROR = "UPSTREAM_CONNECTION_ERROR"
        UPSTREAM_INVALID_RESPONSE = "UPSTREAM_INVALID_RESPONSE"
        NO_USABLE_RESULTS = "NO_USABLE_RESULTS"
        UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
        # 上游已成功,但確認時自己的 pending 已過期且該月額度已滿(D4 確認時兜底);
        # result/usage 仍保存。
        QUOTA_EXCEEDED_AT_CONFIRM = "QUOTA_EXCEEDED_AT_CONFIRM"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    event = models.ForeignKey(
        "events.Event", on_delete=models.CASCADE, related_name="restaurant_recommendations"
    )
    # `YYYY-MM`,建立當下以 Asia/Taipei 計算;請求歸屬建立時的月份(D6),
    # 跨午夜才完成也不改。
    quota_period = models.CharField(max_length=7)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    # 解析後實際送出的條件(即回應的 resolvedPreferences)。
    preferences = models.JSONField()
    # 成功時回傳給前端的 restaurants + notes。
    result = models.JSONField(null=True, blank=True)
    error_code = models.CharField(
        max_length=32, choices=ErrorCode.choices, null=True, blank=True
    )
    # 失敗時上游原始回應(或例外訊息)前 2,000 字。
    error_detail = models.TextField(null=True, blank=True)
    model = models.CharField(max_length=100)
    # 上游 usage 原樣保存(token、cost)。
    usage = models.JSONField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)
    # 用 default 而非 auto_now_add:pending 過期與月份歸屬都以它為準,
    # 需要能由呼叫端(與測試)明確指定同一個 `now`。
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["user", "quota_period", "status"], name="rec_req_user_period_status"
            ),
            models.Index(fields=["user", "event", "status"], name="rec_req_user_event_status"),
        ]

    def __str__(self):
        return f"{self.user_id} {self.quota_period} {self.status}"
