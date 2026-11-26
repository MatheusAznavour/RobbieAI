"""Windows-native desktop primitives: DPI awareness, screen capture, window switching,
clipboard handling, Tesseract detection, and validated action dispatch.
"""

from __future__ import annotations

import ctypes
import difflib
import io
import logging
import os
import platform
import shutil
import time
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

from robbie.tools.actions import (
    ClickAction,
    DoneAction,
    DragAndDropAction,
    FailAction,
    HighlightTextSpanAction,
    HoldAndPressAction,
    HotkeyAction,
    OpenApplicationAction,
    RobbieAction,
    SaveToKnowledgeAction,
    ScrollAction,
    SwitchApplicationAction,
    TypeTextAction,
    WaitAction,
)

logger = logging.getLogger("robbie.tools.windows")


class ActionExecutionError(RuntimeError):
    """Raised when a validated desktop action fails during execution."""


class WindowsPermissionError(ActionExecutionError):
    """Raised when Windows UIPI/UAC boundaries block interaction with a target window."""


@dataclass(frozen=True)
class DisplayInfo:
    """Geometry and DPI metadata for the active Windows desktop."""

    primary_width: int
    primary_height: int
    virtual_left: int
    virtual_top: int
    virtual_width: int
    virtual_height: int
    monitor_count: int
    dpi_aware: bool


@dataclass(frozen=True)
class TesseractStatus:
    """Detection result for the optional native Tesseract OCR binary."""

    available: bool
    executable_path: str | None


@dataclass(frozen=True)
class WindowEntry:
    """Metadata for a visible top-level Windows application window."""

    hwnd: int
    title: str
    pid: int
    process_name: str
    is_minimized: bool


def configure_windows_dpi_awareness() -> bool:
    """Enable per-monitor DPI awareness on Windows so screenshot and input pixels align."""
    if platform.system() != "Windows":
        return False
    try:
        shcore = ctypes.windll.shcore
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        result = shcore.SetProcessDpiAwareness(2)
        # S_OK (0) or E_ACCESSDENIED (0x80070005 when already set)
        if result in (0, -2147024891):
            return True
    except Exception:
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:
        return False


def get_windows_display_info(*, configure_dpi: bool = True) -> DisplayInfo:
    """Inspect Windows primary/virtual monitor dimensions and DPI awareness."""
    dpi_aware = configure_windows_dpi_awareness() if configure_dpi else False
    if platform.system() == "Windows":
        try:
            user32 = ctypes.windll.user32
            primary_width = int(user32.GetSystemMetrics(0))  # SM_CXSCREEN
            primary_height = int(user32.GetSystemMetrics(1))  # SM_CYSCREEN
            virtual_left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            virtual_top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            virtual_width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            virtual_height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            monitor_count = int(user32.GetSystemMetrics(80))  # SM_CMONITORS
            return DisplayInfo(
                primary_width=max(primary_width, 1),
                primary_height=max(primary_height, 1),
                virtual_left=virtual_left,
                virtual_top=virtual_top,
                virtual_width=max(virtual_width, primary_width, 1),
                virtual_height=max(virtual_height, primary_height, 1),
                monitor_count=max(monitor_count, 1),
                dpi_aware=dpi_aware,
            )
        except Exception as exc:
            logger.warning("Failed to query Windows display metrics via user32: %s", exc)

    return DisplayInfo(
        primary_width=1920,
        primary_height=1080,
        virtual_left=0,
        virtual_top=0,
        virtual_width=1920,
        virtual_height=1080,
        monitor_count=1,
        dpi_aware=dpi_aware,
    )


def detect_tesseract(configured_cmd: str | None = None) -> TesseractStatus:
    """Detect native Tesseract OCR binary without failing if unavailable."""
    candidate = (configured_cmd or os.environ.get("TESSERACT_CMD", "")).strip()
    if candidate:
        if os.path.isfile(candidate):
            return TesseractStatus(available=True, executable_path=candidate)
        resolved = shutil.which(candidate)
        if resolved:
            return TesseractStatus(available=True, executable_path=resolved)

    found = shutil.which("tesseract")
    if found:
        return TesseractStatus(available=True, executable_path=found)
    return TesseractStatus(available=False, executable_path=None)


