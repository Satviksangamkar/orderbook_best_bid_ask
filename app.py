#!/usr/bin/env python3
import argparse
import asyncio
import json
import requests
import websockets
import time
import sys
from typing import Dict
import redis.asyncio as aioredis
from redis.exceptions import RedisError

# ─── CLI Arguments ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Binance orderbook tracker")
parser.add_argument("--symbol",            default="BTCUSDT")
parser.add_argument("--update-speed",     default="100ms")
parser.add_argument("--print-interval",   type=float, default=3.0)
parser.add_argument("--refresh-interval", type=float, default=60.0)
parser.add_argument("--redis-dsn",        required=True,
                    help="e.g. redis://:password@hostname:port")
args = parser.parse_args()

SYMBOL           = args.symbol
UPDATE_SPEED     = args.update_speed
PRINT_INTERVAL   = args.print_interval
REFRESH_INTERVAL = args.refresh_interval
REDIS_DSN        = args.redis_dsn

# ─── Bands & Endpoints ──────────────────────────────────────────────────────
BANDS = [
    (0,    1),
    (1,    2.5),
    (2.5,  5),
    (5,   10),
    (10,  25),
]

BASE_URL          = "https://fapi.binance.com"
SNAPSHOT_ENDPOINT = "/fapi/v1/depth"
WS_URL_TEMPLATE   = "wss://fstream.binance.com/ws/{symbol}@depth@{speed}"

def clear_screen():
    # ANSI “clear screen” (no os.system)
    print("\033c", end="")

def fetch_snapshot(symbol: str, limit: int = None):
    resp = requests.get(
        f"{BASE_URL}{SNAPSHOT_ENDPOINT}",
        params={"symbol": symbol, **({"limit": limit} if limit else {})},
        timeout=5
    )
    resp.raise_for_status()
    data = resp.json()
    bids = {float(p): float(q) for p, q in data["bids"]}
    asks = {float(p): float(q) for p, q in data["asks"]}
    return bids, asks, data["lastUpdateId"]

def apply_diff(bids: Dict[float, float], asks: Dict[float, float], diff: dict):
    for side, book in (("b", bids), ("a", asks)):
        for price_str, qty_str in diff.get(side, []):
            price, qty = float(price_str), float(qty_str)
            if qty == 0:
                book.pop(price, None)
            else:
                book[price] = qty

def calculate_band_metrics(bids: Dict[float, float], asks: Dict[float, float]):
    if not bids or not asks:
        return None, None, [], [], []
    best_bid = max(bids)
    best_ask = min(asks)
    bid_vols, ask_vols, ratios = [], [], []
    for lo, hi in BANDS:
        bv = sum(
            qty for price, qty in bids.items()
            if lo <= (best_bid - price) / best_bid * 100 < hi
            or (hi == BANDS[-1][1] and lo <= (best_bid - price) / best_bid * 100 <= hi)
        )
        av = sum(
            qty for price, qty in asks.items()
            if lo <= (price - best_ask) / best_ask * 100 < hi
            or (hi == BANDS[-1][1] and lo <= (price - best_ask) / best_ask * 100 <= hi)
        )
        delta = bv - av
        total = bv + av
        ratio = (delta / total) * 100 if total else 0.0
        bid_vols.append(bv)
        ask_vols.append(av)
        ratios.append(ratio)
    return best_bid, best_ask, bid_vols, ask_vols, ratios

async def store_snapshot(redis_client, symbol: str, bids, asks, last_update_id: int):
    try:
        timestamp_min = int(time.time() // 60)
        key = f"snapshot:{symbol}:{timestamp_min}"
        payload = {
            "lastUpdateId": last_update_id,
            "bids": {f"{p:.8f}": q for p, q in bids.items()},
            "asks": {f"{p:.8f}": q for p, q in asks.items()}
        }
        await redis_client.set(key, json.dumps(payload))
        await redis_client.expire(key, 120 * 60)
    except RedisError as e:
        print(f"[ERROR][store_snapshot] Redis error: {e}", file=sys.stderr)

async def main():
    while True:
        try:
            redis_client = aioredis.from_url(REDIS_DSN, decode_responses=True)
            await redis_client.ping()

            ws_url = WS_URL_TEMPLATE.format(
                symbol=SYMBOL.lower(),
                speed=UPDATE_SPEED
            )

            ws = await websockets.connect(ws_url)
            buffer, start = [], time.time()
            while time.time() - start < 1.0:
                msg   = await ws.recv()
                frame = json.loads(msg)
                buffer.append(frame.get("data", frame))

            bids, asks, last_update_id = fetch_snapshot(SYMBOL)
            synced = False
            for diff in buffer:
                U, u = diff.get("U"), diff.get("u")
                if u <= last_update_id:
                    continue
                if not synced:
                    if U <= last_update_id + 1 <= u:
                        synced = True
                    else:
                        continue
                apply_diff(bids, asks, diff)
                last_update_id = u

            clear_screen()
            print("✅  Snapshot synced. Entering live loop…")

            last_print   = time.time()
            last_refresh = time.time()

            async for msg in ws:
                now = time.time()
                if now - last_refresh >= REFRESH_INTERVAL:
                    bids, asks, last_update_id = fetch_snapshot(SYMBOL)
                    await store_snapshot(redis_client, SYMBOL, bids, asks, last_update_id)
                    last_refresh = now
                    continue

                diff = json.loads(msg).get("data", {})
                U, u = diff.get("U"), diff.get("u")
                if u <= last_update_id:
                    continue
                if U > last_update_id + 1:
                    bids, asks, last_update_id = fetch_snapshot(SYMBOL)
                    await store_snapshot(redis_client, SYMBOL, bids, asks, last_update_id)
                    last_refresh = now
                    continue

                apply_diff(bids, asks, diff)
                last_update_id = u

                if now - last_print >= PRINT_INTERVAL:
                    last_print = now
                    bb, ba, bv, av, ratios = calculate_band_metrics(bids, asks)
                    clear_screen()
                    print(f"{SYMBOL} | Best Bid: {bb:.2f} | Best Ask: {ba:.2f}")
                    print("-" * 60)
                    print(f"{'Band':>8} | {'Bid Vol':>10} | {'Ask Vol':>10} | {'Δ':>8} | {'Ratio':>8}")
                    print("-" * 60)
                    for (lo, hi), bvol, avol, r in zip(BANDS, bv, av, ratios):
                        print(f"{lo:>3.1f}-{hi:<4.1f}% | {bvol:10.3f} | {avol:10.3f} | {bvol-avol:8.3f} | {r:+7.2f}%")
                    print()

        except (websockets.ConnectionClosed, requests.RequestException,
                json.JSONDecodeError, RedisError) as e:
            print(f"[RECOVERABLE ERROR] {e}; reconnecting in 5s…", file=sys.stderr)
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[UNEXPECTED ERROR] {e}; restarting in 5s…", file=sys.stderr)
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        clear_screen()
        print("✋  Received exit signal. Goodbye.")
