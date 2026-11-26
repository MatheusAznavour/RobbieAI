"""Windows-native Robbie ACI subclassing Agent-S S3 OSWorldACI without touching vendor/Agent-S."""

from __future__ import annotations

import inspect
import logging
import os
import re
from collections.abc import Callable
from typing import Any, Literal

import pytesseract
from gui_agents.s3.agents.grounding import OSWorldACI, agent_action
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY

from robbie.agents.llm_engine import (
    DEFAULT_DESKTOP_MAX_RETRIES,
    build_robbie_lmm_agent,
    call_robbie_llm,
)
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
from robbie.tools.windows import (
    DisplayInfo,
    TesseractStatus,
    detect_tesseract,
    get_windows_display_info,
)

logger = logging.getLogger("robbie.agents.windows_aci")


def call_llm_safe(
    agent: Any,
    temperature: float | None = None,
    use_thinking: bool = False,
    **kwargs: Any,
) -> str:
    """Bounded, non-swallowing replacement for upstream Agent-S call_llm_safe."""
    return call_robbie_llm(
        agent,
        temperature=temperature,
        use_thinking=use_thinking,
        **kwargs,
    )


class GroundingError(RuntimeError):
    """Raised when the visual grounding model returns unusable coordinates."""


class OcrUnavailableError(RuntimeError):
    """Raised when an OCR-dependent action is requested without native Tesseract."""


class UnsupportedActionError(ValueError):
    """Raised when a forbidden or platform-incompatible action is requested."""


BASE_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "type",
        "drag_and_drop",
        "scroll",
        "hotkey",
        "hold_and_press",
        "switch_applications",
        "open",
        "save_to_knowledge",
        "wait",
        "done",
        "fail",
    }
)


