"""Icons drawn in code: rounded strokes, crisp at any size and scale.
Font glyphs render inconsistently across systems; these do not."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPen, QPixmap,
                           QPolygonF)


def _pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _canvas(size: int):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def arrow_up(color: str = "#ffffff", size: int = 64) -> QIcon:
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.11))
    p.drawLine(QPointF(s * 0.50, s * 0.76), QPointF(s * 0.50, s * 0.30))
    p.drawLine(QPointF(s * 0.50, s * 0.26), QPointF(s * 0.31, s * 0.45))
    p.drawLine(QPointF(s * 0.50, s * 0.26), QPointF(s * 0.69, s * 0.45))
    p.end()
    return QIcon(pm)


def plus(color: str = "#98a1b0", size: int = 64) -> QIcon:
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.11))
    p.drawLine(QPointF(s * 0.50, s * 0.26), QPointF(s * 0.50, s * 0.74))
    p.drawLine(QPointF(s * 0.26, s * 0.50), QPointF(s * 0.74, s * 0.50))
    p.end()
    return QIcon(pm)


def pencil(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """A filled compose pencil that remains legible at small Windows sizes."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.25, s * 0.65), QPointF(s * 0.60, s * 0.30),
        QPointF(s * 0.73, s * 0.43), QPointF(s * 0.38, s * 0.78),
    ]))
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.18, s * 0.84), QPointF(s * 0.24, s * 0.66),
        QPointF(s * 0.37, s * 0.79),
    ]))
    p.setBrush(QColor("#ffffff"))
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.18, s * 0.84), QPointF(s * 0.21, s * 0.75),
        QPointF(s * 0.27, s * 0.81),
    ]))
    p.setPen(_pen("#ffffff", s * 0.055))
    p.drawLine(QPointF(s * 0.57, s * 0.34), QPointF(s * 0.69, s * 0.46))
    p.end()
    return QIcon(pm)


def arrow_down(color: str = "#ffffff", size: int = 64) -> QIcon:
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.105))
    p.drawLine(QPointF(s * 0.50, s * 0.22), QPointF(s * 0.50, s * 0.72))
    p.drawLine(QPointF(s * 0.27, s * 0.51), QPointF(s * 0.50, s * 0.74))
    p.drawLine(QPointF(s * 0.73, s * 0.51), QPointF(s * 0.50, s * 0.74))
    p.end()
    return QIcon(pm)


def download(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """An arrow settling onto a base line: save a copy of something."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.11))
    p.drawLine(QPointF(s * 0.50, s * 0.18), QPointF(s * 0.50, s * 0.58))
    p.drawLine(QPointF(s * 0.50, s * 0.62), QPointF(s * 0.32, s * 0.44))
    p.drawLine(QPointF(s * 0.50, s * 0.62), QPointF(s * 0.68, s * 0.44))
    p.drawLine(QPointF(s * 0.22, s * 0.80), QPointF(s * 0.78, s * 0.80))
    p.end()
    return QIcon(pm)


def refresh(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """A platform-independent circular recovery arrow."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.09))
    p.drawArc(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64),
              38 * 16, 286 * 16)
    p.drawLine(QPointF(s * 0.69, s * 0.17), QPointF(s * 0.82, s * 0.20))
    p.drawLine(QPointF(s * 0.81, s * 0.20), QPointF(s * 0.78, s * 0.34))
    p.end()
    return QIcon(pm)


def people(color: str = "#98a1b0", size: int = 64) -> QIcon:
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.09))
    p.drawEllipse(QPointF(s * 0.38, s * 0.36), s * 0.13, s * 0.13)
    p.drawArc(int(s * 0.18), int(s * 0.55), int(s * 0.40), int(s * 0.34),
              0, 180 * 16)
    p.drawEllipse(QPointF(s * 0.68, s * 0.40), s * 0.10, s * 0.10)
    p.drawArc(int(s * 0.54), int(s * 0.58), int(s * 0.32), int(s * 0.28),
              0, 180 * 16)
    p.end()
    return QIcon(pm)


def smiley(color: str = "#98a1b0", size: int = 64) -> QIcon:
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.085))
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.30, s * 0.30)
    p.drawArc(int(s * 0.34), int(s * 0.40), int(s * 0.32), int(s * 0.28),
              200 * 16, 140 * 16)
    p.setPen(_pen(color, s * 0.11))
    p.drawPoint(QPointF(s * 0.41, s * 0.42))
    p.drawPoint(QPointF(s * 0.59, s * 0.42))
    p.end()
    return QIcon(pm)


