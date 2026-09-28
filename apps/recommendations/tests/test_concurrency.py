"""額度併發安全與連點防護(tasks.md section 3;design.md D4/D5/D11)。

Seam:HTTP(DRF `APIClient`)。引擎以執行緒安全的 `ConcurrentFakeEngine` 替換 view
用的 `get_engine()`。

併發測試用 `@pytest.mark.django_db(transaction=True)` 讓每個執行緒拿到真正獨立的
DB connection(寫法比照 apps/events/tests/test_views.py 的
`test_comment_rate_limit_concurrent_legit_requests_only_one_succeeds`)。

為了讓競爭條件「每次都」重現、而不是靠運氣:`hold_after_count` 把 view 在鎖內用的
`count_used`(模組層級名稱 `apps.recommendations.views.count_used`)包一層,計數完後
在 `threading.Barrier` 等其他請求也算完(有逾時)。
- 有 `User` 列鎖時:第二個請求卡在 `select_for_update`,到不了計數,barrier 逾時
  後第一個請求才繼續——結果正確。
- 沒有鎖時:所有請求都先算出同一個 `used`、再一起建立 `pending`——必定超用。
所有等待都有逾時,實作有 bug 時測試會失敗而不是卡住。
"""

import logging
import threading
from datetime import timedelta

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from apps.events.models import Event
from apps.recommendations import quota as quota_module
from apps.recommendations import views as views_module
from apps.recommendations.engines import UpstreamTimeout
from apps.recommendations.models import RestaurantRecommendationRequest
from apps.recommendations.quota import quota_period_for
from apps.recommendations.tests.test_views import (
    _auth_client,
    _create_finalized_event,
    _create_user,
    _default_result,
    _events,
    _url,
)

Status = RestaurantRecommendationRequest.Status

HOLD_TIMEOUT = 1.0  # 鎖內 barrier 的等待上限(有鎖時必定逾時一次)
ENGINE_TIMEOUT = 5.0  # 假引擎等待另一個請求的上限
JOIN_TIMEOUT = 20.0


class ConcurrentFakeEngine:
    """執行緒安全的假引擎。`gate` 非 None 時,recommend 會等它被 set(有逾時)。"""

    model_name = "preset:low"

    def __init__(self, *, gate=None, side_effect=None, error=None):
        self.gate = gate
        self.side_effect = side_effect
        self.error = error
        self.calls = 0
        self._lock = threading.Lock()

    def is_available(self):
        return True

    def recommend(self, context):
        with self._lock:
            self.calls += 1
        if self.gate is not None:
            self.gate.wait(timeout=ENGINE_TIMEOUT)
        if self.side_effect is not None:
            self.side_effect()
        if self.error is not None:
            raise self.error
        return _default_result()


@pytest.fixture
def engine(monkeypatch):
    engine = ConcurrentFakeEngine()
    monkeypatch.setattr("apps.recommendations.views.get_engine", lambda: engine)
    return engine


class CountHold:
    """包住 view 的 `count_used`:算完後在 barrier 等 `parties` 個請求都算完。"""

    def __init__(self, parties, timeout):
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.calls = 0
        self.passed = 0
        self.broken = 0
        self._lock = threading.Lock()

    def __call__(self, user, *, now):
        used = quota_module.count_used(user, now=now)
        with self._lock:
            self.calls += 1
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            with self._lock:
                self.broken += 1
        else:
            with self._lock:
                self.passed += 1
        return used


@pytest.fixture
def hold_after_count(monkeypatch):
    def install(parties, timeout=HOLD_TIMEOUT):
        hold = CountHold(parties, timeout)
        # raising=False:section 3 實作前 view 還沒有這個名稱,RED 時要看到的是
        # 行為上的失敗,而不是 AttributeError。各測試最後會斷言 hold.calls,
        # 確保實作之後 view 確實經過這個 seam。
        monkeypatch.setattr(views_module, "count_used", hold, raising=False)
        return hold

    return install


def _seed(user, event, count, *, record_status=Status.SUCCEEDED, created_at=None):
    now = timezone.now()
    created_at = created_at or now
    RestaurantRecommendationRequest.objects.bulk_create(
        RestaurantRecommendationRequest(
            user=user,
            event=event,
            quota_period=quota_period_for(now),
            status=record_status,
            preferences={},
            model="preset:low",
            created_at=created_at,
        )
        for _ in range(count)
    )


def _succeeded_this_month(user):
    return RestaurantRecommendationRequest.objects.filter(
        user=user, status=Status.SUCCEEDED, quota_period=quota_period_for(timezone.now())
    ).count()


def _post_concurrently(jobs, *, on_response=None):
    """`jobs` 為 (user, event_id) 清單,同時送出,回傳 [(status_code, body), ...]。"""
    results = []
    results_lock = threading.Lock()
    errors = []
    start = threading.Barrier(len(jobs), timeout=JOIN_TIMEOUT)
    clients = [_auth_client(user) for user, _ in jobs]

    def send(client, event_id):
        try:
            start.wait()
            response = client.post(_url(event_id), {}, format="json")
            with results_lock:
                results.append((response.status_code, response.json()))
            if on_response is not None:
                on_response(response)
        except Exception as exc:  # noqa: BLE001 — 轉交主執行緒斷言,不吞掉
            with results_lock:
                errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=send, args=(client, event_id))
        for client, (_, event_id) in zip(clients, jobs, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads), "request thread hung"
    assert errors == []
    return results


