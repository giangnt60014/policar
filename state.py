"""
Shared state management for Policar.
Bot writes; dashboard reads. Atomic file writes prevent corruption.
"""
import json
import os
import threading
from datetime import datetime, timezone

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
_lock = threading.Lock()


def _default_coin(base_bet: float) -> dict:
    return {
        # --- Martingale fields ---
        "current_bet": base_bet,
        "streak":      0,           # >0 win streak, <0 loss streak
        "last_result": None,        # "win" | "loss" | None
        "last_slug":   None,
        "wins":        0,
        "losses":      0,
        "total_spent": 0.0,
        "history":     [],          # last 20 entries
        # --- Follower sub-dict ---
        "follower": _default_follower(),
    }


def _default_follower() -> dict:
    return {
        "last_result":    None,   # "win" | "loss" | None
        "last_direction": None,   # "Up" | "Down" — what was bought
        "last_slug":      None,
        "wins":           0,
        "losses":         0,
        "total_spent":    0.0,
        "history":        [],     # last 20 entries
    }


def load() -> dict:
    with _lock:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
        return {"bot": {}, "coins": {}, "log": []}


def save(state: dict) -> None:
    """Atomic write — no partial reads from dashboard."""
    tmp = STATE_FILE + ".tmp"
    with _lock:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)


def ensure_coin(state: dict, coin: str, base_bet: float) -> None:
    state.setdefault("coins", {})
    if coin not in state["coins"]:
        state["coins"][coin] = _default_coin(base_bet)
    else:
        # Backfill follower sub-dict for existing state.json entries
        state["coins"][coin].setdefault("follower", _default_follower())


def add_log(state: dict, msg: str, max_entries: int = 50) -> None:
    state.setdefault("log", [])
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    state["log"].insert(0, {"time": ts, "msg": msg})
    state["log"] = state["log"][:max_entries]


def apply_result(state: dict, coin: str, base_bet: float, max_bet: float,
                 prev_slug: str, bet: float, won: bool, winner: str) -> None:
    """Update Martingale state for one coin after resolution."""
    ensure_coin(state, coin, base_bet)
    cs = state["coins"][coin]

    if won:
        cs["wins"]        += 1
        cs["streak"]       = cs["streak"] + 1 if cs["streak"] >= 0 else 1
        cs["current_bet"]  = base_bet
        cs["last_result"]  = "win"
        add_log(state, f"[{coin.upper()}] ✅ WIN  slug={prev_slug}  bet=${bet:.2f}  → reset to ${base_bet:.2f}")
    else:
        cs["losses"]      += 1
        cs["streak"]       = cs["streak"] - 1 if cs["streak"] <= 0 else -1
        next_bet           = min(bet * 2, max_bet)
        cs["current_bet"]  = next_bet
        cs["last_result"]  = "loss"
        add_log(state, f"[{coin.upper()}] ❌ LOSS slug={prev_slug}  bet=${bet:.2f}  → next ${next_bet:.2f}")

    cs["total_spent"] += bet
    cs["history"].insert(0, {
        "slug":   prev_slug,
        "bet":    bet,
        "result": "win" if won else "loss",
        "winner": winner,
    })
    cs["history"] = cs["history"][:20]


def apply_follower_result(state: dict, coin: str, slug: str,
                          bet: float, won: bool, direction: str) -> None:
    """Update Follower state for one coin after resolution (no martingale doubling)."""
    cs = state["coins"][coin]
    fl = cs.setdefault("follower", _default_follower())

    result_str        = "win" if won else "loss"
    fl["last_result"]    = result_str
    fl["last_direction"] = direction
    fl["last_slug"]      = slug
    fl["total_spent"]   += bet

    if won:
        fl["wins"] += 1
        add_log(state, f"[{coin.upper()}] FOLLOWER ✅ WIN   dir={direction}  bet=${bet:.2f}")
    else:
        fl["losses"] += 1
        add_log(state, f"[{coin.upper()}] FOLLOWER ❌ LOSS  dir={direction}  bet=${bet:.2f}")

    fl["history"].insert(0, {
        "slug":      slug,
        "bet":       bet,
        "result":    result_str,
        "direction": direction,
    })
    fl["history"] = fl["history"][:20]
