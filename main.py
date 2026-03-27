import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
COINS        = {c.strip().lower() for c in os.environ.get("COINS", "btc").split(",")}
DIRECTION    = os.environ.get("DIRECTION", "Up")  # "Up" or "Down"

CURL_HEADERS = [
    "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "-H", "Accept: application/json",
    "-H", "Accept-Language: en-US,en;q=0.9",
    "-H", "Referer: https://polymarket.com/",
]


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
# Market discovery — ONE API call for all coins
# ---------------------------------------------------------------------------

def _curl_get(url: str) -> list | dict:
    """Fetch a URL via subprocess curl with gzip support (bypasses Cloudflare TLS fingerprinting)."""
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


def fetch_current_markets(ts: int) -> list[dict]:
    """
    Single API call: GET all active 5M up-or-down events tagged with '5M' and 'up-or-down'.
    Returns only events matching the current window timestamp AND configured COINS.

    Each returned dict: { coin, slug, question, token_id }
    """
    url = (
        f"{GAMMA_API}/events"
        f"?tag_slug=5M&tag_slug=up-or-down"
        f"&active=true&closed=false&limit=50"
    )
    events = _curl_get(url)
    if not isinstance(events, list):
        raise ValueError(f"Unexpected API response shape: {type(events)}")

    ts_suffix = f"-{ts}"
    matched = []

    for event in events:
        slug        = event.get("slug", "")
        series_slug = event.get("seriesSlug", "")

        # Must belong to the current 5-minute window
        if not slug.endswith(ts_suffix):
            continue

        # Derive coin name: "btc-up-or-down-5m" → "btc"
        coin = series_slug.replace("-up-or-down-5m", "").lower()
        if coin not in COINS:
            continue

        market    = (event.get("markets") or [{}])[0]
        raw_ids   = market.get("clobTokenIds", "[]")
        raw_out   = market.get("outcomes", "[]")
        token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
        outcomes  = json.loads(raw_out)  if isinstance(raw_out,  str) else raw_out

        # Match configured direction → token_id
        token_id = None
        for i, outcome in enumerate(outcomes):
            if outcome.strip().lower() == DIRECTION.lower() and i < len(token_ids):
                token_id = token_ids[i]
                break

        if not token_id:
            print(f"  [{coin.upper()}] No token for direction '{DIRECTION}'. Outcomes: {outcomes}")
            continue

        matched.append({
            "coin":     coin,
            "slug":     slug,
            "question": market.get("question") or slug,
            "token_id": token_id,
        })

    return matched


def fetch_current_markets_with_retry(
    ts: int,
    max_attempts: int = 3,
    delay: int = 15,
) -> list[dict]:
    """Retry fetching until all configured COINS have a market for this window."""
    for attempt in range(1, max_attempts + 1):
        print(f"  Attempt {attempt}/{max_attempts} — querying tag_slug=5M&up-or-down (ts={ts}) …")
        try:
            markets = fetch_current_markets(ts)
            found_coins = {m["coin"] for m in markets}
            missing     = COINS - found_coins
            if markets and not missing:
                return markets
            if markets and missing:
                print(f"  Found {[m['coin'].upper() for m in markets]}, missing: {[c.upper() for c in missing]}")
            else:
                print(f"  No markets found for window ts={ts} yet.")
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            print(f"  API error: {exc}")

        if attempt < max_attempts:
            print(f"  Retrying in {delay}s …")
            time.sleep(delay)

    # Return whatever we managed to find on last attempt (partial is better than nothing)
    try:
        return fetch_current_markets(ts)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Order signing (parallel, local — no network) + batch submission
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


def sign_order(client: ClobClient, market: dict) -> Optional[tuple[str, PostOrdersArgs]]:
    """Sign one market order locally (no network). Returns (coin, PostOrdersArgs) or None."""
    coin     = market["coin"]
    token_id = market["token_id"]
    label    = coin.upper()
    try:
        order_args   = MarketOrderArgs(token_id=token_id, amount=ORDER_AMOUNT, side=BUY)
        signed_order = client.create_market_order(order_args)
        print(f"  [{label}] Signed BUY {DIRECTION} ${ORDER_AMOUNT} USDC  (token …{token_id[-8:]})")
        return coin, PostOrdersArgs(order=signed_order, orderType=OrderType.FOK)
    except Exception as exc:
        print(f"  [{label}] Signing failed: {exc}")
        return None


def post_orders_with_retry(
    client: ClobClient,
    batch: list[PostOrdersArgs],
    labels: list[str],
    max_attempts: int = 3,
) -> None:
    """Submit all signed orders in ONE POST /orders call, with up to 3 retries."""
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\nSubmitting {len(batch)} order(s) [{', '.join(labels)}]"
                  f" — attempt {attempt}/{max_attempts} …")
            response = client.post_orders(batch)
            print(f"Batch response: {response}")
            return
        except Exception as exc:
            print(f"Batch attempt {attempt} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(2)
    print(f"All {max_attempts} batch attempts failed.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle() -> None:
    ts      = current_window_timestamp()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"Cycle  : {now_str}")
    print(f"Window : {ts}  |  Direction: {DIRECTION}  |  Amount: ${ORDER_AMOUNT} USDC")
    print(f"Coins  : {', '.join(sorted(c.upper() for c in COINS))}")

    # --- Phase 1: ONE API call → all markets for current window + configured coins ---
    print(f"\n[Phase 1] Fetching all active 5M up-or-down markets …")
    markets = fetch_current_markets_with_retry(ts)

    if not markets:
        print("No markets found. Skipping cycle.")
        return

    print(f"  Found: {[m['slug'] for m in markets]}")

    # --- Phase 2: Initialize client ---
    print("\n[Phase 2] Initializing CLOB client …")
    try:
        client = build_client()
    except Exception as exc:
        print(f"Client init failed: {exc}")
        return

    # --- Phase 3: Sign all orders in parallel (local, no network) ---
    print(f"\n[Phase 3] Signing {len(markets)} order(s) in parallel …")
    signed: list[tuple[str, PostOrdersArgs]] = []

    with ThreadPoolExecutor(max_workers=len(markets)) as pool:
        futures = [pool.submit(sign_order, client, m) for m in markets]
        for future in futures:
            result = future.result()
            if result:
                signed.append(result)

    if not signed:
        print("No orders signed successfully. Skipping submission.")
        return

    # --- Phase 4: Submit ALL signed orders in ONE batch call ---
    labels = [coin.upper() for coin, _ in signed]
    batch  = [args for _, args in signed]
    print(f"\n[Phase 4] Batch submitting {len(batch)} order(s): {', '.join(labels)}")
    post_orders_with_retry(client, batch, labels)


def main() -> None:
    print("=== Policar — Polymarket 5-min Bot ===")
    print(f"Coins     : {', '.join(sorted(c.upper() for c in COINS))}")
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
