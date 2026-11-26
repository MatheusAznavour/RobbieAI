"""Structured desktop action schemas and strict AST-only call parser (zero eval/exec)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

KEY_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z0-9_\-\+\[\];',./\\=`]{1,32}$")
MAX_KEYS_PER_ACTION: Final[int] = 6
MAX_TEXT_LENGTH: Final[int] = 10000
MAX_WAIT_SECONDS: Final[float] = 30.0

MouseButton = Literal["left", "middle", "right"]


def _validate_key_list(keys: list[str], *, allow_empty: bool = True) -> list[str]:
    if not allow_empty and not keys:
        raise ValueError("At least one key must be specified.")
    if len(keys) > MAX_KEYS_PER_ACTION:
        raise ValueError(f"At most {MAX_KEYS_PER_ACTION} keys are allowed per action.")
    normalized: list[str] = []
    for raw_key in keys:
        key = raw_key.strip().lower()
        if not KEY_NAME_PATTERN.match(key):
            raise ValueError(f"Unsupported or invalid key name: {raw_key!r}")
        normalized.append(key)
    return normalized


class ClickAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["click"] = "click"
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    num_clicks: int = Field(default=1, ge=1, le=3)
    button: MouseButton = "left"
    hold_keys: list[str] = Field(default_factory=list)
    element_description: str = Field(min_length=1, max_length=1000)

    @field_validator("hold_keys")
    @classmethod
    def validate_hold_keys(cls, value: list[str]) -> list[str]:
        return _validate_key_list(value, allow_empty=True)


class TypeTextAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["type"] = "type"
    text: str = Field(max_length=MAX_TEXT_LENGTH)
    x: int | None = Field(default=None, ge=0)
    y: int | None = Field(default=None, ge=0)
    overwrite: bool = False
    enter: bool = False
    element_description: str | None = Field(default=None, max_length=1000)


class DragAndDropAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["drag_and_drop"] = "drag_and_drop"
    start_x: int = Field(ge=0)
    start_y: int = Field(ge=0)
    end_x: int = Field(ge=0)
    end_y: int = Field(ge=0)
    hold_keys: list[str] = Field(default_factory=list)
    starting_description: str = Field(min_length=1, max_length=1000)
    ending_description: str = Field(min_length=1, max_length=1000)

    @field_validator("hold_keys")
    @classmethod
    def validate_hold_keys(cls, value: list[str]) -> list[str]:
        return _validate_key_list(value, allow_empty=True)


class HighlightTextSpanAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["highlight_text_span"] = "highlight_text_span"
    start_x: int = Field(ge=0)
    start_y: int = Field(ge=0)
    end_x: int = Field(ge=0)
    end_y: int = Field(ge=0)
    button: MouseButton = "left"
    starting_phrase: str = Field(min_length=1, max_length=500)
    ending_phrase: str = Field(min_length=1, max_length=500)


class ScrollAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["scroll"] = "scroll"
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    clicks: int = Field(ge=-500, le=500)
    horizontal: bool = False
    element_description: str = Field(min_length=1, max_length=1000)


class HotkeyAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["hotkey"] = "hotkey"
    keys: list[str]

    @field_validator("keys")
    @classmethod
    def validate_keys(cls, value: list[str]) -> list[str]:
        return _validate_key_list(value, allow_empty=False)


class HoldAndPressAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["hold_and_press"] = "hold_and_press"
    hold_keys: list[str]
    press_keys: list[str]

    @field_validator("hold_keys", "press_keys")
    @classmethod
    def validate_keys(cls, value: list[str]) -> list[str]:
        return _validate_key_list(value, allow_empty=False)


class SwitchApplicationAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["switch_applications"] = "switch_applications"
    app_name: str = Field(min_length=1, max_length=256)

    @field_validator("app_name")
    @classmethod
    def validate_app_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Application name must not be empty.")
        return cleaned


class OpenApplicationAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["open"] = "open"
    target: str = Field(min_length=1, max_length=256)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Target application or filename must not be empty.")
        if any(ch in cleaned for ch in ("\r", "\n", "\x00")):
            raise ValueError("Control characters are not allowed in target name.")
        return cleaned


class SaveToKnowledgeAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["save_to_knowledge"] = "save_to_knowledge"
    notes: list[str] = Field(min_length=1, max_length=50)


class WaitAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["wait"] = "wait"
    seconds: float = Field(ge=0.0, le=MAX_WAIT_SECONDS)


class DoneAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["done"] = "done"


class FailAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["fail"] = "fail"


RobbieAction = Annotated[
    ClickAction
    | TypeTextAction
    | DragAndDropAction
    | HighlightTextSpanAction
    | ScrollAction
    | HotkeyAction
    | HoldAndPressAction
    | SwitchApplicationAction
    | OpenApplicationAction
    | SaveToKnowledgeAction
    | WaitAction
    | DoneAction
    | FailAction,
    Field(discriminator="kind"),
]

ROBBIE_ACTION_TYPES: Final[tuple[type[BaseModel], ...]] = (
    ClickAction,
    TypeTextAction,
    DragAndDropAction,
    HighlightTextSpanAction,
    ScrollAction,
    HotkeyAction,
    HoldAndPressAction,
    SwitchApplicationAction,
    OpenApplicationAction,
    SaveToKnowledgeAction,
    WaitAction,
    DoneAction,
    FailAction,
)


class UnsafeActionCodeError(ValueError):
    """Raised when model-generated action code fails strict AST allowlist parsing."""


@dataclass(frozen=True)
class ParsedAgentCall:
    """Validated representation of a single `agent.<method>(*args, **kwargs)` call."""

    method_name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _extract_safe_literal(node: ast.AST) -> Any:
    """Safely evaluate only static Python literals (never calls, names, or expressions)."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return node.value
        raise UnsafeActionCodeError(f"Unsupported constant type: {type(node.value).__name__}")

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        if isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)):
            val = node.operand.value
            return -val if isinstance(node.op, ast.USub) else +val
        raise UnsafeActionCodeError("Unary operators are only allowed on numeric literals.")

    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_extract_safe_literal(elt) for elt in node.elts]
        return list(items) if isinstance(node, ast.List) else tuple(items)

    raise UnsafeActionCodeError(
        f"Non-literal expression '{type(node).__name__}' is forbidden in agent action arguments."
    )


