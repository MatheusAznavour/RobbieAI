"""Windows-native desktop primitives: DPI awareness, screen capture, window switching,
controlled app launching, clipboard handling, Tesseract detection, and interruptible dispatch.
"""

from __future__ import annotations

import ctypes
import difflib
import io
import logging
import os
import platform
import shutil
import subprocess
import threading
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
from robbie.tools.safety import (
    BLOCKED_INTERPRETERS_AND_LOLBINS,
    SHELL_METACHAR_PATTERN,
    classify_open_target,
)

logger = logging.getLogger("robbie.tools.windows")


class ActionExecutionError(RuntimeError):
    """Raised when a validated desktop action fails during execution."""


class ActionInterruptedError(ActionExecutionError):
    """Raised when an action is aborted at a safe boundary by cancellation or emergency stop."""


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
            primary_width = int(user32.GetSystemMetrics(0))
            primary_height = int(user32.GetSystemMetrics(1))
            virtual_left = int(user32.GetSystemMetrics(76))
            virtual_top = int(user32.GetSystemMetrics(77))
            virtual_width = int(user32.GetSystemMetrics(78))
            virtual_height = int(user32.GetSystemMetrics(79))
            monitor_count = int(user32.GetSystemMetrics(80))
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

        for win in windows:
            proc_stem = win.process_name.lower().removesuffix(".exe")
            if query == proc_stem or query == win.title.lower():
                return win

        for win in windows:
            proc_lower = win.process_name.lower()
            if query in win.title.lower() or (proc_lower and query in proc_lower):
                return win

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


class AppLauncherProtocol(Protocol):
    def launch(self, target: str, target_kind: str = "application") -> str: ...


def _default_windows_app_paths_registry_reader(exe_name: str) -> list[str]:
    """Query Windows HKCU/HKLM App Paths registry keys for an executable name."""
    if platform.system() != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []

    subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    results: list[str] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _reg_type = winreg.QueryValueEx(key, "")
                if isinstance(value, str) and value.strip():
                    results.append(value.strip())
        except OSError:
            continue
    return results


def _validate_resolved_executable_path(
    raw_value: str,
    *,
    file_exists: Any = os.path.isfile,
) -> str | None:
    """Validate a registry-resolved executable path strictly (no args, no LOLBins, no UNC)."""
    candidate = raw_value.strip()
    if len(candidate) >= 2 and candidate[0] == '"' and candidate[-1] == '"':
        candidate = candidate[1:-1].strip()
    if not candidate:
        return None
    if candidate.startswith("\\\\") or candidate.startswith("//"):
        return None
    if SHELL_METACHAR_PATTERN.search(candidate):
        return None

    expanded = os.path.expandvars(candidate).strip()
    if not os.path.isabs(expanded):
        return None
    if not expanded.lower().endswith(".exe"):
        return None

    basename = os.path.basename(expanded).lower()
    stem = os.path.splitext(basename)[0]
    if stem in BLOCKED_INTERPRETERS_AND_LOLBINS or basename in BLOCKED_INTERPRETERS_AND_LOLBINS:
        return None

    if not file_exists(expanded):
        return None
    return expanded


def resolve_windows_app_path(
    app_name: str,
    *,
    registry_reader: Any = None,
    file_exists: Any = os.path.isfile,
) -> str | None:
    """Resolve an approved application name via Windows App Paths registry safely."""
    cleaned = app_name.strip()
    if not cleaned or "\\" in cleaned or "/" in cleaned:
        return None
    if any(ch.isspace() for ch in cleaned):
        return None
    if SHELL_METACHAR_PATTERN.search(cleaned):
        return None

    stem = os.path.splitext(cleaned.lower())[0]
    if (
        stem in BLOCKED_INTERPRETERS_AND_LOLBINS
        or cleaned.lower() in BLOCKED_INTERPRETERS_AND_LOLBINS
    ):
        return None

    exe_name = cleaned if cleaned.lower().endswith(".exe") else f"{cleaned}.exe"
    reader = registry_reader or _default_windows_app_paths_registry_reader
    for raw_candidate in reader(exe_name):
        validated = _validate_resolved_executable_path(raw_candidate, file_exists=file_exists)
        if validated is not None:
            return validated
    return None


