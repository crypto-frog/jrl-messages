"""Left rail list. Two modes share one model and delegate:
conversations (default) and search results (while the search box has text)."""
from dataclasses import dataclass, field
from typing import Optional
import zlib

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QRect, QSize,
                            Qt, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QListView, QStyledItemDelegate

from . import theme
from .icons import eye_off


def close_rect(row_rect: QRect) -> QRect:
    """The full Hide chip drawn on unread rows, centered in the hide zone.
    Sized from the text scale so it stays an easy target at every size
    setting."""
    d = max(30, theme.dim(32))
    return QRect(row_rect.right() - d - theme.dim(8),
                 row_rect.center().y() - d // 2, d, d)


def hide_zone(row_rect: QRect) -> QRect:
    """The right-hand strip of a row that hides the conversation.

    Every conversation row is two actions: the left side opens it, this
    zone hides it. Unread rows advertise the zone loudly with the eye-off
    chip; already-read rows keep only a subtle shade so the list stays
    calm, but the click still works. Painter and mouse routing share this
    exact geometry."""
    width = max(44, theme.dim(48))
    return QRect(row_rect.right() - width + 1, row_rect.top(),
                 width, row_rect.height())


_HIDE_ICON_CACHE: dict[tuple[str, int], object] = {}


def hide_icon_pixmap(color: str, px: int):
    """The eye-off Hide mark, rendered once per accent color and size.

    Painted from the same icon code as the labelled Hidden button below
    the list, so hiding a chat and reviewing hidden chats share one
    visual language. Cached because delegates paint on every mouse move.
    """
    key = (color, int(px))
    cached = _HIDE_ICON_CACHE.get(key)
    if cached is None:
        if len(_HIDE_ICON_CACHE) > 32:   # accent/scale changes are rare
            _HIDE_ICON_CACHE.clear()
        cached = eye_off(color).pixmap(int(px), int(px))
        _HIDE_ICON_CACHE[key] = cached
    return cached


class ChatListView(QListView):
    hide_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # A tooltip on the whole list reappeared over every row and even while
        # selecting a conversation.  Keep the instruction for assistive
        # technology without covering the user's messages.
        self.setAccessibleDescription(
            "Hover a conversation to reveal Hide, or use Control H or the "
            "right-click menu.")
        # The delegate paints the Hide control from State_MouseOver, which
        # only exists if the viewport generates hover events and repaints as
        # the cursor moves between rows. Without this, only rows repainted
        # for another reason (selection, unread updates) ever showed Hide.
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WA_Hover, True)
        self.viewport().setMouseTracking(True)
        self._hover_row = -1
        self._hover_in_zone = False

    def _repaint_hover_change(self, pos):
        index = self.indexAt(pos)
        row = index.row() if index.isValid() else -1
        in_zone = False
        if index.isValid():
            r = self.visualRect(index).adjusted(6, 3, -6, -3)
            in_zone = hide_zone(r).contains(pos)
        if row != self._hover_row or in_zone != self._hover_in_zone:
            for stale in (self._hover_row, row):
                if stale >= 0:
                    idx = self.model().index(stale, 0)
                    if idx.isValid():
                        self.viewport().update(self.visualRect(idx))
            self._hover_row = row
            self._hover_in_zone = in_zone
            self.setCursor(Qt.PointingHandCursor if in_zone
                           else Qt.ArrowCursor)

    def mouseMoveEvent(self, e):
        self._repaint_hover_change(e.position().toPoint())
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        if self._hover_row >= 0 and self.model() is not None:
            idx = self.model().index(self._hover_row, 0)
            if idx.isValid():
                self.viewport().update(self.visualRect(idx))
        self._hover_row = -1
        self._hover_in_zone = False
        self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        index = self.indexAt(e.position().toPoint())
        if index.isValid():
            row = index.data(Qt.UserRole)
            r = self.visualRect(index).adjusted(6, 3, -6, -3)
            # Right side hides, left side opens: one row, two actions.
            if (row is not None and row.focus_guid is None
                    and hide_zone(r).contains(e.position().toPoint())):
                self.hide_requested.emit(row.chat_guid)
                return
        super().mousePressEvent(e)

from ..util.textutil import initials
from ..util.timefmt import fmt_list_time

AVATAR_HUES = ["#5b7bd5", "#5aa06e", "#b3719b", "#a08a5a", "#6f8fa8", "#8a6fb8"]


@dataclass
class Row:
    chat_guid: str
    title: str
    snippet: str
    when: Optional[int]
    unread: int = 0
    is_group: bool = False
    focus_guid: Optional[str] = None   # set for search results
    extra: dict = field(default_factory=dict)


class ListModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.rows: list[Row] = []

    def set_rows(self, rows: list[Row]):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.UserRole:
            return self.rows[index.row()]
        if role == Qt.DisplayRole:
            return self.rows[index.row()].title
        return None


class RowDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.rect.width(), theme.dim(66))

    def paint(self, painter: QPainter, option, index):
        row: Row = index.data(Qt.UserRole)
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        r = option.rect.adjusted(6, 3, -6, -3)

        from PySide6.QtWidgets import QStyle
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        if selected:
            painter.setBrush(QColor(theme.SEL_BG))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(r, 10, 10)
            painter.setBrush(QColor(theme.ACCENT))
            bar_h = r.height() - theme.dim(22)
            painter.drawRoundedRect(
                r.left() + theme.dim(3), r.top() + theme.dim(11),
                max(2, theme.dim(3)), bar_h, 1, 1)
        elif hovered:
            painter.setBrush(QColor(theme.HOVER_BG))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(r, 10, 10)

        # avatar
        a = theme.dim(40)
        av = QRect(r.left() + 8, r.top() + (r.height() - a) // 2, a, a)
        base = QColor(theme.ACCENT)
        # Python randomizes hash() between launches. CRC32 keeps each contact's
        # avatar colour stable without persisting extra UI state.
        stable = zlib.crc32((row.chat_guid or "").encode("utf-8"))
        shift = (stable % 61) - 30
        h = (base.hue() + shift) % 360
        top = QColor.fromHsv(h, min(200, base.saturation()),
                             min(255, base.value()))
        from PySide6.QtGui import QLinearGradient
        grad = QLinearGradient(av.topLeft(), av.bottomRight())
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, top.darker(135))
        painter.setBrush(grad)
        if row.unread:
            from PySide6.QtGui import QPen
            pen = QPen(QColor(theme.ACCENT))
            pen.setWidth(2)
            painter.setPen(pen)
        else:
            painter.setPen(Qt.NoPen)
        painter.drawEllipse(av)
        painter.setPen(Qt.NoPen)
        f = QFont(option.font)
        f.setPointSizeF(10.5 * theme.scale())
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QColor("white"))
        painter.drawText(av, Qt.AlignCenter, initials(row.title))

        left = av.right() + 12
        when = fmt_list_time(row.when)
        fm_small = QFontMetrics(option.font)
        time_w = fm_small.horizontalAdvance(when) + 4

        # title
        tf = QFont(option.font)
        tf.setPointSizeF(10.6 * theme.scale())
        tf.setBold(row.unread > 0)
        painter.setFont(tf)
        painter.setPen(QColor(theme.ACCENT if selected else theme.TEXT))
        title_rect = QRect(left, r.top() + theme.dim(10),
                           r.right() - left - time_w - 18, theme.dim(20))
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(tf).elidedText(row.title, Qt.ElideRight,
                                                     title_rect.width()))

        # time (yields to the ✕ while hovered)
        if not (hovered and row.focus_guid is None):
            painter.setFont(option.font)
            painter.setPen(QColor(theme.ACCENT if row.unread else theme.MUTED))
            painter.drawText(QRect(r.right() - time_w - 10,
                                   r.top() + theme.dim(10),
                                   time_w + 4, theme.dim(20)),
                             Qt.AlignRight | Qt.AlignVCenter, when)

        # snippet
        sf = QFont(option.font)
        sf.setPointSizeF(9.6 * theme.scale())
        painter.setFont(sf)
        painter.setPen(QColor(theme.TEXT if row.unread else theme.MUTED))
        snip_rect = QRect(left, r.top() + theme.dim(34),
                          r.right() - left - theme.dim(42), theme.dim(18))
        painter.drawText(snip_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         QFontMetrics(sf).elidedText(row.snippet or "", Qt.ElideRight,
                                                     snip_rect.width()))

        # The hide zone (conversation rows only, not search results).
        # Unread rows advertise it with the full eye-off chip in the accent
        # color. Already-read rows stay calm on purpose: hovering shows only
        # a soft shade rising along the right edge, deepening with a faint
        # eye-off ghost when the cursor is actually inside the zone, enough
        # to say "clicking here does something" without decorating every
        # row. Left side opens; this side hides.
        if hovered and row.focus_guid is None:
            if row.unread:
                cr = close_rect(r)
                painter.setBrush(QColor(theme.HOVER_BG))
                pen = QPen(QColor(theme.ACCENT_BORDER))
                pen.setWidthF(max(1.0, 1.0 * theme.scale()))
                painter.setPen(pen)
                painter.drawEllipse(cr)
                icon_px = max(16, int(cr.width() * 0.62))
                pm = hide_icon_pixmap(theme.ACCENT, icon_px)
                painter.drawPixmap(
                    cr.center().x() - icon_px // 2 + 1,
                    cr.center().y() - icon_px // 2 + 1, pm)
            else:
                view = option.widget
                in_zone = bool(getattr(view, "_hover_in_zone", False))
                zone = hide_zone(r)
                from PySide6.QtGui import QLinearGradient, QPainterPath
                clip = QPainterPath()
                clip.addRoundedRect(r, 10, 10)
                painter.save()
                painter.setClipPath(clip)
                shade = QLinearGradient(zone.left(), 0, zone.right(), 0)
                tint = QColor(theme.ACCENT)
                tint.setAlpha(64 if in_zone else 30)
                edge = QColor(theme.ACCENT)
                edge.setAlpha(0)
                shade.setColorAt(0.0, edge)
                shade.setColorAt(1.0, tint)
                painter.setPen(Qt.NoPen)
                painter.setBrush(shade)
                painter.drawRect(zone)
                if in_zone:
                    icon_px = max(15, theme.dim(17))
                    pm = hide_icon_pixmap(theme.ACCENT, icon_px)
                    painter.setOpacity(0.55)
                    painter.drawPixmap(
                        zone.center().x() - icon_px // 2 + theme.dim(6),
                        zone.center().y() - icon_px // 2, pm)
                    painter.setOpacity(1.0)
                painter.restore()

        # unread badge with count
        if row.unread:
            text = str(row.unread) if row.unread < 100 else "99+"
            bf = QFont(option.font)
            bf.setPointSizeF(8.2 * theme.scale())
            bf.setBold(True)
            painter.setFont(bf)
            bw = max(theme.dim(18),
                     QFontMetrics(bf).horizontalAdvance(text) + theme.dim(10))
            bh = theme.dim(17)
            bx = r.right() - bw - 8
            by = r.top() + theme.dim(36)
            painter.setBrush(QColor(theme.ACCENT))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(bx, by, bw, bh, bh // 2, bh // 2)
            painter.setPen(QColor("white"))
            painter.drawText(QRect(bx, by, bw, bh), Qt.AlignCenter, text)
        painter.restore()
