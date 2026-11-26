"""Fail-closed Agent-S S3 adapter with AST-only action parsing (zero eval/exec)."""

from __future__ import annotations

import logging
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gui_agents.s3.agents.agent_s import AgentS3
from gui_agents.s3.agents.worker import Worker
from gui_agents.s3.core.mllm import LMMAgent
from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY
from gui_agents.s3.utils.common_utils import parse_code_from_string, split_thinking_response
from gui_agents.s3.utils.formatters import SINGLE_ACTION_FORMATTER

from robbie.agents.llm_engine import (
    DEFAULT_DESKTOP_MAX_RETRIES,
    build_robbie_lmm_agent,
    call_robbie_llm,
    call_robbie_llm_formatted,
)
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


def call_llm_formatted(
    generator: Any,
    format_checkers: list[Callable[[str], tuple[bool, str]]],
    **kwargs: Any,
) -> str:
    """Bounded, non-swallowing replacement for upstream Agent-S call_llm_formatted."""
    return call_robbie_llm_formatted(generator, format_checkers, **kwargs)


@dataclass(frozen=True)
class StepPrediction:
    """Structured output from a single Agent-S S3 turn."""

    action: RobbieAction
    plan: str
    plan_code: str
    reflection: str | None
    notes: tuple[str, ...]