class WindowsAppLauncher:
    """Controlled Windows application/document launcher.

    Never interprets targets as shell commands, never passes CLI arguments, and
    never types arbitrary strings into Windows Start search.
    """

    def __init__(
        self,
        *,
        app_path_resolver: Any = None,
        popen_fn: Any = None,
    ) -> None:
        self._app_path_resolver = app_path_resolver or resolve_windows_app_path
        self._popen = popen_fn or subprocess.Popen

    def launch(self, target: str, target_kind: str = "application") -> str:
        classification = classify_open_target(target)
        if not classification.allowed:
            raise ActionExecutionError(f"Blocked launch target '{target}': {classification.reason}")

        if platform.system() != "Windows":
            raise ActionExecutionError("WindowsAppLauncher is only supported on Windows.")

        # 1. Explicitly allowlisted Windows binaries launched directly without shell.
        if classification.resolved_executable:
            resolved = shutil.which(classification.resolved_executable)
            if not resolved:
                raise ActionExecutionError(
                    f"Allowlisted Windows binary '{classification.resolved_executable}' not found."
                )
            self._popen([resolved], shell=False)
            return f"Launched allowlisted application '{classification.resolved_executable}'."

        # 2. User-approved safe document file opened via OS file association (no shell).
        expanded = os.path.abspath(os.path.expanduser(target))
        if target_kind == "file" or os.path.isfile(expanded):
            if not os.path.isfile(expanded):
                raise ActionExecutionError(f"Approved file target does not exist: '{target}'.")
            os.startfile(expanded)  # type: ignore[attr-defined]
            return f"Opened approved document '{os.path.basename(expanded)}'."

        # 3. User-approved non-allowlisted application resolved via PATH (no args, no shell).
        resolved_app = shutil.which(target)
        if resolved_app:
            validated_path = _validate_resolved_executable_path(resolved_app)
            if validated_path is None:
                raise ActionExecutionError(
                    f"Resolved PATH target for '{target}' failed executable validation."
                )
            self._popen([validated_path], shell=False)
            return f"Launched approved application '{os.path.basename(validated_path)}'."

        # 4. User-approved non-allowlisted application resolved via Windows App Paths registry.
        registry_app = self._app_path_resolver(target)
        if registry_app:
            self._popen([registry_app], shell=False)
            return f"Launched approved application '{os.path.basename(registry_app)}'."

        raise ActionExecutionError(
            f"Could not safely resolve application or document target '{target}'."
        )


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
    """Executes strictly validated RobbieAction objects with cooperative stop interruption."""

    def __init__(
        self,
        *,
        display_info: DisplayInfo | None = None,
        input_driver: InputDriverProtocol | None = None,
        window_manager: WindowManagerProtocol | None = None,
        clipboard: ClipboardProtocol | None = None,
        app_launcher: AppLauncherProtocol | None = None,
        dry_run: bool = False,
    ) -> None:
        self.display_info = display_info or get_windows_display_info()
        self._input_driver = input_driver
        self.window_manager: WindowManagerProtocol = window_manager or WindowsWindowManager()
        self.clipboard: ClipboardProtocol = clipboard or WindowsClipboard()
        self.app_launcher: AppLauncherProtocol = app_launcher or WindowsAppLauncher()
        self.dry_run = dry_run

    @property
    def input_driver(self) -> InputDriverProtocol:
        if self._input_driver is None:
            self._input_driver = PyAutoGuiInputDriver()
        return self._input_driver

    @staticmethod
    def _check_stop(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise ActionInterruptedError("Action aborted by cancellation or emergency stop.")

    def _interruptible_sleep(
        self,
        seconds: float,
        stop_event: threading.Event | None,
    ) -> None:
        if seconds <= 0:
            self._check_stop(stop_event)
            return
        if stop_event is None:
            self.input_driver.sleep(seconds)
            return
        if stop_event.wait(timeout=seconds):
            raise ActionInterruptedError("Wait aborted by cancellation or emergency stop.")

    def _validate_point(self, x: int, y: int) -> tuple[int, int]:
        max_x = self.display_info.primary_width - 1
        max_y = self.display_info.primary_height - 1
        if x < 0 or y < 0 or x > max_x or y > max_y:
            raise ActionExecutionError(
                f"Coordinates ({x}, {y}) are outside primary monitor bounds "
                f"(0..{max_x}, 0..{max_y})."
            )
        return x, y

    def execute(
        self,
        action: RobbieAction,
        *,
        stop_event: threading.Event | None = None,
    ) -> ActionExecutionResult:
        self._check_stop(stop_event)

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
                stop_event,
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
                self._check_stop(stop_event)
                self.input_driver.click(x, y, 1, "left")

            if action.overwrite:
                self._check_stop(stop_event)
                self.input_driver.hotkey("ctrl", "a")
                self._check_stop(stop_event)
                self.input_driver.press("backspace")

            self._check_stop(stop_event)
            has_non_ascii = any(ord(ch) > 127 for ch in action.text)
            if has_non_ascii:
                previous_clipboard: str | None = None
                try:
                    previous_clipboard = self.clipboard.get_text()
                except Exception:
                    previous_clipboard = None
                try:
                    self._check_stop(stop_event)
                    self.clipboard.set_text(action.text)
                    self._check_stop(stop_event)
                    self.input_driver.hotkey("ctrl", "v")
                    self._interruptible_sleep(0.05, stop_event)
                finally:
                    if previous_clipboard is not None:
                        try:
                            self.clipboard.set_text(previous_clipboard)
                        except Exception:
                            pass
            else:
                self.input_driver.write_ascii(action.text)

            if action.enter:
                self._check_stop(stop_event)
                self.input_driver.press("enter")

            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Typed {len(action.text)} character(s) (unicode={has_non_ascii}).",
            )

        if isinstance(action, DragAndDropAction):
            sx, sy = self._validate_point(action.start_x, action.start_y)
            ex, ey = self._validate_point(action.end_x, action.end_y)
            self._check_stop(stop_event)
            self.input_driver.move_to(sx, sy)
            self._with_held_keys(
                action.hold_keys,
                stop_event,
                lambda: self._perform_drag(sx, sy, ex, ey, "left", stop_event),
            )
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Dragged from ({sx}, {sy}) to ({ex}, {ey}).",
            )

        if isinstance(action, HighlightTextSpanAction):
            sx, sy = self._validate_point(action.start_x, action.start_y)
            ex, ey = self._validate_point(action.end_x, action.end_y)
            self._check_stop(stop_event)
            self.input_driver.move_to(sx, sy)
            self._perform_drag(sx, sy, ex, ey, action.button, stop_event)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Highlighted span ({sx}, {sy}) -> ({ex}, {ey}).",
            )

        if isinstance(action, ScrollAction):
            x, y = self._validate_point(action.x, action.y)
            self._check_stop(stop_event)
            self.input_driver.move_to(x, y)
            self._check_stop(stop_event)
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
            self._check_stop(stop_event)
            self.input_driver.hotkey(*action.keys)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Pressed hotkey {action.keys}.",
            )

        if isinstance(action, HoldAndPressAction):
            self._with_held_keys(
                action.hold_keys,
                stop_event,
                lambda: self._press_sequence(action.press_keys, stop_event),
            )
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Held {action.hold_keys} and pressed {action.press_keys}.",
            )

        if isinstance(action, SwitchApplicationAction):
            self._check_stop(stop_event)
            matched = self.window_manager.activate_application(action.app_name)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Activated window '{matched.title}' (pid={matched.pid}).",
            )

        if isinstance(action, OpenApplicationAction):
            self._check_stop(stop_event)
            detail = self.app_launcher.launch(action.target, action.target_kind)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=detail,
            )

        if isinstance(action, WaitAction):
            self._interruptible_sleep(action.seconds, stop_event)
            return ActionExecutionResult(
                kind=action.kind,
                status="ok",
                detail=f"Waited {action.seconds}s.",
            )

        raise ActionExecutionError(f"Unsupported action type: {type(action).__name__}")

    def _perform_drag(
        self,
        _sx: int,
        _sy: int,
        ex: int,
        ey: int,
        button: str,
        stop_event: threading.Event | None,
    ) -> None:
        self._check_stop(stop_event)
        try:
            self.input_driver.drag_to(ex, ey, 0.3, button)
        finally:
            try:
                self.input_driver.mouse_up()
            except Exception:
                pass

    def _press_sequence(
        self,
        press_keys: list[str],
        stop_event: threading.Event | None,
    ) -> None:
        for key in press_keys:
            self._check_stop(stop_event)
            self.input_driver.press(key)

    def _with_held_keys(
        self,
        hold_keys: list[str],
        stop_event: threading.Event | None,
        operation: Any,
    ) -> None:
        pressed: list[str] = []
        try:
            for key in hold_keys:
                self._check_stop(stop_event)
                self.input_driver.key_down(key)
                pressed.append(key)
            self._check_stop(stop_event)
            operation()
        finally:
            for key in reversed(pressed):
                try:
                    self.input_driver.key_up(key)
                except Exception:
                    pass
