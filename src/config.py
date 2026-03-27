from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field

from models import ClientInfo, Model, TelegramConfig


def _default_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "codex-i-gotta-run"
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return appdata / "codex-i-gotta-run"
    return home / ".local" / "share" / "codex-i-gotta-run"


class AppConfig(Model):
    data_dir: Path = Field(default_factory=_default_data_dir)
    app_server_command: list[str] = Field(default_factory=list)
    app_server_cwd: Path | None = None
    client_info: ClientInfo = Field(default_factory=ClientInfo)
    experimental_api: bool = False
    opt_out_notification_methods: list[str] = Field(default_factory=list)
    turn_start_options: dict[str, Any] = Field(default_factory=dict)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    log_level: str = "INFO"
    recent_event_limit: int = 20

    @property
    def app_server_instance(self) -> str | None:
        if not self.app_server_command:
            return None
        return " ".join(self.app_server_command)


def _resolve_config_path(path: Path | None) -> Path | None:
    if path is not None:
        return path
    default_path = Path.cwd() / "config.toml"
    if default_path.exists():
        return default_path
    return None


def _merge_dict(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_optional_env_var(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    env_name = name.strip()
    if not env_name:
        return None
    return os.environ.get(env_name)


def _parse_config_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    server = raw.get("server", {})
    client = raw.get("client", {})
    registry = raw.get("registry", {})
    logging = raw.get("logging", {})
    telegram = raw.get("telegram", {})
    turn_start_options = raw.get("turn_start_options", {})
    telegram_bot_token = telegram.get("bot_token")
    if not telegram_bot_token:
        telegram_bot_token = _resolve_optional_env_var(telegram.get("bot_token_env"))

    parsed: dict[str, Any] = {
        "app_server_command": list(server.get("command", [])),
        "app_server_cwd": server.get("cwd"),
        "experimental_api": bool(server.get("experimental_api", False)),
        "opt_out_notification_methods": list(server.get("opt_out_notification_methods", [])),
        "turn_start_options": dict(turn_start_options) if isinstance(turn_start_options, dict) else {},
        "telegram": {
            "bot_token": telegram_bot_token,
            "api_base_url": telegram.get("api_base_url", TelegramConfig().api_base_url),
            "poll_timeout_seconds": telegram.get("poll_timeout_seconds", TelegramConfig().poll_timeout_seconds),
            "allowed_chat_ids": list(telegram.get("allowed_chat_ids", [])),
            "allowed_usernames": list(telegram.get("allowed_usernames", [])),
            "default_chat_id": telegram.get("default_chat_id"),
        },
        "log_level": logging.get("level", "INFO"),
        "client_info": {
            "name": client.get("name", ClientInfo().name),
            "title": client.get("title", ClientInfo().title),
            "version": client.get("version", ClientInfo().version),
        },
    }
    data_dir = registry.get("data_dir")
    if data_dir is not None:
        parsed["data_dir"] = data_dir
    return parsed


def load_config(config_path: Path | None = None) -> AppConfig:
    merged = _parse_config_file(_resolve_config_path(config_path))
    config = AppConfig.model_validate(merged)
    config.data_dir = config.data_dir.expanduser().resolve()
    if config.app_server_cwd is not None:
        config.app_server_cwd = config.app_server_cwd.expanduser().resolve()
    return config
