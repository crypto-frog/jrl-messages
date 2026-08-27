"""Main window. A viewer and composer over the shared database.

Since 3.0.0 no network worker lives here. The background agent (started
at logon, supervised, and restarted automatically) owns the socket, the
3-second reconciler, sending, downloads, the watchdog, Recover, and Wake
Mac. This window renders the agent's events, reads the same SQLite
database for everything heavy, and enqueues outgoing work durably in the
outbox table, so nothing the user does is lost even while the agent is
restarting.
"""
import logging
import os
import time

from PySide6.QtCore import (QByteArray, QEvent, QSize, Qt, QTimer, Slot)
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QListView,
                               QApplication, QMainWindow, QMenu,
                               QPushButton, QSplitter,
                               QSizePolicy, QSystemTrayIcon, QVBoxLayout,
                               QWidget)

from .. import config, constants
from ..api.rest import BBClient
from ..store.repo import Repo
from ..util.textutil import fts_escape, snippet
from ..util.timefmt import fmt_clock
from . import theme
from .agent_link import AgentLink
from .alert_center import AlertCenterPanel
from .chat_list import ChatListView, ListModel, Row, RowDelegate
from .compose_dialog import ComposeDialog
from .group_dialog import GroupDialog
from .activity_log import (ActivityLogHandler, ActivityPanel,
                           ActivityRecorder, WindowWarden)
from .connection_badge import ConnectionBadge
from .notify import (NotificationPopup, PopupManager, PresentationResult,
                     play_notification_sound)
from ..phone.link import PhoneLinkWorker
from ..util.codes import extract_code
from .icons import bell, bolt, eye_off, gear, pencil, power, refresh
from .settings_dialog import SettingsDialog
from .thread_view import ThreadView
from .tooltips import TooltipController

log = logging.getLogger(__name__)


