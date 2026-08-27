"""The 3.6.0 desktop upgrades, against real widgets offscreen.

Per-conversation drafts (a leak fix as much as a feature), the jump
pill's new-arrivals counter, the plain-text transcript exporter, and
the keyboard shortcuts card. This module sorts first alphabetically on
purpose: it creates the full QApplication that widget construction
needs, and the later suites' QCoreApplication checks reuse it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("JRL_SMOKE", "1")

try:
    import platformdirs  # noqa: F401
except ModuleNotFoundError:
    platformdirs = types.ModuleType("platformdirs")
    platformdirs.user_data_dir = lambda *_a, **_k: tempfile.gettempdir()
    sys.modules["platformdirs"] = platformdirs

ROOT = Path(__file__).parents[1]

PEER = "+15555550100"
CHAT_A = "iMessage;-;+15555550100"
CHAT_B = "iMessage;-;+15555550111"


def _repo(tmpdir: Path):
    from app.store.db import Database
    from app.store.repo import Repo
    return Repo(Database(tmpdir / "t.db"))


def _add_chat(repo, guid, name):
    repo.upsert_chat({"guid": guid, "display_name": name, "is_group": 0,
                      "participants": f'["{PEER}"]',
                      "last_activity": 1, "archived": 0})


def _add_msg(repo, guid, chat, ts, from_me=0, text="hi",
             sender=PEER, edited=0):
    repo.db.write(
        "INSERT INTO messages(guid, chat_guid, sender_address, is_from_me,"
        " text, date_created, is_edited) VALUES(?,?,?,?,?,?,?)",
        (guid, chat, sender, from_me, text, ts, edited))


def _chat_row(repo, guid):
    return repo.db.one("SELECT * FROM chats WHERE guid=?", (guid,))


class _WidgetCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = _repo(self.tmp)
        self.addCleanup(self._tmp.cleanup)

    def _view(self):
        from app.ui.thread_view import ThreadView
        tv = ThreadView(self.repo)
        self.addCleanup(tv.deleteLater)
        return tv


class DraftTests(_WidgetCase):
    """Half-typed text used to FOLLOW the user into the next
    conversation: the exact way a wrong-recipient send begins. Now each
    conversation keeps its own draft, restored on return, cleared on
    send."""

    def setUp(self):
        super().setUp()
        _add_chat(self.repo, CHAT_A, "Alice")
        _add_chat(self.repo, CHAT_B, "Bob")

    def test_draft_no_longer_leaks_into_the_next_conversation(self):
        tv = self._view()
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        tv.composer.edit.setPlainText("sensitive words meant for Alice")
        tv.load_chat(_chat_row(self.repo, CHAT_B))
        self.assertEqual("", tv.composer.edit.toPlainText(),
                         "Alice's half-typed message must never appear "
                         "in Bob's composer")

    def test_each_conversation_keeps_and_restores_its_own_draft(self):
        tv = self._view()
        staged = self.tmp / "exhibit.txt"
        staged.write_text("x", encoding="utf-8")
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        tv.composer.edit.setPlainText("draft for Alice")
        tv.composer.stage_files([str(staged)])
        tv.load_chat(_chat_row(self.repo, CHAT_B))
        tv.composer.edit.setPlainText("draft for Bob")
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        self.assertEqual("draft for Alice", tv.composer.edit.toPlainText())
        self.assertEqual([str(staged)], tv.composer.files,
                         "staged files are part of the draft")
        tv.load_chat(_chat_row(self.repo, CHAT_B))
        self.assertEqual("draft for Bob", tv.composer.edit.toPlainText())
        self.assertEqual([], tv.composer.files)

    def test_reload_of_the_same_chat_leaves_the_composer_alone(self):
        tv = self._view()
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        tv.composer.edit.setPlainText("still typing")
        tv.load_chat(_chat_row(self.repo, CHAT_A), preserve_scroll=True)
        self.assertEqual("still typing", tv.composer.edit.toPlainText(),
                         "a live rebuild must not eat the draft")

    def test_sending_clears_the_stored_draft(self):
        tv = self._view()
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        tv.composer.edit.setPlainText("out it goes")
        tv.composer._send()
        tv.load_chat(_chat_row(self.repo, CHAT_B))
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        self.assertEqual("", tv.composer.edit.toPlainText(),
                         "sent content is no longer a draft")


class JumpCounterTests(_WidgetCase):
    """While the user reads history, arrivals below the fold count up
    on the jump pill ('3 new messages') instead of yanking the view or
    passing silently; any route back to the bottom resets it."""

    def setUp(self):
        super().setUp()
        _add_chat(self.repo, CHAT_A, "Alice")
        _add_msg(self.repo, "m1", CHAT_A, 1_000, text="first")

    def _incoming(self, tv, guid, ts, from_me=0):
        _add_msg(self.repo, guid, CHAT_A, ts, from_me=from_me, text="new")
        tv.apply_message({"chat_guid": CHAT_A, "guid": guid,
                          "is_from_me": from_me})

    def test_arrivals_below_the_fold_count_up_and_reset_at_bottom(self):
        tv = self._view()
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        tv.near_bottom = lambda: False        # the user is reading history
        self._incoming(tv, "m2", 2_000)
        self.assertEqual("1 new message", tv.jump_btn.text())
        self._incoming(tv, "m3", 3_000)
        self.assertEqual("2 new messages", tv.jump_btn.text())
        # The user's own send always returns to the bottom; it is not
        # an unseen arrival and must not inflate the count.
        self._incoming(tv, "m4", 4_000, from_me=1)
        self.assertEqual("2 new messages", tv.jump_btn.text())
        tv.near_bottom = lambda: True
        tv._update_jump()
        self.assertEqual("Most recent", tv.jump_btn.text())
        self.assertEqual(0, tv._jump_new)
        self.assertFalse(tv.jump_btn.isVisible())

    def test_switching_conversations_starts_the_count_fresh(self):
        _add_chat(self.repo, CHAT_B, "Bob")
        tv = self._view()
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        tv.near_bottom = lambda: False
        self._incoming(tv, "m2", 2_000)
        self.assertEqual(1, tv._jump_new)
        tv.load_chat(_chat_row(self.repo, CHAT_B))
        self.assertEqual(0, tv._jump_new)
        self.assertEqual("Most recent", tv.jump_btn.text())


class TranscriptTests(_WidgetCase):
    """Ctrl+E writes the whole conversation as plain text: dated lines,
    Me for the user's side, attachment markers, edit markers."""

    def test_export_writes_the_conversation_in_order(self):
        _add_chat(self.repo, CHAT_A, "Case Notes")
        _add_msg(self.repo, "m1", CHAT_A, 1_700_000_000_000,
                 text="hello there")
        _add_msg(self.repo, "m2", CHAT_A, 1_700_000_060_000, from_me=1,
                 text="on my way", edited=1)
        _add_msg(self.repo, "m3", CHAT_A, 1_700_000_120_000, from_me=1,
                 text=None)
        self.repo.db.write(
            "INSERT INTO attachments(guid, message_guid, file_name)"
            " VALUES('a1','m3','scan.pdf')")
        tv = self._view()
        tv.load_chat(_chat_row(self.repo, CHAT_A))
        out = self.tmp / "transcript.txt"
        written = tv.export_conversation(path=str(out))
        self.assertEqual(str(out), written)
        content = out.read_text(encoding="utf-8")
        self.assertIn("Case Notes", content)
        self.assertIn("3 messages", content)
        self.assertIn(f"{PEER}: hello there", content)
        self.assertIn("Me: on my way  [edited]", content)
        self.assertIn("[attachment: scan.pdf]", content)
        self.assertLess(content.index("hello there"),
                        content.index("on my way"))
        self.assertLess(content.index("on my way"),
                        content.index("scan.pdf"))

    def test_export_with_nothing_open_is_a_quiet_no_op(self):
        tv = self._view()
        self.assertIsNone(tv.export_conversation(path=str(self.tmp / "x")))


