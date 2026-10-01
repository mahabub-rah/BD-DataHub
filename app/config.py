"""Runtime configuration, read from environment variables (optionally a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    database_path: Path = field(
        default_factory=lambda: Path(os.getenv("BDH_DATABASE", BASE_DIR / "data" / "bd_data_hub.db"))
    )
    monitor_user_agent: str = field(
        default_factory=lambda: os.getenv(
            "BDH_MONITOR_UA",
            "BangladeshDataHubLinkChecker/1.0 (+metadata link monitoring; respects robots.txt)",
        )
    )
    monitor_concurrency: int = field(default_factory=lambda: int(os.getenv("BDH_MONITOR_CONCURRENCY", "6")))
    monitor_timeout: float = field(default_factory=lambda: float(os.getenv("BDH_MONITOR_TIMEOUT", "25")))
    # Minimum seconds between two requests to the same host.
    monitor_host_delay: float = field(default_factory=lambda: float(os.getenv("BDH_MONITOR_HOST_DELAY", "2")))
    # Comma-separated host names the site answers to (e.g. "bd-data-hub.onrender.com"); empty allows any.
    allowed_hosts: list[str] = field(default_factory=lambda: [
        h.strip() for h in os.getenv("BDH_ALLOWED_HOSTS", "").split(",") if h.strip()])
    # Minimum hours between two runs started from the public "Check sources now" button.
    refresh_cooldown_hours: float = field(default_factory=lambda: float(os.getenv("BDH_REFRESH_COOLDOWN_HOURS", "6")))
    # Contact address sent to OpenAlex by the papers harvester (its "polite pool"). Omitted when unset.
    openalex_mailto: str | None = field(default_factory=lambda: os.getenv("BDH_OPENALEX_MAILTO") or None)


settings = Settings()
