"""
Personal NIFTY Order Book + 1-Min Candles + Option OI Screen
Supports:
  1. "demo"   — Simulated live NIFTY/BANKNIFTY order book, candles & option OI (no account needed)
  2. "upstox" — Live data via your Upstox Analytics Token (.env), using official upstox-python-sdk
"""

# ==============================================================================
# SETTINGS — Every setting is here at the top with a one-line comment
# ==============================================================================
SOURCE = "demo"                  # Startup data source: "demo" or "upstox" (overridden by .env if token is set)
DEFAULT_SYMBOL = "NIFTY"         # Default index future on startup: "NIFTY", "BANKNIFTY", or "SENSEX"
BAND_SIZE = 5.0                  # Order book price band grouping size in index points (1, 2, 5, or 10)
DEMO_LEVELS = 20                 # Number of waiting order levels on each side in demo mode
DEMO_LOT_SIZE = 65               # Lot size used to count lots in demo mode (live mode reads exact broker lot size)
HIGHLIGHT_TOP_N = 3              # Number of largest order bands highlighted solid & labelled on each side
BOOK_REFRESH_SECONDS = 1.0       # How often demo order book updates and browser refreshes (in seconds)
OI_REFRESH_SECONDS = 60          # How often option chain open interest refreshes (seconds; keeps API usage tiny)
OI_RANGE_POINTS = 500            # Option chain strike window (+/- points around current NIFTY price)
OI_STEP_POINTS = 50              # Option strike interval in points for NIFTY (BANKNIFTY auto-scales to 100)
MAX_CANDLES = 375                # Maximum 1-minute candles kept in memory (full 6h15m NSE/BSE session)
MAX_LADDER_ROWS = 45             # Maximum price ladder rows rendered at once so the book stays readable
RECONNECT_DELAY_SECONDS = 10     # Seconds to wait before reconnecting Upstox WebSocket after a drop
UPSTOX_DEPTH_MODE = "full"       # Upstox WebSocket mode: "full" (free 5 levels) or "full_d30" (30 levels, Plus Pack)
HOST = "0.0.0.0"                 # Server bind host (set to "127.0.0.1" on your own PC for local-only access)
PORT = 8000                      # HTTP port where the web screen is served (http://127.0.0.1:8000)

# ==============================================================================
# IMPORTS (Standard library + python-dotenv + upstox-python-sdk)
# ==============================================================================
import base64
import hmac
import json
import math
import os
import random
import statistics
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

try:
    import upstox_client
    from upstox_client.rest import ApiException
    HAS_UPSTOX_SDK = True
except ImportError:
    upstox_client = None
    ApiException = Exception
    HAS_UPSTOX_SDK = False

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
INDEX_HTML_PATH = BASE_DIR / "index.html"
IST = timezone(timedelta(hours=5, minutes=30))

UNDERLYING_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

SYMBOL_DEFAULTS = {
    "NIFTY": {
        "exchange": "NSE",
        "base_price": 25350.0,
        "lot_size": DEMO_LOT_SIZE,
        "oi_range": OI_RANGE_POINTS,
        "oi_step": OI_STEP_POINTS,
        "default_band": 5.0,
        "big_band": 5,
        "near_window": 20,
        "demo_basis": 6.5,
        "step_scale": 1.0,
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "base_price": 53400.0,
        "lot_size": 30,
        "oi_range": 1500,
        "oi_step": 100,
        "default_band": 5.0,
        "big_band": 10,
        "near_window": 50,
        "demo_basis": 18.0,
        "step_scale": 2.4,
    },
    "SENSEX": {
        "exchange": "BSE",
        "base_price": 83500.0,
        "lot_size": 20,
        "oi_range": 2000,
        "oi_step": 100,
        "default_band": 10.0,
        "big_band": 20,
        "near_window": 100,
        "demo_basis": 25.0,
        "step_scale": 3.5,
    },
}

UPSTOX_RATE_LIMIT_NOTE = (
    "Upstox API limits: 50 requests/sec, 500 requests/min, 2,000 requests/30 min. "
    "This screen uses 1 WebSocket stream + 1 Option Chain call per minute (<0.2% of limit)."
)


# ==============================================================================
# HELPERS (Time, Safe .env Token Reader, Order Book Grouping)
# ==============================================================================
def now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open_ist() -> bool:
    dt = now_ist()
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    mins = dt.hour * 60 + dt.minute
    return (9 * 60 + 15) <= mins <= (15 * 60 + 30)


def load_env_config() -> dict:
    """Read .env and OS environment variables (Render env vars override empty .env)."""
    cfg = {}
    if ENV_PATH.exists():
        cfg.update({k: (v or "").strip() for k, v in dotenv_values(ENV_PATH).items() if k})
    for key in (
        "UPSTOX_ANALYTICS_TOKEN",
        "UPSTOX_ACCESS_TOKEN",
        "BROKER_TOKEN",
        "SOURCE",
        "UPSTOX_DEPTH_MODE",
        "HOST",
        "PORT",
        "APP_PASSWORD",
    ):
        env_val = os.environ.get(key)
        if env_val and env_val.strip():
            cfg[key] = env_val.strip()
    return cfg


def get_app_password() -> str:
    """Optional password to keep your personal screen private when hosted on Render."""
    return load_env_config().get("APP_PASSWORD", "").strip()


def get_upstox_token() -> str:
    """Return Upstox Analytics/Access token from .env, stripped of accidental quotes."""
    cfg = load_env_config()
    raw = cfg.get("UPSTOX_ANALYTICS_TOKEN") or cfg.get("UPSTOX_ACCESS_TOKEN") or cfg.get("BROKER_TOKEN") or ""
    token = raw.strip().strip('"').strip("'")
    if token.lower() in ("", "your-token-here", "your-key-or-token", "paste-token-here"):
        return ""
    return token


def normalize_expiry(val) -> str:
    """Normalize SDK expiry (datetime, date, epoch ms, or string) into YYYY-MM-DD."""
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, (int, float)):
        ts = val / 1000.0 if val > 1e11 else float(val)
        return datetime.fromtimestamp(ts, tz=IST).strftime("%Y-%m-%d")
    s = str(val).strip()
    return s[:10] if len(s) >= 10 else s


def extract_field(obj, *keys, default=None):
    """Safely extract nested key/attribute from either dict or Upstox SDK model."""
    cur = obj
    for k in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
    return default if cur is None else cur


def parse_upstox_error(exc: Exception) -> tuple[bool, str]:
    """
    Inspect an Upstox ApiException without leaking credentials.
    Returns (is_token_expired_or_invalid, plain_english_message).
    """
    status = getattr(exc, "status", None)
    body = getattr(exc, "body", "") or ""
    code = ""
    api_msg = ""
    if body:
        try:
            parsed = json.loads(body)
            errs = parsed.get("errors") or []
            if errs:
                code = errs[0].get("errorCode") or errs[0].get("error_code") or ""
                api_msg = errs[0].get("message") or ""
        except Exception:
            pass

    if status == 401 or code in ("UDAPI100050", "UDAPI100011", "UDAPI100068"):
        msg = (
            "Your Upstox Analytics Token is invalid or has expired (HTTP 401). "
            "How to renew: Open https://account.upstox.com/developer/apps#analytics -> "
            "click 'Generate Token' -> copy the new token -> paste it after UPSTOX_ANALYTICS_TOKEN= in .env -> "
            "click 'Retry Upstox' on the page (or restart app.py)."
        )
        return True, msg

    if status == 429:
        return False, "Upstox rate limit reached (HTTP 429). Close any second copy running and wait 60 seconds."

    detail = f"{code}: {api_msg}".strip(": ") if (code or api_msg) else str(exc).splitlines()[0][:180]
    return False, f"Upstox API error ({status or 'network'}): {detail}"


