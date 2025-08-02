"""main.py — Unified trendline bot (clean imports)

Two concurrent coroutines:
• trendline_loop   – visual analytics
• breakout_loop    – trades + milestones + exits

Edit MONITOR_TF for desired candle size.
"""
from __future__ import annotations
import datetime


import asyncio 
import logging
from typing import Any, Dict, Optional, Set
import traceback
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

try:
    import ccxt.async_support as ccxt
except ModuleNotFoundError:  # pragma: no cover
    raise ModuleNotFoundError("Install with: pip install 'ccxt>=4,<5'")

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("ccxt.base.exchange").setLevel(logging.WARNING)  # mute ccxt noise



# ───── Internal modules ──────────────────────────────────────────────────
from logic import (
    execute_auto_buy,
    update_market_sell,   # ← unified sell helper
    send_telegram_message,
    check_milestones,
    open_trades,
    detect_manual_buys,
    save_trades,          # ← needed in breakout_loop cleanup & exits

)

from trendlines import (
    _analyse_symbol,
    SYMBOLS,
    OPEN_SYMBOLS,
    SLEEP_SEC,
    _select_line,          
    _two_candle_breakout,
    _count_touchpoints,
    PIVOT_W_DEFAULT,
    CANDLE_LIMIT,          # ← bring the constant into main.py
    fetch_top_gainers,
    on_entry,
    on_exit,
)


# ───── Configuration ─────────────────────────────────────────────────────
PIVOT_W_OVERRIDE = {"1m": 3}   # 3-bar half-width ⇒ 7-bar pivots
EXCLUDE_RECENT  = 1   # ignore last 3 bars
TOL_PCT_OVERRIDE = 0.0
ANALYSIS_TF = "5m"          # structural timeframe
MONITOR_TF  = "5m"          # execution timeframe


_TF_SEC = lambda tf: int(tf[:-1]) * {"m": 60, "h": 3600, "d": 86_400}[tf[-1]]

# ───── Helper functions ──────────────────────────────────────────────────
async def _fetch_df(ex, symbol: str, tf: str, limit: int = 300) -> pd.DataFrame:
    ohlcv = await ex.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df


def _dbg_breakout(df, line, direction):
    broke = _two_candle_breakout(df, line, direction)
    if DEBUG_DETECT:
        idx_prev, idx_curr = len(df)-2, len(df)-1
        body_prev  = df.close.iat[idx_prev]
        body_curr  = df.close.iat[idx_curr]
        wick_curr  = df.high.iat[idx_curr] if direction=="up" else df.low.iat[idx_curr]
        y_prev = _trendline_y(idx_prev, line["priceA"], line["idxA"], line["slope"])
        y_curr = _trendline_y(idx_curr, line["priceA"], line["idxA"], line["slope"])
        delta  = (wick_curr - y_curr) / y_curr if direction=="up" else (y_curr - wick_curr) / y_curr
        side   = "↑" if direction=="up" else "↓"
        print(f"[BRK?] {df.symbol.iat[0]} {side} | "
              f"prevBody={body_prev:.4f} y_prev={y_prev:.4f} | "
              f"wick={wick_curr:.4f} y_curr={y_curr:.4f} | "
              f"delta={delta:.4%} need>{BREAK_PCT:.4%} ⇒ {broke}")
    return broke


# --------------------------------------------------------------------------
# Put this near your other globals
DEBUG_DETECT: bool = True        # flip to False to silence debug prints
# --------------------------------------------------------------------------

