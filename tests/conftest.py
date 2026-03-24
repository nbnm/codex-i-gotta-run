from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def fake_server_setup(tmp_path: Path) -> dict[str, object]:
    state_path = tmp_path / "fake-server-state.json"
    config_path = tmp_path / "config.toml"
    env = os.environ.copy()
    env["FAKE_APP_SERVER_STATE_PATH"] = str(state_path)
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                f'command = ["{sys.executable}", "-m", "tests.fake_app_server"]',
                "",
                "[registry]",
                f'data_dir = "{(tmp_path / "registry").as_posix()}"',
                "",
                "[logging]",
                'level = "INFO"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "env": env,
        "config_path": config_path,
        "state_path": state_path,
    }


@pytest.fixture()
def cli_app():
    return app