# ---------------------------------------------------------------------------
# ① 額度用完 / ⑥ quota_denied log
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_quota_exhausted_returns_403_and_engine_not_called(engine, caplog):
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(user, event, 20)
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED"
    assert engine.calls == 0
    assert RestaurantRecommendationRequest.objects.count() == 20
    assert not RestaurantRecommendationRequest.objects.filter(status=Status.PENDING).exists()

    [record] = _events(caplog, "ai_rec.quota_denied")
    assert record.levelno == logging.INFO
    assert record.user_id == str(user.id)
    assert record.used == 20
    assert record.limit == 20
    assert record.period == quota_period_for(timezone.now())


@pytest.mark.django_db
def test_fresh_pending_occupies_quota_slot(engine):
    """19 次成功 + 1 筆未過期 pending(其他活動)= 已用 20 → 403。"""
    user = _create_user()
    event = _create_finalized_event(user)
    other = _create_finalized_event(user)
    _seed(user, event, 19)
    _seed(
        user,
        other,
        1,
        record_status=Status.PENDING,
        created_at=timezone.now() - timedelta(minutes=1),
    )

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED"
    assert engine.calls == 0


@pytest.mark.django_db
def test_nineteen_used_allows_exactly_one_more(engine):
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(user, event, 19)
    client = _auth_client(user)

    first = client.post(_url(event.id), {}, format="json")
    second = client.post(_url(event.id), {}, format="json")

    assert first.status_code == status.HTTP_201_CREATED
    assert first.json()["quota"]["used"] == 20
    assert second.status_code == status.HTTP_403_FORBIDDEN
    assert _succeeded_this_month(user) == 20
    assert engine.calls == 1


@pytest.mark.django_db
@override_settings(AI_RECOMMENDATION_QUOTA_PER_USER=0)
def test_zero_limit_denies_every_request(engine):
    user = _create_user()
    event = _create_finalized_event(user)

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED"
    assert engine.calls == 0
    assert not RestaurantRecommendationRequest.objects.exists()


@pytest.mark.django_db
def test_failed_records_do_not_count_toward_quota(engine):
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(user, event, 19)
    _seed(user, event, 5, record_status=Status.FAILED)

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# 同活動進行中 / ⑥ in_progress_denied log / ④ 殘留 pending
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_fresh_pending_on_same_event_returns_409_and_logs(engine, caplog):
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(
        user,
        event,
        1,
        record_status=Status.PENDING,
        created_at=timezone.now() - timedelta(minutes=4, seconds=50),
    )
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "AI_RECOMMENDATION_IN_PROGRESS"
    assert engine.calls == 0
    assert RestaurantRecommendationRequest.objects.count() == 1

    [record] = _events(caplog, "ai_rec.in_progress_denied")
    assert record.levelno == logging.INFO
    assert record.user_id == str(user.id)
    assert record.event_id == event.id
    assert _events(caplog, "ai_rec.quota_denied") == []


@pytest.mark.django_db
def test_in_progress_is_checked_before_quota(engine):
    """D4:鎖內先檢查同活動進行中,再檢查額度。"""
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(user, event, 19)
    _seed(
        user,
        event,
        1,
        record_status=Status.PENDING,
        created_at=timezone.now() - timedelta(minutes=1),
    )

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["code"] == "AI_RECOMMENDATION_IN_PROGRESS"


@pytest.mark.django_db
def test_stale_pending_on_same_event_does_not_block(engine):
    """④ > 5 分鐘的殘留 pending 不再視為進行中,也不佔額度。"""
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(user, event, 19)
    _seed(
        user,
        event,
        1,
        record_status=Status.PENDING,
        created_at=timezone.now() - timedelta(minutes=5, seconds=1),
    )

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["quota"]["used"] == 20
    assert engine.calls == 1


@pytest.mark.django_db
def test_finished_request_on_same_event_does_not_block(engine):
    user = _create_user()
    event = _create_finalized_event(user)
    _seed(user, event, 1)
    _seed(user, event, 1, record_status=Status.FAILED)

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED


# ---------------------------------------------------------------------------
# ② 已用 19 次,三個不同活動同時送出
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_requests_on_different_events_cannot_exceed_quota(engine, hold_after_count):
    user = _create_user()
    events = [_create_finalized_event(user) for _ in range(3)]
    _seed(user, events[0], 19)
    hold = hold_after_count(parties=3)

    results = _post_concurrently([(user, e.id) for e in events])

    codes = sorted(code for code, _ in results)
    assert codes == [201, 403, 403]
    assert all(
        body["code"] == "AI_RECOMMENDATION_QUOTA_EXCEEDED" for code, body in results if code == 403
    )
    assert _succeeded_this_month(user) == 20
    assert not RestaurantRecommendationRequest.objects.filter(status=Status.PENDING).exists()
    assert engine.calls == 1
    assert hold.calls == 3


