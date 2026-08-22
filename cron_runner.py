#!/usr/bin/env python3
"""Run one cycle of the MCP-mediated options agent.

    cron_runner.py --dry-run --force --ticker SPY   # offline test, no orders
    cron_runner.py --dry-run                        # scheduled default
    cron_runner.py --live                           # real orders, market hours only

`--dry-run` is the default and `--live` must be given explicitly, so flipping to
real orders is always a deliberate act rather than an omission.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")

from agent.config import ConfigError, load_config  # noqa: E402
from agent.journal import TradeJournal  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.mcp_client import MCPClient  # noqa: E402
from agent.orchestrator import Orchestrator  # noqa: E402

logger = logging.getLogger("cron_runner")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Run the full cycle but never submit an order. Default.")
    mode.add_argument("--live", action="store_true",
                      help="Submit real orders to the configured Alpaca account.")
    parser.add_argument("--ticker", action="append", metavar="SYMBOL",
                        help="Analyse only this underlying. Repeatable.")
    parser.add_argument("--force", action="store_true",
                        help="Skip the market-hours check, for testing outside trading hours.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def configure_logging(verbose: bool, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_dir / "agent.log"))
    except OSError:
        pass  # stdout alone is enough; cron captures it
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # The MCP server is chatty on stderr at INFO and drowns the cycle log.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run(args: argparse.Namespace) -> int:
    config = load_config()
    dry_run = not args.live
    tickers = [t.upper() for t in (args.ticker or config.underlyings)]

    journal = TradeJournal(config.paths["journal"])
    llm = LLMClient.from_config(config.llm)

    logger.info(
        "=== Cycle start (%s) — %s | %s ===",
        "DRY RUN" if dry_run else "LIVE", ", ".join(tickers), llm.describe(),
    )

    async with MCPClient() as mcp:
        missing = mcp.missing_read_tools()
        if missing:
            logger.warning("Allowlisted tools absent from the server: %s", missing)

        orchestrator = Orchestrator(mcp, llm, config, journal, dry_run=dry_run)
        summary = await orchestrator.run_cycle(tickers, force=args.force)

    if summary.get("skipped"):
        logger.info("Cycle %s skipped: market closed.", summary["run_id"])
        return 0

    for ticker, outcome in summary.get("outcomes", {}).items():
        logger.info(
            "%s: %s — %s (turns: %s)",
            ticker, outcome.get("outcome"),
            (outcome.get("detail") or outcome.get("execution", {}).get("message", ""))[:160],
            outcome.get("turns_used", "-"),
        )

    logger.info(
        "=== Cycle %s complete — %d approved, %d submitted, %d rejected, %d exits, %.1fs ===",
        summary["run_id"], summary["approved"], summary["submitted"],
        summary["rejected"], summary["exits"], summary.get("duration_seconds", 0.0),
    )
    return 1 if summary.get("errors") else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose, REPO_ROOT / "logs")
    try:
        return asyncio.run(run(args))
    except ConfigError as exc:
        logger.error("Configuration problem: %s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001 — an unattended run must log, not vanish
        logger.exception("Cycle failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
