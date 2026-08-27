"""Dark theme with selectable accent color and scalable typography.
apply() rebuilds the palette and stylesheet from the chosen accent and
font scale. Widgets read colors and sizes through this module at build
time, so re-applying plus re-rendering picks everything up live."""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#0e1013"
PANEL = "#14161b"
PANEL2 = "#1a1d24"
BORDER = "#252a34"
TEXT = "#e7e9ee"
MUTED = "#98a1b0"
FAIL = "#e5484d"
OK = "#3fb950"
WARN = "#d9a441"
BUBBLE_THEM = "#20242c"

# The tint suite: name -> (accent, pressed, my-bubble). Ordered as a
# color wheel so the Settings swatch grid reads naturally. The original
# seven names keep their exact values, so an existing config.json lands
# on the same look after an upgrade.
ACCENTS = {
    "Blue":     ("#4f8cff", "#3a6fd6", "#2f5fd0"),
    "Ocean":    ("#3ab5e6", "#2d92ba", "#1a6b8c"),
    "Indigo":   ("#7688f7", "#5f6dd0", "#4d59bb"),
    "Violet":   ("#8b7cf6", "#6f61d8", "#5b4fc7"),
    "Orchid":   ("#c678dd", "#a25db6", "#87499c"),
    "Rose":     ("#ec6a9c", "#c9527f", "#ad3f69"),
    "Crimson":  ("#e25563", "#ba4551", "#9d3844"),
    "Coral":    ("#ff7f6e", "#d46557", "#b35247"),
    "Sunset":   ("#f08b4b", "#c96f39", "#a85a2c"),
    "Amber":    ("#e3a53c", "#bd8630", "#9c6f28"),
    "Gold":     ("#d3b13f", "#ac9033", "#77621f"),
    "Sage":     ("#a8bd77", "#879d5c", "#5c6f3c"),
    "Green":    ("#4cc077", "#3a9d5f", "#2e8150"),
    "Mint":     ("#46d19a", "#37ab7e", "#23795a"),
    "Teal":     ("#35c2b0", "#2a9c8d", "#1f8175"),
    "Graphite": ("#93a1b8", "#75839a", "#49536a"),
}

FONT_SIZES = {"Default": 1.0, "Large": 1.15, "Larger": 1.3, "Largest": 1.5}

# Responsive bubbles: the fraction of the conversation pane a bubble may
# use, and a readability ceiling so lines never grow unreadably long on a
# very wide window. The ceiling scales with the text size.
BUBBLE_PANE_FRAC = 0.72
BUBBLE_MAX_BASE_PX = 680

ACCENT, ACCENT_DOWN, BUBBLE_ME = ACCENTS["Blue"]
_SCALE = 1.0

# accent-derived surfaces, recomputed in apply()
SEL_BG = "#232a38"
HOVER_BG = "#1a1e26"
ACCENT_BORDER = "#3a4c6e"
SCROLL_HOVER = "#414a5a"
SCROLL_BASE = "#333a47"
ACCENT_LINE = "#4a5f8f"
BUBBLE_EDGE = "#2a3242"
BUBBLE_ME_GRAD = "#2f5fd0"


def _blend(a: str, b: str, t: float) -> str:
    """Mix color a into color b at weight t (0..1)."""
    av = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    bv = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * t + y * (1 - t)):02x}"
                         for x, y in zip(av, bv))


_SWATCH_CACHE: dict = {}


def swatch_pixmap(name: str, size: int):
    """A rounded color patch for the tint picker: the accent fading into
    its bubble shade, so one glance shows the whole pair. Cached per
    (name, size); safe to call on every dialog build."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import (QLinearGradient, QPainter, QPainterPath,
                               QPen, QPixmap)
    key = (name, int(size))
    cached = _SWATCH_CACHE.get(key)
    if cached is not None:
        return cached
    accent, _down, bubble = ACCENTS.get(name, ACCENTS["Blue"])
    s = int(size)
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(1.0, 1.0, s - 2.0, s - 2.0)
    radius = max(4.0, s * 0.30)
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    grad.setColorAt(0.0, QColor(accent))
    grad.setColorAt(1.0, QColor(bubble))
    p.fillPath(path, grad)
    p.setPen(QPen(QColor(_blend("#ffffff", accent, 0.30)), 1.0))
    p.drawPath(path)
    p.end()
    _SWATCH_CACHE[key] = pm
    return pm


def scale() -> float:
    return _SCALE


def fs(base: float) -> str:
    """Scaled font size for stylesheets."""
    return f"{base * _SCALE:.1f}pt"


def dim(base: int) -> int:
    """Scaled pixel dimension."""
    return int(round(base * _SCALE))


def responsive_bubble_limit(viewport_px: int) -> int:
    """Width a bubble may use for a given conversation-pane width.

    Pure so it is unit-testable: a fixed fraction of the pane, never less
    than a usable floor, and capped at a text-scaled readability ceiling.
    Bubbles therefore grow and shrink with the window on their own; there
    is no width setting to manage.
    """
    avail = max(300, int(viewport_px) - 36)
    ceiling = dim(BUBBLE_MAX_BASE_PX)
    return max(240, min(int(avail * BUBBLE_PANE_FRAC), ceiling))


def _qss() -> str:
    # Every radius, padding, and track below goes through dim(), so the
    # chrome keeps its proportions at Large, Larger, and Largest instead
    # of shrinking relative to the text it frames.
    bar = max(8, dim(9))
    bar_r = bar // 2
    return f"""
