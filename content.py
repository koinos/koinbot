"""Content loading, validation and rendering for koinbot.

All user-facing text lives in content/*.yml so it can be changed via
pull request without touching code. The server-side updater only
auto-deploys merges that touch content/ exclusively — code changes
always require a manual deploy.

Run `python3 content.py` to validate the content files (used by CI and
by the server-side updater before restarting the bot).
"""
import datetime
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import yaml

CONTENT_DIR = Path(__file__).resolve().parent / 'content'

COMMAND_RE = re.compile(r'^[a-z0-9_]{1,32}$')
PROJECT_ID_RE = re.compile(r'^[a-z0-9-]{1,32}$')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Commands implemented in code; content must not shadow them.
RESERVED_COMMANDS = {
    'info', 'start', 'menu', 'report',
    'projects', 'project', 'updates', 'x', 'kai',
    'guides', 'docs', 'international', 'exchange', 'exchanges', 'cex',
    'buy', 'media', 'social', 'stake', 'whitepaper', 'wallets',
}

# Menu keys the inline keyboard in telegrambot.py links to.
REQUIRED_MENUS = {
    'guides', 'exchanges', 'wallets', 'international',
    'social', 'stake', 'whitepaper',
}


# Tags Telegram accepts in parse_mode='HTML'.
ALLOWED_TAGS = {
    'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
    'a', 'code', 'pre', 'tg-spoiler', 'span', 'blockquote',
}

# Telegram only supports these named entities (plus all numeric ones).
ALLOWED_ENTITIES = {'lt', 'gt', 'amp', 'quot'}

# Headroom under Telegram's 4096-character message limit.
MAX_MESSAGE_LEN = 4000
MAX_UPDATE_LEN = 500
# The welcome template expands {usernames} at runtime; keep headroom.
MAX_WELCOME_LEN = 3000


class ContentError(Exception):
    pass


def _check(cond, msg):
    if not cond:
        raise ContentError(msg)


def _valid_link(url):
    if any(c in url for c in ' <>"\''):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme in ('http', 'https'):
        return bool(parts.hostname)
    if parts.scheme == 'tg':
        return bool(parts.netloc or parts.path)
    return False


class _TelegramHTMLChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []
        self.errors = []

    def handle_entityref(self, name):
        if name not in ALLOWED_ENTITIES:
            self.errors.append(f'entity &{name}; is not supported by Telegram')

    def handle_charref(self, name):
        pass  # numeric entities are supported

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f'unsupported tag <{tag}>')
            return
        # Telegram allows attributes only in these exact forms.
        if tag == 'a':
            if [k for k, _ in attrs] != ['href'] or not attrs[0][1]:
                self.errors.append('<a> needs exactly one non-empty href attribute')
            elif not _valid_link(attrs[0][1]):
                self.errors.append(f'<a> href is not a valid http(s)/tg link: {attrs[0][1]}')
        elif tag == 'span':
            if attrs != [('class', 'tg-spoiler')]:
                self.errors.append('<span> is only supported as <span class="tg-spoiler">')
        elif tag == 'code':
            if attrs and not (len(attrs) == 1 and attrs[0][0] == 'class'
                              and (attrs[0][1] or '').startswith('language-')):
                self.errors.append('<code> only allows class="language-..."')
        elif tag == 'blockquote':
            if attrs and [k for k, _ in attrs] != ['expandable']:
                self.errors.append('<blockquote> only allows the expandable attribute')
        elif attrs:
            self.errors.append(f'<{tag}> must not have attributes')
        # Telegram forbids formatting inside pre/code except <pre><code>.
        if any(t in ('pre', 'code') for t in self.stack) and \
                not (tag == 'code' and self.stack[-1] == 'pre'):
            self.errors.append(f'<{tag}> is not allowed inside <pre>/<code>')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f'unsupported closing tag </{tag}>')
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f'mismatched closing tag </{tag}>')
        else:
            self.stack.pop()

    def handle_startendtag(self, tag, attrs):
        self.errors.append(f'unsupported self-closing tag <{tag}/>')

    def handle_comment(self, data):
        self.errors.append('HTML comments are not supported by Telegram')

    def handle_decl(self, decl):
        self.errors.append(f'unsupported declaration <!{decl}>')

    def handle_pi(self, data):
        self.errors.append(f'unsupported processing instruction <?{data}>')


