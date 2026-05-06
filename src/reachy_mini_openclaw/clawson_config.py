"""Clawson-specific configuration loader.

Kept separate from upstream's `config.py` so we don't muddy the inherited
env-var schema. Reads `~/.config/clawson/config.toml` first, then falls
back to environment variables — env wins on collision.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import tomllib  # py3.11+
except ImportError:                                  # pragma: no cover
    import tomli as tomllib                          # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "clawson" / "config.toml"


@dataclass
class ClawsonConfig:
    github_token: Optional[str] = None
    # Phase 4 will add vercel_token; Phase 5 will add widget_host/port.

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_token)


def load_clawson_config(path: Path = DEFAULT_CONFIG_PATH) -> ClawsonConfig:
    """Load clawson config from TOML, then layer env vars on top."""
    raw: dict = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except Exception as e:
            logger.warning("could not parse %s: %s", path, e)
            raw = {}

    github_section = raw.get("github") or {}
    cfg = ClawsonConfig(
        github_token=os.environ.get("GITHUB_TOKEN") or github_section.get("token"),
    )
    return cfg
