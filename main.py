import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

from curl_cffi.requests import Session
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PostOrdersArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY
from web3 import Web3

import state as st

load_dotenv()

HOST        = "https://clob.polymarket.com"
GAMMA_API   = "https://gamma-api.polymarket.com"
POLYGON_RPC = os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")

PK           = os.environ.get("PK")
FUNDER       = os.environ.get("FUNDER")
BASE_BET     = float(os.environ.get("ORDER_AMOUNT", "2"))
MAX_BET      = float(os.environ.get("MAX_BET", "64"))
COINS        = {c.strip().lower() for c in os.environ.get("COINS", "btc").split(",")}
DIRECTION    = os.environ.get("DIRECTION", "Up")   # martingale fixed direction

# Strategy selection
STRATEGY       = os.environ.get("STRATEGY", "martingale").lower()  # martingale | follower | both
FOLLOWER_BET   = float(os.environ.get("FOLLOWER_BET", str(BASE_BET)))
RUN_MARTINGALE = STRATEGY in ("martingale", "both")
RUN_FOLLOWER   = STRATEGY in ("follower", "both")

MAX_PRICE = 0.60   # martingale: skip if live BUY price > this

# ---------------------------------------------------------------------------
# Chainlink on-chain price feeds (Polygon mainnet)
# ---------------------------------------------------------------------------

_CHAINLINK_FEEDS: dict[str, str] = {
    "btc":  "0xc907E116054Ad103354f2D350FD2514433D57F6F",
    "eth":  "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "xrp":  "0x785ba89291f676b5386652eB12b30cF361020694",
    "sol":  "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "doge": "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444",
    "bnb":  "0x82a6c4AF830caa6c97bb504425f6A992840954D2",
    "matic":"0xAB594600376Ec9fD91F8e885dADF0CE036862dE0",
}

_AGGREGATOR_ABI = [
    {
        "inputs": [], "name": "latestRoundData",
        "outputs": [
            {"name": "roundId",         "type": "uint80"},
            {"name": "answer",          "type": "int256"},
            {"name": "startedAt",       "type": "uint256"},
            {"name": "updatedAt",       "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view", "type": "function",
    },
    {
        "inputs": [], "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view", "type": "function",
    },
]

_w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))


def get_chainlink_price(coin: str, max_attempts: int = 3) -> Optional[float]:
    """Read latest price from Chainlink on-chain aggregator on Polygon."""
    address = _CHAINLINK_FEEDS.get(coin.lower())
    if not address:
        print(f"  [{coin.upper()}] No Chainlink feed configured")
        return None
    contract = _w3.eth.contract(
        address=Web3.to_checksum_address(address),
        abi=_AGGREGATOR_ABI,
    )
    for attempt in range(1, max_attempts + 1):
        try:
            decimals                     = contract.functions.decimals().call()
            _, answer, _, updated_at, _ = contract.functions.latestRoundData().call()
            price = answer / (10 ** decimals)
            age   = int(time.time()) - updated_at
            print(f"  [{coin.upper()}] Chainlink: ${price:,.4f}  (updated {age}s ago)")
            return price
        except Exception as exc:
            print(f"  [{coin.upper()}] Chainlink error ({attempt}/{max_attempts}): {exc}")
            if attempt < max_attempts:
                time.sleep(2)
    return None


# ---------------------------------------------------------------------------
# Curl helper
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _get_session() -> Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = Session(impersonate="chrome120")
    return _thread_local.session


def _reset_session() -> Session:
    _thread_local.session = Session(impersonate="chrome120")
    return _thread_local.session


