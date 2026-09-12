"""Live KOIN price for /price.

Two different assets get two different lines, on purpose:

  * native KOIN on Koinos, quoted from the two KoinDX stablecoin pools
  * bridged vKOIN on Ethereum / Base / Solana, from DexScreener

They are separate tokens behind a bridge and they trade 6-9% apart, so
merging them into one "KOIN price" would be wrong. CoinGecko is not used
at all: its koinos feed has no reporting market left (every CEX delisted)
and has served the same frozen tick since 2026-03-27 while still
answering HTTP 200 — exactly the failure mode this module refuses.

The native market is tiny (low four figures of stablecoin). A few hundred
dollars moves the quote by double digits, so the number is only ever
published with its venue, its method, its depth and a quantitative
thin-liquidity warning. Everything here is built to print NOTHING rather
than print something wrong:

  * every figure is read from two independent hosts and must agree
  * pool reserves are cross-checked against the pools' real token
    balances, which also settles which side is the base token
  * chain head must be current, and the pools must have traded recently
  * native and bridged must be within a sane band of each other
  * on any doubt, render_block() returns the no-data block

render_block() never raises and never returns the placeholder.
"""
import asyncio
import logging
import os
import time
from decimal import Decimal, InvalidOperation

import aiohttp

logger = logging.getLogger(__name__)

PLACEHOLDER = '{price}'

KOIN = '19GYjDBVXU7keLbYvMLazsGQn3GTWHjHkK'

# KoinDX KOIN/stable pools. Hardcoded, never discovered at runtime: a
# pool address picked up dynamically is a pool an attacker can create.
POOLS = (
    {'addr': '1E8AjPZxKLJGFouXtNNkPcYxAgsTh4AUTN',
     'quote': 'vUSDT', 'quote_addr': '12VoHz41a4HtfiyhTWbg9RXqGMRbYk6pXh'},
    {'addr': '19ygpmxRiM9zU7yiVc9wnbtHEK4Aa6s2gL',
     'quote': 'vUSDC', 'quote_addr': '1N8iYrYEJdCVK1rhbqv3qZUzHcpoeKmFnj'},
)

DEFAULT_HOSTS = 'https://koinosscan.com,https://api.koinos.io'
HEAD_RPC = 'https://api.koinos.io/'

# Bridged vKOIN. chain id is DexScreener's, checked against the response
# so a pair on an unexpected chain cannot slip in.
VKOIN = (
    ('Ethereum', 'ethereum', '0xa50ad3a559A10f384a5bB2e27516f63E0B937b1A'),
    ('Base', 'base', '0x9b61660Cb1a6920E9c912570cD210020B956F34E'),
    ('Solana', 'solana', '8AUxdPqYU4FBy5rZDhMJxTniPs7gtEfdHjP3UKM71m6G'),
)
DEX_TOKENS_URL = 'https://api.dexscreener.com/latest/dex/tokens/'

# api.koinos.io rejects a bare python user agent.
UA = 'koinbot/1.0 (+https://github.com/interfecto/koinbot)'

SATS = Decimal(10) ** 8

CACHE_TTL = 120          # serve a fresh read for this long
FAIL_TTL = 120           # after a failure, do not hammer upstream
MAX_SERVE_AGE = 900      # never serve a cached block older than this
DEADLINE = 10            # whole refresh budget
FETCH_TIMEOUT = 6        # per request
MAX_BYTES = 262_144      # cap before any parsing

HOST_TOLERANCE = Decimal('0.01')      # cross-host price agreement
DEPTH_TOLERANCE = Decimal('0.05')     # cross-host depth agreement
BALANCE_TOLERANCE = Decimal('0.02')   # reserves vs real balances
BALANCE_FLOOR = Decimal(1_000_000)    # 0.01 token, absolute slack
MAX_BASIS = Decimal('0.20')           # native vs bridged divergence
HEAD_LAG = 120                        # chain head may trail this much
HEAD_SKEW = 60                        # ...and lead this much
MAX_TRADE_AGE = 7 * 24 * 3600         # dead market cutoff
MIN_PAIR_LIQUIDITY = Decimal(1000)    # per bridged pair
MIN_PAIR_VOLUME = Decimal(100)        # per bridged pair, rolling 24 h
MIN_BRIDGED_PAIRS = 2

# Absurd-value guards. An upstream is free to answer "1e999999"; without
# these a single junk field formats into a multi-megabyte message.
MIN_SANE_PRICE = Decimal('0.000000000001')
MAX_SANE_PRICE = Decimal('1000000')
MAX_SANE_USD = Decimal('1000000000000')
MAX_BLOCK_CHARS = 1500                # rendered block ceiling

WINDOW_SECONDS = 600     # group-wide budget so /price cannot be a pump
WINDOW_MAX = 20

