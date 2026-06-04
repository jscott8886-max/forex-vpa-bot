"""
ForexAI Bot 3 - Volume Price Analysis Strategy
Pairs: EUR/USD, GBP/USD, USD/JPY via OANDA API
"""
import os, time, logging, json, math
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.instruments as instruments

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_KEY     = os.getenv("OANDA_API_KEY", "")
ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID", "")
PAPER_MODE  = os.getenv("PAPER_MODE", "true").lower() == "true"
ENVIRONMENT = "practice" if PAPER_MODE else "live"
PAIRS       = ["EUR_USD", "GBP_USD", "USD_JPY"]
STATE_FILE  = "/tmp/forex_vpa_state.json"
PIP_MULT    = {"EUR_USD": 10000, "GBP_USD": 10000, "USD_JPY": 100}

STRATEGY = {
    "stop_loss_pips":      15,
    "take_profit_pips":    30,
    "position_units":      10000,
    "volume_spike_mult":   1.5,   # lower threshold for forex (less volume data)
    "volume_avg_period":   20,
    "min_close_ratio":     0.6,
    "effort_result_ratio": 0.02,  # 0.02% for forex (tighter than crypto)
    "min_score":           3,
    "cooldown_minutes":    15,
}

bot_state = {
    "running":         True,
    "killed":          False,
    "positions":       {},
    "closed_trades":   [],
    "diary":           [],
    "day_pnl":         0.0,
    "total_trades":    0,
    "win_count":       0,
    "account_balance": 0.0,
    "account_equity":  0.0,
    "signals":         {},
    "market_open":     False,
    "cooldowns":       {},
    "last_signal_data":{},
}

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "diary":         bot_state["diary"][-200:],
                "closed_trades": bot_state["closed_trades"][-100:],
                "day_pnl":       bot_state["day_pnl"],
                "total_trades":  bot_state["total_trades"],
                "win_count":     bot_state["win_count"],
            }, f)
    except Exception as e:
        log.error(f"Save state error: {e}")

def diary_entry(symbol, text, entry_type="trade"):
    bot_state["diary"].append({
        "time":   datetime.now().strftime("%H:%M"),
        "symbol": symbol,
        "text":   text,
        "type":   entry_type,
    })
    save_state()

def get_oanda_client():
    return oandapyV20.API(access_token=API_KEY, environment=ENVIRONMENT)

def is_market_open():
    now = datetime.now(timezone.utc)
    day = now.weekday()
    hour = now.hour
    if day == 5: return False
    if day == 6 and hour < 21: return False
    if day == 4 and hour >= 21: return False
    return True

def get_account_data():
    try:
        client = get_oanda_client()
        r = accounts.AccountSummary(ACCOUNT_ID)
        client.request(r)
        acc = r.response["account"]
        bot_state["account_balance"] = float(acc.get("balance", 0))
        bot_state["account_equity"]  = float(acc.get("NAV", 0))
    except Exception as e:
        log.error(f"Account fetch error: {e}")

