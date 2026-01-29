import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

import win32gui
import win32process
import win32api
import win32con

from pynput import keyboard, mouse


# -----------------------------
# App icon handling (dev + PyInstaller onefile)
# -----------------------------
def resource_path(rel: str) -> str:
    """
    Returns an absolute path to a resource file.
    Works in development and when bundled with PyInstaller (--onefile).
    """
    base = getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent))
    return str(Path(base) / rel)


APP_ICON_REL = "assets/waxy.ico"  # <-- Put your .ico here (relative to this .py)
APP_ICON_PATH = resource_path(APP_ICON_REL)


def set_windows_app_user_model_id(app_id: str):
    # Helps Windows show the correct taskbar icon/grouping.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# -----------------------------
# Window enumeration + filtering
# -----------------------------
BROWSER_EXES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
}


def _is_real_window(hwnd: int) -> bool:
    if not hwnd:
        return False
    if not win32gui.IsWindowVisible(hwnd):
        return False
    title = win32gui.GetWindowText(hwnd).strip()
    if not title:
        return False
    # Best-effort skip tool windows
    try:
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        WS_EX_TOOLWINDOW = 0x00000080
        if ex_style & WS_EX_TOOLWINDOW:
            return False
    except Exception:
        pass
    return True


def _get_exe_name_for_hwnd(hwnd: int) -> str:
    """Best-effort. Returns lowercase exe name (e.g. 'chrome.exe') or ''."""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        access = win32con.PROCESS_QUERY_LIMITED_INFORMATION
        try:
            hproc = win32api.OpenProcess(access, False, pid)
        except Exception:
            hproc = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
            )
        try:
            path = win32process.GetModuleFileNameEx(hproc, 0)
            if not path:
                return ""
            return path.split("\\")[-1].lower()
        finally:
            try:
                win32api.CloseHandle(hproc)
            except Exception:
                pass
    except Exception:
        return ""


def _window_allowed(hwnd: int, title: str) -> bool:
    tl = title.strip().lower()

    # Allow the actual game window by title match
    if "2004scape game" in tl:
        return True

    # Allow browser windows by exe name
    exe = _get_exe_name_for_hwnd(hwnd)
    if exe in BROWSER_EXES:
        return True

    return False


