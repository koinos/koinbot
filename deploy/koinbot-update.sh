#!/usr/bin/env bash
# koinbot auto-updater — runs from a systemd timer every 2 minutes.
#
# Auto-deploys merges to origin/main ONLY when every changed file is
# inside content/ (command texts, projects, project updates). Any other
# change (bot code, Dockerfile, this script, ...) is never applied
# automatically — it is logged and, if ADMIN_CHAT_ID is set in .env,
# reported once via Telegram so a human can deploy manually.
#
# Before restarting the bot, the new content is validated inside the
# existing bot image; invalid content is rolled back and reported.
set -euo pipefail

REPO=/root/koinbot
LOG=/var/log/koinbot-update.log
STATE=/var/lib/koinbot-update.notified
IMAGE=koinbot:local

log() {
    echo "$(date -Is) $*" >> "$LOG"
}

notify() {
    # Best-effort Telegram notification, deduplicated per commit+event so
    # a retry that eventually succeeds still reports its success.
    local text="$1" remote="$2" event="$3"
    local key="${remote}:${event}"
    [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$key" ] && return 0
    local token chat_id
    token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$REPO/.env" 2>/dev/null | cut -d= -f2- || true)
    chat_id=$(grep -E '^ADMIN_CHAT_ID=' "$REPO/.env" 2>/dev/null | cut -d= -f2- || true)
    if [ -z "$token" ] || [ -z "$chat_id" ]; then
        echo "$key" > "$STATE"
        return 0
    fi
    # URL via stdin config so the token never appears in argv. State is
    # only recorded on successful delivery so failed sends are retried.
    if curl -sS -m 10 --fail --config - \
            --data-urlencode "chat_id=${chat_id}" \
            --data-urlencode "text=${text}" >/dev/null 2>&1 <<EOF
url = "https://api.telegram.org/bot${token}/sendMessage"
EOF
    then
        echo "$key" > "$STATE"
    else
        log "WARN: notification delivery failed for $key (will retry next run)"
    fi
}

cd "$REPO"
git fetch --quiet origin main

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
[ "$LOCAL" = "$REMOTE" ] && exit 0

# --no-renames: a rename of a code file into content/ must show both
# paths, otherwise it would slip through the content-only gate.
CHANGED=$(git diff --name-only --no-renames "$LOCAL" "$REMOTE")

# No net file changes (e.g. empty commits): fast-forward and move on so
# the updater doesn't stay stuck behind a no-op commit forever.
if [ -z "$CHANGED" ]; then
    git merge --ff-only "$REMOTE" >> "$LOG" 2>&1
    log "FF $REMOTE: no file changes"
    exit 0
fi

# Strict allowlist: ONLY the two known YAML files may auto-deploy. Any
# other path under content/ (e.g. a content/__init__.py that would
# shadow content.py as a package on import) requires a manual deploy.
# Here-string instead of a pipe: with pipefail, grep -q exiting early
# would SIGPIPE echo on large change lists and bypass this gate.
if grep -qvE '^content/(commands|projects)\.yml$' <<< "$CHANGED"; then
    log "SKIP $REMOTE: non-content changes pending, manual deploy required: $(echo "$CHANGED" | tr '\n' ' ')"
    notify "koinbot: new commits on main touch code (not just content/). Manual deploy required on the server. Changed: $(echo "$CHANGED" | tr '\n' ' ')" "$REMOTE" code-pending
    exit 0
fi

git merge --ff-only "$REMOTE" >> "$LOG" 2>&1

if ! docker run --rm --entrypoint python3 \
        -v "$REPO/content:/app/content:ro" \
        "$IMAGE" /app/content.py >> "$LOG" 2>&1; then
    git reset --hard "$LOCAL" >> "$LOG" 2>&1
    log "ROLLBACK $REMOTE: content validation failed"
    notify "koinbot: content on main FAILED validation — rolled back, bot keeps running on previous content. Fix the YAML and merge again." "$REMOTE" validation-failed
    exit 0
fi

# Known tradeoff: a restart drops in-flight captcha state (same as any
# bot restart); a user joining during the ~3min window may skip the
# check. Accepted — deploys are rare and moderation still applies.
# Roll back on restart failure so the next timer run retries the deploy
# instead of seeing LOCAL == REMOTE while the bot still runs old content.
if ! docker restart koinbot >> "$LOG" 2>&1; then
    git reset --hard "$LOCAL" >> "$LOG" 2>&1
    log "RETRY $REMOTE: docker restart failed, rolled back for retry"
    notify "koinbot: docker restart failed during content deploy — will retry on the next timer run." "$REMOTE" restart-failed
    exit 1
fi

log "DEPLOYED $REMOTE: $(echo "$CHANGED" | tr '\n' ' ')"
notify "koinbot: content update deployed ($(echo "$CHANGED" | tr '\n' ' '))" "$REMOTE" deployed