def _curl_get(url: str) -> dict:
    for attempt in range(2):
        try:
            resp = _get_session().get(url, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if "35" in str(exc) and attempt == 0:
                _reset_session()
                continue
            raise


# ---------------------------------------------------------------------------
# Market discovery — Martingale (single direction token)
# ---------------------------------------------------------------------------

def _extract_market(event: dict, coin: str) -> Optional[dict]:
    now_utc  = datetime.now(timezone.utc)
    end_date = event.get("endDate", "")
    if end_date:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end_dt <= now_utc:
            return None

    slug      = event.get("slug", "")
    market    = (event.get("markets") or [{}])[0]
    raw_ids   = market.get("clobTokenIds", "[]")
    raw_out   = market.get("outcomes", "[]")
    token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes  = json.loads(raw_out) if isinstance(raw_out, str) else raw_out

    for i, outcome in enumerate(outcomes):
        if outcome.strip().lower() == DIRECTION.lower() and i < len(token_ids):
            return {
                "coin":     coin,
                "slug":     slug,
                "question": market.get("question") or slug,
                "token_id": token_ids[i],
            }
    return None


def fetch_coin_market(coin: str, ts: int,
                      max_attempts: int = 5, delay: int = 6) -> Optional[dict]:
    slug = f"{coin}-updown-5m-{ts}"
    url  = f"{GAMMA_API}/events/slug/{slug}"
    for attempt in range(1, max_attempts + 1):
        try:
            event  = _curl_get(url)
            result = _extract_market(event, coin)
            if result:
                print(f"  [{coin.upper()}] Found: {slug}")
                return result
            print(f"  [{coin.upper()}] Market exists but no usable token ({attempt}/{max_attempts})")
        except Exception as exc:
            print(f"  [{coin.upper()}] Error ({attempt}/{max_attempts}): {exc}")
        if attempt < max_attempts:
            time.sleep(delay)
    return None


def fetch_all_markets(ts: int) -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(COINS)) as pool:
        futures = {pool.submit(fetch_coin_market, coin, ts): coin for coin in COINS}
        results = []
        for future in as_completed(futures):
            coin = futures[future]
            try:
                r = future.result()
                if r:
                    results.append(r)
            except Exception as exc:
                print(f"  [{coin.upper()}] Fetch thread crashed: {exc}")
    return results


# ---------------------------------------------------------------------------
# Market discovery — Follower (both Up and Down tokens)
# ---------------------------------------------------------------------------

def _extract_market_both(event: dict, coin: str) -> Optional[dict]:
    """Extract token IDs for both Up and Down outcomes."""
    now_utc  = datetime.now(timezone.utc)
    end_date = event.get("endDate", "")
    if end_date:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end_dt <= now_utc:
            return None

    slug      = event.get("slug", "")
    market    = (event.get("markets") or [{}])[0]
    raw_ids   = market.get("clobTokenIds", "[]")
    raw_out   = market.get("outcomes", "[]")
    token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes  = json.loads(raw_out) if isinstance(raw_out, str) else raw_out

    up_token_id   = None
    down_token_id = None
    for i, outcome in enumerate(outcomes):
        ol = outcome.strip().lower()
        if ol == "up" and i < len(token_ids):
            up_token_id = token_ids[i]
        elif ol == "down" and i < len(token_ids):
            down_token_id = token_ids[i]

    if not up_token_id or not down_token_id:
        return None

    return {
        "coin":          coin,
        "slug":          slug,
        "up_token_id":   up_token_id,
        "down_token_id": down_token_id,
    }


def fetch_coin_market_both(coin: str, ts: int,
                           max_attempts: int = 3, delay: int = 2) -> Optional[dict]:
    """Fetch both-token market at xx:04:55 — market is mature, short retries."""
    slug = f"{coin}-updown-5m-{ts}"
    url  = f"{GAMMA_API}/events/slug/{slug}"
    for attempt in range(1, max_attempts + 1):
        try:
            event  = _curl_get(url)
            result = _extract_market_both(event, coin)
            if result:
                return result
            print(f"  [{coin.upper()}] Follower: both-token market not usable ({attempt}/{max_attempts})")
        except Exception as exc:
            print(f"  [{coin.upper()}] Follower fetch error ({attempt}/{max_attempts}): {exc}")
        if attempt < max_attempts:
            time.sleep(delay)
    return None


def fetch_all_markets_both(ts: int) -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(COINS)) as pool:
        futures = {pool.submit(fetch_coin_market_both, coin, ts): coin for coin in COINS}
        results = []
        for future in as_completed(futures):
            coin = futures[future]
            try:
                r = future.result()
                if r:
                    results.append(r)
            except Exception as exc:
                print(f"  [{coin.upper()}] Follower fetch thread crashed: {exc}")
    return results


# ---------------------------------------------------------------------------
# CLOB helpers
# ---------------------------------------------------------------------------

