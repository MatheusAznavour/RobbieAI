"""Fail-closed execution safety policy and Windows application target classifier."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Final, Literal

from robbie.core.security import CREDENTIAL_PATTERNS
from robbie.tools.actions import (
    ClickAction,
    DragAndDropAction,
    HoldAndPressAction,
    HotkeyAction,
    OpenApplicationAction,
    RobbieAction,
    ScrollAction,
    SwitchApplicationAction,
    TypeTextAction,
)

RiskLevel = Literal["low", "medium", "high", "critical"]

RISK_ORDER: Final[dict[RiskLevel, int]] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


@dataclass(frozen=True)
class SafetyDecision:
    """Policy evaluation result determining whether an action can run or needs approval."""

    allowed: bool
    requires_approval: bool
    risk_level: RiskLevel
    category: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OpenTargetClassification:
    """Classification of an OpenApplicationAction target."""

    allowed: bool
    requires_approval: bool
    risk_level: RiskLevel
    category: str
    resolved_executable: str | None = None
    reason: str = ""


# Strictly benign, fixed-binary Windows applications that may launch without approval
# when invoked under a non-sensitive task instruction (never with CLI arguments).
ALLOWLISTED_SAFE_APPS: Final[dict[str, str]] = {
    "notepad": "notepad.exe",
    "notepad.exe": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "calc.exe": "calc.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "mspaint.exe": "mspaint.exe",
    "wordpad": "write.exe",
}

# Command interpreters, administrative tools, and Windows LOLBins — always blocked,
# even with user approval (approval never grants arbitrary shell execution).
BLOCKED_INTERPRETERS_AND_LOLBINS: Final[frozenset[str]] = frozenset(
    {
        "cmd",
        "powershell",
        "pwsh",
        "wt",
        "windows terminal",
        "bash",
        "wsl",
        "wslg",
        "sh",
        "zsh",
        "mshta",
        "rundll32",
        "regsvr32",
        "certutil",
        "bitsadmin",
        "cscript",
        "wscript",
        "msiexec",
        "schtasks",
        "at",
        "sc",
        "net",
        "net1",
        "wmic",
        "reg",
        "regedit",
        "regedt32",
        "taskkill",
        "takeown",
        "icacls",
        "cacls",
        "bcdedit",
        "diskpart",
        "format",
        "vssadmin",
        "wbadmin",
        "cipher",
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "ftp",
        "telnet",
        "forfiles",
        "pcalua",
        "syncappvpublishingserver",
        "installutil",
        "msbuild",
        "odbcconf",
        "cmstp",
        "mavinject",
        "hh",
        "control",
        "mmc",
        "msconfig",
        "taskmgr",
        "secpol",
        "gpedit",
        "dcomcnfg",
        "netsh",
        "whoami",
        "python",
        "pythonw",
        "py",
        "node",
        "npm",
        "npx",
        "pip",
        "winget",
        "choco",
        "scoop",
    }
)

BLOCKED_EXECUTABLE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".exe",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".psd1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".hta",
        ".scr",
        ".pif",
        ".reg",
        ".com",
        ".msi",
        ".msp",
        ".mst",
        ".jar",
        ".cpl",
        ".msc",
        ".inf",
        ".lnk",
    }
)

SHELL_METACHAR_PATTERN: Final[re.Pattern[str]] = re.compile(r"[&|;><`$%^\"'(){}*?]")
CLI_SWITCH_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+[-/][a-zA-Z0-9?]")
URL_SCHEME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(://|^(?:https?|ftp|file|javascript|vbscript|data|mailto|ms-[a-z0-9-]+):)",
    re.IGNORECASE,
)


def classify_open_target(target: str) -> OpenTargetClassification:
    """Strictly classify an OpenApplicationAction target.

    Hard-blocks command interpreters, Windows LOLBins, CLI arguments/switches,
    shell metacharacters, URLs, UNC network paths, and arbitrary executables/scripts.
    Requires explicit approval for any non-allowlisted application or document file.
    """
    cleaned = target.strip()
    lower = cleaned.lower()

    if not cleaned:
        return OpenTargetClassification(
            allowed=False,
            requires_approval=False,
            risk_level="critical",
            category="blocked_command_execution",
            reason="Empty launch target is forbidden.",
        )

    if URL_SCHEME_PATTERN.search(cleaned):
        return OpenTargetClassification(
            allowed=False,
            requires_approval=False,
            risk_level="critical",
            category="blocked_command_execution",
            reason="URLs and protocol handlers are not permitted in OpenApplicationAction.",
        )

    if cleaned.startswith("\\\\") or cleaned.startswith("//"):
        return OpenTargetClassification(
            allowed=False,
            requires_approval=False,
            risk_level="critical",
            category="blocked_command_execution",
            reason="UNC network paths are not permitted in OpenApplicationAction.",
        )

    if SHELL_METACHAR_PATTERN.search(cleaned):
        return OpenTargetClassification(
            allowed=False,
            requires_approval=False,
            risk_level="critical",
            category="blocked_command_execution",
            reason="Shell metacharacters or quotes are forbidden in application targets.",
        )

    if CLI_SWITCH_PATTERN.search(cleaned):
        return OpenTargetClassification(
            allowed=False,
            requires_approval=False,
            risk_level="critical",
            category="blocked_command_execution",
            reason="Command-line flags/arguments are forbidden in application targets.",
        )

    # Check exact allowlist BEFORE generic .exe rejection (e.g., 'notepad.exe').
    if lower in ALLOWLISTED_SAFE_APPS:
        return OpenTargetClassification(
            allowed=True,
            requires_approval=False,
            risk_level="low",
            category="allowlisted_application",
            resolved_executable=ALLOWLISTED_SAFE_APPS[lower],
            reason="Explicitly allowlisted benign Windows application.",
        )

    # Check full name, first token, and basename stem against blocked interpreters/LOLBins.
    first_token = lower.split()[0]
    first_basename = os.path.basename(first_token.replace("\\", "/"))
    first_stem = os.path.splitext(first_basename)[0]
    full_basename = os.path.basename(lower.replace("\\", "/"))
    full_stem = os.path.splitext(full_basename)[0]

    for candidate in (lower, first_token, first_basename, first_stem, full_basename, full_stem):
        if candidate in BLOCKED_INTERPRETERS_AND_LOLBINS:
            return OpenTargetClassification(
                allowed=False,
                requires_approval=False,
                risk_level="critical",
                category="blocked_command_execution",
                reason=(
                    f"Command interpreter or Windows LOLBin '{candidate}' is explicitly blocked."
                ),
            )

    # Reject executable/script/installer extensions on both full target and first token.
    for token in (cleaned, first_token):
        _, ext = os.path.splitext(token.lower())
        if ext in BLOCKED_EXECUTABLE_EXTENSIONS:
            return OpenTargetClassification(
                allowed=False,
                requires_approval=False,
                risk_level="critical",
                category="blocked_command_execution",
                reason=f"Executable or script extension '{ext}' is blocked from direct launch.",
            )

    # Reject anything that looks like a command followed by file/URL arguments
    # (e.g., "app foo.txt" or "foo http://..."). Paths with spaces inside a single
    # filename are checked safely because paths contain directory separators or extensions.
    tokens = cleaned.split()
    if len(tokens) > 1 and any(
        "." in tok and not os.path.splitext(cleaned)[1] for tok in tokens[1:]
    ):
        return OpenTargetClassification(
            allowed=False,
            requires_approval=False,
            risk_level="critical",
            category="blocked_command_execution",
            reason="Multi-token command-like invocation is not permitted.",
        )

    # All remaining non-allowlisted applications or document files fail closed to approval.
    return OpenTargetClassification(
        allowed=True,
        requires_approval=True,
        risk_level="high",
        category="non_allowlisted_open",
        reason=(
            f"Non-allowlisted application or file target '{cleaned}' requires explicit approval."
        ),
    )


SENSITIVE_CATEGORY_PATTERNS: Final[tuple[tuple[str, RiskLevel, re.Pattern[str]], ...]] = (
    (
        "file_deletion",
        "high",
        re.compile(
            r"\b(delete|remove|erase|wipe|format|shred|trash|recycle\s*bin|"
            r"replace|overwrite|discard|uninstall|clear\s+all|reset|"
            r"rm\s+-|del\s+/|rmdir|remove-item|clear-recyclebin|"
            r"excluir|apagar|remover|substituir|sobrescrever|"
            r"l[öo]schen|entfernen|supprimer|effacer|eliminar|borrar)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "software_installation",
        "high",
        re.compile(
            r"\b(installs?|downloads?|upgrades?|updates?|setup|msiexec|winget|choco|scoop|"
            r"pip\s+install|npm\s+install|run\s+as\s+administrator|extract|unzip|"
            r"instalar|baixar|herunterladen|t[ée]l[ée]charger|descargar)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "security_settings",
        "critical",
        re.compile(
            r"\b(windows\s+security|defender|firewall|regedit|registry|"
            r"uac|user\s+account\s+control|group\s+policy|gpedit|secpol|"
            r"bitlocker|certificates?|permissions?|disable\s+protection|admin|"
            r"seguran[çc]a|permiss[õo]es|sicherheit|s[ée]curit[ée])\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_access",
        "critical",
        re.compile(
            r"\b(passwords?|passphrases?|credentials?|api[_\s-]?keys?|secrets?|tokens?|"
            r"ssh[_\s-]?keys?|id_rsa|\.env|1password|bitwarden|keepass|lastpass|"
            r"vaults?|private\s+keys?|sign\s+in|log\s+in|login|authenticate|"
            r"senhas?|credencia(?:l|is)|passwort|mot\s+de\s+passe|contrase[ñn]as?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "financial_transaction",
        "critical",
        re.compile(
            r"\b(pay|payments?|checkout|purchases?|buy|place\s+order|orders?|"
            r"transfers?|wire|send\s+money|banks?|banking|chase|stripe|paypal|"
            r"venmo|zelle|billing|invoices?|credit\s+cards?|debit\s+cards?|card\s+numbers?|"
            r"deposits?|withdraw(?:al)?|trade|pagar|pagamentos?|transferir|banco|compras?|"
            r"bezahlen|zahlung|payer|paiement)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "committing_or_external_action",
        "high",
        re.compile(
            r"\b(confirm|submit|send|apply|ok|yes|proceed|continue|accept|"
            r"approve|agree|post|publish|share|save|finish|complete|authorize|"
            r"confirmar|enviar|aceitar|salvar|publicar|compartilhar|"
            r"best[äa]tigen|senden|confirmer|envoyer|guardar)\b",
            re.IGNORECASE,
        ),
    ),
)

# Read-only UI navigation/inspection descriptors that may execute without approval
# ONLY when no sensitive category or sensitive task instruction is present.
READ_ONLY_UI_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(menu|tab|help|search|sidebar|address\s+bar|scroll|scrollbar|"
    r"minimize|maximize|view|preview|zoom|toolbar|breadcrumb|navigation|"
    r"heading|panel|explorer\s+tree|folder\s+tree|window\s+title)\b",
    re.IGNORECASE,
)

BLOCKED_SYSTEM_HOTKEYS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"ctrl", "alt", "delete"}),
        frozenset({"ctrl", "alt", "del"}),
        frozenset({"win", "l"}),
    }
)

# Strictly benign, non-committing navigation/copy hotkeys allowed without approval
# ONLY when the task instruction itself is benign.
SAFE_READ_ONLY_HOTKEYS: Final[frozenset[frozenset[str]]] = frozenset(
    {
        frozenset({"ctrl", "c"}),
        frozenset({"ctrl", "f"}),
        frozenset({"ctrl", "tab"}),
        frozenset({"ctrl", "shift", "tab"}),
        frozenset({"alt", "tab"}),
        frozenset({"escape"}),
        frozenset({"esc"}),
        frozenset({"tab"}),
        frozenset({"up"}),
        frozenset({"down"}),
        frozenset({"left"}),
        frozenset({"right"}),
        frozenset({"pageup"}),
        frozenset({"pagedown"}),
        frozenset({"home"}),
        frozenset({"end"}),
    }
)


def _match_sensitive_text(text: str) -> tuple[str, RiskLevel, list[str]] | None:
    if not text:
        return None
    reasons: list[str] = []
    highest_risk: RiskLevel = "low"
    primary_category = "general"

    for category, risk, pattern in SENSITIVE_CATEGORY_PATTERNS:
        if pattern.search(text):
            reasons.append(f"Matched sensitive category '{category}'.")
            if RISK_ORDER[risk] > RISK_ORDER[highest_risk]:
                highest_risk = risk
                primary_category = category

    for cred_pattern in CREDENTIAL_PATTERNS:
        if cred_pattern.search(text):
            reasons.append("Matched recognizable credential or payment card pattern.")
            if RISK_ORDER["critical"] > RISK_ORDER[highest_risk]:
                highest_risk = "critical"
                primary_category = "credential_access"
            break

    if not reasons:
        return None
    return primary_category, highest_risk, reasons


def _escalate_with_instruction(
    base: SafetyDecision,
    instruction_match: tuple[str, RiskLevel, list[str]] | None,
    action_kind: str,
) -> SafetyDecision:
    """Propagate sensitive task-instruction context to any allowed action."""
    if not base.allowed or instruction_match is None:
        return base

    instr_cat, instr_risk, instr_reasons = instruction_match
    merged_risk: RiskLevel = (
        instr_risk if RISK_ORDER[instr_risk] > RISK_ORDER[base.risk_level] else base.risk_level
    )
    merged_cat = (
        instr_cat if RISK_ORDER[instr_risk] > RISK_ORDER[base.risk_level] else base.category
    )
    merged_reasons = tuple(
        dict.fromkeys(
            [
                *base.reasons,
                *instr_reasons,
                f"Action '{action_kind}' occurs under a sensitive task instruction.",
            ]
        )
    )
    return SafetyDecision(
        allowed=True,
        requires_approval=True,
        risk_level=merged_risk,
        category=merged_cat,
        reasons=merged_reasons,
    )


class ActionSafetyPolicy:
    """Fail-closed safety policy enforcing non-bypassable blocking and approval rules."""

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

        instruction_match = _match_sensitive_text(task_instruction)

        if isinstance(action, OpenApplicationAction):
            classification = classify_open_target(action.target)
            if not classification.allowed:
                return SafetyDecision(
                    allowed=False,
                    requires_approval=False,
                    risk_level="critical",
                    category=classification.category,
                    reasons=(classification.reason,),
                )
            base = SafetyDecision(
                allowed=True,
                requires_approval=classification.requires_approval,
                risk_level=classification.risk_level,
                category=classification.category,
                reasons=(classification.reason,) if classification.requires_approval else (),
            )
            target_match = _match_sensitive_text(action.target)
            if target_match:
                base = _escalate_with_instruction(base, target_match, action.kind)
            return _escalate_with_instruction(base, instruction_match, action.kind)

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
            if key_set in SAFE_READ_ONLY_HOTKEYS:
                base = SafetyDecision(
                    allowed=True,
                    requires_approval=False,
                    risk_level="low",
                    category="keyboard_navigation",
                )
                return _escalate_with_instruction(base, instruction_match, action.kind)

            # Fail closed for Enter, Delete, Ctrl+Enter, Alt+F4, Win+R, Ctrl+S,
            # and any non-allowlisted hotkey combination.
            is_delete = "delete" in key_set or "del" in key_set
            base = SafetyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="high",
                category="file_deletion" if is_delete else "committing_hotkey",
                reasons=(f"Hotkey {action.keys} is state-committing or non-allowlisted.",),
            )
            return _escalate_with_instruction(base, instruction_match, action.kind)

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
            base = SafetyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="high",
                category="file_deletion"
                if ("delete" in combined or "del" in combined)
                else "committing_hotkey",
                reasons=(
                    f"Hold-and-press sequence {action.hold_keys}+{action.press_keys} "
                    "requires explicit approval.",
                ),
            )
            return _escalate_with_instruction(base, instruction_match, action.kind)

        if isinstance(action, DragAndDropAction):
            # Drag-and-drop can delete, move, overwrite, or upload files; fail closed.
            combined = f"{action.starting_description} {action.ending_description}"
            match = _match_sensitive_text(combined)
            if match:
                cat, risk, reasons = match
                base = SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level=risk,
                    category=cat,
                    reasons=tuple(reasons),
                )
            else:
                base = SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level="high",
                    category="drag_and_drop_mutation",
                    reasons=(
                        "Drag-and-drop operations may move, overwrite, or delete files "
                        "and require explicit user approval.",
                    ),
                )
            return _escalate_with_instruction(base, instruction_match, action.kind)

        if isinstance(action, TypeTextAction):
            reasons_list: list[str] = []
            risk: RiskLevel = "low"
            category = "text_entry"
            requires_approval = False

            if action.overwrite:
                requires_approval = True
                risk = "high"
                category = "file_or_data_overwrite"
                reasons_list.append("overwrite=True clears existing content and requires approval.")

            if action.enter:
                requires_approval = True
                if RISK_ORDER["high"] > RISK_ORDER[risk]:
                    risk = "high"
                    category = "committing_or_external_action"
                reasons_list.append("enter=True submits text/commands and requires approval.")

            inspectable = action.text
            if action.element_description:
                inspectable = f"{action.element_description} {inspectable}"
            match = _match_sensitive_text(inspectable)
            if match:
                m_cat, m_risk, m_reasons = match
                requires_approval = True
                reasons_list.extend(m_reasons)
                if RISK_ORDER[m_risk] > RISK_ORDER[risk] or (
                    RISK_ORDER[m_risk] == RISK_ORDER[risk]
                    and category in {"text_entry", "committing_or_external_action"}
                ):
                    risk = m_risk
                    category = m_cat

            base = SafetyDecision(
                allowed=True,
                requires_approval=requires_approval,
                risk_level=risk,
                category=category,
                reasons=tuple(dict.fromkeys(reasons_list)),
            )
            return _escalate_with_instruction(base, instruction_match, action.kind)

        if isinstance(action, ClickAction):
            match = _match_sensitive_text(action.element_description)
            if match:
                m_cat, m_risk, m_reasons = match
                base = SafetyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_level=m_risk,
                    category=m_cat,
                    reasons=tuple(m_reasons),
                )
                return _escalate_with_instruction(base, instruction_match, action.kind)

            # Fail closed on visually/semantically ambiguous clicks unless the target
            # is clearly a read-only navigation/inspection control.
            if READ_ONLY_UI_PATTERN.search(action.element_description):
                base = SafetyDecision(
                    allowed=True,
                    requires_approval=False,
                    risk_level="low",
                    category="gui_navigation",
                )
                return _escalate_with_instruction(base, instruction_match, action.kind)

            base = SafetyDecision(
                allowed=True,
                requires_approval=True,
                risk_level="medium",
                category="ambiguous_ui_action",
                reasons=(
                    "Click target could not be classified as strictly read-only; "
                    "failing closed to require user approval.",
                ),
            )
            return _escalate_with_instruction(base, instruction_match, action.kind)

        if isinstance(action, (ScrollAction, SwitchApplicationAction)):
            base = SafetyDecision(
                allowed=True,
                requires_approval=False,
                risk_level="low",
                category="gui_navigation",
            )
            return _escalate_with_instruction(base, instruction_match, action.kind)

        # Any other action type (e.g., HighlightTextSpanAction) inherits instruction context.
        base = SafetyDecision(
            allowed=True,
            requires_approval=False,
            risk_level="low",
            category="gui_interaction",
        )
        return _escalate_with_instruction(base, instruction_match, action.kind)
