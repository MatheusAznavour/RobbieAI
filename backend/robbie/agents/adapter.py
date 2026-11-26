"""Isolated Agent-S S3 adapter with AST-only action parsing (zero eval/exec)."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gui_agents.s3.agents.agent_s import AgentS3
from gui_agents.s3.agents.worker import Worker
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY
from gui_agents.s3.utils.common_utils import call_llm_formatted, parse_code_from_string
from gui_agents.s3.utils.formatters import SINGLE_ACTION_FORMATTER

from robbie.agents.windows_aci import RobbieWindowsACI
from robbie.providers.config import AgentRuntimeConfig
from robbie.tools.actions import (
    ROBBIE_ACTION_TYPES,
    RobbieAction,
    UnsafeActionCodeError,
    WaitAction,
    parse_agent_call_ast,
)
from robbie.tools.windows import DisplayInfo, get_windows_display_info

logger = logging.getLogger("robbie.agents.adapter")

OSWORLD_SUDO_LINE = (
    "7. My computer's password is 'osworld-public-evaluation', "
    "feel free to use it when you need sudo rights."
)
ROBBIE_SAFETY_PROMPT_LINE = (
    "7. Never attempt to execute arbitrary shell or Python code; "
    "use only the provided GUI action methods."
)
CODE_AGENT_SECTION_RE = re.compile(
    r"### Code Agent.*?# General Task Guidelines",
    re.DOTALL,
)


@dataclass(frozen=True)
class StepPrediction:
    """Structured output from a single Agent-S S3 turn."""

    action: RobbieAction
    plan: str
    plan_code: str
    reflection: str | None
    notes: tuple[str, ...]


class SafeRobbieWorker(Worker):
    """Subclass of Agent-S S3 Worker that eliminates internal `eval()` calls.

    Upstream `Worker.generate_next_action` calls `common_utils.create_pyautogui_code`
    which executes raw `eval(code)` twice (once in `CODE_VALID_FORMATTER` and again
    when building the action). `SafeRobbieWorker` replaces both with strict AST
    validation (`parse_agent_call_ast`) and direct invocation of allowlisted
    `RobbieWindowsACI` methods returning typed `RobbieAction` models.
    """

    grounding_agent: RobbieWindowsACI

    def reset(self) -> None:
        skipped_actions = ["set_cell_values", "call_code_agent"]
        if not self.grounding_agent.tesseract_status.available:
            skipped_actions.append("highlight_text_span")

        sys_prompt = PROCEDURAL_MEMORY.construct_simple_worker_procedural_memory(
            type(self.grounding_agent),
            skipped_actions=skipped_actions,
        ).replace("CURRENT_OS", "windows")
        sys_prompt = CODE_AGENT_SECTION_RE.sub("# General Task Guidelines", sys_prompt)
        sys_prompt = sys_prompt.replace(OSWORLD_SUDO_LINE, ROBBIE_SAFETY_PROMPT_LINE)

        self.generator_agent = self._create_agent(sys_prompt)
        self.reflection_agent = self._create_agent(PROCEDURAL_MEMORY.REFLECTION_ON_TRAJECTORY)

        self.turn_count = 0
        self.worker_history = []
        self.reflections = []
        self.cost_this_turn = 0
        self.screenshot_inputs = []

    def _validate_ast_call_format(self, response: str) -> tuple[bool, str]:
        code = parse_code_from_string(response)
        if not code:
            return False, "Incorrect code: Response must contain one Python code block."
        try:
            parsed = parse_agent_call_ast(code, self.grounding_agent.allowed_action_methods)
            method = getattr(self.grounding_agent, parsed.method_name)
            inspect.signature(method).bind(*parsed.args, **parsed.kwargs)
            return True, ""
        except (UnsafeActionCodeError, TypeError, AttributeError) as exc:
            return (
                False,
                "Incorrect code: Action must be a single valid allowlisted method call "
                f"with literal arguments ({exc}).",
            )

    def generate_next_action(  # type: ignore[override]
        self,
        instruction: str,
        obs: dict[str, Any],
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, Any], list[RobbieAction]]:
        self.grounding_agent.assign_screenshot(obs)
        self.grounding_agent.set_task_instruction(instruction)

        generator_message = (
            ""
            if self.turn_count > 0
            else "The initial screen is provided. No action has been taken yet."
        )

        if self.turn_count == 0:
            prompt_with_instructions = self.generator_agent.system_prompt.replace(
                "TASK_DESCRIPTION",
                instruction,
            )
            self.generator_agent.add_system_prompt(prompt_with_instructions)

        reflection: str | None = None
        reflection_thoughts: str | None = None
        if not (cancel_check and cancel_check()):
            reflection, reflection_thoughts = self._generate_reflection(instruction, obs)
            if reflection:
                generator_message += (
                    "REFLECTION: You may use this reflection on the previous action and "
                    f"overall trajectory:\n{reflection}\n"
                )

        if cancel_check and cancel_check():
            return {
                "plan": "",
                "plan_code": "",
                "action": WaitAction(seconds=0.0),
                "reflection": reflection,
                "reflection_thoughts": reflection_thoughts,
            }, [WaitAction(seconds=0.0)]

        generator_message += f"\nCurrent Text Buffer = [{','.join(self.grounding_agent.notes)}]\n"

        self.generator_agent.add_message(
            generator_message,
            image_content=obs["screenshot"],
            role="user",
        )

        format_checkers = [
            SINGLE_ACTION_FORMATTER,
            self._validate_ast_call_format,
        ]
        plan = call_llm_formatted(
            self.generator_agent,
            format_checkers,
            temperature=self.temperature,
            use_thinking=self.use_thinking,
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")

        plan_code = parse_code_from_string(plan)
        parsed = parse_agent_call_ast(plan_code, self.grounding_agent.allowed_action_methods)
        action_method = getattr(self.grounding_agent, parsed.method_name)
        action: RobbieAction = action_method(*parsed.args, **parsed.kwargs)

        executor_info: dict[str, Any] = {
            "plan": plan,
            "plan_code": plan_code,
            "action": action,
            "reflection": reflection,
            "reflection_thoughts": reflection_thoughts,
        }
        self.turn_count += 1
        self.screenshot_inputs.append(obs["screenshot"])
        self.flush_messages()
        return executor_info, [action]


class RobbieAgentS3(AgentS3):
    """Subclass of AgentS3 wiring SafeRobbieWorker without modifying vendor/Agent-S."""

    def reset(self) -> None:
        self.executor = SafeRobbieWorker(
            worker_engine_params=self.worker_engine_params,
            grounding_agent=self.grounding_agent,
            platform="windows",
            max_trajectory_length=self.max_trajectory_length,
            enable_reflection=self.enable_reflection,
        )


class AgentS3Adapter:
    """Facade isolating Robbie's backend and runner from Agent-S S3 internals."""

    def __init__(
        self,
        config: AgentRuntimeConfig,
        *,
        display_info: DisplayInfo | None = None,
        tesseract_cmd: str | None = None,
        aci: RobbieWindowsACI | None = None,
        agent: RobbieAgentS3 | None = None,
    ) -> None:
        self.config = config
        self.display_info = display_info or get_windows_display_info()

        worker_params = config.worker.to_engine_params()
        grounding_params = config.grounding.to_engine_params()

        self.aci = aci or RobbieWindowsACI(
            engine_params_for_generation=worker_params,
            engine_params_for_grounding=grounding_params,
            display_info=self.display_info,
            tesseract_cmd=tesseract_cmd,
        )
        self.agent = agent or RobbieAgentS3(
            worker_engine_params=worker_params,
            grounding_agent=self.aci,
            platform="windows",
            max_trajectory_length=config.max_trajectory_length,
            enable_reflection=config.enable_reflection,
        )

    def reset(self) -> None:
        self.aci.notes.clear()
        self.agent.reset()

    def predict_step(
        self,
        instruction: str,
        screenshot_png: bytes,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> StepPrediction:
        """Run one synchronous Agent-S S3 prediction step (call off the asyncio loop)."""
        obs = {"screenshot": screenshot_png}
        executor = self.agent.executor
        if isinstance(executor, SafeRobbieWorker):
            info, actions = executor.generate_next_action(
                instruction=instruction,
                obs=obs,
                cancel_check=cancel_check,
            )
        else:
            info, actions = self.agent.predict(instruction=instruction, observation=obs)

        action = actions[0]
        if not isinstance(action, ROBBIE_ACTION_TYPES):
            raise UnsafeActionCodeError(
                "Adapter expected a validated RobbieAction, not an executable string."
            )

        return StepPrediction(
            action=action,
            plan=str(info.get("plan", "")),
            plan_code=str(info.get("plan_code", "")),
            reflection=info.get("reflection"),
            notes=tuple(self.aci.notes),
        )
