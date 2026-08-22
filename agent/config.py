"""Configuration loading and validation.

Two JSON files under ``config/`` hold every tunable threshold, and ``.env``
holds the Alpaca credentials. Nothing in this package reads a threshold from a
literal — a run either finds a valid config or refuses to start. Silent
fallbacks are the wrong behaviour for an agent that trades unattended.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"
LOG_DIR = REPO_ROOT / "logs"

JOURNAL_PATH = DATA_DIR / "journal.jsonl"
IV_HISTORY_PATH = DATA_DIR / "iv_history.jsonl"
BASELINE_PATH = DATA_DIR / "account_baseline.json"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or unusable."""


@dataclass(frozen=True)
class Credentials:
    api_key: str
    secret_key: str
    paper: bool = True


@dataclass(frozen=True)
class AgentConfig:
    """Everything the agent needs to run one cycle."""

    risk: dict[str, Any]
    options: dict[str, Any]
    agent: dict[str, Any] = field(default_factory=dict)
    credentials: Credentials | None = None
    paths: dict[str, Path] = field(default_factory=dict)

    # ---- Risk thresholds, surfaced as properties so the units are unambiguous.

    @property
    def max_abs_delta(self) -> float:
        return float(self.risk["delta"]["max_abs"])

    @property
    def max_loss_pct(self) -> float:
        return float(self.risk["notional"]["max_loss_pct"])

    @property
    def max_portfolio_delta_pct(self) -> float:
        return float(self.risk["portfolio"]["max_delta_dollars_pct"])

    @property
    def min_credit_usd(self) -> float:
        return float(self.risk["premium"]["min_credit_usd"])

    @property
    def min_dte(self) -> int:
        return int(self.risk["dte"]["min_days"])

    @property
    def max_dte(self) -> int:
        return int(self.risk["dte"]["max_days"])

    @property
    def kill_switch_pct(self) -> float:
        return float(self.risk["daily_loss"]["kill_switch_pct"])

    @property
    def min_bp_reserve_pct(self) -> float:
        return float(self.risk["buying_power"]["min_reserve_pct"])

    # ---- Strategy knobs.

    @property
    def underlyings(self) -> list[str]:
        return list(self.options["underlyings"])

    @property
    def delta_range(self) -> tuple[float, float]:
        lo, hi = self.options["spread_builder"]["delta_range"]
        return (float(lo), float(hi))

    @property
    def spread_widths(self) -> tuple[float, float]:
        sb = self.options["spread_builder"]
        return (float(sb["min_spread_width"]), float(sb["max_spread_width"]))

    @property
    def iv(self) -> dict[str, Any]:
        return self.options["iv"]

    @property
    def execution(self) -> dict[str, Any]:
        return self.options["execution"]

    @property
    def position_management(self) -> dict[str, Any]:
        return self.options["position_management"]

    @property
    def spread_builder(self) -> dict[str, Any]:
        return self.options["spread_builder"]

    # ---- V2 additions: the model loop and the MCP data layer.

    @property
    def llm(self) -> dict[str, Any]:
        return self.agent["llm"]

    @property
    def max_turns_per_ticker(self) -> int:
        return int(self.agent["max_turns_per_ticker"])

    @property
    def cycle_timeout_seconds(self) -> int:
        return int(self.agent["cycle_timeout_seconds"])

    @property
    def chain(self) -> dict[str, Any]:
        """Chain-fetch settings: feed, strike bracket, and result limit."""
        return self.agent["chain"]

    @property
    def options_feed(self) -> str:
        """Always passed explicitly.

        The MCP server's schema default is ``opra``, and an account without a
        signed OPRA agreement gets a 403 rather than a silent downgrade to the
        free indicative feed.
        """
        return str(self.chain["feed"])


# Every key that must exist before a run is allowed to begin. Checked eagerly so
# a typo surfaces at startup rather than three hours into an unattended session.
_REQUIRED_RISK_KEYS = [
    ("delta", "max_abs"),
    ("notional", "max_loss_pct"),
    ("portfolio", "max_delta_dollars_pct"),
    ("premium", "min_credit_usd"),
    ("dte", "min_days"),
    ("dte", "max_days"),
    ("daily_loss", "kill_switch_pct"),
    ("buying_power", "min_reserve_pct"),
]

