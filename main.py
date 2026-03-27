import json
import os
import sys
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderType, MarketOrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

load_dotenv()

HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

PK = os.environ.get("PK")
FUNDER = os.environ.get("FUNDER")
ORDER_AMOUNT = float(os.environ.get("ORDER_AMOUNT", "10"))


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def get_market_by_slug(slug: str) -> dict:
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"No market found for slug: {slug!r}")
    return data[0]


def build_client() -> ClobClient:
    if not PK:
        sys.exit("Error: PK not set in .env")
    if not FUNDER:
        sys.exit("Error: FUNDER not set in .env")

    client = ClobClient(
        HOST,
        key=PK,
        chain_id=POLYGON,
        signature_type=1,   # Email / Magic wallet (delegated signing)
        funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def main():
    print("=== Policar — Polymarket Order Bot ===")
    print(f"Signature type : 1 (Email/Magic wallet)")
    print(f"Funder address : {FUNDER}")
    print(f"Order amount   : ${ORDER_AMOUNT} USDC")
    print()

    # --- Wait for slug input ---
    slug = input("Enter market slug: ").strip()
    if not slug:
        sys.exit("No slug entered. Exiting.")

    # --- Resolve slug → tokens ---
    print(f"\nLooking up market '{slug}'...")
    try:
        market = get_market_by_slug(slug)
    except (ValueError, RuntimeError, requests.RequestException) as e:
        sys.exit(f"Error: {e}")

    question = market.get("question") or market.get("title") or slug

    raw_ids = market.get("clobTokenIds", "[]")
    raw_outcomes = market.get("outcomes", "[]")
    token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
    outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

    if not token_ids:
        sys.exit("Error: Market has no token IDs. It may be resolved or unavailable.")

    tokens = [{"outcome": outcomes[i] if i < len(outcomes) else f"Token {i}",
               "token_id": token_ids[i]} for i in range(len(token_ids))]

    print(f"\nMarket: {question}")
    print()
    for i, token in enumerate(tokens):
        print(f"  [{i}] {token['outcome']:10s}  token_id: {token['token_id']}")

    # --- Select outcome ---
    print()
    choice_raw = input("Select outcome index [0]: ").strip() or "0"
    try:
        choice = int(choice_raw)
        selected = tokens[choice]
    except (ValueError, IndexError):
        sys.exit(f"Invalid selection: {choice_raw!r}")

    token_id = selected["token_id"]
    outcome = selected["outcome"]

    if not token_id:
        sys.exit("Error: Selected token has no token_id.")

    # --- Confirm ---
    print(f"\nAbout to place:")
    print(f"  Market  : {question}")
    print(f"  Outcome : {outcome}")
    print(f"  Side    : BUY")
    print(f"  Amount  : ${ORDER_AMOUNT} USDC")
    print(f"  Type    : FOK (Fill or Kill)")
    confirm = input("\nProceed? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        sys.exit("Cancelled.")

    # --- Build client & place order ---
    print("\nInitializing client and signing credentials...")
    client = build_client()

    order_args = MarketOrderArgs(
        token_id=token_id,
        amount=ORDER_AMOUNT,
        side=BUY,
    )
    market_order = client.create_market_order(order_args)

    print("Submitting order...")
    response = client.post_order(market_order, OrderType.FOK)

    print(f"\nOrder response: {response}")


if __name__ == "__main__":
    main()
