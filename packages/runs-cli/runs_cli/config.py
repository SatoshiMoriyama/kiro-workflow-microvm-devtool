"""Configuration loading for kiro-microvm.

Precedence for every key: CLI argument > environment variable >
config file (~/.config/kiro-microvm/config) > built-in default.

The config file uses KEY=VALUE lines. Blank lines and lines starting with
"#" are ignored, and one pair of surrounding quotes is stripped from values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

APP_NAME = "kiro-microvm"

DEFAULT_REGION = "ap-northeast-1"
DEFAULT_STACK_NAME = "KiroMicrovmBase"
DEFAULT_IMAGE_NAME = "kiro-microvm"

KEYS = (
    "AWS_PROFILE",
    "AWS_REGION",
    "STACK_NAME",
    "MICROVM_IMAGE_NAME",
    "KIRO_MICROVM_RESULTS_DIR",
)

_DEFAULTS = {
    "AWS_REGION": DEFAULT_REGION,
    "STACK_NAME": DEFAULT_STACK_NAME,
    "MICROVM_IMAGE_NAME": DEFAULT_IMAGE_NAME,
}


class ConfigError(Exception):
    """Raised when required configuration is missing."""


def config_file_path(env: Mapping[str, str] | None = None) -> str:
    """Return the config file path, honoring XDG_CONFIG_HOME."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME, "config")


def data_dir(env: Mapping[str, str] | None = None) -> str:
    """Return the data directory, honoring XDG_DATA_HOME."""
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(base, APP_NAME)


def parse_config_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines into a dict."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def load_config_file(path: str) -> dict[str, str]:
    """Read and parse a config file. A missing or unreadable file yields {}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return parse_config_text(f.read())
    except OSError:
        return {}


@dataclass
class Config:
    aws_profile: str | None
    aws_region: str
    stack_name: str
    image_name: str
    results_dir: str

    def require_profile(self) -> str:
        """Return AWS_PROFILE or raise ConfigError. There is no default."""
        if not self.aws_profile:
            raise ConfigError(
                "AWS_PROFILE is not set. Set it in the environment or in "
                "~/.config/kiro-microvm/config"
            )
        return self.aws_profile


def resolve(
    cli: Mapping[str, str | None] | None = None,
    env: Mapping[str, str] | None = None,
    config_path: str | None = None,
) -> Config:
    """Resolve settings. `cli` maps config keys (e.g. AWS_PROFILE) to values."""
    cli = cli or {}
    env = os.environ if env is None else env
    file_values = load_config_file(config_path or config_file_path(env))

    def get(key: str) -> str | None:
        for source in (cli, env, file_values):
            value = source.get(key)
            if value:
                return value
        return _DEFAULTS.get(key)

    results_dir = get("KIRO_MICROVM_RESULTS_DIR") or os.path.join(
        data_dir(env), "results"
    )
    return Config(
        aws_profile=get("AWS_PROFILE"),
        aws_region=get("AWS_REGION") or DEFAULT_REGION,
        stack_name=get("STACK_NAME") or DEFAULT_STACK_NAME,
        image_name=get("MICROVM_IMAGE_NAME") or DEFAULT_IMAGE_NAME,
        results_dir=os.path.abspath(os.path.expanduser(results_dir)),
    )