_cache = {'ts': 0.0, 'block': None, 'fail_ts': None}
_lock = asyncio.Lock()
_window = []


def _hosts():
    raw = os.environ.get('PRICE_HOSTS', '').strip() or DEFAULT_HOSTS
    hosts = [h.strip().rstrip('/') for h in raw.split(',') if h.strip()]
    if len(hosts) != 2:
        raise PriceError('PRICE_HOSTS must name exactly two hosts')
    return hosts


def _enabled():
    return os.environ.get('PRICE_ENABLED', '1').strip() != '0'


class PriceError(Exception):
    """Any reason not to publish a number."""


# --- transport ------------------------------------------------------------

async def _get_json(session, url, method='GET', payload=None):
    kwargs = {'headers': {'User-Agent': UA}}
    if method == 'POST':
        kwargs['json'] = payload
    async with session.request(method, url, **kwargs) as resp:
        if resp.status != 200:
            raise PriceError(f'HTTP {resp.status} from {url}')
        chunks, total = [], 0
        async for chunk in resp.content.iter_chunked(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise PriceError(f'response over {MAX_BYTES} bytes from {url}')
        body = b''.join(chunks)
    try:
        import json
        data = json.loads(body.decode('utf-8', 'strict'))
    except Exception as e:
        raise PriceError(f'unparseable body from {url}: {e}')
    if not isinstance(data, dict):
        raise PriceError(f'non-object body from {url}')
    # A node answering a REST path with a JSON-RPC envelope is a
    # misrouted request, not data. Seen in the wild at HTTP 200.
    if method == 'GET' and ('jsonrpc' in data or 'error' in data):
        raise PriceError(f'JSON-RPC envelope on REST path {url}')
    return data


def _int_field(data, key, where):
    raw = data.get(key)
    if not isinstance(raw, str) or not raw.isdigit():
        raise PriceError(f'{where}: {key} is not an integer string')
    value = int(raw)
    if value <= 0:
        raise PriceError(f'{where}: {key} is not positive')
    return value


def _sane_price(value, where):
    if not (MIN_SANE_PRICE <= value <= MAX_SANE_PRICE):
        raise PriceError(f'{where}: price {value} outside sane range')
    return value


def _sane_usd(value, where):
    if not (0 <= value <= MAX_SANE_USD):
        raise PriceError(f'{where}: usd amount {value} outside sane range')
    return value


def _decimal(raw, where):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        raise PriceError(f'{where}: not a number')
    if not value.is_finite():
        raise PriceError(f'{where}: not finite')
    return value


# --- native KOIN ----------------------------------------------------------

async def _balance_sats(session, host, token, holder):
    data = await _get_json(session, f'{host}/v1/token/{token}/balance/{holder}')
    value = _decimal(data.get('value'), f'{token} balance of {holder}')
    if value < 0:
        raise PriceError(f'negative balance for {holder}')
    return (value * SATS).to_integral_value()


def _close_enough(a, b):
    bigger = max(a, b)
    return abs(a - b) <= max(bigger * BALANCE_TOLERANCE, BALANCE_FLOOR)


async def _pool_quote(session, host, pool):
    """Price of KOIN in the pool's quote token, with its depth.

    Orientation is decided by the pool's real token balances, not
    assumed from field order: a wrong guess would invert the price.
    """
    where = f'{host} pool {pool["addr"]}'
    data = await _get_json(session, f'{host}/v1/contract/{pool["addr"]}/get_reserves')
    reserve_a = _int_field(data, 'reserve_a', where)
    reserve_b = _int_field(data, 'reserve_b', where)
    block_time_ms = _int_field(data, 'block_time', where)

    koin_sats = await _balance_sats(session, host, KOIN, pool['addr'])
    quote_sats = await _balance_sats(session, host, pool['quote_addr'], pool['addr'])

    a_is_koin = _close_enough(koin_sats, Decimal(reserve_a)) and \
        _close_enough(quote_sats, Decimal(reserve_b))
    b_is_koin = _close_enough(koin_sats, Decimal(reserve_b)) and \
        _close_enough(quote_sats, Decimal(reserve_a))
    if a_is_koin == b_is_koin:
        raise PriceError(f'{where}: orientation ambiguous against real balances')

    koin_reserve, quote_reserve = (reserve_a, reserve_b) if a_is_koin else (reserve_b, reserve_a)
    price = Decimal(quote_reserve) / Decimal(koin_reserve)
    if not price.is_finite():
        raise PriceError(f'{where}: derived price not usable')
    _sane_price(price, where)
    quote_sats = Decimal(quote_reserve)
    _sane_usd(quote_sats / SATS, f'{where} depth')
    # Freshness is per pool, not per pool set: a deep pool that stopped
    # trading still dominates the depth weighting, so a fresh shallow
    # pool must not vouch for it.
    age = time.time() - block_time_ms / 1000
    if age > MAX_TRADE_AGE:
        raise PriceError(f'{where}: last trade {age / 86400:.1f} days ago')
    if age < -HEAD_SKEW:
        raise PriceError(f'{where}: block time is in the future')
    return {
        'addr': pool['addr'],
        'price': price,
        'quote_sats': quote_sats,
        'block_time_ms': block_time_ms,
    }


async def _native_on_host(session, host):
    quotes = []
    for pool in POOLS:
        quotes.append(await _pool_quote(session, host, pool))
    if len(quotes) != len(POOLS):
        raise PriceError(f'{host}: incomplete pool set')
    # Depth-weighted: the deeper pool is the better quote.
    total_quote = sum(q['quote_sats'] for q in quotes)
    if total_quote <= 0:
        raise PriceError(f'{host}: empty pools')
    price = sum(q['price'] * q['quote_sats'] for q in quotes) / total_quote
    _sane_price(price, f'{host} weighted price')
    return {
        'price': price,
        'stable_usd': total_quote / SATS,
        'newest_trade_ms': max(q['block_time_ms'] for q in quotes),
        'pools': {q['addr']: q for q in quotes},
    }


async def _check_head(session):
    data = await _get_json(session, HEAD_RPC, 'POST', {
        'jsonrpc': '2.0', 'id': 1, 'method': 'chain.get_head_info', 'params': {},
    })
    result = data.get('result')
    if not isinstance(result, dict):
        raise PriceError('head info missing result')
    head_ms = _int_field(result, 'head_block_time', 'head info')
    now_ms = int(time.time() * 1000)
    if now_ms - head_ms > HEAD_LAG * 1000:
        raise PriceError('chain head is behind')
    if head_ms - now_ms > HEAD_SKEW * 1000:
        raise PriceError('chain head is ahead of the clock')
    return head_ms


async def _native(session):
    await _check_head(session)
    readings = []
    for host in _hosts():
        readings.append(await _native_on_host(session, host))
    first, second = readings[0], readings[1]
    low = min(first['price'], second['price'])
    if low <= 0:
        raise PriceError('native price not positive')
    if abs(first['price'] - second['price']) / low > HOST_TOLERANCE:
        raise PriceError('hosts disagree on the native price')
    # Aggregate agreement is not enough: two per-pool errors in opposite
    # directions cancel out, and identical ratios over wildly different
    # reserves would still publish the first host's depth unchecked.
    if set(first['pools']) != set(second['pools']):
        raise PriceError('hosts priced different pools')
    for addr, a in first['pools'].items():
        b = second['pools'][addr]
        pool_low = min(a['price'], b['price'])
        if pool_low <= 0 or abs(a['price'] - b['price']) / pool_low > HOST_TOLERANCE:
            raise PriceError(f'hosts disagree on pool {addr} price')
        depth_low = min(a['quote_sats'], b['quote_sats'])
        if depth_low <= 0 or abs(a['quote_sats'] - b['quote_sats']) / depth_low > DEPTH_TOLERANCE:
            raise PriceError(f'hosts disagree on pool {addr} depth')
    trade_age = time.time() - first['newest_trade_ms'] / 1000
    return {
        'price': first['price'],
        'liquidity_usd': first['stable_usd'] * 2,
        'trade_age_hours': trade_age / 3600,
    }


# --- bridged vKOIN --------------------------------------------------------

def _qualifying_pairs(data, chain_id, address):
    pairs = data.get('pairs')
    if not isinstance(pairs, list):
        return []
    out = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if pair.get('chainId') != chain_id:
            continue
        base = pair.get('baseToken')
        if not isinstance(base, dict):
            continue
        if str(base.get('address', '')).lower() != address.lower():
            continue
        try:
            price = _decimal(pair.get('priceUsd'), 'priceUsd')
            liquidity = _decimal((pair.get('liquidity') or {}).get('usd'), 'liquidity')
            volume = _decimal((pair.get('volume') or {}).get('h24'), 'volume')
        except PriceError:
            continue
        if liquidity > MAX_SANE_USD or volume > MAX_SANE_USD:
            continue
        if not (MIN_SANE_PRICE <= price <= MAX_SANE_PRICE):
            continue
        # DexScreener exposes no snapshot timestamp, so a rolling 24 h
        # volume floor is the only freshness signal available. It is a
        # weak one: treat the bridged figure as corroboration for the
        # native quote, never as the headline on its own.
        if liquidity < MIN_PAIR_LIQUIDITY or volume < MIN_PAIR_VOLUME:
            continue
        out.append({'price': price, 'liquidity': liquidity})
    return out


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


async def _bridged(session):
    found = []
    for _label, chain_id, address in VKOIN:
        try:
            data = await _get_json(session, DEX_TOKENS_URL + address)
        except PriceError as e:
            logger.info(f'bridged vKOIN on {chain_id} unavailable: {e}')
            continue
        found.extend(_qualifying_pairs(data, chain_id, address))
    if len(found) < MIN_BRIDGED_PAIRS:
        raise PriceError('not enough qualifying bridged pairs')
    median = _sane_price(_median([p['price'] for p in found]), 'bridged median')
    liquidity = _sane_usd(sum(p['liquidity'] for p in found), 'bridged liquidity')
    return {'price': median, 'liquidity_usd': liquidity, 'pairs': len(found)}


# --- rendering ------------------------------------------------------------

def _money(value, places=6):
    return f'${value:,.{places}f}'


def _whole(value):
    return f'${value:,.0f}'


def _build(native, bridged, read_ts):
    stamp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(read_ts))
    stable = native['liquidity_usd'] / 2
    return '\n'.join([
        f'🪙 <b>{_money(native["price"])}</b> — native KOIN on Koinos',
        f'<i>KoinDX KOIN/vUSDT + KOIN/vUSDC · pool reserve ratio · '
        f'{_whole(native["liquidity_usd"])} pool liquidity</i>',
        '',
        f'🌉 <b>{_money(bridged["price"])}</b> — bridged vKOIN on Ethereum, Base and Solana',
        f'<i>DexScreener · median of {bridged["pairs"]} pools · '
        f'{_whole(bridged["liquidity_usd"])} pool liquidity</i>',
        '',
        f'⚠️ <i>Thin liquidity: the KoinDX pools hold about {_whole(stable)} of '
        f'stablecoin, so a few hundred dollars moves this number by double digits. '
        f'Reference only, not financial advice.</i>',
        f'🕐 <i>Read {stamp} · last KoinDX trade '
        f'{native["trade_age_hours"]:.1f} h ago</i>',
    ])