def build_grouped_ladder(
    bids: list[tuple[float, int]],
    asks: list[tuple[float, int]],
    band_size: float,
    lot_size: int,
    highlight_top_n: int,
    mid_price: float,
) -> dict:
    """
    Group raw (price, quantity) buy & sell levels into `band_size` point bands,
    count quantities in lots, and mark the `highlight_top_n` largest bands on each side.
    """
    lot_sz = max(1, int(lot_size or 1))
    bs = max(0.5, float(band_size or 1.0))

    def snap_bid(p: float) -> float:
        return round(math.floor(round(p / bs, 6)) * bs, 2)

    def snap_ask(p: float) -> float:
        return round(math.ceil(round(p / bs, 6)) * bs, 2)

    buy_bands: dict[float, float] = {}
    sell_bands: dict[float, float] = {}

    for price, qty in bids:
        if price > 0 and qty > 0:
            b = snap_bid(price)
            buy_bands[b] = buy_bands.get(b, 0.0) + (qty / lot_sz)

    for price, qty in asks:
        if price > 0 and qty > 0:
            b = snap_ask(price)
            sell_bands[b] = sell_bands.get(b, 0.0) + (qty / lot_sz)

    # Rank top N largest bands on each side
    top_buy = sorted(buy_bands.items(), key=lambda x: (-x[1], -x[0]))[:highlight_top_n]
    top_sell = sorted(sell_bands.items(), key=lambda x: (-x[1], x[0]))[:highlight_top_n]
    buy_ranks = {p: idx + 1 for idx, (p, lots) in enumerate(top_buy) if lots > 0}
    sell_ranks = {p: idx + 1 for idx, (p, lots) in enumerate(top_sell) if lots > 0}

    all_prices = sorted(set(buy_bands.keys()) | set(sell_bands.keys()), reverse=True)
    if not all_prices:
        center = round((mid_price or 25000.0) / bs) * bs
        ladder_prices = [round(center + (i * bs), 2) for i in range(10, -11, -1)]
    else:
        high_p = all_prices[0]
        low_p = all_prices[-1]
        total_steps = int(round((high_p - low_p) / bs)) + 1
        if total_steps <= MAX_LADDER_ROWS:
            ladder_prices = [round(high_p - i * bs, 2) for i in range(total_steps)]
        else:
            # Keep active bands centered near mid_price if outer levels are far apart
            center = round((mid_price or ((high_p + low_p) / 2.0)) / bs) * bs
            half = MAX_LADDER_ROWS // 2
            window_prices = {round(center + i * bs, 2) for i in range(-half, half + 1)}
            window_prices.update(all_prices)
            ladder_prices = sorted(window_prices, reverse=True)[:MAX_LADDER_ROWS]

    max_lots = max(
        [lots for lots in buy_bands.values()] + [lots for lots in sell_bands.values()] + [1.0]
    )

    rows = []
    for p in ladder_prices:
        b_lots = round(buy_bands.get(p, 0.0), 1)
        s_lots = round(sell_bands.get(p, 0.0), 1)
        rows.append({
            "price": p,
            "buy_lots": b_lots,
            "sell_lots": s_lots,
            "buy_rank": buy_ranks.get(p, 0),
            "sell_rank": sell_ranks.get(p, 0),
            "buy_highlight": p in buy_ranks,
            "sell_highlight": p in sell_ranks,
        })

    total_buy_lots = round(sum(buy_bands.values()), 1)
    total_sell_lots = round(sum(sell_bands.values()), 1)

    return {
        "rows": rows,
        "max_band_lots": round(max_lots, 1),
        "total_buy_lots": total_buy_lots,
        "total_sell_lots": total_sell_lots,
        "top_buy_bands": [{"price": p, "lots": round(l, 1), "rank": i + 1} for i, (p, l) in enumerate(top_buy)],
        "top_sell_bands": [{"price": p, "lots": round(l, 1), "rank": i + 1} for i, (p, l) in enumerate(top_sell)],
    }


