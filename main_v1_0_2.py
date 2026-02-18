# Preserved historical version of main.py (v1.0.2); not used by the app at runtime.
import sys
import time
import math
import threading
import shutil
import os
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtMultimedia import QSoundEffect

import win32gui
import win32process
import win32api
import win32con

from pynput import keyboard, mouse


# -----------------------------
# Resources (dev + PyInstaller onefile)
# -----------------------------
def resource_path(rel: str) -> str:
    base = getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent))
    return str(Path(base) / rel)


APP_ICON_REL = "assets/waxy.ico"
APP_ICON_PATH = resource_path(APP_ICON_REL)

SOUNDS_DIR_REL = "assets/sounds"
SOUNDS_DIR_PATH = Path(resource_path(SOUNDS_DIR_REL))

DEFAULT_SOUND_FILE = "town_crier_ring_bell_down.wav"
DEFAULT_SOUND_THRESHOLD = 15
DEFAULT_VOLUME = 0.50  # QSoundEffect: 0..1


def set_windows_app_user_model_id(app_id: str):
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
    try:
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        WS_EX_TOOLWINDOW = 0x00000080
        if ex_style & WS_EX_TOOLWINDOW:
            return False
    except Exception:
        pass
    return True


def _get_exe_name_for_hwnd(hwnd: int) -> str:
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
    if "2004scape game" in tl:
        return True
    exe = _get_exe_name_for_hwnd(hwnd)
    return exe in BROWSER_EXES


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
    if not clicked_hwnd or not target_hwnd:
        return False
    if clicked_hwnd == target_hwnd:
        return True

    cur = clicked_hwnd
    while cur:
        try:
            cur = win32gui.GetParent(cur)
        except Exception:
            break
        if cur == target_hwnd:
            return True

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
    threshold_seconds: int = 15
    count_below_zero: bool = True

    sound_threshold_seconds: int = DEFAULT_SOUND_THRESHOLD
    sound_file_rel: str = ""
    volume: float = DEFAULT_VOLUME  # 0..1


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
# - Manual edge/corner resize (stable)
# - Subtle dotted triangle grip (painted, not a widget)
# -----------------------------
class MiniOverlay(QtWidgets.QWidget):
    restore_requested = QtCore.Signal()
    geometry_changed = QtCore.Signal(QtCore.QRect)

    GRIP = 14  # px dotted triangle area in bottom-right

    def __init__(self, app_icon: QtGui.QIcon | None = None):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        if app_icon is not None and not app_icon.isNull():
            self.setWindowIcon(app_icon)

        # allow smaller than main UI
        self.setMinimumSize(140, 80)

        self._press_global = QtCore.QPoint()
        self._start_geo = QtCore.QRect()
        self._dragging = False
        self._resizing = False

        self.display = QtWidgets.QLCDNumber()
        self.display.setSegmentStyle(QtWidgets.QLCDNumber.Flat)
        self.display.setDigitCount(5)
        self.display.setStyleSheet("QLCDNumber { background: transparent; color: #33ff66; }")
        # make right-click / drag easy anywhere, even on digits
        self.display.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.display, 1)

    def set_time_text(self, text: str, danger: bool, digit_count: int):
        self.display.setDigitCount(digit_count)
        self.display.display(text)
        color = "#ff3b30" if danger else "#33ff66"
        self.display.setStyleSheet(f"QLCDNumber {{ background: transparent; color: {color}; }}")

    def _in_grip(self, pos: QtCore.QPoint) -> bool:
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        g = self.GRIP
        return (x >= w - g) and (y >= h - g)

    def paintEvent(self, event: QtGui.QPaintEvent):
        super().paintEvent(event)

        # subtle dotted triangle in bottom-right
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        # dots: light gray with some transparency
        brush = QtGui.QBrush(QtGui.QColor(220, 220, 220, 160))
        p.setBrush(brush)
        p.setPen(QtCore.Qt.NoPen)

        g = self.GRIP
        start_x = self.width() - g + 3
        start_y = self.height() - g + 3
        step = 4
        r = 1.2

        # draw dots in a right triangle pattern
        # rows from top of grip area to bottom
        for row in range(0, g // step):
            # dots per row increases towards bottom-right
            count = row + 1
            for col in range(count):
                x = start_x + (g - step * (row + 1)) + col * step
                y = start_y + row * step
                p.drawEllipse(QtCore.QPointF(x, y), r, r)

        p.end()

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.RightButton:
            self.restore_requested.emit()
            event.accept()
            return

        if event.button() == QtCore.Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._start_geo = self.geometry()
            if self._in_grip(event.position().toPoint()):
                self._resizing = True
                self.setCursor(QtCore.Qt.SizeFDiagCursor)
            else:
                self._dragging = True
                self.setCursor(QtCore.Qt.ArrowCursor)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        if self._resizing or self._dragging:
            delta = event.globalPosition().toPoint() - self._press_global
            g = QtCore.QRect(self._start_geo)

            minw = self.minimumWidth()
            minh = self.minimumHeight()

            if self._dragging:
                g.moveTopLeft(g.topLeft() + delta)
            else:
                new_w = max(minw, g.width() + delta.x())
                new_h = max(minh, g.height() + delta.y())
                g.setSize(QtCore.QSize(new_w, new_h))

            self.setGeometry(g)
            event.accept()
            return

        if self._in_grip(event.position().toPoint()):
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        else:
            self.setCursor(QtCore.Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        self._dragging = False
        self._resizing = False
        self.setCursor(QtCore.Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def moveEvent(self, event: QtGui.QMoveEvent):
        super().moveEvent(event)
        self.geometry_changed.emit(self.geometry())

    def resizeEvent(self, event: QtGui.QResizeEvent):
        super().resizeEvent(event)
        self.geometry_changed.emit(self.geometry())


class ClickableSlider(QtWidgets.QSlider):
    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.LeftButton:
            opt = QtWidgets.QStyleOptionSlider()
            self.initStyleOption(opt)
            handle = self.style().subControlRect(
                QtWidgets.QStyle.CC_Slider, opt, QtWidgets.QStyle.SC_SliderHandle, self
            )
            if handle.contains(event.position().toPoint()):
                super().mousePressEvent(event)
                return

            pos = event.position().toPoint()
            if self.orientation() == QtCore.Qt.Horizontal:
                groove = self.rect()
                x = max(0, min(groove.width() - 1, pos.x()))
                ratio = x / max(1, groove.width() - 1)
                value = self.minimum() + int(round(ratio * (self.maximum() - self.minimum())))
                self.setValue(value)
            else:
                groove = self.rect()
                y = max(0, min(groove.height() - 1, pos.y()))
                ratio = 1.0 - (y / max(1, groove.height() - 1))
                value = self.minimum() + int(round(ratio * (self.maximum() - self.minimum())))
                self.setValue(value)
            event.accept()
            return
        super().mousePressEvent(event)


# -----------------------------
# Main UI
# -----------------------------
class MainWindow(QtWidgets.QMainWindow):
    ORG_NAME = "Waxy"
    APP_NAME = "Waxy Timer"

    UI_MAX_W = 900
    UI_MAX_H = 420

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
        self.overlay.geometry_changed.connect(self._on_miniplayer_geometry_changed)
        self._mini_last_geo: QtCore.QRect | None = None

        self._sound = QSoundEffect(self)
        self._sound.setLoopCount(1)
        self._sound_fired = False
        self._last_remaining = None

        self._suppress_miniplayer_until = 0.0

        self.setWindowTitle("waxy timer")

        # shorter + polished
        self.resize(700, 250)
        self.setMinimumSize(640, 250)

        self.setStyleSheet(
            """
            QMainWindow { background: #c0c0c0; }

            QFrame#settingsPanel, QFrame#timerPanel {
                background: #dcdcdc;
                border: 2px solid #808080;
                border-radius: 8px;
            }

            QSplitter::handle { background: #c0c0c0; }
            QSplitter::handle:horizontal { width: 12px; }

            QLabel { color: #000; font-family: Segoe UI; }
            QCheckBox { font-family: Segoe UI; }

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
                font-weight: 400;
                font-size: 16px;
            }
            QPushButton {
                background: #e6e6e6;
                border: 2px outset #a9a9a9;
                font-weight: 400;
            }
            QToolButton:pressed, QPushButton:pressed { border: 2px inset #a9a9a9; }
            QToolButton:focus, QPushButton:focus { outline: none; }

            QSlider::groove:horizontal {
                border: 2px inset #a9a9a9;
                height: 8px;
                background: #fff;
                margin: 0px;
            }
            QSlider::handle:horizontal {
                background: #b0b0b0;
                border: 2px outset #a9a9a9;
                width: 14px;
                margin: -6px 0;
            }
            QSlider::handle:horizontal:pressed { border: 2px inset #a9a9a9; }
            """
        )

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(12)
        root.addWidget(self.splitter, 1)

        # -------- Left panel --------
        settings_panel = QtWidgets.QFrame()
        settings_panel.setObjectName("settingsPanel")
        settings_panel.setMinimumWidth(300)
        self.splitter.addWidget(settings_panel)

        grid = QtWidgets.QGridLayout(settings_panel)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)  # tighter overall
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 2)
        grid.setColumnStretch(4, 0)
        grid.setColumnStretch(5, 0)

        row = 0

        # Window combo + refresh
        ctrl_h = 36
        self.window_combo = QtWidgets.QComboBox()
        self.window_combo.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.window_combo.setMinimumHeight(ctrl_h)

        self.refresh_btn = QtWidgets.QToolButton()
        self.refresh_btn.setText("\u21bb")
        self.refresh_btn.setToolTip("Refresh window list")
        self.refresh_btn.clicked.connect(self._refresh_windows)
        self.refresh_btn.setFixedSize(ctrl_h, ctrl_h)

        grid.addWidget(self.window_combo, row, 0, 1, 5)
        grid.addWidget(self.refresh_btn, row, 5, 1, 1)
        row += 1

        # Length + checkbox
        self.length_combo = QtWidgets.QComboBox()
        self.length_combo.addItem("90 seconds", 90)
        self.length_combo.addItem("10 minutes", 600)
        self.length_combo.currentIndexChanged.connect(self._on_length_changed)
        self.length_combo.setMinimumHeight(ctrl_h)
        self.below_zero_chk = QtWidgets.QCheckBox("Count below zero")
        self.below_zero_chk.stateChanged.connect(self._on_below_zero_changed)
        self.below_zero_chk.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        grid.addWidget(self.length_combo, row, 0, 1, 3)
        grid.addWidget(self.below_zero_chk, row, 3, 1, 3)
        row += 1

        # Threshold block spans FULL width -> removes that awkward empty band
        thr_block = QtWidgets.QGridLayout()
        thr_block.setHorizontalSpacing(14)
        thr_block.setVerticalSpacing(0)
        thr_block.setContentsMargins(0, 0, 0, 0)

        lbl_red = QtWidgets.QLabel("Turn red at")
        lbl_red.setAlignment(QtCore.Qt.AlignHCenter)
        lbl_red.setStyleSheet("QLabel { font-size: 11px; }")

        lbl_sound = QtWidgets.QLabel("Play sound at")
        lbl_sound.setAlignment(QtCore.Qt.AlignHCenter)
        lbl_sound.setStyleSheet("QLabel { font-size: 11px; }")

        self.threshold_edit = QtWidgets.QLineEdit()
        self.threshold_edit.setAlignment(QtCore.Qt.AlignHCenter)
        self.threshold_edit.setFixedHeight(26)
        self.threshold_edit.setFixedWidth(90)
        self.threshold_edit.editingFinished.connect(self._on_threshold_changed)

        self.sound_threshold_edit = QtWidgets.QLineEdit()
        self.sound_threshold_edit.setAlignment(QtCore.Qt.AlignHCenter)
        self.sound_threshold_edit.setFixedHeight(26)
        self.sound_threshold_edit.setFixedWidth(90)
        self.sound_threshold_edit.editingFinished.connect(self._on_sound_threshold_changed)

        # center the pair using stretches
        thr_block.addItem(QtWidgets.QSpacerItem(1, 1, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum), 0, 0, 2, 1)
        thr_block.addWidget(lbl_red, 0, 1)
        thr_block.addWidget(lbl_sound, 0, 2)
        thr_block.addWidget(self.threshold_edit, 1, 1)
        thr_block.addWidget(self.sound_threshold_edit, 1, 2)
        thr_block.addItem(QtWidgets.QSpacerItem(1, 1, QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Minimum), 0, 3, 2, 1)

        thr_wrap = QtWidgets.QWidget()
        thr_wrap.setLayout(thr_block)

        grid.addWidget(thr_wrap, row, 0, 1, 6)
        row += 1

        grid.addItem(
            QtWidgets.QSpacerItem(0, 6, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed),
            row,
            0,
            1,
            6,
        )
        row += 1

        # Sound dropdown + play
        self.sound_combo = QtWidgets.QComboBox()
        self.sound_combo.currentIndexChanged.connect(self._on_sound_selection_changed)
        self.sound_combo.setMinimumHeight(ctrl_h)

        self.sound_play_btn = QtWidgets.QToolButton()
        self.sound_play_btn.setText("\u25B6")
        self.sound_play_btn.setToolTip("Play selected sound")
        self.sound_play_btn.clicked.connect(self._play_selected_sound_sample)
        self.sound_play_btn.setFixedSize(ctrl_h, ctrl_h)

        self.sound_upload_btn = QtWidgets.QToolButton()
        self.sound_upload_btn.setText("\u2191")
        self.sound_upload_btn.setToolTip("Upload .wav sound")
        self.sound_upload_btn.clicked.connect(self._upload_sound)
        self.sound_upload_btn.setFixedSize(ctrl_h, ctrl_h)

        grid.addWidget(self.sound_combo, row, 0, 1, 4)
        grid.addWidget(self.sound_play_btn, row, 4, 1, 1)
        grid.addWidget(self.sound_upload_btn, row, 5, 1, 1)
        row += 1

        grid.addItem(
            QtWidgets.QSpacerItem(0, 6, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Fixed),
            row,
            0,
            1,
            6,
        )
        row += 1

        # Volume slider + reset/value
        self.vol_label = QtWidgets.QLabel("Volume")
        self.vol_label.setFixedWidth(54)

        self.volume_slider = ClickableSlider(QtCore.Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setSingleStep(1)
        self.volume_slider.setPageStep(10)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)

        self.volume_reset_btn = QtWidgets.QPushButton("50")
        self.volume_reset_btn.setToolTip("Click to reset volume to 50")
        self.volume_reset_btn.setFixedWidth(44)
        self.volume_reset_btn.clicked.connect(self._reset_volume_to_default)

        grid.addWidget(self.vol_label, row, 0, 1, 1)
        grid.addWidget(self.volume_slider, row, 1, 1, 4)
        grid.addWidget(self.volume_reset_btn, row, 5, 1, 1)
        row += 1

        grid.setRowStretch(row, 1)

        # -------- Right panel --------
        timer_panel = QtWidgets.QFrame()
        timer_panel.setObjectName("timerPanel")
        self.splitter.addWidget(timer_panel)

        t_layout = QtWidgets.QVBoxLayout(timer_panel)
        t_layout.setContentsMargins(14, 10, 14, 10)
        t_layout.setSpacing(8)

        hint = QtWidgets.QLabel("Right-click timer box to activate miniplayer • Right-click miniplayer to restore")
        hint.setStyleSheet("QLabel { color: #222; font-size: 10px; }")
        hint.setWordWrap(True)
        t_layout.addWidget(hint, 0)

        self.timer_box = QtWidgets.QFrame()
        self.timer_box.setStyleSheet(
            """
            QFrame {
                background: #000;
                border: 2px inset #3a3a3a;
                border-radius: 10px;
            }
            """
        )
        self.timer_box.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.timer_box.customContextMenuRequested.connect(lambda _pos: self._enter_miniplayer())

        box_layout = QtWidgets.QVBoxLayout(self.timer_box)
        box_layout.setContentsMargins(14, 14, 14, 14)

        self.timer_display = QtWidgets.QLCDNumber()
        self.timer_display.setSegmentStyle(QtWidgets.QLCDNumber.Flat)
        self.timer_display.setDigitCount(3)
        self.timer_display.setStyleSheet("QLCDNumber { background: transparent; color: #33ff66; }")
        self.timer_display.setMinimumHeight(120)
        box_layout.addWidget(self.timer_display, 1)

        t_layout.addWidget(self.timer_box, 1)

        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        self._refresh_windows()
        self._populate_sound_list()
        self._load_persistent_state()

        self.timer_model.reset_to_full()
        self._rearm_sound()

        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.timeout.connect(self._tick_ui)
        self.ui_timer.start(100)
        self._tick_ui()

    # ---------------- Sound helpers ----------------
    def _available_wavs(self) -> list[Path]:
        try:
            items = {}
            if SOUNDS_DIR_PATH.exists() and SOUNDS_DIR_PATH.is_dir():
                for p in SOUNDS_DIR_PATH.glob("*.wav"):
                    if p.is_file():
                        items[p.name.lower()] = p

            extra_dir = Path(__file__).resolve().parent / "assets" / "sounds"
            if extra_dir.exists() and extra_dir.is_dir():
                for p in extra_dir.glob("*.wav"):
                    if p.is_file():
                        items[p.name.lower()] = p

            user_dir = self._user_sounds_dir()
            if user_dir.exists() and user_dir.is_dir():
                for p in user_dir.glob("*.wav"):
                    if p.is_file():
                        items[p.name.lower()] = p

            if items:
                return sorted(items.values(), key=lambda p: p.name.lower())
        except Exception:
            pass
        return []

    def _populate_sound_list(self):
        self.sound_combo.blockSignals(True)
        self.sound_combo.clear()

        wavs = self._available_wavs()
        if not wavs:
            self.sound_combo.addItem("None (no .wav found)", "")
        else:
            self.sound_combo.addItem("None", "")
            for p in wavs:
                self.sound_combo.addItem(p.stem, p.name)

        self.sound_combo.blockSignals(False)

    def _set_sound_by_relname(self, rel_name: str):
        rel_name = (rel_name or "").strip()
        if not rel_name:
            self._sound.setSource(QtCore.QUrl())
            self.timer_model.config.sound_file_rel = ""
            return

        rel_name = Path(rel_name).name
        full = (self._user_sounds_dir() / rel_name).resolve()
        if not full.exists():
            full = (SOUNDS_DIR_PATH / rel_name).resolve()
            if not full.exists():
                self._sound.setSource(QtCore.QUrl())
                self.timer_model.config.sound_file_rel = ""
                return

        self.timer_model.config.sound_file_rel = rel_name
        self._sound.setSource(QtCore.QUrl.fromLocalFile(str(full)))

    def _apply_volume_to_sound(self):
        v = float(self.timer_model.config.volume)
        v = max(0.0, min(1.0, v))
        self.timer_model.config.volume = v
        try:
            self._sound.setVolume(v)
        except Exception:
            pass

    def _play_sound_once(self):
        try:
            if self._sound.source().isEmpty():
                return
            self._apply_volume_to_sound()
            self._sound.play()
        except Exception:
            pass

    def _play_selected_sound_sample(self):
        rel = self.sound_combo.currentData() or ""
        self._set_sound_by_relname(rel)
        self._play_sound_once()

    def _user_sounds_dir(self) -> Path:
        base = os.getenv("APPDATA")
        if base:
            return Path(base) / "Waxy Timer" / "sounds"
        base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation)
        return Path(base) / "sounds"

    def _upload_sound(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Choose a .wav file",
            "",
            "WAV files (*.wav)",
        )
        if not path:
            return

        src = Path(path)
        if src.suffix.lower() != ".wav" or not src.is_file():
            return

        user_dir = self._user_sounds_dir()
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return

        dest = user_dir / src.name
        if dest.exists():
            stem = src.stem
            suffix = src.suffix
            i = 1
            while True:
                candidate = user_dir / f"{stem}_{i}{suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
                i += 1

        try:
            shutil.copy2(src, dest)
        except Exception:
            return

        self._populate_sound_list()
        for i in range(self.sound_combo.count()):
            if (self.sound_combo.itemData(i) or "") == dest.name:
                self.sound_combo.setCurrentIndex(i)
                break

    def _rearm_sound(self):
        self._sound_fired = False

    # ---------------- Persistence ----------------
    def _load_persistent_state(self):
        geo = self.settings.value("main/geometry", None)
        if isinstance(geo, QtCore.QByteArray):
            self.restoreGeometry(geo)

        s = self.settings.value("main/splitter", None)
        if isinstance(s, QtCore.QByteArray):
            self.splitter.restoreState(s)
        else:
            self.splitter.setSizes([320, 340])

        length = self.settings.value("config/length_seconds", 90, int)
        self.length_combo.setCurrentIndex(0 if int(length) == 90 else 1)
        self.timer_model.config.length_seconds = int(self.length_combo.currentData())

        below = self.settings.value("config/count_below_zero", True)
        if isinstance(below, str):
            below = below.strip().lower() in ("1", "true", "yes", "on")
        self.below_zero_chk.setChecked(bool(below))
        self.timer_model.config.count_below_zero = bool(below)

        saved_thr = self.settings.value("config/threshold_seconds", None)
        if saved_thr is None:
            self.timer_model.config.threshold_seconds = 15 if int(self.timer_model.config.length_seconds) == 90 else 60
        else:
            try:
                self.timer_model.config.threshold_seconds = max(0, int(saved_thr))
            except Exception:
                self.timer_model.config.threshold_seconds = 15
        self.threshold_edit.setText(str(self.timer_model.config.threshold_seconds))

        has_saved_sound_thr = self.settings.contains("config/sound_threshold_seconds")
        saved_sound_thr = self.settings.value("config/sound_threshold_seconds", None)
        if not has_saved_sound_thr or saved_sound_thr is None:
            self.timer_model.config.sound_threshold_seconds = DEFAULT_SOUND_THRESHOLD
        else:
            try:
                self.timer_model.config.sound_threshold_seconds = max(0, int(saved_sound_thr))
            except Exception:
                self.timer_model.config.sound_threshold_seconds = DEFAULT_SOUND_THRESHOLD
        self.sound_threshold_edit.setText(str(self.timer_model.config.sound_threshold_seconds))

        has_saved_sound = self.settings.contains("config/sound_file_rel")
        saved_sound_file = self.settings.value("config/sound_file_rel", "")
        if not isinstance(saved_sound_file, str):
            saved_sound_file = ""
        saved_sound_file = saved_sound_file.strip()

        if not has_saved_sound:
            if (SOUNDS_DIR_PATH / DEFAULT_SOUND_FILE).exists():
                saved_sound_file = DEFAULT_SOUND_FILE
            else:
                saved_sound_file = ""

        selected_idx = 0
        for i in range(self.sound_combo.count()):
            if (self.sound_combo.itemData(i) or "") == saved_sound_file:
                selected_idx = i
                break
        self.sound_combo.setCurrentIndex(selected_idx)
        self._set_sound_by_relname(self.sound_combo.currentData() or "")

        vol_val = self.settings.value("config/volume", DEFAULT_VOLUME)
        try:
            vol_val = float(vol_val)
        except Exception:
            vol_val = DEFAULT_VOLUME
        vol_val = max(0.0, min(1.0, vol_val))
        self.timer_model.config.volume = vol_val

        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(int(round(vol_val * 100)))
        self.volume_slider.blockSignals(False)
        self.volume_reset_btn.setText(str(int(round(vol_val * 100))))
        self._apply_volume_to_sound()

        hint = self.settings.value("config/preferred_window_hint", "2004scape game")
        if not isinstance(hint, str):
            hint = "2004scape game"
        self._preferred_window_hint = hint.lower()
        self._select_best_default_target()

    def _save_persistent_state(self):
        self.settings.setValue("main/geometry", self.saveGeometry())
        self.settings.setValue("main/splitter", self.splitter.saveState())

        self.settings.setValue("config/length_seconds", int(self.timer_model.config.length_seconds))
        self.settings.setValue("config/count_below_zero", bool(self.timer_model.config.count_below_zero))
        self.settings.setValue("config/threshold_seconds", int(self.timer_model.config.threshold_seconds))
        self.settings.setValue("config/sound_threshold_seconds", int(self.timer_model.config.sound_threshold_seconds))
        self.settings.setValue("config/sound_file_rel", str(self.timer_model.config.sound_file_rel or ""))
        self.settings.setValue("config/volume", float(self.timer_model.config.volume))

        cur_title = self.window_combo.currentText().strip().lower()
        if "2004scape game" in cur_title and not any(
            b in cur_title for b in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")
        ):
            self.settings.setValue("config/preferred_window_hint", "2004scape game")
        else:
            self.settings.setValue("config/preferred_window_hint", cur_title)

        self.settings.sync()

    # ---------------- Target selection ----------------
    def _refresh_windows(self):
        current_hwnd = int(self.window_combo.currentData()) if self.window_combo.currentData() else 0

        self.window_combo.blockSignals(True)
        self.window_combo.clear()
        for hwnd, title in list_target_windows():
            self.window_combo.addItem(title, int(hwnd))
        self.window_combo.blockSignals(False)

        try:
            self.window_combo.currentIndexChanged.disconnect()
        except Exception:
            pass
        self.window_combo.currentIndexChanged.connect(self._on_target_changed)

        if current_hwnd:
            for i in range(self.window_combo.count()):
                if int(self.window_combo.itemData(i)) == current_hwnd:
                    self.window_combo.setCurrentIndex(i)
                    self.timer_model.set_target_hwnd(current_hwnd)
                    return

        self._select_best_default_target()

    def _select_best_default_target(self):
        if self.window_combo.count() == 0:
            self.timer_model.set_target_hwnd(0)
            return

        best_idx = None
        for i in range(self.window_combo.count()):
            title = self.window_combo.itemText(i).lower()
            if "2004scape game" in title and not any(
                b in title for b in ("chrome", "edge", "firefox", "brave", "opera", "vivaldi")
            ):
                best_idx = i
                break

        if best_idx is None and getattr(self, "_preferred_window_hint", ""):
            hint = self._preferred_window_hint
            for i in range(self.window_combo.count()):
                if hint and hint in self.window_combo.itemText(i).lower():
                    best_idx = i
                    break

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
        self._rearm_sound()

    # ---------------- Handlers ----------------
    def _on_length_changed(self, _idx):
        length = int(self.length_combo.currentData())
        self.timer_model.config.length_seconds = length
        self.timer_model.reset_to_full()
        self._rearm_sound()
        self.timer_model.config.threshold_seconds = 15 if length == 90 else 60
        self.threshold_edit.setText(str(self.timer_model.config.threshold_seconds))

    def _on_below_zero_changed(self, _state):
        self.timer_model.config.count_below_zero = self.below_zero_chk.isChecked()

    def _on_threshold_changed(self):
        try:
            val = max(0, int(float(self.threshold_edit.text().strip())))
            self.timer_model.config.threshold_seconds = val
            self.threshold_edit.setText(str(val))
        except Exception:
            self.threshold_edit.setText(str(self.timer_model.config.threshold_seconds))

    def _on_sound_threshold_changed(self):
        try:
            val = max(0, int(float(self.sound_threshold_edit.text().strip())))
            self.timer_model.config.sound_threshold_seconds = val
            self.sound_threshold_edit.setText(str(val))
            self._rearm_sound()
        except Exception:
            self.sound_threshold_edit.setText(str(self.timer_model.config.sound_threshold_seconds))

    def _on_sound_selection_changed(self, _idx):
        rel = self.sound_combo.currentData() or ""
        self._set_sound_by_relname(rel)
        self._rearm_sound()

    def _on_volume_changed(self, value: int):
        value = max(0, min(100, int(value)))
        self.volume_reset_btn.setText(str(value))
        self.timer_model.config.volume = float(value) / 100.0
        self._apply_volume_to_sound()

    def _reset_volume_to_default(self):
        self.volume_slider.setValue(50)

    # ---------------- Miniplayer behavior ----------------
    def _on_miniplayer_geometry_changed(self, rect: QtCore.QRect):
        if self.overlay.isVisible():
            self._mini_last_geo = QtCore.QRect(rect)

    def _enter_miniplayer(self):
        if time.time() < self._suppress_miniplayer_until:
            return

        if self._mini_last_geo is None:
            sz = self.timer_box.size()
            w = max(self.overlay.minimumWidth(), int(sz.width()))
            h = max(self.overlay.minimumHeight(), int(sz.height()))
        else:
            w = max(self.overlay.minimumWidth(), int(self._mini_last_geo.width()))
            h = max(self.overlay.minimumHeight(), int(self._mini_last_geo.height()))

        top_left = self.timer_box.mapToGlobal(QtCore.QPoint(0, 0))
        self.overlay.setGeometry(QtCore.QRect(top_left, QtCore.QSize(w, h)))

        self.overlay.show()
        self.hide()

    def _restore_from_miniplayer(self):
        self._suppress_miniplayer_until = time.time() + 0.35

        ui_min_w, ui_min_h = self.minimumWidth(), self.minimumHeight()

        cur_w, cur_h = self.width(), self.height()
        target_w = max(ui_min_w, cur_w)
        target_h = max(ui_min_h, cur_h)

        overlay_pos = self.overlay.pos()

        self.overlay.hide()
        self.show()

        # resize first, then process events so layouts settle
        self.resize(target_w, target_h)
        QtWidgets.QApplication.processEvents()

        # align so timer_box global top-left matches overlay top-left
        cur_timer_global = self.timer_box.mapToGlobal(QtCore.QPoint(0, 0))
        delta = overlay_pos - cur_timer_global
        self.move(self.pos() + delta)

        self.activateWindow()

    # ---------------- Time formatting + draw ----------------
    def _quantize_seconds_for_display(self, seconds: float) -> int:
        if seconds > 0:
            return int(math.ceil(seconds))
        if seconds < 0:
            return -int(math.ceil(abs(seconds)))
        return 0

    def _format_time(self, seconds: float) -> tuple[str, int]:
        neg = seconds < 0
        s = self._quantize_seconds_for_display(seconds)
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

        if self._last_remaining is not None and remaining > (self._last_remaining + 0.75):
            self._rearm_sound()

        sound_thr = float(self.timer_model.config.sound_threshold_seconds)
        if not self._sound_fired and self._last_remaining is not None:
            if (self._last_remaining > sound_thr) and (remaining <= sound_thr):
                self._play_sound_once()
                self._sound_fired = True

        self._last_remaining = remaining

        danger = remaining <= float(self.timer_model.config.threshold_seconds)
        text, digits = self._format_time(remaining)

        if int(self.timer_model.config.length_seconds) == 90:
            self.timer_display.setDigitCount(max(3, digits))
        else:
            self.timer_display.setDigitCount(6 if text.startswith("-") else 5)

        self.timer_display.display(text)
        color = "#ff3b30" if danger else "#33ff66"
        self.timer_display.setStyleSheet(f"QLCDNumber {{ background: transparent; color: {color}; }}")

        if self.overlay.isVisible():
            overlay_digits = (
                max(3, digits)
                if int(self.timer_model.config.length_seconds) == 90
                else (6 if text.startswith("-") else 5)
            )
            self.overlay.set_time_text(text, danger, overlay_digits)

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
