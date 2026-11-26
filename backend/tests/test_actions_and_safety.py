"""Tests for structured action schemas, AST-only parsing, and safety policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from robbie.agents.windows_aci import BASE_ALLOWED_ACTIONS
from robbie.tools.actions import (
    ClickAction,
    HotkeyAction,
    OpenApplicationAction,
    TypeTextAction,
    UnsafeActionCodeError,
    WaitAction,
    parse_agent_call_ast,
)
from robbie.tools.safety import ActionSafetyPolicy


def test_ast_parser_accepts_valid_literal_agent_calls() -> None:
    parsed_click = parse_agent_call_ast(
        'agent.click("The blue Save button", 1, "left", [])',
        BASE_ALLOWED_ACTIONS,
    )
    assert parsed_click.method_name == "click"
    assert parsed_click.args == ("The blue Save button", 1, "left", [])
    assert parsed_click.kwargs == {}

    parsed_type = parse_agent_call_ast(
        'agent.type(element_description="Search box", text="hello", enter=True)',
        BASE_ALLOWED_ACTIONS,
    )
    assert parsed_type.method_name == "type"
    assert parsed_type.kwargs == {
        "element_description": "Search box",
        "text": "hello",
        "enter": True,
    }

    parsed_scroll = parse_agent_call_ast(
        'agent.scroll("Document area", -5)',
        BASE_ALLOWED_ACTIONS,
    )
    assert parsed_scroll.args == ("Document area", -5)


@pytest.mark.parametrize(
    "malicious_code",
    [
        "__import__('os').system('calc')",
        "agent.click(__import__('os').system('calc'))",
        "agent.call_code_agent('rm -rf /')",
        "agent.set_cell_values({'A1': 'x'}, 'Sheet', 'Sheet1')",
        "agent.__class__.__bases__",
        "exec('import os')",
        "eval('1+1')",
        "agent.click('ok'); import os",
        "agent.click(*['x'])",
        "agent.click(**{'element_description': 'x'})",
        "other.click('button')",
        "agent.wait(1 + 2)",
        "",
    ],
)
def test_ast_parser_rejects_unsafe_or_unsupported_code(malicious_code: str) -> None:
    with pytest.raises(UnsafeActionCodeError):
        parse_agent_call_ast(malicious_code, BASE_ALLOWED_ACTIONS)


def test_action_models_enforce_strict_schema_validation() -> None:
    with pytest.raises(ValidationError):
        ClickAction(x=-1, y=10, element_description="Negative coord")

    with pytest.raises(ValidationError):
        ClickAction(x=10, y=10, num_clicks=5, element_description="Too many clicks")

    with pytest.raises(ValidationError):
        WaitAction(seconds=120.0)

    with pytest.raises(ValidationError):
        HotkeyAction(keys=["ctrl", "bad key with spaces"])

    with pytest.raises(ValidationError):
        OpenApplicationAction(target="notepad\nwhoami")

    with pytest.raises(ValidationError):
        ClickAction(x=10, y=10, element_description="Ok", extra_field=True)  # type: ignore[call-arg]


def test_safety_policy_blocks_shells_and_requires_approval_for_sensitive_categories() -> None:
    policy = ActionSafetyPolicy()

    # File deletion
    del_hotkey = policy.evaluate(HotkeyAction(keys=["shift", "delete"]))
    assert del_hotkey.allowed is True
    assert del_hotkey.requires_approval is True
    assert del_hotkey.category == "file_deletion"

    del_click = policy.evaluate(
        ClickAction(x=100, y=100, element_description="Click the Delete permanently button")
    )
    assert del_click.requires_approval is True
    assert del_click.category == "file_deletion"

    # Command interpreters are hard-blocked (not even approvable)
    open_ps = policy.evaluate(OpenApplicationAction(target="powershell.exe"))
    assert open_ps.allowed is False
    assert open_ps.risk_level == "critical"

    install_cmd = policy.evaluate(TypeTextAction(text="winget install SomeTool", enter=True))
    assert install_cmd.requires_approval is True
    assert install_cmd.category == "software_installation"

    # Security settings
    sec_click = policy.evaluate(
        ClickAction(x=50, y=50, element_description="Toggle Windows Defender Firewall off")
    )
    assert sec_click.requires_approval is True
    assert sec_click.category == "security_settings"

    # Credential access
    cred_type = policy.evaluate(
        TypeTextAction(
            text="super-secret-value",
            element_description="API Key password input field",
        )
    )
    assert cred_type.requires_approval is True
    assert cred_type.category == "credential_access"

    # Financial transaction
    pay_click = policy.evaluate(
        ClickAction(x=300, y=400, element_description="Click Confirm Payment button")
    )
    assert pay_click.requires_approval is True
    assert pay_click.category == "financial_transaction"

    # Blocked system hotkey
    blocked = policy.evaluate(HotkeyAction(keys=["ctrl", "alt", "delete"]))
    assert blocked.allowed is False
    assert blocked.risk_level == "critical"

    # Strictly read-only navigation GUI interaction does not require approval
    benign = policy.evaluate(
        ClickAction(x=200, y=200, element_description="Click the Help menu tab")
    )
    assert benign.allowed is True
    assert benign.requires_approval is False
    assert benign.risk_level == "low"