def sync_positions():
    try:
        client = get_oanda_client()
        r = trades.OpenTrades(ACCOUNT_ID)
        client.request(r)
        open_trades = r.response.get("trades", [])
        live_ids = set()
        for t in open_trades:
            inst = t["instrument"]
            live_ids.add(inst)
            if inst not in bot_state["positions"]:
                bot_state["positions"][inst] = {
                    "trade_id":       t["id"],
                    "entry":          float(t["price"]),
                    "units":          float(t["currentUnits"]),
                    "open_time":      t.get("openTime", "")[:16].replace("T", " "),
                    "symbol":         inst,
                    "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                }
            else:
                bot_state["positions"][inst]["unrealized_pnl"] = float(t.get("unrealizedPL", 0))
        for inst in list(bot_state["positions"].keys()):
            if inst not in live_ids:
                del bot_state["positions"][inst]
    except Exception as e:
        log.error(f"Position sync error: {e}")

def get_candles(pair, count=50, granularity="M5"):
    try:
        client = get_oanda_client()
        end   = datetime.now(timezone.utc)
        start = end - timedelta(hours=8)
        params = {
            "count": count,
            "granularity": granularity,
            "price": "M",
            "from": start.isoformat(),
            "to":   end.isoformat(),
        }
        r = instruments.InstrumentsCandles(pair, params=params)
        client.request(r)
        candles = r.response.get("candles", [])
        data = []
        for c in candles:
            if c.get("complete", False):
                mid = c["mid"]
                data.append({
                    "time":   c["time"],
                    "open":   float(mid["o"]),
                    "high":   float(mid["h"]),
                    "low":    float(mid["l"]),
                    "close":  float(mid["c"]),
                    "volume": int(c.get("volume", 0)),
                })
        if not data:
            return None
        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        log.error(f"Candles error {pair}: {e}")
        return None

def analyze_vpa(pair):
    try:
        df = get_candles(pair, count=50, granularity="M5")
        if df is None or len(df) < 25:
            return "HOLD", {}

        opens   = df["open"].values
        highs   = df["high"].values
        lows    = df["low"].values
        closes  = df["close"].values
        volumes = df["volume"].values

        current_price = float(closes[-1])
        avg_vol = float(np.mean(volumes[-STRATEGY["volume_avg_period"]-1:-1]))
        if avg_vol == 0:
            return "HOLD", {}

        curr_vol   = float(volumes[-1])
        curr_open  = float(opens[-1])
        curr_high  = float(highs[-1])
        curr_low   = float(lows[-1])
        curr_close = float(closes[-1])
        curr_range = curr_high - curr_low if curr_high != curr_low else 0.00001

        prev_vol   = float(volumes[-2])
        prev_open  = float(opens[-2])
        prev_close = float(closes[-2])

        curr_close_ratio = (curr_close - curr_low) / curr_range

        is_vol_spike      = curr_vol >= avg_vol * STRATEGY["volume_spike_mult"]
        is_prev_vol_spike = prev_vol >= avg_vol * STRATEGY["volume_spike_mult"]

        curr_bullish = curr_close > curr_open
        curr_bearish = curr_close < curr_open
        prev_bearish = prev_close < prev_open

        price_move_pct = abs(curr_close - curr_open) / curr_open * 100
        is_absorption  = is_prev_vol_spike and prev_bearish and price_move_pct < STRATEGY["effort_result_ratio"]

        is_narrow_range = curr_range / current_price < 0.0002  # tighter for forex
        is_no_supply    = is_narrow_range and curr_vol < avg_vol * 0.7 and curr_close > curr_open

        vol_rising   = all(volumes[-3+i] < volumes[-2+i] for i in range(2))
        price_rising = all(closes[-3+i] < closes[-2+i] for i in range(2))
        vol_trend_up = vol_rising and price_rising

        buy_score = sell_score = 0

        if is_vol_spike and curr_bullish and curr_close_ratio >= STRATEGY["min_close_ratio"]:
            buy_score += 2
        if is_absorption:
            buy_score += 2
        if is_no_supply:
            buy_score += 1
        if vol_trend_up:
            buy_score += 1
        if curr_close > prev_close and curr_vol > avg_vol:
            buy_score += 1

        if is_vol_spike and curr_bearish and curr_close_ratio <= (1 - STRATEGY["min_close_ratio"]):
            sell_score += 2
        if is_vol_spike and curr_bullish and curr_close_ratio < 0.3:
            sell_score += 2
        if curr_close < prev_close and curr_vol > avg_vol:
            sell_score += 1

        sig_data = {
            "price":       round(current_price, 5),
            "buy_score":   buy_score,
            "sell_score":  sell_score,
            "vol_spike":   bool(is_vol_spike),
            "absorption":  bool(is_absorption),
            "no_supply":   bool(is_no_supply),
            "vol_trend_up":bool(vol_trend_up),
            "curr_vol":    round(curr_vol, 0),
            "avg_vol":     round(avg_vol, 0),
            "vol_ratio":   round(curr_vol / avg_vol, 2) if avg_vol > 0 else 0,
            "close_ratio": round(curr_close_ratio, 2),
            "strategy":    "VPA",
            "signal":      "HOLD",
        }

        log.info(f"{pair} | price={current_price:.5f} vol_ratio={curr_vol/avg_vol:.1f}x BUY={buy_score} SELL={sell_score}")

        if buy_score >= STRATEGY["min_score"] and buy_score > sell_score:
            return "BUY", {**sig_data, "signal": "BUY"}
        elif sell_score >= STRATEGY["min_score"] and sell_score > buy_score:
            return "SELL", {**sig_data, "signal": "SELL"}
        return "HOLD", sig_data

    except Exception as e:
        log.error(f"VPA error {pair}: {e}")
        return "HOLD", {"price": 0, "strategy": "VPA"}

def is_in_cooldown(pair):
    cooldown_until = bot_state["cooldowns"].get(pair)
    if cooldown_until and datetime.now() < cooldown_until:
        return True
    return False

def set_cooldown(pair):
    bot_state["cooldowns"][pair] = datetime.now() + timedelta(minutes=STRATEGY["cooldown_minutes"])

def is_signal_stale(pair, sig_data):
    last = bot_state["last_signal_data"].get(pair)
    if last is None:
        bot_state["last_signal_data"][pair] = sig_data
        return False
    if (sig_data.get("buy_score") == last.get("buy_score") and
        sig_data.get("vol_ratio") == last.get("vol_ratio")):
        log.warning(f"{pair} — stale VPA signal, skipping")
        return True
    bot_state["last_signal_data"][pair] = sig_data
    return False

def place_order(pair, units, sl_price, tp_price):
    try:
        client = get_oanda_client()
        data = {
            "order": {
                "type":        "MARKET",
                "instrument":  pair,
                "units":       str(int(units)),
                "timeInForce": "FOK",
                "stopLossOnFill":   {"price": f"{sl_price:.5f}"},
                "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
            }
        }
        r = orders.Orders(ACCOUNT_ID, data=data)
        client.request(r)
        return r.response
    except Exception as e:
        log.error(f"Order error {pair}: {e}")
        return None

def close_trade(trade_id):
    try:
        client = get_oanda_client()
        r = trades.TradeClose(ACCOUNT_ID, tradeID=trade_id)
        client.request(r)
        return r.response
    except Exception as e:
        log.error(f"Close trade error: {e}")
        return None

def clean_nan(obj):
    if obj is None: return None
    if isinstance(obj, datetime): return obj.isoformat()
    if hasattr(obj, '__module__') and type(obj).__module__ == 'numpy':
        try: obj = obj.item()
        except: return 0
    if isinstance(obj, bool): return obj
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, int): return obj
    if isinstance(obj, str): return obj
    if isinstance(obj, dict): return {str(k): clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [clean_nan(v) for v in obj]
    try: return str(obj)
    except: return None

def trading_loop():
    if not API_KEY or not ACCOUNT_ID:
        log.warning("No OANDA credentials — bot idle")
        return

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    get_account_data()
    sync_positions()

    log.info(f"ForexAI VPA Bot started | Paper={PAPER_MODE}")
    diary_entry("SYSTEM",
        f"ForexAI VPA Bot started | Vol spike: {STRATEGY['volume_spike_mult']}x | "
        f"Min score: {STRATEGY['min_score']} | SL={STRATEGY['stop_loss_pips']}pips | "
        f"TP={STRATEGY['take_profit_pips']}pips | Cooldown={STRATEGY['cooldown_minutes']}min", "system")

    while True:
        try:
            if bot_state["killed"]:
                time.sleep(5)
                continue

            market_open = is_market_open()
            bot_state["market_open"] = market_open

            if not market_open:
                log.info("Forex market closed — waiting")
                time.sleep(300)
                continue

            get_account_data()
            sync_positions()
            now = datetime.now()
            pip_map = PIP_MULT

            for pair in PAIRS:
                signal, sig_data = analyze_vpa(pair)
                bot_state["signals"][pair] = sig_data

                in_position = pair in bot_state["positions"]

                if in_position:
                    pos   = bot_state["positions"][pair]
                    price = sig_data.get("price", pos["entry"])
                    pnl   = pos.get("unrealized_pnl", 0)
                    pips  = (price - pos["entry"]) * pip_map[pair]

                    if signal == "SELL":
                        result = close_trade(pos["trade_id"])
                        if result:
                            win = pnl > 0
                            bot_state["closed_trades"].append({
                                "symbol": pair, "entry": pos["entry"], "exit": price,
                                "units": pos["units"], "pnl": round(pnl, 2),
                                "pips": round(pips, 1), "win": win,
                                "time": pos["open_time"],
                                "close_time": now.strftime("%H:%M"),
                                "signal": "VPA distribution"
                            })
                            bot_state["day_pnl"]       = round(bot_state["day_pnl"] + pnl, 2)
                            bot_state["total_trades"] += 1
                            if win: bot_state["win_count"] += 1
                            del bot_state["positions"][pair]
                            diary_entry(pair,
                                f"{'WIN' if win else 'LOSS'} | {pos['entry']:.5f} → {price:.5f} | "
                                f"P&L ${pnl:.2f} | {pips:+.1f} pips",
                                "win" if win else "loss")
                            if not win:
                                set_cooldown(pair)
                            save_state()

                elif signal == "BUY" and not bot_state["killed"]:
                    if is_in_cooldown(pair):
                        continue
                    if is_signal_stale(pair, sig_data):
                        continue
                    price = sig_data.get("price", 0)
                    if price <= 0:
                        continue
                    pip   = 1 / pip_map[pair]
                    sl    = round(price - STRATEGY["stop_loss_pips"] * pip, 5)
                    tp    = round(price + STRATEGY["take_profit_pips"] * pip, 5)
                    result = place_order(pair, STRATEGY["position_units"], sl, tp)
                    if result:
                        bot_state["positions"][pair] = {
                            "trade_id":       result.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID", ""),
                            "entry":          price,
                            "units":          STRATEGY["position_units"],
                            "open_time":      now.strftime("%H:%M"),
                            "symbol":         pair,
                            "unrealized_pnl": 0,
                            "sl": sl, "tp": tp,
                        }
                        diary_entry(pair,
                            f"BUY | {price:.5f} | {STRATEGY['position_units']:,} units | "
                            f"SL={sl:.5f} TP={tp:.5f} | "
                            f"Vol {sig_data.get('vol_ratio',0):.1f}x | Score {sig_data.get('buy_score',0)}",
                            "trade")
                        save_state()

            time.sleep(60)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(30)

app = Flask(__name__)
CORS(app)

@app.after_request
def add_no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response

@app.route("/status")
def status():
    get_account_data()
    sync_positions()
    wins  = bot_state["win_count"]
    total = bot_state["total_trades"]
    payload = {
        "running":          bot_state["running"],
        "killed":           bot_state["killed"],
        "paper_mode":       PAPER_MODE,
        "market_open":      bot_state["market_open"],
        "positions":        bot_state["positions"],
        "closed_trades":    bot_state["closed_trades"][-50:],
        "diary":            bot_state["diary"][-100:],
        "day_pnl":          bot_state["day_pnl"],
        "total_trades":     total,
        "win_rate":         round(wins/total*100) if total > 0 else 0,
        "strategy":         STRATEGY,
        "signals":          bot_state["signals"],
        "account_balance":  bot_state["account_balance"],
        "account_equity":   bot_state["account_equity"],
        "version":          "ForexVPA-1.0",
    }
    return jsonify(clean_nan(payload))

@app.route("/killswitch", methods=["POST"])
def killswitch():
    data = request.json or {}
    bot_state["killed"] = data.get("kill", True)
    diary_entry("SYSTEM", f"Kill switch {'KILLED' if bot_state['killed'] else 'RESUMED'}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/diary")
def get_diary():
    return jsonify({"diary": bot_state["diary"]})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(),
                    "version": "ForexVPA-1.0", "market_open": is_market_open()})

@app.route("/")
def index():
    try:
        return open("/app/index.html").read()
    except Exception:
        return open("index.html").read()

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
