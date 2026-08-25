#!/usr/bin/env bash
#
# Install the market-hours cron schedule for the options agent.
#
#   ./install_cron.sh              # dry-run mode (default) — analyses, places nothing
#   ./install_cron.sh --live       # LIVE mode — submits real orders to the account
#   ./install_cron.sh --uninstall  # remove the schedule
#
# Dry-run is the default deliberately. Installing a schedule and starting
# autonomous order placement are two different decisions, and the second one
# should be made on purpose.
#
# The schedule runs EVERY five minutes, all hours, and lets cron_runner.py decide
# whether to act. That is deliberate.
#
# The obvious version — "*/5 9-16" with CRON_TZ=America/New_York — does not work
# here. Ubuntu's cron 3.0pl1 ignored CRON_TZ on this machine and evaluated the
# hour range against local time instead, so a PKT box ran the agent 09:00-16:00
# PKT, which is 00:00-07:00 in New York: precisely when the market is shut. The
# failure is silent, because a schedule that never fires looks identical to one
# that has nothing to do.
#
# Rather than depend on a timezone feature that may or may not be honoured,
# every cycle asks Alpaca what time it is. A no-op costs one get_clock call and
# exits in about three seconds, and it is immune to DST on either continent.
#
# Fifteen minutes, not five. Groq's free tier allows 1,000 requests a day but
# only 8,000 tokens a minute, and one ticker costs roughly 6,000-12,000 tokens
# across its turns. At a five-minute cadence three underlyings saturate that
# window continuously: every cycle spent its time waiting on 429s and none of
# them finished. At fifteen the budget recovers between cycles and a cycle
# completes in under a minute. Raise it back only with a paid tier.
#
# PATH is set explicitly because cron's default is /usr/bin:/bin, and the MCP
# server is launched as `uvx alpaca-mcp-server` from ~/.local/bin. Without this
# every scheduled cycle fails at connect time while working perfectly when run
# by hand — the exact failure that hides until someone checks the dashboard.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="# alpaca-options-agent"
MODE="--dry-run"

case "${1:-}" in
  --live)
    MODE="--live"
    ;;
  --uninstall)
    crontab -l 2>/dev/null | sed "/${MARKER}\$/,/${MARKER}-end\$/d" | crontab - || true
    echo "Schedule removed."
    crontab -l 2>/dev/null || echo "(crontab is now empty)"
    exit 0
    ;;
  --dry-run|"")
    ;;
  *)
    echo "Unknown option: $1" >&2
    echo "Usage: $0 [--dry-run | --live | --uninstall]" >&2
    exit 2
    ;;
esac

if [[ "${MODE}" == "--live" ]]; then
  echo "About to schedule LIVE order placement every 5 minutes during market hours."
  echo "Account: the one configured in ${REPO}/.env"
  echo "The model cannot place these orders — it proposes, and a nine-rule gate decides."
  read -r -p "Type 'live' to confirm: " CONFIRM
  if [[ "${CONFIRM}" != "live" ]]; then
    echo "Aborted. Nothing was installed."
    exit 1
  fi
fi

mkdir -p "${REPO}/logs"

BLOCK="$(cat <<CRON
${MARKER}
PATH=${HOME}/.local/bin:${HOME}/go/bin:/usr/local/bin:/usr/bin:/bin
*/15 * * * * cd ${REPO} && ${REPO}/.venv/bin/python cron_runner.py ${MODE} >> ${REPO}/logs/cron.log 2>&1; ${REPO}/scripts/push_journal.sh >> ${REPO}/logs/journal_push.log 2>&1
${MARKER}-end
CRON
)"

# Remove any previous install of this block, then append the current one.
EXISTING="$(crontab -l 2>/dev/null | sed "/${MARKER}\$/,/${MARKER}-end\$/d" || true)"
printf '%s\n%s\n' "${EXISTING}" "${BLOCK}" | sed '/^$/d' | crontab -

echo "Installed in ${MODE} mode. Current crontab:"
echo
crontab -l
echo
echo "Watch it run:   tail -f ${REPO}/logs/cron.log"
echo "Switch to live: ${REPO}/install_cron.sh --live"
echo "Remove:         ${REPO}/install_cron.sh --uninstall"
