import logging
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.events.lifecycle import compute_display_status
from apps.events.views import _get_event_or_404
from config.exceptions import ApiError, Gone

from .engines import (
    EngineError,
    NoUsableResults,
    RecommendationContext,
    UpstreamTimeout,
    get_engine,
)
from .models import RestaurantRecommendationRequest
from .preferences import resolve_preferences
from .quota import PENDING_EXPIRY, compute_quota, count_used, quota_period_for
from .serializers import RecommendationRequestSerializer, flatten_errors

logger = logging.getLogger(__name__)

Status = RestaurantRecommendationRequest.Status


def serialize_quota(quota):
    """額度的 API 形狀。推薦成功回應的 ``quota`` 欄位也用這個(spec「查詢當月額度」)。"""
    return {
        "period": quota.period,
        "limit": quota.limit,
        "used": quota.used,
        "remaining": quota.remaining,
        "available": quota.available,
        "resetsAt": quota.resets_at.isoformat(),
    }


class AIRecommendationQuotaView(APIView):
    """``GET /api/me/ai-recommendation-quota/`` — 已登入使用者當月的 AI 推薦額度。

    掛在 config/urls.py,與 ``api/me/`` 並列(design.md D1)。外部服務未設定時仍回
    200,只把 ``serviceAvailable`` 設為 false。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        body = serialize_quota(compute_quota(request.user))
        body["serviceAvailable"] = get_engine().is_available()
        return Response(body)


# ---------------------------------------------------------------------------
# POST /api/events/{id}/restaurant-recommendations/
# ---------------------------------------------------------------------------

ERROR_DETAIL_MAX_LENGTH = 2000

# 條件式 update 更新 0 列(紀錄在請求期間因活動或使用者刪除被 cascade 刪除)時,
# log 帶的 error_code(design.md D4)。不是 DB 的 ErrorCode——此時已沒有紀錄可寫。
RECORD_MISSING = "RECORD_MISSING"

_NOT_FINALIZED_STATUSES = {"voting_open", "voting_closed_pending", "cancelled"}

# EngineError 子類別 → (HTTP 狀態, API code, 訊息)。
_ENGINE_ERROR_RESPONSES = {
    UpstreamTimeout: (504, "AI_RECOMMENDATION_UPSTREAM_TIMEOUT", "AI 推薦服務回應逾時，請稍後再試"),
    NoUsableResults: (
        502,
        "AI_RECOMMENDATION_UPSTREAM_FAILED",
        "找不到符合條件的餐廳，請調整條件後再試",
    ),
}
_DEFAULT_ENGINE_ERROR_RESPONSE = (
    502,
    "AI_RECOMMENDATION_UPSTREAM_FAILED",
    "AI 推薦服務暫時無法使用，請稍後再試",
)


def _check_event_recommendable(event):
    """D2 第 4 步:只有已定案、定案日期未過、連結未失效的活動可以推薦。"""
    display_status = compute_display_status(
        event.status,
        event.response_deadline,
        event.finalized_at,
        event.cancelled_at,
        event.final_slot.date if event.final_slot else None,
        timezone.now(),
    )
    if display_status in _NOT_FINALIZED_STATUSES:
        raise ApiError(
            "活動尚未定案，無法取得餐廳推薦", code="EVENT_NOT_FINALIZED", status_code=409
        )
    if display_status == "finalized_past":
        raise ApiError(
            "聚會日期已過，無法取得餐廳推薦", code="EVENT_ALREADY_PAST", status_code=409
        )
    if display_status == "link_expired":
        raise Gone("此活動連結已失效（活動結束超過7天）", code="LINK_EXPIRED")


def _usage_number(usage, *path):
    value = usage
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _log_extra(event_name, record, **fields):
    return {
        "event": event_name,
        "request_id": str(record.id),
        "user_id": str(record.user_id),
        "event_id": record.event_id,
        **fields,
    }


class RestaurantRecommendationView(APIView):
    """``POST /api/events/{id}/restaurant-recommendations/`` — 活動擁有者取得 AI 餐廳推薦。

    檢查順序見 design.md D2(1–7 全部在建立任何紀錄之前);預留 → 呼叫 → 確認/釋放
    見 D4:``pending`` 紀錄本身就是預留,呼叫上游時不持有 transaction;失敗的補償是
    把 ``pending`` 轉成 ``failed``(不再計數)。狀態轉換用
    ``filter(pk=..., status=pending).update(...)``,確保只會轉換一次。

    額度上限(403)與同活動進行中(409)在 ``_reserve`` 的 ``User`` 列鎖內檢查。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        user = request.user
        event = _get_event_or_404(id)
        if user != event.owner:
            raise PermissionDenied("僅活動擁有者可取得餐廳推薦")
        _check_event_recommendable(event)

        engine = get_engine()
        if not engine.is_available():
            logger.error(
                "ai_rec.unavailable",
                extra={
                    "event": "ai_rec.unavailable",
                    "user_id": str(user.id),
                    "event_id": event.id,
                },
            )
            raise ApiError(
                "AI 推薦服務目前無法使用", code="AI_RECOMMENDATION_UNAVAILABLE", status_code=503
            )

        serializer = RecommendationRequestSerializer(data=request.data)
        if not serializer.is_valid():
            raise ValidationError(flatten_errors(serializer.errors))
        preferences = resolve_preferences(event, serializer.validated_data)

        now = timezone.now()
        record = self._reserve(user, event, preferences, engine.model_name, now)

        started = time.monotonic()
        try:
            result = engine.recommend(RecommendationContext(preferences=preferences))
        except EngineError as exc:
            return self._handle_engine_error(record, exc, started)
        except Exception as exc:
            self._mark_failed(
                record,
                "UNEXPECTED_ERROR",
                f"{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise

        try:
            latency_ms = _elapsed_ms(started)
            result_body = {"restaurants": result.restaurants, "notes": result.notes}
            # savepoint:寫入失敗時只回滾這一步,外層若有 transaction 仍可繼續
            # 把紀錄標成 failed。
            with transaction.atomic():
                updated = RestaurantRecommendationRequest.objects.filter(
                    pk=record.pk, status=Status.PENDING
                ).update(
                    status=Status.SUCCEEDED,
                    result=result_body,
                    usage=result.usage,
                    model=result.model,
                    latency_ms=latency_ms,
                    completed_at=timezone.now(),
                )
        except Exception as exc:
            # 上游成功但結果寫不進 DB(例如無法序列化)——不可殘留 pending 佔用額度。
            self._mark_failed(
                record,
                "UNEXPECTED_ERROR",
                f"{type(exc).__name__}: {exc}",
                started,
                level=logging.ERROR,
            )
            raise
        success_fields = {
            "latency_ms": latency_ms,
            "restaurant_count": len(result.restaurants),
            "total_tokens": _usage_number(result.usage, "total_tokens"),
            "cost_usd": _usage_number(result.usage, "cost", "total_cost"),
            "model": result.model,
        }
        if updated:
            logger.info(
                "ai_rec.succeeded",
                extra=_log_extra("ai_rec.succeeded", record, **success_fields),
            )
        else:
            # 紀錄已被 cascade 刪除:仍回 201 與結果,本次不計次(D4 可接受的極端情況)。
            logger.warning(
                "ai_rec.succeeded",
                extra=_log_extra(
                    "ai_rec.succeeded", record, error_code=RECORD_MISSING, **success_fields
                ),
            )
        return Response(
            {
                "id": str(record.id),
                **result_body,
                "resolvedPreferences": preferences,
                "quota": serialize_quota(compute_quota(user, now=now)),
            },
            status=status.HTTP_201_CREATED,
        )

    def _reserve(self, user, event, preferences, model_name, now):
        """D4 預留:以 ``User`` 列鎖序列化同一使用者的「檢查 + 建立 pending」。

        鎖只包住這一小段(呼叫上游時不持有鎖與 transaction)。同一使用者的後續請求
        在鎖內計數時一定看得到這筆 ``pending``,所以不會超用;不同使用者鎖的是不同
        列,互不阻擋。鎖 ``User`` 列而不是紀錄表:額度為 0 筆時紀錄表沒有列可鎖。
        """
        period = quota_period_for(now)
        with transaction.atomic():
            User.objects.select_for_update().only("pk").get(pk=user.pk)
            in_progress = RestaurantRecommendationRequest.objects.filter(
                user=user,
                event=event,
                status=Status.PENDING,
                created_at__gt=now - PENDING_EXPIRY,
            ).exists()
            if in_progress:
                logger.info(
                    "ai_rec.in_progress_denied",
                    extra={
                        "event": "ai_rec.in_progress_denied",
                        "user_id": str(user.id),
                        "event_id": event.id,
                    },
                )
                raise ApiError(
                    "此活動已有進行中的推薦請求，請稍候",
                    code="AI_RECOMMENDATION_IN_PROGRESS",
                    status_code=409,
                )
            limit = settings.AI_RECOMMENDATION_QUOTA_PER_USER
            used = count_used(user, now=now)
            if used >= limit:
                logger.info(
                    "ai_rec.quota_denied",
                    extra={
                        "event": "ai_rec.quota_denied",
                        "user_id": str(user.id),
                        "used": used,
                        "limit": limit,
                        "period": period,
                    },
                )
                raise ApiError(
                    "本月 AI 推薦次數已用完",
                    code="AI_RECOMMENDATION_QUOTA_EXCEEDED",
                    status_code=403,
                )
            return RestaurantRecommendationRequest.objects.create(
                user=user,
                event=event,
                quota_period=period,
                status=Status.PENDING,
                preferences=preferences,
                model=model_name,
                created_at=now,
            )

    def _handle_engine_error(self, record, exc, started):
        detail = exc.raw_detail if exc.raw_detail is not None else str(exc)
        self._mark_failed(
            record,
            exc.error_code,
            detail,
            started,
            level=logging.WARNING,
            upstream_status=getattr(exc, "status", None),
            exc=exc,
        )
        http_status, code, message = next(
            (resp for cls, resp in _ENGINE_ERROR_RESPONSES.items() if isinstance(exc, cls)),
            _DEFAULT_ENGINE_ERROR_RESPONSE,
        )
        body = {"message": message, "code": code}
        if isinstance(exc, NoUsableResults):
            body["notes"] = exc.notes
        return Response(body, status=http_status)

    def _mark_failed(
        self, record, error_code, detail, started, *, level, upstream_status=None, exc=None
    ):
        latency_ms = _elapsed_ms(started)
        updated = RestaurantRecommendationRequest.objects.filter(
            pk=record.pk, status=Status.PENDING
        ).update(
            status=Status.FAILED,
            error_code=error_code,
            error_detail=str(detail)[:ERROR_DETAIL_MAX_LENGTH],
            latency_ms=latency_ms,
            completed_at=timezone.now(),
        )
        if not updated:
            # 紀錄已被 cascade 刪除:照常回錯誤,log 改帶 RECORD_MISSING 且至少 WARNING
            # (非預期例外維持 ERROR,不降級)。
            error_code = RECORD_MISSING
            level = max(level, logging.WARNING)
        logger.log(
            level,
            "ai_rec.failed",
            exc_info=True if exc is None else (type(exc), exc, exc.__traceback__),
            extra=_log_extra(
                "ai_rec.failed",
                record,
                error_code=error_code,
                latency_ms=latency_ms,
                upstream_status=upstream_status,
            ),
        )


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)
