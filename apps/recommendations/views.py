import logging
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.events.lifecycle import compute_display_status
from apps.events.models import Event
from apps.events.views import _get_event_or_404
from config.exceptions import ApiError, Gone

from .engines import (
    EngineError,
    NoUsableResults,
    RecommendationContext,
    UpstreamTimeout,
    get_engine,
)
from .models import EventRestaurantSelection, RestaurantRecommendationRequest
from .preferences import resolve_preferences
from .quota import (
    PENDING_EXPIRY,
    compute_quota,
    count_used,
    exceeds_quota_at_confirm,
    quota_period_for,
)
from .serializers import (
    RecommendationRequestSerializer,
    RestaurantSelectionSerializer,
    flatten_errors,
)

logger = logging.getLogger(__name__)

Status = RestaurantRecommendationRequest.Status
ErrorCode = RestaurantRecommendationRequest.ErrorCode


def serialize_quota(quota, *, service_available):
    """額度的 API 形狀。推薦成功回應的 ``quota`` 欄位也用這個(spec「查詢當月額度」)。

    兩個端點的欄位集合必須完全相同,所以 ``serviceAvailable`` 一律在這裡組出,
    呼叫端只提供值(task 4.6)。
    """
    return {
        "period": quota.period,
        "limit": quota.limit,
        "used": quota.used,
        "remaining": quota.remaining,
        "available": quota.available,
        "resetsAt": quota.resets_at.isoformat(),
        "serviceAvailable": service_available,
    }


