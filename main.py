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

import state as st

load_dotenv()

HOST      = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

PK             = os.environ.get("PK")
FUNDER         = os.environ.get("FUNDER")
BASE_BET       = float(os.environ.get("ORDER_AMOUNT", "2"))
MAX_BET        = float(os.environ.get("MAX_BET", "64"))
COINS          = {c.strip().lower() for c in os.environ.get("COINS", "btc").split(",")}
DIRECTION      = os.environ.get("DIRECTION", "Up")  # "Up" or "Down"



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
# Phase 0 — Check resolution of previous window (Martingale input)
# ---------------------------------------------------------------------------

RESOLVE_TIMEOUT = 120  # seconds — Polymarket resolution can take 1-2 min after window close


def check_resolution(coin: str, prev_ts: int, delay: int = 5) -> Optional[str]:
    """
    Polls until the previous window resolves (outcomePrices hits 1.0) or
    RESOLVE_TIMEOUT seconds elapse.  Returns winning outcome or None.
    """
    slug     = f"{coin}-updown-5m-{prev_ts}"
    url      = f"{GAMMA_API}/events/slug/{slug}"
    deadline = time.monotonic() + RESOLVE_TIMEOUT
    attempt  = 0

    while time.monotonic() < deadline:
        attempt += 1
        remaining = int(deadline - time.monotonic())
        try:
            event      = _curl_get(url)
            market     = (event.get("markets") or [{}])[0]
            raw_prices = market.get("outcomePrices", "[]")
            raw_out    = market.get("outcomes", "[]")
            prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            outcomes   = json.loads(raw_out)    if isinstance(raw_out,    str) else raw_out

            for i, price_str in enumerate(prices):
                if float(price_str) >= 0.99 and i < len(outcomes):
                    print(f"  [{coin.upper()}] Resolved → {outcomes[i]} (attempt {attempt})")
                    return outcomes[i]

            print(f"  [{coin.upper()}] Not resolved yet, {remaining}s left …")
        except Exception as exc:
            print(f"  [{coin.upper()}] Resolution error: {exc}, {remaining}s left …")

        if time.monotonic() < deadline:
            time.sleep(delay)

    print(f"  [{coin.upper()}] Timed out after {RESOLVE_TIMEOUT}s — skipping Martingale update")
    return None


def resolve_all_previous(app_state: dict, prev_ts: int) -> None:
    """
    Blocking: parallel resolution check for all coins that bet last cycle.
    Waits up to RESOLVE_TIMEOUT seconds per coin (coins run concurrently).
    Updates Martingale state before returning — callers must not proceed
    to Phase 1 until this returns.
    """
    coins_to_check = [
        coin for coin in COINS
        if app_state.get("coins", {}).get(coin, {}).get("last_slug")
        == f"{coin}-updown-5m-{prev_ts}"
    ]
    if not coins_to_check:
        return

    def _resolve(coin: str) -> None:
        bet    = app_state["coins"][coin].get("current_bet", BASE_BET)
        winner = check_resolution(coin, prev_ts)
        if winner is None:
            return
        st.apply_result(app_state, coin, BASE_BET, MAX_BET,
                        f"{coin}-updown-5m-{prev_ts}", bet,
                        winner == DIRECTION, winner)

    with ThreadPoolExecutor(max_workers=len(coins_to_check)) as pool:
        futures = [pool.submit(_resolve, coin) for coin in coins_to_check]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                print(f"  Resolution thread error: {exc}")

    st.save(app_state)


# ---------------------------------------------------------------------------
# Phase 1 — Market discovery (per-coin slug, parallel)
# ---------------------------------------------------------------------------

MAX_PRICE = 0.60  # skip if our direction's token costs more than this


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
    raw_prices = market.get("outcomePrices", "[]")
    token_ids  = json.loads(raw_ids)    if isinstance(raw_ids,    str) else raw_ids
    outcomes   = json.loads(raw_out)    if isinstance(raw_out,    str) else raw_out
    prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices

    token_id = None
    price    = None
    for i, outcome in enumerate(outcomes):
        if outcome.strip().lower() == DIRECTION.lower() and i < len(token_ids):
            token_id = token_ids[i]
            price    = float(prices[i]) if i < len(prices) else None
            break

    if not token_id:
        return None

    if price is not None and price > MAX_PRICE:
        print(f"  [{coin.upper()}] Price {price:.3f} > {MAX_PRICE} — skipping")
        return None

    return {
        "coin":     coin,
        "slug":     slug,
        "question": market.get("question") or slug,
        "token_id": token_id,
        "price":    price,
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
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
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
            r = future.result()
            if r:
                results.append(r)
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
               max_attempts: int = 3, delay: int = 5) -> Optional[tuple[str, PostOrdersArgs]]:
    coin     = market["coin"]
    token_id = market["token_id"]
    label    = coin.upper()

    for attempt in range(1, max_attempts + 1):
        try:
            order_args   = MarketOrderArgs(token_id=token_id, amount=bet, side=BUY)
            signed_order = client.create_market_order(order_args)
            print(f"  [{label}] Signed BUY {DIRECTION} ${bet:.2f} USDC (token …{token_id[-8:]})")
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
    prev_ts = ts - 300
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

    # --- Phase 0 + Phase 1 run concurrently ---
    # Phase 0: resolve previous window (need result before signing)
    # Phase 1: fetch open markets (pure network, independent of Phase 0)
    # Phase 2+3 wait until Phase 0 is done to get correct bet sizes.

    has_prev = any(
        app_state["coins"].get(coin, {}).get("last_slug") ==
        f"{coin}-updown-5m-{prev_ts}"
        for coin in COINS
    )

    print(f"\n[Phase 0+1] Running resolution check and market fetch in parallel …")
    st.add_log(app_state, f"Phase 0+1: ts={ts} prev_ts={prev_ts}")
    st.save(app_state)

    with ThreadPoolExecutor(max_workers=2) as pool:
        phase0_future = pool.submit(resolve_all_previous, app_state, prev_ts) \
                        if has_prev else None
        phase1_future = pool.submit(fetch_all_markets, ts)

        markets = phase1_future.result()           # wait for Phase 1
        if phase0_future is not None:
            phase0_future.result()                 # wait for Phase 0 (blocks Phase 2+3)
            print("[Phase 0] Done.")

    print(f"[Phase 1] Ready: {[m['slug'] for m in markets]}")

    if not markets:
        print("[Phase 1] No markets found. Skipping cycle.")
        st.add_log(app_state, "No markets found — skipped")
        st.save(app_state)
        return

    # --- Phase 2: Sign orders in parallel (local, no network) ---
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

    # Record last_slug so Phase 0 knows to check it next cycle
    for coin, _ in signed:
        app_state["coins"][coin]["last_slug"] = f"{coin}-updown-5m-{ts}"

    st.add_log(app_state, f"Batch done — {', '.join(labels)} | response: {response}")
    st.save(app_state)


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
        trigger = next_trigger_time()
        wait    = (trigger - datetime.now(timezone.utc)).total_seconds()
        print(f"Next trigger : {trigger.strftime('%H:%M:%S UTC')}  (in {wait:.1f}s)")
        time.sleep(max(0, wait))
        run_cycle(client, app_state)


if __name__ == "__main__":
    main()
