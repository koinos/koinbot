"""Latest-X-post integration via Nitter RSS.

X's official API cannot read tweets without a paid tier, so this
fetches the public Nitter RSS feed for @KoinosNetwork. Nitter is an
unofficial scraper and notoriously flaky — every code path degrades
gracefully: /x falls back to a plain profile link, the auto-poster
just retries on the next cycle.

Feed items are untrusted external input, and the mirror is a free
pseudonymous service nobody here controls, so the relay is written as
if the feed were hostile: the only clickable element in a posted
message is the canonical x.com link, rebuilt from the numeric status
ID. Relayed text is HTML-escaped AND every URL in it is defanged,
because escaping stops markup but not Telegram's automatic linking of
bare URLs. @mentions are defused so a relayed post cannot ping anyone.
The feed body is parsed with regexes (no XML parser, so no
entity-expansion attack surface) under a hard size cap.
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import aiohttp

import links

logger = logging.getLogger(__name__)

PROFILE_NAME = '@KoinosNetwork'
PROFILE_URL = 'https://x.com/koinosnetwork'
DEFAULT_FEED_URLS = 'https://nitter.net/koinosnetwork/rss'


def _feed_urls():
    # Comma-separated X_FEED_URLS overrides the default so a dead
    # Nitter instance can be swapped without a code deploy. Resolved
    # lazily (see _state_file).
    raw = os.environ.get('X_FEED_URLS', DEFAULT_FEED_URLS)
    return [u.strip() for u in raw.split(',') if u.strip()]
def _state_file():
    # Resolved lazily so load_dotenv() in the main module (which runs
    # after imports) can still supply X_STATE_FILE.
    return Path(os.environ.get('X_STATE_FILE', '/app/state/xfeed.json'))
CACHE_TTL = 600  # /x serves a cached post for up to 10 minutes
FETCH_TIMEOUT = 10
MAX_FEED_BYTES = 1_000_000
MAX_POST_TEXT = 900
MAX_POSTS_PER_CYCLE = 3

_ITEM_RE = re.compile(r'<item>(.*?)</item>', re.S)
_TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)
_LINK_RE = re.compile(r'<link>(.*?)</link>', re.S)
_DATE_RE = re.compile(r'<pubDate>(.*?)</pubDate>', re.S)
_STATUS_RE = re.compile(r'/status/(\d+)')

_cache = {'ts': 0.0, 'post': None, 'fail_ts': None}
_fetch_lock = asyncio.Lock()
FAIL_TTL = 120  # after a failed fetch, /x serves the fallback this long


def parse_feed(text):
    """Extract original posts from a Nitter RSS body.

    Replies and retweets are dropped by design for BOTH /x and the
    auto-poster — the bot surfaces the account's own announcements,
    not conversation noise. Returns posts newest-first:
    [{'id', 'text', 'date'}, ...].
    """
    posts = []
    for m in _ITEM_RE.finditer(text):
        item = m.group(1)
        title_m = _TITLE_RE.search(item)
        link_m = _LINK_RE.search(item)
        if not link_m:
            continue
        # Media-only posts may carry an empty or self-closing <title/>.
        tweet_text = html.unescape(title_m.group(1)).strip() if title_m else ''
        # Nitter prefixes replies with "R to @user:" and retweets with
        # "RT by @user:" — only original posts belong in the feed.
        if tweet_text.startswith(('R to ', 'RT by ')):
            continue
        status_m = _STATUS_RE.search(link_m.group(1))
        if not status_m:
            continue
        date_m = _DATE_RE.search(item)
        posts.append({
            'id': int(status_m.group(1)),
            'text': tweet_text or '📷 (media post)',
            'date': html.unescape(date_m.group(1)).strip() if date_m else '',
        })
    posts.sort(key=lambda p: p['id'], reverse=True)
    return posts


def _relayed(raw):
    """Someone else's words, rendered so they cannot act on the reader.

    Escaping alone is not enough here: Telegram auto-links bare URLs in
    plain text, so a hostile or hijacked mirror could publish a working
    phishing link under an official-looking header. Every URL in the
    relayed body is therefore defanged rather than removed — the reader
    still sees exactly what was written, and the canonical link below
    the post is the only thing that stays clickable, which also makes
    it the only candidate for the link-preview card.
    """
    # Mentions first: "user@evil.tld" hides a bare domain from the URL
    # pass until the @ is broken, so defusing afterwards would leave a
    # live link behind.
    text = links.defuse_mentions(raw)
    text = links.defang(text)
    return html.escape(text, quote=False)


def format_post(post, header):
    """Render a post as a Telegram-HTML message."""
    # Truncate the raw text BEFORE escaping — slicing afterwards could
    # split an entity like &amp; and produce invalid Telegram HTML.
    # Defanging runs after the cut for the same reason it does in kai:
    # slicing a string can expose a URL that was not there before.
    raw = post['text']
    if len(raw) > MAX_POST_TEXT:
        raw = raw[:MAX_POST_TEXT] + '…'
    url = f'https://x.com/KoinosNetwork/status/{post["id"]}'
    lines = [header, '', _relayed(raw), '']
    if post.get('date'):
        lines.append(f'🕐 <i>{_relayed(post["date"])}</i>')
    lines.append(f'🔗 <a href="{url}">View on X</a>')
    return '\n'.join(lines)


def fallback_message():
    return (f'🐦 Could not reach the X feed right now.\n'
            f'🔗 <a href="{PROFILE_URL}">{PROFILE_NAME} on X</a>')


def _read_feed_file(path):
    """A host-side curl timer drops the feed here: the public Nitter
    mirrors fingerprint Python's TLS stack and 403 us regardless of
    headers, while the host's curl passes. Stale files (fetcher broken)
    count as unavailable rather than serving old news forever."""
    st = os.stat(path)
    max_age = int(os.environ.get('X_FEED_FILE_MAX_AGE', '7200') or 7200)
    if time.time() - st.st_mtime > max_age:
        raise RuntimeError('cached feed file is stale')
    with open(path, 'rb') as f:
        return f.read(MAX_FEED_BYTES)


async def _fetch_http(url):
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Some feed mirrors whitelist known RSS-reader agents;
        # X_FEED_UA lets us present as one without a deploy.
        ua = os.environ.get('X_FEED_UA', '').strip() or 'Mozilla/5.0 (koinbot)'
        async with session.get(url, headers={'User-Agent': ua},
                               allow_redirects=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f'HTTP {resp.status}')
            # read(n) may return a partial chunk — accumulate
            # until EOF or the size cap.
            chunks, total = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_FEED_BYTES:
                    break
            return b''.join(chunks)


async def fetch_posts():
    """Fetch and parse the feed. Returns [] when unavailable."""
    last_err = None
    for url in _feed_urls():
        try:
            if url.startswith('file://'):
                body = _read_feed_file(url[len('file://'):])
            else:
                body = await _fetch_http(url)
            posts = parse_feed(body.decode('utf-8', 'replace'))
            if posts:
                return posts
            last_err = RuntimeError('feed parsed but contained no posts')
        except Exception as e:
            last_err = e
    logger.warning(f'X feed unavailable: {last_err}')
    return []


async def get_latest_cached():
    """Newest post for /x, cached; stale data beats no data.

    A lock coalesces concurrent /x calls into one upstream request, and
    failed fetches are negative-cached so a command burst during a
    Nitter outage cannot stampede the container or the upstream.
    """
    now = time.monotonic()
    if _cache['post'] and now - _cache['ts'] < CACHE_TTL:
        return _cache['post']
    async with _fetch_lock:
        now = time.monotonic()
        if _cache['post'] and now - _cache['ts'] < CACHE_TTL:
            return _cache['post']
        if _cache['fail_ts'] is not None and now - _cache['fail_ts'] < FAIL_TTL:
            return _cache['post']
        posts = await fetch_posts()
        if posts:
            _cache['ts'] = time.monotonic()
            _cache['post'] = posts[0]
        else:
            _cache['fail_ts'] = time.monotonic()
    return _cache['post']


def _load_state():
    try:
        return json.loads(_state_file().read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f'could not read X feed state: {e}')
        return {}


def _save_state(state):
    try:
        path = _state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state))
        tmp.replace(path)
    except Exception as e:
        logger.warning(f'could not save X feed state: {e}')


async def autopost_loop(send_message, chat_id, poll_seconds=300):
    """Announce new posts in the main chat.

    The first run only records a baseline so a (re)deploy never reposts
    history; duplicates across restarts are prevented by persisting the
    last announced status ID.
    """
    state = _load_state()
    logger.info(f'X auto-post loop started (chat {chat_id}, every {poll_seconds}s)')
    while True:
        try:
            posts = await fetch_posts()
            if posts:
                _cache['ts'] = time.monotonic()
                _cache['post'] = posts[0]
                last = state.get('last_posted_id')
                if last is None:
                    state['last_posted_id'] = posts[0]['id']
                    _save_state(state)
                else:
                    new_posts = sorted(
                        (p for p in posts if p['id'] > last),
                        key=lambda p: p['id'],
                    )[:MAX_POSTS_PER_CYCLE]
                    for p in new_posts:
                        sent = await send_message(
                            chat_id,
                            format_post(p, f'🐦 <b>New post from {PROFILE_NAME}</b>'),
                            link_preview=True,
                        )
                        if sent:
                            state['last_posted_id'] = p['id']
                            _save_state(state)
                        else:
                            break  # sending failed; retry this post next cycle
        except Exception as e:
            logger.error(f'X autopost loop error: {e}')
        await asyncio.sleep(poll_seconds)