# ==============================================================================
# MARKET ENGINE (Manages Both Demo Simulator & Live Upstox Feed)
# ==============================================================================
class MarketScreenEngine:
    def __init__(self):
        self.lock = threading.Lock()
        env_cfg = load_env_config()
        token_present = bool(get_upstox_token())

        configured_source = (env_cfg.get("SOURCE") or "").lower()
        if configured_source in ("demo", "upstox"):
            self.source = configured_source
        elif SOURCE.lower() == "upstox" or token_present:
            self.source = "upstox"
        else:
            self.source = "demo"

        self.symbol = DEFAULT_SYMBOL
        self.band_size = float(BAND_SIZE)
        self.depth_mode = (env_cfg.get("UPSTOX_DEPTH_MODE") or UPSTOX_DEPTH_MODE or "full").strip()
        if self.depth_mode not in ("full", "full_d30"):
            self.depth_mode = "full"

        # Shared live state
        self.running = True
        self.session_id = 0
        self.streamer = None

        self._reset_symbol_state(self.symbol)

        # Start background threads
        threading.Thread(target=self._demo_loop, daemon=True).start()
        if self.source == "upstox":
            self._start_upstox_session()

    def _reset_symbol_state(self, symbol: str):
        defaults = SYMBOL_DEFAULTS.get(symbol, SYMBOL_DEFAULTS["NIFTY"])
        base = defaults["base_price"]
        lot_sz = defaults["lot_size"]

        self.contract_name = f"{symbol} DEMO FUT" if self.source == "demo" else f"{symbol} FUT"
        self.instrument_key = ""
        self.future_expiry = ""
        self.option_expiry = ""
        self.lot_size = lot_sz
        self.feed_levels = DEMO_LEVELS if self.source == "demo" else (30 if self.depth_mode == "full_d30" else 5)

        self.ltp = base
        self.mid_price = base
        self.spot_price = round(base - defaults.get("demo_basis", 6.5), 2)
        self.spot_basis = float(defaults.get("demo_basis", 6.5))
        self.prev_close = base - 42.5
        self.best_bid = base - 0.5
        self.best_ask = base + 0.5
        self.bids: list[tuple[float, int]] = []
        self.asks: list[tuple[float, int]] = []
        self.candles: list[dict] = []
        self.option_rows: list[dict] = []
        self.option_summary: dict = {}
        self.last_book_ts = 0.0
        self.last_oi_ts = 0.0

        # Big Orders tracker state ("Big orders right now" tab)
        self._band_lots_samples: deque[float] = deque(maxlen=400)
        self._near_depth_samples: deque[float] = deque(maxlen=300)
        self._spread_samples: deque[float] = deque(maxlen=300)
        self._resting_walls: dict[tuple[str, int], dict] = {}
        self._wall_events: deque[dict] = deque(maxlen=16)
        self.big_orders_snapshot: dict = {
            "items": [],
            "events": [],
            "near_window": defaults.get("near_window", 20),
            "depth_pct_usual": 100,
            "spread": 0.0,
            "usual_spread": 0.0,
            "usual_band_lots": 0.0,
            "buyers_pct": 50,
            "sellers_pct": 50,
            "buy_near_lots": 0,
            "sell_near_lots": 0,
        }

        # Per-band live Order-Flow tracker (waiting, bought, sold, pulled/cancelled)
        self._prev_band_buy: dict[float, float] = {}
        self._prev_band_sell: dict[float, float] = {}
        self._prev_depth_bounds: tuple[float, float] = (0.0, 0.0)
        self._pulled_buy: dict[float, float] = {}
        self._pulled_sell: dict[float, float] = {}
        self._bought_lots: dict[float, float] = {}
        self._sold_lots: dict[float, float] = {}
        self._last_flow_ts: float = 0.0
        self._last_flow_bs: float = float(self.band_size)
        self._last_vtt: float = 0.0

        self.connection_status = "starting"
        self.status_message = "Initializing market screen..."
        self.token_expired = False

        # Demo state anchors ("resting liquidity walls" so top-3 bands behave realistically)
        self._demo_walls_buy: dict[float, int] = {}
        self._demo_walls_sell: dict[float, int] = {}
        if self.source == "demo":
            self._seed_demo_history()

    # --------------------------------------------------------------------------
    # CANDLE UPDATER (1-minute candles from middle of best buy & best sell)
    # --------------------------------------------------------------------------
    def _update_candle_locked(self, price: float, ts: float | None = None):
        if price <= 0:
            return
        now_sec = int(ts if ts is not None else time.time())
        bucket = (now_sec // 60) * 60
        p = round(float(price), 2)
        if self.candles and self.candles[-1]["time"] == bucket:
            c = self.candles[-1]
            c["high"] = round(max(c["high"], p), 2)
            c["low"] = round(min(c["low"], p), 2)
            c["close"] = p
        else:
            # Across overnight / >3-min session gaps, start candle at actual new tick `p`
            if self.candles and (bucket - self.candles[-1]["time"]) <= 180:
                open_p = self.candles[-1]["close"]
            else:
                open_p = p
            self.candles.append({
                "time": bucket,
                "open": round(open_p, 2),
                "high": round(max(open_p, p), 2),
                "low": round(min(open_p, p), 2),
                "close": p,
            })
            if len(self.candles) > MAX_CANDLES:
                self.candles = self.candles[-MAX_CANDLES:]

    def _update_order_flow_locked(
        self,
        trade_price: float = 0.0,
        trade_lots: float = 0.0,
        trade_side: str = "",
    ):
        """
        Track per-band:
          - pulled (cancelled) buy & sell lots (depth decrease minus traded executions, ~18s decay)
          - aggressive bought (lifting ask) & sold (hitting bid) lots (~20s decay)
        """
        now = time.time()
        bs = max(0.5, float(self.band_size or 5.0))
        lot_sz = max(1, int(self.lot_size or 1))

        if abs(bs - self._last_flow_bs) > 1e-6:
            self._prev_band_buy.clear()
            self._prev_band_sell.clear()
            self._pulled_buy.clear()
            self._pulled_sell.clear()
            self._bought_lots.clear()
            self._sold_lots.clear()
            self._last_flow_bs = bs

        dt = min(5.0, max(0.05, now - self._last_flow_ts)) if self._last_flow_ts > 0 else 1.0
        self._last_flow_ts = now
        decay_pull = math.exp(-dt / 18.0)
        decay_trade = math.exp(-dt / 20.0)

        for dct, dec in (
            (self._pulled_buy, decay_pull),
            (self._pulled_sell, decay_pull),
            (self._bought_lots, decay_trade),
            (self._sold_lots, decay_trade),
        ):
            for k in list(dct.keys()):
                nv = dct[k] * dec
                if nv < 0.45:
                    dct.pop(k, None)
                else:
                    dct[k] = nv

        def snap_bid(p: float) -> float:
            return round(math.floor(round(p / bs, 6)) * bs, 2)

        def snap_ask(p: float) -> float:
            return round(math.ceil(round(p / bs, 6)) * bs, 2)

        curr_buy: dict[float, float] = {}
        curr_sell: dict[float, float] = {}
        for price, qty in self.bids:
            if price > 0 and qty > 0:
                b = snap_bid(price)
                curr_buy[b] = curr_buy.get(b, 0.0) + (qty / lot_sz)
        for price, qty in self.asks:
            if price > 0 and qty > 0:
                b = snap_ask(price)
                curr_sell[b] = curr_sell.get(b, 0.0) + (qty / lot_sz)

        tp_band = 0.0
        if trade_price > 0 and trade_lots > 0:
            if trade_side == "BUY":
                tp_band = snap_ask(trade_price)
                self._bought_lots[tp_band] = self._bought_lots.get(tp_band, 0.0) + trade_lots
            else:
                tp_band = snap_bid(trade_price)
                self._sold_lots[tp_band] = self._sold_lots.get(tp_band, 0.0) + trade_lots

        curr_min_bid = min(curr_buy.keys()) if curr_buy else 0.0
        curr_max_ask = max(curr_sell.keys()) if curr_sell else 0.0

        if self._prev_band_buy and curr_min_bid > 0:
            for p, prev_l in self._prev_band_buy.items():
                # Only count cancellations inside currently visible depth window
                if p >= curr_min_bid:
                    drop = prev_l - curr_buy.get(p, 0.0)
                    exec_here = trade_lots if (trade_side == "SELL" and abs(tp_band - p) < 1e-4) else 0.0
                    pulled = drop - exec_here
                    if pulled >= 1.5:
                        self._pulled_buy[p] = self._pulled_buy.get(p, 0.0) + pulled

        if self._prev_band_sell and curr_max_ask > 0:
            for p, prev_l in self._prev_band_sell.items():
                if p <= curr_max_ask:
                    drop = prev_l - curr_sell.get(p, 0.0)
                    exec_here = trade_lots if (trade_side == "BUY" and abs(tp_band - p) < 1e-4) else 0.0
                    pulled = drop - exec_here
                    if pulled >= 1.5:
                        self._pulled_sell[p] = self._pulled_sell.get(p, 0.0) + pulled

        self._prev_band_buy = curr_buy
        self._prev_band_sell = curr_sell
        self._prev_depth_bounds = (curr_min_bid, curr_max_ask)

    # --------------------------------------------------------------------------
    # BIG ORDERS ENGINE ("Big orders right now" tab — resting walls & context)
    # --------------------------------------------------------------------------
    @staticmethod
    def _fmt_resting(seconds: float) -> str:
        s = max(1, int(round(seconds)))
        if s < 60:
            return f"resting {s}s"
        return f"resting {s // 60} min"

    def _update_big_orders_locked(self):
        defaults = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"])
        w = int(defaults.get("big_band", 5))
        near_win = int(defaults.get("near_window", 20))
        strike_step = int(defaults.get("oi_step", 50))
        lot_sz = max(1, int(self.lot_size or 1))
        mid = float(self.mid_price or self.ltp or defaults["base_price"])
        now = time.time()

        # Group buy & sell depth into [band_low, band_low + w] ranges
        buy_bands: dict[int, float] = {}
        sell_bands: dict[int, float] = {}
        buy_near = 0.0
        sell_near = 0.0

        for price, qty in self.bids:
            if price > 0 and qty > 0:
                lots = qty / lot_sz
                b_low = int(math.floor((price - 1e-6) / w) * w)
                buy_bands[b_low] = buy_bands.get(b_low, 0.0) + lots
                if (mid - price) <= near_win:
                    buy_near += lots

        for price, qty in self.asks:
            if price > 0 and qty > 0:
                lots = qty / lot_sz
                s_low = int(math.floor(price / w) * w)
                sell_bands[s_low] = sell_bands.get(s_low, 0.0) + lots
                if (price - mid) <= near_win:
                    sell_near += lots

        # Update rolling median baselines ("usual" band lots, near depth, spread)
        all_band_vals = [v for v in list(buy_bands.values()) + list(sell_bands.values()) if v > 0]
        for v in all_band_vals:
            self._band_lots_samples.append(v)

        tot_near = buy_near + sell_near
        if tot_near > 0:
            self._near_depth_samples.append(tot_near)

        cur_spread = round(self.best_ask - self.best_bid, 2) if (self.best_ask > 0 and self.best_bid > 0) else 0.0
        if cur_spread > 0:
            self._spread_samples.append(cur_spread)

        usual_band_lots = (
            float(statistics.median(self._band_lots_samples))
            if self._band_lots_samples
            else 10.0
        )
        usual_band_lots = max(1.0, usual_band_lots)

        usual_near = (
            float(statistics.median(self._near_depth_samples))
            if self._near_depth_samples
            else max(1.0, tot_near)
        )
        depth_pct_usual = int(round((tot_near / max(1.0, usual_near)) * 100.0)) if tot_near > 0 else 0

        usual_spread = (
            round(float(statistics.median(self._spread_samples)), 2)
            if self._spread_samples
            else cur_spread
        )

        # Lookup map for nearest option strike OI alignment
        oi_map = {int(r["strike"]): r for r in self.option_rows if "strike" in r}
        basis = float(self.spot_basis or 0.0)
        time_label = now_ist().strftime("%H:%M:%S")

        seen_keys: set[tuple[str, int]] = set()
        items: list[dict] = []

        def evaluate_side(side: str, bands_dict: dict[int, float]):
            for b_low, lots in bands_dict.items():
                b_high = b_low + w
                mult = round(lots / usual_band_lots, 1)
                key = (side, b_low)
                already_resting = key in self._resting_walls

                # Enter at >= 2.0x usual; stay active down to >= 1.8x usual (hysteresis)
                if mult >= 2.0 or (already_resting and mult >= 1.8):
                    seen_keys.add(key)
                    if not already_resting:
                        self._resting_walls[key] = {
                            "since": now,
                            "last_seen": now,
                            "peak_lots": round(lots, 1),
                            "band_high": b_high,
                        }
                        self._wall_events.appendleft({
                            "time": time_label,
                            "type": "APPEARED",
                            "side": side,
                            "range_label": f"{b_low:,}–{b_high:,}",
                            "detail": f"{int(round(lots))} lots ({mult:.1f}× usual)",
                        })
                    else:
                        rec = self._resting_walls[key]
                        rec["last_seen"] = now
                        rec["peak_lots"] = max(rec["peak_lots"], round(lots, 1))

                    rec = self._resting_walls[key]
                    rest_sec = max(1.0, now - rec["since"])

                    if side == "SELL":
                        dist = max(0.0, round(b_low - mid, 1))
                        dist_label = f"{dist:.1f} pts above"
                    else:
                        dist = max(0.0, round(mid - b_high, 1))
                        dist_label = f"{dist:.1f} pts below"

                    band_mid = (b_low + b_high) / 2.0
                    implied_spot = int(round(band_mid - basis))
                    nearest_strike = int(round(implied_spot / strike_step) * strike_step)
                    oi_row = oi_map.get(nearest_strike, {})

                    items.append({
                        "side": side,
                        "band_low": b_low,
                        "band_high": b_high,
                        "range_label": f"{b_low:,}–{b_high:,}",
                        "lots": int(round(lots)),
                        "mult": mult,
                        "mult_label": f"{mult:.1f}× usual",
                        "distance": dist,
                        "distance_label": dist_label,
                        "resting_sec": int(round(rest_sec)),
                        "resting_label": self._fmt_resting(rest_sec),
                        "implied_spot": implied_spot,
                        "nearest_strike": nearest_strike,
                        "ce_oi_lots": oi_row.get("ce_oi_lots", 0),
                        "pe_oi_lots": oi_row.get("pe_oi_lots", 0),
                    })

        evaluate_side("SELL", sell_bands)
        evaluate_side("BUY", buy_bands)

        # Expire walls unseen for > 8 seconds (grace window avoids false resets on 5-level depth)
        expired_keys = [
            k for k, rec in self._resting_walls.items()
            if k not in seen_keys and (now - rec["last_seen"]) > 8.0
        ]
        for k in expired_keys:
            rec = self._resting_walls.pop(k)
            rested_for = rec["last_seen"] - rec["since"]
            if rested_for >= 12.0:
                side_k, b_low_k = k
                b_high_k = rec.get("band_high", b_low_k + w)
                self._wall_events.appendleft({
                    "time": time_label,
                    "type": "GONE",
                    "side": side_k,
                    "range_label": f"{b_low_k:,}–{b_high_k:,}",
                    "detail": f"peak {int(round(rec['peak_lots']))} lots · after {self._fmt_resting(rested_for).replace('resting ', '')}",
                })

        # Sort nearest to current price first (exact match to screenshot!)
        items.sort(key=lambda x: (x["distance"], -x["mult"]))

        buyers_pct = int(round((buy_near / tot_near) * 100.0)) if tot_near > 0 else 50
        sellers_pct = 100 - buyers_pct

        self.big_orders_snapshot = {
            "items": items,
            "events": list(self._wall_events),
            "near_window": near_win,
            "big_band": w,
            "depth_pct_usual": depth_pct_usual,
            "spread": cur_spread,
            "usual_spread": usual_spread,
            "usual_band_lots": round(usual_band_lots, 1),
            "buyers_pct": buyers_pct,
            "sellers_pct": sellers_pct,
            "buy_near_lots": int(round(buy_near)),
            "sell_near_lots": int(round(sell_near)),
            "spot_price": round(mid - basis, 2),
            "basis": round(basis, 2),
        }

    # --------------------------------------------------------------------------
    # DEMO SIMULATOR (Runs when source == "demo")
    # --------------------------------------------------------------------------
    def _seed_demo_history(self):
        defaults = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"])
        base = defaults["base_price"]
        step_scale = defaults.get("step_scale", 1.0)
        now_bucket = (int(time.time()) // 60) * 60
        p = base - random.uniform(-12, 12) * step_scale
        candles = []
        # Seed last 120 1-minute candles (2 hours) so interactive chart zoom-out & pan have rich history
        for i in range(120, 0, -1):
            t = now_bucket - i * 60
            o = round(p, 2)
            delta = random.gauss(0, 1.8 * step_scale) + (base - p) * 0.06
            c = round(o + delta, 2)
            wick_up = abs(random.gauss(0, 1.1 * step_scale))
            wick_dn = abs(random.gauss(0, 1.1 * step_scale))
            h = round(max(o, c) + wick_up, 2)
            l = round(min(o, c) - wick_dn, 2)
            candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
            p = c

        self.candles = candles
        self.mid_price = p
        self.ltp = p
        self.prev_close = round(base - 38.0 * step_scale, 2)
        self._refresh_demo_walls(p)
        self._tick_demo_book_locked()
        self._generate_demo_oi_locked(initial=True)
        self.connection_status = "demo_active"
        self.status_message = "Demo mode active (simulated live NIFTY order book & OI — no broker account needed)."

    def _refresh_demo_walls(self, center: float):
        defaults = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"])
        w = int(defaults.get("big_band", 5))
        c = int(round(center / w) * w)
        now = time.time()
        # Seed realistic multi-minute resting walls so Demo Mode looks authentic immediately
        self._demo_walls_buy = {
            c - 1 * w: random.randint(680, 1120),  # Prominent active Big Buyer wall right at best bid band!
            c - 3 * w: random.randint(65, 115),
            c - 5 * w: random.randint(45, 85),
        }
        self._demo_walls_sell = {
            c + 1 * w: random.randint(85, 135),
            c + 2 * w: random.randint(95, 145),
            c + 4 * w: random.randint(65, 110),
            c + 7 * w: random.randint(115, 165),
            c + 8 * w: random.randint(75, 125),
        }
        # Pre-seed realistic pulled/bought/sold order-flow activity on startup
        if not self._pulled_sell and not self._pulled_buy:
            self._pulled_sell[float(c + 1 * w)] = float(random.randint(240, 520))
            self._pulled_sell[float(c + 2 * w)] = float(random.randint(95, 210))
            self._pulled_buy[float(c - 2 * w)] = float(random.randint(140, 290))
            self._pulled_buy[float(c - 3 * w)] = float(random.randint(90, 220))
            self._pulled_buy[float(c - 4 * w)] = float(random.randint(60, 180))
            self._sold_lots[float(c)] = float(random.randint(10, 28))
            self._bought_lots[float(c + 1 * w)] = float(random.randint(12, 34))
        # Pre-seed realistic resting ages on initial startup
        if not self._resting_walls:
            for bp, bl in self._demo_walls_buy.items():
                self._resting_walls[("BUY", bp)] = {
                    "since": now - random.randint(14, 480),
                    "last_seen": now,
                    "peak_lots": float(bl),
                    "band_high": bp + w,
                }
            for sp, sl in self._demo_walls_sell.items():
                self._resting_walls[("SELL", sp)] = {
                    "since": now - random.randint(15, 900),
                    "last_seen": now,
                    "peak_lots": float(sl),
                    "band_high": sp + w,
                }

    def _tick_demo_book_locked(self):
        defaults = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"])
        lot_sz = defaults["lot_size"]
        step_scale = defaults.get("step_scale", 1.0)
        w = int(defaults.get("big_band", 5))

        # Gentle wandering price with subtle bounce off resting walls
        drift = (defaults["base_price"] - self.mid_price) * 0.03
        move = random.gauss(drift, 0.75 * step_scale)
        self.mid_price = round(self.mid_price + move, 2)
        half_spread = random.choice([0.5, 1.0, 1.5]) * step_scale
        anchor = round(self.mid_price * 2) / 2.0

        self.best_bid = round(anchor - half_spread, 2)
        self.best_ask = round(anchor + half_spread, 2)
        self.ltp = round(random.choice([self.best_bid, self.best_ask, anchor]), 2)
        self.spot_basis = float(defaults.get("demo_basis", 6.5))
        self.spot_price = round(anchor - self.spot_basis, 2)

        # Rotate only 1 wall rarely (~1.5% chance/sec) so most walls genuinely rest for minutes
        if not self._demo_walls_buy:
            self._refresh_demo_walls(anchor)
        elif random.random() < 0.015:
            c = int(round(anchor / w) * w)
            if random.random() < 0.5 and self._demo_walls_buy:
                old_k = random.choice(list(self._demo_walls_buy.keys()))
                self._demo_walls_buy.pop(old_k, None)
                self._demo_walls_buy[c - random.choice([1, 2, 3, 5]) * w] = random.randint(150, 290)
            elif self._demo_walls_sell:
                old_k = random.choice(list(self._demo_walls_sell.keys()))
                self._demo_walls_sell.pop(old_k, None)
                self._demo_walls_sell[c + random.choice([1, 2, 3, 5]) * w] = random.randint(150, 290)

        bids = []
        asks = []
        level_step = max(1.0, w / 2.0)
        used_buy_walls = set()
        used_sell_walls = set()
        for idx in range(DEMO_LEVELS):
            bp = round(self.best_bid - idx * level_step, 2)
            ap = round(self.best_ask + idx * level_step, 2)

            b_band = int(math.floor(round(bp / w, 6)) * w)
            s_band = int(math.ceil(round(ap / w, 6)) * w)

            b_extra = 0
            if b_band in self._demo_walls_buy and b_band not in used_buy_walls:
                used_buy_walls.add(b_band)
                b_extra = int(self._demo_walls_buy[b_band] * random.uniform(0.985, 1.015))

            s_extra = 0
            if s_band in self._demo_walls_sell and s_band not in used_sell_walls:
                used_sell_walls.add(s_band)
                s_extra = int(self._demo_walls_sell[s_band] * random.uniform(0.985, 1.015))

            b_lots = max(3, random.randint(7, 11) + b_extra)
            s_lots = max(3, random.randint(7, 11) + s_extra)

            bids.append((bp, b_lots * lot_sz))
            asks.append((ap, s_lots * lot_sz))

        self.bids = bids
        self.asks = asks
        self.lot_size = lot_sz
        self.feed_levels = DEMO_LEVELS
        self.contract_name = f"{self.symbol} DEMO FUT"
        self.last_book_ts = time.time()

        true_mid = round((self.best_bid + self.best_ask) / 2.0, 2)
        self.mid_price = true_mid

        # Simulate realistic aggressive trade + occasional spoof/pull in Demo Mode (without auto-diff noise)
        now_d = time.time()
        dt_d = min(3.0, max(0.2, now_d - self._last_flow_ts)) if self._last_flow_ts > 0 else 1.0
        self._last_flow_ts = now_d
        dec_d = math.exp(-dt_d / 18.0)
        for dct in (self._pulled_buy, self._pulled_sell, self._bought_lots, self._sold_lots):
            for k in list(dct.keys()):
                if dct[k] * dec_d < 0.5:
                    dct.pop(k, None)
                else:
                    dct[k] *= dec_d

        bs = max(0.5, float(self.band_size or 5.0))
        c_band = round(true_mid / bs) * bs
        if random.random() < 0.45:
            if random.random() < 0.52:
                self._bought_lots[round(c_band + bs, 2)] = min(90.0, self._bought_lots.get(round(c_band + bs, 2), 0.0) + random.randint(4, 14))
            else:
                self._sold_lots[round(c_band, 2)] = min(90.0, self._sold_lots.get(round(c_band, 2), 0.0) + random.randint(4, 14))
        if random.random() < 0.22:
            if random.random() < 0.5:
                pb = round(c_band - random.choice([2, 3, 4]) * bs, 2)
                self._pulled_buy[pb] = min(380.0, self._pulled_buy.get(pb, 20.0) + random.randint(25, 95))
            else:
                ps = round(c_band + random.choice([1, 2]) * bs, 2)
                self._pulled_sell[ps] = min(560.0, self._pulled_sell.get(ps, 30.0) + random.randint(35, 130))

        self._update_candle_locked(true_mid)
        self._update_big_orders_locked()

        if (time.time() - self.last_oi_ts) >= OI_REFRESH_SECONDS or not self.option_rows:
            self._generate_demo_oi_locked(initial=False)

    def _generate_demo_oi_locked(self, initial: bool = False):
        defaults = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"])
        step = defaults["oi_step"]
        rng = defaults["oi_range"]
        lot_sz = defaults["lot_size"]

        atm = int(round(self.mid_price / step) * step)
        strikes = list(range(atm - rng, atm + rng + step, step))

        prev_map = {r["strike"]: r for r in self.option_rows}
        rows = []
        for st in strikes:
            dist = st - self.mid_price
            if initial or st not in prev_map:
                # Realistic OI bell-curve: higher Call OI above ATM, higher Put OI below ATM, peaks at round 100/500 strikes
                round_bonus = 1.65 if (st % (step * 4) == 0) else (1.28 if (st % (step * 2) == 0) else 1.0)
                ce_bias = 1.45 if dist >= 0 else 0.55
                pe_bias = 1.45 if dist <= 0 else 0.55
                decay = math.exp(-((abs(dist) / (rng * 0.72)) ** 2))

                ce_lots = int(max(800, random.uniform(18000, 48000) * decay * ce_bias * round_bonus))
                pe_lots = int(max(800, random.uniform(18000, 48000) * decay * pe_bias * round_bonus))
                ce_chg_lots = int(ce_lots * random.uniform(-0.08, 0.24))
                pe_chg_lots = int(pe_lots * random.uniform(-0.08, 0.24))
            else:
                old = prev_map[st]
                ce_lots = max(500, int(old["ce_oi_lots"] + random.randint(-450, 650)))
                pe_lots = max(500, int(old["pe_oi_lots"] + random.randint(-450, 650)))
                ce_chg_lots = int(old["ce_chg_lots"] + random.randint(-250, 380))
                pe_chg_lots = int(old["pe_chg_lots"] + random.randint(-250, 380))

            intrinsic_ce = max(0.0, self.mid_price - st)
            intrinsic_pe = max(0.0, st - self.mid_price)
            time_val = max(4.0, (115.0 if self.symbol == "NIFTY" else 280.0) * math.exp(-abs(dist) / (step * 5.5)))

            rows.append({
                "strike": st,
                "is_atm": st == atm,
                "ce_oi": ce_lots * lot_sz,
                "pe_oi": pe_lots * lot_sz,
                "ce_oi_lots": ce_lots,
                "pe_oi_lots": pe_lots,
                "ce_chg_lots": ce_chg_lots,
                "pe_chg_lots": pe_chg_lots,
                "ce_ltp": round(intrinsic_ce + time_val, 2),
                "pe_ltp": round(intrinsic_pe + time_val, 2),
                "ce_iv": round(12.5 + abs(dist) / (step * 8.0), 1),
                "pe_iv": round(13.2 + abs(dist) / (step * 7.5), 1),
            })

        self._finalize_oi_summary_locked(rows, expiry_label="Demo Nearest Expiry")

    def _finalize_oi_summary_locked(self, rows: list[dict], expiry_label: str, full_pcr: float | None = None):
        if not rows:
            self.option_rows = []
            self.option_summary = {}
            return

        top_ce = sorted(rows, key=lambda r: r["ce_oi_lots"], reverse=True)[:3]
        top_pe = sorted(rows, key=lambda r: r["pe_oi_lots"], reverse=True)[:3]
        ce_rank_map = {r["strike"]: i + 1 for i, r in enumerate(top_ce)}
        pe_rank_map = {r["strike"]: i + 1 for i, r in enumerate(top_pe)}

        max_oi_lots = max([max(r["ce_oi_lots"], r["pe_oi_lots"]) for r in rows] + [1])
        for r in rows:
            r["ce_rank"] = ce_rank_map.get(r["strike"], 0)
            r["pe_rank"] = pe_rank_map.get(r["strike"], 0)

        tot_ce = sum(r["ce_oi_lots"] for r in rows)
        tot_pe = sum(r["pe_oi_lots"] for r in rows)
        pcr = full_pcr if full_pcr is not None else (round(tot_pe / tot_ce, 2) if tot_ce > 0 else 0.0)

        self.option_rows = rows
        self.option_expiry = expiry_label
        self.last_oi_ts = time.time()
        self.option_summary = {
            "expiry": expiry_label,
            "max_oi_lots": max_oi_lots,
            "max_ce_strike": top_ce[0]["strike"] if top_ce else 0,
            "max_ce_lots": top_ce[0]["ce_oi_lots"] if top_ce else 0,
            "max_pe_strike": top_pe[0]["strike"] if top_pe else 0,
            "max_pe_lots": top_pe[0]["pe_oi_lots"] if top_pe else 0,
            "top_ce_strikes": [{"strike": r["strike"], "lots": r["ce_oi_lots"]} for r in top_ce],
            "top_pe_strikes": [{"strike": r["strike"], "lots": r["pe_oi_lots"]} for r in top_pe],
            "total_ce_lots": tot_ce,
            "total_pe_lots": tot_pe,
            "pcr": round(pcr, 2),
        }

    def _demo_loop(self):
        while self.running:
            time.sleep(BOOK_REFRESH_SECONDS)
            with self.lock:
                if self.source == "demo":
                    self._tick_demo_book_locked()

    # --------------------------------------------------------------------------
    # UPSTOX LIVE INTEGRATION (Analytics Token + Official upstox-python-sdk)
    # --------------------------------------------------------------------------
    def _stop_streamer_quietly(self):
        st = self.streamer
        self.streamer = None
        if st is not None:
            try:
                st.disconnect()
            except Exception:
                pass

    def _start_upstox_session(self):
        with self.lock:
            self.session_id += 1
            sid = self.session_id
            self._stop_streamer_quietly()
            self.connection_status = "connecting"
            self.token_expired = False
            self.status_message = "Connecting to Upstox API using token from .env..."

        threading.Thread(target=self._upstox_worker, args=(sid,), daemon=True).start()

    def _is_session_active(self, sid: int) -> bool:
        with self.lock:
            return self.running and self.source == "upstox" and self.session_id == sid

    def _upstox_worker(self, sid: int):
        if not HAS_UPSTOX_SDK:
            with self.lock:
                if self.session_id == sid:
                    self.connection_status = "error"
                    self.status_message = "upstox-python-sdk is not installed. Run: pip install upstox-python-sdk python-dotenv"
            return

        token = get_upstox_token()
        if not token:
            with self.lock:
                if self.session_id == sid:
                    self.connection_status = "missing_token"
                    self.token_expired = True
                    self.status_message = (
                        "No Upstox Analytics Token found yet. "
                        "On Render: Dashboard -> Environment -> add UPSTOX_ANALYTICS_TOKEN -> Save. "
                        "On PC: paste after UPSTOX_ANALYTICS_TOKEN= in .env -> Save -> click 'Retry Upstox'."
                    )
            print("[Upstox] Waiting for UPSTOX_ANALYTICS_TOKEN in Environment Variables / .env.")
            return

        cfg = upstox_client.Configuration()
        cfg.access_token = token
        client = upstox_client.ApiClient(cfg)

        try:
            # 1. Resolve nearest-expiry NIFTY / BANKNIFTY Future & lot size
            fut = self._resolve_nearest_future(client, self.symbol)
            if not self._is_session_active(sid):
                return

            fut_key = fut["instrument_key"]
            fut_sym = fut["trading_symbol"]
            fut_exp = fut["expiry"]
            fut_lot = int(fut.get("lot_size") or SYMBOL_DEFAULTS[self.symbol]["lot_size"])
            expected_levels = 30 if self.depth_mode == "full_d30" else 5

            with self.lock:
                self.instrument_key = fut_key
                self.contract_name = fut_sym
                self.future_expiry = fut_exp
                self.lot_size = fut_lot
                self.feed_levels = expected_levels
                self.status_message = (
                    f"Resolved {fut_sym} (Expiry {fut_exp}, Lot Size {fut_lot}). "
                    f"Loading initial candles, quote & {expected_levels}-level WebSocket depth..."
                )

            print(f"[Upstox] Nearest future: {fut_sym} | Key: {fut_key} | Expiry: {fut_exp} | Lot size: {fut_lot}")
            print(f"[Upstox] {UPSTOX_RATE_LIMIT_NOTE}")

            # 2. Load recent 1-minute candles so chart starts populated immediately
            self._load_upstox_initial_candles(client, fut_key, sid)

            # 3. Load initial REST market quote & depth snapshot (works even after market hours!)
            self._load_upstox_initial_quote(client, fut_key, sid)

            # 4. Fetch Option Chain OI immediately, then repeat every 60s in background
            self._fetch_upstox_option_chain(client, sid)
            threading.Thread(target=self._upstox_oi_poller, args=(client, sid), daemon=True).start()

            # 5. Connect WebSocket MarketDataStreamerV3 with 10s auto-reconnect loop
            self._run_upstox_websocket_loop(client, fut_key, sid)

        except ApiException as exc:
            expired, msg = parse_upstox_error(exc)
            print(f"[Upstox] {msg}")
            with self.lock:
                if self.session_id == sid:
                    self.connection_status = "token_expired" if expired else "error"
                    self.token_expired = expired
                    self.status_message = msg
        except Exception as exc:
            msg = f"Could not connect to Upstox: {str(exc)[:180]}"
            print(f"[Upstox] {msg}")
            with self.lock:
                if self.session_id == sid:
                    self.connection_status = "error"
                    self.status_message = msg

    def _resolve_nearest_future(self, client, symbol: str) -> dict:
        defaults = SYMBOL_DEFAULTS.get(symbol.upper(), SYMBOL_DEFAULTS["NIFTY"])
        exch = defaults.get("exchange", "NSE")
        inst_api = upstox_client.InstrumentsApi(client)
        resp = inst_api.search_instrument(
            symbol,
            exchanges=exch,
            segments="FO",
            instrument_types="FUT",
            records=30,
        )
        raw_list = resp.data or []
        matches = []
        for item in raw_list:
            u_sym = str(extract_field(item, "underlying_symbol", default="")).upper()
            t_sym = str(extract_field(item, "trading_symbol", default="")).upper()
            is_exact = u_sym == symbol.upper()
            is_sensex_alias = (
                symbol.upper() == "SENSEX"
                and u_sym in ("SENSEX", "BSX")
                and "SENSEX50" not in t_sym
                and "BANKEX" not in t_sym
            )
            if is_exact or is_sensex_alias:
                exp = normalize_expiry(extract_field(item, "expiry", default=""))
                matches.append({
                    "instrument_key": extract_field(item, "instrument_key", default=""),
                    "trading_symbol": extract_field(item, "trading_symbol", default=f"{symbol} FUT"),
                    "expiry": exp,
                    "lot_size": int(extract_field(item, "lot_size", default=defaults["lot_size"]) or defaults["lot_size"]),
                })

        if not matches:
            raise RuntimeError(f"No {exch} FO futures found for {symbol}.")

        today_s = now_ist().strftime("%Y-%m-%d")
        upcoming = [m for m in matches if m["expiry"] >= today_s]
        chosen_pool = sorted(upcoming if upcoming else matches, key=lambda m: m["expiry"])
        return chosen_pool[0]

    def _load_upstox_initial_candles(self, client, fut_key: str, sid: int):
        try:
            hist_api = upstox_client.HistoryV3Api(client)
            resp = hist_api.get_intra_day_candle_data(fut_key, "minutes", 1)
            raw_candles = extract_field(resp, "data", "candles", default=[]) or []

            if not raw_candles:
                # Outside market hours / weekend: load most recent session's 1-min candles
                to_dt = now_ist().date()
                from_dt = to_dt - timedelta(days=6)
                resp = hist_api.get_historical_candle_data1(
                    fut_key, "minutes", 1, to_dt.isoformat(), from_dt.isoformat()
                )
                raw_candles = extract_field(resp, "data", "candles", default=[]) or []

            parsed = []
            for row in raw_candles:
                if len(row) >= 5:
                    ts_str = str(row[0])
                    dt = datetime.fromisoformat(ts_str)
                    bucket = (int(dt.timestamp()) // 60) * 60
                    parsed.append({
                        "time": bucket,
                        "open": round(float(row[1]), 2),
                        "high": round(float(row[2]), 2),
                        "low": round(float(row[3]), 2),
                        "close": round(float(row[4]), 2),
                    })

            parsed.sort(key=lambda c: c["time"])
            if parsed:
                with self.lock:
                    if self.session_id == sid:
                        self.candles = parsed[-MAX_CANDLES:]
                        last_c = self.candles[-1]["close"]
                        self.ltp = last_c
                        self.mid_price = last_c
        except Exception as exc:
            print(f"[Upstox] Historical candle preload skipped: {str(exc)[:120]}")

    def _load_upstox_initial_quote(self, client, fut_key: str, sid: int):
        try:
            mq_api = upstox_client.MarketQuoteApi(client)
            resp = mq_api.get_full_market_quote(fut_key, "2.0")
            data = resp.data or {}
            entry = None
            for val in data.values():
                tok = extract_field(val, "instrument_token", default="")
                if tok == fut_key or entry is None:
                    entry = val

            if not entry:
                return

            ltp = float(extract_field(entry, "last_price", default=0.0) or 0.0)
            net_chg = float(extract_field(entry, "net_change", default=0.0) or 0.0)
            buy_depth = extract_field(entry, "depth", "buy", default=[]) or []
            sell_depth = extract_field(entry, "depth", "sell", default=[]) or []

            bids = []
            asks = []
            for d in buy_depth:
                p = float(extract_field(d, "price", default=0.0) or 0.0)
                q = int(float(extract_field(d, "quantity", default=0) or 0))
                if p > 0 and q > 0:
                    bids.append((p, q))
            for d in sell_depth:
                p = float(extract_field(d, "price", default=0.0) or 0.0)
                q = int(float(extract_field(d, "quantity", default=0) or 0))
                if p > 0 and q > 0:
                    asks.append((p, q))

            with self.lock:
                if self.session_id != sid:
                    return
                if ltp > 0:
                    self.ltp = round(ltp, 2)
                    self.prev_close = round(ltp - net_chg, 2)
                    self.mid_price = round(ltp, 2)
                if bids:
                    self.bids = bids
                    self.best_bid = bids[0][0]
                if asks:
                    self.asks = asks
                    self.best_ask = asks[0][0]
                if bids and asks:
                    self.mid_price = round((self.best_bid + self.best_ask) / 2.0, 2)
                if bids or asks:
                    self.feed_levels = max(len(bids), len(asks), self.feed_levels)
                    self.last_book_ts = time.time()
                    self._update_big_orders_locked()
        except Exception as exc:
            print(f"[Upstox] Initial quote snapshot skipped: {str(exc)[:120]}")

    def _upstox_oi_poller(self, client, sid: int):
        while self._is_session_active(sid):
            time.sleep(OI_REFRESH_SECONDS)
            if not self._is_session_active(sid):
                break
            self._fetch_upstox_option_chain(client, sid)

    def _fetch_upstox_option_chain(self, client, sid: int):
        try:
            underlying_key = UNDERLYING_INDEX_KEYS.get(self.symbol, "NSE_INDEX|Nifty 50")
            opt_api = upstox_client.OptionsApi(client)

            # Discover nearest valid option expiry
            expiries = []
            try:
                contracts_resp = opt_api.get_option_contracts(underlying_key)
                contracts = contracts_resp.data or []
                expiries = sorted({
                    normalize_expiry(extract_field(c, "expiry", default=""))
                    for c in contracts
                    if normalize_expiry(extract_field(c, "expiry", default=""))
                })
            except Exception:
                expiries = []

            if not expiries:
                exch = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"]).get("exchange", "NSE")
                inst_api = upstox_client.InstrumentsApi(client)
                for exp_hint in ("current_week", "current_month"):
                    s_resp = inst_api.search_instrument(
                        self.symbol, exchanges=exch, segments="FO",
                        instrument_types="CE", expiry=exp_hint, records=10
                    )
                    for hit in (s_resp.data or []):
                        e_str = normalize_expiry(extract_field(hit, "expiry", default=""))
                        if e_str:
                            expiries.append(e_str)
                    if expiries:
                        expiries = sorted(set(expiries))
                        break

            today_s = now_ist().strftime("%Y-%m-%d")
            valid_exps = [e for e in expiries if e >= today_s]
            nearest_exp = (valid_exps or expiries)[0] if (valid_exps or expiries) else ""

            if not nearest_exp:
                return

            chain_resp = opt_api.get_put_call_option_chain(underlying_key, nearest_exp)
            chain = chain_resp.data or []
            if not isinstance(chain, list):
                chain = [chain]
            if not chain:
                return

            defaults = SYMBOL_DEFAULTS.get(self.symbol, SYMBOL_DEFAULTS["NIFTY"])
            rng = defaults["oi_range"]

            spot = 0.0
            for entry in chain:
                spot = float(extract_field(entry, "underlying_spot_price", default=0.0) or 0.0)
                if spot > 0:
                    break

            with self.lock:
                if spot > 0 and self.session_id == sid:
                    self.spot_price = round(spot, 2)
                    if self.mid_price > 0:
                        self.spot_basis = round(self.mid_price - spot, 2)
                ref_price = self.mid_price if self.mid_price > 0 else spot
                lot_sz = max(1, self.lot_size)

            center_price = spot if spot > 0 else ref_price
            all_ce_oi = 0.0
            all_pe_oi = 0.0
            parsed_all = []

            for entry in chain:
                strike_f = float(extract_field(entry, "strike_price", default=0.0) or 0.0)
                if strike_f <= 0:
                    continue
                strike = int(round(strike_f)) if abs(strike_f - round(strike_f)) < 1e-4 else strike_f

                ce = extract_field(entry, "call_options")
                pe = extract_field(entry, "put_options")

                ce_oi = float(extract_field(ce, "market_data", "oi", default=0.0) or 0.0)
                ce_prev_oi = float(extract_field(ce, "market_data", "prev_oi", default=ce_oi) or 0.0)
                ce_ltp = float(extract_field(ce, "market_data", "ltp", default=0.0) or 0.0)
                ce_iv = float(extract_field(ce, "option_greeks", "iv", default=0.0) or 0.0)

                pe_oi = float(extract_field(pe, "market_data", "oi", default=0.0) or 0.0)
                pe_prev_oi = float(extract_field(pe, "market_data", "prev_oi", default=pe_oi) or 0.0)
                pe_ltp = float(extract_field(pe, "market_data", "ltp", default=0.0) or 0.0)
                pe_iv = float(extract_field(pe, "option_greeks", "iv", default=0.0) or 0.0)

                all_ce_oi += ce_oi
                all_pe_oi += pe_oi

                if abs(strike_f - center_price) <= rng:
                    parsed_all.append({
                        "strike": strike,
                        "is_atm": False,
                        "ce_oi": int(ce_oi),
                        "pe_oi": int(pe_oi),
                        "ce_oi_lots": int(round(ce_oi / lot_sz)),
                        "pe_oi_lots": int(round(pe_oi / lot_sz)),
                        "ce_chg_lots": int(round((ce_oi - ce_prev_oi) / lot_sz)),
                        "pe_chg_lots": int(round((pe_oi - pe_prev_oi) / lot_sz)),
                        "ce_ltp": round(ce_ltp, 2),
                        "pe_ltp": round(pe_ltp, 2),
                        "ce_iv": round(ce_iv * 100.0 if 0 < ce_iv < 1.5 else ce_iv, 1),
                        "pe_iv": round(pe_iv * 100.0 if 0 < pe_iv < 1.5 else pe_iv, 1),
                    })

            parsed_all.sort(key=lambda r: r["strike"])
            if parsed_all:
                atm_row = min(parsed_all, key=lambda r: abs(r["strike"] - center_price))
                atm_row["is_atm"] = True

            full_pcr = round(all_pe_oi / all_ce_oi, 2) if all_ce_oi > 0 else 0.0

            with self.lock:
                if self.session_id == sid:
                    self._finalize_oi_summary_locked(parsed_all, expiry_label=nearest_exp, full_pcr=full_pcr)
        except ApiException as exc:
            expired, msg = parse_upstox_error(exc)
            print(f"[Upstox OI] {msg}")
            if expired:
                with self.lock:
                    if self.session_id == sid:
                        self.connection_status = "token_expired"
                        self.token_expired = True
                        self.status_message = msg
        except Exception as exc:
            print(f"[Upstox OI] Refresh warning: {str(exc)[:140]}")

    def _run_upstox_websocket_loop(self, client, fut_key: str, sid: int):
        while self._is_session_active(sid):
            closed_event = threading.Event()
            mode = self.depth_mode

            streamer = upstox_client.MarketDataStreamerV3(client, [fut_key], mode)
            with self.lock:
                if self.session_id != sid:
                    return
                self.streamer = streamer

            def on_open():
                levels_label = 30 if mode == "full_d30" else 5
                mkt_note = (
                    "Live market feed streaming."
                    if is_market_open_ist()
                    else "Connected! Note: NSE F&O market hours are 09:15–15:30 IST (Mon–Fri), so live depth stays quiet outside market hours."
                )
                with self.lock:
                    if self.session_id == sid:
                        self.connection_status = "live_connected"
                        self.token_expired = False
                        self.feed_levels = levels_label
                        self.status_message = f"Upstox WebSocket connected ({levels_label} depth levels, mode='{mode}'). {mkt_note}"
                print(f"[Upstox WS] Connected & subscribed to {fut_key} in '{mode}' ({levels_label} levels) mode.")

            def on_message(msg):
                if not isinstance(msg, dict):
                    return
                feeds = msg.get("feeds") or {}
                feed = feeds.get(fut_key)
                if not feed and feeds:
                    feed = next(iter(feeds.values()))
                if not isinstance(feed, dict):
                    return

                full_feed = feed.get("fullFeed") or {}
                mff = full_feed.get("marketFF") or {}
                ltpc = mff.get("ltpc") or feed.get("ltpc") or {}

                ltp = float(ltpc.get("ltp") or 0.0)
                ltq = float(ltpc.get("ltq") or 0.0)
                cp = float(ltpc.get("cp") or 0.0)
                vtt = float(mff.get("vtt") or 0.0)
                quotes = (mff.get("marketLevel") or {}).get("bidAskQuote") or []

                bids = []
                asks = []
                for q in quotes:
                    bp = float(q.get("bidP") or 0.0)
                    bq = int(float(q.get("bidQ") or 0))
                    ap = float(q.get("askP") or 0.0)
                    aq = int(float(q.get("askQ") or 0))
                    if bp > 0 and bq > 0:
                        bids.append((bp, bq))
                    if ap > 0 and aq > 0:
                        asks.append((ap, aq))

                with self.lock:
                    if self.session_id != sid:
                        return
                    prev_mid = self.mid_price or ltp
                    prev_ask = self.best_ask or prev_mid
                    prev_bid = self.best_bid or prev_mid

                    trade_qty = 0.0
                    if vtt > 0:
                        if self._last_vtt > 0 and vtt > self._last_vtt:
                            trade_qty = vtt - self._last_vtt
                        self._last_vtt = vtt
                    elif ltq > 0:
                        trade_qty = ltq

                    if ltp > 0:
                        self.ltp = round(ltp, 2)
                    if cp > 0:
                        self.prev_close = round(cp, 2)
                    if bids:
                        self.bids = bids
                        self.best_bid = bids[0][0]
                    if asks:
                        self.asks = asks
                        self.best_ask = asks[0][0]

                    if quotes:
                        self.feed_levels = len(quotes)

                    if self.bids and self.asks:
                        mid = round((self.best_bid + self.best_ask) / 2.0, 2)
                    else:
                        mid = self.ltp

                    trade_lots = trade_qty / max(1, self.lot_size) if trade_qty > 0 else 0.0
                    trade_side = "BUY" if (ltp >= prev_ask or ltp >= prev_mid) else "SELL"
                    self._update_order_flow_locked(ltp, trade_lots, trade_side)

                    if mid > 0:
                        self.mid_price = mid
                        self._update_candle_locked(mid)
                    self.last_book_ts = time.time()
                    self._update_big_orders_locked()

            def on_error(err):
                err_str = str(err)
                print(f"[Upstox WS] Error: {err_str[:160]}")
                if "401" in err_str or "Unauthorized" in err_str or "403" in err_str:
                    with self.lock:
                        if self.session_id == sid:
                            self.connection_status = "token_expired"
                            self.token_expired = True
                            self.status_message = (
                                "Upstox WebSocket rejected the token (HTTP 401/403). "
                                "Generate a fresh Analytics Token at https://account.upstox.com/developer/apps#analytics, "
                                "paste it into .env, and click 'Retry Upstox'."
                            )
                closed_event.set()

            def on_close(*_args):
                closed_event.set()

            streamer.on("open", on_open)
            streamer.on("message", on_message)
            streamer.on("error", on_error)
            streamer.on("close", on_close)

            try:
                streamer.connect()
                while self._is_session_active(sid) and not closed_event.wait(timeout=1.0):
                    pass
            except Exception as exc:
                print(f"[Upstox WS] Connection exception: {str(exc)[:140]}")

            self._stop_streamer_quietly()
            if not self._is_session_active(sid):
                break

            with self.lock:
                if self.token_expired:
                    # Do not spam reconnects when token is invalid/expired
                    break
                self.connection_status = "reconnecting"
                self.status_message = f"Upstox WebSocket dropped. Reconnecting in {RECONNECT_DELAY_SECONDS} seconds..."

            print(f"[Upstox WS] Disconnected. Reconnecting in {RECONNECT_DELAY_SECONDS}s...")
            for _ in range(RECONNECT_DELAY_SECONDS):
                if not self._is_session_active(sid):
                    return
                time.sleep(1.0)

    # --------------------------------------------------------------------------
    # RUNTIME CONTROLS & SNAPSHOT BUILDER
    # --------------------------------------------------------------------------
    def apply_control(self, payload: dict) -> dict:
        restart_upstox = False
        with self.lock:
            new_source = str(payload.get("source") or self.source).lower()
            new_symbol = str(payload.get("symbol") or self.symbol).upper()
            new_band = float(payload.get("band_size") or self.band_size)
            new_depth_mode = str(payload.get("depth_mode") or self.depth_mode)
            force_reconnect = bool(payload.get("reconnect", False))

            if new_band > 0:
                self.band_size = new_band

            if new_depth_mode in ("full", "full_d30") and new_depth_mode != self.depth_mode:
                self.depth_mode = new_depth_mode
                if new_source == "upstox":
                    restart_upstox = True

            source_changed = new_source in ("demo", "upstox") and new_source != self.source
            symbol_changed = new_symbol in SYMBOL_DEFAULTS and new_symbol != self.symbol

            if symbol_changed and "band_size" not in payload:
                self.band_size = float(SYMBOL_DEFAULTS[new_symbol].get("default_band", 1.0))

            if source_changed or symbol_changed or force_reconnect:
                self.session_id += 1
                self._stop_streamer_quietly()
                self.source = new_source if new_source in ("demo", "upstox") else self.source
                self.symbol = new_symbol if new_symbol in SYMBOL_DEFAULTS else self.symbol
                self._reset_symbol_state(self.symbol)
                if self.source == "upstox":
                    restart_upstox = True

        if restart_upstox:
            self._start_upstox_session()

        return self.get_state_snapshot()

    def get_state_snapshot(self) -> dict:
        token_configured = bool(get_upstox_token())
        with self.lock:
            ladder = build_grouped_ladder(
                bids=self.bids,
                asks=self.asks,
                band_size=self.band_size,
                lot_size=self.lot_size,
                highlight_top_n=HIGHLIGHT_TOP_N,
                mid_price=self.mid_price,
            )
            # Enrich ladder rows (plus any active pulled/traded levels) with order-flow metrics
            row_map = {round(float(r["price"]), 2): r for r in ladder["rows"]}
            bs_now = max(0.5, float(self.band_size or 5.0))
            mid_snap = round((self.mid_price or 25000.0) / bs_now) * bs_now
            for i in range(-18, 19):
                p_k = round(mid_snap + i * bs_now, 2)
                if p_k not in row_map:
                    row_map[p_k] = {
                        "price": p_k,
                        "buy_lots": 0.0,
                        "sell_lots": 0.0,
                        "buy_rank": 0,
                        "sell_rank": 0,
                        "buy_highlight": False,
                        "sell_highlight": False,
                    }

            non_zero = [
                v for r in row_map.values()
                for v in (r["buy_lots"], r["sell_lots"])
                if v > 0
            ]
            usual_row = statistics.median(non_zero) if len(non_zero) >= 4 else 35.0
            big_thresh = max(usual_row * 1.9, 55.0)

            enriched_rows = []
            for p_k in sorted(row_map.keys(), reverse=True):
                r = dict(row_map[p_k])
                pb = int(round(self._pulled_buy.get(p_k, 0.0)))
                ps = int(round(self._pulled_sell.get(p_k, 0.0)))
                bt = int(round(self._bought_lots.get(p_k, 0.0)))
                sd = int(round(self._sold_lots.get(p_k, 0.0)))
                r["pulled_buy"] = pb
                r["pulled_sell"] = ps
                r["pulled_total"] = pb + ps
                r["bought_lots"] = bt
                r["sold_lots"] = sd
                r["big_buyer"] = bool(r["buy_lots"] >= big_thresh)
                r["big_seller"] = bool(r["sell_lots"] >= big_thresh)
                enriched_rows.append(r)

            ladder["rows"] = enriched_rows
            ladder["usual_row_lots"] = round(usual_row, 1)

            chg = round(self.ltp - self.prev_close, 2) if self.prev_close else 0.0
            chg_pct = round((chg / self.prev_close) * 100.0, 2) if self.prev_close else 0.0
            spread = round(self.best_ask - self.best_bid, 2) if (self.best_ask > 0 and self.best_bid > 0) else 0.0
            oi_age = int(max(0, time.time() - self.last_oi_ts)) if self.last_oi_ts else 0
            book_age = int(max(0, time.time() - self.last_book_ts)) if self.last_book_ts else 0
            big_orders_out = dict(self.big_orders_snapshot)
            big_orders_out["updated_ago_sec"] = book_age

            return {
                "server_time_ist": now_ist().strftime("%H:%M:%S IST"),
                "candle_sec_into_min": int(time.time()) % 60,
                "market_open": is_market_open_ist(),
                "source": self.source,
                "symbol": self.symbol,
                "contract_name": self.contract_name,
                "future_expiry": self.future_expiry,
                "lot_size": self.lot_size,
                "band_size": self.band_size,
                "depth_mode": self.depth_mode,
                "feed_levels": self.feed_levels,
                "highlight_top_n": HIGHLIGHT_TOP_N,
                "token_configured": token_configured,
                "token_expired": self.token_expired,
                "can_setup_deploy": not bool(os.environ.get("RENDER")),
                "connection_status": self.connection_status,
                "status_message": self.status_message,
                "rate_limit_note": UPSTOX_RATE_LIMIT_NOTE,
                "ltp": self.ltp,
                "mid_price": self.mid_price,
                "spot_price": self.spot_price,
                "best_bid": self.best_bid,
                "best_ask": self.best_ask,
                "spread": spread,
                "change": chg,
                "change_pct": chg_pct,
                "book": ladder,
                "big_orders": big_orders_out,
                "candles": list(self.candles),
                "option_chain": {
                    "rows": list(self.option_rows),
                    "summary": dict(self.option_summary),
                    "refresh_seconds": OI_REFRESH_SECONDS,
                    "age_seconds": oi_age,
                },
            }


# ==============================================================================
# HTTP SERVER (Serves index.html + /api/state + /api/control)
# ==============================================================================
ENGINE = MarketScreenEngine()


class ScreenRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Quiet request logging so terminal stays clean and readable
        pass

    def _check_auth(self) -> bool:
        """If APP_PASSWORD is set (e.g. on Render), enforce HTTP Basic Auth for privacy."""
        required_pw = get_app_password()
        if not required_pw:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8", errors="ignore")
                _, _, provided_pw = decoded.partition(":")
                if hmac.compare_digest(provided_pw, required_pw):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Personal NIFTY Order Book Screen"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Password required (enter anything as username, and your APP_PASSWORD as password).")
        return False

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if not self._check_auth():
            return
        if parsed.path in ("/", "/index.html"):
            if not INDEX_HTML_PATH.exists():
                self.send_error(404, "index.html not found")
                return
            html = INDEX_HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif parsed.path == "/api/state":
            self._send_json(200, ENGINE.get_state_snapshot())
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}

        if parsed.path == "/api/control":
            snapshot = ENGINE.apply_control(payload)
            self._send_json(200, snapshot)
        else:
            self.send_error(404, "Not found")


def main():
    env_cfg = load_env_config()
    bind_host = env_cfg.get("HOST") or HOST
    bind_port = int(env_cfg.get("PORT") or PORT)

    server = ThreadingHTTPServer((bind_host, bind_port), ScreenRequestHandler)
    print("=" * 72)
    print("  PERSONAL NIFTY ORDER BOOK SCREEN")
    print(f"  Open in browser : http://127.0.0.1:{bind_port}")
    print(f"  Active source   : {ENGINE.source.upper()}  (Switch anytime from top bar)")
    print("  Stop server     : Press Ctrl+C")
    print("=" * 72)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping NIFTY Order Book Screen...")
        ENGINE.running = False
        ENGINE._stop_streamer_quietly()
        server.server_close()


if __name__ == "__main__":
    main()
