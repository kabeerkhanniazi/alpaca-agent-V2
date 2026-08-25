#!/usr/bin/env bash
#
# Publish the trade journal so the deployed dashboard can read it.
#
#   ./scripts/push_journal.sh            # push to the data branch
#   ./scripts/push_journal.sh --snapshot # also refresh the committed snapshot
#
# The dashboard on Streamlit Cloud has no journal of its own: data/ is
# gitignored and the Cloud filesystem is ephemeral. This pushes the journal to
# an orphan `data` branch, which the dashboard fetches by raw URL.
#
# Two properties matter here:
#
#   The branch is ORPHAN and each push AMENDS a single commit, then force-pushes.
#   Running this every 5 minutes for a week would otherwise add ~2000 commits.
#   This way the branch holds exactly one commit, forever, and `main`'s history
#   stays clean and readable.
#
#   Pushing to `data` does NOT redeploy the app. Streamlit Cloud only rebuilds on
#   pushes to the branch it tracks (main), so the dashboard picks up fresh data
#   on its next cache expiry without an app restart.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOURNAL="${REPO}/data/journal.jsonl"
SNAPSHOT="${REPO}/data/snapshot/journal_snapshot.jsonl"
BRANCH="data"

cd "${REPO}"

if [[ ! -s "${JOURNAL}" ]]; then
  echo "No journal at ${JOURNAL} — nothing to publish."
  exit 0
fi

# Refuse to publish anything that looks like a credential. The journal should
# only ever hold trade decisions, but this is the step that makes it public.
#
# Matches secret VALUES, not key NAMES. The earlier version also matched the
# bare words "API_KEY" and "SECRET_KEY", which appear legitimately in prose —
# the agent's own 401 message says "check ALPACA_API_KEY and ALPACA_SECRET_KEY
# in .env" — so a helpful error silently blocked every publish for days while
# the dashboard sat on stale data. A guard that cries wolf is worse than no
# guard, because the failure looks like nothing happening.
SECRET_PATTERNS=(
  'PK[A-Z0-9]{16,}'                                  # Alpaca key id
  'sk-or-v1-[A-Za-z0-9]{20,}'                        # OpenRouter
  'gsk_[A-Za-z0-9]{30,}'                             # Groq
  '(API_KEY|SECRET_KEY|TOKEN)"?[[:space:]]*[:=][[:space:]]*"?[A-Za-z0-9_-]{16,}'
)
for pattern in "${SECRET_PATTERNS[@]}"; do
  if grep -qE "${pattern}" "${JOURNAL}"; then
    echo "REFUSING TO PUSH: the journal contains something key-shaped." >&2
    echo "  matched: ${pattern}" >&2
    echo "  offending line(s):" >&2
    grep -nE "${pattern}" "${JOURNAL}" | head -3 | cut -c1-160 >&2
    exit 1
  fi
done

if [[ "${1:-}" == "--snapshot" ]]; then
  mkdir -p "$(dirname "${SNAPSHOT}")"
  cp "${JOURNAL}" "${SNAPSHOT}"
  echo "Snapshot refreshed at ${SNAPSHOT} — commit it to main to update the fallback."
fi

WORKTREE="$(mktemp -d)"
trap 'git worktree remove --force "${WORKTREE}" 2>/dev/null || true; rm -rf "${WORKTREE}"' EXIT

# A detached worktree keeps the main checkout untouched, so this can run from
# cron while you are working in the repo.
if git show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git worktree add --force "${WORKTREE}" "${BRANCH}" >/dev/null 2>&1
else
  git worktree add --force --detach "${WORKTREE}" >/dev/null 2>&1
  git -C "${WORKTREE}" checkout --orphan "${BRANCH}" >/dev/null 2>&1
  git -C "${WORKTREE}" rm -rf . >/dev/null 2>&1 || true
fi

cp "${JOURNAL}" "${WORKTREE}/journal.jsonl"
[[ -f "${REPO}/data/iv_history.jsonl" ]] && cp "${REPO}/data/iv_history.jsonl" "${WORKTREE}/iv_history.jsonl"

cat > "${WORKTREE}/README.md" <<'INNER'
# data branch

Machine-written. Holds the live trade journal the dashboard reads.

This branch is orphan and carries exactly one commit, amended and force-pushed
on every update — otherwise a five-minute publish cadence would bury the real
history under thousands of commits. Do not merge it into `main`.
INNER

git -C "${WORKTREE}" add -A
if git -C "${WORKTREE}" diff --cached --quiet; then
  echo "Journal unchanged since the last push — nothing to do."
  exit 0
fi

STAMP="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
LINES="$(wc -l < "${JOURNAL}")"
MSG="journal snapshot ${STAMP} (${LINES} events)"

# Amend when the branch already has a commit, so history stays at exactly one.
if git -C "${WORKTREE}" rev-parse --verify HEAD >/dev/null 2>&1; then
  git -C "${WORKTREE}" commit --amend -q -m "${MSG}"
else
  git -C "${WORKTREE}" commit -q -m "${MSG}"
fi

git -C "${WORKTREE}" push --force -q origin "${BRANCH}"
echo "Published ${LINES} events to the '${BRANCH}' branch at ${STAMP}."
