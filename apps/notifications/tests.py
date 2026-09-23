import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.events.models import Event, Slot

from .tasks import (
    _healthcheck_send_test_email,
    send_event_cancelled_email,
    send_event_finalized_email,
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

    send_event_finalized_email(event.id)

    assert len(mailoutbox) == 0


@pytest.mark.django_db
def test_send_event_cancelled_email_skips_when_event_no_longer_cancelled(mailoutbox):
    """同上,cancel 版本。"""
    event = _create_event(status=Event.Status.ACTIVE)

    send_event_cancelled_email(event.id)

    assert len(mailoutbox) == 0
