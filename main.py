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

load_dotenv()

HOST      = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

PK           = os.environ.get("PK")
FUNDER       = os.environ.get("FUNDER")
ORDER_AMOUNT = float(os.environ.get("ORDER_AMOUNT", "2"))
COINS        = [c.strip().lower() for c in os.environ.get("COINS", "btc").split(",")]
DIRECTION    = os.environ.get("DIRECTION", "Up")  # "Up" or "Down"

CURL_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

def current_window_timestamp() -> int:
    """Floor current UTC time to the nearest 5-minute boundary → unix timestamp."""
    now = datetime.now(timezone.utc)
    floored = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    return int(floored.timestamp())


def next_trigger_time() -> datetime:
    """Return the next xx:x0:10 or xx:x5:10 UTC moment."""
    now = datetime.now(timezone.utc)
    floored = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    trigger = floored + timedelta(seconds=10)
    if trigger <= now:
        trigger += timedelta(minutes=5)
    return trigger


# ---------------------------------------------------------------------------
# Market lookup via subprocess curl (bypasses Cloudflare TLS fingerprinting)
# ---------------------------------------------------------------------------

def _curl_get(url: str) -> dict:
    result = subprocess.run(
        ["curl", "-s", "--max-time", "10", "-A", CURL_UA, url],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exited {result.returncode}: {result.stderr.strip()}")
    raw = result.stdout.strip()
    if not raw:
        raise ValueError("Empty response from API")
    return json.loads(raw)


def get_market(slug: str) -> dict:
    data = _curl_get(f"{GAMMA_API}/events/slug/{slug}")
    markets = data.get("markets") if isinstance(data, dict) else None
    if not markets:
        raise ValueError("Event not found or has no markets")
    market = markets[0]
    if not market.get("clobTokenIds"):
        raise ValueError("Market has no token IDs")
    return market


def get_market_with_retry(slug: str, max_attempts: int = 3, delay: int = 15) -> dict:
    for attempt in range(1, max_attempts + 1):
        print(f"  [{slug}] Attempt {attempt}/{max_attempts} — GET /events/slug/{slug}")
        try:
            return get_market(slug)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"  [{slug}] Not ready: {exc}")
            if attempt < max_attempts:
                print(f"  [{slug}] Retrying in {delay}s …")
                time.sleep(delay)
    raise ValueError(f"Market '{slug}' unavailable after {max_attempts} attempts")


# ---------------------------------------------------------------------------
# Phase 1 — parallel: fetch market + sign order (no network for signing)
# ---------------------------------------------------------------------------

def fetch_and_sign(
    client: ClobClient,
    coin: str,
    ts: int,
) -> Optional[tuple[str, PostOrdersArgs]]:
    """
    Fetch the market for one coin and sign a market order locally.
    Returns (coin, PostOrdersArgs) on success, or None if the coin should be skipped.
    Does NOT submit anything to the exchange.
    """
    slug  = f"{coin}-updown-5m-{ts}"
    label = coin.upper()

    try:
        market = get_market_with_retry(slug)
    except ValueError as exc:
        print(f"  [{label}] Skipping — {exc}")
        return None

    question  = market.get("question") or slug
    raw_ids   = market.get("clobTokenIds", "[]")
    raw_out   = market.get("outcomes", "[]")
    token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes  = json.loads(raw_out)  if isinstance(raw_out,  str) else raw_out

    token_id = None
    for i, outcome in enumerate(outcomes):
        if outcome.strip().lower() == DIRECTION.lower() and i < len(token_ids):
            token_id = token_ids[i]
            break

    if not token_id:
        print(f"  [{label}] No token for direction '{DIRECTION}'. Outcomes: {outcomes}")
        return None

    print(f"  [{label}] {question}")
    print(f"  [{label}] Signing BUY {DIRECTION} — ${ORDER_AMOUNT} USDC  (token …{token_id[-8:]})")

    order_args   = MarketOrderArgs(token_id=token_id, amount=ORDER_AMOUNT, side=BUY)
    signed_order = client.create_market_order(order_args)

    return coin, PostOrdersArgs(order=signed_order, orderType=OrderType.FOK)


# ---------------------------------------------------------------------------
# Phase 2 — single call: submit all signed orders in one request (with retry)
# ---------------------------------------------------------------------------

def post_orders_with_retry(
    client: ClobClient,
    batch: list[PostOrdersArgs],
    labels: list[str],
    max_attempts: int = 3,
) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\nSubmitting {len(batch)} order(s) in one call (attempt {attempt}/{max_attempts}) …")
            response = client.post_orders(batch)
            print(f"Batch response: {response}")
            return
        except Exception as exc:
            print(f"Batch attempt {attempt} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(2)
    print(f"All {max_attempts} batch attempts failed. Giving up.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def build_client() -> ClobClient:
    client = ClobClient(
        HOST,
        key=PK,
        chain_id=POLYGON,
        signature_type=1,   # Email / Magic wallet delegated signing
        funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def run_cycle() -> None:
    ts      = current_window_timestamp()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"Cycle  : {now_str}")
    print(f"Window : {ts}  |  Direction: {DIRECTION}  |  Amount: ${ORDER_AMOUNT} USDC")
    print(f"Coins  : {', '.join(c.upper() for c in COINS)}")

    print("\nInitializing CLOB client …")
    try:
        client = build_client()
    except Exception as exc:
        print(f"Client init failed: {exc}")
        return

    # --- Phase 1: fetch all markets + sign all orders in parallel ---
    print(f"\nFetching markets & signing orders in parallel …")
    signed: list[tuple[str, PostOrdersArgs]] = []

    with ThreadPoolExecutor(max_workers=len(COINS)) as pool:
        futures = {
            pool.submit(fetch_and_sign, client, coin, ts): coin
            for coin in COINS
        }
        for future in as_completed(futures):
            coin = futures[future]
            exc  = future.exception()
            if exc:
                print(f"  [{coin.upper()}] Unexpected error: {exc}")
                continue
            result = future.result()
            if result:
                signed.append(result)

    if not signed:
        print("No orders to submit.")
        return

    # --- Phase 2: submit all signed orders in ONE network call ---
    labels = [coin.upper() for coin, _ in signed]
    batch  = [args for _, args in signed]
    print(f"\nReady to submit: {', '.join(labels)}")
    post_orders_with_retry(client, batch, labels)


def main() -> None:
    print("=== Policar — Polymarket 5-min Bot ===")
    print(f"Coins     : {', '.join(c.upper() for c in COINS)}")
    print(f"Direction : {DIRECTION}")
    print(f"Amount    : ${ORDER_AMOUNT} USDC per coin")
    print(f"Funder    : {FUNDER}")
    print()

    if not PK:
        sys.exit("Error: PK not set in .env")
    if not FUNDER:
        sys.exit("Error: FUNDER not set in .env")

    while True:
        trigger = next_trigger_time()
        wait    = (trigger - datetime.now(timezone.utc)).total_seconds()
        print(f"Next trigger : {trigger.strftime('%H:%M:%S UTC')}  (in {wait:.1f}s)")
        time.sleep(max(0, wait))
        run_cycle()


if __name__ == "__main__":
    main()
