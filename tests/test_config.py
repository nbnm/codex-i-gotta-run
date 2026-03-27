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


def test_load_config_parses_telegram_settings(tmp_path: Path, monkeypatch) -> None:
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
                "[telegram]",
                'telegram_bot_token_env = "TEST_TELEGRAM_BOT_TOKEN"',
                'telegram_bot_allow_username = "TEST_TELEGRAM_BOT_ALLOW_USERNAME"',
                'telegram_default_chat_id_env = "TEST_TELEGRAM_DEFAULT_CHAT_ID"',
                'telegram_allowed_chat_ids_env = "TEST_TELEGRAM_ALLOWED_CHAT_IDS"',
                "poll_timeout_seconds = 10",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TEST_TELEGRAM_BOT_ALLOW_USERNAME", "@oleg")
    monkeypatch.setenv("TEST_TELEGRAM_DEFAULT_CHAT_ID", "777")
    monkeypatch.setenv("TEST_TELEGRAM_ALLOWED_CHAT_IDS", "777,888")

    config = load_config(config_path)

    assert config.telegram.bot_token == "test-token"
    assert config.telegram.default_chat_id == 777
    assert config.telegram.allowed_chat_ids == [777, 888]
    assert config.telegram.username == "@oleg"
    assert config.telegram.poll_timeout_seconds == 10


def test_load_config_resolves_telegram_bot_token_from_explicit_env_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'telegram_bot_token_env = "TELEGRAM_BOT_TOKEN"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")

    config = load_config(config_path)

    assert config.telegram.bot_token == "env-token"


def test_load_config_resolves_telegram_allowed_username_from_explicit_env_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'telegram_bot_allow_username = "TELEGRAM_BOT_ALLOW_USERNAME"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_BOT_ALLOW_USERNAME", "@oleg")

    config = load_config(config_path)

    assert config.telegram.username == "@oleg"


def test_load_config_resolves_telegram_default_chat_id_from_explicit_env_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'telegram_default_chat_id_env = "TELEGRAM_DEFAULT_CHAT_ID"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_DEFAULT_CHAT_ID", "777")

    config = load_config(config_path)

    assert config.telegram.default_chat_id == 777


def test_load_config_resolves_telegram_allowed_chat_ids_from_explicit_env_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[telegram]",
                'telegram_allowed_chat_ids_env = "TELEGRAM_ALLOWED_CHAT_IDS"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "777, 888")

    config = load_config(config_path)

    assert config.telegram.allowed_chat_ids == [777, 888]
