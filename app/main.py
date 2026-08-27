"""Window entry point.

Since 3.0.0 this process is a viewer and composer only. All networking,
sync, sending, and the Wake Mac machinery live in the background agent
(app/agent), which install.bat keeps running from logon. The window talks
to the agent over a local per-user channel and reads the same database.
"""
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from . import config, constants
from .logging_setup import setup_logging
from .store.db import Database
from .store.repo import Repo
from .ui import theme
from .ui.main_window import MainWindow


def main() -> int:
    constants.ensure_dirs()
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(constants.APP_NAME)
    # The main window hides to the notification area by default. Tool-card
    # lifetimes must never make Qt infer that the last window has closed.
    # MainWindow's explicit Quit path owns process shutdown.
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(constants.DATA_DIR / "app.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        # A copy is already running (very possibly hidden in the tray).
        # Ask it to come forward and exit quietly; launching the app twice
        # should feel like opening it, never like an error. The message box
        # remains only as the fallback when the running copy cannot hear us.
        try:
            from PySide6.QtNetwork import QLocalSocket
            sock = QLocalSocket()
            sock.connectToServer(constants.window_pipe_name())
            if sock.waitForConnected(1500):
                sock.disconnectFromServer()
                return 0
        except Exception:
            pass
        QMessageBox.information(
            None, constants.APP_NAME,
            "JRL Messages is already running. Check the system tray, or "
            "use the round power button inside the app to quit it "
            "completely.")
        return 0

    settings = config.load()
    theme.apply(app, settings.accent, settings.font_scale)
    db = Database(constants.DB_PATH)
    repo = Repo(db)

    win = MainWindow(repo, settings)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
