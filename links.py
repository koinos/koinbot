"""Link handling shared by every path that relays text the bot did not write.

Telegram auto-links bare URLs in plain text, so HTML-escaping is not
enough: escaping stops markup, not link creation. Any channel that
republishes somebody else's words under the bot's identity has to deal
with that separately, or it becomes a phishing delivery service with
the project's credibility attached.

Two strategies, because the two channels want different things:

  strip_links  - replace a non-allowlisted URL with a marker. Used for
                 model output, where a link that should not be there is
                 better deleted than shown.
  defang       - keep the text readable but break auto-linking, by
                 inserting a zero-width space after each dot. Used for
                 relayed posts, where removing the URL would mangle
                 someone's actual words; the reader still sees what was
                 written and can follow the canonical link instead.

Both also defuse @mentions, so relayed text can never ping a real
account.
"""
import re

# Domain labels may be nearly anything Telegram links — including
# emoji/symbol labels like ➡️.ws — so the label class is "no
# whitespace, no sentence punctuation" rather than \w.
URL_RE = re.compile(
    r'(?:[a-z][a-z0-9+.-]*://|tg:|www\.|t\.me/)[^\s<>()"\']+'
    r'|(?<![\w@./])(?:[^\s<>()"\'.,;:!?@\\/]+\.)+[^\W\d_]{2,24}\b(?::\d{1,5})?(?:/[^\s<>()"\']*)?'
    r'|(?<![\w./])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/[^\s<>()"\']*)?',
    re.IGNORECASE)

ZWSP = '​'


def host_allowed(url, allowed_hosts):
    """True only when the URL certainly resolves to an allowed host."""
    # Backslashes and userinfo let the apparent host differ from what
    # clients actually resolve (https://evil.com\.koinos.io/...) —
    # reject them outright instead of trying to parse like a browser.
    if '\\' in url or '@' in url:
        return False
    # Only hierarchical http(s) may pass — tg:, javascript:, ftp: and
    # friends (Telegram auto-links tg: deep links) are always rejected.
    scheme = re.match(r'^([a-z][a-z0-9+.-]*):', url.lower())
    if scheme and scheme.group(1) not in ('http', 'https'):
        return False
    host = re.sub(r'^[a-z][a-z0-9+.-]*://', '', url.lower())
    host = host.split('/', 1)[0].split('?', 1)[0].split('#', 1)[0]
    host = host.split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return any(host == d or host.endswith('.' + d) for d in allowed_hosts)


def strip_links(text, allowed_hosts, marker='[link removed]'):
    """Replace every non-allowlisted URL with a marker."""
    return URL_RE.sub(
        lambda m: m.group(0) if host_allowed(m.group(0), allowed_hosts) else marker,
        text)


def _break(url):
    # A zero-width space after each dot leaves the URL readable and
    # copyable while stopping Telegram from turning it into a link, so
    # it also cannot become the message's link-preview card.
    broken = url.replace('.', '.' + ZWSP)
    # Dots are not enough on their own: tg://resolve?domain=x contains
    # none, and Telegram links it anyway. Break after the scheme colon
    # too, which is invisible and covers every dotless scheme.
    scheme = re.match(r'^[a-z][a-z0-9+.-]*:', broken, re.IGNORECASE)
    if scheme:
        cut = scheme.end()
        broken = broken[:cut] + ZWSP + broken[cut:]
    return broken


def defang(text, allowed_hosts=()):
    """Make every non-allowlisted URL visible but not clickable."""
    return URL_RE.sub(
        lambda m: m.group(0) if host_allowed(m.group(0), allowed_hosts) else _break(m.group(0)),
        text)


def defuse_mentions(text):
    """Stop relayed text from pinging real accounts."""
    return text.replace('@', '@' + ZWSP)
