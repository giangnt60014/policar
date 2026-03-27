import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

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

CURL_HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "-H", "Accept: application/json",
    "-H", "Accept-Language: en-US,en;q=0.9",
    "-H", "Referer: https://polymarket.com/",
]


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

def _curl_get(url: str) -> dict:
    result = subprocess.run(
        ["curl", "-s", "--compressed", "--max-time", "10"] + CURL_HEADERS + [url],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exited {result.returncode}: {result.stderr.strip()}")
    raw = result.stdout.strip()
    if not raw:
        raise ValueError("Empty response from API")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Phase 0 — Check resolution of previous window (Martingale input)
# ---------------------------------------------------------------------------

def check_resolution(coin: str, prev_ts: int,
                     max_attempts: int = 10, delay: int = 5) -> Optional[str]:
    """
    Returns the winning outcome ("Up" or "Down") for the previous 5-min window,
    or None if it couldn't be determined after max_attempts.
    Retries because the market may not have settled yet.
    """
    slug = f"{coin}-updown-5m-{prev_ts}"
    url  = f"{GAMMA_API}/events/slug/{slug}"

    for attempt in range(1, max_attempts + 1):
        try:
            event      = _curl_get(url)
            market     = (event.get("markets") or [{}])[0]
            raw_prices = market.get("outcomePrices", "[]")
            raw_out    = market.get("outcomes", "[]")
            prices     = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
            outcomes   = json.loads(raw_out)    if isinstance(raw_out,    str) else raw_out

            for i, price_str in enumerate(prices):
                if float(price_str) >= 0.99 and i < len(outcomes):
                    print(f"  [{coin.upper()}] Prev window resolved → {outcomes[i]}")
                    return outcomes[i]

            print(f"  [{coin.upper()}] Not resolved yet ({attempt}/{max_attempts})")
        except Exception as exc:
            print(f"  [{coin.upper()}] Resolution check error: {exc} ({attempt}/{max_attempts})")

        if attempt < max_attempts:
            time.sleep(delay)

    print(f"  [{coin.upper()}] Could not determine resolution after {max_attempts} attempts")
    return None


def resolve_all_previous(app_state: dict, prev_ts: int) -> None:
    """Parallel resolution check for all coins; update Martingale state."""
    def _resolve(coin: str) -> None:
        prev_slug  = f"{coin}-updown-5m-{prev_ts}"
        coin_state = app_state.get("coins", {}).get(coin, {})

        # Only check if we actually bet on this coin last cycle
        if coin_state.get("last_slug") != prev_slug:
            return

        bet    = coin_state.get("current_bet", BASE_BET)
        winner = check_resolution(coin, prev_ts)
        if winner is None:
            return

        st.apply_result(app_state, coin, BASE_BET, MAX_BET,
                        prev_slug, bet, winner == DIRECTION, winner)

    with ThreadPoolExecutor(max_workers=len(COINS)) as pool:
        futures = [pool.submit(_resolve, coin) for coin in COINS]
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                print(f"  Resolution thread error: {exc}")

    st.save(app_state)


# ---------------------------------------------------------------------------
# Phase 1 — Market discovery (per-coin slug, parallel)
# ---------------------------------------------------------------------------

def _extract_market(event: dict, coin: str) -> Optional[dict]:
    now_utc  = datetime.now(timezone.utc)
    end_date = event.get("endDate", "")
    if end_date:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end_dt <= now_utc:
            return None

    slug     = event.get("slug", "")
    market   = (event.get("markets") or [{}])[0]
    raw_ids  = market.get("clobTokenIds", "[]")
    raw_out  = market.get("outcomes", "[]")
    token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes  = json.loads(raw_out)  if isinstance(raw_out,  str) else raw_out

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
                      max_attempts: int = 3, delay: int = 5) -> Optional[dict]:
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

    # --- Phase 0: Resolve previous window & update Martingale state ---
    has_prev = any(
        app_state["coins"].get(coin, {}).get("last_slug") ==
        f"{coin}-updown-5m-{prev_ts}"
        for coin in COINS
    )
    if has_prev:
        print(f"\n[Phase 0] Resolving previous window (ts={prev_ts}) …")
        st.add_log(app_state, f"Phase 0: resolving window ts={prev_ts}")
        resolve_all_previous(app_state, prev_ts)

    # --- Phase 1: Fetch open market per coin (parallel) ---
    print(f"\n[Phase 1] Fetching open markets (parallel, ts={ts}) …")
    st.add_log(app_state, f"Phase 1: fetching markets ts={ts}")
    st.save(app_state)

    markets = fetch_all_markets(ts)
    if not markets:
        print("No markets found. Skipping cycle.")
        st.add_log(app_state, "No markets found — skipped")
        st.save(app_state)
        return

    print(f"  Ready: {[m['slug'] for m in markets]}")

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
