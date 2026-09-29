import pytest
from django.db import connections

from config.pgbouncer_cleanup import release_pgbouncer_server_connections


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Wrap pytest-django's django_db_setup: this teardown runs before the
    original one DROPs the test database, and releases PgBouncer's server
    connections to it when PGBOUNCER_ADMIN_URL is set (design.md D4 Q6).
    No-op for direct connections."""
    yield
    for alias in connections:
        with django_db_blocker.unblock():
            connections[alias].close()
        release_pgbouncer_server_connections(connections[alias].settings_dict["NAME"])