def parse_agent_call_ast(
    code: str,
    allowed_methods: frozenset[str],
) -> ParsedAgentCall:
    """Parse a single `agent.<method>(...)` call using AST only — never `eval()`.

    Rejects:
    - Multiple statements or non-call expressions
    - Any receiver other than the literal identifier `agent`
    - Private/dunder methods or methods outside `allowed_methods`
    - Star-args (`*args`, `**kwargs`) and non-literal argument expressions
    """
    cleaned = code.strip()
    if not cleaned:
        raise UnsafeActionCodeError("Action code is empty.")

    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError as exc:
        raise UnsafeActionCodeError(f"Invalid action syntax: {exc.msg}") from exc

    call_node = tree.body
    if not isinstance(call_node, ast.Call):
        raise UnsafeActionCodeError("Action code must be a single `agent.<method>(...)` call.")

    func_node = call_node.func
    if (
        not isinstance(func_node, ast.Attribute)
        or not isinstance(func_node.value, ast.Name)
        or func_node.value.id != "agent"
    ):
        raise UnsafeActionCodeError("Only calls on the `agent` object are permitted.")

    method_name = func_node.attr
    if method_name.startswith("_") or method_name not in allowed_methods:
        raise UnsafeActionCodeError(f"Action method '{method_name}' is not allowed.")

    args: list[Any] = []
    for arg_node in call_node.args:
        if isinstance(arg_node, ast.Starred):
            raise UnsafeActionCodeError("Starred arguments (*args) are not permitted.")
        args.append(_extract_safe_literal(arg_node))

    kwargs: dict[str, Any] = {}
    for kw_node in call_node.keywords:
        if kw_node.arg is None:
            raise UnsafeActionCodeError("Keyword unpacking (**kwargs) is not permitted.")
        kwargs[kw_node.arg] = _extract_safe_literal(kw_node.value)

    return ParsedAgentCall(method_name=method_name, args=tuple(args), kwargs=kwargs)
