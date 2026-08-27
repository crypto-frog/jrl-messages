"""Group details: who is in the conversation, with add, remove, and
rename. Viewing always works. Changing membership requires the Mac's
Private API mode; without it the controls disable themselves and say
exactly why, instead of failing mysteriously."""
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout)

from ..api.rest import ApiError
from ..util.textutil import normalize_address, to_imessage_address
from . import theme

_REQUIRES = ("Managing members requires Private API mode in BlueBubbles "
             "on the Mac (Settings > Private API). Sending and receiving "
             "in this group works regardless.")


class _Op(QObject):
    done = Signal(bool, str, str)   # ok, action, detail

    def start(self, fn, action: str, detail: str):
        def work():
            try:
                fn()
                self.done.emit(True, action, detail)
            except ApiError as e:
                self.done.emit(False, action, str(e))
            except Exception:
                self.done.emit(False, action, "Unexpected error; see log.")
        threading.Thread(target=work, daemon=True).start()


class GroupDialog(QDialog):
    def __init__(self, repo, client, chat_guid: str, handles: dict,
                 private_api: bool, parent=None, on_changed=None):
        super().__init__(parent)
        self.repo = repo
        self.client = client
        self.chat_guid = chat_guid
        self.handles = handles
        self.private_api = private_api
        self.on_changed = on_changed
        self.contacts = repo.contacts_all()

        row = repo.db.one("SELECT * FROM chats WHERE guid=?", (chat_guid,))
        self._display_name = (row["display_name"] or "") if row else ""

        self.setWindowTitle("Group details")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)

        name_row = QHBoxLayout()
        self.name_edit = QLineEdit(self._display_name)
        self.name_edit.setPlaceholderText("Group name")
        self.rename_btn = QPushButton("Rename")
        self.rename_btn.clicked.connect(self._rename)
        name_row.addWidget(self.name_edit, 1)
        name_row.addWidget(self.rename_btn)
        root.addLayout(name_row)

        self.members = QListWidget()
        self.members.setMinimumHeight(160)
        root.addWidget(QLabel("Members"))
        root.addWidget(self.members, 1)

        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove)
        root.addWidget(self.remove_btn, 0, Qt.AlignLeft)

        root.addWidget(QLabel("Add someone"))
        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("Name, number, or email")
        self.add_edit.textChanged.connect(self._filter_add)
        root.addWidget(self.add_edit)
        self.add_results = QListWidget()
        self.add_results.setFixedHeight(110)
        root.addWidget(self.add_results)
        self.add_btn = QPushButton("Add to group")
        self.add_btn.clicked.connect(self._add)
        root.addWidget(self.add_btn, 0, Qt.AlignLeft)

        self.state = QLabel("")
        self.state.setWordWrap(True)
        self.state.setStyleSheet(f"color: {theme.MUTED};")
        root.addWidget(self.state)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignRight)

        self._op = _Op()
        self._op.done.connect(self._on_done, Qt.QueuedConnection)

        if not self.private_api:
            for w in (self.rename_btn, self.remove_btn, self.add_btn,
                      self.name_edit, self.add_edit, self.add_results):
                w.setEnabled(False)
            self.state.setText(_REQUIRES)

        self._reload_members()
        self._filter_add("")

    # ------------------------------------------------ data

    def _reload_members(self):
        self.members.clear()
        for addr in self.repo.participants_of(self.chat_guid):
            name = self.repo.name_for(addr, self.handles)
            label = name if name != addr else addr
            item = QListWidgetItem(f"{label}   \u00b7   {addr}"
                                   if label != addr else addr)
            item.setData(Qt.UserRole, addr)
            self.members.addItem(item)

    def _filter_add(self, text: str):
        q = (text or "").strip().lower()
        current = {normalize_address(a)
                   for a in self.repo.participants_of(self.chat_guid)}
        self.add_results.clear()
        from ..util.textutil import looks_like_address
        if looks_like_address(q):
            addr = to_imessage_address(q)
            item = QListWidgetItem(f"Add: {addr}")
            item.setData(Qt.UserRole, addr)
            self.add_results.addItem(item)
        shown = 0
        for name, address in self.contacts:
            if shown >= 40:
                break
            if normalize_address(address) in current:
                continue
            if q and q not in f"{name} {address}".lower():
                continue
            item = QListWidgetItem(f"{name}   \u00b7   {address}")
            item.setData(Qt.UserRole, address)
            self.add_results.addItem(item)
            shown += 1

    # ------------------------------------------------ actions

    def _busy(self, text: str):
        self.state.setStyleSheet(f"color: {theme.MUTED};")
        self.state.setText(text)
        for b in (self.rename_btn, self.remove_btn, self.add_btn):
            b.setEnabled(False)

    def _idle(self):
        if self.private_api:
            for b in (self.rename_btn, self.remove_btn, self.add_btn):
                b.setEnabled(True)

    def _rename(self):
        name = self.name_edit.text().strip()
        if not name or self.client is None:
            return
        self._busy("Renaming\u2026")
        self._op.start(lambda: self.client.rename_chat(self.chat_guid, name),
                       "rename", name)

    def _remove(self):
        items = self.members.selectedItems()
        if not items or self.client is None:
            return
        addr = items[0].data(Qt.UserRole)
        self._busy(f"Removing {addr}\u2026")
        self._op.start(
            lambda: self.client.remove_participant(self.chat_guid, addr),
            "remove", addr)

    def _add(self):
        items = self.add_results.selectedItems()
        if not items or self.client is None:
            return
        addr = to_imessage_address(items[0].data(Qt.UserRole))
        self._busy(f"Adding {addr}\u2026")
        self._op.start(
            lambda: self.client.add_participant(self.chat_guid, addr),
            "add", addr)

    def _on_done(self, ok: bool, action: str, detail: str):
        self._idle()
        if not ok:
            self.state.setStyleSheet(f"color: {theme.FAIL};")
            self.state.setText(detail)
            return
        parts = self.repo.participants_of(self.chat_guid)
        if action == "add" and detail not in parts:
            parts.append(detail)
            self.repo.set_participants(self.chat_guid, parts)
        elif action == "remove":
            norm = normalize_address(detail)
            parts = [p for p in parts if normalize_address(p) != norm]
            self.repo.set_participants(self.chat_guid, parts)
        elif action == "rename":
            self.repo.db.write(
                "UPDATE chats SET display_name=? WHERE guid=?",
                (detail, self.chat_guid))
        self.state.setStyleSheet(f"color: {theme.OK};")
        self.state.setText("Done.")
        self._reload_members()
        self._filter_add(self.add_edit.text())
        if self.on_changed:
            self.on_changed(self.chat_guid)
