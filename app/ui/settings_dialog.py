"""Connection, alerts, and appearance settings. Shown automatically on
first run. Organized into three tabs so the growing option set stays
scannable; every control keeps its old attribute name so tests and
callers are unaffected. The connection test runs on a worker thread so
the dialog stays responsive, and the tint picker is a grid of named
color swatches that preview live."""
import threading

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from .. import config, constants
from ..api.rest import ApiError, BBClient
from . import theme


class _Tester(QObject):
    done = Signal(bool, str)

    def start(self, url, password):
        def work():
            client = BBClient(url, password)
            try:
                client.ping()
                info = client.server_info()
                ver = info.get("server_version", "") if isinstance(info, dict) else ""
                self.done.emit(True, f"Connected{f' (server {ver})' if ver else ''}")
            except ApiError as e:
                self.done.emit(False, str(e))
            except Exception:
                self.done.emit(False, "Unexpected error; see log")
            finally:
                client.close()
        threading.Thread(target=work, daemon=True).start()


class _PhoneScanner(QObject):
    """Runs the blocking Bluetooth discovery on a worker thread."""

    done = Signal(list, list, str)   # rows, notes, error text

    def start(self):
        def work():
            try:
                from ..phone.link import discover_phones
                rows, notes = discover_phones()
                self.done.emit(rows, notes, "")
            except Exception as e:
                self.done.emit([], [], str(e) or e.__class__.__name__)
        threading.Thread(target=work, daemon=True).start()


class _PhoneWizard(QObject):
    """Runs the guided connect ceremony on a worker thread: find the
    phone by proof, trigger the pairing while the user watches the
    phone, subscribe, and prove the link with a live round trip. The
    progress signal streams the coaching text into the dialog."""

    progress = Signal(str)
    done = Signal(object, list)      # result dict or None, notes

    def start(self):
        def work():
            try:
                from ..phone.link import setup_iphone
                result, notes = setup_iphone(progress=self.progress.emit)
                self.done.emit(result, notes)
            except Exception as e:
                self.done.emit(
                    None, [f"setup failed: {str(e) or type(e).__name__}"])
        threading.Thread(target=work, daemon=True).start()


