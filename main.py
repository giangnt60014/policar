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

PK             = os.environ.get("PK")
FUNDER         = os.environ.get("FUNDER")
BASE_BET       = float(os.environ.get("ORDER_AMOUNT", "2"))
MAX_BET        = float(os.environ.get("MAX_BET", "64"))
COINS          = {c.strip().lower() for c in os.environ.get("COINS", "btc").split(",")}
DIRECTION      = os.environ.get("DIRECTION", "Up")  # "Up" or "Down"

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
            decimals     = contract.functions.decimals().call()
            _, answer, _, updated_at, _ = contract.functions.latestRoundData().call()
            price = answer / (10 ** decimals)
            age   = int(time.time()) - updated_at
            print(f"  [{coin.upper()}] Chainlink price: ${price:,.4f}  (updated {age}s ago)")
            return price
        except Exception as exc:
            print(f"  [{coin.upper()}] Chainlink error ({attempt}/{max_attempts}): {exc}")
            if attempt < max_attempts:
                time.sleep(2)
    return None



# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def current_window_timestamp() -> int:
    now = datetime.now(timezone.utc)
    floored = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    return int(floored.timestamp())


def next_trigger_time() -> datetime:
    now = datetime.now(timezone.utc)
    floored = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    trigger = floored + timedelta(seconds=10)
    if trigger <= now:
        trigger += timedelta(minutes=5)
    return trigger


# ---------------------------------------------------------------------------
# Curl helper
# ---------------------------------------------------------------------------

# Per-thread persistent Session — reuses TLS connection, avoids Cloudflare resets
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
            # libcurl error 35 = SSL connect error — recreate session and retry once
            if "35" in str(exc) and attempt == 0:
                _reset_session()
                continue
            raise


# ---------------------------------------------------------------------------
# Chainlink resolution — compare price_to_beat vs close_price
# ---------------------------------------------------------------------------

def resolve_by_chainlink(app_state: dict, ts: int, coins_bet: list[str]) -> None:
    """
    Fetch closing price for each coin from Chainlink and apply Martingale.
    Called at xx:04:59 after orders were placed this cycle.
    """
    print(f"\n[Resolution] Fetching closing prices from Chainlink …")
    for coin in coins_bet:
        cs           = app_state["coins"].get(coin, {})
        price_to_beat = cs.get("price_to_beat")
        bet           = cs.get("current_bet", BASE_BET)
        slug          = f"{coin}-updown-5m-{ts}"

        close_price = get_chainlink_price(coin)
        if close_price is None:
            print(f"  [{coin.upper()}] Could not fetch closing price — skipping Martingale update")
            continue

        cs["close_price"] = close_price

        if price_to_beat is None:
            print(f"  [{coin.upper()}] No price_to_beat recorded — skipping Martingale update")
            continue

        won = close_price >= price_to_beat if DIRECTION.lower() == "up" else close_price < price_to_beat
        result_str = "WIN" if won else "LOSS"
        print(f"  [{coin.upper()}] {result_str}  open={price_to_beat:,.4f}  close={close_price:,.4f}  direction={DIRECTION}")
        st.apply_result(app_state, coin, BASE_BET, MAX_BET, slug, bet, won,
                        DIRECTION if won else ("Down" if DIRECTION == "Up" else "Up"))

    st.save(app_state)


# ---------------------------------------------------------------------------
# Phase 1 — Market discovery (per-coin slug, parallel)
# ---------------------------------------------------------------------------

MAX_PRICE = 0.60  # skip if live BUY price > this


def get_live_price(token_id: str) -> Optional[float]:
    """Fetch current BUY price from the CLOB order book (live, not Gamma cache)."""
    url = f"{HOST}/price?token_id={token_id}&side=BUY"
    data = _curl_get(url)
    raw = data.get("price")
    return float(raw) if raw is not None else None


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

    token_id = None
    for i, outcome in enumerate(outcomes):
        if outcome.strip().lower() == DIRECTION.lower() and i < len(token_ids):
            token_id = token_ids[i]
            break

    if not token_id:
        return None

    return {
        "coin":     coin,
        "slug":     slug,
        "question": market.get("question") or slug,
        "token_id": token_id,
    }


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
            print(f"  [{coin.upper()}] Retrying in {delay}s …")
            time.sleep(delay)

    print(f"  [{coin.upper()}] No open market found after {max_attempts} attempts. Skipping.")
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
# CLOB client
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Phase 2 — Sign orders (local, parallel)
# ---------------------------------------------------------------------------

