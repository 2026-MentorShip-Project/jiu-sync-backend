from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from apps.events.models import Event


@shared_task
def _healthcheck_send_test_email(to_email):
    """驗證 Celery（``CELERY_TASK_ALWAYS_EAGER``）＋ Django 6.1 Mailers 這條從
    沒被真的呼叫過的管線走得通（見
    openspec/changes/add-event-lifecycle/tasks.md 0.1/0.2）。不是正式功能，
    之後被 ``send_event_finalized_email``/``send_event_cancelled_email``
    取代後可以刪掉。
    """
    send_mail(
        "healthcheck",
        "healthcheck",
        settings.DEFAULT_FROM_EMAIL,
        [to_email],
    )


def _event_share_url(event):
    return f"{settings.FRONTEND_BASE_URL.rstrip('/')}/events/{event.id}"


def _notification_recipients(event):
    """留過 Email 的參與者 + 主揪本人（若有填寫）。刻意不過濾
    ``ParticipantResponse.deleted_at``——``send_event_cancelled_email`` 執行
    當下，這場活動的投票紀錄已經被 ``EventCancelView`` 軟刪除，若在這裡過濾
    會查到空結果、通知信永遠寄不出去，見 design.md D2a。``send_event_finalized_email``
    不涉及軟刪除，兩者共用同一支查詢寫法更簡單。去重、保留原本出現順序。
    """
    emails = list(
        event.responses.exclude(email__isnull=True).values_list("email", flat=True)
    )
    if event.host_email:
        emails.append(event.host_email)
    return list(dict.fromkeys(emails))


@shared_task
def send_event_finalized_email(event_id):
    """主揪定案活動後通知留過 Email 的參與者與主揪本人（design.md D1，
    `add-event-lifecycle`）。傳 ``event_id``（純值）不傳 model instance——
    task 實際執行時才查，才不會撈到請求當下序列化後的舊物件。"""
    event = Event.objects.select_related("final_slot").filter(pk=event_id).first()
    if event is None or event.status != Event.Status.FINALIZED:
        # code-review 抓到:on_commit 排入佇列後、task 真正執行前，活動可能
        # 已經被後續請求改成別的狀態（例如很快又被取消）——這裡是 task 實際
        # 執行當下唯一能重新核對狀態的地方，早期檢查用的是排入當下的舊值。
        return
    recipients = _notification_recipients(event)
    if not recipients:
        return

    slot = event.final_slot
    slot_time = f" {slot.time}" if slot and slot.time else ""
    lines = [
        f"主揪已經將活動「{event.title}」定案。",
        f"最終時間：{slot.date}{slot_time}" if slot else "",
        f"備註：{event.final_note}" if event.final_note else "",
        f"活動詳情：{_event_share_url(event)}",
    ]
    body = "\n".join(line for line in lines if line)
    send_mail(
        f"「{event.title}」已定案",
        body,
        settings.DEFAULT_FROM_EMAIL,
        recipients,
    )


@shared_task
def send_event_cancelled_email(event_id):
    """主揪取消活動後通知留過 Email 的參與者（含投票已因本次取消而被軟刪除
    的）與主揪本人（design.md D1/D2a，`add-event-lifecycle`）。"""
    event = Event.objects.filter(pk=event_id).first()
    if event is None or event.status != Event.Status.CANCELLED:
        # 同 send_event_finalized_email 的理由——task 執行當下重新核對狀態。
        return
    recipients = _notification_recipients(event)
    if not recipients:
        return

    body = (
        f"主揪已經取消活動「{event.title}」。\n"
        f"活動詳情：{_event_share_url(event)}"
    )
    send_mail(
        f"「{event.title}」已取消",
        body,
        settings.DEFAULT_FROM_EMAIL,
        recipients,
    )


@shared_task
def send_event_reopened_email(event_id):
    """主揪重新開放投票後通知留過 Email 的參與者與主揪本人（design.md D4，
    `add-event-reopen`）。"""
    event = Event.objects.filter(pk=event_id).first()
    if event is None or event.status != Event.Status.ACTIVE:
        # 同 send_event_finalized_email 的理由——task 執行當下重新核對狀態。
        return
    recipients = _notification_recipients(event)
    if not recipients:
        return

    body = (
        f"主揪已經重新開放活動「{event.title}」的投票。\n"
        f"新的投票截止時間：{event.response_deadline}\n"
        f"活動詳情：{_event_share_url(event)}"
    )
    send_mail(
        f"「{event.title}」已重新開放投票",
        body,
        settings.DEFAULT_FROM_EMAIL,
        recipients,
    )