# ---------------------------------------------------------------------------
# ③ 同一活動連點兩下
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_double_click_on_same_event_calls_engine_once(monkeypatch, hold_after_count):
    """假引擎被呼叫後會一直等到「另一個請求已回應」(或逾時)才回傳,確保兩個
    請求在引擎呼叫期間真的重疊。"""
    other_responded = threading.Event()
    engine = ConcurrentFakeEngine(gate=other_responded)
    monkeypatch.setattr("apps.recommendations.views.get_engine", lambda: engine)
    user = _create_user()
    event = _create_finalized_event(user)
    hold = hold_after_count(parties=2)

    results = _post_concurrently(
        [(user, event.id), (user, event.id)],
        on_response=lambda response: other_responded.set(),
    )

    codes = sorted(code for code, _ in results)
    assert codes == [201, 409]
    [(_, conflict_body)] = [(c, b) for c, b in results if c == 409]
    assert conflict_body["code"] == "AI_RECOMMENDATION_IN_PROGRESS"
    assert engine.calls == 1
    assert RestaurantRecommendationRequest.objects.count() == 1
    assert RestaurantRecommendationRequest.objects.get().status == Status.SUCCEEDED
    # 409 的那個請求在同活動進行中檢查就被擋下,不會走到計數。
    assert hold.calls == 1


# ---------------------------------------------------------------------------
# ⑤ 不同使用者互不阻擋
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_different_users_do_not_block_each_other(engine, hold_after_count):
    """兩位使用者各自在鎖內計數時必須能同時進行(barrier 兩方都通過),代表鎖
    只鎖各自的 `User` 列,而不是全域鎖。兩人都已用 19 次,各自還能成功一次。"""
    alice = _create_user(email="alice@example.com", google_sub="sub-a")
    bob = _create_user(email="bob@example.com", google_sub="sub-b")
    alice_event = _create_finalized_event(alice)
    bob_event = _create_finalized_event(bob)
    _seed(alice, alice_event, 19)
    _seed(bob, bob_event, 19)
    # 正確實作下兩方會同時抵達、立刻通過;逾時放寬只是避免慢速 CI 誤判。
    hold = hold_after_count(parties=2, timeout=ENGINE_TIMEOUT)

    results = _post_concurrently([(alice, alice_event.id), (bob, bob_event.id)])

    assert sorted(code for code, _ in results) == [201, 201]
    assert hold.calls == 2
    assert hold.passed == 2
    assert _succeeded_this_month(alice) == 20
    assert _succeeded_this_month(bob) == 20


# ---------------------------------------------------------------------------
# ⑦ 紀錄於引擎呼叫期間被刪除(條件式 update 更新 0 列)
# ---------------------------------------------------------------------------


def _delete_event(event_id):
    return lambda: Event.objects.filter(pk=event_id).delete()


@pytest.mark.django_db
def test_record_deleted_during_success_still_returns_201_and_warns(engine, caplog):
    user = _create_user()
    event = _create_finalized_event(user)
    engine.side_effect = _delete_event(event.id)
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_201_CREATED
    assert [r["name"] for r in response.json()["restaurants"]] == ["好吃小館", "第二間"]
    assert not RestaurantRecommendationRequest.objects.exists()

    [record] = _events(caplog, "ai_rec.succeeded")
    assert record.levelno == logging.WARNING
    assert record.error_code == "RECORD_MISSING"
    assert record.request_id == response.json()["id"]


@pytest.mark.django_db
def test_record_deleted_during_upstream_failure_still_returns_error_and_warns(engine, caplog):
    user = _create_user()
    event = _create_finalized_event(user)
    engine.side_effect = _delete_event(event.id)
    engine.error = UpstreamTimeout("read timeout")
    caplog.set_level(logging.INFO, logger="apps.recommendations")

    response = _auth_client(user).post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert response.json()["code"] == "AI_RECOMMENDATION_UPSTREAM_TIMEOUT"
    assert not RestaurantRecommendationRequest.objects.exists()

    [record] = _events(caplog, "ai_rec.failed")
    assert record.levelno == logging.WARNING
    assert record.error_code == "RECORD_MISSING"


@pytest.mark.django_db
def test_record_deleted_during_unexpected_error_still_returns_500_and_logs(engine, caplog):
    """非預期例外依 spec 維持 ERROR 等級(不因紀錄消失而降級),同樣帶 RECORD_MISSING。"""
    user = _create_user()
    event = _create_finalized_event(user)
    engine.side_effect = _delete_event(event.id)
    engine.error = RuntimeError("boom")
    caplog.set_level(logging.INFO, logger="apps.recommendations")
    client = _auth_client(user)
    client.raise_request_exception = False

    response = client.post(_url(event.id), {}, format="json")

    assert response.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    [record] = _events(caplog, "ai_rec.failed")
    assert record.levelno == logging.ERROR
    assert record.error_code == "RECORD_MISSING"
