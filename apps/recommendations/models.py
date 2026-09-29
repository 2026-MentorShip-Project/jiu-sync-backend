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


class EventRestaurantSelection(models.Model):
    """主揪從推薦結果選定、綁定到活動的餐廳(design.md D13)。

    每個活動最多一筆(OneToOne);換一間就覆蓋同一列,不保留歷史(分析改用
    ``ai_rec.restaurant_selected`` log)。``restaurant`` 是選定當下從推薦紀錄
    ``result`` 複製的快照,活動詳情直接讀它,不解析推薦 JSON。

    events app 只透過 ``related_name="restaurant_selection"`` 字串讀取本表,
    不 import 本模組(避免循環依賴)。
    """

    event = models.OneToOneField(
        "events.Event", on_delete=models.CASCADE, related_name="restaurant_selection"
    )
    # 來源推薦紀錄(追溯用)。
    recommendation = models.ForeignKey(
        RestaurantRecommendationRequest,
        on_delete=models.CASCADE,
        related_name="selections",
    )
    # 該次推薦內的餐廳 `id`(`r1`…`r5`),可分析主揪選第幾名。
    restaurant_ref = models.CharField(max_length=20)
    # 選定當下複製的完整餐廳物件(與推薦回應同 camelCase 形狀)。
    restaurant = models.JSONField()
    # 首次選定 / 最後一次換選;由 view 以同一個 `now` 明確指定,
    # 首次選定時兩者相等。
    selected_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.event_id} {self.restaurant_ref}"
