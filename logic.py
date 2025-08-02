# logic.py
import os
import json
import csv
import logging
import math
import sys
import pandas as pd 
import requests

from binance.client import Client
from binance.exceptions import BinanceAPIException
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict


TRAIL_ACTIVATION_PCT = 0.005  # Start trailing once gain hits 1%
TRAIL_STOP_GAP_PCT   = 0.01  # Trigger sell if 1% below peak
TSL_GAP_PCT = 0.01  # 1% trailing stop
HARD_STOP_PCT = - 0.0075  # -1% hard stop for Manual trades

# === 🕒 Global Candle Interval Settings ===
CANDLE_INTERVAL = "5m"         # Used for entry logic (Ross, Box, etc.)
EXIT_CANDLE_INTERVAL = "5m"    # Used for exit logic (TSL, red candle, etc.


# --- Strategy Parameters ---
SURGE_MIN_PCT = 0.01
SURGE_CAP_AVG_PCT  = 0.003   # new: max average surge per candle (0.3%)
PULLBACK_MAX_PCT = SURGE_MIN_PCT * 0.5
TSL_GAP_PCT = 0.01

EMA_FAST = 9
EMA_MID  = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# percent of equity per trade, and max concurrent trades
RISK_PROFILES = {
    'Bearish': {'max_trades': 2, 'trade_size_pct': 0.005},   # 0.5% per trade, 1 open position
    'Neutral': {'max_trades': 5, 'trade_size_pct': 0.01},    # 1% per trade, up to 3 positions
    'Bullish': {'max_trades': 5, 'trade_size_pct': 0.02},    # 2% per trade, up to 5 positions
}