def get_live_price(token_id: str, max_attempts: int = 3) -> Optional[float]:
    """Fetch current BUY price from the CLOB order book, with retry."""
    url = f"{HOST}/price?token_id={token_id}&side=BUY"
    for attempt in range(1, max_attempts + 1):
        try:
            data = _curl_get(url)
            raw  = data.get("price")
            return float(raw) if raw is not None else None
        except Exception as exc:
            if attempt < max_attempts:
                time.sleep(2)
            else:
                raise


def build_client(max_attempts: int = 3, delay: int = 5) -> ClobClient:
    for attempt in range(1, max_attempts + 1):
        try:
            client = ClobClient(HOST, key=PK, chain_id=POLYGON,
                                signature_type=1, funder=FUNDER)
            client.set_api_creds(client.create_or_derive_api_creds())
            return client
        except Exception as exc:
            print(f"  Client init attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(delay)
    raise RuntimeError("CLOB client init failed after 3 attempts")


def post_orders_with_retry(client: ClobClient, batch: list[PostOrdersArgs],
                           labels: list[str], max_attempts: int = 3) -> Optional[dict]:
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\nSubmitting {len(batch)} order(s) [{', '.join(labels)}]"
                  f" — attempt {attempt}/{max_attempts} …")
            response = client.post_orders(batch)
            print(f"Batch response: {json.dumps(response, indent=2)}")
            return response
        except Exception as exc:
            print(f"Batch attempt {attempt} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(5)
    print(f"All {max_attempts} batch attempts failed.")
    return None


# ---------------------------------------------------------------------------
# Martingale strategy — sign (single direction, with price cap)
# ---------------------------------------------------------------------------

def sign_order(client: ClobClient, market: dict, bet: float,
               max_attempts: int = 5, delay: int = 5) -> Optional[tuple[str, PostOrdersArgs]]:
    coin     = market["coin"]
    token_id = market["token_id"]
    label    = coin.upper()

    for attempt in range(1, max_attempts + 1):
        try:
            live_price = get_live_price(token_id)
            if live_price is not None and live_price > MAX_PRICE:
                print(f"  [{label}] Live price {live_price:.3f} > {MAX_PRICE} — skipping")
                return None
            order_args   = MarketOrderArgs(token_id=token_id, amount=bet, side=BUY)
            signed_order = client.create_market_order(order_args)
            price_str    = f" @ ${live_price:.3f}" if live_price is not None else ""
            print(f"  [{label}] Martingale: signed BUY {DIRECTION} ${bet:.2f}{price_str}")
            return coin, PostOrdersArgs(order=signed_order, orderType=OrderType.FOK)
        except Exception as exc:
            print(f"  [{label}] Sign attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Follower strategy — pick direction + sign (no price cap)
# ---------------------------------------------------------------------------

def pick_follower_direction(coin: str,
                            up_token_id: str,
                            down_token_id: str) -> Optional[tuple[str, str]]:
    """
    Fetch live BUY prices for both tokens.
    Returns (direction, token_id) for the higher-priced (market-favourite) token.
    """
    up_price   = get_live_price(up_token_id)
    down_price = get_live_price(down_token_id)
    if up_price is None or down_price is None:
        print(f"  [{coin.upper()}] Follower: could not fetch both prices")
        return None
    if up_price >= down_price:
        print(f"  [{coin.upper()}] Follower → UP   (up={up_price:.3f}  down={down_price:.3f})")
        return "Up", up_token_id
    else:
        print(f"  [{coin.upper()}] Follower → DOWN (down={down_price:.3f}  up={up_price:.3f})")
        return "Down", down_token_id


def sign_follower_order(client: ClobClient, market_both: dict,
                        bet: float, max_attempts: int = 5,
                        delay: int = 3) -> Optional[tuple[str, str, PostOrdersArgs]]:
    """
    Pick direction from live prices then sign a FOK order.
    Returns (coin, direction_chosen, PostOrdersArgs).
    No MAX_PRICE cap — follower intentionally buys the higher-priced token.
    """
    coin = market_both["coin"]
    pick = pick_follower_direction(coin, market_both["up_token_id"], market_both["down_token_id"])
    if pick is None:
        return None
    direction, token_id = pick

    for attempt in range(1, max_attempts + 1):
        try:
            order_args   = MarketOrderArgs(token_id=token_id, amount=bet, side=BUY)
            signed_order = client.create_market_order(order_args)
            print(f"  [{coin.upper()}] Follower: signed BUY {direction} ${bet:.2f}")
            return coin, direction, PostOrdersArgs(order=signed_order, orderType=OrderType.FOK)
        except Exception as exc:
            print(f"  [{coin.upper()}] Follower sign attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Resolution — Martingale via Chainlink, Follower via Polymarket outcome
# ---------------------------------------------------------------------------

def resolve_martingale(app_state: dict, ts: int, coins: list[str]) -> None:
    """
    Fetch Chainlink closing price at xx:04:59, compare with price_to_beat.
    Only called when martingale placed bets.
    """
    print(f"\n[Martingale Resolution] Fetching Chainlink closing prices …")
    slug = f"{{coin}}-updown-5m-{ts}"
    for coin in coins:
        cs            = app_state["coins"].get(coin, {})
        price_to_beat = cs.get("price_to_beat")
        close_price   = get_chainlink_price(coin)

        if close_price is None:
            print(f"  [{coin.upper()}] No close price — skipping")
            continue
        cs["close_price"] = close_price

        if price_to_beat is None:
            print(f"  [{coin.upper()}] No price_to_beat — skipping")
            continue

        bet = cs.get("current_bet", BASE_BET)
        won = (close_price >= price_to_beat if DIRECTION.lower() == "up"
               else close_price < price_to_beat)
        print(f"  [{coin.upper()}] {'WIN' if won else 'LOSS'}"
              f"  open={price_to_beat:,.4f}  close={close_price:,.4f}  dir={DIRECTION}")
        st.apply_result(app_state, coin, BASE_BET, MAX_BET,
                        f"{coin}-updown-5m-{ts}", bet, won,
                        DIRECTION if won else ("Down" if DIRECTION == "Up" else "Up"))
    st.save(app_state)




# ---------------------------------------------------------------------------
# Martingale cycle — Phases 1-3, returns immediately after submission
# ---------------------------------------------------------------------------

def run_martingale_cycle(client: ClobClient, app_state: dict,
                         ts: int) -> list[str]:
    """
    Fetch markets → sign → submit for the martingale strategy.
    Returns list of coins that placed bets (for later resolution).
    Does NOT sleep.
    """
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"[Martingale] {now_str}  ts={ts}  direction={DIRECTION}")

    print(f"\n[Martingale P1] Fetching markets …")
    st.add_log(app_state, f"Martingale P1: ts={ts}")
    st.save(app_state)

    markets = fetch_all_markets(ts)
    print(f"[Martingale P1] Ready: {[m['slug'] for m in markets]}")

    if not markets:
        st.add_log(app_state, "Martingale: no markets — skipped")
        st.save(app_state)
        return []

    print(f"\n[Martingale P2] Signing {len(markets)} order(s) …")
    signed: list[tuple[str, PostOrdersArgs]] = []
    with ThreadPoolExecutor(max_workers=len(markets)) as pool:
        sign_futures = {
            pool.submit(sign_order, client, m,
                        app_state["coins"].get(m["coin"], {}).get("current_bet", BASE_BET)): m["coin"]
            for m in markets
        }
        for f, coin in sign_futures.items():
            try:
                r = f.result()
                if r:
                    signed.append(r)
            except Exception as exc:
                print(f"  [{coin.upper()}] Martingale sign thread crashed: {exc}")

    if not signed:
        st.add_log(app_state, "Martingale: no orders signed — skipped")
        st.save(app_state)
        return []

    labels    = [coin.upper() for coin, _ in signed]
    batch     = [args for _, args in signed]
    coins_bet = [coin for coin, _ in signed]

    print(f"\n[Martingale P3] Submitting {len(batch)} order(s): {', '.join(labels)}")
    st.add_log(app_state, f"Martingale P3: {', '.join(labels)}")
    st.save(app_state)

    response = post_orders_with_retry(client, batch, labels)
    for coin in coins_bet:
        app_state["coins"][coin]["close_price"] = None
        app_state["coins"][coin]["last_slug"]   = f"{coin}-updown-5m-{ts}"

    st.add_log(app_state, f"Martingale done — {', '.join(labels)}")
    st.save(app_state)
    return coins_bet


# ---------------------------------------------------------------------------
# Follower cycle — pick direction from live prices, sign, submit
# ---------------------------------------------------------------------------

def run_follower_cycle(client: ClobClient, app_state: dict,
                       ts: int) -> dict[str, str]:
    """
    Fetch both-token markets → pick direction from live prices → sign → submit.
    Returns {coin: direction_chosen} for resolution.
    Does NOT sleep.
    """
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"[Follower] {now_str}  ts={ts}  bet=${FOLLOWER_BET}")

    print(f"\n[Follower P1] Fetching both-token markets …")
    markets_both = fetch_all_markets_both(ts)

    if not markets_both:
        st.add_log(app_state, "Follower: no markets — skipped")
        st.save(app_state)
        return {}

    print(f"\n[Follower P2] Picking directions and signing …")
    signed: list[tuple[str, str, PostOrdersArgs]] = []
    with ThreadPoolExecutor(max_workers=len(markets_both)) as pool:
        sign_futures = {
            pool.submit(sign_follower_order, client, m, FOLLOWER_BET): m["coin"]
            for m in markets_both
        }
        for f, coin in sign_futures.items():
            try:
                r = f.result()
                if r:
                    signed.append(r)
            except Exception as exc:
                print(f"  [{coin.upper()}] Follower sign thread crashed: {exc}")

    if not signed:
        st.add_log(app_state, "Follower: no orders signed — skipped")
        st.save(app_state)
        return {}

    labels         = [coin.upper() for coin, _, _ in signed]
    batch          = [args for _, _, args in signed]
    follower_coins = {coin: direction for coin, direction, _ in signed}

    print(f"\n[Follower P3] Submitting {len(batch)} order(s): {', '.join(labels)}")
    st.add_log(app_state, f"Follower P3: {', '.join(labels)}")
    st.save(app_state)

    response = post_orders_with_retry(client, batch, labels)
    st.add_log(app_state, f"Follower done — {', '.join(labels)}")
    st.save(app_state)
    return follower_coins


# ---------------------------------------------------------------------------
# Interactive startup prompts
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str) -> str:
    """Print prompt with default hint, return stripped input or default."""
    try:
        val = input(f"{prompt} [{default}]: ").strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit("Aborted.")


