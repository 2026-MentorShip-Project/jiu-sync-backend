import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.events.ids import generate_short_id
from apps.events.models import Event, Slot

from ..tasks import (
    _event_share_url,
    _healthcheck_send_test_email,
    send_event_cancelled_email,
    send_event_created_email,
    send_event_finalized_email,
    send_event_reopened_email,
)


def test_celery_eager_and_mailers_pipeline_actually_works(mailoutbox):
    """環境健檢（design.md/tasks.md 0.1/0.2）：CELERY_TASK_ALWAYS_EAGER=True 讓
    .delay() 在呼叫當下同步執行完，Django 6.1 的 MAILERS 在測試環境自動換成
    locmem（見 django.test.utils.setup_test_environment，不需要額外設定）。
    這條管線在本專案從沒被真的呼叫過，先用最小案例證明走得通，再疊上
    finalize/cancel 的正式通知信邏輯。"""
    _healthcheck_send_test_email.delay("test@example.com")

    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["test@example.com"]


def _create_event(status=Event.Status.ACTIVE, **overrides):
    owner = User.objects.create_user(
        email="host@example.com", google_sub="sub-1", display_name="Host", avatar_url=""
    )
    defaults = {
        "owner": owner,
        "title": "測試活動",
        "host_nickname": "小明",
        "host_email": "host@example.com",  # 一定要有收件人，才能真的驗證是
        # 「status 不對，被守門擋下」而不是「本來就沒人可以寄」。
        "mode": "date_only",
        "response_deadline": timezone.now() + timezone.timedelta(days=3),
        "status": status,
    }
    defaults.update(overrides)
    event = Event.objects.create(**defaults)
    Slot.objects.create(event=event, date="2026-10-01")
    return event


@pytest.mark.django_db
def test_send_event_finalized_email_skips_when_event_no_longer_finalized(mailoutbox):
    """code-review 補充:finalize 的 on_commit 排入佇列後、task 真正執行前，
    活動若已經被（更快處理完的另一個請求）改成別的狀態，不該再寄出一封聲稱
    「已定案」但其實矛盾的信。task 執行當下重新核對 status，不只信賴排入
    當下的舊狀態。"""
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=timezone.now())

    send_event_finalized_email(event.id, timezone.now())

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_cancelled_email_skips_when_event_no_longer_cancelled(mailoutbox):
    """同上,cancel 版本。"""
    event = _create_event(status=Event.Status.ACTIVE)

    send_event_cancelled_email(event.id, timezone.now())

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_finalized_email_skips_when_finalized_at_is_stale(mailoutbox):
    """第二輪 code-review 抓到:光核對「現在的 status」擋不住
    reopen→finalize→reopen→finalize 這種來回——舊 task 執行當下看到的 status
    可能剛好又符合，因而重複寄信。task 額外核對呼叫端排入當下記下的
    finalized_at 是否還跟資料庫現在的值一致,不一致代表這是一個被後續
    transition 蓋過的過期 task,不該寄信。"""
    event = _create_event(status=Event.Status.FINALIZED, finalized_at=timezone.now())
    stale_finalized_at = event.finalized_at - timezone.timedelta(minutes=1)

    send_event_finalized_email(event.id, stale_finalized_at)

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_finalized_email_sends_when_finalized_at_matches_current(mailoutbox):
    """對照組:傳入的 finalized_at 跟資料庫現在的值一致(不是過期 task)→
    正常寄信。"""
    event = _create_event(status=Event.Status.FINALIZED, finalized_at=timezone.now())
    slot = event.slots.first()
    event.final_slot = slot
    event.save()

    send_event_finalized_email(event.id, event.finalized_at)

    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_send_event_cancelled_email_skips_when_cancelled_at_is_stale(mailoutbox):
    """同 finalize 版本的過期 task 判斷,cancel 版本。"""
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=timezone.now())
    stale_cancelled_at = event.cancelled_at - timezone.timedelta(minutes=1)

    send_event_cancelled_email(event.id, stale_cancelled_at)

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_cancelled_email_sends_when_cancelled_at_matches_current(mailoutbox):
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=timezone.now())

    send_event_cancelled_email(event.id, event.cancelled_at)

    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_send_event_reopened_email_skips_when_response_deadline_is_stale(mailoutbox):
    """reopen 沒有專屬的時間戳欄位,借用 response_deadline 本身當作 transition
    身分——舊 task 排入當下記下的 deadline 若跟資料庫現在的值不一致,代表
    活動後來又被重新開放過一次(deadline 已經是更新的值),這是過期 task。"""
    event = _create_event(status=Event.Status.ACTIVE)
    stale_deadline = event.response_deadline - timezone.timedelta(days=1)

    send_event_reopened_email(event.id, stale_deadline)

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_reopened_email_sends_when_response_deadline_matches_current(
    mailoutbox,
):
    event = _create_event(status=Event.Status.ACTIVE)

    send_event_reopened_email(event.id, event.response_deadline)

    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_send_event_created_email_sends_to_host_with_share_url(mailoutbox):
    """① event.host_email 存在時,task 執行後 outbox 收到一封信,收件人為
    host_email,內容含 _event_share_url(event)（design.md，
    `add-event-created-email`）。"""
    event = _create_event(host_email="host@example.com")

    send_event_created_email(event.id)

    assert len(mailoutbox) == 1
    assert mailoutbox[0].to == ["host@example.com"]
    assert _event_share_url(event) in mailoutbox[0].body


@pytest.mark.django_db
def test_send_event_created_email_skips_when_host_email_is_none(mailoutbox):
    """② event.host_email 為 None 時不寄信（D1:建立活動當下沒有任何參與者,
    只需要核對主揪本人的收件地址是否存在）。"""
    event = _create_event(host_email=None)

    send_event_created_email(event.id)

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_created_email_skips_when_host_email_is_blank(mailoutbox):
    """② event.host_email 為空字串時不寄信,同上。"""
    event = _create_event(host_email="")

    send_event_created_email(event.id)

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_created_email_skips_when_event_not_found(mailoutbox):
    """③ event_id 對應不到任何 Event 時(防禦性,D2)不拋例外、不寄信。"""
    send_event_created_email(generate_short_id())

    assert len(mailoutbox) == 0
