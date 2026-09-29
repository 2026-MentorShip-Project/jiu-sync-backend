"""Release PgBouncer's server connections to the pytest database before
pytest-django drops it (add-pgbouncer-and-celery-worker design.md D4 Q6).

Only used by the root conftest.py. PgBouncer keeps idle server connections
to test_* databases, so DROP DATABASE fails with "is being accessed by other
users". KILL drops them; KILL also leaves the database paused in PgBouncer
(new connections wait until RESUME — verified in task 2.1), so RESUME follows
immediately. Enabled only when PGBOUNCER_ADMIN_URL is set; errors propagate.
"""

import logging
import os
from collections.abc import Callable, Mapping

import psycopg

ADMIN_URL_ENV = "PGBOUNCER_ADMIN_URL"
CONNECT_TIMEOUT_SECONDS = 5

logger = logging.getLogger(__name__)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def release_pgbouncer_server_connections(
    database_name: str,
    *,
    environ: Mapping[str, str] = os.environ,
    connect: Callable[..., psycopg.Connection] = psycopg.connect,
) -> None:
    admin_url = environ.get(ADMIN_URL_ENV, "")
    if not admin_url:
        return
    if not database_name:
        raise ValueError("database_name is required to release PgBouncer connections")

    identifier = _quote_identifier(database_name)
    # PgBouncer's admin console has no transactions; autocommit avoids BEGIN.
    with connect(admin_url, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS) as admin:
        logger.info("PgBouncer: KILL %s (drop server connections before DROP)", identifier)
        admin.execute(f"KILL {identifier}")
        logger.info("PgBouncer: RESUME %s", identifier)
        admin.execute(f"RESUME {identifier}")
