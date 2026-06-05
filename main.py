# ForexAI VPA Bot - v1.1 (fixed candle fetch)
import os, time, logging, math
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

OANDA_API_KEY    = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
PAPER_MODE       = os.environ.get("PAPER_MODE", "true").lower() == "true"
OANDA_ENV        = "practice" if PAPER_MODE else "live"

SYMBOLS = ["EUR_USD", "GBP_USD", "USD_JPY"]

STRATEGY = {
    "stop_loss_pips": 15, "take_profit_pips": 30,
    "position_units": 10000, "cooldown_minutes": 15,
    "volume_avg_period": 20, "volume_spike_mult": 1.5,
    "min_close_ratio": 0.6, "effort_result_ratio": 0.02,
    "min_score": 3
}

bot_state = {
    "running": True, "killed": False, "positions": {},
    "closed_trades": [], "diary": [], "day_pnl": 0.0,
    "total_trades": 0, "win_count": 0, "signals": {s: {} for s in SYMBOLS},
    "account_balance": 0.0, "account_equity": 0.0, "account_nav": 0.0,
    "active_cooldowns": {}, "market_open": False,
    "market_regime": "UNKNOWN", "version": "ForexVPA-1.1"
}

def get_oanda_client():
    import oandapyV20
    return oandapyV20.API(access_token=OANDA_API_KEY, environment=OANDA_ENV)

def get_candles(symbol, granularity="M5", count=100):
    """Fetch candles using only count - no from/to conflict"""
    try:
        import oandapyV20.endpoints.instruments as instruments
        client = get_oanda_client()
        params = {"granularity": granularity, "count": count, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)
        candles = r.response.get("candles", [])
        result = []
        for c in candles:
            if c.get("complete", False):
                m = c["mid"]
                result.append({
                    "time": c["time"],
                    "open": float(m["o"]), "high": float(m["h"]),
                    "low": float(m["l"]), "close": float(m["c"]),
                    "volume": int(c.get("volume", 0))
                })
        return result
    except Exception as e:
        log.error(f"Candles error {symbol}: {e}")
        return []

def pip_value(symbol):
    return 0.0001 if "JPY" not in symbol else 0.01

def calc_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def is_market_open():
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    h = now.hour + now.minute / 60
    if wd == 4 and h >= 21: return False
    if wd == 5: return False
    if wd == 6 and h < 21: return False
    return True

def get_account_info():
    try:
        import oandapyV20.endpoints.accounts as accounts
        client = get_oanda_client()
        r = accounts.AccountSummary(OANDA_ACCOUNT_ID)
        client.request(r)
        acct = r.response["account"]
        bot_state["account_balance"] = float(acct.get("balance", 0))
        bot_state["account_nav"]     = float(acct.get("NAV", 0))
        bot_state["account_equity"]  = float(acct.get("NAV", 0))
    except Exception as e:
        log.error(f"Account info error: {e}")

def sync_positions():
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.OpenTrades(OANDA_ACCOUNT_ID)
        client.request(r)
        open_trades = r.response.get("trades", [])
        synced = {}
        for t in open_trades:
            sym = t["instrument"]
            synced[sym] = {
                "symbol": sym, "entry": float(t["price"]),
                "units": int(t["currentUnits"]), "trade_id": t["id"],
                "open_time": t.get("openTime", datetime.now(timezone.utc).isoformat()),
                "current_price": float(t["price"]),
                "unrealized_pnl": float(t.get("unrealizedPL", 0))
            }
        bot_state["positions"] = synced
    except Exception as e:
        log.error(f"Sync positions error: {e}")

def place_order(symbol, units, side):
    try:
        import oandapyV20.endpoints.orders as orders
        client = get_oanda_client()
        actual_units = units if side == "BUY" else -units
        data = {"order": {"type": "MARKET", "instrument": symbol, "units": str(actual_units)}}
        r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=data)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
        return None

def close_position(symbol, trade_id):
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.TradeClose(OANDA_ACCOUNT_ID, trade_id)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Close position error {symbol}: {e}")
        return None

def add_diary(symbol, text, entry_type="info"):
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M"), "symbol": symbol, "text": text, "type": entry_type}
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 200:
        bot_state["diary"] = bot_state["diary"][:200]

