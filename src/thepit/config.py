"""Configuration.

One TOML file plus environment variables for secrets. No pydantic-settings, no
python-dotenv: 3.12 ships `tomllib`, and secrets belong in the environment
rather than in a file that could be committed to a public repository.

**Mode is a constructor argument to the process, read from argv.** It is never
read from the config file and never mutable at runtime. That is what makes
paper and live separate types rather than a boolean, and it is why `Config`
carries `mode` but has no setter for it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Mode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


DEFAULT_HOME = Path.home() / ".thepit"

# A default watchlist: liquid, well covered, and spread across sectors so
# correlation between agents is at least possible to observe. Not a
# recommendation, and not chosen with any view about these companies.
DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "XOM", "WMT", "UNH", "PG",
]


@dataclass(frozen=True)
class Config:
    mode: Mode = Mode.PAPER
    home: Path = DEFAULT_HOME
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Separate bind for the read-only view. Control endpoints are mounted only
    # on the loopback listener; this one gets the read-only router. Empty
    # disables remote viewing entirely, which is the safe default.
    lan_host: str = ""
    lan_port: int = 8001

    quote_interval_open_s: float = 5.0
    quote_interval_closed_s: float = 300.0
    bar_interval_s: float = 300.0
    news_interval_s: float = 600.0

    raw_recording: bool = True
    raw_retention_days: int = 90

    @property
    def data_dir(self) -> Path:
        # Separate directories per mode, not a column in one database. A column
        # is one bad WHERE clause away from cross-contamination, and it makes
        # "delete all my paper data" a dangerous query.
        return self.home / self.mode.value

    @property
    def db_path(self) -> Path:
        return self.data_dir / "thepit.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def state_dir(self) -> Path:
        # Shared across modes on purpose: the kill switch must stop everything,
        # not just whichever mode happens to be running.
        return self.home / "state"

    @property
    def is_live(self) -> bool:
        return self.mode is Mode.LIVE


def load(path: Path | None = None, *, mode: Mode = Mode.PAPER) -> Config:
    """Load config, applying environment overrides.

    Mode comes from the caller (argv), never from the file.
    """
    home = Path(os.environ.get("THEPIT_HOME", DEFAULT_HOME)).expanduser()
    path = path or (home / "config.toml")

    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    feed = raw.get("feed", {})
    api = raw.get("api", {})

    return Config(
        mode=mode,
        home=home,
        symbols=raw.get("symbols", list(DEFAULT_SYMBOLS)),
        api_host=api.get("host", "127.0.0.1"),
        api_port=int(api.get("port", 8000)),
        lan_host=api.get("lan_host", ""),
        lan_port=int(api.get("lan_port", 8001)),
        quote_interval_open_s=float(feed.get("quote_interval_open_s", 5.0)),
        quote_interval_closed_s=float(feed.get("quote_interval_closed_s", 300.0)),
        bar_interval_s=float(feed.get("bar_interval_s", 300.0)),
        news_interval_s=float(feed.get("news_interval_s", 600.0)),
        raw_recording=bool(raw.get("raw_recording", True)),
        raw_retention_days=int(raw.get("raw_retention_days", 90)),
    )
