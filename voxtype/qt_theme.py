"""Compact dark theme QSS — lifted from telecode/tray/qt_theme.py so the
look matches across both apps."""
from __future__ import annotations

BG          = "#0c0f14"
BG_ELEV     = "#10141c"
BG_CARD     = "#151a24"
BG_ROW      = "#1a2030"
BG_HOVER    = "#1d2334"
FG          = "#e6ebf2"
FG_DIM      = "#8a96aa"
FG_MUTE     = "#4f5a70"
ACCENT      = "#6ba4ff"
ACCENT_2    = "#56e0c2"
WARN        = "#f5a524"
ERR         = "#ff6e6e"
OK          = "#56e0c2"
BORDER      = "#1e2636"
BORDER_SOFT = "#171d28"


QSS = f"""
* {{
    color: {FG};
    font-family: "Inter", "Segoe UI Variable", "Segoe UI", -apple-system, sans-serif;
    font-size: 13px;
}}
QWidget#window_root {{ background: {BG}; }}

QMenu {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px;
    font-size: 12.5px;
}}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 4px; margin: 1px 2px; }}
QMenu::item:selected {{ background: {BG_ROW}; }}
QMenu::item:disabled {{ color: {FG_MUTE}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 6px; }}

QListWidget#sidebar {{
    background: {BG_ELEV};
    border: none;
    border-right: 1px solid {BORDER_SOFT};
    outline: 0;
    padding: 10px 6px;
}}
QListWidget#sidebar::item {{
    padding: 9px 14px; border-radius: 6px; color: {FG_DIM};
    margin-bottom: 2px; min-height: 22px;
}}
QListWidget#sidebar::item:hover {{ background: {BG_HOVER}; color: {FG}; }}
QListWidget#sidebar::item:selected {{
    background: {BG_CARD}; color: {FG};
    border-left: 2px solid {ACCENT}; font-weight: 500;
}}

QWidget#titlebar {{ background: {BG}; border-bottom: 1px solid {BORDER_SOFT}; }}
QLabel#titlebar_title {{ font-weight: 600; font-size: 13px; letter-spacing: 0.02em; }}
QLabel#titlebar_icon {{ font-size: 14px; }}
QPushButton.tb_btn {{
    background: transparent; border: none; color: {FG_DIM};
    padding: 0 16px; font-size: 13px; min-height: 34px; max-height: 34px;
}}
QPushButton.tb_btn:hover {{ background: {BG_HOVER}; color: {FG}; }}
QPushButton.tb_close:hover {{ background: #e81123; color: white; }}

QScrollArea {{ background: {BG}; border: none; }}
QWidget#content {{ background: {BG}; }}

QFrame.card {{ background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel.card_title {{ font-size: 13px; font-weight: 600; }}
QLabel.card_sub {{ font-size: 11px; color: {FG_DIM}; }}
QLabel.section_header {{
    font-size: 10px; font-weight: 600; color: {FG_MUTE};
    text-transform: uppercase; letter-spacing: 0.08em; padding-top: 2px;
}}
QLabel.row_label {{ font-size: 12px; }}
QLabel.row_help  {{ color: {FG_MUTE}; font-size: 10.5px; }}
QLabel.key_path  {{ color: {FG_MUTE}; font-family: Consolas, monospace; font-size: 10px; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: #0d1118; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 4px 8px; selection-background-color: {ACCENT};
    selection-color: {BG}; min-height: 20px;
}}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}

QComboBox {{
    background: #0d1118; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 4px 22px 4px 8px; min-height: 20px;
}}
QComboBox:hover, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 4px;
    selection-background-color: {BG_ROW}; outline: 0; padding: 2px;
}}

QPushButton {{
    background: {BG_ROW}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 4px 10px; font-size: 11.5px; min-height: 22px;
}}
QPushButton:hover {{ border: 1px solid {ACCENT}; background: {BG_HOVER}; }}
QPushButton:disabled {{ color: {FG_MUTE}; background: {BG_CARD}; }}
QPushButton.primary {{
    background: {ACCENT}; color: {BG}; border: 1px solid {ACCENT}; font-weight: 600;
}}
QPushButton.primary:hover {{ background: #7db0ff; }}
QPushButton.danger {{ color: {ERR}; }}
QPushButton.danger:hover {{ background: rgba(255, 110, 110, 25); border: 1px solid {ERR}; }}
QPushButton.ghost {{
    background: transparent; border: 1px solid transparent; color: {FG_DIM};
}}
QPushButton.ghost:hover {{ color: {FG}; border: 1px solid {BORDER}; }}

QLabel.stat_pill {{
    background: {BG_CARD}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 3px 8px; font-size: 11px; color: {FG_DIM};
}}
QLabel.stat_pill_ok  {{ color: {OK};  border-color: rgba(86, 224, 194, 80); }}
QLabel.stat_pill_err {{ color: {ERR}; border-color: rgba(255, 110, 110, 80); }}
QLabel.toggle_label  {{ font-size: 12px; }}

/* Scrollbars — visible on dark backgrounds (matches telecode tray). */
QScrollBar:vertical {{
    background: {BG_ELEV}; width: 12px; margin: 2px 0; border: none;
}}
QScrollBar::handle:vertical {{
    background: #3a4563; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: #5b6a92; }}
QScrollBar:horizontal {{
    background: {BG_ELEV}; height: 12px; margin: 0 2px; border: none;
}}
QScrollBar::handle:horizontal {{
    background: #3a4563; border-radius: 5px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: #5b6a92; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0; height: 0; background: transparent;
}}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {BG_CARD}; border: 1px solid {BORDER}; color: {FG};
    padding: 4px 8px; font-size: 11px;
}}
"""