def check_market_regime():
    """Use EUR_USD daily 200 EMA as regime filter for forex"""
    try:
        candles = get_candles("EUR_USD", "D", 210)
        if len(candles) < 200:
            return "UNKNOWN"
        closes = [c["close"] for c in candles]
        ema200 = calc_ema(closes, 200)
        if not ema200:
            return "UNKNOWN"
        price = closes[-1]
        return "BULL" if price > ema200[-1] else "BEAR"
    except Exception as e:
        log.error(f"Regime check error: {e}")
        return "UNKNOWN"

def generate_vpa_signal(symbol):
    try:
        candles = get_candles(symbol, "M5", 60)
        if len(candles) < 25:
            return {}

        volumes = [c["volume"] for c in candles]
        closes  = [c["close"]  for c in candles]
        opens   = [c["open"]   for c in candles]
        highs   = [c["high"]   for c in candles]
        lows    = [c["low"]    for c in candles]

        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        curr_vol = volumes[-1]
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1

        price = closes[-1]
        curr_open  = opens[-1]
        curr_high  = highs[-1]
        curr_low   = lows[-1]
        curr_close = closes[-1]

        bar_range = curr_high - curr_low
        if bar_range == 0:
            return {}

        close_ratio = (curr_close - curr_low) / bar_range

        # VPA signals
        buy_score = 0
        sell_score = 0
        signals_detected = []

        # Volume spike
        if vol_ratio >= STRATEGY["volume_spike_mult"]:
            if close_ratio >= STRATEGY["min_close_ratio"]:
                buy_score += 2
                signals_detected.append("VOL_SPIKE_BULL")
            else:
                sell_score += 2
                signals_detected.append("VOL_SPIKE_BEAR")

        # Absorption (big volume, tiny move)
        price_move_pct = bar_range / price if price > 0 else 0
        if vol_ratio >= 2.0 and price_move_pct < STRATEGY["effort_result_ratio"]:
            if curr_close > curr_open:
                buy_score += 2
                signals_detected.append("ABSORPTION_BULL")
            else:
                sell_score += 2
                signals_detected.append("ABSORPTION_BEAR")

        # No supply (low volume up bar)
        if vol_ratio < 0.7 and curr_close > curr_open and close_ratio > 0.5:
            buy_score += 1
            signals_detected.append("NO_SUPPLY")

        # No demand (low volume down bar)
        if vol_ratio < 0.7 and curr_close < curr_open and close_ratio < 0.5:
            sell_score += 1
            signals_detected.append("NO_DEMAND")

        # Trend confirmation via EMA
        ema20 = calc_ema(closes, 20)
        if ema20:
            if price > ema20[-1]:
                buy_score += 1
            else:
                sell_score += 1

        return {
            "price": price, "vol_ratio": round(vol_ratio, 2),
            "close_ratio": round(close_ratio, 2),
            "buy_score": buy_score, "sell_score": sell_score,
            "signals": signals_detected
        }
    except Exception as e:
        log.error(f"VPA signal error {symbol}: {e}")
        return {}