def _build_checked(native, bridged, read_ts):
    block = _build(native, bridged, read_ts)
    if len(block) > MAX_BLOCK_CHARS:
        raise PriceError(f'rendered block is {len(block)} characters')
    return block


NO_DATA = '\n'.join([
    '🤔 <b>No verified price right now.</b>',
    '<i>This bot only prints a number when two independent Koinos nodes agree, '
    'the pool balances check out on-chain and the bridged markets are in range. '
    'Try again in a few minutes.</i>',
])


def _window_ok():
    now = time.monotonic()
    while _window and now - _window[0] > WINDOW_SECONDS:
        _window.pop(0)
    if len(_window) >= WINDOW_MAX:
        return False
    _window.append(now)
    return True


async def _refresh():
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        native = await _native(session)
        bridged = await _bridged(session)
    basis = abs(native['price'] - bridged['price']) / bridged['price']
    if basis > MAX_BASIS:
        logger.warning(
            f'native {native["price"]} and bridged {bridged["price"]} diverge '
            f'by {basis:.1%} — refusing to publish')
        raise PriceError('native and bridged prices diverge too far')
    return _build_checked(native, bridged, time.time())


async def get_block():
    """Rendered price block, or None when nothing may be published."""
    if not _enabled():
        return None
    now = time.monotonic()
    if _cache['block'] and now - _cache['ts'] < CACHE_TTL:
        return _cache['block']
    async with _lock:
        now = time.monotonic()
        if _cache['block'] and now - _cache['ts'] < CACHE_TTL:
            return _cache['block']
        # Judged fresh at the moment of serving, never before an await:
        # a refresh that burns the whole deadline can push the cached
        # block past MAX_SERVE_AGE while we wait on it.
        def servable():
            if not _cache['block']:
                return None
            if time.monotonic() - _cache['ts'] >= MAX_SERVE_AGE:
                return None
            return _cache['block']

        if _cache['fail_ts'] is not None and now - _cache['fail_ts'] < FAIL_TTL:
            return servable()
        if not _window_ok():
            logger.info('price window exhausted; serving cache only')
            return servable()
        try:
            block = await asyncio.wait_for(_refresh(), DEADLINE)
        except Exception as e:
            logger.warning(f'price unavailable: {e}')
            _cache['fail_ts'] = time.monotonic()
            return servable()
        _cache['ts'] = time.monotonic()
        _cache['fail_ts'] = None
        _cache['block'] = block
        return block


async def render(text):
    """Substitute the price placeholder in a content command body.

    Never raises, and never lets the placeholder reach Telegram.
    """
    if PLACEHOLDER not in text:
        return text
    try:
        block = await get_block()
    except Exception as e:
        logger.warning(f'price render failed: {e}')
        block = None
    return text.replace(PLACEHOLDER, block or NO_DATA)