# --- Configuration & Setup ---
API_KEY = os.getenv('BINANCE_API_KEY') 
API_SECRET = os.getenv('BINANCE_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

last_sell_time = {}  # {symbol: datetime}
cooldown_period = timedelta(minutes=15)
support_touched = {}
last_btc_warning = None
btc_was_blocked = False
processed_manual_buys = set()


daily_pnl = 0.0
last_reset = datetime.now(timezone.utc).date()
CSV_LOG = Path("trade_log.csv")
CSV_HEADERS = ["timestamp", "symbol", "entry", "exit", "gain_usd", "gain_pct",
               "peak_gain_pct", "reason", "forecast", "strategy", "passed", "failed"]
PNL_FILE = Path("pnl.json")
MANUAL_POS_FILE = Path('manual_positions.json')
BLACKLISTED_SYMBOLS = {"USDCUSDT"}


# Load manual buys
if MANUAL_POS_FILE.exists():
    with open(MANUAL_POS_FILE, 'r') as f:
        open_positions = json.load(f)
else:
    open_positions = {}

def to_binance_symbol(pair: str) -> str:
    """Convert 'HBAR/USDT' → 'HBARUSDT' for Binance API."""
    return pair.replace("/", "").upper()

def base_asset(pair: str) -> str:
    """Extract base asset 'HBAR' from 'HBAR/USDT'."""
    return pair.split("/")[0].upper()

def update_market_sell(symbol: str, trade: dict):
    try:
        # === Cancel old limit order if present ===
        if trade.get("limit_order_id"):
            try:
                client.cancel_order(
                    symbol=to_binance_symbol(symbol),
                    orderId=trade["limit_order_id"]
                )
                trade.pop("limit_order_id", None)
            except Exception as e:
                logging.warning(f"⚠️ Could not cancel old limit order for {symbol}: {e}")

        # === Prepare Binance-safe symbols ===
        api_symbol = to_binance_symbol(symbol)
        asset      = base_asset(symbol)

        # === Get balance & precision ===
        bal_resp = client.get_asset_balance(asset) or {"free": "0"}
        balance  = float(bal_resp["free"])
        if balance <= 0:
            logging.info(f"Skip sell {symbol}: zero balance")
            return

        info      = client.get_symbol_info(api_symbol)
        step_size = float(
            next(
                f["stepSize"]
                for f in info["filters"]
                if f["filterType"] == "LOT_SIZE"
            )
        )
        precision = int(round(-math.log10(step_size)))

        qty = math.floor(balance / step_size) * step_size
        qty = round(qty, precision)
        if qty <= 0:
            return

        # === Execute Market Sell ===
        order = client.order_market_sell(symbol=api_symbol, quantity=qty)
        if order.get("status") == "FILLED":
            logging.info(f"💥 Market sell executed for {symbol}")
        else:
            logging.warning(f"⚠️ Market sell not filled for {symbol}: {order}")

    except Exception as e:
        logging.error(f"🔥 Error executing market sell for {symbol}: {e}", exc_info=True)

def get_btc_regime(lookback_candles: int = 2, stable_thresh: float = 0.005, drifting_thresh: float = -0.02) -> str:
    try:
        klines = client.get_klines(symbol="BTCUSDT", interval="1h", limit=lookback_candles + 1)
        if not klines or len(klines) < lookback_candles + 1:
            return "stable"
        closes = [float(k[4]) for k in klines]
        old, new = closes[0], closes[-1]
        pct = (new - old) / old
        if abs(pct) <= stable_thresh:
            return "stable"
        if pct < drifting_thresh:
            return "plunging"
        return "drifting"
    except:
        return "stable"

def btc_micro_ok():
    """
    Returns True if the last 2 closed 1m BTC bars are both green.
    """
    kl = client.get_klines(symbol="BTCUSDT", interval="1m", limit=2)
    if not kl or len(kl) < 2:
        return False

    # require both of the last 2 bars to close above open
    for bar in kl:
        o, c = float(bar[1]), float(bar[4])
        if c <= o:
            return False
    return True


def token_current_1m_ok(symbol: str):
    """
    Returns True if the last 1m bar of the given symbol is green.
    """
    kl = client.get_klines(symbol=symbol, interval="1m", limit=1)
    if not kl or len(kl) < 1:
        return False

    for bar in kl:
        o, c = float(bar[1]), float(bar[4])
        if c <= o:
            return False
    return True

def save_manual_positions():
    serializable_positions = {}
    for symbol, data in open_positions.items():
        serializable_data = data.copy()
        if isinstance(serializable_data.get("buy_time"), datetime):
            serializable_data["buy_time"] = serializable_data["buy_time"].isoformat()
        serializable_positions[symbol] = serializable_data

    with open(MANUAL_POS_FILE, 'w') as f:
        json.dump(serializable_positions, f, indent=2)

alert_cache: Dict[str, str] = {}  
client = Client(API_KEY, API_SECRET)

TRADE_FILE = Path('open_trades.json')

# Load or initialize open trades
if TRADE_FILE.exists():
    with open(TRADE_FILE, 'r') as f:
        open_trades: Dict[str, Dict] = json.load(f)
else:
    open_trades = {}

def is_within_40pct_from_low(current_price, low_24h, high_24h):
    if high_24h == low_24h:
        return False
    distance_from_low = (current_price - low_24h) / (high_24h - low_24h)
    return distance_from_low <= 0.40

def save_trades():
    with open(TRADE_FILE, 'w') as f:
        json.dump(open_trades, f, indent=2)

def get_klines(symbol: str, interval: str, limit: int = 50):
    return client.get_klines(symbol=symbol, interval=interval, limit=limit)

def get_top_gainers(limit: int = 15) -> list[str]:
    tickers = client.get_ticker_24hr()
    sorted_by_pct = sorted(
        [t for t in tickers if t["symbol"].endswith("USDT") and t["symbol"] not in BLACKLISTED_SYMBOLS],
        key=lambda t: float(t["priceChangePercent"]), reverse=True
    )
    return [t["symbol"] for t in sorted_by_pct[:limit]]

def detect_surge_pattern(klines: list) -> bool:
    msg, surge_pct, _ = detect_ross_setup(klines)
    return msg is not None

def compute_surge_pct(klines: list) -> float:
    _, surge_pct, _ = detect_ross_setup(klines)
    return surge_pct or 0.0


def evaluate_ross_strategy(symbol, candles, ticker_24h):

    scan_btc_conditions()
    message, surge_pct, surge_candles = detect_ross_setup(candles)
    if message is None:
        return

    current_price = float(candles[-1][4])
    low_24h = float(ticker_24h.get("lowPrice", 0))
    high_24h = float(ticker_24h.get("highPrice", 1))

    if not is_within_40pct_from_low(current_price, low_24h, high_24h):
        return

    send_telegram_message(f"{message}\\n📥 Entry triggered ✅ (Ross Strategy)")
    execute_auto_buy(symbol, "Ross", surge_pct=0.0)

def grade_filters(passes: dict) -> str:
    emacount = sum([passes.get(x, False) for x in ['EMA9', 'EMA20',]])
    vwap = passes.get('VWAP', False)
    macd = passes.get('MACD', False)

    if vwap and macd and emacount >= 2:
        return "A"
    elif vwap and macd and emacount == 1:
        return "B"
    elif (vwap and macd) or (sum(passes.values()) >= 3):
        return "C"
    elif sum(passes.values()) == 2:
        return "D"
    elif sum(passes.values()) == 1:
        return "E"
    return "F"

def log_surge_candidate(symbol, surge, pullback, confirm, filter_passes):
    # rebuild parts in the new order
    parts = [f"{symbol}:"]

    # 1) Bitcoin
    if 'Bitcoin' in filter_passes:
        parts.append(f"Bitcoin {'✅' if filter_passes['Bitcoin'] else '❌'}")

    # 2) Sideways
    if 'Sideways' in filter_passes:
        parts.append(f"Sideways {'✅' if filter_passes['Sideways'] else '❌'}")

    # 3) MACD
    if 'MACD' in filter_passes:
        parts.append(f"MACD {'✅' if filter_passes['MACD'] else '❌'}")

    # 4) VWAP
    if 'VWAP' in filter_passes:
        parts.append(f"VWAP {'✅' if filter_passes['VWAP'] else '❌'}")

    # 5) EMAs
    if 'EMAs' in filter_passes:
        parts.append(f"EMAs {'✅' if filter_passes['EMAs'] else '❌'}")

    # 6) Surge, Pullback, Confirm
    parts.append(f"Surge {'✅' if surge else '❌'}")
    parts.append(f"Pullback {'✅' if pullback else '❌'}")
    parts.append(f"Confirm {'✅' if confirm else '❌'}")

    print(" > ".join(parts))


def send_telegram_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning('Telegram config missing, skipping message')
        return
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': text}
    try:
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
    except Exception as e:
        logging.error(f'Telegram error: {e}')

def detect_ross_setup(candles: list) -> tuple[str, float, int] | tuple[None, None, None]:
    """
    Detects Ross-style Surge → Pullback → Confirm setup.
    Supports:
      • ANY number of consecutive green candles (>= 3) as the “surge”
      • ANY number of consecutive red candles as the “pullback”, 
        as long as sum(drawdowns) ≤ 50% of total surge
      • Confirmation candle must be green
    """
    if len(candles) < 20:
        return None, None, None

    closes = [float(k[4]) for k in candles]
    opens = [float(k[1]) for k in candles]

    # --- 1) Check Confirmation Candle is Green ---
    confirm = candles[-1]
    confirm_open = float(confirm[1])
    confirm_close = float(confirm[4])
    if confirm_close <= confirm_open:
        return None, None, None

    # --- 2) Find all consecutive red bars immediately before confirm as pullback ---
    pullback_pct_sum = 0.0
    pullback_end_idx = len(candles) - 2
    pullback_bars = 0
    for i in range(len(candles) - 2, -1, -1):
        o = float(candles[i][1])
        c = float(candles[i][4])
        if c < o:
            # red bar → accumulate its drawdown
            pullback_pct_sum += (o - c) / o
            pullback_bars += 1
        else:
            break

    if pullback_bars == 0:
        return None, None, None  # must have at least one red bar as pullback

    # --- 3) Detect Surge BEFORE the pullback region (all consecutive green bars) ---
    surge_pct_sum = 0.0
    surge_start_price = None
    surge_end_price = None
    surge_bars = 0

    for j in range(len(candles) - 2 - pullback_bars, -1, -1):
        o = float(candles[j][1])
        c = float(candles[j][4])
        if c > o:
            # green bar → count it in surge
            if surge_end_price is None:
                surge_end_price = c
            surge_start_price = o
            surge_pct_sum += (c - o) / o
            surge_bars += 1
        else:
            break

    # Must have at least 3 green candles in the surge
    if surge_bars < 3:
        return None, None, None

    # --- 4) Reject if pullback > 50% of total surge ---
    if pullback_pct_sum > surge_pct_sum * 0.5:
        return None, None, None

    # --- 5) Confirm must either break or bounce above the final pullback high ---
    final_pull_high = float(candles[-2][2])
    if confirm_close <= final_pull_high:
        return None, None, None


    # --- 6) Compute percentages for message ---
    confirm_pct = (confirm_close - confirm_open) / confirm_open
    time_left_ms = confirm[6] - int(datetime.now(timezone.utc).timestamp() * 1000)
    minutes_left = max(0, int(time_left_ms / 60000))

    message = (
        f"📊📦 Ross Setup Detected\n\n"
        f"✅ Surge: +{surge_pct_sum * 100:.2f}% over {surge_bars} candles\n"
        f"✅ Pullback: -{pullback_pct_sum * 100:.2f}% over {pullback_bars} candles\n"
        f"✅ Confirm: +{confirm_pct * 100:.2f}% (candle)\n"
        f"⏳ Candle ends in ~{minutes_left}m"
    )

    return message, surge_pct_sum, surge_bars



# --- Manual Buys ---

def detect_manual_buys():
    """
    Scan for new manual buys (balances > 0), require notional ≥ $5,
    enforce cooldown, verify lot size, seed metadata for exits,
    and trigger a heartbeat milestone on entry.
    """
    balances = client.get_account().get("balances", [])
    new_buys = []
    now = datetime.now(timezone.utc)
    logging.debug(f"detect_manual_buys: starting scan at {now.isoformat()}")

    for asset in balances:
        asset_name = asset["asset"]
        free = float(asset.get("free", 0))
        locked = float(asset.get("locked", 0))
        total = free + locked
        logging.debug(f"Checking asset {asset_name}: free={free}, locked={locked}, total={total}")

        # Skip USDT or dust
        if asset_name == "USDT" or total < 1e-6:
            logging.debug(f"Skipping {asset_name}: USDT or dust")
            continue

        symbol = asset_name + "USDT"

        # Already tracking this trade?
        if symbol in open_trades:
            logging.info("%s: skipped %s buy, already positioned", symbol, side)
            return False
    
        # Spot accounts can't open new SHORT positions without margin/futures.
        if side == "short":
            asset = base_asset(symbol)
            try:
                bal = float((client.get_asset_balance(asset) or {"free": "0"})["free"])
            except Exception:
                bal = 0.0
            if bal <= 0:
                logging.warning("%s: skipping short — no %s balance on spot", symbol, asset)
                send_telegram_message(f"⚠️ SHORT skipped for {symbol}: no {asset} balance on spot.")
                return False


        # Idempotent guard: don’t re-process the same manual buy
        if symbol in processed_manual_buys:
            logging.debug(f"Skipping {symbol}: already processed_manual_buys")
            continue

        # Cooldown after manual sell
        last_sell = last_sell_time.get(symbol)
        if last_sell and (now - last_sell) < cooldown_period:
            logging.debug(f"Skipping {symbol}: cooldown in effect since {last_sell.isoformat()}")
            continue

        # Verify lot size
        api_symbol = symbol.replace("/", "")
        try:
            info = client.get_symbol_info(api_symbol)
            min_qty = float(
                next(f["minQty"] for f in info["filters"] if f["filterType"] == "LOT_SIZE")
            )
            logging.debug(f"{symbol} min_qty={min_qty}")
        except Exception as e:
            logging.warning(f"⚠️ Couldn't verify lot size for {symbol}: {e}", exc_info=True)
            continue
        if total < min_qty:
            logging.debug(f"Skipping {symbol}: total {total} < min_qty {min_qty}")
            continue

        # Fetch current price
        try:
            price = float(client.get_symbol_ticker(symbol=api_symbol)["price"])
            logging.debug(f"{symbol} price={price}")
        except Exception as e:
            logging.warning(f"⚠️ Price fetch error for {symbol}: {e}", exc_info=True)
            continue

        # Require notional ≥ $5
        if price * total < 5.0:
            logging.debug(f"Skipping {symbol}: notional {price * total:.2f} < 5.0")
            continue

        # Seed trade metadata
        stop_price = price * (1 + HARD_STOP_PCT)  # HARD_STOP_PCT = -0.01 → 1% below entry
        trade_data = {
            "entry":               price,
            "entry_price":         price,
            "quantity":            total,
            "initial_qty":         total,
            "peak":                price,
            "surge_pct":           0.0,
            "stop":                stop_price,
            "phase1":              False,
            "phase2":              False,
            "phase3":              False,
            "buy_time":            now.isoformat(),
            "milestones":          [],
            "last_milestone_time": None,
            "strategy":            "Manual",
            "limit_order_id":      None,
        }
        open_trades[symbol] = trade_data

        # 🔔 Heartbeat on manual entry
        check_milestones(symbol, trade_data, price)

        processed_manual_buys.add(symbol)
        new_buys.append(symbol)
        logging.info(f"Registered manual buy → {symbol} @ {price}, qty={total}")

    # Announce & persist
    for sym in new_buys:
        send_telegram_message(f"🟡 Manual buy detected: {sym} @ ${open_trades[sym]['entry']:.4f}")
    if new_buys:
        save_trades()
        logging.debug("detect_manual_buys: trades saved")




FILTER_LOG = 'filter_cycles.csv'
FILTER_FIELDS = [
    'timestamp','symbol','current_price','gain_pct',
    'ENABLE_VOLUME_SPIKE','ENABLE_EMA_ALIGNMENT',
    'ENABLE_MACD_MOMENTUM','ENABLE_VWAP_GATE',
    'ENABLE_SIDEWAYS','ENABLE_BTC_CANDLE',
    'ENABLE_TOKEN_CANDLE'
]

def log_filter_cycle(symbol, current_price, gain_pct, filters: dict):
    """Append one row per cycle with all filter flags."""
    new_file = not os.path.isfile(FILTER_LOG)
    with open(FILTER_LOG, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FILTER_FIELDS)
        if new_file:
            writer.writeheader()
        row = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'symbol': symbol,
            'current_price': current_price,
            'gain_pct': gain_pct
        }
        # merge in the booleans
        for fld in FILTER_FIELDS[4:]:
            row[fld] = bool(filters.get(fld, False))
        writer.writerow(row)
      