def list_target_windows():
    results = []

    def enum_cb(hwnd, _):
        if _is_real_window(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if _window_allowed(hwnd, title):
                results.append((int(hwnd), title))
        return True

    win32gui.EnumWindows(enum_cb, None)
    results.sort(key=lambda x: x[1].lower())
    return results


def get_foreground_window():
    return win32gui.GetForegroundWindow()


def hwnd_is_target_or_child(clicked_hwnd: int, target_hwnd: int) -> bool:
    """
    Check whether clicked_hwnd is the target window or belongs to it.
    Uses parent-walk and a more reliable root-ancestor check.
    """
    if not clicked_hwnd or not target_hwnd:
        return False
    if clicked_hwnd == target_hwnd:
        return True

    # Parent chain
    cur = clicked_hwnd
    while cur:
        try:
            cur = win32gui.GetParent(cur)
        except Exception:
            break
        if cur == target_hwnd:
            return True

    # Root ancestor check (helps with many modern windows/surfaces)
    try:
        GA_ROOT = 2
        clicked_root = win32gui.GetAncestor(clicked_hwnd, GA_ROOT)
        target_root = win32gui.GetAncestor(target_hwnd, GA_ROOT)
        if clicked_root and target_root and clicked_root == target_root:
            return True
    except Exception:
        pass

    return False


# -----------------------------
# Timer model
# -----------------------------
@dataclass
class TimerConfig:
    length_seconds: int = 90
    threshold_seconds: int = 15  # default for 90s
    count_below_zero: bool = True  # default ON


class ActivityTimer(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.config = TimerConfig()
        self.target_hwnd: int = 0
        self._last_activity_ts = time.time()
        self._lock = threading.Lock()

    def set_target_hwnd(self, hwnd: int):
        self.target_hwnd = int(hwnd) if hwnd else 0

    def reset_to_full(self):
        with self._lock:
            self._last_activity_ts = time.time()

    def note_activity(self):
        with self._lock:
            self._last_activity_ts = time.time()

    def remaining_seconds(self) -> float:
        with self._lock:
            elapsed = time.time() - self._last_activity_ts
        remaining = self.config.length_seconds - elapsed
        if not self.config.count_below_zero:
            remaining = max(0.0, remaining)
        return remaining


# -----------------------------
# Global hooks
# -----------------------------
class InputHookController(QtCore.QObject):
    """
    Rules:
      - Count keystrokes only if target window is foreground.
      - Count mouse clicks only if click lands inside target window or its child.
      - Ignore mouse move and wheel.
    """
    def __init__(self, timer: ActivityTimer):
        super().__init__()
        self.timer = timer
        self._stop_event = threading.Event()
        self._thread = None
        self._kb_listener = None
        self._mouse_listener = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        try:
            if self._kb_listener:
                self._kb_listener.stop()
        except Exception:
            pass
        try:
            if self._mouse_listener:
                self._mouse_listener.stop()
        except Exception:
            pass

        # Best effort join
        try:
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=1.0)
        except Exception:
            pass

        self._kb_listener = None
        self._mouse_listener = None
        self._thread = None

    def _run(self):
        def on_key_press(_key):
            target = self.timer.target_hwnd
            if not target:
                return
            if get_foreground_window() == target:
                self.timer.note_activity()

        def on_click(x, y, _button, pressed):
            if not pressed:
                return
            target = self.timer.target_hwnd
            if not target:
                return
            try:
                clicked_hwnd = win32gui.WindowFromPoint((int(x), int(y)))
            except Exception:
                return
            if hwnd_is_target_or_child(clicked_hwnd, target):
                self.timer.note_activity()

        self._kb_listener = keyboard.Listener(on_press=on_key_press)
        self._mouse_listener = mouse.Listener(on_click=on_click)

        self._kb_listener.start()
        self._mouse_listener.start()

        while not self._stop_event.is_set():
            time.sleep(0.1)


# -----------------------------
# Miniplayer overlay
# -----------------------------
class MiniOverlay(QtWidgets.QWidget):
    restore_requested = QtCore.Signal()

    def __init__(self, app_icon: QtGui.QIcon | None = None):
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.Tool |
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        if app_icon is not None and not app_icon.isNull():
            self.setWindowIcon(app_icon)

        self._drag_pos = None

        self.display = QtWidgets.QLCDNumber()
        self.display.setSegmentStyle(QtWidgets.QLCDNumber.Flat)
        self.display.setDigitCount(5)
        self.display.setStyleSheet("""
            QLCDNumber { background: transparent; color: #33ff66; }
        """)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.display, 1)

        grip_row = QtWidgets.QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self.grip = QtWidgets.QSizeGrip(self)
        grip_row.addWidget(self.grip, 0, QtCore.Qt.AlignBottom | QtCore.Qt.AlignRight)
        layout.addLayout(grip_row)

        self.setMinimumSize(140, 60)

    def set_time_text(self, text: str, danger: bool, digit_count: int):
        self.display.setDigitCount(digit_count)
        self.display.display(text)
        color = "#ff3b30" if danger else "#33ff66"
        self.display.setStyleSheet(f"QLCDNumber {{ background: transparent; color: {color}; }}")

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.RightButton:
            self.restore_requested.emit()
            event.accept()
            return
        if event.button() == QtCore.Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if self._drag_pos is not None and (event.buttons() & QtCore.Qt.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        self._drag_pos = None
        event.accept()


# -----------------------------
# Main UI
# -----------------------------
class MainWindow(QtWidgets.QMainWindow):
    ORG_NAME = "Waxy"
    APP_NAME = "Waxy Timer"

    def __init__(self, app_icon: QtGui.QIcon | None = None):
        super().__init__()

        QtCore.QCoreApplication.setOrganizationName(self.ORG_NAME)
        QtCore.QCoreApplication.setApplicationName(self.APP_NAME)
        self.settings = QtCore.QSettings(self.ORG_NAME, self.APP_NAME)

        self._app_icon = app_icon if app_icon is not None else QtGui.QIcon()
        if self._app_icon is not None and not self._app_icon.isNull():
            self.setWindowIcon(self._app_icon)

        self.timer_model = ActivityTimer()
        self.hooks = InputHookController(self.timer_model)
        self.hooks.start()

        self.overlay = MiniOverlay(app_icon=self._app_icon)
        self.overlay.restore_requested.connect(self._restore_from_miniplayer)

        self.setWindowTitle("waxy timer")
        self.resize(820, 360)
        self.setMinimumSize(720, 260)

        # Win98-ish style + readable inputs; toolbuttons sized in code (responsive)
        self.setStyleSheet("""
            QMainWindow { background: #c0c0c0; }

            QFrame#settingsPanel, QFrame#timerPanel {
                background: #dcdcdc;
                border: 2px solid #808080;
                border-radius: 8px;
            }

            QLabel { color: #000; font-family: Segoe UI; }

            QComboBox, QLineEdit {
                color: #000;
                background: #fff;
                border: 2px inset #a9a9a9;
                padding: 4px 6px;
                font-family: Segoe UI;
                min-height: 26px;
                selection-background-color: #0a246a;
                selection-color: #fff;
            }

            QComboBox QAbstractItemView {
                background: #fff;
                color: #000;
                selection-background-color: #0a246a;
                selection-color: #fff;
            }

            QToolButton {
                background: #e6e6e6;
                border: 2px outset #a9a9a9;
                padding: 0px;
                font-weight: 900;
            }
            QToolButton:pressed { border: 2px inset #a9a9a9; }
            QToolButton:focus { outline: none; }
        """)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)

        # -------- Left panel --------
        settings_panel = QtWidgets.QFrame()
        settings_panel.setObjectName("settingsPanel")
        settings_panel.setMinimumWidth(320)
        self.splitter.addWidget(settings_panel)

        s_layout = QtWidgets.QVBoxLayout(settings_panel)
        s_layout.setContentsMargins(12, 12, 12, 12)
        s_layout.setSpacing(10)

        s_layout.addWidget(QtWidgets.QLabel("Target window"))
        window_row = QtWidgets.QHBoxLayout()
        window_row.setSpacing(8)

        self.window_combo = QtWidgets.QComboBox()
        self.window_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.window_combo.setMinimumWidth(240)

        self.refresh_btn = QtWidgets.QToolButton()
        self.refresh_btn.setText("⟳")
        self.refresh_btn.setToolTip("Refresh window list")
        self.refresh_btn.clicked.connect(self._refresh_windows)

        window_row.addWidget(self.window_combo, 1)
        window_row.addWidget(self.refresh_btn, 0)
        s_layout.addLayout(window_row)

        s_layout.addWidget(QtWidgets.QLabel("Timer length"))
        self.length_combo = QtWidgets.QComboBox()
        self.length_combo.addItem("90 seconds", 90)
        self.length_combo.addItem("10 minutes", 600)
        self.length_combo.currentIndexChanged.connect(self._on_length_changed)
        s_layout.addWidget(self.length_combo)

        self.below_zero_chk = QtWidgets.QCheckBox("Count below zero")
        self.below_zero_chk.stateChanged.connect(self._on_below_zero_changed)
        s_layout.addWidget(self.below_zero_chk)

        s_layout.addWidget(QtWidgets.QLabel("Turn red at (seconds remaining)"))
        self.threshold_edit = QtWidgets.QLineEdit()
        self.threshold_edit.setPlaceholderText("e.g. 15")
        self.threshold_edit.editingFinished.connect(self._on_threshold_changed)
        s_layout.addWidget(self.threshold_edit)

        s_layout.addStretch(1)

        # ---- bottom row: miniplayer button on RIGHT ----
        bottom_row = QtWidgets.QHBoxLayout()
        bottom_row.addStretch(1)
        self.miniplayer_btn = QtWidgets.QToolButton()
        self.miniplayer_btn.setText("▣")
        self.miniplayer_btn.setToolTip("Miniplayer")
        self.miniplayer_btn.clicked.connect(self._enter_miniplayer)
        bottom_row.addWidget(self.miniplayer_btn, 0, QtCore.Qt.AlignRight)
        s_layout.addLayout(bottom_row)

        # -------- Right panel --------
        timer_panel = QtWidgets.QFrame()
        timer_panel.setObjectName("timerPanel")
        self.splitter.addWidget(timer_panel)

        t_layout = QtWidgets.QVBoxLayout(timer_panel)
        t_layout.setContentsMargins(12, 12, 12, 12)
        t_layout.setSpacing(12)

        self.timer_box = QtWidgets.QFrame()
        self.timer_box.setStyleSheet("""
            QFrame {
                background: #000;
                border: 2px inset #3a3a3a;
                border-radius: 10px;
            }
        """)
        # Right-click timer box opens miniplayer
        self.timer_box.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.timer_box.customContextMenuRequested.connect(lambda _pos: self._enter_miniplayer())

        box_layout = QtWidgets.QVBoxLayout(self.timer_box)
        box_layout.setContentsMargins(14, 14, 14, 14)

        self.timer_display = QtWidgets.QLCDNumber()
        self.timer_display.setSegmentStyle(QtWidgets.QLCDNumber.Flat)
        self.timer_display.setDigitCount(3)  # 90s mode default
        self.timer_display.setStyleSheet("QLCDNumber { background: transparent; color: #33ff66; }")
        self.timer_display.setMinimumHeight(140)
        box_layout.addWidget(self.timer_display, 1)

        t_layout.addWidget(self.timer_box, 1)

        # Keep sides proportional
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        # Populate targets then load saved settings (which may reselect)
        self._refresh_windows()
        self._load_persistent_state()

        self.timer_model.reset_to_full()

        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self._tick_ui)
        self.ui_timer.start(100)

        self._apply_responsive_sizes()
        self._tick_ui()

    # ---------------- Persistence ----------------
    def _load_persistent_state(self):
        # Geometry
        geo = self.settings.value("main/geometry", None)
        if isinstance(geo, QtCore.QByteArray):
            self.restoreGeometry(geo)

        # Splitter sizes
        s = self.settings.value("main/splitter", None)
        if isinstance(s, QtCore.QByteArray):
            self.splitter.restoreState(s)
        else:
            self.splitter.setSizes([360, 460])

        # Timer length
        length = self.settings.value("config/length_seconds", 90, int)
        idx = 0 if int(length) == 90 else 1
        self.length_combo.setCurrentIndex(idx)
        self.timer_model.config.length_seconds = int(self.length_combo.currentData())

        # Count below zero (DEFAULT TRUE if never saved)
        below = self.settings.value("config/count_below_zero", True)
        if isinstance(below, str):
            below = below.strip().lower() in ("1", "true", "yes", "on")
        self.below_zero_chk.setChecked(bool(below))
        self.timer_model.config.count_below_zero = bool(below)

        # Threshold (if never saved, use defaults by length)
        saved_thr = self.settings.value("config/threshold_seconds", None)
        if saved_thr is None:
            self._apply_default_threshold_for_length(self.timer_model.config.length_seconds)
        else:
            try:
                self.timer_model.config.threshold_seconds = max(0, int(saved_thr))
            except Exception:
                self._apply_default_threshold_for_length(self.timer_model.config.length_seconds)
        self.threshold_edit.setText(str(self.timer_model.config.threshold_seconds))

        # Preferred window hint
        self._preferred_window_hint = self.settings.value(
            "config/preferred_window_hint", "2004scape game"
        )
        if not isinstance(self._preferred_window_hint, str):
            self._preferred_window_hint = "2004scape game"
        self._preferred_window_hint = self._preferred_window_hint.lower()

        # Choose target (game first)
        self._select_best_default_target()

    def _save_persistent_state(self):
        self.settings.setValue("main/geometry", self.saveGeometry())
        self.settings.setValue("main/splitter", self.splitter.saveState())

        self.settings.setValue("config/length_seconds", int(self.timer_model.config.length_seconds))
        self.settings.setValue("config/count_below_zero", bool(self.timer_model.config.count_below_zero))
        self.settings.setValue("config/threshold_seconds", int(self.timer_model.config.threshold_seconds))

        # Save a hint so we can reselect on next launch
        cur_title = self.window_combo.currentText().strip().lower()
        if "2004scape game" in cur_title and not any(
            b in cur_title for b in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")
        ):
            self.settings.setValue("config/preferred_window_hint", "2004scape game")
        else:
            self.settings.setValue("config/preferred_window_hint", cur_title)

        self.settings.sync()

    # ---------------- Target window selection ----------------
    def _refresh_windows(self):
        current_hwnd = int(self.window_combo.currentData()) if self.window_combo.currentData() else 0

        self.window_combo.blockSignals(True)
        self.window_combo.clear()

        windows = list_target_windows()
        for hwnd, title in windows:
            # Show ONLY the window title (no hex handle)
            self.window_combo.addItem(title, int(hwnd))

        self.window_combo.blockSignals(False)

        try:
            self.window_combo.currentIndexChanged.disconnect()
        except Exception:
            pass
        self.window_combo.currentIndexChanged.connect(self._on_target_changed)

        # Keep current hwnd if still present
        if current_hwnd:
            for i in range(self.window_combo.count()):
                if int(self.window_combo.itemData(i)) == current_hwnd:
                    self.window_combo.setCurrentIndex(i)
                    self.timer_model.set_target_hwnd(current_hwnd)
                    return

        # Otherwise choose default
        self._select_best_default_target()

    def _select_best_default_target(self):
        if self.window_combo.count() == 0:
            self.timer_model.set_target_hwnd(0)
            return

        best_idx = None

        # Priority 1: actual "2004Scape Game" that is NOT obviously a browser window
        for i in range(self.window_combo.count()):
            title = self.window_combo.itemText(i).lower()
            if "2004scape game" in title and not any(
                b in title for b in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")
            ):
                best_idx = i
                break

        # Priority 2: user preference hint
        if best_idx is None and getattr(self, "_preferred_window_hint", ""):
            hint = self._preferred_window_hint
            for i in range(self.window_combo.count()):
                if hint and hint in self.window_combo.itemText(i).lower():
                    best_idx = i
                    break

        # Priority 3: any 2004scape-related window (including browser tab)
        if best_idx is None:
            for i in range(self.window_combo.count()):
                if "2004scape game" in self.window_combo.itemText(i).lower():
                    best_idx = i
                    break

        if best_idx is None:
            best_idx = 0

        self.window_combo.setCurrentIndex(best_idx)
        hwnd = int(self.window_combo.currentData()) if self.window_combo.currentData() else 0
        self.timer_model.set_target_hwnd(hwnd)

    def _on_target_changed(self, _idx):
        hwnd = int(self.window_combo.currentData()) if self.window_combo.currentData() else 0
        self.timer_model.set_target_hwnd(hwnd)
        self.timer_model.reset_to_full()

    # ---------------- Settings handlers ----------------
    def _apply_default_threshold_for_length(self, length_seconds: int):
        self.timer_model.config.threshold_seconds = 15 if int(length_seconds) == 90 else 60
        self.threshold_edit.setText(str(self.timer_model.config.threshold_seconds))

    def _on_length_changed(self, _idx):
        length = int(self.length_combo.currentData())
        self.timer_model.config.length_seconds = length
        self.timer_model.reset_to_full()
        # Default thresholds: 90s -> 15, 10m -> 60
        self._apply_default_threshold_for_length(length)

    def _on_below_zero_changed(self, _state):
        self.timer_model.config.count_below_zero = self.below_zero_chk.isChecked()

    def _on_threshold_changed(self):
        text = self.threshold_edit.text().strip()
        try:
            val = int(float(text))
            val = max(0, val)
            self.timer_model.config.threshold_seconds = val
            self.threshold_edit.setText(str(val))
        except Exception:
            self.threshold_edit.setText(str(self.timer_model.config.threshold_seconds))

    # ---------------- Miniplayer ----------------
    def _enter_miniplayer(self):
        # Size the miniplayer to match the timer box size
        size = self.timer_box.size()
        if size.width() > 0 and size.height() > 0:
            self.overlay.resize(size)

        # Position near top-right of main window
        main_geo = self.geometry()
        self.overlay.move(
            main_geo.x() + main_geo.width() - self.overlay.width() - 20,
            main_geo.y() + 40
        )

        self.overlay.show()
        self.hide()

    def _restore_from_miniplayer(self):
        self.overlay.hide()
        self.show()
        self.activateWindow()

    # ---------------- Responsive sizing ----------------
    def _apply_responsive_sizes(self):
        # Scale buttons based on left panel width
        try:
            left_w = self.splitter.sizes()[0]
        except Exception:
            left_w = 360

        # Button edge length + glyph size; clamp so it never gets absurd
        btn = max(28, min(52, int(left_w * 0.13)))
        fsz = max(16, min(34, int(btn * 0.62)))

        # Apply to toolbuttons
        for b in (self.refresh_btn, self.miniplayer_btn):
            b.setFixedSize(btn, btn)
            f = b.font()
            f.setPointSize(fsz)
            b.setFont(f)

        # Refresh symbol looks better slightly smaller than the miniplayer symbol
        f = self.refresh_btn.font()
        f.setPointSize(max(14, fsz - 3))
        self.refresh_btn.setFont(f)

    def resizeEvent(self, event: QtGui.QResizeEvent):
        super().resizeEvent(event)
        self._apply_responsive_sizes()

    # ---------------- Time formatting + draw ----------------
    def _format_time(self, seconds: float) -> tuple[str, int]:
        """
        Returns (text, digit_count).
          - length == 90: show seconds-only (90..0..-12)
          - else: show MM:SS
        """
        neg = seconds < 0
        s = int(seconds)
        abs_s = abs(s)

        if int(self.timer_model.config.length_seconds) == 90:
            out = str(abs_s)
            if neg:
                out = "-" + out
            return out, max(3, len(out))

        mm = abs_s // 60
        ss = abs_s % 60
        out = f"{mm:02d}:{ss:02d}"
        if neg:
            out = "-" + out
            return out, 6
        return out, 5

    def _tick_ui(self):
        remaining = float(self.timer_model.remaining_seconds())
        danger = remaining <= float(self.timer_model.config.threshold_seconds)

        text, digits = self._format_time(remaining)

        # Main display digit count
        if int(self.timer_model.config.length_seconds) == 90:
            self.timer_display.setDigitCount(max(3, digits))
        else:
            self.timer_display.setDigitCount(6 if text.startswith("-") else 5)

        self.timer_display.display(text)

        color = "#ff3b30" if danger else "#33ff66"
        self.timer_display.setStyleSheet(f"QLCDNumber {{ background: transparent; color: {color}; }}")

        # Overlay display if visible
        if self.overlay.isVisible():
            if int(self.timer_model.config.length_seconds) == 90:
                overlay_digits = max(3, digits)
            else:
                overlay_digits = 6 if text.startswith("-") else 5
            self.overlay.set_time_text(text, danger, overlay_digits)

    # ---------------- Close ----------------
    def closeEvent(self, event: QtGui.QCloseEvent):
        try:
            self._save_persistent_state()
        except Exception:
            pass

        try:
            self.hooks.stop()
        except Exception:
            pass
        try:
            self.overlay.close()
        except Exception:
            pass

        super().closeEvent(event)


def main():
    set_windows_app_user_model_id("Waxy.WaxyTimer")

    app = QtWidgets.QApplication(sys.argv)

    icon = QtGui.QIcon(APP_ICON_PATH) if APP_ICON_PATH else QtGui.QIcon()
    if icon and not icon.isNull():
        app.setWindowIcon(icon)

    w = MainWindow(app_icon=icon)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