async def _detect_breakout(ex, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Scan one symbol on MONITOR_TF (5m) using rays from all timeframes.
    Returns a breakout dict if any ray breaks out on 5m candles, or None.
    """
    stage = "start"
    try:
        # Fetch 5m candles for breakout detection
        df_5m = await _fetch_df(ex, symbol, MONITOR_TF, limit=250)
        df_5m = df_5m.iloc[:-1].copy()  # use only fully CLOSED bars; ccxt last row is live
        stage = "fetch_5m"
    except Exception as e:
        if DEBUG_DETECT:
            print(f"[ERR] {symbol} | fetch_df failed: {e}")
            traceback.print_exc()
        return None

    if df_5m is None or len(df_5m) < 50:
        if DEBUG_DETECT:
            print(f"[SKIP] {symbol} | df_5m len {len(df_5m) if df_5m is not None else 'None'} (<50)")
        return None

    # Fetch all rays from all timeframes
    try:
        all_lines = await _analyse_symbol(
            ex,
            symbol,
            analysis_tf=ANALYSIS_TF,
            pivot_w=PIVOT_W_OVERRIDE.get(ANALYSIS_TF, PIVOT_W_DEFAULT),
            tol_pct=TOL_PCT_OVERRIDE,
            force_exclude_recent=EXCLUDE_RECENT
        )
        stage = "fetch_lines"
    except Exception as e:
        if DEBUG_DETECT:
            print(f"[ERR] {symbol} | _analyse_symbol failed: {e}")
            traceback.print_exc()
        return None

    if not all_lines:
        if DEBUG_DETECT:
            print(f"[SKIP] {symbol} | no valid lines from _analyse_symbol")
        return None

    # Check breakouts for each ray on 5m candles
    for direction in ("up", "down"):
        lines = all_lines.get(direction, [])
        if not lines:
            if DEBUG_DETECT:
                print(f"[DBG] {symbol} | {direction} | no lines available")
            continue

        for line in lines:
            tf = line.get("tf", "unknown")
            # Project ray onto 5m timeframe (time-aware; no exact TS match required)
            A_src = line["A_ts"] if isinstance(line["A_ts"], pd.Timestamp) else pd.Timestamp(line["A_ts"], tz="UTC")
            B_src = line["B_ts"] if isinstance(line["B_ts"], pd.Timestamp) else pd.Timestamp(line["B_ts"], tz="UTC")
            dt_sec = (B_src - A_src).total_seconds()
            if dt_sec == 0:
                if DEBUG_DETECT:
                    print(f"[DBG] {symbol} | {direction} | tf={tf} | invalid line (A_ts==B_ts)")
                continue

            # Slope per second from source TF
            slope_per_sec = (line["priceB"] - line["priceA"]) / dt_sec

            # Choose an anchor inside the 5m window, nearest to A_src (or start of df)
            start_ts = df_5m.index[0]
            end_ts   = df_5m.index[-1]
            anchor_ts = A_src
            if anchor_ts < start_ts or anchor_ts > end_ts:
                anchor_ts = start_ts

            # Nearest 5m index to anchor_ts
            pos = df_5m.index.searchsorted(anchor_ts)
            if pos == len(df_5m):
                if DEBUG_DETECT:
                    print(f"[DBG] {symbol} | {direction} | tf={tf} | anchor beyond 5m window")
                continue
            if pos > 0 and (anchor_ts - df_5m.index[pos - 1]) <= (df_5m.index[pos] - anchor_ts):
                idxA = pos - 1
            else:
                idxA = pos
            if idxA >= len(df_5m) - 1:
                if DEBUG_DETECT:
                    print(f"[DBG] {symbol} | {direction} | tf={tf} | anchor too close to window end")
                continue

            A_proj_ts = df_5m.index[idxA]
            next_ts   = df_5m.index[idxA + 1]

            # Price at A_proj_ts using time slope
            priceA_proj = line["priceA"] + slope_per_sec * (A_proj_ts - A_src).total_seconds()
            # Slope per 5m index step
            step_sec = (next_ts - A_proj_ts).total_seconds()
            slope_idx = slope_per_sec * step_sec

            projected_line = {
                "A_ts": A_proj_ts,
                "B_ts": next_ts,
                "priceA": priceA_proj,
                "priceB": priceA_proj + slope_idx,
                "idxA": idxA,
                "idxB": idxA + 1,
                "slope": slope_idx,
            }


            # ---- Touches on SOURCE TF (strict quality), breakout on CLOSED 5m (execution) ----
            # 1) Source TF touches (A/B timestamps are native to 'tf')
            try:
                # Fetch a CLOSED-bar window that MUST include A and B on the source TF
                tf_sec = {"m": 60, "h": 3600, "d": 86400}[tf[-1]] * int(tf[:-1])
                A_ts = line["A_ts"] if isinstance(line["A_ts"], pd.Timestamp) else pd.Timestamp(line["A_ts"], tz="UTC")
                pad_bars = 5
                since_ms = int((A_ts.tz_convert("UTC") if A_ts.tzinfo else A_ts.tz_localize("UTC")).timestamp() * 1000) - pad_bars * tf_sec * 1000
                since_ms = max(0, since_ms)

                ohlc_src = await ex.fetch_ohlcv(symbol, timeframe=tf, since=since_ms, limit=CANDLE_LIMIT)
                df_src = pd.DataFrame(ohlc_src, columns=["ts", "open", "high", "low", "close", "volume"])
                df_src["ts"] = pd.to_datetime(df_src["ts"], unit="ms", utc=True)
                df_src.set_index("ts", inplace=True)
                df_src = df_src.iloc[:-1].copy()  # CLOSED bars only

                if line["A_ts"] not in df_src.index or line["B_ts"] not in df_src.index:
                    if DEBUG_DETECT:
                        print(f"[DBG] {symbol} | {direction} | tf={tf} | A/B not in src df after refill (skipping)")
                    continue

                idxA_src = df_src.index.get_loc(line["A_ts"])
                idxB_src = df_src.index.get_loc(line["B_ts"])

                if idxB_src <= idxA_src:
                    if DEBUG_DETECT:
                        print(f"[DBG] {symbol} | {direction} | tf={tf} | bad indices A>=B")
                    continue

                # Rebuild the line in source space for accurate touch counting
                line_src = {
                    "A_ts":   line["A_ts"],
                    "B_ts":   line["B_ts"],
                    "priceA": line["priceA"],
                    "priceB": line["priceB"],
                    "idxA":   idxA_src,
                    "idxB":   idxB_src,
                    "slope":  (line["priceB"] - line["priceA"]) / (idxB_src - idxA_src),
                }
                touches_src = _count_touchpoints(
                    df_src, line_src, direction=direction, exclude_last=EXCLUDE_RECENT, start_from_A=True
                )
            except Exception as e:
                if DEBUG_DETECT:
                    print(f"[ERR] {symbol} | {direction} | tf={tf} | src touches error: {e}")
                continue

            # 2) 5m closed-bar breakout (execution trigger)
            broke = _two_candle_breakout(df_5m, projected_line, direction)

            # Optional: 5m touches for info only
            touches_5m = _count_touchpoints(
                df_5m, projected_line, direction=direction, exclude_last=EXCLUDE_RECENT
            )

            if DEBUG_DETECT:
                print(
                    f"[DBG] {symbol} | {direction} | tf={tf} | "
                    f"src_touches={touches_src} (need ≥3) | 5m_touches={touches_5m} (info) | breakout={broke}"
                )

            # Entry gate: STRICT — require ≥3 touches on the source TF + closed-bar breakout on 5m
            if broke and touches_src >= 3:
                if DEBUG_DETECT:
                    print(f"[ENTRY] {symbol} | {direction.upper()} breakout ⚡️ on tf={tf}")
                return {
                    "side": "long" if direction == "down" else "short",
                    "entry_price": df_5m.close.iat[-1],  # closed 5m price
                    "entry_line": projected_line,
                    "timeframe": tf,
                }
            else:
                if DEBUG_DETECT:
                    reason = "no breakout" if not broke else "insufficient touches (source TF)"
                    print(f"[NO-ENTRY] {symbol} | {direction} | tf={tf} | {reason}")



async def _crossed_opposite(ex, symbol: str, trade: Dict[str, Any], df: pd.DataFrame) -> bool:
    """
    Check if price broke the steepest clean opposite trendline on CLOSED 5m candles.
    Projects the line onto df's index before breakout check.
    """
    # Use only CLOSED bars (ccxt returns the live bar as last row)
    df = df.iloc[:-1].copy()
    if len(df) < 5:
        return False

    side = trade["side"]
    direction = "down" if side == "long" else "up"

    # Get both directions, then pick the correct exit line:
    lines = await _analyse_symbol(
        ex,
        symbol,
        pivot_w=PIVOT_W_OVERRIDE.get(MONITOR_TF, PIVOT_W_DEFAULT),
        tol_pct=TOL_PCT_OVERRIDE,
        force_exclude_recent=EXCLUDE_RECENT,
        side=None  # ← fetch both, we'll pick
    )
    if not lines:
        return False

    exit_dir = "up" if side == "long" else "down"   # LONG exits watch support; SHORT exits watch resistance
    candidates = lines.get(exit_dir, [])
    if not candidates:
        return False
    opp_line = candidates[0]  # single steepest clean line per our analyser


    # Time-aware projection of the opposite line onto 5m index
    A_src = opp_line["A_ts"] if isinstance(opp_line["A_ts"], pd.Timestamp) else pd.Timestamp(opp_line["A_ts"], tz="UTC")
    B_src = opp_line["B_ts"] if isinstance(opp_line["B_ts"], pd.Timestamp) else pd.Timestamp(opp_line["B_ts"], tz="UTC")
    dt_sec = (B_src - A_src).total_seconds()
    if dt_sec <= 0:
        return False

    slope_per_sec = (opp_line["priceB"] - opp_line["priceA"]) / dt_sec

    start_ts = df.index[0]
    end_ts   = df.index[-1]
    anchor_ts = A_src if (start_ts <= A_src <= end_ts) else start_ts

    pos = df.index.searchsorted(anchor_ts)
    if pos == len(df):
        return False
    idxA = pos - 1 if pos > 0 and (anchor_ts - df.index[pos - 1]) <= (df.index[pos] - anchor_ts) else pos
    if idxA >= len(df) - 1:
        return False

    A_proj_ts = df.index[idxA]
    next_ts   = df.index[idxA + 1]
    priceA_proj = opp_line["priceA"] + slope_per_sec * (A_proj_ts - A_src).total_seconds()
    step_sec = (next_ts - A_proj_ts).total_seconds()
    slope_idx = slope_per_sec * step_sec

    projected_line = {
        "A_ts": A_proj_ts,
        "B_ts": next_ts,
        "priceA": priceA_proj,
        "priceB": priceA_proj + slope_idx,
        "idxA": idxA,
        "idxB": idxA + 1,
        "slope": slope_idx,
    }

    # Closed-bar exit check: sign matters
    cross_dir = _two_candle_breakout(df, projected_line, exit_dir)

    if DEBUG_DETECT:
        print(f"[EXIT-CHK] {symbol} | exit_dir={exit_dir} | cross_dir={cross_dir}")

    # LONG → exit only on bearish break of support (cross_dir == -1 on 'up' line)
    # SHORT → exit only on bullish break of resistance (cross_dir == +1 on 'down' line)
    if side == "long":
        return cross_dir == -1
    else:
        return cross_dir == +1


_open: Dict[str, Dict[str, Any]] = {}

# ───── Coroutines ────────────────────────────────────────────────────────
async def trendline_loop():
    ex = ccxt.binance()
    logging.info("Trendline monitor started …")
    try:
        # Initialize SYMBOLS on first run
        global SYMBOLS
        if not SYMBOLS:  # Only fetch if SYMBOLS is empty
            SYMBOLS = await fetch_top_gainers(ex)
            if not SYMBOLS:
                logging.warning("No top gainers fetched, using fallback symbols")
                SYMBOLS = ["BTC/USDT", "ETH/USDT"]  # Fallback to avoid empty list
        while True:
            # Update SYMBOLS with top 30 gainers at the start of each cycle
            SYMBOLS = await fetch_top_gainers(ex)
            if not SYMBOLS:
                logging.warning("No top gainers fetched, skipping cycle")
                await asyncio.sleep(SLEEP_SEC)
                continue
            # ① pick the right pivot width for the active timeframe
            pivot_w = PIVOT_W_OVERRIDE.get(MONITOR_TF, PIVOT_W_DEFAULT)

            for symbol in SYMBOLS:
                try:
                    # ② pass it (and any other overrides) into the worker
                    await _analyse_symbol(
                        ex,
                        symbol,
                        analysis_tf=ANALYSIS_TF,
                        pivot_w=PIVOT_W_OVERRIDE.get(ANALYSIS_TF, PIVOT_W_DEFAULT),
                        tol_pct=TOL_PCT_OVERRIDE,
                        force_exclude_recent=None,   # or set an int to override mapping
                    )
                except Exception:
                    print(f"\n❌ HARD ERROR in _analyse_symbol for {symbol}")
                    traceback.print_exc()

            await asyncio.sleep(SLEEP_SEC)
    finally:
        await ex.close()




async def breakout_loop():
    ex = ccxt.binance()
    logging.info("Breakout loop running on %s candles", MONITOR_TF)
    try:
        while True:
            logging.info("Current state: _open=%s, open_trades=%s, OPEN_SYMBOLS=%s",
                         list(_open.keys()), list(open_trades.keys()), list(OPEN_SYMBOLS))
            # ── CLEANUP STALE ENTRIES --------------------------------------------
            for symbol in list(_open.keys()):
                if symbol not in open_trades:
                    logging.info("%s: removing stale entry from _open", symbol)
                    _open.pop(symbol, None)
                    on_exit(symbol)
            for symbol in list(open_trades.keys()):
                try:
                    asset = symbol.replace("USDT", "")
                    balance = float(client.get_asset_balance(asset)["free"])
                    if balance < 1e-6:
                        logging.info("%s: removing closed position from open_trades", symbol)
                        del open_trades[symbol]
                        on_exit(symbol)
                        _open.pop(symbol, None)
                        save_trades()
                except Exception as e:
                    logging.warning("%s: failed to check balance for cleanup: %s", symbol, e)

            # ── ENTRY ----------------------------------------------------------------
            detection_tasks = [_detect_breakout(ex, s) for s in SYMBOLS]
            for symbol, outcome in zip(
                SYMBOLS, await asyncio.gather(*detection_tasks, return_exceptions=True)
            ):
                if isinstance(outcome, Exception):
                    logging.exception("%s detection error: %s", symbol, outcome)
                    continue
                if not outcome:
                    logging.debug("%s: no breakout detected", symbol)
                    continue
                if symbol in _open:
                    logging.info("%s: skipped buy, already in _open", symbol)
                    continue
                if symbol in open_trades:
                    logging.info("%s: skipped buy, already in open_trades", symbol)
                    continue
                side = outcome["side"]
                price = outcome["entry_price"]
                logging.info("%s: attempting buy for %s breakout @ %.4f", symbol, side.upper(), price)
                try:
                    success = execute_auto_buy(
                        symbol, f"Breakout{MONITOR_TF}", price, side=side, max_open=10, invest_pct=0.1
                    )
                    if success:
                        on_entry(symbol)  # ensure persistence/monitoring list
                        _open[symbol] = outcome
                        send_telegram_message(
                            f"⚡️ {side.upper()} breakout {symbol} @ {price:.4f}"
                        )
                        logging.info("%s: buy executed successfully", symbol)

                    else:
                        logging.warning("%s: buy failed in execute_auto_buy", symbol)
                except Exception as e:
                    logging.exception("%s: entry failed due to exception: %s", symbol, e)

            # ── EXIT + MILESTONE CHECKS --------------------------------------------
            for symbol in OPEN_SYMBOLS:
                if symbol not in open_trades:
                    logging.warning("%s: removing from OPEN_SYMBOLS, not in open_trades", symbol)
                    on_exit(symbol)
                    _open.pop(symbol, None)
                    continue

                try:
                    df = await _fetch_df(ex, symbol, MONITOR_TF, limit=150)
                    df_c = df.iloc[:-1] if len(df) > 1 else df
                    current = df_c.close.iat[-1]

                    # Milestone checks
                    try:
                        check_milestones(symbol, open_trades[symbol], current)
                    except Exception as e:
                        logging.warning("%s: milestone error: %s", symbol, e)

                    # Exit on opposite trendline cross
                    if await _crossed_opposite(ex, symbol, open_trades[symbol], df):
                        update_market_sell(symbol, open_trades[symbol])
                        send_telegram_message(
                            f"🔴 Exit {symbol} – crossed opposite trendline"
                        )
                        on_exit(symbol)
                        _open.pop(symbol, None)
                        del open_trades[symbol]
                        save_trades()
                        logging.info("%s: removed from open_trades after exit", symbol)

                except Exception as e:
                    logging.exception("%s: exit loop error: %s", symbol, e)

            await asyncio.sleep(_TF_SEC(MONITOR_TF))
    finally:
        await ex.close()




# ───── Entrypoint ────────────────────────────────────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S", handlers=[logging.StreamHandler()], force=True)
    # No existing line
    logging.Formatter.converter = lambda *args: (datetime.datetime.now() + datetime.timedelta(hours=2)).timetuple()
    send_telegram_message("🟢 Bot live — breakout engine ON …")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s    %(message)s", datefmt="%Y-%m-%d %H:%M:%S,%f")

    await asyncio.gather(trendline_loop(), breakout_loop())


if __name__ == "__main__":
    asyncio.run(main())
