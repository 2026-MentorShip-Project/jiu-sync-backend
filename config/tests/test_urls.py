from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from apps.events.views import EventDetailView


class AdminSiteHeaderTest(TestCase):
    def test_site_header_carries_deploy_marker(self):
        self.assertIn("deploy-verify-v1", admin.site.site_header)


class OpenApiDocsTest(TestCase):
    def test_event_detail_auth_configuration_supports_schema_introspection(self):
        view = EventDetailView()

        self.assertIsNotNone(view.get_permissions())
        self.assertIsNotNone(view.get_authenticators())

    def test_schema_and_swagger_urls_are_available(self):
        schema_response = self.client.get(reverse("schema"))
        swagger_response = self.client.get(reverse("swagger-ui"))

        self.assertEqual(schema_response.status_code, 200)
        self.assertEqual(schema_response.json()["openapi"], "3.0.3")
        self.assertEqual(swagger_response.status_code, 200)
