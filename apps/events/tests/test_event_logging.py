"""活動狀態變更與投票的事件 log(add-observability-stack design.md D11、tasks.md 4.3)。

只以 id 識別對象:不含 email、活動標題、暱稱。只在交易 commit 後輸出,失敗的請求
(409 / 400)不輸出。
"""

import json
import logging
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.events.models import ParticipantResponse

from .test_views import (
    EVENTS_URL,
    _add_slot,
    _auth_client,
    _cancel_url,
    _create_event,
    _create_user,
    _finalize_event,
    _finalize_url,
    _reopen_url,
    _response_payload,
    _responses_url,
    _slot_availabilities,
    _valid_payload,
)

pytestmark = pytest.mark.django_db

SENSITIVE = ("host@example.com", "participant@example.com", "颱風天續攤", "小明", "小華")


@pytest.fixture(autouse=True)
def _info_level(caplog):
    caplog.set_level(logging.INFO, logger="apps")


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


def _assert_no_sensitive_data(record):
    payload = json.dumps(
        {
            "message": record.getMessage(),
            **{k: str(v) for k, v in record.__dict__.items() if k.endswith("_id")},
        },
        ensure_ascii=False,
    )
    for value in SENSITIVE:
        assert value not in payload


def test_create_event_logs_event_created_after_commit(caplog, django_capture_on_commit_callbacks):
    user = _create_user()
    client = _auth_client(user)

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(EVENTS_URL, _valid_payload(), format="json")

    assert response.status_code == status.HTTP_201_CREATED
    records = _events(caplog, "event.created")
    assert len(records) == 1
    assert records[0].event_id == response.json()["id"]
    assert records[0].user_id == str(user.id)
    assert records[0].levelno == logging.INFO
    _assert_no_sensitive_data(records[0])


def test_create_event_without_commit_does_not_log(caplog, django_capture_on_commit_callbacks):
    client = _auth_client(_create_user())

    with django_capture_on_commit_callbacks(execute=False):
        client.post(EVENTS_URL, _valid_payload(), format="json")

    assert _events(caplog, "event.created") == []


def test_invalid_create_event_does_not_log(caplog, django_capture_on_commit_callbacks):
    client = _auth_client(_create_user())

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post(EVENTS_URL, _valid_payload(title=""), format="json")

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert _events(caplog, "event.created") == []


def test_finalize_logs_event_finalized(caplog, django_capture_on_commit_callbacks):
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()

    with django_capture_on_commit_callbacks(execute=True):
        response = _auth_client(owner).post(
            _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
        )

    assert response.status_code == status.HTTP_200_OK
    records = _events(caplog, "event.finalized")
    assert len(records) == 1
    assert records[0].event_id == str(event.id)
    assert records[0].user_id == str(owner.id)
    _assert_no_sensitive_data(records[0])


def test_finalize_conflict_does_not_log(caplog, django_capture_on_commit_callbacks):
    owner = _create_user()
    event = _create_event(owner)
    slot = event.slots.first()
    _finalize_event(event, slot)

    with django_capture_on_commit_callbacks(execute=True):
        response = _auth_client(owner).post(
            _finalize_url(event.id), {"finalSlotId": str(slot.id)}, format="json"
        )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert _events(caplog, "event.finalized") == []


def test_cancel_logs_event_cancelled(caplog, django_capture_on_commit_callbacks):
    owner = _create_user()
    event = _create_event(owner)

    with django_capture_on_commit_callbacks(execute=True):
        response = _auth_client(owner).post(_cancel_url(event.id), format="json")

    assert response.status_code == status.HTTP_200_OK
    records = _events(caplog, "event.cancelled")
    assert len(records) == 1
    assert records[0].event_id == str(event.id)
    assert records[0].user_id == str(owner.id)


def test_cancel_twice_logs_only_once(caplog, django_capture_on_commit_callbacks):
    owner = _create_user()
    event = _create_event(owner)
    client = _auth_client(owner)

    with django_capture_on_commit_callbacks(execute=True):
        first = client.post(_cancel_url(event.id), format="json")
        second = client.post(_cancel_url(event.id), format="json")

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_409_CONFLICT
    assert len(_events(caplog, "event.cancelled")) == 1


def test_reopen_logs_event_reopened(caplog, django_capture_on_commit_callbacks):
    owner = _create_user()
    event = _create_event(owner)
    _finalize_event(event, event.slots.first())
    deadline = timezone.now() + timedelta(days=5)

    with django_capture_on_commit_callbacks(execute=True):
        response = _auth_client(owner).post(
            _reopen_url(event.id), {"responseDeadline": deadline.isoformat()}, format="json"
        )

    assert response.status_code == status.HTTP_200_OK
    records = _events(caplog, "event.reopened")
    assert len(records) == 1
    assert records[0].event_id == str(event.id)
    assert records[0].user_id == str(owner.id)


def test_reopen_active_event_does_not_log(caplog, django_capture_on_commit_callbacks):
    owner = _create_user()
    event = _create_event(owner)
    deadline = timezone.now() + timedelta(days=5)

    with django_capture_on_commit_callbacks(execute=True):
        response = _auth_client(owner).post(
            _reopen_url(event.id), {"responseDeadline": deadline.isoformat()}, format="json"
        )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert _events(caplog, "event.reopened") == []


def test_participant_response_logs_response_created(caplog, django_capture_on_commit_callbacks):
    event = _create_event(_create_user())
    slot_ids = [str(event.slots.first().id), str(_add_slot(event).id)]

    with django_capture_on_commit_callbacks(execute=True):
        response = APIClient().post(
            _responses_url(event.id),
            _response_payload(slotAvailabilities=_slot_availabilities(available=slot_ids)),
            format="json",
        )

    assert response.status_code == status.HTTP_201_CREATED
    records = _events(caplog, "event.response_created")
    assert len(records) == 1
    assert records[0].event_id == str(event.id)
    assert records[0].response_id == str(ParticipantResponse.objects.get(event=event).id)
    assert not hasattr(records[0], "user_id")
    _assert_no_sensitive_data(records[0])


def test_invalid_participant_response_does_not_log(caplog, django_capture_on_commit_callbacks):
    event = _create_event(_create_user())

    with django_capture_on_commit_callbacks(execute=True):
        response = APIClient().post(
            _responses_url(event.id), _response_payload(slotAvailabilities=[]), format="json"
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert _events(caplog, "event.response_created") == []