def cleanup_trades():
    updated = False

    for symbol in list(open_trades.keys()):
        asset = symbol.replace("USDT", "")
        try:
            balance = float(client.get_asset_balance(asset)["free"])
            if balance < 1e-6:
                # 🖊️ Log & send CSV for manual exit
                trade = open_trades[symbol]
                entry = trade['entry_price']
                current = float(client.get_symbol_ticker(symbol=symbol)["price"])
                qty = trade['quantity']
                pnl = (current - entry) * qty
                gain_pct = (current / entry - 1) * 100
                peak_gain_pct = (trade['peak'] / entry - 1) * 100
                log_trade(
                    symbol,
                    entry,
                    current,
                    pnl,
                    gain_pct,
                    peak_gain_pct,
                    "ManualSell",
                    trade.get("forecast", "Unknown"),
                    trade.get("strategy"),
                    trade.get("passed"),
                    trade.get("failed")
                )
                # now remove it
                del open_trades[symbol]
                last_sell_time[symbol] = datetime.now(timezone.utc)
                send_telegram_message(f"⚪️ Removed closed position: {symbol}")
                updated = True
        except Exception as e:
            logging.error(f"Trade cleanup error for {symbol}: {e}", exc_info=True)

    if updated:
        save_trades()

def purge_dust_balances():
    try:
        balances = client.get_account().get("balances", [])
        dust_candidates = []

        for asset in balances:
            sym = asset["asset"]
            free = float(asset["free"])
            if sym == "USDT" or free == 0:
                continue

            pair = f"{sym}USDT"

            try:
                # Get min quantity and price
                info = client.get_symbol_info(pair)
                price = float(client.get_symbol_ticker(symbol=pair)['price'])
                min_qty = next(float(f["minQty"]) for f in info["filters"] if f["filterType"] == "LOT_SIZE")
                total_value = free * price

                # Consider dust if below $1.00 regardless of minQty
                if total_value < 1.00:
                    dust_candidates.append(sym)

            except Exception as e:
                logging.warning(f"⚠️ Could not validate dust for {pair}: {e}", exc_info=True)
                continue

        if not dust_candidates:
            logging.info("🧼 No dust candidates.")
            return

        # Supported redemption list
        dust_info = client.get_dust_assets()
        supported = {d["asset"] for d in dust_info.get("userAssetDribblets", [])}

        to_purge = [sym for sym in dust_candidates if sym in supported]
        if not to_purge:
            logging.info(f"🧼 No supported dust to purge among {dust_candidates}.")
            return

        result = client.transfer_dust(asset=",".join(to_purge))
        logging.info(f"🧹 Dust purged: {to_purge} → {result}")
        send_telegram_message(f"🧹 Purged dust for: {', '.join(to_purge)}")

    except Exception as e:
        logging.error(f"💥 Dust purge error: {e}", exc_info=True)
        send_telegram_message(f"⚠️ Dust purge failed: {e}")