class SafeRobbieWorker(Worker):
    """Subclass of Agent-S S3 Worker that eliminates internal `eval()` calls
    and uses Robbie's bounded OpenAI-compatible engine.

    Upstream `Worker.generate_next_action` calls `common_utils.create_pyautogui_code`
    which executes raw `eval(code)` twice (once in `CODE_VALID_FORMATTER` and again
    when building the action). `SafeRobbieWorker` replaces both with strict AST
    validation (`parse_agent_call_ast` with signature binding) and direct invocation
    of allowlisted `RobbieWindowsACI` methods returning typed `RobbieAction` models.
    """

    grounding_agent: RobbieWindowsACI

    def __init__(
        self,
        worker_engine_params: dict[str, Any],
        grounding_agent: RobbieWindowsACI,
        platform: str = "windows",
        max_trajectory_length: int = 8,
        enable_reflection: bool = True,
        *,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_DESKTOP_MAX_RETRIES,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._http_timeout_seconds = http_timeout_seconds
        self._max_retries = max_retries
        self._client_factory = client_factory
        super().__init__(
            worker_engine_params=worker_engine_params,
            grounding_agent=grounding_agent,
            platform=platform,
            max_trajectory_length=max_trajectory_length,
            enable_reflection=enable_reflection,
        )
        # Preserve upstream CLI default: None when temperature is omitted (do not coerce to 0.0).
        self.temperature = worker_engine_params.get("temperature")

    def _create_agent(
        self,
        system_prompt: str | None = None,
        engine_params: dict[str, Any] | None = None,
    ) -> LMMAgent:
        return build_robbie_lmm_agent(
            engine_params or self.engine_params,
            system_prompt=system_prompt,
            http_timeout_seconds=self._http_timeout_seconds,
            max_retries=self._max_retries,
            client_factory=self._client_factory,
        )

    def reset(self) -> None:
        if not isinstance(self.grounding_agent, RobbieWindowsACI):
            raise UnsafeActionCodeError(
                "SafeRobbieWorker requires a RobbieWindowsACI grounding agent."
            )

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

    def _generate_reflection(
        self,
        instruction: str,
        obs: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        reflection: str | None = None
        reflection_thoughts: str | None = None
        if self.enable_reflection:
            if self.turn_count == 0:
                text_content = textwrap.dedent(
                    f"""
                    Task Description: {instruction}
                    Current Trajectory below:
                    """
                )
                updated_sys_prompt = self.reflection_agent.system_prompt + "\n" + text_content
                self.reflection_agent.add_system_prompt(updated_sys_prompt)
                self.reflection_agent.add_message(
                    text_content="The initial screen is provided. No action has been taken yet.",
                    image_content=obs["screenshot"],
                    role="user",
                )
            else:
                self.reflection_agent.add_message(
                    text_content=self.worker_history[-1],
                    image_content=obs["screenshot"],
                    role="user",
                )
                full_reflection = call_robbie_llm(
                    self.reflection_agent,
                    temperature=self.temperature,
                    use_thinking=self.use_thinking,
                )
                reflection, reflection_thoughts = split_thinking_response(full_reflection)
                self.reflections.append(reflection)
        return reflection, reflection_thoughts

    def _validate_ast_call_format(self, response: str) -> tuple[bool, str]:
        code = parse_code_from_string(response)
        if not code:
            return False, "Incorrect code: Response must contain one Python code block."
        try:
            parse_agent_call_ast(
                code,
                self.grounding_agent.allowed_action_methods,
                method_signatures=self.grounding_agent.allowed_method_signatures,
            )
            return True, ""
        except UnsafeActionCodeError as exc:
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
        if not isinstance(self.grounding_agent, RobbieWindowsACI):
            raise UnsafeActionCodeError(
                "SafeRobbieWorker requires a RobbieWindowsACI grounding agent."
            )

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
            cancel_check=cancel_check,
        )
        self.worker_history.append(plan)
        self.generator_agent.add_message(plan, role="assistant")

        plan_code = parse_code_from_string(plan)
        parsed = parse_agent_call_ast(
            plan_code,
            self.grounding_agent.allowed_action_methods,
            method_signatures=self.grounding_agent.allowed_method_signatures,
        )
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

    def __init__(
        self,
        worker_engine_params: dict[str, Any],
        grounding_agent: RobbieWindowsACI,
        platform: str = "windows",
        max_trajectory_length: int = 8,
        enable_reflection: bool = True,
        *,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_DESKTOP_MAX_RETRIES,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._http_timeout_seconds = http_timeout_seconds
        self._max_retries = max_retries
        self._client_factory = client_factory
        super().__init__(
            worker_engine_params=worker_engine_params,
            grounding_agent=grounding_agent,
            platform=platform,
            max_trajectory_length=max_trajectory_length,
            enable_reflection=enable_reflection,
        )

    def reset(self) -> None:
        if not isinstance(self.grounding_agent, RobbieWindowsACI):
            raise UnsafeActionCodeError("RobbieAgentS3 requires a RobbieWindowsACI instance.")
        self.executor = SafeRobbieWorker(
            worker_engine_params=self.worker_engine_params,
            grounding_agent=self.grounding_agent,
            platform="windows",
            max_trajectory_length=self.max_trajectory_length,
            enable_reflection=self.enable_reflection,
            http_timeout_seconds=self._http_timeout_seconds,
            max_retries=self._max_retries,
            client_factory=self._client_factory,
        )


class AgentS3Adapter:
    """Fail-closed facade isolating Robbie from Agent-S S3 internals."""

    def __init__(
        self,
        config: AgentRuntimeConfig,
        *,
        display_info: DisplayInfo | None = None,
        tesseract_cmd: str | None = None,
        aci: RobbieWindowsACI | None = None,
        agent: RobbieAgentS3 | None = None,
        http_timeout_seconds: float | None = None,
        max_retries: int = DEFAULT_DESKTOP_MAX_RETRIES,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        if aci is not None and not isinstance(aci, RobbieWindowsACI):
            raise UnsafeActionCodeError(
                f"AgentS3Adapter requires RobbieWindowsACI; got {type(aci).__name__}."
            )
        if agent is not None and not isinstance(agent, RobbieAgentS3):
            raise UnsafeActionCodeError(
                f"AgentS3Adapter requires RobbieAgentS3; got {type(agent).__name__}."
            )

        self.config = config
        self.display_info = display_info or get_windows_display_info()

        worker_params = config.worker.to_engine_params()
        grounding_params = config.grounding.to_engine_params()

        self.aci = aci or RobbieWindowsACI(
            engine_params_for_generation=worker_params,
            engine_params_for_grounding=grounding_params,
            display_info=self.display_info,
            tesseract_cmd=tesseract_cmd,
            http_timeout_seconds=http_timeout_seconds,
            max_retries=max_retries,
            client_factory=client_factory,
        )
        self.agent = agent or RobbieAgentS3(
            worker_engine_params=worker_params,
            grounding_agent=self.aci,
            platform="windows",
            max_trajectory_length=config.max_trajectory_length,
            enable_reflection=config.enable_reflection,
            http_timeout_seconds=http_timeout_seconds,
            max_retries=max_retries,
            client_factory=client_factory,
        )
        self._assert_safe_runtime()

    def _assert_safe_runtime(self) -> SafeRobbieWorker:
        if not isinstance(self.aci, RobbieWindowsACI):
            raise UnsafeActionCodeError(
                f"Unsafe ACI type '{type(self.aci).__name__}'; refusing execution."
            )
        if self.aci.env is not None or getattr(self.aci, "code_agent", None) is not None:
            raise UnsafeActionCodeError(
                "LocalEnv / CodeAgent must remain disabled on RobbieWindowsACI."
            )
        if not isinstance(self.agent, RobbieAgentS3):
            raise UnsafeActionCodeError(
                f"Unsafe agent type '{type(self.agent).__name__}'; refusing execution."
            )
        executor = getattr(self.agent, "executor", None)
        if not isinstance(executor, SafeRobbieWorker):
            raise UnsafeActionCodeError(
                f"Unsafe worker type '{type(executor).__name__}'; "
                "refusing fallback to upstream Agent-S Worker."
            )
        return executor

    def reset(self) -> None:
        self.aci.notes.clear()
        self.agent.reset()
        self._assert_safe_runtime()

    def predict_step(
        self,
        instruction: str,
        screenshot_png: bytes,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> StepPrediction:
        """Run one synchronous Agent-S S3 prediction step (never falls back to upstream eval)."""
        executor = self._assert_safe_runtime()
        obs = {"screenshot": screenshot_png}
        info, actions = executor.generate_next_action(
            instruction=instruction,
            obs=obs,
            cancel_check=cancel_check,
        )

        if not actions:
            raise UnsafeActionCodeError("Adapter produced no action.")
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