class PhonePickerDialog(QDialog):
    """The guided way to link the phone. Connect my iPhone runs the
    whole ceremony (find by proof, pair while the user watches the
    phone, subscribe, live round-trip proof) and saves the winner; the
    list and OK remain for choosing a named device by hand."""

    def __init__(self, parent=None, phone_pause=None):
        super().__init__(parent)
        self._phone_pause = phone_pause
        self.setWindowTitle("Connect your iPhone")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumSize(480, 430)
        self.choice = None       # (name, address) once accepted

        lay = QVBoxLayout(self)
        hint = QLabel(
            "Two steps, once. First, 'Phone Link pairing…' opens "
            "Microsoft's QR flow: scan it with the iPhone camera and "
            "allow Link to Windows — Microsoft's own app on the phone "
            "performs the pairing with the correct iOS prompts every "
            "time. Second, 'Connect my iPhone' finds the phone by "
            "proof, attaches to that pairing, and proves the link "
            "live. This app never creates or removes pairings itself.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        lay.addWidget(hint)
        self.listing = QListWidget()
        self.listing.setObjectName("plainPicker")
        self.listing.itemDoubleClicked.connect(lambda _i: self._accept())
        lay.addWidget(self.listing, 1)
        self.state = QLabel("Scanning…")
        self.state.setWordWrap(True)
        self.state.setStyleSheet(
            f"color: {theme.TEXT}; font-size: {theme.fs(9.4)}; "
            f"padding: {theme.dim(4)}px;")
        self.state.setMinimumHeight(theme.dim(52))
        lay.addWidget(self.state)
        row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect my iPhone")
        self.connect_btn.setObjectName("accent")
        self.connect_btn.setToolTip(
            "Find the phone by proof and attach to the pairing Windows "
            "already holds, then prove the link with a live round "
            "trip. This never creates a pairing; Phone Link's QR flow "
            "does that part best.")
        self.connect_btn.clicked.connect(self._connect_wizard)
        row.addWidget(self.connect_btn)
        self.phone_link_btn = QPushButton("Phone Link pairing…")
        self.phone_link_btn.setToolTip(
            "Open Microsoft Phone Link's pairing (the QR flow). Its QR "
            "starts Microsoft's own app on the iPhone, which pairs "
            "from inside iOS with the correct prompts and pages every "
            "time. Pair there once, confirm a notification shows in "
            "Phone Link, then press Connect my iPhone.")
        self.phone_link_btn.clicked.connect(self._open_phone_link)
        row.addWidget(self.phone_link_btn)
        self.rescan_btn = QPushButton("Scan again")
        self.rescan_btn.clicked.connect(self._scan)
        row.addWidget(self.rescan_btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        lay.addLayout(row)

        self._scanner = _PhoneScanner()
        self._scanner.done.connect(self._on_scanned, Qt.QueuedConnection)
        self._wizard = _PhoneWizard()
        self._wizard.progress.connect(self._on_progress,
                                      Qt.QueuedConnection)
        self._wizard.done.connect(self._on_wizard_done,
                                  Qt.QueuedConnection)
        self._scan()

    def _set_busy(self, busy: bool):
        self.rescan_btn.setEnabled(not busy)
        self.connect_btn.setEnabled(not busy)
        self.phone_link_btn.setEnabled(not busy)

    def _open_phone_link(self):
        try:
            from ..phone.link import open_phone_link
            outcome = open_phone_link()
        except Exception as e:
            outcome = str(e) or e.__class__.__name__
        import logging
        logging.getLogger(__name__).warning(
            "Phone Link pairing launched by the user (%s)", outcome)
        self._on_progress(
            f"{outcome}. Scan the QR with the iPhone camera, allow "
            "Link to Windows, finish the pairing, and check that a "
            "notification appears in Phone Link once. Then come back "
            "and press Connect my iPhone.")

    def _on_progress(self, text: str):
        emphasize = "ACTION NEEDED" in text
        self.state.setStyleSheet(
            f"color: {theme.WARN if emphasize else theme.TEXT}; "
            f"font-size: {theme.fs(10.2 if emphasize else 9.4)}; "
            f"font-weight: {650 if emphasize else 400}; "
            f"padding: {theme.dim(4)}px;")
        self.state.setText(text)

    def _scan(self):
        self._set_busy(True)
        self.listing.clear()
        self._on_progress("Scanning for about 8 seconds…")
        self._scanner.start()

    def _connect_wizard(self):
        if self._phone_pause is not None:
            try:
                self._phone_pause()
            except Exception:
                pass
        self._set_busy(True)
        self.listing.clear()
        self._on_progress(
            "Starting. Keep the iPhone unlocked in your hand and watch "
            "it for a pairing prompt…")
        self._wizard.start()

    @staticmethod
    def _row_label(name, address, source, rssi) -> str:
        from ..phone.link import closeness
        tags = {"verified": "YOUR IPHONE — verified",
                "paired": "paired with this PC",
                "paired-voice": "paired (voice link)",
                "apple": "Apple, name hidden"}
        parts = [name, address]
        tag = tags.get(source, "")
        where = closeness(rssi)
        if tag and where:
            parts.append(f"{tag} · {where}")
        elif tag or where:
            parts.append(tag or where)
        return "   ·   ".join(parts)

    def _fill(self, rows):
        self.listing.clear()
        for name, address, source, rssi in rows:
            item = QListWidgetItem(self._row_label(
                name, address, source, rssi))
            item.setData(Qt.UserRole, (name, address))
            self.listing.addItem(item)
        if rows:
            self.listing.setCurrentRow(0)

    def _show_notes(self, notes):
        if notes:
            summary = " · ".join(str(n) for n in notes[:6])
            self.state.setToolTip(summary)
            import logging
            logging.getLogger(__name__).info("Phone discovery: %s", summary)

    def _on_scanned(self, rows, notes, error):
        self._set_busy(False)
        self._show_notes(notes)
        if error:
            self.state.setText(
                f"Bluetooth unavailable: {error}. Is Bluetooth on? "
                "On this PC, support installs with install.bat.")
            return
        if not rows:
            self.state.setText(
                "Nothing found. Pair the iPhone with Windows first "
                "(Settings → Bluetooth & devices → Add device), keep it "
                "nearby and awake, then scan again.")
            return
        self._fill(rows)
        self.state.setText(
            f"{len(rows)} device(s). Pick your iPhone if it is listed "
            "as paired, or press Connect my iPhone and it will be "
            "identified by proof.")

    def _on_wizard_done(self, result, notes):
        self._set_busy(False)
        self._show_notes(notes)
        if result:
            self.choice = ("Your iPhone", result.get("address", ""))
            if result.get("needs_phone_link"):
                # Found and remembered, but the PC holds no pairing for
                # it. Keep the dialog open and route through the flow
                # that pairs iPhones correctly every time.
                self._on_progress(
                    "ACTION NEEDED · Found your iPhone, but it is not "
                    "paired with this PC yet. Press 'Phone Link "
                    "pairing…', scan the QR with the phone, allow Link "
                    "to Windows, and confirm a notification shows in "
                    "Phone Link once. Then press Connect my iPhone "
                    "again; this app attaches to that pairing and "
                    "takes over the presentation.")
                return
            if result.get("paired_pending"):
                import platform
                pc = platform.node() or "this PC"
                # Keep the dialog open: this coaching is the one thing
                # the user must actually read.
                self._on_progress(
                    "ACTION NEEDED · Paired! One switch left, on the "
                    f"phone itself: Settings → Bluetooth → '{pc}' → "
                    "(i) → turn ON Share System Notifications (pick "
                    "Other if asked what kind of device; if the switch "
                    "is missing, wait a minute and reopen the (i)). "
                    "Then press OK here and Save. If the phone shows "
                    f"'{pc}' with NO (i) at all, the entry is a ghost: "
                    "press OK and Save anyway, then follow the "
                    "clean-slate steps the bell shows (remove on both "
                    "sides, pair via Windows Settings first, then "
                    "Connect again).")
                return
            timing = result.get("ms")
            self._on_progress(
                "Connected, paired, and proven"
                + (f" ({timing} ms round trip)" if timing else "")
                + ". Press Save in Settings and you are done.")
            # The ceremony succeeded end to end; close with the choice
            # made so the only remaining step is Save.
            self.accept()
            return
        detail = f" Details: {notes[-1]}." if notes else ""
        self._on_progress(
            "The setup did not complete." + detail + " Usual fixes: "
            "keep the phone unlocked and next to the PC, approve the "
            "prompt on the phone quickly, and if it keeps failing turn "
            "the iPhone's Bluetooth off and on, then try again. "
            "Diagnostics were written to the Activity panel.")
        import logging
        logging.getLogger(__name__).warning(
            "iPhone setup incomplete: %s", " · ".join(
                str(n) for n in notes))

    def _accept(self):
        item = self.listing.currentItem()
        if item is not None:
            self.choice = item.data(Qt.UserRole)
        if self.choice:
            # Either a list pick or a wizard result (the paired-pending
            # path leaves the dialog open with the choice already made).
            self.accept()
            return
        self._on_progress("Pick a device first, run Connect my iPhone, "
                          "or Cancel.")


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None, on_preview=None,
                 on_test=None, on_verify=None, on_reset_tips=None,
                 on_phone_test=None, phone_status=None,
                 phone_pause=None):
        super().__init__(parent)
        self.settings = settings
        self._on_preview = on_preview
        self._on_test = on_test
        self._on_verify = on_verify
        self._on_reset_tips = on_reset_tips
        self._on_phone_test = on_phone_test
        self._phone_status = phone_status
        self._phone_pause = phone_pause
        self.setWindowTitle("Settings")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(540)

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_connection_tab()
        self._build_alerts_tab()
        self._build_look_tab()

        hint = QLabel("Changes preview live behind this window. "
                      "Save keeps them; Cancel puts everything back.")
        hint.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(8.8)};")
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        version = QLabel(f"JRL Messages v{constants.VERSION}")
        version.setStyleSheet(f"color: {theme.MUTED}; font-size: {theme.fs(8.4)};")
        version.setAlignment(Qt.AlignRight)
        root.addWidget(version)

        self._tester = _Tester()
        self._tester.done.connect(self._on_tested, Qt.QueuedConnection)

    # ------------------------------------------------------- Connection

    def _build_connection_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        intro = QLabel(
            "Server: the BlueBubbles address from your Mac. With Tailscale this "
            "is the Mac's tailnet name plus the port, and the same address works "
            "at home, in court, or abroad.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.MUTED};")
        lay.addWidget(intro)

        form = QFormLayout()
        self.url = QLineEdit(self.settings.server_url)
        self.url.setPlaceholderText("http://your-mac.tailnet-name.ts.net:1234")
        self.pw = QLineEdit(config.get_password(self.settings))
        self.pw.setEchoMode(QLineEdit.Password)
        self.pw.setPlaceholderText("BlueBubbles server password")
        form.addRow("Server URL", self.url)
        form.addRow("Password", self.pw)

        # Automatic Wake Mac. The background agent restarts Messages on the
        # Mac after this much incoming silence, so texts Apple was holding
        # back arrive without the button. It never runs while a send is
        # queued or on the wire.
        self.auto_wake_combo = QComboBox()
        self._auto_wake_map = {"Off": 0}
        for minutes in constants.AUTO_WAKE_CHOICES:
            if minutes:
                self._auto_wake_map[f"After {minutes} quiet minutes"] = minutes
        for label in self._auto_wake_map:
            self.auto_wake_combo.addItem(label)
        current_wake = getattr(self.settings, "auto_wake_minutes",
                               constants.AUTO_WAKE_DEFAULT_MIN)
        for label, minutes in self._auto_wake_map.items():
            if minutes == current_wake:
                self.auto_wake_combo.setCurrentText(label)
        self.auto_wake_combo.setToolTip(
            "When nothing has arrived for this long, the background agent "
            "restarts Messages on the Mac so held-back texts come through. "
            "Skipped automatically while any of your messages is sending.")
        form.addRow("Auto Wake Mac", self.auto_wake_combo)
        lay.addLayout(form)

        row = QHBoxLayout()
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        row.addWidget(self.test_btn)
        row.addWidget(self.result, 1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.tabs.addTab(page, "Connection")

    # ------------------------------------------------------------ Alerts

    def _build_alerts_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        form = QFormLayout()

        # Two plain master switches, exactly as asked: popups on or off,
        # sound on or off. Independent of each other and of the style.
        self.popups_enabled = QCheckBox(
            "Show a popup for every new message")
        self.popups_enabled.setChecked(
            getattr(self.settings, "popups_enabled", True))
        form.addRow("Popups", self.popups_enabled)

        self.notification_sound = QCheckBox(
            "Play a sound for every new message")
        self.notification_sound.setChecked(
            getattr(self.settings, "notification_sound", True))
        form.addRow("Sound", self.notification_sound)

        # Texts sent to your own number or email look sent-by-you to Apple
        # but arrive here and should alert like anything else.
        self.self_chat_alerts = QCheckBox(
            "Alert for texts you send to yourself")
        self.self_chat_alerts.setChecked(
            getattr(self.settings, "self_chat_alerts", True))
        self.self_chat_alerts.setToolTip(
            "A text to your own number or email is marked as sent by you "
            "on every Apple device, but it arrives here and alerts. Texts "
            "sent from this app never alert.")
        self_row = QHBoxLayout()
        self_row.addWidget(self.self_chat_alerts)
        self.self_addresses = QLineEdit(
            getattr(self.settings, "self_addresses", ""))
        self.self_addresses.setPlaceholderText(
            "Your other numbers/emails, comma separated (optional)")
        self.self_addresses.setToolTip(
            "The account your Mac reports is recognized automatically. "
            "Add any other own number or email here, for example "
            "+15875550123, so its self-conversation alerts too.")
        self_row.addWidget(self.self_addresses, 1)
        form.addRow("Self-texts", self_row)

        self.notify_combo = QComboBox()
        self._notify_map = {
            "Interactive popup (Copy/Fill)": "popup",
            "Windows notification (Focus Assist applies)": "system",
        }
        for label in self._notify_map:
            self.notify_combo.addItem(label)
        for label, mode in self._notify_map.items():
            if mode == getattr(self.settings, "notify_mode", "popup"):
                self.notify_combo.setCurrentText(label)

        notify_row = QHBoxLayout()
        notify_row.addWidget(self.notify_combo, 1)
        test_btn = QPushButton("Test alert")
        test_btn.setToolTip(
            "Fire a real test alert through the exact live pipeline, "
            "honoring the Popups and Sound switches above")
        test_btn.clicked.connect(
            lambda: self._on_test and self._on_test(
                self._notify_map.get(
                    self.notify_combo.currentText(), "popup"),
                self.notification_sound.isChecked(),
                self.popups_enabled.isChecked()))
        notify_row.addWidget(test_btn)
        verify_btn = QPushButton("Verify line")
        verify_btn.setToolTip("Send a check to your own number and "
                              "measure the round trip")
        verify_btn.clicked.connect(
            lambda: self._on_verify and self._on_verify())
        notify_row.addWidget(verify_btn)
        form.addRow("Alert style", notify_row)

        self.interactive_codes = QCheckBox(
            "Always show Copy and Fill for verification codes")
        self.interactive_codes.setChecked(
            getattr(self.settings, "interactive_codes", True))
        self.interactive_codes.setToolTip(
            "Even when Windows notification is selected, code messages use "
            "the interactive popup because the tray API cannot provide "
            "reliable action buttons")
        form.addRow("Code actions", self.interactive_codes)

        # The in-app notification center: the bell beside the gear.
        self.alert_center_enabled = QCheckBox(
            "Keep a feed of recent alerts behind the bell")
        self.alert_center_enabled.setChecked(
            getattr(self.settings, "alert_center_enabled", True))
        self.alert_center_enabled.setToolTip(
            "Every alert (messages, Mac wakes, repairs, connection "
            "changes) is kept in a quiet in-app list, including ones "
            "raised while you were away. Off hides the bell entirely; "
            "single entries can always be hidden from the list itself.")
        form.addRow("Bell", self.alert_center_enabled)

        # iPhone notification mirroring over Bluetooth: the same ANCS
        # mechanism a smartwatch uses. Experimental because Bluetooth
        # adapters and iOS versions vary; it can be switched off at any
        # time without touching anything else. Texts are never mirrored
        # twice; the Mac relay owns those.
        self.phone_link_enabled = QCheckBox(
            "Mirror iPhone notifications arriving over Bluetooth "
            "(experimental)")
        self.phone_link_enabled.setChecked(
            getattr(self.settings, "phone_link_enabled", False))
        self.phone_link_enabled.setToolTip(
            "Shows your iPhone's app notifications (social, email, bank, "
            "calendar…) as popups and in the bell while the phone is "
            "paired with Windows and within Bluetooth range. Texts keep "
            "coming through the Mac as always and are never doubled. "
            "First time: pair the iPhone in Windows Bluetooth settings, "
            "choose it below, then allow notifications on the phone.")
        form.addRow("iPhone", self.phone_link_enabled)

        self._phone_choice = (
            getattr(self.settings, "phone_ble_name", ""),
            getattr(self.settings, "phone_ble_address", ""))
        phone_row = QHBoxLayout()
        self.phone_device_label = QLabel(self._phone_device_text())
        self.phone_device_label.setStyleSheet(f"color: {theme.MUTED};")
        phone_row.addWidget(self.phone_device_label, 1)
        phone_test_btn = QPushButton("Test link")
        phone_test_btn.setToolTip(
            "Ask your iPhone a real question over the link right now. "
            "An answer with its timing proves the whole pipe; a failure "
            "says exactly which step is stuck. The result appears as a "
            "popup and in the bell.")
        phone_test_btn.clicked.connect(
            lambda: self._on_phone_test and self._on_phone_test())
        phone_row.addWidget(phone_test_btn)
        pick_btn = QPushButton("Choose iPhone…")
        pick_btn.setToolTip("Scan for your paired iPhone and remember it")
        pick_btn.clicked.connect(self._pick_phone)
        phone_row.addWidget(pick_btn)
        form.addRow("", phone_row)

        self.phone_ignore_apps = QLineEdit(
            getattr(self.settings, "phone_ignore_apps", ""))
        self.phone_ignore_apps.setPlaceholderText(
            "Mute noisy apps, comma separated (e.g. instagram, tiktok)")
        self.phone_ignore_apps.setToolTip(
            "Any app whose identifier contains one of these words stays "
            "quiet. Texts (Messages) are always muted here because they "
            "already arrive through the Mac with full history.")
        form.addRow("Mute apps", self.phone_ignore_apps)
        lay.addLayout(form)
        lay.addStretch(1)
        self.tabs.addTab(page, "Alerts")

    # -------------------------------------------------------------- Look

    def _build_look_tab(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        form = QFormLayout()

        self.size_combo = QComboBox()
        for name in theme.FONT_SIZES:
            self.size_combo.addItem(name)
        current = min(theme.FONT_SIZES,
                      key=lambda k: abs(theme.FONT_SIZES[k]
                                        - (self.settings.font_scale or 1.0)))
        self.size_combo.setCurrentText(current)
        form.addRow("Text size", self.size_combo)
        lay.addLayout(form)

        # The tint suite: every accent color as a named patch, not a bare
        # name. Clicking previews live exactly like the old combo did.
        tint_label = QLabel("Tint color")
        tint_label.setStyleSheet(f"color: {theme.MUTED};")
        lay.addWidget(tint_label)
        self._accent_name = (self.settings.accent
                             if self.settings.accent in theme.ACCENTS
                             else "Blue")
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, theme.dim(2), 0, theme.dim(4))
        grid.setSpacing(theme.dim(6))
        self.swatch_group = QButtonGroup(self)
        self.swatch_group.setExclusive(True)
        self._swatch_buttons = {}
        patch = theme.dim(26)
        columns = 4
        for index, name in enumerate(theme.ACCENTS):
            b = QToolButton()
            b.setObjectName("swatch")
            b.setText(name)
            b.setIcon(QIcon(theme.swatch_pixmap(name, patch)))
            b.setIconSize(QSize(patch, patch))
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumWidth(theme.dim(76))
            b.setToolTip(f"{name} tint")
            b.setAccessibleName(f"{name} tint")
            b.clicked.connect(
                lambda _checked=False, n=name: self._select_accent(n))
            self.swatch_group.addButton(b)
            self._swatch_buttons[name] = b
            grid.addWidget(b, index // columns, index % columns)
        grid_host.setStyleSheet(
            f"QToolButton#swatch {{ background: {theme.PANEL2}; "
            f"border: 1px solid {theme.BORDER}; "
            f"border-radius: {theme.dim(9)}px; padding: {theme.dim(5)}px; "
            f"font-size: {theme.fs(8.2)}; color: {theme.MUTED}; }} "
            f"QToolButton#swatch:hover {{ border-color: {theme.ACCENT}; "
            f"color: {theme.TEXT}; }} "
            "QToolButton#swatch:checked { border: 2px solid #ffffff; "
            "color: white; font-weight: 600; }")
        if self._accent_name in self._swatch_buttons:
            self._swatch_buttons[self._accent_name].setChecked(True)
        lay.addWidget(grid_host)

        behavior = QFormLayout()
        self.tooltip_combo = QComboBox()
        self._tooltip_map = {
            "Show each tip twice": "limited",
            "Always show tips": "always",
            "Turn tips off": "off",
        }
        for label in self._tooltip_map:
            self.tooltip_combo.addItem(label)
        for label, mode in self._tooltip_map.items():
            if mode == getattr(self.settings, "tooltip_mode", "limited"):
                self.tooltip_combo.setCurrentText(label)
        tips_row = QHBoxLayout()
        tips_row.addWidget(self.tooltip_combo, 1)
        reset_tips = QPushButton("Reset learned tips")
        reset_tips.clicked.connect(self._reset_tips)
        tips_row.addWidget(reset_tips)
        behavior.addRow("Help tips", tips_row)

        self.close_to_tray = QCheckBox(
            "Keep notifications running when I close the window")
        self.close_to_tray.setChecked(
            getattr(self.settings, "close_to_tray", True))
        behavior.addRow("Close button", self.close_to_tray)
        lay.addLayout(behavior)
        lay.addStretch(1)
        self.tabs.addTab(page, "Look")

        self.size_combo.currentTextChanged.connect(self._emit_preview)

    # ----------------------------------------------------------- actions

    def _phone_device_text(self) -> str:
        name, address = self._phone_choice
        live = ""
        if self._phone_status is not None:
            try:
                live = self._phone_status() or ""
            except Exception:
                live = ""
        if name or address:
            base = f"Following: {name or 'iPhone'} ({address})"
            return f"{base} · {live}" if live else base
        return live or "No iPhone chosen yet"

    def _pick_phone(self):
        picker = PhonePickerDialog(self, phone_pause=self._phone_pause)
        if picker.exec() and picker.choice:
            name, address = picker.choice
            # The Find button's verified label is proof text, not a
            # name; store something short and human instead.
            if name.lower().startswith("your iphone (verified"):
                name = "Your iPhone"
            self._phone_choice = (name, address)
            self.phone_device_label.setText(self._phone_device_text())
            self.phone_link_enabled.setChecked(True)

    def _select_accent(self, name: str):
        self._accent_name = name
        self._emit_preview()

    def _emit_preview(self, _text=None):
        if self._on_preview is not None:
            self._on_preview(
                self._accent_name,
                theme.FONT_SIZES.get(self.size_combo.currentText(), 1.0))

    def _test(self):
        url = self.url.text().strip().rstrip("/")
        if not url:
            self.result.setText("Enter the server URL first.")
            return
        self.test_btn.setEnabled(False)
        self.result.setStyleSheet(f"color: {theme.MUTED};")
        self.result.setText("Testing…")
        self._tester.start(url, self.pw.text())

    def _on_tested(self, ok: bool, msg: str):
        self.test_btn.setEnabled(True)
        self.result.setStyleSheet(
            f"color: {theme.OK};" if ok else f"color: {theme.FAIL};")
        self.result.setText(msg)

    def _save(self):
        self.settings.server_url = self.url.text().strip().rstrip("/")
        self.settings.font_scale = theme.FONT_SIZES.get(
            self.size_combo.currentText(), 1.0)
        self.settings.accent = self._accent_name
        self.settings.auto_wake_minutes = self._auto_wake_map.get(
            self.auto_wake_combo.currentText(),
            constants.AUTO_WAKE_DEFAULT_MIN)
        self.settings.notify_mode = self._notify_map.get(
            self.notify_combo.currentText(), "popup")
        self.settings.interactive_codes = self.interactive_codes.isChecked()
        self.settings.popups_enabled = self.popups_enabled.isChecked()
        self.settings.notification_sound = self.notification_sound.isChecked()
        self.settings.self_chat_alerts = self.self_chat_alerts.isChecked()
        self.settings.self_addresses = self.self_addresses.text().strip()
        self.settings.alert_center_enabled = \
            self.alert_center_enabled.isChecked()
        self.settings.phone_link_enabled = \
            self.phone_link_enabled.isChecked()
        self.settings.phone_ble_name = self._phone_choice[0]
        self.settings.phone_ble_address = self._phone_choice[1]
        self.settings.phone_ignore_apps = \
            self.phone_ignore_apps.text().strip()
        self.settings.tooltip_mode = self._tooltip_map.get(
            self.tooltip_combo.currentText(), "limited")
        self.settings.close_to_tray = self.close_to_tray.isChecked()
        config.save(self.settings)
        config.set_password(self.settings, self.pw.text())
        self.accept()

    def _reset_tips(self):
        self.settings.tooltip_seen = {}
        if self._on_reset_tips is not None:
            self._on_reset_tips()