def _app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(theme.ACCENT))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 16, 16)
    p.setPen(QColor("white"))
    f = p.font()
    f.setPointSize(24)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "J")
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, repo: Repo, settings):
        super().__init__()
        self.repo = repo
        self.settings = settings
        self.handles: dict = {}
        self.open_chat_guid = None
        self._socket_up = False
        self._socket_reason = ""
        self._agent_online = False
        self._draining_events = False
        self._queued_event_guids: set[str] = set()
        self._delivery_more_pending = False
        self._closing = False
        self._recovery_state = "idle"
        self._wake_state = "idle"
        self._wake_origin = "manual"
        self._really_quit = False
        self._shutdown_done = False
        self._tray_close_notice_shown = False
        self._last_notification_chat_guid = ""
        self._last_alert_signal = 0.0
        self._scroll_target_guid = None
        self._popup_ack_groups: dict[str, list[str]] = {}
        self._popup_pending_guids: set[str] = set()
        self._popup_signalled_keys: set[str] = set()
        self._connection_details = "No connection check has completed yet."
        self.caps = {"private_api": False}
        self._dialog_clients: list = []
        # Notification-center bookkeeping: which message GUIDs were already
        # written to the durable feed this session (the feed table dedupes
        # durably; this set only skips repeat work on 2.5 s sweeps), and
        # rate-limit state for connection-change entries.
        self._feed_logged_guids: set[str] = set()
        self._feed_link_kind = "ok"
        self._feed_link_down_ms = None    # None = never recorded
        self._feed_link_loss_recorded = False
        self._feed_phone_state = "idle"
        self._feed_phone_down_ms = None   # None = never recorded
        self._feed_phone_loss_recorded = False

        # Session activity: everything the app does (connections, errors,
        # refusals, resets) lands here and is visible live in the Activity
        # panel opened from the connection chip. Warnings and errors from
        # every module are mirrored in through a logging bridge.
        self.activity = ActivityRecorder(self)
        self._activity_handler = ActivityLogHandler(self.activity)
        logging.getLogger().addHandler(self._activity_handler)
        self._activity_panel = None
        self.activity.record("app", f"window v{constants.VERSION} starting")

        self.tooltip_controller = TooltipController(settings, self)
        QApplication.instance().installEventFilter(self.tooltip_controller)
        QApplication.instance().aboutToQuit.connect(self._shutdown)

        log.info("JRL Messages v%s window starting", constants.VERSION)
        self.setWindowTitle(
            f"{constants.APP_NAME}  ·  v{constants.VERSION}")
        self.setWindowIcon(_app_icon())
        self.resize(1180, 760)
        self.setMinimumSize(900, 600)

        self.split = QSplitter()
        self.split.setChildrenCollapsible(False)

        # ---- left rail
        left = QWidget()
        left.setStyleSheet(f"background: {theme.PANEL};")
        ll = QVBoxLayout(left)
        self.left_layout = ll
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(8)
        search_row = QHBoxLayout()
        self.search_row = search_row
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search messages   (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._on_search)
        search_row.addWidget(self.search, 1)
        self.compose_btn = QPushButton("New")
        self.compose_btn.setToolTip("New message (Ctrl+N)")
        self.compose_btn.setProperty("jrlTipId", "new-message")
        self.compose_btn.setAccessibleName("New message")
        self.compose_btn.setCursor(Qt.PointingHandCursor)
        self.compose_btn.clicked.connect(self.open_compose)
        search_row.addWidget(self.compose_btn)
        self.unread_pill = QPushButton()
        self.unread_pill.setCursor(Qt.PointingHandCursor)
        self.unread_pill.setToolTip("Show the newest unread conversation")
        self.unread_pill.setProperty("jrlTipId", "newest-unread")
        self.unread_pill.setAccessibleName(
            "Open newest unread conversation")
        self.unread_pill.clicked.connect(self._open_next_unread)
        self.unread_pill.hide()
        self._style_unread_pill()
        search_row.addWidget(self.unread_pill, 0, Qt.AlignVCenter)
        ll.addLayout(search_row)

        self.list = ChatListView()
        self.list.hide_requested.connect(self._hide_chat)
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.model = ListModel()
        self.list.setModel(self.model)
        self.list.setItemDelegate(RowDelegate(self.list))
        self.list.clicked.connect(self._on_row_clicked)
        self.list.activated.connect(self._on_row_clicked)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.empty_list_label = QLabel("")
        self.empty_list_label.setAlignment(Qt.AlignCenter)
        self.empty_list_label.setWordWrap(True)
        self.empty_list_label.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(9.5)}; "
            f"padding: {theme.dim(24)}px;")
        self.empty_list_label.hide()
        ll.addWidget(self.empty_list_label, 1)
        ll.addWidget(self.list, 1)

        self.undo_bar = QWidget()
        ub = QHBoxLayout(self.undo_bar)
        ub.setContentsMargins(8, 4, 8, 4)
        self.undo_label = QLabel("")
        self.undo_label.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(8.8)};")
        undo_btn = QPushButton("Undo")
        undo_btn.setObjectName("ghost")
        undo_btn.clicked.connect(self._undo_hide)
        ub.addWidget(self.undo_label, 1)
        ub.addWidget(undo_btn)
        self.undo_bar.setStyleSheet(
            f"background: {theme.PANEL2}; border: 1px solid {theme.BORDER}; "
            "border-radius: 9px;")
        self.undo_bar.hide()
        self._undo_stack: list = []
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(8000)
        self._undo_timer.timeout.connect(self.undo_bar.hide)
        ll.addWidget(self.undo_bar)

        # Connection chip: the old bare green dot is now a proper labelled
        # element in the same natural spot. A larger animated badge (an
        # orbiting comet while connected, a breathing ring while degraded,
        # a still broken ring when offline) sits with the status text in
        # one rounded chip; clicking either opens connection details.
        foot = QHBoxLayout()
        self.conn_chip = QWidget()
        self.conn_chip.setObjectName("connChip")
        self.conn_chip.setAccessibleName("Connection status")
        chip_lay = QHBoxLayout(self.conn_chip)
        self.conn_chip_layout = chip_lay
        self.badge = ConnectionBadge()
        self.badge.setToolTip("Connection state · click for details")
        self.badge.setProperty("jrlTipId", "connection-badge")
        self.badge.clicked.connect(self._show_connection_details)
        chip_lay.addWidget(self.badge, 0, Qt.AlignVCenter)
        self.status = QPushButton("Offline")
        self.status.setObjectName("connectionStatus")
        self.status.setFlat(True)
        self.status.setCursor(Qt.PointingHandCursor)
        self.status.setAccessibleName("Connection details")
        self.status.clicked.connect(self._show_connection_details)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setMinimumWidth(0)
        chip_lay.addWidget(self.status, 1)
        self._style_conn_chip()
        # The bell: opens the in-app notification center, a quiet feed of
        # everything the app has alerted on, including alerts raised while
        # nobody was watching. The unseen count is drawn into the icon.
        self.bell_btn = QPushButton()
        self.bell_btn.setObjectName("ghost")
        self.bell_btn.setToolTip(
            "Notifications: every alert this app has raised, including "
            "ones you were away for. (Ctrl+B)")
        self.bell_btn.setProperty("jrlTipId", "alert-center")
        self.bell_btn.setAccessibleName("Notifications")
        self.bell_btn.setCursor(Qt.PointingHandCursor)
        self.bell_btn.clicked.connect(self._toggle_alert_center)
        self.settings_btn = QPushButton()
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setProperty("jrlTipId", "settings")
        self.settings_btn.setAccessibleName("Settings")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.clicked.connect(self.open_settings)
        # The dedicated quit control: ends the window process completely,
        # every time, so nothing lingers for Task Manager. Sits beside the
        # gear with the same drawn-icon treatment; the background agent
        # keeps collecting, exactly as designed.
        self.quit_btn = QPushButton()
        self.quit_btn.setObjectName("ghost")
        self.quit_btn.setToolTip(
            "Quit completely: closes this window and its notifications "
            "and fully ends the process. Background collection by the "
            "agent continues.")
        self.quit_btn.setProperty("jrlTipId", "quit-completely")
        self.quit_btn.setAccessibleName("Quit completely")
        self.quit_btn.setCursor(Qt.PointingHandCursor)
        self.quit_btn.clicked.connect(self.quit_completely)
        foot.addWidget(self.conn_chip, 1)
        foot.addWidget(self.bell_btn)
        foot.addWidget(self.settings_btn)
        foot.addWidget(self.quit_btn)
        ll.addLayout(foot)

        self.foot_actions = QHBoxLayout()
        self.foot_actions.setSpacing(theme.dim(7))
        self.hidden_btn = QPushButton("Hidden")
        self.hidden_btn.setToolTip("Open hidden conversations")
        self.hidden_btn.setProperty("jrlTipId", "hidden-conversations")
        self.hidden_btn.setAccessibleName("Open hidden conversations")
        self.hidden_btn.setCursor(Qt.PointingHandCursor)
        self.hidden_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.hidden_btn.clicked.connect(self._restore_hidden_dialog)
        self.foot_actions.addWidget(self.hidden_btn, 1)
        self.recover_btn = QPushButton("Recover")
        self.recover_btn.setToolTip(
            "Run a full line repair: rebuild the Windows connection, rescan "
            "the Mac database, then safely restart Messages on the Mac to "
            "release anything Apple is holding. Local messages, settings, "
            "and history are not deleted. (Ctrl+Shift+R)")
        self.recover_btn.setProperty("jrlTipId", "recover-line")
        self.recover_btn.setAccessibleName(
            "Reconnect and rescan recent messages")
        self.recover_btn.setCursor(Qt.PointingHandCursor)
        self.recover_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.recover_btn.clicked.connect(self.recover_messages)
        self.foot_actions.addWidget(self.recover_btn, 1)
        ll.addLayout(self.foot_actions)
        self.wake_btn = QPushButton("Wake Mac")
        self.wake_btn.setToolTip(
            "Restart Messages on the Mac so Apple hands over any texts it "
            "is still holding back, then re-check everything here. Nothing "
            "is sent and nothing is deleted. Use this when texts visible "
            "on your iPhone have not arrived here. The agent also does "
            "this automatically after quiet periods; see Settings. "
            "(Ctrl+Shift+M)")
        self.wake_btn.setProperty("jrlTipId", "wake-mac")
        self.wake_btn.setAccessibleName(
            "Restart Messages on the Mac to fetch held-back texts")
        self.wake_btn.setCursor(Qt.PointingHandCursor)
        self.wake_btn.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.wake_btn.clicked.connect(self.wake_mac)
        ll.addWidget(self.wake_btn)
        self._style_left_actions()

        # ---- thread pane
        self.thread = ThreadView(self.repo)
        self.thread.send_message.connect(self._on_send)
        self.thread.need_download.connect(self._on_need_download)
        self.thread.retry_outbox.connect(self._on_retry)
        self.thread.refresh_requested.connect(self._on_refresh_chat)
        self.thread.group_details.connect(self.open_group_details)

        self.split.addWidget(left)
        self.split.addWidget(self.thread)
        self.split.setSizes([340, 840])
        self.setCentralWidget(self.split)
        self._restore_layout()

        # The notification center is a CHILD overlay of this window, never
        # a new top-level window (the storm rule), anchored above the bell.
        self.alert_center = AlertCenterPanel(
            self, self.repo, on_open_chat=self._open_from_feed,
            on_changed=self._update_bell)
        try:
            self.repo.feed_prune()
        except Exception:
            log.exception("Feed prune failed; continuing")

        self.popups = PopupManager(
            self._popup_open, anchor_widget=self,
            on_presented=self._on_popup_presented,
            on_rejected=self._on_popup_rejected)
        # The warden watches for any top-level window that should not
        # exist, names it in the Activity log, and hides it. See
        # activity_log.py; this is the flight recorder for the ghost
        # window storms and the guarantee they stay off the screen.
        self.warden = WindowWarden(
            self, self.activity, extra_expected=(NotificationPopup,), parent=self)
        self.warden.start()
        self.tray = QSystemTrayIcon(_app_icon(), self)
        self.tray.setToolTip(constants.APP_NAME)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self._open_last_notification)
        # Parented and kept: an unowned local QMenu is collected by Python
        # and dies underneath the tray icon.
        self._tray_menu = QMenu(self)
        open_action = self._tray_menu.addAction("Open JRL Messages")
        open_action.triggered.connect(self._show_window)
        settings_action = self._tray_menu.addAction("Settings…")
        settings_action.triggered.connect(
            lambda: (self._show_window(), self.open_settings()))
        self._tray_menu.addSeparator()
        quit_action = self._tray_menu.addAction(
            "Quit window and notifications")
        quit_action.triggered.connect(self._quit_from_tray)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.show()

        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(300)
        self._reload_timer.timeout.connect(self.reload_chats)

        self._server_newest_ts = 0
        self._verify = None            # {"token": str, "t0": float}
        self._toast_queue: list = []
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(900)
        self._toast_timer.timeout.connect(self._flush_toasts)

        # Independent of the agent's local event channel: if Windows delays or
        # drops a batch signal while minimized, the durable ledger still
        # presents its alert within this bounded interval.
        self._notification_sweep = QTimer(self)
        self._notification_sweep.setInterval(constants.NOTIFICATION_SWEEP_MS)
        self._notification_sweep.timeout.connect(self._drain_delivery_events)
        self._notification_sweep.start()

        self._repair_stranded_hidden()
        # Replay any unread/notification work left unacknowledged while no
        # window was open. The agent keeps collecting around the clock; this
        # is where a returning user's unread counts and fresh popups come from.
        QTimer.singleShot(0, self._drain_delivery_events)

        esc = QShortcut(QKeySequence(Qt.Key_Escape), self.search,
                        activated=lambda: (self.search.clear(),
                                           self.list.setFocus()))
        esc.setContext(Qt.WidgetShortcut)
        QShortcut(QKeySequence("Ctrl+F"), self,
                  activated=lambda: (self.search.setFocus(),
                                     self.search.selectAll()))
        QShortcut(QKeySequence("Ctrl+K"), self,
                  activated=lambda: (self.search.setFocus(),
                                     self.search.selectAll()))
        QShortcut(QKeySequence("F5"), self, activated=self._refresh_current)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.open_compose)
        QShortcut(QKeySequence("Ctrl+Shift+R"), self,
                  activated=self.recover_messages)
        QShortcut(QKeySequence("Ctrl+Shift+M"), self,
                  activated=self.wake_mac)
        QShortcut(QKeySequence("Ctrl+H"), self,
                  activated=lambda: self.open_chat_guid
                  and self._hide_chat(self.open_chat_guid))
        QShortcut(QKeySequence("Ctrl+Tab"), self,
                  activated=lambda: self._cycle(1))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self,
                  activated=lambda: self._cycle(-1))
        QShortcut(QKeySequence("Ctrl+B"), self,
                  activated=self._toggle_alert_center)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
        QShortcut(QKeySequence("Ctrl+L"), self,
                  activated=self._show_connection_details)
        QShortcut(QKeySequence("Ctrl+E"), self,
                  activated=lambda: self.thread.export_conversation())
        QShortcut(QKeySequence("Ctrl+/"), self,
                  activated=self._show_shortcuts)
        QShortcut(QKeySequence("F1"), self, activated=self._show_shortcuts)

        # ---- agent channel
        self.agent = AgentLink(self)
        a = self.agent
        a.hello.connect(self._on_agent_hello)
        a.status.connect(self._on_agent_status)
        a.batch.connect(self._on_agent_batch)
        a.chats_refreshed.connect(self._on_contacts_or_chats)
        a.chat_refreshed.connect(self._on_chat_refreshed)
        a.backfill_page.connect(self._on_backfill_page)
        a.socket_state.connect(self._on_socket_state)
        a.caps_changed.connect(self._on_caps)
        a.wake_event.connect(self._on_wake_event)
        a.recovery_event.connect(self._on_recovery_event)
        a.outbox_changed.connect(self._on_outbox_changed)
        a.message_sent.connect(self._on_message_sent)
        a.attachment_ready.connect(self._on_attachment_ready)
        a.attachment_failed.connect(self._on_download_failed)
        a.push_broken.connect(self._on_push_broken)
        a.link_state.connect(self._on_link_state)
        a.poll_failed.connect(
            lambda err: self.activity.record("check", f"retrying: {err}"))
        a.backfill_done.connect(
            lambda: self.activity.record("sync", "history indexing complete"))

        # Single-instance activation: a second launch of the app finds this
        # channel and asks the existing window to come forward, instead of
        # the old "already running" error that sent the user to Task
        # Manager. Best-effort: if the name is taken (test harnesses build
        # several windows in one process), this window simply skips it.
        self._activation_server = None
        try:
            from PySide6.QtNetwork import QLocalServer
            name = constants.window_pipe_name()
            server = QLocalServer(self)
            server.setSocketOptions(QLocalServer.UserAccessOption)
            QLocalServer.removeServer(name)
            if server.listen(name):
                server.newConnection.connect(self._on_activation_request)
                self._activation_server = server
            else:
                log.info("Window activation channel unavailable: %s",
                         server.errorString())
        except Exception:
            log.exception("Window activation channel could not start")

        # iPhone notification mirroring over Bluetooth (experimental).
        # The worker object always exists so its handlers are testable;
        # the radio itself starts only when enabled with a chosen phone,
        # and never under the smoke harnesses.
        self.phone = PhoneLinkWorker(self.settings, self)
        self.phone.notification.connect(
            self._on_phone_notification, Qt.QueuedConnection)
        self.phone.status.connect(
            self._on_phone_status, Qt.QueuedConnection)
        self.phone.learned.connect(
            self._on_phone_learned, Qt.QueuedConnection)
        self.phone.test_result.connect(
            self._on_phone_test_result, Qt.QueuedConnection)
        self._phone_last_address = (
            getattr(self.settings, "phone_ble_address", "") or "").strip()

        if os.environ.get("JRL_SMOKE"):
            return
        self.agent.start()
        self._apply_phone_link_settings()
        pw = config.get_password(self.settings)
        if not self.settings.server_url or not pw:
            QTimer.singleShot(150, self.open_settings)

    def _on_activation_request(self):
        server = self._activation_server
        if server is None:
            return
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is not None:
                sock.disconnectFromServer()
                sock.deleteLater()
        self.activity.record("app", "second launch detected; showing this window")
        self._show_window()

    # ------------------------------------------------ agent channel

    @Slot(str)
    def _on_link_state(self, state: str):
        self._agent_online = state == "connected"
        self.activity.record("link", {
            "connected": "agent channel connected",
            "connecting": "connecting to the background agent…",
            "offline": "agent channel offline · restart requested",
        }.get(state, state))
        if state == "offline" and not self._closing:
            self.set_status(
                "fail",
                "Background agent offline · restarting it automatically…")
        elif state == "connecting" and not self._closing:
            self.set_status("warn", "Connecting to the background agent…")

    @Slot(dict)
    def _on_agent_hello(self, hello: dict):
        self._agent_online = True
        agent_version = hello.get("version") or ""
        if agent_version and agent_version != constants.VERSION:
            self.activity.record(
                "agent", f"version mismatch (agent {agent_version}, window "
                f"{constants.VERSION}); rotating the agent")
            self.set_status(
                "warn", f"Updating background agent {agent_version} → "
                f"{constants.VERSION}…")
            self.agent.restart_for_upgrade()
            return
        self.activity.record(
            "agent", f"ready · v{agent_version or '?'} pid "
            f"{hello.get('pid', '?')}")
        self.caps.update(hello.get("caps") or {})
        self._socket_up = bool(hello.get("socket_up"))
        self._socket_reason = hello.get("socket_reason") or ""
        self._wake_state = hello.get("wake_state") or "idle"
        self._wake_origin = hello.get("wake_origin") or "manual"
        self._recovery_state = hello.get("recovery_state") or "idle"
        ts = hello.get("newest_ts") or 0
        self._server_newest_ts = max(self._server_newest_ts, int(ts))
        self._style_left_actions()
        self.set_status(hello.get("status_kind") or "warn",
                        hello.get("status_text") or "Connecting…")
        self._schedule_reload()
        if self.open_chat_guid:
            self.thread.refresh_from_repo(preserve_scroll=True)
        self._drain_delivery_events()

    @Slot(str, str, object)
    def _on_agent_status(self, kind: str, text: str, newest_ts):
        try:
            if newest_ts:
                self._server_newest_ts = max(
                    self._server_newest_ts, int(newest_ts))
        except (TypeError, ValueError):
            pass
        self.activity.record(
            {"ok": "status", "warn": "warn", "fail": "error"}.get(
                kind, "status"), text)
        self.set_status(kind, text)

    @Slot(dict)
    def _on_caps(self, caps: dict):
        self.caps.update(caps or {})

    @Slot(bool, str)
    def _on_socket_state(self, up: bool, reason: str):
        self._socket_up = up
        self._socket_reason = reason
        self.activity.record(
            "push", "live push connected" if up
            else f"live push offline ({reason or 'no reason given'})")

    # ------------------------------------------------ dialogs

    def _dialog_client(self):
        """A short-lived REST client for dialog-driven server work (create
        chat, group membership). Dialogs run these calls on their own worker
        threads and surface failures inline, exactly as before; the client
        is closed after the dialog finishes."""
        pw = config.get_password(self.settings)
        base = self.settings.base_url()
        if not base or not pw:
            return None
        client = BBClient(base, pw)
        self._dialog_clients.append(client)
        return client

    def _close_dialog_clients(self):
        for client in self._dialog_clients:
            try:
                client.close()
            except Exception:
                pass
        self._dialog_clients.clear()

    def open_compose(self):
        client = self._dialog_client()
        dlg = ComposeDialog(self.repo, client, self,
                            on_open=self._open_new_target,
                            private_api=self.caps.get("private_api", False))
        dlg.exec()
        self._close_dialog_clients()

    def open_group_details(self, chat_guid: str):
        client = self._dialog_client()
        dlg = GroupDialog(self.repo, client, chat_guid, self.handles,
                          self.caps.get("private_api", False), self,
                          on_changed=self._on_group_changed)
        dlg.exec()
        self._close_dialog_clients()

    def _on_group_changed(self, chat_guid: str):
        self.agent.send({"cmd": "group_changed", "chat_guid": chat_guid})
        if chat_guid == self.open_chat_guid:
            self.open_conversation(chat_guid)
        self._schedule_reload()

    def _open_new_target(self, chat_guid: str):
        self.repo.ensure_chat(chat_guid)
        self._resurrect_if_hidden(chat_guid, "opened from compose")
        self.agent.send({"cmd": "poke_chat", "chat_guid": chat_guid})
        self.agent.send({"cmd": "poke", "chats": True})
        self.open_conversation(chat_guid)

    def open_settings(self):
        before = (self.settings.server_url, config.get_password(self.settings))
        dlg = SettingsDialog(self.settings, self,
                             on_preview=self.apply_theme,
                             on_test=self._test_popup,
                             on_verify=self.verify_line,
                             on_reset_tips=self.tooltip_controller.reset_learned,
                             on_phone_test=self.test_phone_link,
                             phone_status=self.phone_link_status_text,
                             phone_pause=self._pause_phone_for_setup)
        appearance_before = (self.settings.accent, self.settings.font_scale)
        if not dlg.exec():
            self.apply_theme()   # restore the saved look after Cancel
            # The setup wizard pauses the phone link while it owns the
            # radio; a Cancel must still hand the link back.
            self._apply_phone_link_settings()
            return
        # Re-theming repolishes every widget in the application; do that
        # work only when the appearance actually changed. Saving an
        # unrelated setting (tips, sounds, auto-wake) must not trigger an
        # app-wide restyle storm.
        if (self.settings.accent, self.settings.font_scale) != \
                appearance_before:
            self.apply_theme()
        self.tooltip_controller.set_mode(self.settings.tooltip_mode)
        self._update_bell()
        self._apply_phone_link_settings()
        after = (self.settings.server_url, config.get_password(self.settings))
        self.activity.record("settings", "saved · agent notified")
        # The agent re-reads config.json itself; it restarts its backend
        # only when the connection details actually changed.
        self.agent.send({"cmd": "settings_changed"})
        if after != before:
            self.set_status("ok", "Applying the new connection settings…")

    def apply_theme(self, accent=None, scale=None):
        from PySide6.QtWidgets import QApplication
        theme.apply(QApplication.instance(),
                    accent if accent is not None else self.settings.accent,
                    scale if scale is not None else self.settings.font_scale)
        self._style_conn_chip()
        self.empty_list_label.setStyleSheet(
            f"color: {theme.MUTED}; font-size: {theme.fs(9.5)}; "
            f"padding: {theme.dim(24)}px;")
        self._style_left_actions()
        self.thread.restyle()
        self._style_unread_pill()
        self.alert_center.restyle()
        self.reload_chats()
        if self.open_chat_guid:
            self.open_conversation(self.open_chat_guid)

    # ------------------------------------------------ recover and wake

    def recover_messages(self):
        """Ask the agent to non-destructively rebuild every transport and
        rescan recent mail. Transient presentation work is dropped here;
        the durable delivery ledger is replayed after the rescan."""
        if self._closing or self._recovery_state == "working":
            return
        if not self.settings.base_url() or not config.get_password(self.settings):
            self.set_status("fail", "Set the server address and password first")
            return
        self._toast_timer.stop()
        self._toast_queue.clear()
        self._queued_event_guids.clear()
        self._delivery_more_pending = False
        self._reload_timer.stop()
        self.reload_chats()
        if self.open_chat_guid:
            self.thread.refresh_from_repo(preserve_scroll=True)
        sent = self.agent.send({"cmd": "recover"})
        if not sent:
            self.set_status(
                "warn",
                "Agent starting… recovery will run as soon as it is back.")

    def wake_mac(self):
        """Ask the agent to restart Messages on the Mac (see agent core)."""
        if self._closing or self._wake_state in ("working", "watching"):
            return
        sent = self.agent.send({"cmd": "wake"})
        if not sent:
            self.set_status(
                "warn",
                "Agent starting… press Wake Mac again in a moment.")

    @Slot(dict)
    def _on_wake_event(self, event: dict):
        self._wake_state = event.get("state") or "idle"
        self._wake_origin = event.get("origin") or "manual"
        found = event.get("found") or 0
        detail = f" · {found} recovered" if found else ""
        self.activity.record(
            "wake", f"{self._wake_origin} wake {self._wake_state}{detail}")
        if self._wake_state == "success":
            self._feed_record(
                "wake", "Mac woken",
                f"{found} held-back texts recovered" if found
                else f"Messages restarted ({self._wake_origin} wake)")
        self._style_left_actions()

    @Slot(dict)
    def _on_recovery_event(self, event: dict):
        state = event.get("state") or "idle"
        restored = event.get("restored") or 0
        self.activity.record(
            "repair", f"recovery {state}"
            + (f" · {restored} restored" if restored else ""))
        finished = (self._recovery_state == "working"
                    and state in ("success", "idle"))
        self._recovery_state = state
        self._style_left_actions()
        if finished:
            self._feed_record(
                "repair", "Line recovered",
                f"{restored} messages restored" if restored
                else "transports rebuilt and recent history rescanned")
            self.reload_chats()
            if self.open_chat_guid:
                self.thread.refresh_from_repo(preserve_scroll=True)
            self._drain_delivery_events()

    # ------------------------------------------------ layout persistence

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.open_chat_guid:
            self.thread.refresh_from_repo(preserve_scroll=True)
        self._drain_delivery_events()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def quit_completely(self):
        """The dedicated close: ends the window process outright, every
        time, so a relaunch never finds a leftover copy. The background
        agent is untouched and keeps collecting."""
        self.activity.record("app", "quit requested; ending the process")
        self._really_quit = True
        self.close()
        QApplication.instance().quit()

    def _quit_from_tray(self):
        self.quit_completely()

    def _shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._closing = True
        self._save_layout()
        self._notification_sweep.stop()
        self.warden.stop()
        try:
            logging.getLogger().removeHandler(self._activity_handler)
        except Exception:
            pass
        QApplication.instance().removeEventFilter(self.tooltip_controller)
        try:
            self.phone.stop()
        except Exception:
            pass
        self.agent.stop()
        self._close_dialog_clients()
        self.tray.hide()
        if self._activation_server is not None:
            try:
                self._activation_server.close()
            except Exception:
                pass
        # The absolute guarantee behind the quit control: if anything ever
        # keeps this interpreter alive after shutdown (a stuck handle, a
        # misbehaving library thread), the process is ended outright. No
        # Task Manager, no "already running" surprises. Test harnesses
        # (JRL_SMOKE) construct and close windows repeatedly and are the
        # one place this hard exit must not fire.
        if not os.environ.get("JRL_SMOKE"):
            QTimer.singleShot(2500, lambda: os._exit(0))

    def closeEvent(self, e):
        panel = getattr(self, "alert_center", None)
        if panel is not None and panel.isVisible():
            panel.hide_panel()
        if (not self._really_quit
                and getattr(self.settings, "close_to_tray", True)
                and QSystemTrayIcon.isSystemTrayAvailable()
                and self.tray.isVisible()):
            self._save_layout()
            self.hide()
            e.ignore()
            if not self._tray_close_notice_shown:
                self._tray_close_notice_shown = True
                if (QSystemTrayIcon.isSystemTrayAvailable()
                        and QSystemTrayIcon.supportsMessages()):
                    self.tray.showMessage(
                        "JRL Messages is still monitoring",
                        "The window is in the notification area. Use its "
                        "menu to quit completely.", _app_icon(), 5000)
            return
        self._really_quit = True
        self._shutdown()
        super().closeEvent(e)
        QTimer.singleShot(0, QApplication.instance().quit)

    def changeEvent(self, e):
        if e.type() == QEvent.ActivationChange and self.isActiveWindow():
            self.agent.send({"cmd": "poke"})
            if self.open_chat_guid:
                self.thread.refresh_from_repo(preserve_scroll=True)
            self._drain_delivery_events()
        super().changeEvent(e)

    def _save_layout(self):
        try:
            self.settings.win_geometry = bytes(
                self.saveGeometry().toHex()).decode("ascii")
            self.settings.splitter_sizes = ",".join(
                str(x) for x in self.split.sizes())
            config.save(self.settings)
        except Exception:
            log.exception("Could not save window layout")

    def _restore_layout(self):
        try:
            if self.settings.win_geometry:
                self.restoreGeometry(QByteArray.fromHex(
                    self.settings.win_geometry.encode("ascii")))
            if self.settings.splitter_sizes:
                sizes = [int(x) for x in self.settings.splitter_sizes.split(",")]
                if len(sizes) == 2 and all(s > 50 for s in sizes):
                    self.split.setSizes(sizes)
        except Exception:
            log.exception("Could not restore window layout")

    # ------------------------------------------------ status

    def _style_conn_chip(self):
        """Size and paint the connection chip from the current theme."""
        self.conn_chip_layout.setContentsMargins(
            theme.dim(8), theme.dim(4), theme.dim(8), theme.dim(4))
        self.conn_chip_layout.setSpacing(theme.dim(8))
        self.conn_chip.setStyleSheet(
            f"QWidget#connChip {{ background: {theme.PANEL2}; "
            f"border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.dim(11)}px; }}")
        self.status.setStyleSheet(
            f"QPushButton {{ color: {theme.MUTED}; border: none; padding: 0; "
            f"background: transparent; "
            f"text-align: left; font-size: {theme.fs(8.8)}; }} "
            f"QPushButton:hover {{ color: {theme.ACCENT}; }}")
        self.badge.restyle()

    def set_status(self, kind: str, text: str):
        self.badge.set_state(
            kind if kind in ("ok", "warn", "fail") else "warn")
        self.status.setText(text)
        import datetime as _dt
        newest = (fmt_clock(self._server_newest_ts)
                  if self._server_newest_ts else "none seen yet")
        last_sync_ms = self.repo.meta_int("last_successful_sync_ms", 0)
        last_sync = (_dt.datetime.fromtimestamp(last_sync_ms / 1000).strftime(
            "%H:%M:%S") if last_sync_ms else "not completed yet")
        agent_line = ("agent connected" if self._agent_online
                      else "agent offline")
        self._connection_details = (
            f"Status: {text}\n"
            f"Newest message synchronized here: {newest}\n"
            f"{self.settings.base_url()}  ·  {agent_line}\n"
            f"Last successful global check: {last_sync}")
        self.status.setToolTip(self._connection_details)
        self.status.setAccessibleDescription(self._connection_details)
        self._record_link_transition(kind, text)

    def _show_connection_details(self):
        """Open the live Activity panel: connection details on top, the
        session's full event stream underneath. Reachable even with help
        tips Off, from the badge or the status text."""
        if self._activity_panel is not None:
            try:
                self._activity_panel.raise_()
                self._activity_panel.activateWindow()
                return
            except RuntimeError:
                self._activity_panel = None
        panel = ActivityPanel(
            self.activity, lambda: self._connection_details, self)
        panel.setAttribute(Qt.WA_DeleteOnClose, True)
        panel.destroyed.connect(
            lambda *_: setattr(self, "_activity_panel", None))
        self._activity_panel = panel
        panel.show()

    # ------------------------------------------------ notification center

    def _toggle_alert_center(self):
        if not getattr(self.settings, "alert_center_enabled", True):
            return
        self.alert_center.toggle(self.bell_btn)

    def _open_from_feed(self, chat_guid: str):
        self.open_conversation(chat_guid)

    def _update_bell(self):
        """Repaint the bell from the durable feed: hidden when the center
        is switched off, otherwise carrying the unseen count in the icon."""
        enabled = bool(getattr(self.settings, "alert_center_enabled", True))
        self.bell_btn.setVisible(enabled)
        panel = getattr(self, "alert_center", None)
        if not enabled:
            if panel is not None and panel.isVisible():
                panel.hide_panel()
            return
        try:
            unseen = self.repo.feed_unseen_count()
        except Exception:
            unseen = 0
        badge = ("9+" if unseen > 9 else str(unseen)) if unseen else ""
        self.bell_btn.setIcon(
            bell(theme.ACCENT, badge=badge, badge_color=theme.FAIL))

    def _feed_record(self, kind: str, title: str, body: str = "",
                     chat_guid=None, message_guid=None, created_ms=None):
        """Write one notification-center entry. Feed bookkeeping must
        never break the alert path, so every failure only logs."""
        try:
            added = self.repo.feed_add(kind, title, body, chat_guid,
                                       message_guid, created_ms)
            if added:
                self._update_bell()
                panel = getattr(self, "alert_center", None)
                if panel is not None:
                    panel.refresh_if_visible()
        except Exception:
            log.exception("Notification feed write failed")

    def _record_link_transition(self, kind: str, text: str):
        """One feed entry when the line goes down, one when it returns,
        rate-limited so a flapping link cannot fill the center. The
        never-recorded state is an explicit None: time.monotonic()
        counts from machine boot, so treating 0.0 as a timestamp would
        silently swallow every transition in the first ten minutes
        after a reboot (found as a once-in-a-boot harness flake)."""
        if kind not in ("ok", "fail"):
            return
        previous = self._feed_link_kind
        self._feed_link_kind = kind
        if kind == "fail" and previous != "fail":
            now = time.monotonic()
            if (self._feed_link_down_ms is None
                    or now - self._feed_link_down_ms > 600):
                self._feed_link_down_ms = now
                self._feed_link_loss_recorded = True
                self._feed_record("link-down", "Connection lost", text)
        elif (kind == "ok" and previous == "fail"
                and self._feed_link_loss_recorded):
            # Recoveries pair one-to-one with recorded losses, so a
            # flapping link cannot fill the bell from either side.
            self._feed_link_loss_recorded = False
            self._feed_record("link-up", "Connection restored", text)

    # ------------------------------------------------ iPhone over Bluetooth

    @Slot(dict)
    def _on_phone_notification(self, d: dict):
        """One mirrored iPhone notification: a bell entry plus the same
        popup and sound pipeline as a message, honoring the two master
        switches. No unread state; these are glances, not conversations."""
        try:
            app_name = d.get("app_name") or "iPhone"
            title = f"{app_name} · iPhone"
            body = d.get("body") or "New notification"
            when = d.get("when_ms") or int(time.time() * 1000)
            marker = f"ancs-{d.get('uid')}-{int(when / 1000)}"
            self._feed_record("phone", title, body, None, marker,
                              created_ms=when)
            if not bool(getattr(self.settings, "popups_enabled", True)):
                if getattr(self.settings, "notification_sound", True):
                    self._signal_notification()
                return
            mode = getattr(self.settings, "notify_mode", "popup")
            self._present_notification(mode, title, body, None, "", [])
        except Exception:
            log.exception("Phone notification presentation failed")

    @Slot(str, str)
    def _on_phone_status(self, level: str, text: str):
        """Every link state change lands in the Activity panel; the bell
        keeps only the meaningful transitions, rate limited."""
        self.activity.record(
            "error" if level == "error" else "phone", text)
        if level == "up" and self._feed_phone_state != "up":
            previous = self._feed_phone_state
            self._feed_phone_state = "up"
            # The first connect always celebrates; reconnects only pair
            # with a recorded loss, so range-edge flapping stays quiet.
            if previous == "idle" or self._feed_phone_loss_recorded:
                self._feed_phone_loss_recorded = False
                self._feed_record(
                    "phone-up", "iPhone link connected", text)
        elif level == "down":
            previous = self._feed_phone_state
            self._feed_phone_state = "down"
            now = time.monotonic()
            if previous in ("up", "idle"):
                if (self._feed_phone_down_ms is None
                        or now - self._feed_phone_down_ms > 600):
                    self._feed_phone_down_ms = now
                    self._feed_phone_loss_recorded = True
                    self._feed_record(
                        "phone-down", "iPhone link lost", text)
            elif (self._feed_phone_down_ms is not None
                    and now - self._feed_phone_down_ms > 1800):
                # A long outage gets a quiet half-hourly reminder, so
                # the bell never again reads as if the app gave up
                # while it was in fact still retrying.
                self._feed_phone_down_ms = now
                self._feed_phone_loss_recorded = True
                self._feed_record(
                    "phone-down", "iPhone still unreachable", text)

    def _pause_phone_for_setup(self):
        """The connect wizard needs sole ownership of the radio while
        it pairs; the background link resumes after the dialog closes
        (Save and Cancel both reapply the link settings)."""
        try:
            if self.phone.running():
                self.phone.stop()
                self.activity.record(
                    "phone", "link paused while the setup wizard runs")
        except Exception:
            log.exception("Could not pause the phone link")

    def phone_link_status_text(self) -> str:
        """One live sentence about the iPhone link, for Settings."""
        if not bool(getattr(self.settings, "phone_link_enabled", False)):
            return "iPhone mirroring is off"
        if not (getattr(self.settings, "phone_ble_address", "")
                or "").strip():
            return "No iPhone chosen yet"
        if not self.phone.running():
            return "Not running (will start on Save)"
        if self.phone.is_connected():
            return "Connected now"
        return "Not connected right now; the app keeps retrying"

    def test_phone_link(self):
        """The iPhone-link test button: a real round trip when the link
        is live, and a plain-language explanation with an immediate
        reconnect when it is not. Every outcome becomes a visible
        alert and a bell entry, so the answer is never buried."""
        enabled = bool(getattr(self.settings, "phone_link_enabled", False))
        address = (getattr(self.settings, "phone_ble_address", "")
                   or "").strip()
        if not enabled or not address:
            self._phone_test_feedback(
                False, "iPhone mirroring is not set up: turn it on in "
                "Settings → Alerts and use Find my iPhone first.")
            return
        if not self.phone.running():
            self._apply_phone_link_settings()
            self._phone_test_feedback(
                False, "The link was not running; it is starting now. "
                "Test again in a few seconds.")
            return
        if not self.phone.is_connected():
            self.phone.kick()
            self._phone_test_feedback(
                False, "Not connected to the phone right now; "
                "reconnecting immediately. Keep the phone near the PC "
                "and watch the bell for 'iPhone link connected', then "
                "test again.")
            return
        self.activity.record("phone", "link test requested")
        self.phone.request_link_test()

    @Slot(bool, str)
    def _on_phone_test_result(self, ok: bool, text: str):
        self._phone_test_feedback(ok, text)

    def _phone_test_feedback(self, ok: bool, text: str):
        """Show a test outcome exactly like a real alert."""
        try:
            title = ("iPhone link · working" if ok
                     else "iPhone link · not working yet")
            self.activity.record("phone" if ok else "warn",
                                 f"link test: {text}")
            self._feed_record("phone" if ok else "phone-down",
                              title, text)
            mode = getattr(self.settings, "notify_mode", "popup")
            self._present_notification(mode, title, text, None, "", [])
        except Exception:
            log.exception("Could not present the link test outcome")

    @Slot(str, str)
    def _on_phone_learned(self, name: str, address: str):
        """The worker re-found the phone at a new address (rotation or
        re-pairing). Persist it so the next start connects directly,
        and keep the re-aim logic from treating it as a user change."""
        try:
            if not address:
                return
            self.settings.phone_ble_address = address
            if name:
                self.settings.phone_ble_name = name
            self._phone_last_address = address.strip()
            config.save(self.settings)
            self.activity.record(
                "phone", f"iPhone re-found at {address}; remembered")
        except Exception:
            log.exception("Could not persist the learned phone address")

    def _apply_phone_link_settings(self):
        """Start, stop, or re-aim the Bluetooth link after a settings
        change. Never runs under the smoke harnesses."""
        if os.environ.get("JRL_SMOKE"):
            return
        enabled = bool(getattr(self.settings, "phone_link_enabled", False))
        address = (getattr(self.settings, "phone_ble_address", "")
                   or "").strip()
        want = enabled and bool(address)
        if enabled and not address:
            self.activity.record(
                "phone", "iPhone mirroring is on but no phone is chosen; "
                "Settings → Alerts → Choose iPhone")
        if want and not self.phone.running():
            self.activity.record("phone", "phone link starting")
            self.phone.start()
        elif not want and self.phone.running():
            self.phone.stop()
            self._feed_phone_state = "idle"
            self.activity.record("phone", "phone link switched off")
        elif want and address != self._phone_last_address:
            self.phone.stop()
            self._feed_phone_state = "idle"
            QTimer.singleShot(1500, self.phone.start)
            self.activity.record("phone", "phone link re-aimed")
        self._phone_last_address = address

    def _alert_title_body(self, event, chat_guid: str):
        """(chat row, alert title, alert body) for one delivery event,
        shared by the popup path and the notification center."""
        chat = self.repo.db.one(
            "SELECT * FROM chats WHERE guid=?", (chat_guid,))
        title = (self.repo.chat_title(chat, self.handles)
                 if chat else "New message")
        body = event["text"] or "Attachment"
        sender = self.repo.name_for(event["sender_address"], self.handles)
        if chat is not None and chat["is_group"] and sender:
            body = f"{sender}: {body}"
        return chat, title, body

    def resizeEvent(self, event):
        super().resizeEvent(event)
        panel = getattr(self, "alert_center", None)
        if panel is not None and panel.isVisible():
            panel.reposition()

    def _self_chat_guid(self):
        info_addr = (self.caps.get("account") or "").strip()
        if info_addr:
            from ..util.textutil import normalize_address
            g = self.repo.chat_for_address(normalize_address(info_addr))
            if g:
                return g
        return None

    def verify_line(self):
        """Send a timestamped check to your own conversation and measure
        the round trip through the Mac and Apple."""
        if not self._agent_online:
            self.set_status("fail", "Agent offline. Try again in a moment.")
            return
        target = self._self_chat_guid()
        if not target:
            self.popups.show(
                "Verify line needs your self-conversation",
                "Text yourself once from your iPhone (your own number), "
                "then press Verify again.", None, "")
            return
        import datetime as _dt
        token = f"JRL line check {_dt.datetime.now().strftime('%H:%M:%S')}"
        self._verify = {"token": token, "t0": time.monotonic()}
        oid = self.repo.enqueue(target, token, None)
        self.agent.send({"cmd": "submit_outbox", "id": oid})
        self.thread.refresh_outbox()
        self.set_status("ok", "Verifying the line…")
        def timeout():
            if self._verify and self._verify["token"] == token:
                self._verify = None
                self.set_status(
                    "warn",
                    "Verify: the Mac accepted the send but Apple's echo "
                    "has not returned; the relay is lagging")
                log.warning("Line verify timed out (relay lag)")
        QTimer.singleShot(30000, timeout)

    def _check_verify_echo(self, m: dict, is_new: bool):
        v = self._verify
        if not v or not is_new or not m.get("is_from_me"):
            return
        if v["token"] in (m.get("text") or ""):
            elapsed = time.monotonic() - v["t0"]
            self._verify = None
            msg = f"Line verified · {elapsed:.1f} s"
            self.set_status("ok", msg)
            self.popups.show("Line verified",
                             f"Your message round-tripped through the Mac "
                             f"and Apple in {elapsed:.1f} seconds.", None, "")
            log.info("Line verified in %.1f s", elapsed)

    def _test_popup(self, mode="popup", sound=True, popups=True):
        """Fire a test alert through the same paths as a real message and
        write the outcome to the Activity panel, so Test is a diagnosis."""
        title = "Test message"
        body = "This is how an incoming text appears while you work."
        self._feed_record("test", title, body)
        if sound:
            played = play_notification_sound()
            self.activity.record(
                "alert", "test sound played" if played
                else "test sound FAILED; check Windows sound output")
        if not popups:
            self.activity.record("alert", "test: popups switched off")
            return
        result = self._present_notification(
            mode, title, body, None, "", [])
        if result is not PresentationResult.SHOWN:
            self.activity.record(
                "warn", f"test alert result: {result.value}")
        QApplication.alert(self, 3000)

    # ------------------------------------------------ chat list

    def _style_left_actions(self):
        """Keep rail actions legible, accent-aware, and scale-responsive."""
        h = theme.dim(36)
        radius = h // 2
        icon_px = theme.dim(18)
        self.left_layout.setContentsMargins(
            theme.dim(10), theme.dim(10), theme.dim(10), theme.dim(10))
        self.left_layout.setSpacing(theme.dim(8))
        self.search_row.setSpacing(theme.dim(7))
        self.foot_actions.setSpacing(theme.dim(7))
        self.search.setFixedHeight(h)

        self.compose_btn.setIcon(pencil("#ffffff"))
        self.compose_btn.setIconSize(QSize(icon_px, icon_px))
        self.compose_btn.setFixedHeight(h)
        self.compose_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT}; color: white; "
            f"border: none; border-radius: {radius}px; font-weight: 650; "
            f"font-size: {theme.fs(9.2)}; padding: 0 {theme.dim(13)}px; }} "
            f"QPushButton:hover {{ background: {theme.ACCENT_DOWN}; }} "
            f"QPushButton:pressed {{ background: {theme.ACCENT_DOWN}; }} "
            "QPushButton:focus { border: 2px solid white; }")

        self.hidden_btn.setIcon(eye_off(theme.ACCENT))
        self.hidden_btn.setIconSize(QSize(icon_px, icon_px))
        self.hidden_btn.setFixedHeight(h)
        self.hidden_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.HOVER_BG}; "
            f"color: {theme.ACCENT}; border: 1px solid {theme.ACCENT_BORDER}; "
            f"border-radius: {radius}px; font-weight: 600; "
            f"font-size: {theme.fs(9.0)}; padding: 0 {theme.dim(10)}px; }} "
            f"QPushButton:hover {{ background: {theme.SEL_BG}; "
            f"border-color: {theme.ACCENT}; }} "
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}")

        labels = {
            "idle": "Recover",
            "working": "Recovering…",
            "success": "Recovered",
        }
        self.recover_btn.setText(labels.get(self._recovery_state, "Recover"))
        recovery_icon = (
            "#ffffff" if self._recovery_state == "working" else theme.ACCENT)
        self.recover_btn.setIcon(refresh(recovery_icon))
        self.recover_btn.setIconSize(QSize(icon_px, icon_px))
        self.recover_btn.setFixedHeight(h)
        self.recover_btn.setEnabled(self._recovery_state != "working")
        self.recover_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.HOVER_BG}; "
            f"color: {theme.ACCENT}; border: 2px solid {theme.ACCENT}; "
            f"border-radius: {radius}px; font-weight: 650; "
            f"font-size: {theme.fs(9.0)}; padding: 0 {theme.dim(10)}px; }} "
            f"QPushButton:hover {{ background: {theme.SEL_BG}; "
            "border-color: white; } "
            f"QPushButton:disabled {{ background: {theme.ACCENT_DOWN}; "
            "color: rgba(255,255,255,210); border: none; } "
            "QPushButton:focus { border: 2px solid white; }")

        wake_labels = {
            "idle": "Wake Mac",
            "working": "Waking the Mac…",
            "watching": "Watching for texts…",
            "success": "Mac woken",
        }
        wake_text = wake_labels.get(self._wake_state, "Wake Mac")
        if (self._wake_origin == "auto"
                and self._wake_state in ("working", "watching")):
            wake_text = "Auto wake running…"
        self.wake_btn.setText(wake_text)
        wake_busy = self._wake_state in ("working", "watching")
        # A white bolt stays legible on the soft accent chip, the solid
        # hover fill, and the busy fill alike; one icon color, no flicker.
        self.wake_btn.setIcon(bolt("#ffffff"))
        self.wake_btn.setIconSize(QSize(icon_px, icon_px))
        self.wake_btn.setFixedHeight(h)
        self.wake_btn.setEnabled(not wake_busy)
        self.wake_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.SEL_BG}; "
            f"color: {theme.ACCENT}; border: 1px solid {theme.ACCENT_BORDER}; "
            f"border-radius: {radius}px; font-weight: 650; "
            f"font-size: {theme.fs(9.0)}; padding: 0 {theme.dim(10)}px; }} "
            f"QPushButton:hover {{ background: {theme.ACCENT}; color: white; "
            f"border-color: {theme.ACCENT}; }} "
            f"QPushButton:pressed {{ background: {theme.ACCENT_DOWN}; "
            "color: white; } "
            f"QPushButton:disabled {{ background: {theme.ACCENT_DOWN}; "
            "color: rgba(255,255,255,215); border: none; } "
            "QPushButton:focus { border: 2px solid white; }")

        # The settings gear and the quit control are drawn icons like every
        # other control, so they follow the accent color and the text-size
        # setting instead of depending on system font glyphs.
        gear_size = theme.dim(34)
        round_icon = (
            f"QPushButton {{ background: transparent; border: none; "
            f"border-radius: {gear_size // 2}px; padding: 0; }} "
            f"QPushButton:hover {{ background: {theme.HOVER_BG}; }} "
            f"QPushButton:pressed {{ background: {theme.SEL_BG}; }} "
            f"QPushButton:focus {{ border: 1px solid {theme.ACCENT}; }}")
        self.bell_btn.setFixedSize(gear_size, gear_size)
        self.bell_btn.setIconSize(QSize(theme.dim(20), theme.dim(20)))
        self.bell_btn.setStyleSheet(round_icon)
        self._update_bell()
        self.settings_btn.setFixedSize(gear_size, gear_size)
        self.settings_btn.setIcon(gear(theme.ACCENT))
        self.settings_btn.setIconSize(
            QSize(theme.dim(19), theme.dim(19)))
        self.settings_btn.setStyleSheet(round_icon)
        self.quit_btn.setFixedSize(gear_size, gear_size)
        self.quit_btn.setIcon(power(theme.ACCENT))
        self.quit_btn.setIconSize(QSize(theme.dim(19), theme.dim(19)))
        self.quit_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"border-radius: {gear_size // 2}px; padding: 0; }} "
            f"QPushButton:hover {{ background: "
            f"{theme._blend(theme.FAIL, theme.PANEL, 0.22)}; }} "
            f"QPushButton:pressed {{ background: "
            f"{theme._blend(theme.FAIL, theme.PANEL, 0.34)}; }} "
            f"QPushButton:focus {{ border: 1px solid {theme.FAIL}; }}")

    def _schedule_reload(self):
        self._reload_timer.start()

    def _set_list_rows(self, rows, empty_text: str):
        self.model.set_rows(rows)
        empty = not rows
        self.empty_list_label.setText(empty_text)
        self.empty_list_label.setVisible(empty)
        self.list.setVisible(not empty)

    def _style_unread_pill(self):
        h = theme.dim(24)
        self.unread_pill.setFixedHeight(h)
        self.unread_pill.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT}; color: white; "
            f"border: none; border-radius: {h // 2}px; font-weight: 600; "
            f"font-size: {theme.fs(8.8)}; padding: 0 {theme.dim(11)}px; }} "
            f"QPushButton:hover {{ background: {theme.ACCENT_DOWN}; }} "
            "QPushButton:focus { border: 2px solid white; }")

    def _open_next_unread(self):
        g = self.repo.first_unread_chat(exclude_guid=self.open_chat_guid)
        if g:
            self.open_conversation(g)

    @Slot()
    def reload_chats(self):
        n = self.repo.unread_total()
        base = f"{constants.APP_NAME}  ·  v{constants.VERSION}"
        self.setWindowTitle(f"({n}) {base}" if n else base)
        self.unread_pill.setText(f"{n} new" if n < 100 else "99+ new")
        self.unread_pill.setVisible(n > 0)
        if self.search.text().strip():
            self._rows_signature = None
            return
        rows = []
        for c in self.repo.chats():
            title = self.repo.chat_title(c, self.handles)
            rows.append(Row(
                chat_guid=c["guid"], title=title,
                snippet=snippet(c["last_text"], c["last_attach"],
                                bool(c["last_from_me"])),
                when=c["last_activity"], unread=c["unread"],
                is_group=bool(c["is_group"])))
        # A model reset repaints the whole rail and drops hover state; skip
        # it entirely when nothing about the list actually changed. Sweeps
        # and status traffic land every few seconds, so this dedupe removes
        # near-constant background churn.
        signature = tuple(
            (r.chat_guid, r.title, r.snippet, r.when, r.unread, r.is_group)
            for r in rows)
        if signature == getattr(self, "_rows_signature", None):
            self._scroll_list_to_target(rows)
            return
        self._rows_signature = signature
        self._set_list_rows(rows, "No conversations yet\n\n"
                            "Messages will appear here after the Mac syncs.")
        if self.open_chat_guid:
            for index, row in enumerate(rows):
                if row.chat_guid == self.open_chat_guid:
                    self.list.setCurrentIndex(self.model.index(index, 0))
                    break
        self._scroll_list_to_target(rows)

    def _scroll_list_to_target(self, rows):
        """Bring the conversation that just received a text into view.

        Scrolls only; selection and the open conversation are never
        hijacked, so reading is undisturbed while the moving conversation
        becomes visible with its sound, popup, and unread badge."""
        target = self._scroll_target_guid
        if not target:
            return
        self._scroll_target_guid = None
        for index, row in enumerate(rows):
            if row.chat_guid == target:
                self.list.scrollTo(self.model.index(index, 0),
                                   QListView.ScrollHint.EnsureVisible)
                break

    @Slot()
    def _on_contacts_or_chats(self):
        self.handles = self.repo.handles_map()
        self.thread.set_handles(self.handles)
        self.reload_chats()

    def _hide_chat(self, chat_guid: str):
        c = self.repo.db.one("SELECT * FROM chats WHERE guid=?", (chat_guid,))
        title = self.repo.chat_title(c, self.handles) if c else "Conversation"
        self.repo.hide_chat(chat_guid)
        self._undo_stack.append(chat_guid)
        self._undo_stack = self._undo_stack[-5:]
        self.undo_label.setText(f"Hidden: {title}")
        self.undo_bar.show()
        self._undo_timer.start()
        if chat_guid == self.open_chat_guid:
            self.open_chat_guid = None
            self.thread.show_empty()
        self.reload_chats()
        log.info("Conversation hidden (messages untouched)")

    def _undo_hide(self):
        if not self._undo_stack:
            self.undo_bar.hide()
            return
        guid = self._undo_stack.pop()
        self.repo.unhide_chat(guid)
        self.undo_bar.hide()
        self.reload_chats()
        self.open_conversation(guid)

    def _restore_hidden_dialog(self):
        from PySide6.QtWidgets import (QDialog, QListWidget, QListWidgetItem,
                                       QVBoxLayout)
        from ..util.timefmt import fmt_list_time
        rows = self.repo.hidden_chats()
        dlg = QDialog(self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setWindowTitle(f"Hidden conversations ({len(rows)})")
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lst = QListWidget()
        for c in rows:
            when = fmt_list_time(c["last_activity"]) if c["last_activity"] \
                else ""
            item = QListWidgetItem(
                f"{self.repo.chat_title(c, self.handles)}"
                f"     ·     {when}")
            item.setData(Qt.UserRole, c["guid"])
            lst.addItem(item)
        if rows:
            lst.setCurrentRow(0)
            lay.addWidget(lst)
        else:
            empty = QLabel("No hidden conversations.")
            empty.setStyleSheet(f"color: {theme.MUTED};")
            lay.addWidget(empty)

        def restore_guid(guid):
            self.repo.unhide_chat(guid)
            self.search.clear()
            self.reload_chats()
            dlg.accept()
            self.open_conversation(guid)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if rows:
            all_btn = QPushButton("Restore all")
            def restore_all():
                for c in rows:
                    self.repo.unhide_chat(c["guid"])
                self.search.clear()
                self.reload_chats()
                dlg.accept()
            all_btn.clicked.connect(restore_all)
            buttons.addWidget(all_btn)
            rest_btn = QPushButton("Restore")
            rest_btn.setObjectName("accent")
            def restore_selected():
                it = lst.currentItem()
                if it:
                    restore_guid(it.data(Qt.UserRole))
            rest_btn.clicked.connect(restore_selected)
            lst.itemDoubleClicked.connect(
                lambda it: restore_guid(it.data(Qt.UserRole)))
            buttons.addWidget(rest_btn)
        close = QPushButton("Close")
        close.clicked.connect(dlg.reject)
        buttons.addWidget(close)
        lay.addLayout(buttons)
        dlg.exec()

    @Slot(str)
    def _on_push_broken(self, reason: str):
        self.activity.record("error", f"live push cannot connect: {reason}")
        self.popups.show(
            "Live push cannot connect",
            f"{reason}. Messages still arrive via 3-second checks. "
            "Please send this reason to your assistant.", None, "")

    def _cycle(self, step: int):
        count = self.model.rowCount()
        if not count:
            return
        row = self.list.currentIndex().row()
        row = (row + step) % count if row >= 0 else 0
        index = self.model.index(row, 0)
        self.list.setCurrentIndex(index)
        self._on_row_clicked(index)

    def _list_menu(self, pos):
        from PySide6.QtWidgets import QApplication, QMenu
        index = self.list.indexAt(pos)
        row: Row = index.data(Qt.UserRole) if index.isValid() else None
        menu = QMenu(self)
        act_read = act_refresh = act_copy = None
        if row is not None:
            act_read = menu.addAction("Mark as read")
            act_refresh = menu.addAction("Refresh conversation")
            act_copy = menu.addAction("Copy address")
            menu.addSeparator()
        act_all = menu.addAction("Mark all as read")
        menu.addSeparator()
        act_hidden = menu.addAction("Hidden conversations…")
        chosen = menu.exec(self.list.mapToGlobal(pos))
        if chosen is None:
            return
        if act_hidden is not None and chosen == act_hidden:
            self._restore_hidden_dialog()
            return
        if chosen == act_all:
            self.repo.mark_all_read()
            self._schedule_reload()
            return
        if row is None:
            return
        if chosen == act_read:
            self.repo.mark_read(row.chat_guid)
            self._schedule_reload()
        elif chosen == act_refresh:
            self._on_refresh_chat(row.chat_guid)
        elif chosen == act_copy:
            parts = self.repo.participants_of(row.chat_guid)
            QApplication.clipboard().setText(", ".join(parts))

    def _on_row_clicked(self, index):
        row: Row = index.data(Qt.UserRole)
        if row is None:
            return
        self.open_conversation(row.chat_guid, row.focus_guid)

    def open_conversation(self, chat_guid: str, focus_guid=None):
        c = self.repo.db.one("SELECT * FROM chats WHERE guid=?", (chat_guid,))
        if c is None:
            return
        self.open_chat_guid = chat_guid
        self.repo.mark_read(chat_guid)
        self.thread.load_chat(c, focus_guid)
        self._schedule_reload()
        self._drain_delivery_events()

    # ------------------------------------------------ search

    def _on_search(self, text: str):
        q = text.strip()
        if not q:
            self.reload_chats()
            return
        expr = fts_escape(q)
        if not expr:
            return
        rows = []
        for r in self.repo.search(expr):
            title = self.repo.chat_title(r, self.handles)
            when = r["date_created"]
            prefix = "You: " if r["is_from_me"] else ""
            rows.append(Row(chat_guid=r["chat_guid"], title=title,
                            snippet=prefix + (r["snip"] or ""), when=when,
                            focus_guid=r["guid"]))
        self._set_list_rows(rows, "No messages found")

    # ------------------------------------------------ incoming traffic

    def _conversation_is_visible(self, chat_guid: str) -> bool:
        app_active = (
            QApplication.applicationState()
            == Qt.ApplicationState.ApplicationActive)
        return bool(
            chat_guid == self.open_chat_guid
            and self.isVisible()
            and not self.isMinimized()
            and app_active
            and self.isActiveWindow())

    @Slot(list, str)
    def _on_agent_batch(self, items: list, _source: str):
        """React to a committed batch the agent already stored: refresh the
        open thread, feed the verify-line stopwatch, and replay the durable
        delivery ledger for unread counts and popups."""
        open_changed = False
        for m in items or []:
            if not isinstance(m, dict):
                continue
            ts = m.get("date_created") or 0
            self._server_newest_ts = max(self._server_newest_ts, ts)
            is_new = bool(m.get("is_new"))
            changed = bool(m.get("changed"))
            self._check_verify_echo(m, is_new)
            if (m.get("chat_guid") == self.open_chat_guid
                    and (is_new or changed)):
                open_changed = True
        if open_changed:
            # Always rebuild from the database.  This makes late/out-of-order
            # messages and complete payload repairs visible immediately while
            # preserving the reader's scroll position.
            self.thread.refresh_from_repo(preserve_scroll=True)
        self._schedule_reload()
        self._drain_delivery_events()

    def _drain_delivery_events(self):
        """Apply unread and notification side effects from durable state.

        The whole body is guarded: any unexpected error is logged (and so
        mirrored into the Activity panel) and the sweep retries, because a
        silent exception here is a silent loss of alerts."""
        if self._draining_events:
            return
        self._draining_events = True
        try:
            self._drain_delivery_events_inner()
        except Exception:
            log.exception(
                "Alert sweep failed; alerts retry on the next sweep")
        finally:
            self._draining_events = False

    def _drain_delivery_events_inner(self):
        now_ms = int(time.time() * 1000)
        mode = getattr(self.settings, "notify_mode", "popup")
        popups_on = bool(getattr(self.settings, "popups_enabled", True))
        sound_on = bool(getattr(self.settings, "notification_sound", True))
        events = self.repo.pending_delivery_events()
        if events:
            for event in events:
                event_guid = event["message_guid"]
                chat_guid = event["chat_guid"]
                self._resurrect_if_hidden(chat_guid, "new activity")
                chat_open = self._conversation_is_visible(chat_guid)
                if not event["unread_done"]:
                    # Unread still respects what is visibly on screen: a
                    # conversation being read does not gain a stale badge.
                    self.repo.apply_unread_event(
                        event_guid, chat_is_open=chat_open)
                if event["notification_done"]:
                    continue
                # Eligibility is based on when this cache first saw the
                # message, not Apple's possibly backdated dateCreated.
                # Initial archive indexing never creates delivery events, so
                # a genuinely late iCloud row can still alert the user.
                age = now_ms - (event["first_seen_ms"] or now_ms)
                # Every alert-worthy arrival also lands in the notification
                # center, including ones too old to pop (collected while no
                # window ran), stamped with when they really arrived. The
                # feed table dedupes durably on GUID; this set only skips
                # repeat work on the 2.5 s sweeps.
                if event_guid not in self._feed_logged_guids:
                    if len(self._feed_logged_guids) > 8000:
                        self._feed_logged_guids.clear()
                    self._feed_logged_guids.add(event_guid)
                    _c, feed_title, feed_body = self._alert_title_body(
                        event, chat_guid)
                    self._feed_record(
                        "message", feed_title, feed_body, chat_guid,
                        event_guid,
                        created_ms=event["first_seen_ms"] or now_ms)
                if age <= constants.NOTIFY_MAX_AGE_MS:
                    # Steer the conversation list to the newest arrival so
                    # the moving conversation is on screen, whatever was
                    # scrolled before.
                    self._scroll_target_guid = chat_guid
                # The sound and popup fire whether or not the window or even
                # this exact conversation is on screen. An open window is no
                # proof of an attentive reader: the user works beside the
                # app and must hear and see every arrival. Only the master
                # switches, or a message too old to be news, stay quiet.
                if age > constants.NOTIFY_MAX_AGE_MS:
                    self.repo.finish_notification_event(event_guid)
                    continue
                if not popups_on:
                    # Sound-only operation: the arrival is still announced,
                    # the ledger completes, and no card is created.
                    if sound_on:
                        self._signal_notification()
                    self.repo.finish_notification_event(event_guid)
                    continue
                if (event_guid in self._queued_event_guids
                        or event_guid in self._popup_pending_guids):
                    continue
                chat, title, body = self._alert_title_body(event, chat_guid)
                code = extract_code(event["text"], event["sender_address"])
                # QSystemTrayIcon system toasts cannot expose dependable
                # application-defined Copy/Fill actions. Route verification
                # codes to our non-activating rich popup unless explicitly
                # disabled; ordinary messages still honor the selected mode.
                event_mode = mode
                if (code and mode == "system"
                        and getattr(self.settings, "interactive_codes", True)):
                    event_mode = "popup"
                self._queue_toast(
                    event_guid, event_mode, title, body, code, chat_guid)
            self._schedule_reload()
            # A full page means more durable work may remain. Do not spin on
            # these same rows while their popups are queued; _flush_toasts()
            # schedules the continuation after it acknowledges this page.
            if len(events) >= 200:
                if any(
                        e["message_guid"] in self._queued_event_guids
                        or e["message_guid"] in self._popup_pending_guids
                        for e in events):
                    self._delivery_more_pending = True
                else:
                    QTimer.singleShot(0, self._drain_delivery_events)

    def _queue_toast(self, event_guid, mode, title, body, code, chat_guid):
        """Collapse a catch-up burst into one summary popup instead of a
        barrage; up to three arrive individually."""
        self._queued_event_guids.add(event_guid)
        self._toast_queue.append(
            (event_guid, mode, title, body, code, chat_guid))
        # Use a bounded collection window. Restarting this timer for every
        # arrival can postpone alerts forever in a busy group chat.
        if not self._toast_timer.isActive():
            self._toast_timer.start()

    def _flush_toasts(self):
        queue, self._toast_queue = self._toast_queue, []
        if not queue:
            return
        # The sound is the one signal that must never depend on any window
        # machinery succeeding. Play it for the burst up front; the 1.5 s
        # throttle absorbs the presentation path's own signal later.
        self._signal_notification()
        retry_needed = False
        try:
            code_items = [item for item in queue
                          if item[4] and item[1] == "popup"]
            ordinary = [item for item in queue
                        if not (item[4] and item[1] == "popup")]
            # Every code remains actionable. The popup manager displays up to
            # three and queues the rest; the durable ledger is acknowledged
            # only as each card is actually shown.
            for event_guid, _mode, title, body, code, guid in reversed(
                    code_items):
                result = self._present_notification(
                    "popup", title, body, code, guid, [event_guid])
                retry_needed |= result is PresentationResult.UNAVAILABLE

            if len(ordinary) <= 3:
                for event_guid, mode, title, body, code, guid in ordinary:
                    result = self._present_notification(
                        mode, title, body, code, guid, [event_guid])
                    retry_needed |= result is PresentationResult.UNAVAILABLE
                return
            mode = ordinary[-1][1]
            names = []
            for _event, _mode, title, _body, _code, _guid in ordinary:
                if title not in names:
                    names.append(title)
            summary = f"{len(ordinary)} new messages"
            body = "From " + ", ".join(names[:3]) + (
                "…" if len(names) > 3 else "")
            last_guid = ordinary[-1][5]
            result = self._present_notification(
                mode, summary, body, None, last_guid,
                [item[0] for item in ordinary])
            retry_needed |= result is PresentationResult.UNAVAILABLE
        except Exception:
            # Leave the ledger pending.  The next drain/restart retries rather
            # than silently losing the notification.
            log.exception("Could not present notification; leaving it pending")
            retry_needed = True
        finally:
            for event_guid, *_rest in queue:
                self._queued_event_guids.discard(event_guid)
            if retry_needed:
                QTimer.singleShot(5000, self._drain_delivery_events)
            if self._delivery_more_pending:
                self._delivery_more_pending = False
                QTimer.singleShot(0, self._drain_delivery_events)

    def _tray_toast_available(self) -> bool:
        return (QSystemTrayIcon.isSystemTrayAvailable()
                and QSystemTrayIcon.supportsMessages()
                and self.tray.isVisible())

    def _present_notification(self, mode: str, title: str, body: str,
                              code, chat_guid: str,
                              event_guids: list[str]) -> PresentationResult:
        """Present through the requested channel, falling back both ways.

        Style is a preference, delivery is a requirement: if the preferred
        channel cannot show this alert, the other channel takes it, and
        every outcome is recorded in the Activity panel so a missing alert
        is never a mystery again."""
        event_guids = list(dict.fromkeys(g for g in event_guids if g))
        if mode == "system":
            if self._tray_toast_available():
                self._last_notification_chat_guid = chat_guid or ""
                self.tray.showMessage(title, body[:180], _app_icon(), 6000)
                for event_guid in event_guids:
                    self.repo.finish_notification_event(event_guid)
                self._signal_notification()
                self.activity.record("alert", f"Windows toast · {title}")
                return PresentationResult.SHOWN
            self.activity.record(
                "warn", "Windows toast unavailable; using the popup card")

        # The app's own always-on card.
        key = ""
        if len(event_guids) == 1:
            key = event_guids[0]
        elif event_guids:
            key = (f"group:{event_guids[0]}:{event_guids[-1]}:"
                   f"{len(event_guids)}")
        if key:
            self._popup_ack_groups[key] = event_guids
            self._popup_pending_guids.update(event_guids)
        result = self.popups.show(
            title, body, code, chat_guid, event_key=key)
        if result is PresentationResult.SHOWN:
            self.activity.record("alert", f"popup card · {title}")
        elif result is PresentationResult.QUEUED:
            self.activity.record(
                "alert", f"popup queued (cards busy) · {title}")
        if result is PresentationResult.UNAVAILABLE:
            if key:
                self._popup_ack_groups.pop(key, None)
                self._popup_pending_guids.difference_update(event_guids)
            # The card channel refused; cross to the toast channel rather
            # than showing nothing at all.
            if mode != "system" and self._tray_toast_available():
                self._last_notification_chat_guid = chat_guid or ""
                self.tray.showMessage(title, body[:180], _app_icon(), 6000)
                for event_guid in event_guids:
                    self.repo.finish_notification_event(event_guid)
                self._signal_notification()
                self.activity.record(
                    "warn", f"popup refused; Windows toast used · {title}")
                return PresentationResult.SHOWN
            self.activity.record(
                "error", f"alert could not be shown, retrying · {title}")
        if not event_guids and result is PresentationResult.SHOWN:
            self._signal_notification()
        return result

    def _on_popup_presented(self, event_key: str):
        event_guids = self._popup_ack_groups.get(
            event_key, [event_key] if event_key else [])
        failed = []
        for event_guid in event_guids:
            try:
                self.repo.finish_notification_event(event_guid)
                self._popup_pending_guids.discard(event_guid)
            except Exception:
                # The card was shown, but a transient SQLite failure must not
                # strand this GUID in the in-memory pending set forever. Keep
                # the mapping and retry the durable acknowledgement.
                failed.append(event_guid)
                log.exception(
                    "Could not acknowledge shown notification %s", event_guid)
        if event_key and event_key not in self._popup_signalled_keys:
            self._popup_signalled_keys.add(event_key)
            self._signal_notification()
        elif not event_key:
            self._signal_notification()
        if failed:
            self._popup_ack_groups[event_key] = failed
            QTimer.singleShot(
                5000, lambda key=event_key: self._on_popup_presented(key))
            return
        self._popup_ack_groups.pop(event_key, None)
        self._popup_signalled_keys.discard(event_key)
        self._schedule_reload()
        if self._delivery_more_pending:
            self._delivery_more_pending = False
            QTimer.singleShot(0, self._drain_delivery_events)

    def _on_popup_rejected(self, event_key: str):
        """A card accepted into the in-memory queue later failed to show.

        Release its in-memory reservation but leave the durable ledger
        untouched; the periodic sweep can then retry it normally.
        """
        event_guids = self._popup_ack_groups.pop(event_key, [])
        self._popup_pending_guids.difference_update(event_guids)
        self._popup_signalled_keys.discard(event_key)
        QTimer.singleShot(5000, self._drain_delivery_events)

    def _signal_notification(self):
        """One explicit sound/taskbar signal per burst, never per message.

        The sound always plays for an accepted alert; an open window is
        not an attentive reader. The taskbar flash is only added when the
        window is not the active one, where it would be meaningless."""
        now = time.monotonic()
        if now - self._last_alert_signal < 1.5:
            return
        self._last_alert_signal = now
        if getattr(self.settings, "notification_sound", True):
            play_notification_sound()
        if not self.isActiveWindow():
            QApplication.alert(self, 3000)

    def _open_last_notification(self):
        if self._last_notification_chat_guid:
            self._popup_open(self._last_notification_chat_guid)

    def _popup_open(self, chat_guid: str):
        if not chat_guid:
            return
        self.showNormal()
        self.activateWindow()
        self.open_conversation(chat_guid)

    @Slot(str)
    def _on_backfill_page(self, chat_guid: str):
        self._schedule_reload()
        if chat_guid == self.open_chat_guid and not self.thread.by_guid:
            c = self.repo.db.one("SELECT * FROM chats WHERE guid=?", (chat_guid,))
            if c is not None:
                self.thread.load_chat(c)

    def _refresh_current(self):
        if self.open_chat_guid:
            self._on_refresh_chat(self.open_chat_guid)
        else:
            self.agent.send({"cmd": "poke", "chats": True, "head": True})

    def _on_refresh_chat(self, chat_guid: str):
        if not self.agent.send({"cmd": "refresh_chat",
                                "chat_guid": chat_guid}):
            self.set_status(
                "warn", "Agent starting… refresh will run in a moment.")

    @Slot(str, object, bool)
    def _on_chat_refreshed(self, chat_guid: str, newest_ts=None,
                           wake_watching: bool = False):
        if not wake_watching:
            if newest_ts:
                self.set_status(
                    "ok", "Up to date with the Mac · newest there: "
                    f"{fmt_clock(newest_ts)}")
            else:
                self.set_status(
                    "ok", "Up to date with the Mac · nothing found there")
        if chat_guid == self.open_chat_guid:
            self.thread.refresh_from_repo(preserve_scroll=True)
        self._schedule_reload()

    def _repair_stranded_hidden(self):
        """One-time on upgrade: older versions could leave an actively
        used conversation stuck hidden. Hiding is for dormant threads,
        so anything hidden with activity in the last week is restored,
        and the log records exactly what."""
        if getattr(self.settings, "hidden_migration_done", False):
            return
        cutoff = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
        restored = 0
        for c in self.repo.hidden_chats():
            if (c["last_activity"] or 0) >= cutoff:
                self.repo.unhide_chat(c["guid"])
                restored += 1
        if restored:
            log.warning("Upgrade repair restored %d recently active "
                        "hidden conversation(s)", restored)
        self.settings.hidden_migration_done = True
        try:
            config.save(self.settings)
        except Exception:
            log.exception("Could not persist migration flag")

    def _resurrect_if_hidden(self, chat_guid: str, reason: str):
        if chat_guid and self.repo.is_hidden(chat_guid):
            self.repo.unhide_chat(chat_guid)
            log.info("Hidden conversation restored (%s)", reason)
            self._schedule_reload()

    def _flash_status(self, text: str, ms: int = 2200):
        prev_text = self.status.text()
        prev_state = self.badge.state
        prev_details = self._connection_details
        self.set_status("ok", text)
        def back():
            self.badge.set_state(prev_state)
            self.status.setText(prev_text)
            self._connection_details = prev_details
            self.status.setToolTip(prev_details)
            self.status.setAccessibleDescription(prev_details)
        QTimer.singleShot(ms, back)

    # ------------------------------------------------ sending

    def _on_send(self, chat_guid: str, text: str, files: list):
        """Enqueue durably, then ask the agent to send. If the agent is
        momentarily down, the rows wait in the outbox and are submitted
        automatically the instant the channel reconnects; nothing is lost
        and nothing can double-send."""
        self._resurrect_if_hidden(chat_guid, "you sent a message")
        ids = []
        for f in files:
            ids.append(self.repo.enqueue(chat_guid, None, f))
        if text:
            ids.append(self.repo.enqueue(chat_guid, text, None))
        delivered = True
        for i in ids:
            delivered = self.agent.send(
                {"cmd": "submit_outbox", "id": i}) and delivered
        self.thread.refresh_outbox()
        if not delivered:
            self.set_status(
                "warn",
                "Agent starting… your message is queued and will send "
                "automatically.")

    def _on_need_download(self, guid: str, file_name: str):
        if not self.agent.send({"cmd": "download", "guid": guid,
                                "file_name": file_name}):
            self.set_status(
                "warn", "Agent starting… the download will begin shortly.")

    def _on_retry(self, outbox_id: int):
        row = self.repo.outbox_row(outbox_id)
        if row is None:
            return
        self.repo.outbox_set(outbox_id, "queued")
        self.agent.send({"cmd": "submit_outbox", "id": outbox_id})
        self.thread.refresh_outbox()

    @Slot(str)
    def _on_outbox_changed(self, chat_guid: str):
        if chat_guid == self.open_chat_guid:
            self.thread.refresh_outbox()

    @Slot(object)
    def _on_message_sent(self, m):
        self._flash_status("Sent ✓")
        if isinstance(m, dict) and m.get("chat_guid") == self.open_chat_guid:
            self.thread.refresh_from_repo(preserve_scroll=True)
        self._schedule_reload()

    @Slot(str, str)
    def _on_attachment_ready(self, guid: str, path: str):
        self.thread.on_attachment_ready(guid, path)

    @Slot(str, str)
    def _on_download_failed(self, _guid: str, error: str):
        self.set_status("warn", error)

    # ------------------------------------------------ helpers

    def _show_shortcuts(self):
        """A quiet reference card for every key the app answers to.
        Ctrl+/ and F1 open it; Esc closes it."""
        from PySide6.QtWidgets import QDialog, QGridLayout
        rows = [
            ("Ctrl+F  /  Ctrl+K", "Search messages"),
            ("Ctrl+N", "New message"),
            ("Enter  /  Shift+Enter", "Send  /  new line in the composer"),
            ("Ctrl+Tab  /  Ctrl+Shift+Tab", "Next / previous conversation"),
            ("F5", "Refresh this conversation and recent messages"),
            ("Ctrl+E", "Save the open conversation as a text file"),
            ("Ctrl+H", "Hide the open conversation"),
            ("Ctrl+B", "Notification center (the bell)"),
            ("Ctrl+Shift+M", "Wake Mac: release held-back texts"),
            ("Ctrl+Shift+R", "Recover: non-destructive global resync"),
            ("Ctrl+L", "Connection details"),
            ("Ctrl+,", "Settings"),
            ("Esc", "Clear the search box"),
            ("Ctrl+/  or  F1", "This list"),
        ]
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard shortcuts")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(theme.dim(22), theme.dim(18),
                               theme.dim(22), theme.dim(18))
        lay.setSpacing(theme.dim(4))
        heading = QLabel("Keyboard shortcuts")
        heading.setStyleSheet(
            f"font-size: {theme.fs(12)}; font-weight: 600;")
        lay.addWidget(heading)
        tick = QWidget()
        tick.setFixedSize(theme.dim(46), max(2, theme.dim(3)))
        tick.setStyleSheet(
            f"background: {theme.ACCENT}; border-radius: 1px;")
        lay.addWidget(tick)
        lay.addSpacing(theme.dim(8))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.dim(16))
        grid.setVerticalSpacing(theme.dim(7))
        for i, (keys, what) in enumerate(rows):
            chip = QLabel(keys)
            chip.setStyleSheet(
                f"background: {theme.PANEL}; "
                f"border: 1px solid {theme.BORDER}; border-radius: 6px; "
                f"padding: 2px 10px; font-size: {theme.fs(8.8)}; "
                "font-weight: 600;")
            desc = QLabel(what)
            desc.setStyleSheet(
                f"font-size: {theme.fs(9.2)}; color: {theme.MUTED};")
            grid.addWidget(chip, i, 0)
            grid.addWidget(desc, i, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        dlg.exec()

    def sizeHint(self):
        return QSize(1180, 760)
