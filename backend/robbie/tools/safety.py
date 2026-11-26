"""Mandatory execution safety policy and approval classification for Robbie actions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Literal

from robbie.tools.actions import (
    ClickAction,
    DragAndDropAction,
    HoldAndPressAction,
    HotkeyAction,
    OpenApplicationAction,
    RobbieAction,
    TypeTextAction,
)

RiskLevel = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class SafetyDecision:
    """Policy evaluation result determining whether an action can run or needs approval."""

    allowed: bool
    requires_approval: bool
    risk_level: RiskLevel
    category: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


SENSITIVE_CATEGORY_PATTERNS: Final[tuple[tuple[str, RiskLevel, re.Pattern[str]], ...]] = (
    (
        "file_deletion",
        "high",
        re.compile(
            r"\b(delete|remove|erase|wipe|format|shred|empty\s+recycle\s+bin|"
            r"rm\s+-|del\s+/|rmdir|remove-item|clear-recyclebin)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "software_installation",
        "high",
        re.compile(
            r"\b(install|uninstall|setup\.exe|msiexec|winget|choco|scoop|"
            r"pip\s+install|npm\s+install|run\s+as\s+administrator)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "security_settings",
        "critical",
        re.compile(
            r"\b(windows\s+security|defender|firewall|regedit|registry|"
            r"uac|user\s+account\s+control|group\s+policy|gpedit|secpol|"
            r"bitlocker|certificate|permissions|disable\s+protection)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_access",
        "critical",
        re.compile(
            r"\b(password|passphrase|credential\s+manager|api[_\s-]?key|"
            r"secret|token|ssh[_\s-]?key|id_rsa|\.env|1password|bitwarden|"
            r"keepass|lastpass|vault|private\s+key)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "financial_transaction",
        "critical",
        re.compile(
            r"\b(pay\s+now|checkout|purchase|buy\s+now|place\s+order|"
            r"wire\s+transfer|send\s+money|bank\s+transfer|credit\s+card|"
            r"billing|confirm\s+payment|submit\s+payment)\b",
            re.IGNORECASE,
        ),
    ),
)

SENSITIVE_OPEN_TARGETS: Final[re.Pattern[str]] = re.compile(
    r"(^|[\\/\s])(cmd|powershell|pwsh|wt|windows\s+terminal|regedit|mmc|"
    r"control|msconfig|taskmgr|msiexec|wscript|cscript|bash|wsl)(\.exe)?$|"
    r"\.(exe|msi|bat|cmd|ps1|vbs|reg|scr)$",
    re.IGNORECASE,
)

DESTRUCTIVE_HOTKEYS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"shift", "delete"}),
        frozenset({"shift", "del"}),
        frozenset({"ctrl", "d"}),
        frozenset({"alt", "f4"}),
    }
)

BLOCKED_SYSTEM_HOTKEYS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"ctrl", "alt", "delete"}),
        frozenset({"ctrl", "alt", "del"}),
        frozenset({"win", "l"}),
    }
)


def _match_sensitive_text(text: str) -> tuple[str, RiskLevel, list[str]] | None:
    reasons: list[str] = []
    highest_risk: RiskLevel = "low"
    primary_category = "general"
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    for category, risk, pattern in SENSITIVE_CATEGORY_PATTERNS:
        if pattern.search(text):
            reasons.append(f"Matched sensitive category '{category}'.")
            if risk_order[risk] > risk_order[highest_risk]:
                highest_risk = risk
                primary_category = category

    if not reasons:
        return None
    return primary_category, highest_risk, reasons


class ActionSafetyPolicy:
    """Enforces non-bypassable approval and blocking rules on structured actions."""

    def evaluate(
        self,
        action: RobbieAction,
        *,
        task_instruction: str = "",
    ) -> SafetyDecision:
        if action.kind in {"wait", "save_to_knowledge", "done", "fail"}:
            return SafetyDecision(
                allowed=True,
                requires_approval=False,
                risk_level="low",
                category="control",
            )

        if isinstance(action, HotkeyAction):
            key_set = frozenset(action.keys)
            if key_set in BLOCKED_SYSTEM_HOTKEYS:
                return SafetyDecision(
                    allowed=False,
                    requires_approval=False,
                    risk_level="critical",
                    category="blocked_system_hotkey",
                    reasons=("System-level lock/security hotkey is blocked.",),
                )
            if key_set in DESTRUCTIVE_HOTKEYS or "delete" in key_set or "del" in key_set:
                return SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level="high",
                    category="file_deletion",
                    reasons=(
                        f"Potentially destructive hotkey {action.keys} requires user approval.",
                    ),
                )
            return SafetyDecision(
                allowed=True,
                requires_approval=False,
                risk_level="low",
                category="keyboard",
            )

        if isinstance(action, HoldAndPressAction):
            combined = frozenset(action.hold_keys) | frozenset(action.press_keys)
            if combined in BLOCKED_SYSTEM_HOTKEYS:
                return SafetyDecision(
                    allowed=False,
                    requires_approval=False,
                    risk_level="critical",
                    category="blocked_system_hotkey",
                    reasons=("System-level lock/security key combination is blocked.",),
                )
            if "delete" in combined or "del" in combined:
                return SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level="high",
                    category="file_deletion",
                    reasons=("Key sequence containing Delete requires user approval.",),
                )
            return SafetyDecision(
                allowed=True,
                requires_approval=False,
                risk_level="low",
                category="keyboard",
            )

        if isinstance(action, OpenApplicationAction):
            if SENSITIVE_OPEN_TARGETS.search(action.target):
                return SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level="critical",
                    category="software_installation",
                    reasons=(
                        f"Opening shell, system tool, or executable '{action.target}' "
                        "requires explicit user approval.",
                    ),
                )
            text_match = _match_sensitive_text(action.target)
            if text_match:
                category, risk, reasons = text_match
                return SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level=risk,
                    category=category,
                    reasons=tuple(reasons),
                )
            return SafetyDecision(
                allowed=True,
                requires_approval=False,
                risk_level="medium",
                category="application_launch",
            )

        inspectable_parts: list[str] = []
        if isinstance(action, ClickAction):
            inspectable_parts.append(action.element_description)
        elif isinstance(action, TypeTextAction):
            inspectable_parts.append(action.text)
            if action.element_description:
                inspectable_parts.append(action.element_description)
        elif isinstance(action, DragAndDropAction):
            inspectable_parts.append(action.starting_description)
            inspectable_parts.append(action.ending_description)

        # Evaluate action-specific text first, plus instruction context for clicks/typing
        # on sensitive workflows (e.g., clicking "Confirm" during a financial or deletion task).
        combined_text = " ".join(inspectable_parts)
        match = _match_sensitive_text(combined_text)
        if match:
            category, risk, reasons = match
            return SafetyDecision(
                allowed=True,
                requires_approval=True,
                risk_level=risk,
                category=category,
                reasons=tuple(reasons),
            )

        if task_instruction and isinstance(action, (ClickAction, TypeTextAction)):
            instr_match = _match_sensitive_text(task_instruction)
            if instr_match:
                category, risk, reasons = instr_match
                return SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level=risk,
                    category=category,
                    reasons=tuple(
                        [
                            *reasons,
                            f"Action '{action.kind}' is part of a sensitive task instruction.",
                        ]
                    ),
                )

        return SafetyDecision(
            allowed=True,
            requires_approval=False,
            risk_level="low",
            category="gui_interaction",
        )