class RobbieWindowsACI(OSWorldACI):
    """Robbie-owned Windows ACI extending Agent-S S3 OSWorldACI.

    Key safety and Windows-compatibility invariants:
    - `env` is unconditionally forced to `None` and `self.code_agent` is cleared to `None`.
    - Every `@agent_action` method returns a validated `RobbieAction` Pydantic model
      instead of raw Python/PyAutoGUI code strings intended for `exec()`.
    - Eliminates Linux-only fallbacks (`wmctrl`, `apt-get xclip`, LibreOffice `uno`).
    - Detects native Tesseract availability gracefully without crashing at init.
    - Uses Robbie's bounded OpenAI-compatible engine for OpenAI-compatible grounding
      and text-span models without modifying `vendor/Agent-S`.
    """

    def __init__(
        self,
        *,
        engine_params_for_generation: dict[str, Any],
        engine_params_for_grounding: dict[str, Any],
        display_info: DisplayInfo | None = None,
        tesseract_cmd: str | None = None,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_DESKTOP_MAX_RETRIES,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.display_info = display_info or get_windows_display_info()
        self.tesseract_status: TesseractStatus = detect_tesseract(tesseract_cmd)
        if self.tesseract_status.available and self.tesseract_status.executable_path:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_status.executable_path
        else:
            logger.info(
                "Native Tesseract OCR not detected; OCR text-span grounding will be disabled."
            )

        super().__init__(
            env=None,
            platform="windows",
            engine_params_for_generation=engine_params_for_generation,
            engine_params_for_grounding=engine_params_for_grounding,
            width=self.display_info.primary_width,
            height=self.display_info.primary_height,
        )
        # Replace upstream unbounded LMMAgent engines with Robbie's bounded engine.
        self.grounding_model = build_robbie_lmm_agent(
            engine_params_for_grounding,
            http_timeout_seconds=http_timeout_seconds,
            max_retries=max_retries,
            client_factory=client_factory,
        )
        self.text_span_agent = build_robbie_lmm_agent(
            engine_params_for_generation,
            system_prompt=PROCEDURAL_MEMORY.PHRASE_TO_WORD_COORDS_PROMPT,
            http_timeout_seconds=http_timeout_seconds,
            max_retries=max_retries,
            client_factory=client_factory,
        )
        # Clear unused upstream CodeAgent instance as defense-in-depth.
        self.code_agent = None

    @property
    def allowed_action_methods(self) -> frozenset[str]:
        if self.tesseract_status.available:
            return BASE_ALLOWED_ACTIONS | frozenset({"highlight_text_span"})
        return BASE_ALLOWED_ACTIONS

    @property
    def allowed_method_signatures(self) -> dict[str, inspect.Signature]:
        """Return bound inspect.Signature objects for all allowed action methods."""
        return {
            name: inspect.signature(getattr(self, name)) for name in self.allowed_action_methods
        }

    def _clamp_coordinates(self, coords: list[int]) -> tuple[int, int]:
        max_x = max(0, self.width - 1)
        max_y = max(0, self.height - 1)
        x = max(0, min(int(coords[0]), max_x))
        y = max(0, min(int(coords[1]), max_y))
        return x, y

    def generate_coords(self, ref_expr: str, obs: dict[str, Any]) -> list[int]:
        """Query the visual grounding model and safely parse (x, y) coordinates."""
        if not obs or "screenshot" not in obs:
            raise GroundingError("Screenshot observation is required for visual grounding.")

        self.grounding_model.reset()
        prompt = f"Query:{ref_expr}\nOutput only the coordinate of one point in your response.\n"
        self.grounding_model.add_message(
            text_content=prompt,
            image_content=obs["screenshot"],
            put_text_last=True,
        )

        response = call_llm_safe(self.grounding_model)
        numericals = re.findall(r"\d+", response or "")
        if len(numericals) < 2:
            raise GroundingError(
                f"Grounding model did not return valid (x, y) coordinates for '{ref_expr}'."
            )
        return [int(numericals[0]), int(numericals[1])]

    def resize_coordinates(self, coordinates: list[int]) -> list[int]:
        resized = super().resize_coordinates(coordinates)
        clamped_x, clamped_y = self._clamp_coordinates(resized)
        return [clamped_x, clamped_y]

    def get_ocr_elements(self, b64_image_data: Any) -> tuple[str, list[dict[str, Any]]]:
        if not self.tesseract_status.available:
            raise OcrUnavailableError(
                "Tesseract OCR binary is not installed or not configured via TESSERACT_CMD."
            )
        return super().get_ocr_elements(b64_image_data)

    def generate_text_coords(
        self,
        phrase: str,
        obs: dict[str, Any],
        alignment: str = "",
    ) -> list[int]:
        if not self.tesseract_status.available:
            raise OcrUnavailableError(
                "Text-span OCR grounding is unavailable because Tesseract is not installed."
            )
        ocr_table, ocr_elements = self.get_ocr_elements(obs["screenshot"])
        if not ocr_elements:
            raise GroundingError("OCR detected no text elements on the current screenshot.")

        alignment_prompt = ""
        if alignment == "start":
            alignment_prompt = (
                "**Important**: Output the word id of the FIRST word in the provided phrase.\n"
            )
        elif alignment == "end":
            alignment_prompt = (
                "**Important**: Output the word id of the LAST word in the provided phrase.\n"
            )

        self.text_span_agent.reset()
        self.text_span_agent.add_message(
            alignment_prompt + "Phrase: " + phrase + "\n" + ocr_table,
            role="user",
        )
        self.text_span_agent.add_message(
            "Screenshot:\n",
            image_content=obs["screenshot"],
            role="user",
        )

        response = call_llm_safe(self.text_span_agent)
        numericals = re.findall(r"\d+", response or "")
        if not numericals:
            raise GroundingError(f"OCR text-span agent did not return a word id for '{phrase}'.")

        text_id = int(numericals[-1])
        if text_id < 0 or text_id >= len(ocr_elements):
            raise GroundingError(
                f"OCR text-span agent returned out-of-range word id {text_id} "
                f"(elements={len(ocr_elements)})."
            )

        elem = ocr_elements[text_id]
        if alignment == "start":
            raw = [elem["left"], elem["top"] + (elem["height"] // 2)]
        elif alignment == "end":
            raw = [elem["left"] + elem["width"], elem["top"] + (elem["height"] // 2)]
        else:
            raw = [
                elem["left"] + (elem["width"] // 2),
                elem["top"] + (elem["height"] // 2),
            ]
        cx, cy = self._clamp_coordinates(raw)
        return [cx, cy]

    @agent_action
    def click(
        self,
        element_description: str,
        num_clicks: int = 1,
        button_type: str = "left",
        hold_keys: list[str] | None = None,
    ) -> RobbieAction:
        """Click on the element
        Args:
            element_description:str, a detailed descriptions of which element to click on.
                This description should be at least a full sentence.
            num_clicks:int, number of times to click the element
            button_type:str, which mouse button to press can be "left", "middle", or "right"
            hold_keys:List, list of keys to hold while clicking
        """
        coords = self.generate_coords(element_description, self.obs)
        x, y = self.resize_coordinates(coords)
        return ClickAction(
            x=x,
            y=y,
            num_clicks=num_clicks,
            button=button_type,  # type: ignore[arg-type]
            hold_keys=list(hold_keys or []),
            element_description=element_description,
        )

    @agent_action
    def switch_applications(self, app_code: str) -> RobbieAction:
        """Switch to a different application that is already open
        Args:
            app_code:str the code name of the application to switch to from the provided
                list of open applications
        """
        return SwitchApplicationAction(app_name=app_code)

    @agent_action
    def open(self, app_or_filename: str) -> RobbieAction:
        """Open any application or file with name app_or_filename. Use this action to open
        applications or files on the desktop, do not open manually.
        Args:
            app_or_filename:str, the name of the application or filename to open
        """
        cleaned = app_or_filename.strip()
        _, ext = os.path.splitext(cleaned.lower())
        target_kind: Literal["application", "file"] = (
            "file"
            if ("\\" in cleaned or "/" in cleaned or (ext and ext != ".exe"))
            else "application"
        )
        return OpenApplicationAction(target=cleaned, target_kind=target_kind)

    @agent_action
    def type(
        self,
        element_description: str | None = None,
        text: str = "",
        overwrite: bool = False,
        enter: bool = False,
    ) -> RobbieAction:
        """Type text/unicode into a specific element
        Args:
            element_description:str, a detailed description of which element to enter text in.
                This description should be at least a full sentence.
            text:str, the text to type
            overwrite:bool, Assign it to True if the text should overwrite the existing text,
                otherwise assign it to False. Using this argument clears all text in an element.
            enter:bool, Assign it to True if the enter key should be pressed after typing the
                text, otherwise assign it to False.
        """
        x: int | None = None
        y: int | None = None
        if element_description is not None:
            coords = self.generate_coords(element_description, self.obs)
            x, y = self.resize_coordinates(coords)
        return TypeTextAction(
            text=text,
            x=x,
            y=y,
            overwrite=overwrite,
            enter=enter,
            element_description=element_description,
        )

    @agent_action
    def save_to_knowledge(self, text: list[str]) -> RobbieAction:
        """Save facts, elements, texts, etc. to a long-term knowledge bank for reuse during
        this task. Can be used for copy-pasting text, saving elements, etc.
        Args:
            text:List[str] the text to save to the knowledge
        """
        if not isinstance(text, list) or not all(isinstance(item, str) for item in text):
            raise ValueError("save_to_knowledge requires a flat list of strings.")
        self.notes.extend(text)
        return SaveToKnowledgeAction(notes=list(text))

    @agent_action
    def drag_and_drop(
        self,
        starting_description: str,
        ending_description: str,
        hold_keys: list[str] | None = None,
    ) -> RobbieAction:
        """Drag from the starting description to the ending description
        Args:
            starting_description:str, a very detailed description of where to start the drag
                action. This description should be at least a full sentence.
            ending_description:str, a very detailed description of where to end the drag
                action. This description should be at least a full sentence.
            hold_keys:List list of keys to hold while dragging
        """
        c1 = self.resize_coordinates(self.generate_coords(starting_description, self.obs))
        c2 = self.resize_coordinates(self.generate_coords(ending_description, self.obs))
        return DragAndDropAction(
            start_x=c1[0],
            start_y=c1[1],
            end_x=c2[0],
            end_y=c2[1],
            hold_keys=list(hold_keys or []),
            starting_description=starting_description,
            ending_description=ending_description,
        )

    @agent_action
    def highlight_text_span(
        self,
        starting_phrase: str,
        ending_phrase: str,
        button: str = "left",
    ) -> RobbieAction:
        """Highlight a text span between a provided starting phrase and ending phrase.
        Use this to highlight words, lines, and paragraphs.
        Args:
            starting_phrase:str, the phrase that denotes the start of the text span you want
                to highlight. If you only want to highlight one word, just pass in that single
                word.
            ending_phrase:str, the phrase that denotes the end of the text span you want to
                highlight. If you only want to highlight one word, just pass in that single word.
            button:str, the button to use to highlight the text span. Defaults to "left".
                Can be "left", "right", or "middle".
        """
        c1 = self.generate_text_coords(starting_phrase, self.obs, alignment="start")
        c2 = self.generate_text_coords(ending_phrase, self.obs, alignment="end")
        return HighlightTextSpanAction(
            start_x=c1[0],
            start_y=c1[1],
            end_x=c2[0],
            end_y=c2[1],
            button=button,  # type: ignore[arg-type]
            starting_phrase=starting_phrase,
            ending_phrase=ending_phrase,
        )

    @agent_action
    def scroll(
        self,
        element_description: str,
        clicks: int,
        shift: bool = False,
    ) -> RobbieAction:
        """Scroll the element in the specified direction
        Args:
            element_description:str, a very detailed description of which element to enter
                scroll in. This description should be at least a full sentence.
            clicks:int, the number of clicks to scroll can be positive (up) or negative (down).
            shift:bool, whether to use shift+scroll for horizontal scrolling
        """
        x, y = self.resize_coordinates(self.generate_coords(element_description, self.obs))
        return ScrollAction(
            x=x,
            y=y,
            clicks=clicks,
            horizontal=shift,
            element_description=element_description,
        )

    @agent_action
    def hotkey(self, keys: list[str]) -> RobbieAction:
        """Press a hotkey combination
        Args:
            keys:List the keys to press in combination in a list format (e.g. ['ctrl', 'c'])
        """
        return HotkeyAction(keys=list(keys))

    @agent_action
    def hold_and_press(self, hold_keys: list[str], press_keys: list[str]) -> RobbieAction:
        """Hold a list of keys and press a list of keys
        Args:
            hold_keys:List, list of keys to hold
            press_keys:List, list of keys to press in a sequence
        """
        return HoldAndPressAction(
            hold_keys=list(hold_keys),
            press_keys=list(press_keys),
        )

    @agent_action
    def wait(self, time: float) -> RobbieAction:
        """Wait for a specified amount of time
        Args:
            time:float the amount of time to wait in seconds
        """
        return WaitAction(seconds=float(time))

    @agent_action
    def done(self) -> RobbieAction:
        """End the current task with a success.
        Use this when you believe the entire task has been fully completed.
        """
        return DoneAction()

    @agent_action
    def fail(self) -> RobbieAction:
        """End the current task with a failure.
        Use this when you believe the entire task is impossible to complete.
        """
        return FailAction()

    @agent_action
    def set_cell_values(
        self,
        cell_values: dict[str, Any],
        app_name: str,
        sheet_name: str,
    ) -> RobbieAction:
        raise UnsupportedActionError(
            "set_cell_values is Linux/LibreOffice-only and is disabled in Robbie."
        )

    @agent_action
    def call_code_agent(self, task: str | None = None) -> RobbieAction:
        raise UnsupportedActionError(
            "call_code_agent (LocalEnv arbitrary code execution) is disabled in Robbie."
        )