class ScreenCaptureProtocol(Protocol):
    def capture_png(self, max_dimension: int = 2400) -> bytes: ...


class WindowsScreenCapture:
    """Captures the primary Windows monitor as PNG bytes with DPI-consistent sizing."""

    def __init__(self, display_info: DisplayInfo | None = None) -> None:
        self.display_info = display_info or get_windows_display_info()

    def capture_png(self, max_dimension: int = 2400) -> bytes:
        from PIL import ImageGrab

        if self.display_info.monitor_count > 1:
            logger.debug(
                "Multi-monitor desktop detected (%d monitors); capturing primary monitor (%dx%d).",
                self.display_info.monitor_count,
                self.display_info.primary_width,
                self.display_info.primary_height,
            )

        bbox = (0, 0, self.display_info.primary_width, self.display_info.primary_height)
        image = ImageGrab.grab(bbox=bbox, all_screens=False)

        width, height = image.size
        scale = min(max_dimension / width, max_dimension / height, 1.0)
        if scale < 1.0:
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            image = image.resize(new_size, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()


class WindowManagerProtocol(Protocol):
    def list_windows(self) -> list[WindowEntry]: ...
    def activate_application(self, app_name: str) -> WindowEntry: ...


class WindowsWindowManager:
    """Native Windows window enumeration and activation via win32gui / psutil."""

    def list_windows(self) -> list[WindowEntry]:
        if platform.system() != "Windows":
            raise ActionExecutionError("WindowsWindowManager is only supported on Windows.")

        import psutil
        import win32gui
        import win32process

        entries: list[WindowEntry] = []

        def _callback(hwnd: int, _extra: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process_name = ""
            try:
                process_name = psutil.Process(pid).name()
            except Exception:
                process_name = ""
            entries.append(
                WindowEntry(
                    hwnd=hwnd,
                    title=title,
                    pid=int(pid),
                    process_name=process_name,
                    is_minimized=bool(win32gui.IsIconic(hwnd)),
                )
            )
            return True

        win32gui.EnumWindows(_callback, None)
        return entries

    @staticmethod
    def match_window(app_name: str, windows: list[WindowEntry]) -> WindowEntry | None:
        query = app_name.strip().lower()
        if not query or not windows:
            return None

        # 1. Exact process stem or substring match against process name or title
        for win in windows:
            proc_stem = win.process_name.lower().removesuffix(".exe")
            if query == proc_stem or query == win.title.lower():
                return win

        for win in windows:
            proc_lower = win.process_name.lower()
            if query in win.title.lower() or (proc_lower and query in proc_lower):
                return win

        # 2. Fuzzy match over combined title + process labels
        candidates = {f"{w.title} {w.process_name}".lower(): w for w in windows}
        close = difflib.get_close_matches(query, list(candidates.keys()), n=1, cutoff=0.35)
        if close:
            return candidates[close[0]]
        return None

    def activate_application(self, app_name: str) -> WindowEntry:
        if platform.system() != "Windows":
            raise ActionExecutionError("Windows application switching requires Windows.")

        import win32con
        import win32gui

        windows = self.list_windows()
        target = self.match_window(app_name, windows)
        if target is None:
            raise ActionExecutionError(f"No open window matched application '{app_name}'.")

        try:
            if target.is_minimized:
                win32gui.ShowWindow(target.hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(target.hwnd, win32con.SW_SHOW)
            win32gui.SetForegroundWindow(target.hwnd)
        except Exception as exc:
            err_msg = str(exc)
            if "Access is denied" in err_msg or "5" in err_msg:
                raise WindowsPermissionError(
                    f"Cannot focus window '{target.title}' (pid={target.pid}): "
                    "target window may be running elevated (UAC/UIPI boundary)."
                ) from exc
            raise ActionExecutionError(
                f"Failed to activate window '{target.title}': {err_msg}"
            ) from exc

        return target


class ClipboardProtocol(Protocol):
    def get_text(self) -> str: ...
    def set_text(self, text: str) -> None: ...


class WindowsClipboard:
    """Windows-native clipboard wrapper using pyperclip without Linux fallbacks."""

    def get_text(self) -> str:
        import pyperclip

        return str(pyperclip.paste() or "")

    def set_text(self, text: str) -> None:
        import pyperclip

        pyperclip.copy(text)


class InputDriverProtocol(Protocol):
    def click(self, x: int, y: int, clicks: int, button: str) -> None: ...
    def move_to(self, x: int, y: int) -> None: ...
    def drag_to(self, x: int, y: int, duration: float, button: str) -> None: ...
    def mouse_up(self) -> None: ...
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def press(self, keys: str | list[str]) -> None: ...
    def hotkey(self, *keys: str) -> None: ...
    def write_ascii(self, text: str) -> None: ...
    def vscroll(self, clicks: int) -> None: ...
    def hscroll(self, clicks: int) -> None: ...
    def sleep(self, seconds: float) -> None: ...


class PyAutoGuiInputDriver:
    """Thin wrapper around PyAutoGUI with fail-safe enabled."""

    def __init__(self) -> None:
        import pyautogui

        pyautogui.FAILSAFE = True
        self._pyautogui = pyautogui

    def click(self, x: int, y: int, clicks: int, button: str) -> None:
        self._pyautogui.click(x, y, clicks=clicks, button=button)

    def move_to(self, x: int, y: int) -> None:
        self._pyautogui.moveTo(x, y)

    def drag_to(self, x: int, y: int, duration: float, button: str) -> None:
        self._pyautogui.dragTo(x, y, duration=duration, button=button)

    def mouse_up(self) -> None:
        self._pyautogui.mouseUp()

    def key_down(self, key: str) -> None:
        self._pyautogui.keyDown(key)

    def key_up(self, key: str) -> None:
        self._pyautogui.keyUp(key)

    def press(self, keys: str | list[str]) -> None:
        self._pyautogui.press(keys)

    def hotkey(self, *keys: str) -> None:
        self._pyautogui.hotkey(*keys)

    def write_ascii(self, text: str) -> None:
        self._pyautogui.write(text)

    def vscroll(self, clicks: int) -> None:
        self._pyautogui.vscroll(clicks)

    def hscroll(self, clicks: int) -> None:
        self._pyautogui.hscroll(clicks)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class ActionExecutionResult:
    """Structured result emitted after executing (or dry-running) a validated action."""

    kind: str
    status: str
    detail: str


class WindowsActionDispatcher:
    """Executes strictly validated RobbieAction objects without `exec()` or shell strings."""

    def __init__(
        self,
        *,
        display_info: DisplayInfo | None = None,
        input_driver: InputDriverProtocol | None = None,
        window_manager: WindowManagerProtocol | None = None,
        clipboard: ClipboardProtocol | None = None,
        dry_run: bool = False,
    ) -> None:
        self.display_info = display_info or get_windows_display_info()
        self._input_driver = input_driver
        self.window_manager: WindowManagerProtocol = window_manager or WindowsWindowManager()
        self.clipboard: ClipboardProtocol = clipboard or WindowsClipboard()
        self.dry_run = dry_run

    @property
    def input_driver(self) -> InputDriverProtocol:
        if self._input_driver is None:
            self._input_driver = PyAutoGuiInputDriver()
        return self._input_driver

    def _validate_point(self, x: int, y: int) -> tuple[int, int]:
        max_x = self.display_info.primary_width - 1
        max_y = self.display_info.primary_height - 1
        if x < 0 or y < 0 or x > max_x or y > max_y:
            raise ActionExecutionError(
                f"Coordinates ({x}, {y}) are outside primary monitor bounds "
                f"(0..{max_x}, 0..{max_y})."
            )
        return x, y

    def execute(self, action: RobbieAction) -> ActionExecutionResult:
        if isinstance(action, (DoneAction, FailAction, SaveToKnowledgeAction)):
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Handled control action '{action.kind}'.",
            )

        if self.dry_run:
            return ActionExecutionResult(
                kind=action.kind,
                status="dry_run",
                detail=f"Dry-run skipped OS execution for '{action.kind}'.",
            )

        if isinstance(action, ClickAction):
            x, y = self._validate_point(action.x, action.y)
            self._with_held_keys(
                action.hold_keys,
                lambda: self.input_driver.click(x, y, action.num_clicks, action.button),
            )
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Clicked ({x}, {y}) button={action.button} clicks={action.num_clicks}.",
            )

        if isinstance(action, TypeTextAction):
            if action.x is not None and action.y is not None:
                x, y = self._validate_point(action.x, action.y)
                self.input_driver.click(x, y, 1, "left")
            if action.overwrite:
                self.input_driver.hotkey("ctrl", "a")
                self.input_driver.press("backspace")
            has_non_ascii = any(ord(ch) > 127 for ch in action.text)
            if has_non_ascii:
                previous_clipboard: str | None = None
                try:
                    previous_clipboard = self.clipboard.get_text()
                except Exception:
                    previous_clipboard = None
                self.clipboard.set_text(action.text)
                self.input_driver.hotkey("ctrl", "v")
                if previous_clipboard is not None:
                    self.input_driver.sleep(0.05)
                    try:
                        self.clipboard.set_text(previous_clipboard)
                    except Exception:
                        pass
            else:
                self.input_driver.write_ascii(action.text)
            if action.enter:
                self.input_driver.press("enter")
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Typed {len(action.text)} character(s) (unicode={has_non_ascii}).",
            )

        if isinstance(action, DragAndDropAction):
            sx, sy = self._validate_point(action.start_x, action.start_y)
            ex, ey = self._validate_point(action.end_x, action.end_y)
            self.input_driver.move_to(sx, sy)
            self._with_held_keys(
                action.hold_keys,
                lambda: (
                    self.input_driver.drag_to(ex, ey, 0.6, "left"),
                    self.input_driver.mouse_up(),
                ),
            )
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Dragged from ({sx}, {sy}) to ({ex}, {ey}).",
            )

        if isinstance(action, HighlightTextSpanAction):
            sx, sy = self._validate_point(action.start_x, action.start_y)
            ex, ey = self._validate_point(action.end_x, action.end_y)
            self.input_driver.move_to(sx, sy)
            self.input_driver.drag_to(ex, ey, 0.6, action.button)
            self.input_driver.mouse_up()
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Highlighted span ({sx}, {sy}) -> ({ex}, {ey}).",
            )

        if isinstance(action, ScrollAction):
            x, y = self._validate_point(action.x, action.y)
            self.input_driver.move_to(x, y)
            if action.horizontal:
                self.input_driver.hscroll(action.clicks)
            else:
                self.input_driver.vscroll(action.clicks)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Scrolled clicks={action.clicks} horizontal={action.horizontal}.",
            )

        if isinstance(action, HotkeyAction):
            self.input_driver.hotkey(*action.keys)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Pressed hotkey {action.keys}.",
            )

        if isinstance(action, HoldAndPressAction):
            self._with_held_keys(
                action.hold_keys,
                lambda: self.input_driver.press(action.press_keys),
            )
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Held {action.hold_keys} and pressed {action.press_keys}.",
            )

        if isinstance(action, SwitchApplicationAction):
            matched = self.window_manager.activate_application(action.app_name)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Activated window '{matched.title}' (pid={matched.pid}).",
            )

        if isinstance(action, OpenApplicationAction):
            # Use Windows Start search UI instead of shell execution so no shell
            # interpolation or arbitrary command execution is introduced.
            self.input_driver.press("win")
            self.input_driver.sleep(0.4)
            self.input_driver.write_ascii(action.target)
            self.input_driver.sleep(0.8)
            self.input_driver.press("enter")
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Requested opening '{action.target}' via Windows Start.",
            )

        if isinstance(action, WaitAction):
            self.input_driver.sleep(action.seconds)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Waited {action.seconds}s.",
            )

        raise ActionExecutionError(f"Unsupported action type: {type(action).__name__}")

    def _with_held_keys(self, hold_keys: list[str], operation: Any) -> None:
        pressed: list[str] = []
        try:
            for key in hold_keys:
                self.input_driver.key_down(key)
                pressed.append(key)
            operation()
        finally:
            for key in reversed(pressed):
                try:
                    self.input_driver.key_up(key)
                except Exception:
                    pass
