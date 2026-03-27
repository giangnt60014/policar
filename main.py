import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
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
    data = _curl_get(f"{GAMMA_API}/markets/slug/{slug}")
    if not data or not data.get("clobTokenIds"):
        raise ValueError("Market not found or has no tokens")
    return data


def get_market_with_retry(slug: str, max_attempts: int = 3, delay: int = 15) -> dict:
    for attempt in range(1, max_attempts + 1):
        print(f"    [{attempt}/{max_attempts}] GET /markets/slug/{slug}")
        try:
            return get_market(slug)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"    Not ready: {exc}")
            if attempt < max_attempts:
                print(f"    Retrying in {delay}s …")
                time.sleep(delay)
    raise ValueError(f"Market '{slug}' unavailable after {max_attempts} attempts")


# ---------------------------------------------------------------------------
# Order placement
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


def trade_coin(client: ClobClient, coin: str, ts: int) -> None:
    slug = f"{coin}-updown-5m-{ts}"
    print(f"\n  [{coin.upper()}] slug: {slug}")

    try:
        market = get_market_with_retry(slug)
    except ValueError as exc:
        print(f"  [{coin.upper()}] Skipping — {exc}")
        return

    question  = market.get("question") or slug
    raw_ids   = market.get("clobTokenIds", "[]")
    raw_out   = market.get("outcomes", "[]")
    token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes  = json.loads(raw_out)  if isinstance(raw_out,  str) else raw_out

    # Match configured direction to the correct token_id
    token_id = None
    for i, outcome in enumerate(outcomes):
        if outcome.strip().lower() == DIRECTION.lower() and i < len(token_ids):
            token_id = token_ids[i]
            break

    if not token_id:
        print(f"  [{coin.upper()}] No token for direction '{DIRECTION}'. Outcomes: {outcomes}")
        return

    print(f"  [{coin.upper()}] {question}")
    print(f"  [{coin.upper()}] BUY {DIRECTION} — ${ORDER_AMOUNT} USDC  (token …{token_id[-8:]})")

    order_args   = MarketOrderArgs(token_id=token_id, amount=ORDER_AMOUNT, side=BUY)
    market_order = client.create_market_order(order_args)
    response     = client.post_order(market_order, OrderType.FOK)
    print(f"  [{coin.upper()}] Response: {response}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle() -> None:
    ts = current_window_timestamp()
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

    for coin in COINS:
        try:
            trade_coin(client, coin, ts)
        except Exception as exc:
            print(f"  [{coin.upper()}] Unexpected error: {exc}")


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
