"""Tests for the /healthz endpoint (config/health.py).

Per design.md Decision 10/11: this is a platform-level "process is alive"
signal, not tied to any app, and must not touch the DB. It must also be
reachable anonymously even though DRF's project-wide default requires
IsAuthenticated (see REST_FRAMEWORK in config/settings/base.py), since CI
health-check polling and k6 hit it with no auth header.
"""
from rest_framework import status
from rest_framework.test import APIClient


def test_healthz_returns_200_for_anonymous_get():
    client = APIClient()

    response = client.get("/healthz/")

    assert response.status_code == status.HTTP_200_OK


def test_healthz_rejects_post():
    client = APIClient()

    response = client.post("/healthz/")

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