_TAG_RE = re.compile(r'</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?>')
_BARE_AMP_RE = re.compile(r'&(?!(?:lt|gt|amp|quot|#[0-9]+|#x[0-9a-fA-F]+);)')


def _check_html(text, where):
    """Telegram rejects messages with invalid HTML at send time, so bad
    markup must never pass validation and reach the running bot."""
    checker = _TelegramHTMLChecker()
    checker.feed(text)
    checker.close()
    if checker.stack:
        checker.errors.append('unclosed tags: ' + ', '.join(checker.stack))
    # HTMLParser silently buffers incomplete tags like "hello <b" and
    # treats stray angle brackets as text — Telegram rejects both.
    leftover = _TAG_RE.sub('', text)
    if '<' in leftover or '>' in leftover:
        checker.errors.append('unescaped or incomplete "<" / ">" (use &lt; / &gt;)')
    if _BARE_AMP_RE.search(leftover):
        checker.errors.append('unescaped "&" (use &amp;)')
    _check(not checker.errors,
           f'{where}: invalid Telegram HTML: {"; ".join(checker.errors)}')


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of silently
    keeping the last value (which would let a PR overwrite content
    without any validation error)."""


def _construct_mapping_no_dups(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f'duplicate mapping key {key!r}', key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_no_dups)


def _load_yaml(name):
    path = CONTENT_DIR / name
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.load(f, Loader=_StrictLoader)
    except FileNotFoundError:
        raise ContentError(f'{name}: file not found')
    except yaml.YAMLError as e:
        raise ContentError(f'{name}: invalid YAML: {e}')
    _check(isinstance(data, dict), f'{name}: top level must be a mapping')
    return data


def load():
    """Load and validate all content files.

    Returns (texts, commands, menus, projects).
    """
    data = _load_yaml('commands.yml')

    texts = data.get('texts')
    _check(isinstance(texts, dict), 'commands.yml: "texts" mapping is required')
    for key in ('main_menu', 'welcome'):
        _check(isinstance(texts.get(key), str) and texts[key].strip(),
               f'commands.yml: texts.{key} must be a non-empty string')
        # welcome expands {usernames} at runtime — reserve room for it.
        limit = MAX_WELCOME_LEN if key == 'welcome' else MAX_MESSAGE_LEN
        _check(len(texts[key]) <= limit,
               f'commands.yml: texts.{key} exceeds {limit} characters')
        _check_html(texts[key], f'commands.yml: texts.{key}')
    _check('{usernames}' in texts['welcome'],
           'commands.yml: texts.welcome must contain the {usernames} placeholder')

    commands = data.get('commands')
    _check(isinstance(commands, dict) and commands,
           'commands.yml: "commands" mapping is required')
    seen = set()
    for name, cfg in commands.items():
        _check(isinstance(cfg, dict),
               f'commands.yml: command "{name}" must be a mapping')
        unknown = set(cfg) - {'text', 'aliases', 'link_preview'}
        _check(not unknown,
               f'commands.yml: command "{name}" has unknown keys: {sorted(unknown)}')
        _check(isinstance(cfg.get('text'), str) and cfg['text'].strip(),
               f'commands.yml: command "{name}" needs a non-empty "text"')
        _check(len(cfg['text']) <= MAX_MESSAGE_LEN,
               f'commands.yml: command "{name}" text exceeds {MAX_MESSAGE_LEN} characters')
        _check(isinstance(cfg.get('link_preview', False), bool),
               f'commands.yml: command "{name}": link_preview must be true/false')
        _check_html(cfg['text'], f'commands.yml: command "{name}"')
        aliases = cfg.get('aliases', [])
        _check(isinstance(aliases, list) and all(isinstance(a, str) for a in aliases),
               f'commands.yml: command "{name}": aliases must be a list of strings')
        for cmd in [name, *aliases]:
            _check(COMMAND_RE.match(cmd),
                   f'commands.yml: invalid command name "{cmd}" (a-z, 0-9, _ only)')
            _check(cmd not in RESERVED_COMMANDS,
                   f'commands.yml: command "{cmd}" is reserved by the bot code')
            _check(cmd not in seen, f'commands.yml: duplicate command "{cmd}"')
            seen.add(cmd)

    menus = data.get('menus')
    _check(isinstance(menus, dict), 'commands.yml: "menus" mapping is required')
    missing = REQUIRED_MENUS - set(menus)
    _check(not missing, f'commands.yml: missing menu entries: {sorted(missing)}')
    for key, text in menus.items():
        _check(isinstance(text, str) and text.strip(),
               f'commands.yml: menu "{key}" must be a non-empty string')
        _check(len(text) <= MAX_MESSAGE_LEN,
               f'commands.yml: menu "{key}" exceeds {MAX_MESSAGE_LEN} characters')
        _check_html(text, f'commands.yml: menu "{key}"')

    pdata = _load_yaml('projects.yml')
    projects = pdata.get('projects')
    _check(isinstance(projects, list) and projects,
           'projects.yml: "projects" list is required')
    ids = set()
    for i, p in enumerate(projects):
        where = f'projects.yml: project #{i + 1}'
        _check(isinstance(p, dict), f'{where} must be a mapping')
        unknown = set(p) - {'id', 'name', 'url', 'category', 'emoji', 'tagline', 'updates'}
        _check(not unknown, f'{where} has unknown keys: {sorted(unknown)}')
        for key in ('id', 'name', 'url', 'category'):
            _check(isinstance(p.get(key), str) and p[key].strip(),
                   f'{where} needs a non-empty "{key}"')
        _check(PROJECT_ID_RE.match(p['id']),
               f'{where}: id "{p["id"]}" must be lowercase a-z, 0-9, "-"')
        _check(p['id'] not in ids, f'{where}: duplicate id "{p["id"]}"')
        ids.add(p['id'])
        try:
            url_parts = urlsplit(p['url'])
            url_ok = url_parts.scheme in ('http', 'https') and bool(url_parts.hostname)
        except ValueError:
            url_ok = False
        _check(url_ok and not any(c in p['url'] for c in ' <>"\''),
               f'{where}: url must be a valid http(s) URL')
        for key in ('emoji', 'tagline'):
            _check(p.get(key) is None or (isinstance(p[key], str) and p[key].strip()),
                   f'{where}: "{key}" must be a non-empty string if present')
        updates = p.get('updates')
        _check(updates is None or isinstance(updates, list),
               f'{where}: "updates" must be a list')
        for j, u in enumerate(updates or []):
            uwhere = f'{where}, update #{j + 1}'
            _check(isinstance(u, dict), f'{uwhere} must be a mapping')
            unknown = set(u) - {'date', 'text'}
            _check(not unknown, f'{uwhere} has unknown keys: {sorted(unknown)}')
            # Unquoted YYYY-MM-DD scalars arrive as datetime.date from
            # PyYAML; normalize so rendering and sorting see ISO strings.
            # datetime.datetime (a date subclass) means a timestamp was
            # written — reject it to keep the documented format.
            date = u.get('date')
            _check(not isinstance(date, datetime.datetime),
                   f'{uwhere}: date must be YYYY-MM-DD without a time part')
            if isinstance(date, datetime.date):
                u['date'] = date.isoformat()
            else:
                _check(isinstance(date, str) and DATE_RE.match(date),
                       f'{uwhere}: date must be YYYY-MM-DD')
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    _check(False, f'{uwhere}: "{date}" is not a real calendar date')
            _check(isinstance(u.get('text'), str) and u['text'].strip(),
                   f'{uwhere}: needs a non-empty "text"')
            _check(len(u['text']) <= MAX_UPDATE_LEN,
                   f'{uwhere}: text exceeds {MAX_UPDATE_LEN} characters')
            _check_html(u['text'], uwhere)

    return texts, commands, menus, projects


def _esc(s):
    return html.escape(s, quote=False)


def _attr(s):
    return html.escape(s, quote=True)


def _emoji(project):
    return html.escape(project.get('emoji') or '🔹', quote=False)


def _join_clamped(lines):
    """Join message lines, stopping before Telegram's length limit.

    Clamping at line granularity keeps the HTML valid — every generated
    line closes the tags it opens.
    """
    out = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > MAX_MESSAGE_LEN:
            out.append('…')
            break
        out.append(line)
        total += len(line) + 1
    return '\n'.join(out)


def render_projects_overview(projects):
    by_category = {}
    for p in projects:
        by_category.setdefault(p['category'], []).append(p)
    lines = ['🚀 <b>Koinos Ecosystem Projects</b>', '']
    for category, plist in by_category.items():
        lines.append(f'<b>{_esc(category)}:</b>')
        for p in plist:
            tagline = f' - {_esc(p["tagline"])}' if p.get('tagline') else ''
            lines.append(
                f'{_emoji(p)} <a href="{_attr(p["url"])}">{_esc(p["name"])}</a>{tagline}'
            )
        lines.append('')
    lines.append('📰 /updates — latest project updates')
    lines.append('🔎 /project &lt;name&gt; — details for one project')
    lines.append('')
    lines.append('🌟 <i>The ecosystem is growing daily!</i>')
    return _join_clamped(lines)


def render_project_detail(project):
    lines = [f'{_emoji(project)} <b>{_esc(project["name"])}</b>']
    if project.get('tagline'):
        lines.append(f'<i>{_esc(project["tagline"])}</i>')
    lines.append(f'🔗 <a href="{_attr(project["url"])}">{_esc(project["url"])}</a>')
    updates = sorted(project.get('updates') or [],
                     key=lambda u: u['date'], reverse=True)
    if updates:
        lines.append('')
        lines.append('📰 <b>Latest updates:</b>')
        for u in updates[:5]:
            lines.append(f'• <i>{u["date"]}</i> — {u["text"].strip()}')
    return _join_clamped(lines)


def render_updates(projects, limit=5):
    entries = []
    for p in projects:
        for u in p.get('updates') or []:
            entries.append((u['date'], p, u))
    if not entries:
        return ('📰 <b>Project Updates</b>\n\n'
                'No updates yet — check back soon!\n\n'
                '💡 <i>Project teams can submit updates via pull request.</i>')
    entries.sort(key=lambda e: e[0], reverse=True)
    lines = ['📰 <b>Latest Project Updates</b>', '']
    for date, p, u in entries[:limit]:
        lines.append(f'{_emoji(p)} <b>{_esc(p["name"])}</b> — <i>{date}</i>')
        lines.append(u['text'].strip())
        lines.append('')
    lines.append('🔎 <i>Details: /project &lt;name&gt;</i>')
    return _join_clamped(lines)


def find_project(projects, query):
    q = query.strip().lower()
    for p in projects:
        if p['id'] == q or p['name'].lower() == q:
            return p
    for p in projects:
        if q in p['name'].lower():
            return p
    return None


if __name__ == '__main__':
    try:
        load()
    except ContentError as e:
        print(f'Content validation FAILED: {e}', file=sys.stderr)
        sys.exit(1)
    print('Content OK')
