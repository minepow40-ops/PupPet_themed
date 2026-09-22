"""
utils/config.py
Loads and provides access to data/config.json.
Supports hot-reload via Config.reload().
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("PupPet.config")

CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.json"


class Config:
    """Thin wrapper around config.json with dot-access helpers."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self.reload()

    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Re-read config.json from disk."""
        try:
            with CONFIG_PATH.open(encoding="utf-8") as fh:
                self._data = json.load(fh)
            log.debug("Config loaded.")
        except FileNotFoundError:
            log.error("data/config.json not found — using empty config.")
        except json.JSONDecodeError as exc:
            log.error("config.json parse error: %s", exc)

    def save(self) -> None:
        """Persist current config to disk."""
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    # Shortcut properties ------------------------------------------------
    @property
    def ticket_category_id(self) -> Optional[int]:
        v = self._data.get("ticket_category_id")
        return int(v) if v else None

    @property
    def log_channel_id(self) -> Optional[int]:
        v = self._data.get("log_channel_id")
        return int(v) if v else None

    @property
    def transcript_channel_id(self) -> Optional[int]:
        v = self._data.get("transcript_channel_id")
        return int(v) if v else None

    @property
    def staff_roles(self) -> list[int]:
        return [int(r) for r in self._data.get("staff_roles", [])]

    @property
    def admin_roles(self) -> list[int]:
        return [int(r) for r in self._data.get("admin_roles", [])]

    @property
    def ticket_settings(self) -> dict[str, Any]:
        return self._data.get("ticket_settings", {})

    @property
    def categories(self) -> dict[str, Any]:
        return self._data.get("categories", {})

    @property
    def priority_colors(self) -> dict[str, int]:
        return self._data.get("priority_colors", {})

    @property
    def embed_cfg(self) -> dict[str, Any]:
        return self._data.get("embeds", {})
