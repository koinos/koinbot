# koinbot

Telegram community bot for [Koinos](https://koinos.io): join captcha,
moderation helpers, info commands, and an ecosystem project directory
with per-project updates.

## Updating the bot via pull request

All user-facing text lives in [`content/`](content/) — **you can change
what the bot says without touching code**:

- [`content/commands.yml`](content/commands.yml) — the static
  `/commands` (add a new key = add a new command) and the texts behind
  the menu buttons.
- [`content/projects.yml`](content/projects.yml) — the ecosystem
  project directory shown by `/projects`.

### Adding a project update

Append an entry to your project's `updates` list in
`content/projects.yml`:

```yaml
  - id: my-project
    name: My Project
    url: https://example.com
    category: dApps & Platforms
    updates:
      - date: 2026-08-10
        text: "V2 launched — now with XYZ support"
```

Updates appear in `/updates` (newest first, across all projects) and in
`/project <name>`. Text may use Telegram HTML (`<b>`, `<i>`,
`<a href="...">`).

### How deployment works

1. Open a PR. CI validates the content files.
2. A maintainer reviews and merges.
3. **Merges that only touch `content/` go live automatically within
   ~2 minutes.** Anything touching code requires a manual deploy by the
   server operator — code is never auto-deployed.

## Commands

| Command | Source |
|---|---|
| `/info`, `/start`, `/menu` | code (menu from `texts.main_menu`) |
| `/projects`, `/project <name>`, `/updates` | rendered from `projects.yml` |
| `/x` | latest original X post from @KoinosNetwork — replies/retweets skipped (Nitter RSS, 10 min cache) |
| `@kai <question>` | AI answer from the [Koinos AI](https://koinosai.com) worker network — main group only, rate-limited, output sanitized (see `kai.py`) |
| `@kai` / `@kai <model> <question>` | bare mention lists the live model classes; a leading model id (validated against that list) routes the question to that class |
| `/report` | code |
| `/mana`, `/rules`, `/claim`, `/price`, `/supply`, `/vhpsupply`, `/roadmap`, `/website`, `/programs`, ... | `commands.yml` |

## Running

```bash
cp .env.example .env   # set TELEGRAM_BOT_TOKEN (and optional ADMIN_CHAT_ID / MAIN_CHAT_ID)
mkdir -p state && chown 10001:10001 state   # writable for the container user
docker compose up -d --build
```

The container is hardened: non-root, read-only filesystem, all
capabilities dropped, memory-limited. The bot uses long polling — no
inbound ports are required.

With `MAIN_CHAT_ID` set, new X posts from @KoinosNetwork are announced
in that chat automatically (polled every `X_POLL_SECONDS`, default
5 min; replies/retweets are skipped; the first run only records a
baseline so history is never reposted). Nitter is unofficial and may be
down for stretches — the bot degrades to a profile link and keeps
retrying.

With `KAI_API_URL` set (a [Koinos AI](https://koinosai.com) Core
`/v1/chat/completions` endpoint, e.g. reached over an SSH tunnel),
mentioning `@kai` in the main group gets an AI answer served by the
Koinos AI worker network. Questions are forwarded to an anonymous
third-party worker in plaintext — group messages are public anyway, and
Kai deliberately answers nowhere else (no DMs). Model output is treated
as untrusted: fully HTML-escaped, non-allowlisted links removed,
@-mentions defused, plus per-user cooldown and a global request window
to protect the free token quota.

Validate content locally:

```bash
pip install -r requirements.txt
python content.py
```

## Server-side auto-updater

`deploy/koinbot-update.sh` (installed to `/usr/local/bin`, driven by the
`deploy/koinbot-update.{service,timer}` systemd units) fetches
`origin/main` every 2 minutes and:

- deploys content-only changes (validates them in the bot image first;
  rolls back and keeps the old content if validation fails),
- never applies code changes — it logs to `/var/log/koinbot-update.log`
  and notifies `ADMIN_CHAT_ID` once per pending commit.
