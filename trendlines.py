#trendline.py
import asyncio
from typing import List, Dict, Optional, Any, Set, Tuple

import pandas as pd
try:
    import ccxt.async_support as ccxt
except ModuleNotFoundError:
    raise ModuleNotFoundError("Install with: pip install 'ccxt>=4,<5'")
import ccxt.async_support as ccxt
import asyncio
import logging  # Added logging import



async def fetch_top_gainers(exchange, limit=20):
    """Fetch top 30 gainers by 24h price change percentage for USDT pairs."""
    try:
        await exchange.load_markets()
        tickers = await exchange.fetch_tickers()
        # Filter for USDT pairs and sort by percentage change
        usdt_pairs = [
            (symbol, data) for symbol, data in tickers.items()
            if symbol.endswith('/USDT') and 'percentage' in data and data['percentage'] is not None
        ]
        # Sort by percentage change (descending) and take top 30
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x[1]['percentage'], reverse=True)[:limit]
        return [symbol for symbol, _ in sorted_pairs]
    except Exception as e:
        print(f"Error fetching top gainers: {e}")
        return []
    finally:
        await exchange.close()

# Initialize dynamic watchlists
SYMBOLS: List[str] = []          # rotating top gainers
OPEN_SYMBOLS: Set[str] = set()  # symbols with open positions (persist monitoring)
LAST_SELECTED: Dict[str, Dict[str, Any]] = {}  # per-symbol lowest viable lines

# Run async function to populate SYMBOLS at startup
async def initialize_symbols():
    global SYMBOLS
    exchange = ccxt.binance()
    SYMBOLS = await fetch_top_gainers(exchange)

def get_monitored_symbols() -> List[str]:
    """Union of current top gainers and any symbols we currently hold positions in."""
    return sorted(set(SYMBOLS) | set(OPEN_SYMBOLS))

def on_entry(symbol: str):
    """Call when a position opens to ensure continued monitoring & exit‑line persistence."""
    OPEN_SYMBOLS.add(symbol)

def on_exit(symbol: str):
    """Call when a position closes to optionally stop persistence."""
    OPEN_SYMBOLS.discard(symbol)
    # Optionally clear persisted lines:
    # LAST_SELECTED.pop(symbol, None)

# Execute the async initialization
loop = asyncio.get_event_loop()
loop.run_until_complete(initialize_symbols())


TIMEFRAMES: List[str] = ["4h", "1h", "15m", "5m"]  # high ➜ low
ENABLED_TFS: Dict[str, bool] = {tf: True for tf in TIMEFRAMES}
CANDLE_LIMIT: int = 600          # ensure low‑TF slice still contains parent‑B
SLEEP_SEC: int = 10
MAX_VIOLATIONS_DEFAULT: int = 0  # ← zero tolerance for perfect cleanliness

# === CONFIG ================================================================


