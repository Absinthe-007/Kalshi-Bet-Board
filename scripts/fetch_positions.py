"""Fetch Rubin's Kalshi positions + recent settlements and write site/data.json.

Env vars (set as GitHub Actions secrets):
  KALSHI_API_KEY_ID   - the Key ID shown when you create the API key
  KALSHI_PRIVATE_KEY  - full contents of the downloaded .key / .pem file
"""
import base64
import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

VERSION = "V1.2"
BASE = "https://api.elections.kalshi.com"
API = "/trade-api/v2"
OUT = Path(__file__).resolve().parent.parent / "site" / "data.json"

KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "").strip()
PEM = os.environ.get("KALSHI_PRIVATE_KEY", "").strip().replace("\\n", "\n")
if not KEY_ID or not PEM:
    sys.exit("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY secret.")
PRIVATE_KEY = serialization.load_pem_private_key(PEM.encode(), password=None)

S = requests.Session()
S.headers["User-Agent"] = "Kalshi-Bet-Board/1.0"


def signed_get(path, params=None):
    """GET an authenticated endpoint. Signature = RSA-PSS(ts + METHOD + path-without-query)."""
    ts = str(int(time.time() * 1000))
    full_path = API + path
    msg = (ts + "GET" + full_path).encode()
    if isinstance(PRIVATE_KEY, ed25519.Ed25519PrivateKey):
        sig = PRIVATE_KEY.sign(msg)
    else:
        sig = PRIVATE_KEY.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    headers = {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }
    r = S.get(BASE + full_path, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def public_get(path, params=None):
    r = S.get(BASE + API + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def paged(path, key, params=None, signed=True, max_pages=20):
    params = dict(params or {})
    out, cursor = [], None
    for _ in range(max_pages):
        if cursor:
            params["cursor"] = cursor
        data = (signed_get if signed else public_get)(path, params)
        out.extend(data.get(key) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


MONEY_CENT_KEYS = {"market_exposure", "realized_pnl", "total_traded", "fees_paid",
                   "revenue", "yes_total_cost", "no_total_cost", "fee_cost",
                   "balance", "portfolio_value"}


def num(obj, key, default=0.0):
    """Read a field that may come as key_dollars (str), key_fp (str) or key (cents/int)."""
    if obj is None:
        return default
    for k in (key + "_dollars", key + "_fp"):
        v = obj.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    v = obj.get(key)
    if v in (None, ""):
        return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if key in MONEY_CENT_KEYS or key.endswith(("_bid", "_ask", "_price")):
        return v / 100.0
    return v


def price(m, key):
    """Market price in dollars (0-1); None if missing."""
    v = m.get(key + "_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(key)
    if v not in (None, ""):
        return float(v) / 100.0
    return None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "market"


def kalshi_url(series, event_title, event_ticker, is_combo):
    if is_combo or not series:
        return "https://kalshi.com/portfolio"
    return f"https://kalshi.com/markets/{series.lower()}/{slug(event_title)}/{event_ticker.lower()}"


def fetch_markets(tickers):
    markets = {}
    tickers = list(tickers)
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            data = public_get("/markets", {"tickers": ",".join(chunk), "limit": 200})
            for m in data.get("markets") or []:
                markets[m["ticker"]] = m
        except requests.HTTPError:
            pass
        for t in chunk:  # fall back one by one for anything missing
            if t not in markets:
                try:
                    markets[t] = public_get(f"/markets/{t}")["market"]
                except requests.HTTPError:
                    markets[t] = {"ticker": t}
    return markets


def fetch_events(event_tickers):
    events = {}
    for e in event_tickers:
        if not e:
            continue
        try:
            d = public_get(f"/events/{e}")
            events[e] = d.get("event", d)
        except requests.HTTPError:
            events[e] = {}
    return events


def leg_info(m, markets):
    """Legs of a combo with a live probability for each, plus the estimated combo win chance."""
    legs, prob = [], 1.0
    subs = (m.get("no_sub_title") or "").split(",")
    for i, leg in enumerate(m.get("mve_selected_legs") or []):
        lm = markets.get(leg.get("market_ticker"), {})
        side = (leg.get("side") or "yes").lower()
        label = subs[i].strip() if i < len(subs) else ""
        if label.lower().startswith(("yes ", "no ")):
            label = label.split(" ", 1)[1]
        label = label or lm.get("yes_sub_title") or lm.get("title") or leg.get("market_ticker")
        result = (lm.get("result") or "").lower()
        if result in ("yes", "no"):
            p = 1.0 if result == side else 0.0
            status = "hit" if p == 1.0 else "miss"
        else:
            yb, ya, lp = price(lm, "yes_bid"), price(lm, "yes_ask"), price(lm, "last_price")
            yes_p = (yb + ya) / 2 if yb is not None and ya not in (None, 0) else lp
            if yes_p is None:
                yes_p = 0.5
            p = yes_p if side == "yes" else 1 - yes_p
            status = "live"
        prob *= p
        legs.append({"label": label, "side": side.upper(), "prob": round(p * 100, 1), "status": status})
    return legs, prob


def category_for(market, event):
    series = (event.get("series_ticker") or market.get("event_ticker", "").split("-")[0]).upper()
    for tag, label in (("NFL", "NFL"), ("NBA", "NBA"), ("NCAAF", "College FB"), ("NCAAB", "College BB"), ("NCAAMB", "College BB"),
                       ("NCAAWB", "College BB"),
                       ("NHL", "NHL"), ("MLB", "MLB"), ("MLS", "Soccer"), ("EPL", "Soccer"),
                       ("UFC", "UFC"), ("ATP", "Tennis"), ("WTA", "Tennis"), ("ITF", "Tennis"),
                       ("MVE", "Combos")):
        if tag in series:
            return label
    return event.get("category") or "Other"


def build_open(positions, markets, events):
    bets = []
    for p in positions:
        qty = num(p, "position")
        if qty == 0:
            continue
        t = p["ticker"]
        m = markets.get(t, {})
        e = events.get(m.get("event_ticker"), {})
        side = "YES" if qty > 0 else "NO"
        n = abs(qty)
        cost = num(p, "market_exposure")
        bid = price(m, "yes_bid" if side == "YES" else "no_bid")
        ask = price(m, "yes_ask" if side == "YES" else "no_ask")
        last = price(m, "last_price")
        if last is not None and side == "NO":
            last = 1 - last
        mark = bid if bid not in (None, 0) else last
        if mark is None:
            mark = cost / n if n else 0
        value = mark * n
        is_combo = bool(m.get("mve_selected_legs")) or "MVE" in (m.get("event_ticker") or "")
        legs, combo_prob = ([], None)
        if is_combo:
            legs, combo_prob = leg_info(m, markets)
            if side == "NO":
                combo_prob = 1 - combo_prob
        pick = m.get("yes_sub_title") if side == "YES" else m.get("no_sub_title")
        if is_combo:
            pick = f"{len(legs)}-leg combo" if side == "YES" else f"Against {len(legs)}-leg combo"
        bets.append({
            "ticker": t,
            "event_ticker": m.get("event_ticker"),
            "event_title": f"Combo · {len(legs)} legs" if is_combo else (e.get("title") or m.get("title") or t),
            "event_sub": "" if is_combo else (e.get("sub_title") or ""),
            "market_title": m.get("title") or t,
            "pick": pick or side,
            "side": side,
            "category": category_for(m, e),
            "contracts": n,
            "avg_cost": round(cost / n, 4) if n else 0,
            "cost": round(cost, 2),
            "mark": round(mark, 4),
            "ask": ask,
            "value": round(value, 2),
            "pnl": round(value - cost, 2),
            "max_payout": round(n * 1.0, 2),
            "max_profit": round(n * 1.0 - cost, 2),
            "win_prob": round((combo_prob if combo_prob is not None else mark) * 100, 1),
            "prob_estimated": combo_prob is not None,
            "fees": round(num(p, "fees_paid"), 2),
            "status": m.get("status", ""),
            "close_time": m.get("close_time"),
            "game_time": m.get("occurrence_datetime") or m.get("expected_expiration_time"),
            "legs": legs,
            "url": kalshi_url(e.get("series_ticker"), e.get("title") or m.get("title"),
                              m.get("event_ticker", ""), is_combo),
        })
    bets.sort(key=lambda b: b.get("game_time") or b.get("close_time") or "9999")
    return bets


def build_settled(closed, settlements, markets, events):
    """Closed bets, P/L from Kalshi's realized P/L (includes early sells) minus fees."""
    by_ticker = {}
    for st in settlements:
        by_ticker.setdefault(st.get("ticker"), st)
    out = []
    for p in closed:
        t = p.get("ticker")
        m = markets.get(t, {})
        e = events.get(m.get("event_ticker"), {})
        st = by_ticker.get(t, {})
        fees = num(p, "fees_paid")
        pnl = num(p, "realized_pnl") - fees
        yes_n, no_n = num(st, "yes_count"), num(st, "no_count")
        yes_c, no_c = num(st, "yes_total_cost"), num(st, "no_total_cost")
        side = "YES" if (yes_n, yes_c) >= (no_n, no_c) else "NO"
        is_combo = "MVE" in (m.get("event_ticker") or t or "")
        legs = leg_info(m, markets)[0] if is_combo else []
        pick = (m.get("yes_sub_title") if side == "YES" else m.get("no_sub_title")) or side
        if is_combo:
            pick = f"{len(legs)}-leg combo"
        print(f"  settled {t[:40]:40s} realized={num(p, 'realized_pnl'):8.2f} fees={fees:6.2f} pnl={pnl:8.2f}")
        out.append({
            "ticker": t,
            "event_title": f"Combo \u00b7 {len(legs)} legs" if is_combo else (e.get("title") or m.get("title") or t),
            "market_title": "" if is_combo else (m.get("title") or t),
            "pick": pick,
            "side": side,
            "category": category_for(m, e),
            "contracts": round(max(yes_n, no_n), 2),
            "cost": round(num(p, "total_traded"), 2),
            "fees": round(fees, 2),
            "pnl": round(pnl, 2),
            "result": "WON" if pnl > 0.004 else ("LOST" if pnl < -0.004 else "PUSH"),
            "market_result": (st.get("market_result") or m.get("result") or "").upper(),
            "legs": legs,
            "settled_time": st.get("settled_time") or m.get("close_time"),
            "url": kalshi_url(e.get("series_ticker"), e.get("title") or m.get("title"),
                              m.get("event_ticker", ""), is_combo),
        })
    out.sort(key=lambda b: b.get("settled_time") or "", reverse=True)
    return out


def main():
    bal = signed_get("/portfolio/balance")
    positions = paged("/portfolio/positions", "market_positions",
                      {"limit": 200, "count_filter": "position"})
    closed = paged("/portfolio/positions", "market_positions",
                   {"limit": 200, "settlement_status": "settled"}, max_pages=5)
    closed = [p for p in closed if num(p, "position") == 0]
    settlements = paged("/portfolio/settlements", "settlements", {"limit": 100}, max_pages=8)
    if positions:
        print("position fields:", sorted(positions[0].keys()))
    if closed:
        print("closed fields:", sorted(closed[0].keys()))

    tickers = {p["ticker"] for p in positions if num(p, "position") != 0}
    tickers |= {p["ticker"] for p in closed}
    markets = fetch_markets(tickers)
    leg_tickers = {leg.get("market_ticker") for m in markets.values()
                   for leg in (m.get("mve_selected_legs") or [])} - set(markets)
    markets.update(fetch_markets(leg_tickers))
    events = fetch_events({markets[t].get("event_ticker") for t in tickers if t in markets})

    open_bets = build_open(positions, markets, events)
    settled = build_settled(closed, settlements, markets, events)

    won = sum(1 for s in settled if s["result"] == "WON")
    lost = sum(1 for s in settled if s["result"] == "LOST")
    data = {
        "version": VERSION,
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "summary": {
            "cash": math.floor(num(bal, "balance") * 100 + 1e-6) / 100,
            "portfolio_value": round(num(bal, "portfolio_value"), 2),
            "open_count": len(open_bets),
            "at_risk": round(sum(b["cost"] for b in open_bets), 2),
            "open_value": round(sum(b["value"] for b in open_bets), 2),
            "unrealized": round(sum(b["pnl"] for b in open_bets), 2),
            "max_payout": round(sum(b["max_payout"] for b in open_bets), 2),
            "settled_pnl": round(sum(s["pnl"] for s in settled), 2),
            "record": {"won": won, "lost": lost},
        },
        "open": open_bets,
        "settled": settled,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    print(f"Wrote {len(open_bets)} open bets, {len(settled)} settled -> {OUT}")


if __name__ == "__main__":
    main()
