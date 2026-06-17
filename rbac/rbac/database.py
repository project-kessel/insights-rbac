#
# Copyright 2019 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Django database settings."""

from app_common_python import LoadedConfig

from .env import ENVIRONMENT


def _pg_session_options() -> str:
    """Build the PostgreSQL ``options`` string for session-level parameters.

    lock_timeout: abort any statement waiting longer than this for a row lock.
    Prevents zombie transactions from piling up when a single lock holder blocks
    many concurrent requests (each blocked connection stays open until the lock
    is released or the connection is killed).
    RDS does not support this option, so we don't add it through app-interface.
    """
    lock_timeout_ms = ENVIRONMENT.int("DATABASE_LOCK_TIMEOUT_MS", default=60_000)
    return f"-c lock_timeout={lock_timeout_ms}"


def config():
    """Database config."""
    # Connection persistence: reuse connections for up to N seconds (0 = close after each request).
    # Set via DATABASE_CONN_MAX_AGE env var; default 60s in production to reduce connection churn.
    conn_max_age = ENVIRONMENT.int("DATABASE_CONN_MAX_AGE", default=60)
    pg_options = _pg_session_options()

    if ENVIRONMENT.bool("CLOWDER_ENABLED", default=False):
        db_obj = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": LoadedConfig.database.name,
            "USER": LoadedConfig.database.username,
            "PASSWORD": LoadedConfig.database.password,
            "HOST": LoadedConfig.database.hostname,
            "PORT": LoadedConfig.database.port,
            "CONN_MAX_AGE": conn_max_age,
            "CONN_HEALTH_CHECKS": True,
        }
        if LoadedConfig.database.rdsCa:
            db_options = {
                "OPTIONS": {
                    "sslmode": ENVIRONMENT.get_value("PGSSLMODE", default="prefer"),
                    "sslrootcert": LoadedConfig.rds_ca(),
                    "options": pg_options,
                }
            }
        else:
            db_options = {
                "OPTIONS": {
                    "options": pg_options,
                }
            }
    else:
        db_obj = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": ENVIRONMENT.get_value("DATABASE_NAME", default=None),
            "USER": ENVIRONMENT.get_value("DATABASE_USER", default=None),
            "PASSWORD": ENVIRONMENT.get_value("DATABASE_PASSWORD", default=None),
            "HOST": ENVIRONMENT.get_value("DATABASE_HOST", default=None),
            "PORT": ENVIRONMENT.get_value("DATABASE_PORT", default=None),
            "CONN_MAX_AGE": conn_max_age,
            "CONN_HEALTH_CHECKS": True,
        }
        db_options = {
            "OPTIONS": {
                "sslmode": ENVIRONMENT.get_value("PGSSLMODE", default="prefer"),
                "sslrootcert": ENVIRONMENT.get_value("PGSSLROOTCERT", default="/etc/rds-certs/rds-cacert"),
                "options": pg_options,
            }
        }

    db_obj.update(db_options)

    return db_obj