* {{ font-family: 'Segoe UI Variable', 'Segoe UI', system-ui, sans-serif; }}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-size: {fs(10.5)}; }}
QLabel {{ background: transparent; }}
QSplitter::handle {{ background: {BORDER}; width: 1px; }}
QSplitter::handle:hover {{ background: {ACCENT_BORDER}; }}

QLineEdit, QPlainTextEdit, QComboBox {{
  background: {PANEL2}; border: 1px solid {BORDER};
  border-radius: {dim(9)}px; padding: {dim(7)}px {dim(10)}px;
  selection-background-color: {ACCENT_DOWN};
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 2px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: {dim(24)}px; }}
QComboBox QAbstractItemView {{ background: {PANEL2}; border: 1px solid {BORDER};
  selection-background-color: {ACCENT_DOWN}; }}

QPushButton {{
  background: {PANEL2}; border: 1px solid {BORDER};
  border-radius: {dim(9)}px; padding: {dim(6)}px {dim(14)}px;
}}
QPushButton:hover {{ background: {HOVER_BG}; border-color: {ACCENT_BORDER}; }}
QPushButton:pressed {{ background: {SEL_BG}; }}
QPushButton#accent {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#accent:hover {{ background: {ACCENT_DOWN}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {MUTED};
  padding: {dim(4)}px {dim(8)}px; font-size: {fs(13)}; }}
QPushButton#ghost:hover {{ color: {ACCENT}; background: {HOVER_BG}; }}
QPushButton#ghost:focus {{ color: {ACCENT}; background: {HOVER_BG};
  border: 1px solid {ACCENT}; }}

QListView {{
  background: {PANEL}; border: none; outline: none; padding: {dim(4)}px;
}}
QListView::item {{ border: none; }}

/* Plain list widgets (device picker, simple choosers) need visible
   selection; the chat list is unaffected because its delegate paints
   every pixel itself. */
QListWidget#plainPicker::item {{ padding: {dim(5)}px; }}
QListWidget#plainPicker::item:selected {{ background: {ACCENT_DOWN};
  color: white; border-radius: {dim(6)}px; }}
QListWidget#plainPicker::item:hover {{ background: {HOVER_BG};
  border-radius: {dim(6)}px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: {bar}px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {SCROLL_BASE};
  border-radius: {bar_r}px; min-height: {dim(30)}px; }}
QScrollBar::handle:vertical:hover {{ background: {SCROLL_HOVER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {bar}px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {SCROLL_BASE};
  border-radius: {bar_r}px; min-width: {dim(30)}px; }}
QScrollBar::handle:horizontal:hover {{ background: {SCROLL_HOVER}; }}

QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER};
  padding: {dim(5)}px; }}
QMenu {{ background: {PANEL2}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: {dim(6)}px {dim(18)}px; }}
QMenu::item:selected {{ background: {ACCENT_DOWN}; }}
"""


def apply(app: QApplication, accent: str = "Blue", font_scale: float = 1.0):
    global ACCENT, ACCENT_DOWN, BUBBLE_ME, _SCALE
    global SEL_BG, HOVER_BG, ACCENT_BORDER, SCROLL_HOVER, SCROLL_BASE
    global ACCENT_LINE, BUBBLE_EDGE, BUBBLE_ME_GRAD
    ACCENT, ACCENT_DOWN, BUBBLE_ME = ACCENTS.get(accent, ACCENTS["Blue"])
    SEL_BG = _blend(ACCENT, PANEL, 0.20)
    HOVER_BG = _blend(ACCENT, PANEL, 0.07)
    ACCENT_BORDER = _blend(ACCENT, BORDER, 0.60)
    SCROLL_HOVER = _blend(ACCENT, "#414a5a", 0.55)
    SCROLL_BASE = _blend(ACCENT, "#333a47", 0.15)
    ACCENT_LINE = _blend(ACCENT, BORDER, 0.80)
    BUBBLE_EDGE = _blend(ACCENT, BORDER, 0.30)
    BUBBLE_ME_GRAD = (
        "qlineargradient(x1:0, y1:0, x2:0, y2:1, "
        f"stop:0 {_blend('#ffffff', BUBBLE_ME, 0.10)}, "
        f"stop:1 {BUBBLE_ME})")
    try:
        _SCALE = max(0.8, min(float(font_scale or 1.0), 2.0))
    except (TypeError, ValueError):
        _SCALE = 1.0
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.Base, QColor(PANEL2))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(PANEL2))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT_DOWN))
    pal.setColor(QPalette.HighlightedText, QColor("white"))
    pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
    app.setPalette(pal)
    app.setStyleSheet(_qss())
