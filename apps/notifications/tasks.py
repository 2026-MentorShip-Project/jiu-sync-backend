import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from apps.events.models import Event

logger = logging.getLogger(__name__)


def _log_sent(task, event_id, recipient_count):
    """寄出後記一筆(add-observability-stack design.md D11)。不記收件人 email 與標題。"""
    logger.info(
        "notification sent",
        extra={
            "event": "notification.sent",
            "task": task,
            "event_id": str(event_id),
            "recipient_count": recipient_count,
        },
    )


def _log_skipped(task, event_id, reason):
    """reason: event_missing / status_changed / stale / no_recipients(D11)。"""
    logger.info(
        "notification skipped: %s",
        reason,
        extra={
            "event": "notification.skipped",
            "task": task,
            "event_id": str(event_id),
            "reason": reason,
        },
    )


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


def _send_to_each_recipient(subject, body, recipients):
    """每個收件人各寄一封獨立的信(收件人只有自己一個)，不是把所有人塞進同一封
    信的 To——code-review 抓到:原本全部塞進同一個 recipients list 傳給
    ``send_mail``，等於每位參與者都能在自己收到的信裡看到其他參與者與主揪的
    Email，隱私外洩。"""
    for recipient in recipients:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient])


@shared_task
def send_event_created_email(event_id):
    """主揪建立活動成功後，把分享連結寄到主揪本人的 Google 帳號信箱
    （design.md D1，`add-event-created-email`）。

    只核對 ``event.host_email`` 是否存在，不重用 ``_notification_recipients()``
    ——那支函式回傳「留過 Email 的參與者 + 主揪本人」，但活動剛建立時還沒有
    任何 ``ParticipantResponse``，語意上這裡只需要主揪本人（design.md D1）。
    ``host_email`` 來自 ``EventCreateView.post()`` 寫入當下的
    ``request.user.email``（Google 帳號 email，恆存在），這裡的存在性檢查是
    防禦性的，跟其他三支通知信的寫法一致。

    不做「過期 task」判斷（design.md D2）：建立活動這個動作對同一個
    ``event.id`` 只會發生一次，不存在被後續 transition 蓋過的疑慮，只需要
    核對 ``event`` 是否存在（純防禦性，理論上不會發生，系統沒有刪除活動的
    功能）。
    """
    task = "send_event_created_email"
    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        _log_skipped(task, event_id, "event_missing")
        return
    if not event.host_email:
        _log_skipped(task, event_id, "no_recipients")
        return

    body = (
        f"你已經成功建立活動「{event.title}」。\n"
        f"分享連結：{_event_share_url(event)}"
    )
    send_mail(
        f"「{event.title}」已建立",
        body,
        settings.DEFAULT_FROM_EMAIL,
        [event.host_email],
    )
    _log_sent(task, event_id, 1)


@shared_task
def send_event_finalized_email(event_id, finalized_at):
    """主揪定案活動後通知留過 Email 的參與者與主揪本人（design.md D1，
    `add-event-lifecycle`）。傳 ``event_id``（純值）不傳 model instance——
    task 實際執行時才查，才不會撈到請求當下序列化後的舊物件。

    ``finalized_at``:呼叫端（view）在排入 task 當下、CAS UPDATE 寫入資料庫的
    同一個值。第二輪 code-review 抓到:只核對「現在的 status」擋不住
    reopen→finalize→reopen→finalize 這種來回——舊 task 執行當下看到的 status
    可能剛好又符合 FINALIZED，因而重複寄信。額外核對這個值是否仍跟資料庫
    現在的 ``finalized_at`` 一致，不一致代表這是被後續 transition 蓋過的
    過期 task，不該寄信（design.md D10）。
    """
    task = "send_event_finalized_email"
    event = Event.objects.select_related("final_slot").filter(pk=event_id).first()
    if event is None:
        _log_skipped(task, event_id, "event_missing")
        return
    if event.status != Event.Status.FINALIZED:
        # code-review 抓到:on_commit 排入佇列後、task 真正執行前，活動可能
        # 已經被後續請求改成別的狀態（例如很快又被取消）——這裡是 task 實際
        # 執行當下唯一能重新核對狀態的地方，早期檢查用的是排入當下的舊值。
        _log_skipped(task, event_id, "status_changed")
        return
    if event.finalized_at != finalized_at:
        _log_skipped(task, event_id, "stale")
        return
    recipients = _notification_recipients(event)
    if not recipients:
        _log_skipped(task, event_id, "no_recipients")
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
    _send_to_each_recipient(f"「{event.title}」已定案", body, recipients)
    _log_sent(task, event_id, len(recipients))


@shared_task
def send_event_cancelled_email(event_id, cancelled_at):
    """主揪取消活動後通知留過 Email 的參與者（含投票已因本次取消而被軟刪除
    的）與主揪本人（design.md D1/D2a，`add-event-lifecycle`）。

    ``cancelled_at``:同 ``send_event_finalized_email`` 的 ``finalized_at``，
    過期 task 判斷用（design.md D10）。
    """
    task = "send_event_cancelled_email"
    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        _log_skipped(task, event_id, "event_missing")
        return
    if event.status != Event.Status.CANCELLED:
        # 同 send_event_finalized_email 的理由——task 執行當下重新核對狀態。
        _log_skipped(task, event_id, "status_changed")
        return
    if event.cancelled_at != cancelled_at:
        _log_skipped(task, event_id, "stale")
        return
    recipients = _notification_recipients(event)
    if not recipients:
        _log_skipped(task, event_id, "no_recipients")
        return

    body = (
        f"主揪已經取消活動「{event.title}」。\n"
        f"活動詳情：{_event_share_url(event)}"
    )
    _send_to_each_recipient(f"「{event.title}」已取消", body, recipients)
    _log_sent(task, event_id, len(recipients))


@shared_task
def send_event_reopened_email(event_id, response_deadline):
    """主揪重新開放投票後通知留過 Email 的參與者與主揪本人（design.md D4，
    `add-event-reopen`）。

    ``response_deadline``:過期 task 判斷用（design.md D10）。reopen 沒有像
    ``finalized_at``/``cancelled_at`` 那樣專屬的時間戳欄位，借用這次 CAS
    UPDATE 寫入的 ``response_deadline`` 本身當作 transition 身分——若資料庫
    現在的值跟排入當下不同，代表活動後來又被重新開放過一次。
    """
    task = "send_event_reopened_email"
    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        _log_skipped(task, event_id, "event_missing")
        return
    if event.status != Event.Status.ACTIVE:
        # 同 send_event_finalized_email 的理由——task 執行當下重新核對狀態。
        _log_skipped(task, event_id, "status_changed")
        return
    if event.response_deadline != response_deadline:
        _log_skipped(task, event_id, "stale")
        return
    recipients = _notification_recipients(event)
    if not recipients:
        _log_skipped(task, event_id, "no_recipients")
        return

    body = (
        f"主揪已經重新開放活動「{event.title}」的投票。\n"
        f"新的投票截止時間：{event.response_deadline}\n"
        f"活動詳情：{_event_share_url(event)}"
    )
    _send_to_each_recipient(f"「{event.title}」已重新開放投票", body, recipients)
    _log_sent(task, event_id, len(recipients))
