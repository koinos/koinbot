#!/bin/bash
# Fetch the X feed for koinbot with host curl. Public Nitter mirrors 403
# the container's Python TLS fingerprint regardless of the user agent,
# while curl from the host gets through, so the fetch happens out here
# and the bot reads the result from a file.
#
# This runs as ROOT, which is what makes the destination matter. It used
# to stage and publish inside ./state, the one directory the container
# can write. A symlink planted there by a compromised bot process would
# have been followed by this root-owned write, turning a container-level
# compromise into an arbitrary file write on the host — the only thing
# that undid the container hardening. Both the staging directory and the
# destination are now root-owned and outside the container's reach; the
# bot mounts ./feed read-only.
#
# Installed at /usr/local/bin/koinbot-feedfetch.sh, run by
# koinbot-feedfetch.timer every 5 minutes.
set -u

UA="FreshRSS/1.24.0 (compatible; koinbot; +https://github.com/interfecto/koinbot)"
URL="https://rss.xcancel.com/koinosnetwork/rss"
DIR=/root/koinbot/feed
OUT="$DIR/feed-koinosnetwork.xml"

install -d -o root -g root -m 755 "$DIR" || exit 1

# Stage INSIDE the destination directory. It is root-owned and mounted
# read-only into the container, so nothing there can be a planted
# symlink, and a rename within one directory is atomic: the bot only
# ever sees the previous complete feed or the new complete feed, never
# a half-written one. Staging elsewhere and copying across would lose
# that guarantee.
TMP=$(mktemp "$DIR/.feed.XXXXXX") || exit 1
trap 'rm -f "$TMP"' EXIT

# --max-filesize bounds a hostile mirror; the feed is normally ~25 KB.
if curl -sf -m 20 --max-filesize 2000000 -A "$UA" "$URL" -o "$TMP"; then
    # Only publish real feeds — whitelist and error notices carry no
    # status links, and publishing one would blank the bot's feed.
    if grep -q "/status/" "$TMP"; then
        chmod 644 "$TMP"
        mv -f "$TMP" "$OUT"
    fi
fi
