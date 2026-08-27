"""Rotating file log plus console output. INFO-level lines never include
message bodies or attachment names; only guids and counts are logged."""
import logging
import logging.handlers
import sys

from . import constants


def setup_logging(debug: bool = False, filename: str = "jrl-messages.log") -> None:
    """The window and the agent are separate processes and must never share
    one rotating file: Windows rename-rotation fails while the other process
    holds the handle. The agent passes its own filename."""
    constants.LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    fh = logging.handlers.RotatingFileHandler(
        constants.LOG_DIR / filename,
        maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("socketio").setLevel(logging.WARNING)