def check_milestones(symbol: str, trade: dict, current_price: float):
    now = datetime.now(timezone.utc)
    entry = trade.get('entry_price', trade.get('entry'))
    peak = max(trade.get('peak', entry), current_price)
    trade['peak'] = peak

    gain_pct = (current_price - entry) / entry * 100
    tsl_trigger_pct = ((peak * (1 - TRAIL_STOP_GAP_PCT)) - entry) / entry * 100

    milestones = trade.setdefault('milestones', [])
    last_time_iso = trade.get('last_milestone_time')
    snail_last_iso = trade.get('last_snail_msg_time')

    # 1️⃣ Heartbeat at entry
    if not milestones:
        msg = f"💓 {symbol} entry confirmed at ${entry:.4f} — monitoring begins!"
        send_telegram_message(msg)
        logging.info(msg)
        milestones.append(0.0)
        trade['last_milestone_time'] = now.isoformat()
        return  # only send the heartbeat on first hit

    # 2️⃣ Tiered milestones
    milestone_messages = [
        (0.25,  f"💓 {symbol} +{gain_pct:.2f}% — early buzz. Peak: ${peak:.4f}"),
        (0.50,  f"🌱 {symbol} +0.50%! Gaining steam. Peak: ${peak:.4f}"),
        (0.75,  f"🌿 {symbol} +0.75%! Momentum building. Peak: ${peak:.4f}"),
        (1.00,  f"🌳 {symbol} +1.00%! Trend confirmed. Peak: ${peak:.4f}"),
        (1.50,  f"🎯 {symbol} +1.50%! On track. Peak: ${peak:.4f}"),
        (2.00,  f"🔥 {symbol} +2.00%! Strong move. Peak: ${peak:.4f}"),
        (3.00,  f"🏁 {symbol} +3.00%! Riding high. Peak: ${peak:.4f}"),
        (4.00,  f"🚀 {symbol} +4.00%! Soaring. Peak: ${peak:.4f}"),
        (5.00,  f"🏆 {symbol} +5.00%! Time to celebrate. Peak: ${peak:.4f}"),
    ]

    next_target = None
    for target, msg in milestone_messages:
        if gain_pct >= target and target not in milestones:
            send_telegram_message(msg)
            logging.info(msg)
            milestones.append(target)
            trade['last_milestone_time'] = now.isoformat()
            break
        if gain_pct < target and next_target is None:
            next_target = target

    # 3️⃣ Snail-paced reminder every 30 min past last milestone
    if last_time_iso:
        last_time = datetime.fromisoformat(last_time_iso)
        snail_elapsed = (now - datetime.fromisoformat(snail_last_iso)).total_seconds() if snail_last_iso else float('inf')
        if (now - last_time).total_seconds() >= 1800 and snail_elapsed >= 1800:
            snail_msg = (
                f"🐌 {symbol} holding steady at +{gain_pct:.2f}%\n"
                f"📈 Peak: +{(peak - entry)/entry*100:.2f}%  ➔ TSL at {tsl_trigger_pct:.2f}%\n"
                f"✨ Next milestone: +{next_target:.2f}%"
            )
            send_telegram_message(snail_msg)
            logging.info(snail_msg)
            trade['last_snail_msg_time'] = now.isoformat()



