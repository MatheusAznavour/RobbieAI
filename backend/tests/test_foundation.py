"""Foundation smoke tests verifying package structure and core imports."""

import importlib

import pytest

import robbie


@pytest.mark.parametrize(
    "module_name",
    [
        "robbie",
        "robbie.agents",
        "robbie.api",
        "robbie.core",
        "robbie.memory",
        "robbie.providers",
        "robbie.tools",
    ],
)
def test_robbie_subpackages_importable(module_name: str) -> None:
    module = importlib.import_module(module_name)
    assert module is not None


def test_robbie_version_defined() -> None:
    assert isinstance(robbie.__version__, str)
    assert robbie.__version__


@pytest.mark.parametrize(
    "dependency_name",
    [
        "fastapi",
        "httpx",
        "pydantic",
        "dotenv",
        "uvicorn",
        "websockets",
        "gui_agents",
        "gui_agents.s3.agents.agent_s",
        "gui_agents.s3.agents.grounding",
    ],
)
def test_core_dependencies_importable(dependency_name: str) -> None:
    module = importlib.import_module(dependency_name)
    assert module is not None
