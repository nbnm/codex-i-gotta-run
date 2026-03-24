from __future__ import annotations

from pathlib import Path

from config import load_config


def test_load_config_prefers_local_config_toml_when_present(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                'command = ["python3", "-m", "tests.fake_app_server"]',
                "",
                "[registry]",
                'data_dir = "./registry"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    config = load_config()

    assert config.app_server_command == ["python3", "-m", "tests.fake_app_server"]
    assert config.data_dir == (tmp_path / "registry").resolve()


def test_load_config_ignores_legacy_sidecar_environment_variables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_I_GOTTA_RUN_SERVER_CMD", "python3 -m tests.fake_app_server")
    monkeypatch.setenv("CODEX_I_GOTTA_RUN_DATA_DIR", str(tmp_path / "legacy-registry"))

    config = load_config()

    assert config.app_server_command == []
    assert config.data_dir != (tmp_path / "legacy-registry").resolve()