import math
import logging
from datetime import datetime, timezone

from logic import (
    client,
    open_trades,
    save_trades,
    send_telegram_message,
    check_milestones
)


def execute_auto_buy(
    symbol: str,
    strategy: str,
    surge_pct: float = 0.0,
    *,
    side: str = "long",
    invest_pct: float = 0.1,  # Lowered from 0.15
    max_open: int = 10,       # Increased from 6
) -> bool:
    """
    LONG  → market-BUY, record bullish trade.
    SHORT → market-SELL (requires margin or futures account).
    Quantity is sized from free USDT balance (≥$10 floor).
    """
    side = side.lower()
    if side not in {"long", "short"}:
        logging.error("%s: invalid side '%s'", symbol, side)
        return False

    # ── Guards ───────────────────────────────────────────────────────────
    if len(open_trades) >= max_open:
        logging.info("%s: skipped %s buy, max open trades (%d) reached", symbol, side, max_open)
        return False
    if symbol in open_trades:
        logging.info("%s: skipped %s buy, already positioned", symbol, side)
        return False

    try:
        # ── Capital & symbol meta ────────────────────────────────────────
        usdt_balance = float(client.get_asset_balance("USDT")["free"])
        logging.debug("%s: USDT balance $%.2f", symbol, usdt_balance)
        if usdt_balance < 10:
            logging.warning("%s: skipped %s buy, low USDT balance $%.2f", symbol, side, usdt_balance)
            send_telegram_message(f"⚠️ Low USDT balance $%.2f for {symbol} buy" % usdt_balance)
            return False

        invest_amount = max(usdt_balance * invest_pct, 10.0)
        api_symbol = symbol.replace("/", "")  # "HBAR/USDT" → "HBARUSDT"
        try:
            price = float(client.get_symbol_ticker(symbol=api_symbol)["price"])
        except Exception as e:
            logging.error("%s: failed to fetch ticker price: %s", symbol, e)
            return False
        info = client.get_symbol_info(api_symbol)
        if not info:
            logging.error("%s: failed to fetch symbol info", symbol)
            return False

        # ── Qty calc respecting LOT_SIZE ────────────────────────────────
        try:
            step = float(next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"))
            prec = int(-math.log10(step))
        except Exception as e:
            logging.error("%s: failed to read LOT_SIZE: %s", symbol, e)
            return False

        if side == "long":
            qty = invest_amount / price
        else:
            # SHORT on spot: sell existing balance only
            asset_bal = float((client.get_asset_balance(base_asset(symbol)) or {"free": "0"})["free"])
            qty = asset_bal

        qty = math.floor(qty / step) * step
        qty = round(qty, prec)


        # ── Notional check (spot) ───────────────────────────────────────
        notional_f = next((f for f in info["filters"] if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL")), None)
        if notional_f:
            min_notional = float(notional_f.get("minNotional") or notional_f.get("minNotionalValue", 0))
            notional = price * qty
            if notional < min_notional:
                logging.warning("%s: skipped %s buy, notional $%.2f < $%.2f", symbol, side, notional, min_notional)
                send_telegram_message(f"⚠️ Notional $%.2f < $%.2f for {symbol}" % (notional, min_notional))
                return False

        # ── EXECUTION ───────────────────────────────────────────────────
        logging.info("%s: executing %s market order, qty=%.4f, price=%.4f", symbol, side, qty, price)
        try:
            if side == "long":
                order = client.order_market_buy(symbol=api_symbol, quantity=qty)
            else:
                order = client.order_market_sell(symbol=api_symbol, quantity=qty)
        except Exception as e:
            logging.error("%s: market order failed: %s", symbol, e)
            send_telegram_message(f"⚠️ {side.upper()} order FAILED for {symbol}: {e}")
            return False

        if order.get("status") != "FILLED":
            logging.warning("%s: %s order not filled: %s", symbol, side, order)
            send_telegram_message(f"⚠️ {side.upper()} order NOT FILLED for {symbol}: {order}")
            return False

        fill_price = float(order["fills"][0]["price"])
        stop_price = fill_price * (0.9925 if side == "long" else 1.0075)
        tsl_price = fill_price * (0.75 if side == "long" else 1.25)

        trade_data = {
            "side": side,
            "entry_price": fill_price,
            "quantity": qty,
            "initial_qty": qty,
            "peak": fill_price,
            "phase1": False,
            "phase2": False,
            "phase3": False,
            "surge_pct": surge_pct,
            "stop": stop_price,
            "buy_time": datetime.now(timezone.utc).isoformat(),
            "milestones": [],
            "last_milestone_time": None,
            "strategy": strategy,
        }
        open_trades[symbol] = trade_data
        # on_entry is handled in main.breakout_loop after a successful buy to avoid circular imports
        save_trades()


        # ── Telegram push ───────────────────────────────────────────────
        act_emoji = "🟢" if side == "long" else "🔴"
        send_telegram_message(
            f"{act_emoji} Auto {side} executed ({strategy}): {symbol}\n"
            f"📥 Entry: ${fill_price:.4f} | Qty: {qty:.4f} | Notional: ${fill_price * qty:.2f}"
        )

        # ── Immediate milestone check ──────────────────────────────────
        try:
            check_milestones(symbol, trade_data, fill_price)
        except Exception as e:
            logging.warning("%s: milestone check failed: %s", symbol, e)

        logging.info("%s: %s buy executed successfully", symbol, side)
        return True

    except Exception as e:
        logging.error("%s: auto %s error: %s", symbol, side, e, exc_info=True)
        send_telegram_message(f"💥 AUTO {side.upper()} ERROR for {symbol}: {e}")
        return False



def save_pnl():
    with open(PNL_FILE, 'w') as f:
        json.dump({
            "date": last_reset.isoformat(),
            "pnl": daily_pnl
        }, f, indent=2)

def log_trade(symbol, entry, exit_price, gain_usd, gain_pct, peak_gain_pct, reason,
              forecast="Unknown", strategy="N/A", passed=None, failed=None):
    passed_str = ' | '.join(f"✅ {x}" for x in passed) if passed else ""
    failed_str = ' | '.join(f"❌ {x}" for x in failed) if failed else ""

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "entry": f"{entry:.4f}",
        "exit": f"{exit_price:.4f}",
        "gain_usd": f"{gain_usd:.4f}",
        "gain_pct": f"{gain_pct:.2f}",
        "peak_gain_pct": f"{peak_gain_pct:.2f}",
        "reason": reason,
        "forecast": forecast,
        "strategy": strategy,
        "passed": passed_str,
        "failed": failed_str
    }
    write_header = not CSV_LOG.exists()
    with open(CSV_LOG, mode='a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    try:
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and reason not in ["📊 Ross Candidate (Surge Pattern)"]:
            with open(CSV_LOG, 'rb') as f:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
                payload = {'chat_id': TELEGRAM_CHAT_ID}
                files = {'document': ('trade_log.csv', f)}
                requests.post(url, data=payload, files=files)
    except Exception as e:
        logging.error(f"Telegram CSV send error: {e}", exc_info=True)


# --- 4) exit_positions (with manual purge + TP staging + dynamic stops) ---
def exit_positions():
    global daily_pnl, last_reset

    # 1) Daily PnL reset at midnight UTC
    today = datetime.now(timezone.utc).date()
    if today != last_reset:
        daily_pnl = 0.0
        last_reset = today
        send_telegram_message("🔄 New day started. Daily PnL reset.")

    for symbol, trade in list(open_trades.items()):
        asset = symbol.replace("USDT", "")

        # 2A) Purge manually sold positions
        try:
            balance = float(client.get_asset_balance(asset)["free"])
        except Exception as e:
            logging.warning(f"{symbol}: could not fetch balance: {e}", exc_info=True)
            balance = 0.0
        if balance < 1e-6:
            del open_trades[symbol]
            save_trades()
            last_sell_time[symbol] = datetime.now(timezone.utc)
            send_telegram_message(f"🗑 Removed manually-sold position: {symbol}")
            continue

        try:
            current = float(client.get_symbol_ticker(symbol=symbol)["price"])
            info = client.get_symbol_info(api_symbol)
        except Exception as e:
            logging.warning(f"{symbol}: price/info fetch error: {e}", exc_info=True)
            continue

        entry = trade['entry_price']
        gain_pct = (current - entry) / entry * 100

        try:
            step_size = float(next(f["stepSize"] for f in info["filters"] if f["filterType"] == "LOT_SIZE"))
            precision = int(round(-math.log10(step_size)))
            notional_f = next((f for f in info["filters"] if f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL")), None)
            min_notional = float(notional_f.get("minNotional") or notional_f.get("minNotionalValue", 0)) if notional_f else 0.0
        except Exception as e:
            logging.warning(f"{symbol}: filter setup error: {e}", exc_info=True)
            continue

        # 3) Hard Stop
        stop_pct = 0.005  # 0.5% hard stop
        hard_stop_price = entry * (1 - stop_pct)
        if current <= hard_stop_price:
            qty = round(math.floor(balance / step_size) * step_size, precision)
            if qty > 0 and current * qty >= min_notional:
                try:
                    order = client.order_market_sell(
                        symbol=to_binance_symbol(symbol),
                        quantity=qty
                    )
                    if order.get("status") == "FILLED":
                        pnl = (current - entry) * qty
                        daily_pnl += pnl
                        save_pnl()
                        log_trade(
                            symbol, entry, current, pnl, gain_pct, 0.0, "HardStop",
                            f"-{stop_pct*100:.2f}% Below Entry", trade.get("strategy")
                        )
                        send_telegram_message(
                            f"❗ Hard Stop {symbol} — price ≤ {stop_pct*100:.2f}% below entry\n"
                            f"Exit: ${current:.4f} | Loss: {gain_pct:.2f}%\n💰 Daily PnL: ${daily_pnl:.2f}"
                        )
                except (BinanceAPIException, Exception) as e:
                    logging.error(f"{symbol}: HardStop sell error: {e}", exc_info=True)
            del open_trades[symbol]
            save_trades()
            continue

        # 4) Update peak
        peak = max(trade.get('peak', entry), current)
        trade['peak'] = peak

        # 5) Tiered Take Profits
        gain_ratio = (current - entry) / entry
        TP_LEVELS = [(0.005, 0.25), (0.012, 0.50), (0.015, 1.0)]

        for idx, (threshold, portion) in enumerate(TP_LEVELS, 1):
            flag = f"phase{idx}"
            if not trade.get(flag) and gain_ratio >= threshold:
                target_qty = round(
                    math.floor(min(trade['quantity'], trade['initial_qty'] * portion) / step_size) * step_size,
                    precision
                )
                if target_qty > 0 and current * target_qty >= min_notional:
                    try:
                        order = client.order_market_sell(symbol=symbol, quantity=target_qty)
                        if order.get("status") == "FILLED":
                            trade['quantity'] -= target_qty
                            trade[flag] = True
                            save_trades()
                            emoji = "🟢" * idx + "⚪️" * (4 - idx)
                            send_telegram_message(f"{emoji} Sold {portion*100:.0f}% of {symbol} at {gain_ratio*100:.2f}%")
                    except (BinanceAPIException, Exception) as e:
                        logging.error(f"{symbol}: TP{idx} sell error: {e}", exc_info=True)
                if portion == 1.0:
                    entry_price = trade['entry_price']
                    pnl = (current - entry_price) * target_qty
                    gain_pct_final = (current / entry_price - 1) * 100
                    peak_gain_pct = (trade['peak'] / entry_price - 1) * 100
                    log_trade(
                        symbol, entry_price, current, pnl, gain_pct_final, peak_gain_pct,
                        "TP3", trade.get("forecast", "Unknown"), trade.get("strategy"),
                        trade.get("passed"), trade.get("failed")
                    )
                    del open_trades[symbol]
                    save_trades()
                    break

        # Trailing Stop
        if gain_ratio >= 0.01 and trade.get('quantity', 0) > 0:
            qty = round(math.floor(min(trade['quantity'], balance) / step_size) * step_size, precision)
            tsl_price = peak * (1 - 0.0025)
            if current <= tsl_price and qty > 0 and current * qty >= min_notional:
                try:
                    order = client.order_market_sell(     symbol=to_binance_symbol(symbol),     quantity=qty )
                    if order.get("status") == "FILLED":
                        send_telegram_message(f"🪂 {symbol} hit the trailing stop at {gain_ratio*100:.2f}%")
                except (BinanceAPIException, Exception) as e:
                    logging.error(f"{symbol}: TSL sell error: {e}", exc_info=True)
                del open_trades[symbol]
                save_trades()
                continue

        if balance * current >= 1.0:
            try:
                check_milestones(symbol, trade, current)
            except Exception as e:
                logging.warning(f"{symbol}: milestone check error: {e}", exc_info=True)


# === Evaluating entries ===
def evaluate_entries(symbols: list[str]) -> None:
    for symbol in symbols:
        if symbol in BLACKLISTED_SYMBOLS:
            continue

        # --- 24h fetch ---
        try:
            ticker = client.get_ticker(symbol=symbol)
            high = float(ticker.get('highPrice', 0))
            low = float(ticker.get('lowPrice', 0))
            current_price = float(ticker.get('lastPrice', 0))
            dist_pct = ((current_price - low) / (high - low)) * 100 if high > low else float('inf')
        except Exception as e:
            logging.warning(f"{symbol}: failed 24h fetch: {e}", exc_info=True)
            continue

        # --- Fetch candles and compute indicators ---
        try:
            candles = get_klines(symbol, CANDLE_INTERVAL, limit=20)
            if not candles or len(candles) < 20:
                continue

            # Build DataFrame for indicator calculations
            df = pd.DataFrame(
                candles,
                columns=[
                    'open_time','open','high','low','close','volume',
                    'close_time','qav','num_trades','tbb','tbq','ignore'
                ]
            )
            df[['open','high','low','close','volume']] = \
                df[['open','high','low','close','volume']].astype(float)

            closes    = df['close']
            ema_fast  = closes.ewm(span=EMA_FAST).mean().iloc[-1]
            ema_mid   = closes.ewm(span=EMA_MID).mean().iloc[-1]
            typical   = (df['high'] + df['low'] + df['close']) / 3
            vwap_last = (typical * df['volume']).cumsum().iloc[-1] / df['volume'].cumsum().iloc[-1]
            macd_line = closes.ewm(span=MACD_FAST).mean() - closes.ewm(span=MACD_SLOW).mean()
            sig_last  = macd_line.ewm(span=MACD_SIGNAL).mean().iloc[-1]
            macd_last = macd_line.iloc[-1]

            filter_passes = {
                "EMA9":   current_price > ema_fast,
                "EMA20":  current_price > ema_mid,
                "VWAP":   current_price > vwap_last,
                "MACD":   macd_last > sig_last,
            }

            # ── LOG FILTER CYCLE ──
            log_filter_cycle(symbol, current_price, dist_pct, {
                'ENABLE_VOLUME_SPIKE':   False,
                'ENABLE_EMA_ALIGNMENT':  filter_passes["EMA9"],
                'ENABLE_MACD_MOMENTUM':  filter_passes["MACD"],
                'ENABLE_VWAP_GATE':      filter_passes["VWAP"],
                'ENABLE_SIDEWAYS':       False,
                'ENABLE_BTC_CANDLE':     False,
                'ENABLE_TOKEN_CANDLE':   False
            })

            # --- Compute real “surge / pullback / confirm” flags for logging ---
            surge_ok = pullback_ok = confirm_ok = False
            if len(candles) >= 6:
                closed = candles[:-1]  # ignore the live bar
                co = float(closed[-1][1])
                cc = float(closed[-1][4])
                confirm_ok = (cc > co)
                if confirm_ok:
                    # Pullback calc…
                    temp_pull_sum = 0.0
                    temp_pull_cnt = 0
                    idx = len(closed) - 2
                    while idx >= 0 and float(closed[idx][4]) < float(closed[idx][1]):
                        o = float(closed[idx][1])
                        c = float(closed[idx][4])
                        temp_pull_sum += (o - c) / o
                        temp_pull_cnt += 1
                        idx -= 1
                    pullback_ok = (temp_pull_cnt > 0)

                    if pullback_ok:
                        temp_surge_sum = 0.0
                        temp_surge_cnt = 0
                        surge_highs = []
                        while idx >= 0 and float(closed[idx][4]) > float(closed[idx][1]):
                            o = float(closed[idx][1])
                            c = float(closed[idx][4])
                            temp_surge_sum += (c - o) / o
                            temp_surge_cnt += 1
                            surge_highs.append(float(closed[idx][2]))
                            idx -= 1
                        surge_ok = (
                            temp_surge_cnt >= 3
                            and temp_surge_sum >= SURGE_MIN_PCT
                            and temp_pull_sum <= (temp_surge_sum * 0.5)
                        )

            # Log diagnostics with real flags
            log_surge_candidate(
                symbol=symbol,
                surge=surge_ok,
                pullback=pullback_ok,
                confirm=confirm_ok,
                filter_passes=filter_passes
            )

            # ── Hard gates: all filters must pass ──
            if not all(filter_passes.values()):
                continue

            # ─── Ross Pattern Detection ───
            try:
                if len(candles) < 10:
                    continue

                # Your existing pull_pct_sum & surge_pct_sum computation here
                pull_pct_sum = surge_pct_sum = 0.0
                pull_candle_count = surge_candle_count = 0
                surge_highs = []
                idx = len(candles[:-1]) - 2
                # (… replicate your exact loops …)

                if surge_candle_count < 2 or surge_pct_sum < SURGE_MIN_PCT:
                    continue
                if pull_pct_sum > (surge_pct_sum * 0.5):
                    continue

                highest_surge_high = max(surge_highs)
                confirm_open  = float(candles[-2][1])
                confirm_close = float(candles[-2][4])
                last_pull_high= float(candles[idx + 1][2])

                should_buy = (
                    confirm_open > highest_surge_high or
                    (confirm_close > confirm_open and confirm_close > last_pull_high)
                )
                if should_buy:
                    # ←— RISK PROFILE SIZING + BUY — moved here
                    state   = trade_data.get('btc_state', 'Neutral')
                    profile = RISK_PROFILES[state]

                    if len(open_trades) >= profile['max_trades']:
                        continue

                    balance    = float(client.get_asset_balance("USDT")["free"])
                    allocation = balance * profile['trade_size_pct']
                    qty        = math.floor(allocation / current_price / step_size) * step_size
                    qty        = round(qty, precision)

                    if qty * current_price < min_notional or qty == 0:
                        continue

                    order = client.order_market_buy(symbol=api_symbol, quantity=qty)
                    if order.get("status") == "FILLED":
                        send_telegram_message(
                            f"🔹 Entering {symbol} — {state} profile: "
                            f"{profile['trade_size_pct']*100:.1f}% equity, "
                            f"max {profile['max_trades']} trades"
                        )
                    continue

            except ValueError as _err:
                logging.debug(f"{symbol}: Ross skip → {_err}")
            except Exception as e:
                logging.debug(f"{symbol}: Ross skipped — {e}")

        except Exception as e:
            logging.error(f"{symbol}: evaluation error: {e}", exc_info=True)