def sign_order(client: ClobClient, market: dict, bet: float,
               max_attempts: int = 5, delay: int = 5) -> Optional[tuple[str, PostOrdersArgs]]:
    coin     = market["coin"]
    token_id = market["token_id"]
    label    = coin.upper()

    for attempt in range(1, max_attempts + 1):
        try:
            # Check live CLOB price before signing
            live_price = get_live_price(token_id)
            if live_price is not None and live_price > MAX_PRICE:
                print(f"  [{label}] Live price {live_price:.3f} > {MAX_PRICE} — skipping")
                return None

            order_args   = MarketOrderArgs(token_id=token_id, amount=bet, side=BUY)
            signed_order = client.create_market_order(order_args)
            price_str    = f" @ ${live_price:.3f}" if live_price is not None else ""
            print(f"  [{label}] Signed BUY {DIRECTION} ${bet:.2f} USDC{price_str} (token …{token_id[-8:]})")
            return coin, PostOrdersArgs(order=signed_order, orderType=OrderType.FOK)
        except Exception as exc:
            print(f"  [{label}] Sign attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(delay)
    return None


# ---------------------------------------------------------------------------
# Phase 3 — Batch submit
# ---------------------------------------------------------------------------

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
# Main cycle
# ---------------------------------------------------------------------------

def run_cycle(client: ClobClient, app_state: dict) -> None:
    ts      = current_window_timestamp()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"\n{'='*60}")
    print(f"Cycle    : {now_str}")
    print(f"Window ts: {ts}  |  Direction: {DIRECTION}")
    print(f"Coins    : {', '.join(sorted(c.upper() for c in COINS))}")

    # Update bot meta in state
    trigger = next_trigger_time()
    app_state.setdefault("bot", {}).update({
        "status":       "running",
        "last_cycle":   now_str,
        "next_trigger": trigger.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "direction":    DIRECTION,
        "base_bet":     BASE_BET,
        "max_bet":      MAX_BET,
    })
    for coin in COINS:
        st.ensure_coin(app_state, coin, BASE_BET)

    # --- Phase 1: Fetch open markets ---
    print(f"\n[Phase 1] Fetching markets …")
    st.add_log(app_state, f"Phase 1: ts={ts}")
    st.save(app_state)

    markets = fetch_all_markets(ts)
    print(f"[Phase 1] Ready: {[m['slug'] for m in markets]}")

    if not markets:
        print("[Phase 1] No markets found. Skipping cycle.")
        st.add_log(app_state, "No markets found — skipped")
        st.save(app_state)
        return

    # --- Phase 2: Sign orders in parallel ---
    print(f"\n[Phase 2] Signing {len(markets)} order(s) in parallel …")
    signed: list[tuple[str, PostOrdersArgs]] = []

    with ThreadPoolExecutor(max_workers=len(markets)) as pool:
        futures = [
            pool.submit(
                sign_order, client, m,
                app_state["coins"].get(m["coin"], {}).get("current_bet", BASE_BET)
            )
            for m in markets
        ]
        for future in futures:
            result = future.result()
            if result:
                signed.append(result)

    if not signed:
        print("No orders signed. Skipping submission.")
        st.add_log(app_state, "No orders signed — skipped")
        st.save(app_state)
        return

    # --- Phase 3: Batch submit ---
    labels = [coin.upper() for coin, _ in signed]
    batch  = [args for _, args in signed]
    print(f"\n[Phase 3] Batch submitting {len(batch)} order(s): {', '.join(labels)}")
    st.add_log(app_state, f"Phase 3: submitting {', '.join(labels)}")
    st.save(app_state)

    response = post_orders_with_retry(client, batch, labels)
    coins_bet = [coin for coin, _ in signed]

    for coin in coins_bet:
        app_state["coins"][coin]["close_price"] = None
        app_state["coins"][coin]["last_slug"]   = f"{coin}-updown-5m-{ts}"

    st.add_log(app_state, f"Batch done — {', '.join(labels)} | response: {response}")
    st.save(app_state)

    # --- Phase 5: Sleep until xx:04:59, then fetch closing price ---
    window_close = datetime.fromtimestamp(ts + 300, tz=timezone.utc)
    fetch_at     = window_close - timedelta(seconds=1)   # xx:04:59
    wait_secs    = (fetch_at - datetime.now(timezone.utc)).total_seconds()

    if wait_secs > 0:
        print(f"\n[Phase 5] Waiting {wait_secs:.1f}s until {fetch_at.strftime('%H:%M:%S UTC')} for closing price …")
        time.sleep(wait_secs)

    print(f"[Phase 5] Fetching closing prices …")
    resolve_by_chainlink(app_state, ts, coins_bet)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== Policar — Polymarket 5-min Martingale Bot ===")
    print(f"Coins     : {', '.join(sorted(c.upper() for c in COINS))}")
    print(f"Direction : {DIRECTION}")
    print(f"Base bet  : ${BASE_BET} USDC  |  Max bet: ${MAX_BET} USDC")
    print(f"Funder    : {FUNDER}")
    print()

    if not PK:
        sys.exit("Error: PK not set in .env")
    if not FUNDER:
        sys.exit("Error: FUNDER not set in .env")

    print("Initializing CLOB client …")
    client = build_client()
    print("Client ready.\n")

    app_state = st.load()

    while True:
        trigger     = next_trigger_time()           # xx:00:10
        window_open = trigger - timedelta(seconds=10)  # xx:00:00

        # Sleep until window open
        wait_open = (window_open - datetime.now(timezone.utc)).total_seconds()
        if wait_open > 0:
            print(f"Window open  : {window_open.strftime('%H:%M:%S UTC')}  (in {wait_open:.1f}s)")
            time.sleep(wait_open)

        # Fetch price-to-beat at exactly xx:00:00 / xx:05:00
        print(f"\n[Price to Beat] Fetching Chainlink at window open {window_open.strftime('%H:%M:%S UTC')} …")
        for coin in COINS:
            price = get_chainlink_price(coin)
            if price is not None:
                app_state.setdefault("coins", {}).setdefault(coin, {})["price_to_beat"] = price
        st.save(app_state)

        # Sleep remaining 10s until trigger
        wait_trigger = (trigger - datetime.now(timezone.utc)).total_seconds()
        if wait_trigger > 0:
            print(f"Next trigger : {trigger.strftime('%H:%M:%S UTC')}  (in {wait_trigger:.1f}s)")
            time.sleep(wait_trigger)

        run_cycle(client, app_state)


if __name__ == "__main__":
    main()