class UpgradeWiringTests(unittest.TestCase):
    """Source-level pins, in the house style: the bindings, the header
    button, and the draft plumbing must stay present and named."""

    def test_main_window_binds_export_and_the_shortcuts_card(self):
        src = (ROOT / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        for needed in ('QKeySequence("Ctrl+E")', 'QKeySequence("Ctrl+/")',
                       '"F1"', "def _show_shortcuts", "Keyboard shortcuts",
                       '"Ctrl+Shift+M"', "Wake Mac"):
            self.assertIn(needed, src)

    def test_thread_view_carries_the_upgrade_surface(self):
        src = (ROOT / "app" / "ui" / "thread_view.py").read_text(
            encoding="utf-8")
        for needed in ("def export_conversation", "Ctrl+E",
                       "def _stash_draft", "def _set_jump_count",
                       "export_btn"):
            self.assertIn(needed, src)

    def test_composer_carries_the_draft_api(self):
        src = (ROOT / "app" / "ui" / "composer.py").read_text(
            encoding="utf-8")
        for needed in ("def draft_state", "def restore_draft",
                       "def clear_content", "confirm_large"):
            self.assertIn(needed, src)
        self.assertIn("confirm_large=False", src,
                      "draft restoration must never pop the large-file "
                      "question box")

    def test_icons_offer_the_download_glyph(self):
        src = (ROOT / "app" / "ui" / "icons.py").read_text(encoding="utf-8")
        self.assertIn("def download", src)


if __name__ == "__main__":
    unittest.main()