# ——— Default trend‑line parameters (override per TF) ———
USE_ABSOLUTE_PIVOTS_DEFAULT: bool = False
PIVOT_W_DEFAULT: int = max(3, CANDLE_LIMIT // 30)  # half‑width for fractals
TOL_PCT_DEFAULT: float = 0.0  # No wiggle room
TOUCH_TOL_PCT: float = 0.002   # 0.1 % – applies ONLY to touch‑count logic
BREAK_PCT: float = 0.002         # 0.2 % buffer on breakout test
EXCLUDE_RECENT_MAP = {"4h": 0, "1h": 0, "15m": 1, "5m": 3}
MAX_CHAIN_PER_TF   = 10  # how many lines we’ll try to chain per TF (newest→older)

# Limit how far back anchors can be chosen per‑TF (bars). Tune to taste.
A_LOOKBACK_BARS: Dict[str, int] = {"4h": 200, "1h": 400, "15m": 800, "5m": 300}
B_LOOKBACK_BARS: Dict[str, int] = {"4h": 200, "1h": 400, "15m": 800, "5m": 300}

# ——— Pretty labels for logs ———
TF_LABEL: Dict[str, str] = {
    "4h": "4h",
    "1h": "1h",
    "15m": "15m",
    "5m": "5m",
}

# ==========================================================================
#                             Low‑level helpers
# ==========================================================================

# ==========================================================================
#                               Logging
# ==========================================================================

def _log_line(sym: str, tf: str, direction: str, line: Dict, touches: int, broke: bool):
    """Print ONE clean A→B ray, highest TF → lowest TF."""
    base = sym.split("/")[0]
    emo = "📉" if direction == "down" else "📈"
    a_str = line['A_ts'].strftime('%Y-%m-%d %H:%M')
    b_str = line['B_ts'].strftime('%Y-%m-%d %H:%M')
    status = "Breakout ✅" if broke else "Breakout ❌"
    touch_emoji = "✅" if touches >= 3 else "❌"
    print(f"{emo} {base} [{tf}] {a_str} - {b_str} | {touches} touches {touch_emoji} | {status}")
    
# ── Helper: how many times has a ray been tested? ─────────────────────────────
def _count_touchpoints(
    df: pd.DataFrame,
    line: Dict,
    *,
    direction: str,
    exclude_last: int = 0,
    tol_pct: float = TOUCH_TOL_PCT,
    start_from_A: bool = False,
) -> int:
    """
    Return the number of candles that *touch* the given trend‑ray without closing
    through it (±tol_pct buffer).
    """
    if line is None or len(df) == 0:
        return 0

    last_allowed = len(df) - exclude_last
    idxA = line["idxA"]
    touches = 0

    start = idxA if start_from_A else idxA + 1
    for idx in range(start, last_allowed):
    
        y = _trendline_y(idx, line["priceA"], idxA, line["slope"])

        if direction == "up":                              # support line
            test_price = min(df.low.iat[idx], df.open.iat[idx], df.close.iat[idx])
            if abs(test_price - y) / y <= tol_pct and df.close.iat[idx] >= y:
                touches += 1
        else:                                              # resistance
            test_price = max(df.high.iat[idx], df.open.iat[idx], df.close.iat[idx])
            if abs(test_price - y) / y <= tol_pct and df.close.iat[idx] <= y:
                touches += 1

    return touches



def _pivots(df: pd.DataFrame, col: str, window: int) -> pd.DataFrame:
    """Return fractal pivots using half‑width *window*."""
    s = df[col]
    if col == "high":
        cond = (s.shift(window) < s) & (s.shift(-window) < s)
        for i in range(1, window):
            cond &= (s.shift(i) < s) & (s.shift(-i) < s)
    else:
        cond = (s.shift(window) > s) & (s.shift(-window) > s)
        for i in range(1, window):
            cond &= (s.shift(i) > s) & (s.shift(-i) > s)
    return df[cond]


def _trendline_y(idx: int, priceA: float, idxA: int, slope: float) -> float:
    return priceA + slope * (idx - idxA)


def _two_candle_breakout(df: pd.DataFrame, line: Dict, direction: str) -> int:
    n = len(df)
    if n < 2:
        logging.debug("%s: no breakout, insufficient candles (%d < 2)", df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown')
        return 0

    idx_prev, idx_curr = n - 2, n - 1
    close_prev = df.close.iat[idx_prev]
    close_curr = df.close.iat[idx_curr]
    high_curr = df.high.iat[idx_curr]
    low_curr = df.low.iat[idx_curr]

    y_prev = _trendline_y(idx_prev, line["priceA"], line["idxA"], line["slope"])
    y_curr = _trendline_y(idx_curr, line["priceA"], line["idxA"], line["slope"])

    TOLERANCE = 0.001  # 0.1% tolerance
    logging.debug(
        "%s [%s] Breakout check: close_prev=%.4f, y_prev=%.4f, close_curr=%.4f, y_curr=%.4f, high_curr=%.4f, low_curr=%.4f",
        df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown', direction,
        close_prev, y_prev, close_curr, y_curr, high_curr, low_curr
    )

    if direction == "up":
        if close_prev > y_prev and close_curr < y_curr * (1 - TOLERANCE) and low_curr < y_curr:
            logging.debug("%s: Bearish breakout detected (uptrend)", df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown')
            return -1
        if close_prev < y_prev and close_curr > y_curr * (1 + TOLERANCE) and high_curr > y_curr:
            logging.debug("%s: Bullish breakout detected (uptrend)", df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown')
            return +1
    else:  # direction == "down"
        if close_prev < y_prev and close_curr > y_curr * (1 + TOLERANCE) and high_curr > y_curr:
            logging.debug("%s: Bullish breakout detected (downtrend)", df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown')
            return +1
        if close_prev > y_prev and close_curr < y_curr * (1 - TOLERANCE) and low_curr < y_curr:
            logging.debug("%s: Bearish breakout detected (downtrend)", df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown')
            return -1

    logging.debug("%s: No breakout detected", df.symbol.iat[0] if hasattr(df, 'symbol') else 'Unknown')
    return 0

#def _two_candle_breakout(df, line, direction):
#    """
#    TEST STUB: always report a breakout so you can confirm buys fire.
#    """
#    return True

#── TEST OVERRIDE: force every breakout to pass ──

# ==========================================================================
#                         Core line-selection logic
# ==========================================================================

DEBUG_GEOM: bool = False  # flip off once happy

def _select_line(
    df: pd.DataFrame,
    direction: str,
    pivot_w: int,
    tol_pct: float,
    use_absolute_pivots: bool,
    seed_A_ts: Optional[pd.Timestamp] = None,
    exclude_recent: int = 1,
    snap_window: int = 3,
    *,
    strict_absolute: bool = False,       # ← NEW: enable “A absolute, B next pivot, no pierce”
    a_max_lookback: Optional[int] = None,
    b_max_lookback: Optional[int] = None,
) -> Optional[Dict]:


    sym = getattr(df, "symbol", [None])[0] or "?"
    cmp_col          = "low" if direction == "up" else "high"

    # Last bar allowed (we build geometry on CLOSED bars only outside)
    last_allowed_idx = len(df) - exclude_recent - 1
    if last_allowed_idx <= 0:
        return None

    # ---------- STRICT 4h PATH ----------
    if strict_absolute:
        # 1) A = absolute extreme inside last a_max_lookback (default 600)
        if a_max_lookback is None:
            a_max_lookback = 600
        a_start = max(0, (last_allowed_idx + 1) - int(a_max_lookback))
        a_slice = df.iloc[a_start : last_allowed_idx + 1]
        if a_slice.empty:
            return None

        A_ts = (a_slice[cmp_col].idxmin() if direction == "up"
                else a_slice[cmp_col].idxmax())
        idxA   = df.index.get_loc(A_ts)
        priceA = float(df.at[A_ts, cmp_col])

        # 2) B candidates = PIVOTS ONLY after A (optionally limit b-lookback)
        b_lo = idxA + 1
        b_hi = last_allowed_idx
        if b_max_lookback is not None:
            b_lo = max(b_lo, (last_allowed_idx + 1) - int(b_max_lookback))
        search_df = df.iloc[b_lo : b_hi + 1]
        if search_df.empty:
            return None

        pivB = _pivots(search_df, cmp_col, pivot_w)
        if pivB.empty:
            return None

        # 3) Scan B from NEWEST → OLDEST to get the closest viable line to the current candle
        for B_ts, row in pivB.iloc[::-1].iterrows():
            priceB = float(row[cmp_col])
            # Uptrend support must have higher low; downtrend resistance must have lower high
            if (direction == "up" and priceB <= priceA) or (direction == "down" and priceB >= priceA):
                continue

            idxB  = df.index.get_loc(B_ts)
            slope = (priceB - priceA) / (idxB - idxA)

            # 4) CLEAN: no wick intersections from (A+1) all the way to last_allowed (between AND after)
            clean = True
            EPS = 1e-12
            for i, r in enumerate(df.iloc[idxA + 1 : last_allowed_idx + 1].itertuples(index=True)):
                ray_y = priceA + slope * (i + 1)
                if direction == "down":
                    # resistance: NO wick (high) above the ray, anywhere
                    if r.high > ray_y + EPS:
                        clean = False
                        break
                else:
                    # support: NO wick (low) below the ray, anywhere
                    if r.low < ray_y - EPS:
                        clean = False
                        break

            if clean:
                return {
                    "A_ts":   A_ts,
                    "B_ts":   B_ts,
                    "priceA": priceA,
                    "priceB": priceB,
                    "idxA":   idxA,
                    "idxB":   idxB,
                    "slope":  slope,
                }

        return None

    # ---------- ORIGINAL (non‑strict) PATH ----------
    # 1) Choose / snap A (seeded or pivot)
    if seed_A_ts:
        try:
            idx_seed = df.index.get_loc(seed_A_ts)
        except KeyError:
            pos = df.index.searchsorted(seed_A_ts)
            if pos == 0:
                idx_seed = 0
            elif pos >= len(df):
                idx_seed = len(df) - 1
            else:
                before = pos - 1
                idx_seed = before if (seed_A_ts - df.index[before]) <= (df.index[pos] - seed_A_ts) else pos
        if idx_seed > last_allowed_idx:
            idx_seed = last_allowed_idx
        lo = max(0, idx_seed - snap_window)
        hi = min(last_allowed_idx, idx_seed + snap_window)
        if lo > hi:
            return None
        window = df.iloc[lo : hi + 1]
        A_ts = (window[cmp_col].idxmin() if direction == "up" else window[cmp_col].idxmax())
    else:
        pivots = _pivots(df.iloc[: last_allowed_idx + 1], cmp_col, pivot_w)
        if pivots.empty:
            return None
        A_ts = (pivots[cmp_col].idxmin() if direction == "up" else pivots[cmp_col].idxmax())

    idxA   = df.index.get_loc(A_ts)
    priceA = float(df.at[A_ts, cmp_col])

    # 2) B candidates
    search_df  = df.iloc[idxA + 1 : last_allowed_idx + 1]
    candidates = (search_df if use_absolute_pivots else _pivots(search_df, cmp_col, pivot_w))
    if candidates.empty:
        return None

    checked = 0
    for B_ts, row in candidates.iloc[::-1].iterrows():  # newest first
        checked += 1
        priceB = float(row[cmp_col])
        if (direction == "up" and priceB <= priceA) or (direction == "down" and priceB >= priceA):
            continue

        idxB  = df.index.get_loc(B_ts)
        slope = (priceB - priceA) / (idxB - idxA)

        # Cleanliness with “recent-window” tolerance as before
        clean = True
        n = len(df)
        first_recent_idx = max(0, n - exclude_recent) if exclude_recent > 0 else n
        guard_last_n = 2 if exclude_recent > 0 else 0
        last_guard_start = n - guard_last_n
        EPS = 1e-10

        for i, r in enumerate(df.iloc[idxA + 1 :].itertuples(index=True)):
            global_idx = idxA + 1 + i
            if guard_last_n and global_idx >= last_guard_start:
                continue
            ray_y = priceA + slope * (i + 1)
            in_recent = global_idx >= first_recent_idx
            if direction == "down":
                test_val = (max(r.open, r.close) if in_recent else r.high)
                if test_val > ray_y * (1 - tol_pct) + EPS:
                    clean = False
                    break
            else:
                test_val = (min(r.open, r.close) if in_recent else r.low)
                if test_val < ray_y * (1 + tol_pct) - EPS:
                    clean = False
                    break

        if clean:
            return {
                "A_ts":   A_ts,
                "B_ts":   B_ts,
                "priceA": priceA,
                "priceB": priceB,
                "idxA":   idxA,
                "idxB":   idxB,
                "slope":  slope,
            }

    return None






# ==========================================================================
#                         Async and analyse section
# ==========================================================================

async def _analyse_symbol(
    ex,
    symbol: str,
    analysis_tf: Optional[str] = None,
    pivot_w: Optional[int] = None,
    tol_pct: Optional[float] = None,
    force_exclude_recent: Optional[int] = None,
    side: Optional[str] = None  # None for both directions, "long" for downtrend, "short" for uptrend
) -> Optional[Dict[str, Any]]:    
    """
    Walk through every TIMEFRAME with identical rules to find trendlines.
    If side is provided, return the steepest clean opposite trendline (down for long, up for short).
    Otherwise, log all trendlines and return None.
    """
    prev_B:     Dict[str, Optional[pd.Timestamp]] = {"up": None, "down": None}
    prev_line:  Dict[str, Optional[Dict]]         = {"up": None, "down": None}
    trend_pts:  Dict[str, List[Dict[str, Any]]]   = {"up": [], "down": []}
    df_by_tf:   Dict[str, pd.DataFrame]           = {}
    lines_out:  Dict[str, List[Dict[str, Any]]]   = {"up": [], "down": []}  # ← collect all viable lines


    lowest_viable: Dict[str, Optional[Dict]] = {"up": None, "down": None}
    lowest_viable_tf: Dict[str, Optional[str]] = {"up": None, "down": None}
    df_by_tf: Dict[str, pd.DataFrame] = {}

    for tf in TIMEFRAMES:
        if not ENABLED_TFS.get(tf, True):
            continue

        # Fetch candles
        ohlc = await ex.fetch_ohlcv(symbol, tf, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlc, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        # Keep full series; per‑TF exclusion handled by EXCLUDE_RECENT_MAP
        df_by_tf[tf] = df



        bar_delta_sec = int((df.index[1] - df.index[0]).total_seconds())
        pivot_w = pivot_w or PIVOT_W_DEFAULT
        tol_pct = tol_pct or TOL_PCT_DEFAULT
        use_abs = USE_ABSOLUTE_PIVOTS_DEFAULT
        exclude_recent = EXCLUDE_RECENT_MAP.get(tf, 0) if force_exclude_recent is None else force_exclude_recent

        for direction in ("up", "down"):
            strict = (tf == "4h")
            attempts = 0
            seed = prev_B[direction]

            while attempts < MAX_CHAIN_PER_TF:
                # 4h strict: ignore seed (absolute A); lower TFs: seed to drill forward
                line = _select_line(
                    df_by_tf[tf],
                    direction=direction,
                    pivot_w=pivot_w,
                    tol_pct=tol_pct,
                    use_absolute_pivots=(False if strict else use_abs),
                    seed_A_ts=(None if strict else seed),
                    exclude_recent=(0 if strict else exclude_recent),
                    snap_window=3,
                    strict_absolute=strict,
                    a_max_lookback=(600 if strict else A_LOOKBACK_BARS.get(tf)),
                    b_max_lookback=(600 if strict else B_LOOKBACK_BARS.get(tf)),
                )

                if line is None:
                    # stop chaining for this direction/TF
                    if attempts == 0:
                        # reset seed when we found nothing at the very first try
                        prev_B[direction] = None
                        prev_line[direction] = None
                    break

                # annotate and collect
                line["tf"] = tf
                lines_out[direction].append(line)

                # record points (for your combined logger)
                for pt in (line["A_ts"], line["B_ts"]):
                    if not any(p["ts"] == pt for p in trend_pts[direction]):
                        trend_pts[direction].append({"ts": pt, "tf": tf})

                # update seeds to drill further within the SAME TF
                prev_B[direction] = line["B_ts"]
                prev_line[direction] = line
                seed = line["B_ts"]

                # for 4h strict we only need the closest viable line (first one)
                if strict:
                    break

                attempts += 1


    # If side is specified, return the steepest clean opposite line
    if side:
        opposite_direction = "down" if side == "long" else "up"
        line = lowest_viable[opposite_direction]
        if line:
            line["tf"] = lowest_viable_tf[opposite_direction]
            return line
        return None

    # Log the most recent line per direction (optional) and return ALL chained lines
    for direction in ("up", "down"):
        if lines_out[direction]:
            last_line = lines_out[direction][-1]
            tf = last_line["tf"]
            df_chosen = df_by_tf.get(tf)
            if df_chosen is not None and len(df_chosen) >= 2:
                cross_dir = _two_candle_breakout(df_chosen, last_line, direction)
                broke = (cross_dir != 0)
                touches = _count_touchpoints(
                    df_chosen, last_line, direction=direction, exclude_last=2, start_from_A=True
                )
                _log_line(symbol, tf, direction, last_line, touches, broke)

    # Keep the latest per-direction for quick reference
    LAST_SELECTED[symbol] = {
        "up":   ({"tf": lines_out["up"][-1]["tf"],   "line": lines_out["up"][-1]}   if lines_out["up"]   else {"tf": None, "line": None}),
        "down": ({"tf": lines_out["down"][-1]["tf"], "line": lines_out["down"][-1]} if lines_out["down"] else {"tf": None, "line": None}),
    }

    return lines_out



# ==========================================================================
#                                Logging
# ==========================================================================

def _log_combined(sym: str, direction: str, points: List[Dict[str, Any]], broke: bool):
    if not points:
        return

    base = sym.split("/")[0]
    emo = "📉" if direction == "down" else "📈"
    label = "Downward" if direction == "down" else "Upward"
    status = "Breakout ✅" if broke else "Breakout ❌"

    print(f"{emo} {base} {label} Trendline\n")
    pairs = [(points[i], points[i + 1]) for i in range(0, len(points) - 1, 2)]
    for i, (a_point, b_point) in enumerate(pairs, 1):
        print(f"RAY {i} - [{a_point['tf']}] Touch Point A: {a_point['ts'].strftime('%Y-%m-%d %H:%M')}")
        print(f"RAY {i} - [{b_point['tf']}] Touch Point B: {b_point['ts'].strftime('%Y-%m-%d %H:%M')}")
    print(f"\n{status}\n")
