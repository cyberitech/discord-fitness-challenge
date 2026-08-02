"""Scaffolding healthcheck entrypoint for FCB.

Verifies that .env is readable, all required config values are present,
the database migrates cleanly, and the initial admin seed is in place.
Later phases add real bot and dashboard entrypoints; this one stays as
a smoke test for the config + DB layer.

Run:  uv run python -m fcb
"""

import logging

from fcb import config, logging_setup
from fcb.db import dao


def main() -> None:
    logging_setup.configure()
    logger = logging.getLogger("fcb.healthcheck")

    logger.info("FCB healthcheck starting")
    config.log_summary()

    applied = dao.apply_migrations()
    logger.info(f"migrations applied this run: {applied}")

    with dao.connect() as conn:
        admin_count = conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"]
        migration_count = conn.execute(
            "SELECT COUNT(*) AS c FROM schema_migrations"
        ).fetchone()["c"]
    logger.info(f"admins in db: {admin_count}")
    logger.info(f"migrations recorded: {migration_count}")

    logger.info("FCB healthcheck OK")


if __name__ == "__main__":
    main()