def trading_loop():
    add_diary("SYSTEM", "ForexAI VPA Bot started | Vol spike: 1.5x | Min score: 3 | SL=15pips | TP=30pips | Cooldown=15min", "system")
    log.info("ForexAI VPA Bot v1.1 started")
    regime_check_time = None

    while True:
        try:
            if not is_market_open():
                bot_state["market_open"] = False
                time.sleep(60)
                continue

            bot_state["market_open"] = True
            get_account_info()
            sync_positions()
            now = datetime.now(timezone.utc)

            # Check regime every 30 minutes
            if not regime_check_time or (now - regime_check_time).total_seconds() > 1800:
                bot_state["market_regime"] = check_market_regime()
                regime_check_time = now
                log.info(f"Market regime: {bot_state['market_regime']}")

            # Clear expired cooldowns
            expired = [s for s, t in bot_state["active_cooldowns"].items()
                       if (now - datetime.fromisoformat(t)).total_seconds() > STRATEGY["cooldown_minutes"] * 60]
            for s in expired:
                del bot_state["active_cooldowns"][s]

            for symbol in SYMBOLS:
                if bot_state["killed"]:
                    break

                sig = generate_vpa_signal(symbol)
                bot_state["signals"][symbol] = sig

                if not sig:
                    continue

                pv = pip_value(symbol)
                price = sig["price"]

                log.info(f"{symbol} | price={price} vol_ratio={sig['vol_ratio']} BUY={sig['buy_score']} SELL={sig['sell_score']} signals={sig['signals']}")

                # Check exits
                if symbol in bot_state["positions"]:
                    pos = bot_state["positions"][symbol]
                    entry = pos["entry"]
                    pnl_pips = (price - entry) / pv

                    should_exit = False
                    reason = ""
                    if pnl_pips >= STRATEGY["take_profit_pips"]:
                        should_exit = True; reason = "Take profit"
                    elif pnl_pips <= -STRATEGY["stop_loss_pips"]:
                        should_exit = True; reason = "Stop loss"
                        bot_state["active_cooldowns"][symbol] = now.isoformat()
                    elif sig["sell_score"] >= STRATEGY["min_score"] and sig["sell_score"] > sig["buy_score"]:
                        should_exit = True; reason = "VPA SELL signal"

                    if should_exit:
                        exit_price = close_position(symbol, pos["trade_id"])
                        if exit_price:
                            pnl = (exit_price - entry) * pos["units"]
                            win = pnl > 0
                            bot_state["day_pnl"] += pnl
                            bot_state["total_trades"] += 1
                            if win: bot_state["win_count"] += 1
                            bot_state["closed_trades"].append({"symbol": symbol, "entry": entry, "exit": exit_price,
                                "pnl": round(pnl,2), "pips": round(pnl_pips,1), "win": win, "reason": reason})
                            add_diary(symbol, f"{'WIN' if win else 'LOSS'} | {entry:.5f} -> {exit_price:.5f} | {round(pnl_pips,1)} pips | ${round(pnl,2)} | {reason}",
                                      "win" if win else "loss")
                            del bot_state["positions"][symbol]

                elif symbol not in bot_state["active_cooldowns"] and not bot_state["killed"]:
                    regime_ok = bot_state["market_regime"] in ["BULL", "UNKNOWN"]
                    if sig["buy_score"] >= STRATEGY["min_score"] and sig["buy_score"] > sig["sell_score"] and regime_ok:
                        entry_price = place_order(symbol, STRATEGY["position_units"], "BUY")
                        if entry_price:
                            bot_state["positions"][symbol] = {"symbol": symbol, "entry": entry_price,
                                "units": STRATEGY["position_units"], "trade_id": "pending",
                                "open_time": now.isoformat(), "current_price": entry_price, "unrealized_pnl": 0}
                            sync_positions()
                            add_diary(symbol, f"BUY VPA | Entry {entry_price:.5f} | Vol {sig['vol_ratio']}x | Score {sig['buy_score']} | {sig['signals']}", "buy")

        except Exception as e:
            log.error(f"Loop error: {e}")

        time.sleep(60)

threading.Thread(target=trading_loop, daemon=True).start()

@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return r

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(),
                    "version": bot_state["version"], "market_open": bot_state["market_open"]})

@app.route("/status")
def status():
    get_account_info()
    wins = bot_state["win_count"]
    total = bot_state["total_trades"]
    return jsonify({
        "running": bot_state["running"], "killed": bot_state["killed"],
        "paper_mode": PAPER_MODE, "market_open": bot_state["market_open"],
        "positions": bot_state["positions"], "closed_trades": bot_state["closed_trades"][-50:],
        "diary": bot_state["diary"][-100:], "day_pnl": bot_state["day_pnl"],
        "total_trades": total, "win_rate": round(wins/total*100) if total > 0 else 0,
        "signals": bot_state["signals"], "strategy": STRATEGY,
        "account_balance": bot_state["account_balance"],
        "account_equity": bot_state["account_equity"],
        "account_nav": bot_state["account_nav"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "market_regime": bot_state["market_regime"],
        "version": bot_state["version"]
    })

@app.route("/diary")
def diary():
    return jsonify({"diary": bot_state["diary"]})

@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    status = "KILLED" if bot_state["killed"] else "RESUMED"
    add_diary("SYSTEM", f"Kill switch {status}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/bars")
def bars():
    symbol = request.args.get("symbol", "EUR_USD")
    tf = request.args.get("timeframe", "M5")
    candles = get_candles(symbol, tf, 150)
    result = [{"time": int(datetime.fromisoformat(c["time"].replace("Z","")).timestamp()),
               "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]} for c in candles]
    return jsonify(result)

@app.route("/")
def index():
    try:
        with open("index.html") as f:
            return f.read()
    except:
        return jsonify({"status": "ForexAI VPA Bot v1.1 running"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