def _ask_float(prompt: str, default: float, min_val: float = 1.0) -> float:
    """Ask for a positive float, re-prompt on bad input."""
    while True:
        raw = _ask(prompt, str(default))
        try:
            val = float(raw)
            if val >= min_val:
                return val
            print(f"  Must be >= {min_val}. Try again.")
        except ValueError:
            print("  Invalid number. Try again.")


def prompt_config() -> None:
    """
    Ask the user to choose strategy and bet sizes interactively.
    Updates module-level globals so all downstream functions see the values.
    """
    global STRATEGY, FOLLOWER_BET, BASE_BET, MAX_BET, DIRECTION
    global RUN_MARTINGALE, RUN_FOLLOWER

    print("\n┌─ Strategy ──────────────────────────────────────────────┐")
    print("│  1) Martingale — fixed direction, doubles bet on loss   │")
    print("│  2) Follower   — buys market-favourite at xx:04:55      │")
    print("│  3) Both       — run both strategies each window        │")
    print("└─────────────────────────────────────────────────────────┘")

    while True:
        choice = _ask("Choice", "1").strip()
        if choice in ("1", "2", "3"):
            break
        print("  Enter 1, 2 or 3.")

    STRATEGY       = {"1": "martingale", "2": "follower", "3": "both"}[choice]
    RUN_MARTINGALE = STRATEGY in ("martingale", "both")
    RUN_FOLLOWER   = STRATEGY in ("follower",   "both")

    if RUN_MARTINGALE:
        print("\n── Martingale settings ───────────────────────────────────")
        while True:
            d = _ask("  Direction (Up / Down)", DIRECTION).strip().capitalize()
            if d in ("Up", "Down"):
                DIRECTION = d
                break
            print("  Enter Up or Down.")
        BASE_BET = _ask_float("  Base bet (USDC)", BASE_BET)
        MAX_BET  = _ask_float("  Max bet  (USDC)", MAX_BET, min_val=BASE_BET)

    if RUN_FOLLOWER:
        print("\n── Follower settings ─────────────────────────────────────")
        FOLLOWER_BET = _ask_float("  Bet per round (USDC)", FOLLOWER_BET)

    print("\n─── Configuration ────────────────────────────────────────")
    print(f"  Strategy  : {STRATEGY.upper()}")
    print(f"  Coins     : {', '.join(sorted(c.upper() for c in COINS))}")
    if RUN_MARTINGALE:
        print(f"  Martingale: {DIRECTION}  base=${BASE_BET}  max=${MAX_BET}")
    if RUN_FOLLOWER:
        print(f"  Follower  : ${FOLLOWER_BET} / round  (market-favourite)")
    print("──────────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== Policar — Polymarket 5-min Bot ===")
    print(f"Coins  : {', '.join(sorted(c.upper() for c in COINS))}")
    print(f"Funder : {FUNDER}")

    if not PK:
        sys.exit("Error: PK not set in .env")
    if not FUNDER:
        sys.exit("Error: FUNDER not set in .env")

    prompt_config()

    print("Initializing CLOB client …")
    client = build_client()
    print("Client ready.\n")

    app_state = st.load()
    for coin in COINS:
        st.ensure_coin(app_state, coin, BASE_BET)
    st.save(app_state)

    while True:
        now         = datetime.now(timezone.utc)
        # Floor to 5-min window boundary
        window_open = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
        # Advance to next window if we're past the martingale trigger (xx:00:10)
        if now >= window_open + timedelta(seconds=10):
            window_open += timedelta(minutes=5)

        ts            = int(window_open.timestamp())
        martingale_at = window_open + timedelta(seconds=10)    # xx:00:10
        follower_at   = window_open + timedelta(seconds=295)   # xx:04:55
        resolution_at = window_open + timedelta(seconds=299)   # xx:04:59

        # ── T+0: sleep to window open, fetch Price to Beat ──────────────────
        wait_open = (window_open - datetime.now(timezone.utc)).total_seconds()
        if wait_open > 0:
            print(f"\n[Timing] Window open {window_open.strftime('%H:%M:%S UTC')} in {wait_open:.1f}s")
            time.sleep(wait_open)

        # Chainlink price-to-beat only needed for Martingale resolution
        if RUN_MARTINGALE:
            print(f"\n[Price to Beat] Fetching Chainlink at {window_open.strftime('%H:%M:%S UTC')} …")
            for coin in COINS:
                price = get_chainlink_price(coin)
                if price is not None:
                    app_state.setdefault("coins", {}).setdefault(coin, {})["price_to_beat"] = price

        app_state.setdefault("bot", {}).update({
            "status":       "running",
            "last_cycle":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "next_trigger": martingale_at.strftime("%H:%M:%S UTC"),
            "direction":    DIRECTION,
            "base_bet":     BASE_BET,
            "max_bet":      MAX_BET,
            "strategy":     STRATEGY,
            "follower_bet": FOLLOWER_BET,
        })
        for coin in COINS:
            st.ensure_coin(app_state, coin, BASE_BET)
        st.save(app_state)

        martingale_coins: list[str]     = []
        follower_coins:   dict[str, str] = {}

        # ── T+10: Martingale ────────────────────────────────────────────────
        if RUN_MARTINGALE:
            wait_m = (martingale_at - datetime.now(timezone.utc)).total_seconds()
            if wait_m > 0:
                print(f"[Timing] Martingale trigger in {wait_m:.1f}s …")
                time.sleep(wait_m)
            martingale_coins = run_martingale_cycle(client, app_state, ts)

        # ── T+295: Follower ─────────────────────────────────────────────────
        if RUN_FOLLOWER:
            wait_f = (follower_at - datetime.now(timezone.utc)).total_seconds()
            if wait_f > 0:
                print(f"\n[Timing] Follower trigger {follower_at.strftime('%H:%M:%S UTC')}"
                      f" in {wait_f:.1f}s …")
                time.sleep(wait_f)
            follower_coins = run_follower_cycle(client, app_state, ts)

        # ── T+299: Martingale resolution via Chainlink ──────────────────────
        if martingale_coins:
            wait_r = (resolution_at - datetime.now(timezone.utc)).total_seconds()
            if wait_r > 0:
                print(f"\n[Timing] Martingale resolution in {wait_r:.1f}s …")
                time.sleep(wait_r)
            resolve_martingale(app_state, ts, martingale_coins)

        # Follower: no resolution check — Polymarket pays out automatically.

        # ── Advance past window close if nothing was placed ──────────────────
        if not martingale_coins and not follower_coins:
            wait_end = (resolution_at - datetime.now(timezone.utc)).total_seconds()
            if wait_end > 0:
                time.sleep(wait_end)


if __name__ == "__main__":
    main()
