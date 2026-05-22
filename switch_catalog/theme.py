from __future__ import annotations

import shutil
from pathlib import Path


DRACULA_STYLESHEET = """
QWidget {
    background: #282a36;
    color: #f8f8f2;
    font-size: 13px;
}
QMainWindow, QDialog, QMessageBox {
    background: #282a36;
}
QTabWidget::pane {
    border: 1px solid #44475a;
}
QTabBar::tab {
    background: #21222c;
    color: #f8f8f2;
    border: 1px solid #44475a;
    padding: 8px 14px;
    min-width: 96px;
}
QTabBar::tab:selected {
    background: #0078ff;
    color: #f8f8f2;
}
QLineEdit, QTextEdit, QComboBox, QListWidget {
    background: #21222c;
    border: 1px solid #44475a;
    border-radius: 6px;
    color: #f8f8f2;
    selection-background-color: #6272a4;
    selection-color: #f8f8f2;
}
QLineEdit, QComboBox {
    padding: 7px;
}
QTextEdit {
    padding: 8px;
}
QListWidget::item {
    border-radius: 4px;
    padding: 6px;
}
QListWidget::item:selected {
    background: #6272a4;
    color: #f8f8f2;
}
QListWidget::item:selected:!active {
    background: #6272a4;
    color: #f8f8f2;
}
QListWidget::item:hover {
    background: #44475a;
    color: #f8f8f2;
}
QPushButton {
    background: #0078ff;
    border: 0;
    border-radius: 6px;
    color: #f8f8f2;
    font-weight: 700;
    padding: 8px 12px;
}
QPushButton:hover {
    background: #3394ff;
}
QPushButton:pressed {
    background: #005ec7;
}
QPushButton#installButton {
    background: #ff5555;
    color: #f8f8f2;
}
QPushButton#installButton:hover {
    background: #ff6e6e;
}
QPushButton#installButton:pressed {
    background: #d63f3f;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
}
QCheckBox::indicator:unchecked {
    background: #21222c;
    border: 1px solid #6272a4;
    border-radius: 4px;
}
QCheckBox::indicator:checked {
    background: #50fa7b;
    border: 1px solid #50fa7b;
    border-radius: 4px;
}
QSplitter::handle {
    background: #44475a;
}
QProgressBar {
    background: #21222c;
    border: 1px solid #44475a;
    border-radius: 6px;
    color: #f8f8f2;
    text-align: center;
}
QProgressBar::chunk {
    background: #50fa7b;
    border-radius: 6px;
}
QMenu {
    background: #21222c;
    color: #f8f8f2;
    border: 1px solid #6272a4;
    padding: 4px;
}
QMenu::item {
    padding: 8px 24px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #0078ff;
    color: #f8f8f2;
}
QMenu::item:disabled {
    color: #6272a4;
}
"""


# A true-black variant for OLED displays, derived from the Dracula sheet so the two
# stay structurally in sync. Only the background tones are swapped to near/true black;
# text, accent, danger and success colors are kept.
OLED_DARK_STYLESHEET = (
    DRACULA_STYLESHEET
    .replace("#282a36", "#000000")  # primary background -> true black
    .replace("#21222c", "#0a0a0a")  # secondary background -> near black
    .replace("#44475a", "#1c1c1c")  # borders / list hover -> dark grey
)


DEFAULT_THEME = "Dracula"
BUILTIN_THEMES: dict[str, str] = {
    "Dracula": DRACULA_STYLESHEET,
    "OLED Dark": OLED_DARK_STYLESHEET,
}


def available_themes() -> list[str]:
    """Built-in theme names followed by any installed ``*.qss`` themes (by stem)."""
    from .paths import THEMES_DIR

    names = list(BUILTIN_THEMES.keys())
    try:
        installed = sorted(p.stem for p in THEMES_DIR.glob("*.qss"))
    except OSError:
        installed = []
    for name in installed:
        if name not in names:
            names.append(name)
    return names


def resolve_stylesheet(name: str) -> str:
    """Return the stylesheet for ``name``, falling back to the default theme."""
    if name in BUILTIN_THEMES:
        return BUILTIN_THEMES[name]
    from .paths import THEMES_DIR

    try:
        return (THEMES_DIR / f"{name}.qss").read_text(encoding="utf-8")
    except OSError:
        return BUILTIN_THEMES[DEFAULT_THEME]


def install_theme(src_path: str | Path) -> str:
    """Copy a ``.qss`` file into the themes folder and return the theme name (stem)."""
    from .paths import THEMES_DIR, ensure_app_dirs

    source = Path(src_path)
    if source.suffix.lower() != ".qss":
        raise ValueError("A theme must be a Qt stylesheet (.qss) file.")
    if not source.is_file():
        raise FileNotFoundError(f"Theme file not found: {source}")
    ensure_app_dirs()
    destination = THEMES_DIR / source.name
    shutil.copy2(source, destination)
    return destination.stem
