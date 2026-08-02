"""Entrypoint for the FCB bot process.

Run:  uv run python -m fcb.bot
"""

import logging

from fcb import config, logging_setup
from fcb.bot.client import run
from fcb.db import dao


def main() -> None:
    logging_setup.configure()
    logger = logging.getLogger("fcb.bot")
    logger.info("FCB bot starting")
    config.log_summary()

    # Migrations run on every start so a fresh checkout comes up clean.
    applied = dao.apply_migrations()
    logger.info(f"migrations applied on startup: {applied}")

    run()


if __name__ == "__main__":
    main()
