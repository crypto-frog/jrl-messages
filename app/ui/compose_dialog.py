"""New Message dialog. Filters the synced address book, accepts raw
numbers or emails, opens an existing conversation instantly, and creates
a new one through the Mac when none exists. Network work runs off the
UI thread; failures land inline in the dialog, never silently."""
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QVBoxLayout,
                               QWidget)

from ..api import models
from ..api.rest import ApiError
from ..util.textutil import (looks_like_address, normalize_address,
                             to_imessage_address)
from . import theme


class _Creator(QObject):
    done = Signal(bool, str)   # ok, chat_guid or error text

    def start(self, client, addresses: list, message: str, service: str):
        def work():
            try:
                data = client.create_chat(addresses, message, service)
                guid = (data.get("guid")
                        or (data.get("chat") or {}).get("guid") or "")
                if guid:
                    self.done.emit(True, guid)
                else:
                    self.done.emit(False,
                                   "Server created no conversation; see log.")
            except ApiError as e:
                self.done.emit(False, str(e))
            except Exception:
                self.done.emit(False, "Unexpected error; see log.")
        threading.Thread(target=work, daemon=True).start()


class ComposeDialog(QDialog):
    def __init__(self, repo, client, parent=None, on_open=None,
                 private_api: bool = False):
        super().__init__(parent)
        self.repo = repo
        self.client = client
        self.on_open = on_open
        self.private_api = private_api
        self.contacts = repo.contacts_all()
        self.picked: list = []   # [(label, address)] chips

        self.setWindowTitle("New message")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        self.chips_row = QHBoxLayout()
        self.chips_row.addStretch(1)
        chips_host = QWidget()
        chips_host.setLayout(self.chips_row)
        root.addWidget(chips_host)
        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText("To: name, phone number, or email")
        self.to_edit.textChanged.connect(self._filter)
        self.to_edit.returnPressed.connect(self._pick_first)
        root.addWidget(self.to_edit)

        self.results = QListWidget()
        self.results.setMinimumHeight(180)
        self.results.itemActivated.connect(self._on_pick)
        self.results.itemClicked.connect(self._on_pick)
        root.addWidget(self.results, 1)

        self.state = QLabel("")
        self.state.setStyleSheet(f"color: {theme.MUTED};")
        self.state.setWordWrap(True)
        root.addWidget(self.state)

        row = QHBoxLayout()
        self.service = QComboBox()
        self.service.addItems(["iMessage", "SMS"])
        row.addWidget(QLabel("Send as"))
        row.addWidget(self.service)
        row.addStretch(1)
        root.addLayout(row)

        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("First message")
        self.message.setFixedHeight(theme.dim(70))
        root.addWidget(self.message)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.go_btn = QPushButton("Open")
        self.go_btn.setObjectName("accent")
        self.go_btn.setEnabled(False)
        self.go_btn.clicked.connect(self._go)
        buttons.addWidget(cancel)
        buttons.addWidget(self.go_btn)
        root.addLayout(buttons)

        self._creator = _Creator()
        self._creator.done.connect(self._on_created, Qt.QueuedConnection)
        self._filter("")
        self.to_edit.setFocus()

    # ------------------------------------------------ list handling

    def _filter(self, text: str):
        q = (text or "").strip().lower()
        self.results.clear()
        if looks_like_address(q):
            addr = to_imessage_address(q)
            item = QListWidgetItem(f"Send to: {addr}")
            item.setData(Qt.UserRole, (addr, addr))
            self.results.addItem(item)
        digits = "".join(c for c in q if c.isdigit())
        shown = 0
        for name, address in self.contacts:
            if shown >= 60:
                break
            hay = f"{name} {address}".lower()
            if q and q not in hay and not (digits and digits in
                                           normalize_address(address)):
                continue
            item = QListWidgetItem(f"{name}   \u00b7   {address}")
            item.setData(Qt.UserRole, (name, address))
            self.results.addItem(item)
            shown += 1
        if self.results.count():
            self.results.setCurrentRow(0)
        else:
            self._selected = None
            self._update_state()

    def _pick_first(self):
        if self.results.count():
            self.results.setCurrentRow(0)
            self._on_pick()

    def _on_pick(self, *_):
        items = self.results.selectedItems()
        if not items:
            self._update_state()
            return
        label, address = items[0].data(Qt.UserRole)
        norm = normalize_address(address)
        if all(normalize_address(a) != norm for _l, a in self.picked):
            self.picked.append((label, address))
            self._add_chip(label, address)
        self._update_state()

    def _add_chip(self, label: str, address: str):
        chip = QPushButton(f"{label}  \u2715")
        chip.setStyleSheet(
            f"QPushButton {{ background: {theme.SEL_BG}; border: 1px solid "
            f"{theme.BORDER}; border-radius: 9px; padding: 3px 9px; }}")
        norm = normalize_address(address)
        chip.clicked.connect(lambda: self._remove_pick(norm, chip))
        self.chips_row.insertWidget(self.chips_row.count() - 1, chip)

    def _remove_pick(self, norm: str, chip):
        self.picked = [(l, a) for l, a in self.picked
                       if normalize_address(a) != norm]
        # Hide and delete in place; never orphan a visible widget into a
        # top-level window object.
        self.chips_row.removeWidget(chip)
        chip.hide()
        chip.deleteLater()
        self._update_state()

    def _resolve(self):
        """(kind, existing_guid) for the current chips:
        kind is none | single | group."""
        if not self.picked:
            return "none", None
        norms = {normalize_address(a) for _l, a in self.picked}
        if len(self.picked) == 1:
            return "single", self.repo.chat_for_address(next(iter(norms)))
        return "group", self.repo.group_for_addresses(norms)

    def _update_state(self):
        self.state.setStyleSheet(f"color: {theme.MUTED};")
        kind, existing = self._resolve()
        if kind == "none":
            self.go_btn.setEnabled(False)
            self.go_btn.setText("Open")
            self.state.setText("")
            self.message.setEnabled(False)
            self.service.setEnabled(False)
            return
        if existing:
            who = (self.picked[0][0] if kind == "single"
                   else f"these {len(self.picked)} people")
            self.go_btn.setText("Open")
            self.state.setText(f"Opens your existing conversation with {who}.")
            self.message.setEnabled(False)
            self.service.setEnabled(False)
            self.go_btn.setEnabled(True)
            return
        self.go_btn.setText("Send")
        self.message.setEnabled(True)
        if kind == "single":
            self.service.setEnabled(True)
            self.state.setText(
                f"Starts a new conversation with {self.picked[0][0]}. "
                "Type the first message below.")
            self.go_btn.setEnabled(True)
        else:
            self.service.setCurrentText("iMessage")
            self.service.setEnabled(False)
            if self.private_api:
                self.state.setText(
                    f"Starts a new group with {len(self.picked)} people. "
                    "Type the first message below.")
                self.go_btn.setEnabled(True)
            else:
                self.state.setText(
                    "Creating a new group requires Private API mode in "
                    "BlueBubbles on the Mac. Existing groups open and work "
                    "normally.")
                self.go_btn.setEnabled(False)

    # ------------------------------------------------ actions

    def _go(self):
        kind, existing = self._resolve()
        if kind == "none":
            return
        if existing:
            if self.on_open:
                self.on_open(existing)
            self.accept()
            return
        if kind == "group" and not self.private_api:
            return
        text = self.message.toPlainText().strip()
        if not text:
            self.state.setStyleSheet(f"color: {theme.WARN};")
            self.state.setText("Type the first message, then press Send.")
            return
        if self.client is None:
            self.state.setStyleSheet(f"color: {theme.FAIL};")
            self.state.setText("Not connected. Check settings first.")
            return
        self.go_btn.setEnabled(False)
        self.state.setStyleSheet(f"color: {theme.MUTED};")
        self.state.setText("Creating conversation\u2026")
        service = self.service.currentText()
        addresses = [to_imessage_address(a) for _l, a in self.picked]
        self._creator.start(self.client, addresses, text, service)

    def _on_created(self, ok: bool, payload: str):
        if not ok:
            self.go_btn.setEnabled(True)
            self.state.setStyleSheet(f"color: {theme.FAIL};")
            self.state.setText(payload)
            return
        guid = payload
        parsed = models.parse_chat({"guid": guid,
                                    "participants": [],
                                    "displayName": ""})
        if parsed:
            self.repo.ensure_chat(guid)
        if self.on_open:
            self.on_open(guid)
        self.accept()