_REQUIRED_AGENT_KEYS = [
    ("llm", "provider"),
    ("llm", "model"),
    ("max_turns_per_ticker",),
    ("cycle_timeout_seconds",),
    ("chain", "feed"),
    ("chain", "strike_bracket_pct"),
]

_REQUIRED_OPTIONS_KEYS = [
    ("underlyings",),
    ("iv", "high_regime_min"),
    ("iv", "low_regime_max"),
    ("spread_builder", "delta_range"),
    ("spread_builder", "min_spread_width"),
    ("spread_builder", "max_spread_width"),
    ("execution", "limit_price_mode"),
    ("position_management", "exit_if_profit_pct"),
]


def _dig(mapping: dict, path: tuple[str, ...]):
    node = mapping
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(".".join(path))
        node = node[key]
    return node


def _validate(mapping: dict, required: list[tuple], label: str) -> None:
    missing = []
    for path in required:
        try:
            _dig(mapping, path)
        except KeyError as exc:
            missing.append(str(exc))
    if missing:
        raise ConfigError(f"{label} is missing required key(s): {', '.join(missing)}")


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def load_credentials() -> Credentials:
    """Read Alpaca credentials from the environment.

    Raises rather than returning a partial object: a run with half a key pair
    would fail later at a much less obvious place.
    """
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise ConfigError(
            "ALPACA_API_KEY and ALPACA_SECRET_KEY must both be set. "
            "Copy .env.example to .env and fill them in."
        )
    # ALPACA_PAPER_TRADE is what the MCP server itself reads; ALPACA_PAPER is
    # accepted too so a .env carried over from the previous build still works.
    raw = os.getenv("ALPACA_PAPER_TRADE") or os.getenv("ALPACA_PAPER") or "true"
    paper = raw.strip().lower() not in ("false", "0", "no", "off")
    return Credentials(api_key=api_key, secret_key=secret_key, paper=paper)


def load_config(with_credentials: bool = True) -> AgentConfig:
    """Load and validate both config files.

    ``with_credentials=False`` is for unit tests and for the pure-computation
    paths (risk gate, spread builder) that never touch the network.
    """
    risk = _load_json(CONFIG_DIR / "risk_config.json")
    options = _load_json(CONFIG_DIR / "options_config.json")
    agent = _load_json(CONFIG_DIR / "agent_config.json")

    _validate(risk, _REQUIRED_RISK_KEYS, "risk_config.json")
    _validate(options, _REQUIRED_OPTIONS_KEYS, "options_config.json")
    _validate(agent, _REQUIRED_AGENT_KEYS, "agent_config.json")

    if risk["dte"]["min_days"] > risk["dte"]["max_days"]:
        raise ConfigError("risk_config.json: dte.min_days cannot exceed dte.max_days")
    if not 0 < risk["notional"]["max_loss_pct"] < 1:
        raise ConfigError("risk_config.json: notional.max_loss_pct must be a fraction between 0 and 1")
    if not 0 < risk["daily_loss"]["kill_switch_pct"] < 1:
        raise ConfigError("risk_config.json: daily_loss.kill_switch_pct must be a fraction between 0 and 1")

    lo, hi = options["spread_builder"]["delta_range"]
    if lo > hi:
        raise ConfigError("options_config.json: spread_builder.delta_range must be [low, high]")

    for directory in (DATA_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)

    if agent["cycle_timeout_seconds"] >= agent.get("cycle_interval_seconds", 300):
        raise ConfigError(
            "agent_config.json: cycle_timeout_seconds must stay below "
            "cycle_interval_seconds, or a hung provider stalls the next cron run."
        )

    return AgentConfig(
        risk=risk,
        options=options,
        agent=agent,
        credentials=load_credentials() if with_credentials else None,
        paths={
            "journal": JOURNAL_PATH,
            "iv_history": IV_HISTORY_PATH,
            "baseline": BASELINE_PATH,
            "logs": LOG_DIR,
            "data": DATA_DIR,
        },
    )