def bolt(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """A filled lightning bolt: the wake/nudge action. Filled shapes stay
    legible at small Windows icon sizes where thin strokes vanish."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF([
        QPointF(s * 0.56, s * 0.14), QPointF(s * 0.30, s * 0.54),
        QPointF(s * 0.47, s * 0.54), QPointF(s * 0.42, s * 0.86),
        QPointF(s * 0.70, s * 0.44), QPointF(s * 0.52, s * 0.44),
    ]))
    p.end()
    return QIcon(pm)


def power(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """The universal quit mark: an open ring with a stem. Drawn in code so
    it follows the accent color and the text-size setting like everything
    else in the rail."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.095))
    p.drawArc(QRectF(s * 0.22, s * 0.24, s * 0.56, s * 0.56),
              125 * 16, 290 * 16)
    p.drawLine(QPointF(s * 0.5, s * 0.14), QPointF(s * 0.5, s * 0.46))
    p.end()
    return QIcon(pm)


def gear(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """A modern thin-stroke settings gear: eight rounded teeth around a
    ring with an open hub. Drawn in code like every other icon, so it
    follows the accent color and stays crisp at any text scale."""
    import math
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.085))
    ring_r = s * 0.26
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), ring_r, ring_r)
    hub_r = s * 0.105
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), hub_r, hub_r)
    inner = s * 0.315
    outer = s * 0.435
    for tooth in range(8):
        angle = math.radians(tooth * 45.0)
        p.drawLine(
            QPointF(s * 0.5 + inner * math.cos(angle),
                    s * 0.5 + inner * math.sin(angle)),
            QPointF(s * 0.5 + outer * math.cos(angle),
                    s * 0.5 + outer * math.sin(angle)))
    p.end()
    return QIcon(pm)


def bell(color: str = "#98a1b0", size: int = 64, badge: str = "",
         badge_color: str = "#e5484d") -> QIcon:
    """The notification-center bell. An optional unseen count is drawn
    into the icon itself, so the button needs no overlay children and
    the badge scales with the text size like everything else."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.085))
    p.drawLine(QPointF(s * 0.50, s * 0.13), QPointF(s * 0.50, s * 0.19))
    p.drawArc(QRectF(s * 0.28, s * 0.19, s * 0.44, s * 0.46),
              0, 180 * 16)
    p.drawLine(QPointF(s * 0.28, s * 0.42), QPointF(s * 0.24, s * 0.60))
    p.drawLine(QPointF(s * 0.72, s * 0.42), QPointF(s * 0.76, s * 0.60))
    p.drawLine(QPointF(s * 0.17, s * 0.61), QPointF(s * 0.83, s * 0.61))
    p.drawArc(QRectF(s * 0.43, s * 0.66, s * 0.14, s * 0.13),
              180 * 16, 180 * 16)
    if badge:
        r = s * 0.195
        cx, cy = s * 0.74, s * 0.27
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(badge_color))
        p.drawEllipse(QPointF(cx, cy), r, r)
        f = QFont()
        f.setPixelSize(max(7, int(s * (0.30 if len(badge) < 2 else 0.24))))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(QColor("#ffffff")))
        p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r),
                   Qt.AlignCenter, badge[:3])
    p.end()
    return QIcon(pm)


def eye(color: str = "#98a1b0", size: int = 64) -> QIcon:
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.085))
    p.drawArc(int(s * 0.18), int(s * 0.30), int(s * 0.64), int(s * 0.40),
              200 * 16, 140 * 16)
    p.drawArc(int(s * 0.18), int(s * 0.30), int(s * 0.64), int(s * 0.40),
              20 * 16, 140 * 16)
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.10, s * 0.10)
    p.end()
    return QIcon(pm)


def eye_off(color: str = "#98a1b0", size: int = 64) -> QIcon:
    """Conventional hidden/visibility-off mark for the labelled control."""
    pm, p = _canvas(size)
    s = float(size)
    p.setPen(_pen(color, s * 0.075))
    p.drawArc(int(s * 0.18), int(s * 0.30), int(s * 0.64), int(s * 0.40),
              200 * 16, 140 * 16)
    p.drawArc(int(s * 0.18), int(s * 0.30), int(s * 0.64), int(s * 0.40),
              20 * 16, 140 * 16)
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.09, s * 0.09)
    p.setPen(_pen(color, s * 0.105))
    p.drawLine(QPointF(s * 0.20, s * 0.20), QPointF(s * 0.80, s * 0.80))
    p.end()
    return QIcon(pm)