class AIRecommendationQuotaView(APIView):
    """``GET /api/me/ai-recommendation-quota/`` — 已登入使用者當月的 AI 推薦額度。

    掛在 config/urls.py,與 ``api/me/`` 並列(design.md D1)。外部服務未設定時仍回
    200,只把 ``serviceAvailable`` 設為 false。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            serialize_quota(
                compute_quota(request.user),
                service_available=get_engine().is_available(),
            )
        )


# ---------------------------------------------------------------------------
# POST /api/events/{id}/restaurant-recommendations/
# ---------------------------------------------------------------------------

ERROR_DETAIL_MAX_LENGTH = 2000

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


def _check_event_recommendable(event, action="取得餐廳推薦"):
    """D2 第 4 步:只有已定案、定案日期未過、連結未失效的活動可以推薦。

    選定推薦餐廳(D13 第 5 步)沿用同一組狀態規則,只換訊息中的動作文字。
    """
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
            f"活動尚未定案，無法{action}", code="EVENT_NOT_FINALIZED", status_code=409
        )
    if display_status == "finalized_past":
        raise ApiError(
            f"聚會日期已過，無法{action}", code="EVENT_ALREADY_PAST", status_code=409
        )
    if display_status == "link_expired":
        raise Gone("此活動連結已失效（活動結束超過7天）", code="LINK_EXPIRED")


def _quota_exceeded_error():
    """預留時與確認時(D4 確認時兜底)共用,兩者 body 必須完全相同。"""
    return ApiError(
        "本月 AI 推薦次數已用完",
        code="AI_RECOMMENDATION_QUOTA_EXCEEDED",
        status_code=403,
    )


def _lock_for_confirm(record):
    """確認時兜底(D4)的鎖:先鎖自己的紀錄、再鎖 ``User`` 列,``now`` 須在此之後取。

    順序必須與 Django cascade 刪除使用者一致(先刪依附紀錄、最後刪 ``User``),
    反過來會互鎖、確認端被 Postgres 中止而變成 500(task 5.4 code-review)。
    兩者都用 ``filter`` 而非 ``get``:紀錄或使用者在請求期間被刪除時不丟例外,
    沿用 ``record_missing`` 規則。額度仍由 ``User`` 列鎖序列化。
    """
    list(
        RestaurantRecommendationRequest.objects.select_for_update()
        .filter(pk=record.pk)
        .only("pk")
    )
    list(User.objects.select_for_update().filter(pk=record.user_id).only("pk"))


def _usage_number(usage, *path):
    value = usage
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _log_extra(event_name, record, *, record_missing=False, model_fallback=False, **fields):
    """`record_missing=True`:條件式 update 更新 0 列(紀錄在請求期間因活動或使用者
    刪除被 cascade 刪除)。log 的 event/等級/error_code 維持原路徑的值,只另帶布林
    `record_missing: true`(design.md D4);其他情況不輸出此欄位。

    `model_fallback=True`:上游未回報 `model`、引擎改用設定值(D8),只在
    `ai_rec.succeeded` 傳入,另帶布林 `model_fallback: true`;其他情況不輸出此欄位。"""
    extra = {
        "event": event_name,
        "request_id": str(record.id),
        "user_id": str(record.user_id),
        "event_id": record.event_id,
        **fields,
    }
    if record_missing:
        extra["record_missing"] = True
    if model_fallback:
        extra["model_fallback"] = True
    return extra


class RestaurantRecommendationView(APIView):
    """``POST /api/events/{id}/restaurant-recommendations/`` — 活動擁有者取得 AI 餐廳推薦。

    檢查順序見 design.md D2(1–7 全部在建立任何紀錄之前);預留 → 呼叫 → 確認/釋放
    見 D4:``pending`` 紀錄本身就是預留,呼叫上游時不持有 transaction;失敗的補償是
    把 ``pending`` 轉成 ``failed``(不再計數)。狀態轉換用
    ``filter(pk=..., status=pending).update(...)``,確保只會轉換一次。

    額度上限(403)與同活動進行中(409)在 ``_reserve`` 的 ``User`` 列鎖內檢查;
    轉 ``succeeded`` 前再於同一把鎖內做確認時兜底(D4):自己的 ``pending`` 已過期且
    該月額度已滿 → ``failed``/``QUOTA_EXCEEDED_AT_CONFIRM``,回相同的 403。
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
            # 寫入失敗時整段回滾(外層若有 transaction 則為 savepoint),之後由
            # except 把紀錄標成 failed。
            with transaction.atomic():
                _lock_for_confirm(record)
                confirmed_at = timezone.now()
                exceeded = exceeds_quota_at_confirm(record, now=confirmed_at)
                outcome = (
                    {"status": Status.FAILED, "error_code": ErrorCode.QUOTA_EXCEEDED_AT_CONFIRM}
                    if exceeded
                    else {"status": Status.SUCCEEDED}
                )
                # 超額時 result/usage 仍保存,方便追查(上游已收費)。
                updated = RestaurantRecommendationRequest.objects.filter(
                    pk=record.pk, status=Status.PENDING
                ).update(
                    **outcome,
                    result=result_body,
                    usage=result.usage,
                    model=result.model,
                    latency_ms=latency_ms,
                    completed_at=confirmed_at,
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
        cost_usd = _usage_number(result.usage, "cost", "total_cost")
        if exceeded:
            # pending 已過期、期間額度被用滿:不計次,回與一般額度用完相同的 403。
            logger.warning(
                "ai_rec.failed",
                extra=_log_extra(
                    "ai_rec.failed",
                    record,
                    record_missing=not updated,
                    error_code=ErrorCode.QUOTA_EXCEEDED_AT_CONFIRM.value,
                    latency_ms=latency_ms,
                    upstream_status=None,
                    cost_usd=cost_usd,
                ),
            )
            raise _quota_exceeded_error()
        success_fields = {
            "latency_ms": latency_ms,
            "restaurant_count": len(result.restaurants),
            "total_tokens": _usage_number(result.usage, "total_tokens"),
            "cost_usd": cost_usd,
            "model": result.model,
        }
        # 紀錄已被 cascade 刪除時仍回 201 與結果,本次不計次(D4 可接受的極端情況)。
        logger.info(
            "ai_rec.succeeded",
            extra=_log_extra(
                "ai_rec.succeeded",
                record,
                record_missing=not updated,
                model_fallback=result.model_fallback,
                **success_fields,
            ),
        )
        return Response(
            {
                "id": str(record.id),
                **result_body,
                "resolvedPreferences": preferences,
                # 走到成功路徑代表開頭 is_available() 已為 true。
                "quota": serialize_quota(compute_quota(user, now=now), service_available=True),
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
                raise _quota_exceeded_error()
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
        # 紀錄已被 cascade 刪除時照常回錯誤,log 保留原等級與 error_code。
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
                record_missing=not updated,
            ),
        )


# ---------------------------------------------------------------------------
# PUT /api/events/{id}/selected-restaurant/
# ---------------------------------------------------------------------------


def _find_restaurant(recommendation, restaurant_ref):
    result = recommendation.result if isinstance(recommendation.result, dict) else {}
    restaurants = result.get("restaurants")
    if not isinstance(restaurants, list):
        return None
    return next(
        (r for r in restaurants if isinstance(r, dict) and r.get("id") == restaurant_ref),
        None,
    )


def _serialize_selection(selection):
    datetime_field = serializers.DateTimeField()
    return {
        "recommendationId": str(selection.recommendation_id),
        "restaurantId": selection.restaurant_ref,
        "restaurant": selection.restaurant,
        "selectedAt": datetime_field.to_representation(selection.selected_at),
        "updatedAt": datetime_field.to_representation(selection.updated_at),
    }


class SelectedRestaurantView(APIView):
    """``PUT /api/events/{id}/selected-restaurant/`` — 主揪從推薦結果選定一間餐廳。

    檢查順序見 design.md D13:認證 → 活動存在 → 擁有者 → body 驗證 → 鎖內(狀態 →
    推薦紀錄 → 餐廳)→ 寫入。狀態檢查、驗證與寫入都在 ``Event`` 列的
    ``select_for_update`` 內:兩個分頁同時選不同間時由鎖序列化(最後寫入者勝、
    兩者皆 200),也不會與 reopen/cancel(同樣更新該列)競態。

    冪等:目前選擇已是同一 ``(recommendation, restaurant_ref)`` 時不寫入,
    ``updatedAt`` 不變、log ``is_change: false``。不修改 ``Event.location``。
    """

    permission_classes = [IsAuthenticated]

    def put(self, request, id):
        user = request.user
        event = _get_event_or_404(id)
        if user != event.owner:
            raise PermissionDenied("僅活動擁有者可選定餐廳")
        serializer = RestaurantSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recommendation_id = serializer.validated_data["recommendationId"]
        restaurant_ref = serializer.validated_data["restaurantId"]

        with transaction.atomic():
            # of=("self",):只鎖活動列;final_slot 是 nullable 外鍵(LEFT JOIN),
            # Postgres 不允許對 outer join 的 nullable 端加 FOR UPDATE。
            event = (
                Event.objects.select_for_update(of=("self",))
                .select_related("final_slot")
                .filter(pk=event.pk)
                .first()
            )
            if event is None:
                # 取得鎖之前活動已被刪除 → 與一開始就不存在相同的 404。
                _get_event_or_404(id)
            _check_event_recommendable(event, action="選定餐廳")

            # 不存在、屬於其他活動、未成功(含有 result 的 QUOTA_EXCEEDED_AT_CONFIRM)
            # 一律同一代碼,不透露其他活動的紀錄是否存在。
            recommendation = RestaurantRecommendationRequest.objects.filter(
                pk=recommendation_id, event=event, status=Status.SUCCEEDED
            ).first()
            if recommendation is None:
                raise ApiError(
                    "推薦紀錄無效，請重新取得推薦",
                    code="INVALID_RECOMMENDATION",
                    status_code=400,
                )
            restaurant = _find_restaurant(recommendation, restaurant_ref)
            if restaurant is None:
                raise ApiError(
                    "此餐廳不在該次推薦結果中", code="INVALID_RESTAURANT", status_code=400
                )

            current = EventRestaurantSelection.objects.filter(event=event).first()
            if (
                current is not None
                and current.recommendation_id == recommendation.id
                and current.restaurant_ref == restaurant_ref
            ):
                selection, is_change = current, False
            else:
                now = timezone.now()
                is_change = current is not None
                fields = {
                    "recommendation": recommendation,
                    "restaurant_ref": restaurant_ref,
                    "restaurant": restaurant,
                    "updated_at": now,
                }
                # selected_at 只在首次選定時寫入;換選只更新 updated_at。
                selection, _ = EventRestaurantSelection.objects.update_or_create(
                    event=event,
                    defaults=fields,
                    create_defaults={**fields, "selected_at": now},
                )

        logger.info(
            "ai_rec.restaurant_selected",
            extra={
                "event": "ai_rec.restaurant_selected",
                "user_id": str(user.id),
                "event_id": event.id,
                "request_id": str(recommendation.id),
                "restaurant_ref": restaurant_ref,
                "is_change": is_change,
            },
        )
        return Response(_serialize_selection(selection))


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)
