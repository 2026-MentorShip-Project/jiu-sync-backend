"""通知信 task 的事件 log(add-observability-stack design.md D11、tasks.md 4.3)。

每次 task 執行恰好一筆:`notification.sent`(含收件人數)或 `notification.skipped`
(含 reason)。不記收件人 email 與活動標題。
"""

import json
import logging
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.events.models import Event, ParticipantResponse

from ..tasks import (
    send_event_cancelled_email,
    send_event_created_email,
    send_event_finalized_email,
    send_event_reopened_email,
)
from .test_tasks import _create_event

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _info_level(caplog):
    caplog.set_level(logging.INFO, logger="apps")


def _notification_records(caplog):
    return [
        r
        for r in caplog.records
        if getattr(r, "event", None) in {"notification.sent", "notification.skipped"}
    ]


def _single(caplog, event_name):
    records = _notification_records(caplog)
    assert len(records) == 1, [r.event for r in records]
    assert records[0].event == event_name
    return records[0]


def _assert_no_sensitive_data(record):
    dumped = json.dumps(
        {"message": record.getMessage(), **{k: str(v) for k, v in record.__dict__.items()}},
        ensure_ascii=False,
    )
    for value in ("host@example.com", "voter@example.com", "測試活動"):
        assert value not in dumped


def _finalized_event():
    finalized_at = timezone.now()
    event = _create_event(status=Event.Status.FINALIZED, finalized_at=finalized_at)
    event.final_slot = event.slots.first()
    event.save()
    ParticipantResponse.objects.create(
        event=event,
        nickname="小華",
        phone_last_three_hash="x",
        email="voter@example.com",
    )
    return event, finalized_at


def test_created_email_logs_sent_with_recipient_count(caplog, mailoutbox):
    event = _create_event()

    send_event_created_email(event.id)

    record = _single(caplog, "notification.sent")
    assert record.task == "send_event_created_email"
    assert record.event_id == str(event.id)
    assert record.recipient_count == 1
    assert len(mailoutbox) == 1
    _assert_no_sensitive_data(record)


def test_created_email_logs_skipped_event_missing(caplog):
    send_event_created_email("NotExist")

    record = _single(caplog, "notification.skipped")
    assert record.reason == "event_missing"
    assert record.event_id == "NotExist"


@pytest.mark.parametrize("host_email", [None, ""])
def test_created_email_logs_skipped_no_recipients(caplog, host_email):
    event = _create_event(host_email=host_email)

    send_event_created_email(event.id)

    assert _single(caplog, "notification.skipped").reason == "no_recipients"


def test_finalized_email_logs_sent_with_recipient_count(caplog, mailoutbox):
    event, finalized_at = _finalized_event()

    send_event_finalized_email(event.id, finalized_at)

    record = _single(caplog, "notification.sent")
    assert record.task == "send_event_finalized_email"
    assert record.recipient_count == 2
    assert len(mailoutbox) == 2
    _assert_no_sensitive_data(record)


def test_finalized_email_logs_skipped_status_changed(caplog):
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=timezone.now())

    send_event_finalized_email(event.id, timezone.now())

    record = _single(caplog, "notification.skipped")
    assert record.task == "send_event_finalized_email"
    assert record.reason == "status_changed"


def test_finalized_email_logs_skipped_stale(caplog):
    event, finalized_at = _finalized_event()

    send_event_finalized_email(event.id, finalized_at - timezone.timedelta(minutes=1))

    assert _single(caplog, "notification.skipped").reason == "stale"


def test_finalized_email_logs_skipped_event_missing(caplog):
    send_event_finalized_email("NotExist", timezone.now())

    assert _single(caplog, "notification.skipped").reason == "event_missing"


def test_finalized_email_logs_skipped_no_recipients(caplog):
    finalized_at = timezone.now()
    event = _create_event(status=Event.Status.FINALIZED, finalized_at=finalized_at, host_email=None)

    send_event_finalized_email(event.id, finalized_at)

    assert _single(caplog, "notification.skipped").reason == "no_recipients"


def test_cancelled_email_logs_sent(caplog, mailoutbox):
    cancelled_at = timezone.now()
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=cancelled_at)

    send_event_cancelled_email(event.id, cancelled_at)

    record = _single(caplog, "notification.sent")
    assert record.task == "send_event_cancelled_email"
    assert record.recipient_count == 1


def test_cancelled_email_logs_skipped_status_changed(caplog):
    event = _create_event()

    send_event_cancelled_email(event.id, timezone.now())

    assert _single(caplog, "notification.skipped").reason == "status_changed"


def test_cancelled_email_logs_skipped_stale(caplog):
    cancelled_at = timezone.now()
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=cancelled_at)

    send_event_cancelled_email(event.id, cancelled_at - timezone.timedelta(minutes=1))

    assert _single(caplog, "notification.skipped").reason == "stale"


def test_reopened_email_logs_sent(caplog, mailoutbox):
    event = _create_event()

    send_event_reopened_email(event.id, event.response_deadline)

    record = _single(caplog, "notification.sent")
    assert record.task == "send_event_reopened_email"
    assert record.recipient_count == 1


def test_reopened_email_logs_skipped_stale(caplog):
    event = _create_event()

    send_event_reopened_email(event.id, event.response_deadline - timezone.timedelta(days=1))

    assert _single(caplog, "notification.skipped").reason == "stale"


def test_reopened_email_logs_skipped_status_changed(caplog):
    event = _create_event(status=Event.Status.CANCELLED, cancelled_at=timezone.now())

    send_event_reopened_email(event.id, event.response_deadline)

    assert _single(caplog, "notification.skipped").reason == "status_changed"


def test_send_failure_propagates_and_does_not_log_sent(caplog):
    event = _create_event()

    with patch("apps.notifications.tasks.send_mail", side_effect=OSError("smtp down")):
        with pytest.raises(OSError):
            send_event_created_email(event.id)

    assert _notification_records(caplog) == []
