"""Tests for RobbieWindowsACI and WindowsActionDispatcher using safe mocks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from robbie.agents.windows_aci import (
    GroundingError,
    OcrUnavailableError,
    RobbieWindowsACI,
    UnsupportedActionError,
)
from robbie.tools.actions import (
    ClickAction,
    HotkeyAction,
    OpenApplicationAction,
    ScrollAction,
    SwitchApplicationAction,
    TypeTextAction,
    WaitAction,
)
from robbie.tools.windows import (
    ActionExecutionError,
    DisplayInfo,
    WindowEntry,
    WindowsActionDispatcher,
    WindowsPermissionError,
    WindowsWindowManager,
    detect_tesseract,
)


@dataclass
class FakeInputDriver:
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def click(self, x: int, y: int, clicks: int, button: str) -> None:
        self.calls.append(("click", (x, y, clicks, button)))

    def move_to(self, x: int, y: int) -> None:
        self.calls.append(("move_to", (x, y)))

    def drag_to(self, x: int, y: int, duration: float, button: str) -> None:
        self.calls.append(("drag_to", (x, y, duration, button)))

    def mouse_up(self) -> None:
        self.calls.append(("mouse_up", None))

    def key_down(self, key: str) -> None:
        self.calls.append(("key_down", key))

    def key_up(self, key: str) -> None:
        self.calls.append(("key_up", key))

    def press(self, keys: str | list[str]) -> None:
        self.calls.append(("press", keys))

    def hotkey(self, *keys: str) -> None:
        self.calls.append(("hotkey", keys))

    def write_ascii(self, text: str) -> None:
        self.calls.append(("write_ascii", text))

    def vscroll(self, clicks: int) -> None:
        self.calls.append(("vscroll", clicks))

    def hscroll(self, clicks: int) -> None:
        self.calls.append(("hscroll", clicks))

    def sleep(self, seconds: float) -> None:
        self.calls.append(("sleep", seconds))


class FakeClipboard:
    def __init__(self, initial: str = "original-clipboard") -> None:
        self.value = initial
        self.history: list[str] = []

    def get_text(self) -> str:
        return self.value

    def set_text(self, text: str) -> None:
        self.value = text
        self.history.append(text)


class FakeAppLauncher:
    def __init__(self) -> None:
        self.launched: list[tuple[str, str]] = []

    def launch(self, target: str, target_kind: str = "application") -> str:
        self.launched.append((target, target_kind))
        return f"Launched {target}"


class FakeWindowManager:
    def __init__(self, windows: list[WindowEntry], *, deny_uac: bool = False) -> None:
        self._windows = windows
        self._deny_uac = deny_uac
        self.activated: list[str] = []

    def list_windows(self) -> list[WindowEntry]:
        return list(self._windows)

    def activate_application(self, app_name: str) -> WindowEntry:
        if self._deny_uac:
            raise WindowsPermissionError("UAC boundary blocked activation.")
        matched = WindowsWindowManager.match_window(app_name, self._windows)
        if matched is None:
            raise ActionExecutionError(f"No window matched {app_name}")
        self.activated.append(matched.title)
        return matched


def _sample_display() -> DisplayInfo:
    return DisplayInfo(
        primary_width=1920,
        primary_height=1080,
        virtual_left=0,
        virtual_top=0,
        virtual_width=3840,
        virtual_height=1080,
        monitor_count=2,
        dpi_aware=True,
    )


def _make_aci(tesseract_cmd: str | None = None) -> RobbieWindowsACI:
    return RobbieWindowsACI(
        engine_params_for_generation={"engine_type": "openai", "model": "gpt-test"},
        engine_params_for_grounding={
            "engine_type": "huggingface",
            "model": "ui-tars-test",
            "base_url": "http://127.0.0.1:9999",
            "grounding_width": 1000,
            "grounding_height": 1000,
        },
        display_info=_sample_display(),
        tesseract_cmd=tesseract_cmd,
    )


def test_windows_aci_disables_local_env_code_agent_and_linux_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    aci = _make_aci()

    assert aci.env is None
    assert aci.code_agent is None
    assert aci.platform == "windows"
    assert aci.tesseract_status.available is False
    assert "call_code_agent" not in aci.allowed_action_methods
    assert "set_cell_values" not in aci.allowed_action_methods
    assert "highlight_text_span" not in aci.allowed_action_methods

    with pytest.raises(UnsupportedActionError):
        aci.call_code_agent("print(1)")

    with pytest.raises(UnsupportedActionError):
        aci.set_cell_values({"A1": 1}, "Sheet.xlsx", "Sheet1")

    with pytest.raises(OcrUnavailableError):
        aci.highlight_text_span("hello", "world")

    with pytest.raises(ValueError, match="flat list of strings"):
        aci.save_to_knowledge([["nested"]])  # type: ignore[list-item]


def test_windows_aci_coordinate_scaling_clamping_and_grounding_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    aci = _make_aci()
    aci.assign_screenshot({"screenshot": b"fake-png"})

    monkeypatch.setattr("robbie.agents.windows_aci.call_llm_safe", lambda _m: "(500, 250)")
    action = aci.click("The submit button", num_clicks=2, button_type="left")
    assert isinstance(action, ClickAction)
    assert action.x == 960
    assert action.y == 270
    assert action.num_clicks == 2

    monkeypatch.setattr("robbie.agents.windows_aci.call_llm_safe", lambda _m: "(5000, 5000)")
    clamped = aci.click("Far edge")
    assert isinstance(clamped, ClickAction)
    assert clamped.x == 1919
    assert clamped.y == 1079

    monkeypatch.setattr("robbie.agents.windows_aci.call_llm_safe", lambda _m: "no coords here")
    with pytest.raises(GroundingError):
        aci.click("Missing button")


def test_detect_tesseract_graceful_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    status = detect_tesseract()
    assert status.available is False
    assert status.executable_path is None


def test_windows_window_manager_matching() -> None:
    windows = [
        WindowEntry(
            hwnd=1,
            title="Untitled - Notepad",
            pid=100,
            process_name="notepad.exe",
            is_minimized=False,
        ),
        WindowEntry(
            hwnd=2,
            title="RobbieAI - Visual Studio Code",
            pid=200,
            process_name="Code.exe",
            is_minimized=True,
        ),
    ]

    assert WindowsWindowManager.match_window("notepad", windows) == windows[0]
    assert WindowsWindowManager.match_window("Visual Studio Code", windows) == windows[1]
    assert WindowsWindowManager.match_window("nonexistent-app-xyz", windows) is None


def test_windows_dispatcher_executes_actions_safely_with_mocks() -> None:
    driver = FakeInputDriver()
    clipboard = FakeClipboard(initial="keep-me")
    launcher = FakeAppLauncher()
    wm = FakeWindowManager(
        [
            WindowEntry(
                hwnd=42,
                title="Calculator",
                pid=777,
                process_name="CalculatorApp.exe",
                is_minimized=False,
            )
        ]
    )
    dispatcher = WindowsActionDispatcher(
        display_info=_sample_display(),
        input_driver=driver,
        window_manager=wm,
        clipboard=clipboard,
        app_launcher=launcher,
    )

    res_click = dispatcher.execute(
        ClickAction(x=100, y=200, hold_keys=["shift"], element_description="Item")
    )
    assert res_click.status == "ok"
    assert driver.calls == [
        ("key_down", "shift"),
        ("click", (100, 200, 1, "left")),
        ("key_up", "shift"),
    ]

    driver.calls.clear()
    res_unicode = dispatcher.execute(
        TypeTextAction(text="Olá Robbie 🤖", x=50, y=60, overwrite=True, enter=True)
    )
    assert res_unicode.status == "ok"
    assert clipboard.history == ["Olá Robbie 🤖", "keep-me"]
    assert clipboard.value == "keep-me"
    assert ("hotkey", ("ctrl", "v")) in driver.calls
    assert ("press", "enter") in driver.calls

    res_switch = dispatcher.execute(SwitchApplicationAction(app_name="Calculator"))
    assert res_switch.status == "ok"
    assert wm.activated == ["Calculator"]

    assert (
        dispatcher.execute(ScrollAction(x=300, y=300, clicks=-3, element_description="List")).status
        == "ok"
    )
    assert dispatcher.execute(HotkeyAction(keys=["ctrl", "c"])).status == "ok"

    # Controlled WindowsAppLauncher is used (never Win-key Start-search typing!)
    driver.calls.clear()
    assert dispatcher.execute(OpenApplicationAction(target="Notepad")).status == "ok"
    assert launcher.launched == [("Notepad", "application")]
    assert driver.calls == []

    assert dispatcher.execute(WaitAction(seconds=0.05)).status == "ok"

    with pytest.raises(ActionExecutionError, match="outside primary monitor bounds"):
        dispatcher.execute(ClickAction(x=3000, y=50, element_description="Second monitor"))

    uac_dispatcher = WindowsActionDispatcher(
        display_info=_sample_display(),
        input_driver=driver,
        window_manager=FakeWindowManager([], deny_uac=True),
        clipboard=clipboard,
        app_launcher=launcher,
    )
    with pytest.raises(WindowsPermissionError):
        uac_dispatcher.execute(SwitchApplicationAction(app_name="ElevatedApp"))

    driver.calls.clear()
    dry_dispatcher = WindowsActionDispatcher(
        display_info=_sample_display(),
        input_driver=driver,
        window_manager=wm,
        clipboard=clipboard,
        app_launcher=launcher,
        dry_run=True,
    )
    dry_res = dry_dispatcher.execute(ClickAction(x=10, y=10, element_description="Button"))
    assert dry_res.status == "dry_run"
    assert driver.calls == []
