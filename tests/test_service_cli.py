from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from codex_thread_orchestrator.cli import app


def test_cli_start_threads_and_status(runner: CliRunner, fake_server_env: dict[str, str], tmp_path: Path) -> None:
    result = runner.invoke(app, ["start", "ship it"], env=fake_server_env)
    assert result.exit_code == 0
    assert "Started thread" in result.stdout

    list_result = runner.invoke(app, ["threads"], env=fake_server_env)
    assert list_result.exit_code == 0
    assert "Threads" in list_result.stdout

    status_result = runner.invoke(app, ["status"], env=fake_server_env)
    assert status_result.exit_code == 0
    assert "Queued inputs:" in status_result.stdout


def test_cli_queue_and_autosteer(runner: CliRunner, fake_server_env: dict[str, str]) -> None:
    start_result = runner.invoke(app, ["start", "hold the turn"], env=fake_server_env)
    assert start_result.exit_code == 0
    thread_id = start_result.stdout.split()[2]

    queue_result = runner.invoke(app, ["queue", thread_id, "finish now"], env=fake_server_env)
    assert queue_result.exit_code == 0
    assert "Queued" in queue_result.stdout

    autosteer_result = runner.invoke(app, ["autosteer", thread_id], env=fake_server_env)
    assert autosteer_result.exit_code == 0
    assert "Autosteer" in autosteer_result.stdout
