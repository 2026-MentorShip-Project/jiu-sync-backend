from django.contrib import admin
from django.test import TestCase


class AdminSiteHeaderTest(TestCase):
    def test_site_header_carries_deploy_marker(self):
        self.assertIn("deploy-verify-v1", admin.site.site_header)
