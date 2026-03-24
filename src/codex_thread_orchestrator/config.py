from __future__ import annotations

import os
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field

from codex_thread_orchestrator.models import ClientInfo, Model


ENV_PREFIX = "CODEX_THREAD_ORCHESTRATOR_"


def _default_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "codex-thread-orchestrator"
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return appdata / "codex-thread-orchestrator"
    return home / ".local" / "share" / "codex-thread-orchestrator"


class AppConfig(Model):
    data_dir: Path = Field(default_factory=_default_data_dir)
    app_server_command: list[str] = Field(default_factory=list)
    app_server_cwd: Path | None = None
    client_info: ClientInfo = Field(default_factory=ClientInfo)
    experimental_api: bool = False
    opt_out_notification_methods: list[str] = Field(default_factory=list)
    log_level: str = "INFO"
    recent_event_limit: int = 20

    @property
    def app_server_instance(self) -> str | None:
        if not self.app_server_command:
            return None
        return " ".join(self.app_server_command)


def _merge_dict(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_config_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    server = raw.get("server", {})
    client = raw.get("client", {})
    registry = raw.get("registry", {})
    logging = raw.get("logging", {})

    return {
        "app_server_command": list(server.get("command", [])),
        "app_server_cwd": server.get("cwd"),
        "experimental_api": bool(server.get("experimental_api", False)),
        "opt_out_notification_methods": list(server.get("opt_out_notification_methods", [])),
        "data_dir": registry.get("data_dir"),
        "log_level": logging.get("level", "INFO"),
        "client_info": {
            "name": client.get("name", ClientInfo().name),
            "title": client.get("title", ClientInfo().title),
            "version": client.get("version", ClientInfo().version),
        },
    }


def _parse_env(env: dict[str, str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    command = env.get(f"{ENV_PREFIX}SERVER_CMD")
    if command:
        data["app_server_command"] = shlex.split(command)
    cwd = env.get(f"{ENV_PREFIX}SERVER_CWD")
    if cwd:
        data["app_server_cwd"] = cwd
    data_dir = env.get(f"{ENV_PREFIX}DATA_DIR")
    if data_dir:
        data["data_dir"] = data_dir
    log_level = env.get(f"{ENV_PREFIX}LOG_LEVEL")
    if log_level:
        data["log_level"] = log_level
    experimental_api = env.get(f"{ENV_PREFIX}EXPERIMENTAL_API")
    if experimental_api:
        data["experimental_api"] = experimental_api.lower() in {"1", "true", "yes", "on"}
    opt_out = env.get(f"{ENV_PREFIX}OPTOUT_NOTIFICATIONS")
    if opt_out:
        data["opt_out_notification_methods"] = [part.strip() for part in opt_out.split(",") if part.strip()]
    return data


def load_config(
    config_path: Path | None = None,
    *,
    data_dir: Path | None = None,
    server_cmd: str | list[str] | None = None,
    env: dict[str, str] | None = None,
) -> AppConfig:
    env_map = dict(os.environ if env is None else env)
    merged: dict[str, Any] = {}
    merged = _merge_dict(merged, _parse_config_file(config_path))
    merged = _merge_dict(merged, _parse_env(env_map))

    if data_dir is not None:
        merged["data_dir"] = data_dir

    if server_cmd is not None:
        merged["app_server_command"] = shlex.split(server_cmd) if isinstance(server_cmd, str) else list(server_cmd)

    config = AppConfig.model_validate(merged)
    config.data_dir = config.data_dir.expanduser().resolve()
    if config.app_server_cwd is not None:
        config.app_server_cwd = config.app_server_cwd.expanduser().resolve()
    return config
