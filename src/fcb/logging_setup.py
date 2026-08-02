"""Root logging configuration for FCB processes.

Called once per process, before any other module logs. Sets up a stdout
stream handler with a consistent format. Later phases will add a SQLite
handler that mirrors WARNING and ERROR records into the `bot_events`
table so the dashboard's logs panel can render them.
"""

import logging
import sys

from fcb import config

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure() -> None:
    """Install stdout handler and set the root level. Idempotent."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
