from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codex_thread_orchestrator.cli import app


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def fake_server_env(tmp_path: Path) -> dict[str, str]:
    state_path = tmp_path / "fake-server-state.json"
    env = os.environ.copy()
    env["FAKE_APP_SERVER_STATE_PATH"] = str(state_path)
    env["CODEX_THREAD_ORCHESTRATOR_SERVER_CMD"] = f"{sys.executable} -m tests.fake_app_server"
    env["CODEX_THREAD_ORCHESTRATOR_DATA_DIR"] = str(tmp_path / "registry")
    return env


@pytest.fixture()
def cli_app():
    return app

