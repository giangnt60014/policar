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
# Market discovery — per-coin slug lookup, parallel
# ---------------------------------------------------------------------------

def _curl_get(url: str) -> dict:
    """Fetch a URL via subprocess curl (bypasses Cloudflare TLS fingerprinting)."""
    result = subprocess.run(
        ["curl", "-s", "--max-time", "10"] + CURL_HEADERS + [url],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exited {result.returncode}: {result.stderr.strip()}")
    raw = result.stdout.strip()
    if not raw:
        raise ValueError("Empty response from API")
    return json.loads(raw)


def _extract_market(event: dict, coin: str) -> Optional[dict]:
    """
    Given an event dict from /events/slug/..., extract market info.
    Returns { coin, slug, question, token_id } or None if unusable.
    """
    now_utc  = datetime.now(timezone.utc)
    end_date = event.get("endDate", "")
    if end_date:
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end_dt <= now_utc:
            return None   # already ended

    slug   = event.get("slug", "")
    market = (event.get("markets") or [{}])[0]

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
        return None

    return {
        "coin":     coin,
        "slug":     slug,
        "question": market.get("question") or slug,
        "token_id": token_id,
    }


def fetch_coin_market(coin: str, ts: int, max_attempts: int = 3, delay: int = 15) -> Optional[dict]:
    """
    Fetch the current open market for one coin using the exact window timestamp.
    Retries up to max_attempts times if the market isn't live yet.
    """
    slug = f"{coin}-updown-5m-{ts}"
    url  = f"{GAMMA_API}/events/slug/{slug}"

    for attempt in range(1, max_attempts + 1):
        try:
            event  = _curl_get(url)
            result = _extract_market(event, coin)
            if result:
                print(f"  [{coin.upper()}] Found: {slug}")
                return result
        except (RuntimeError, ValueError, json.JSONDecodeError):
            pass

        if attempt < max_attempts:
            print(f"  [{coin.upper()}] Not found (attempt {attempt}/{max_attempts}), retrying in {delay}s …")
            time.sleep(delay)

    print(f"  [{coin.upper()}] No open market found after {max_attempts} attempts. Skipping.")
    return None


def fetch_all_markets(ts: int) -> list[dict]:
    """Fetch open markets for all configured COINS in parallel."""
    results = []
    with ThreadPoolExecutor(max_workers=len(COINS)) as pool:
        futures = {pool.submit(fetch_coin_market, coin, ts): coin for coin in COINS}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
    return results


# ---------------------------------------------------------------------------
# Order signing (parallel, local) + batch submission
# ---------------------------------------------------------------------------

def build_client(max_attempts: int = 3, delay: int = 5) -> ClobClient:
    for attempt in range(1, max_attempts + 1):
        try:
            client = ClobClient(
                HOST,
                key=PK,
                chain_id=POLYGON,
                signature_type=1,   # Email / Magic wallet delegated signing
                funder=FUNDER,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            return client
        except Exception as exc:
            print(f"  Client init attempt {attempt}/{max_attempts} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(delay)
    raise RuntimeError(f"CLOB client init failed after {max_attempts} attempts")


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
    """Submit all signed orders in ONE batch call, with up to 3 retries."""
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\nSubmitting {len(batch)} order(s) [{', '.join(labels)}]"
                  f" — attempt {attempt}/{max_attempts} …")
            response = client.post_orders(batch)
            print(f"Batch response: {json.dumps(response, indent=2)}")
            return
        except Exception as exc:
            print(f"Batch attempt {attempt} failed: {exc}")
            if attempt < max_attempts:
                time.sleep(2)
    print(f"All {max_attempts} batch attempts failed.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle(client: ClobClient) -> None:
    ts      = current_window_timestamp()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"\n{'='*60}")
    print(f"Cycle    : {now_str}")
    print(f"Window ts: {ts}  |  Direction: {DIRECTION}  |  Amount: ${ORDER_AMOUNT} USDC")
    print(f"Coins    : {', '.join(sorted(c.upper() for c in COINS))}")

    # --- Phase 1: fetch open market per coin in parallel ---
    print(f"\n[Phase 1] Fetching open markets (parallel) …")
    markets = fetch_all_markets(ts)

    if not markets:
        print("No markets found. Skipping cycle.")
        return

    print(f"  Ready  : {[m['slug'] for m in markets]}")

    # --- Phase 2: Sign all orders in parallel (local, no network) ---
    print(f"\n[Phase 2] Signing {len(markets)} order(s) in parallel …")
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

    # --- Phase 3: Submit ALL signed orders in ONE batch call ---
    labels = [coin.upper() for coin, _ in signed]
    batch  = [args for _, args in signed]
    print(f"\n[Phase 3] Batch submitting {len(batch)} order(s): {', '.join(labels)}")
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

    print("Initializing CLOB client …")
    client = build_client()
    print("Client ready.\n")

    while True:
        trigger = next_trigger_time()
        wait    = (trigger - datetime.now(timezone.utc)).total_seconds()
        print(f"Next trigger : {trigger.strftime('%H:%M:%S UTC')}  (in {wait:.1f}s)")
        time.sleep(max(0, wait))
        run_cycle(client)


if __name__ == "__main__":
    main()
