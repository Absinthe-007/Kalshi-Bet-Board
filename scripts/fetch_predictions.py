"""Game winner + score predictions for upcoming NFL and NBA games -> site/predictions.json.

Pulls from several free public sources and blends them into one consensus pick:
  - Sportsbook line (DraftKings via ESPN): moneyline (vig removed), spread, total
  - ESPN Matchup Predictor (FPI for NFL, BPI for NBA)
  - Kalshi game-winner markets (KXNFLGAME / KXNBAGAME)
  - Polymarket game moneyline markets
  - Stats model: each team's points scored/allowed per game this season

No API keys needed. Every source is optional; a game shows whatever sources had it.
"""
import datetime as dt
import json
import math
import re
import time
from statistics import NormalDist
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent.parent / "site" / "predictions.json"
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (Kalshi-Bet-Board predictions)"

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY = "https://gamma-api.polymarket.com"

LEAGUES = {
    "NFL": {"path": "football/nfl", "days": 8, "kalshi": "KXNFLGAME", "poly": "nfl",
            "hfa": 1.5, "sd": 13.5},
    "NBA": {"path": "basketball/nba", "days": 3, "kalshi": "KXNBAGAME", "poly": "nba",
            "hfa": 2.5, "sd": 12.5},
}
# How much each source counts in the consensus win chance
WEIGHTS = {"vegas": 1.0, "kalshi": 1.0, "polymarket": 1.0, "espn": 1.0, "stats": 0.5}
SOURCE_NAMES = {"vegas": "Sportsbook", "kalshi": "Kalshi", "polymarket": "Polymarket",
                "espn": "ESPN", "stats": "Stats model"}


def get(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1 + i)
    return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def parse_time(s):
    """ISO-ish timestamp ('...Z', '...+00:00', '2026-11-13 01:15:00+00') -> aware datetime, or None."""
    try:
        s = s.strip().replace(" ", "T").replace("Z", "+00:00")
        if re.search(r"[+-]\d\d$", s):
            s += ":00"
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def american_to_prob(o):
    o = fnum(str(o).replace("+", "")) if o not in (None, "", "OFF", "EVEN") else (100.0 if o == "EVEN" else None)
    if o is None or o == 0:
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def prob_to_american(p):
    if not p or p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


# ---------------------------------------------------------------- ESPN
def espn_games(league, cfg):
    games, today = [], dt.datetime.now(dt.timezone.utc).date()
    seen = set()
    for d in range(-1, cfg["days"]):
        day = (today + dt.timedelta(days=d)).strftime("%Y%m%d")
        data = get(f"{ESPN}/{cfg['path']}/scoreboard", {"dates": day})
        for e in (data or {}).get("events") or []:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            state = e.get("status", {}).get("type", {}).get("state")
            start = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if state == "post" or start < dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5):
                continue
            c = e["competitions"][0]
            teams = {}
            for t in c["competitors"]:
                tm = t["team"]
                teams[t["homeAway"]] = {
                    "id": tm.get("id"), "abbr": tm.get("abbreviation"), "name": tm.get("displayName"),
                    "short": tm.get("shortDisplayName") or tm.get("name"), "nick": tm.get("name"),
                    "location": tm.get("location"), "logo": tm.get("logo"),
                    "record": (t.get("records") or [{}])[0].get("summary", ""),
                    "score": fnum(t.get("score")) if state == "in" else None,
                }
            if "home" not in teams or "away" not in teams:
                continue
            games.append({
                "id": e["id"], "league": league, "start": e["date"], "state": state,
                "status": e.get("status", {}).get("type", {}).get("shortDetail", ""),
                "home": teams["home"], "away": teams["away"],
                "odds": (c.get("odds") or [None])[0],
                "espn_url": next((l["href"] for l in e.get("links") or [] if "summary" in (l.get("rel") or [])
                                  or "gamecast" in (l.get("rel") or [])), None),
            })
    return games


def espn_summary(league, cfg, gid):
    return get(f"{ESPN}/{cfg['path']}/summary", {"event": gid}) or {}


def team_ratings(cfg):
    """team id -> (points for per game, points against per game, games)."""
    data = get(f"{ESPN_V2}/{cfg['path']}/standings") or {}
    out = {}

    def walk(n):
        for c in n.get("children") or []:
            yield from walk(c)
        for e in (n.get("standings") or {}).get("entries") or []:
            yield e

    for e in walk(data):
        st = {s.get("name"): s.get("value") for s in e.get("stats") or []}
        g = sum(fnum(st.get(k)) or 0 for k in ("wins", "losses", "ties"))
        pf, pa = fnum(st.get("avgPointsFor")), fnum(st.get("avgPointsAgainst"))
        if pf is None and g:
            pf, pa = (fnum(st.get("pointsFor")) or 0) / g, (fnum(st.get("pointsAgainst")) or 0) / g
        if pf and g:
            out[e["team"]["id"]] = (pf, pa, g)
    return out


def parse_line(odds, summary):
    """Sportsbook moneyline (both sides), home spread, total. Prefers pickcenter."""
    books = (summary.get("pickcenter") or []) + ([odds] if odds else [])
    for b in books:
        if not b:
            continue
        ml = b.get("moneyline") or {}
        hml = (b.get("homeTeamOdds") or {}).get("moneyLine") or ((ml.get("home") or {}).get("close") or {}).get("odds")
        aml = (b.get("awayTeamOdds") or {}).get("moneyLine") or ((ml.get("away") or {}).get("close") or {}).get("odds")
        spread, total = fnum(b.get("spread")), fnum(b.get("overUnder"))
        if hml or spread is not None:
            return {"book": (b.get("provider") or {}).get("name") or "Sportsbook",
                    "home_ml": hml, "away_ml": aml, "spread": spread, "total": total,
                    "details": b.get("details")}
    return None


# ---------------------------------------------------------------- Kalshi
def kalshi_markets(series):
    out, cursor = [], None
    for _ in range(5):
        p = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = get(f"{KALSHI}/markets", p)
        if not d:
            break
        out += d.get("markets") or []
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def kprice(m):
    def f(k):
        v = m.get(k + "_dollars")
        if v not in (None, ""):
            return float(v)
        v = m.get(k)
        return float(v) / 100 if v not in (None, "") else None
    b, a, l = f("yes_bid"), f("yes_ask"), f("last_price")
    if b is not None and a and a > 0 and a - b <= 0.15:
        return (a + b) / 2
    return l if l else (b if b else None)


def label_matches(label, team):
    """Kalshi labels are city names, with a nickname initial when a city has 2 teams ('New York G')."""
    lab = norm(label)
    loc, nick = norm(team["location"]), norm(team["nick"])
    return lab in (loc, loc + nick[:1], nick, norm(team["name"]), norm(team["short"]))


def match_kalshi(game, markets):
    start = dt.datetime.fromisoformat(game["start"].replace("Z", "+00:00"))
    events = {}
    for m in markets:
        t = m.get("occurrence_datetime") or m.get("expected_expiration_time")
        if t:
            mt = parse_time(t)
            if mt and abs((mt - start).total_seconds()) > 8 * 3600:
                continue
        events.setdefault(m.get("event_ticker"), []).append(m)
    for ev, ms in events.items():
        found = {}
        for m in ms:
            for side in ("home", "away"):
                if label_matches(m.get("yes_sub_title"), game[side]):
                    found[side] = m
        if len(found) == 2:
            ph, pa = kprice(found["home"]), kprice(found["away"])
            if ph is None and pa is None:
                return None
            if ph is None:
                ph = 1 - pa
            if pa is None:
                pa = 1 - ph
            series = ev.split("-")[0].lower()
            return {"home": ph / (ph + pa), "home_label": found["home"].get("yes_sub_title"),
                    "away_label": found["away"].get("yes_sub_title"),
                    "url": f"https://kalshi.com/markets/{series}/x/{ev.lower()}"}
    return None


# ---------------------------------------------------------------- Polymarket
def poly_events(tag):
    sports = get(f"{POLY}/sports") or []
    sid = next((s.get("series") for s in sports if s.get("sport") == tag), None)
    if not sid:
        return []
    out = []
    for off in (0, 100, 200):
        d = get(f"{POLY}/events", {"series_id": sid, "closed": "false", "limit": 100, "offset": off})
        if not d:
            break
        out += d
        if len(d) < 100:
            break
    return out


def match_poly(game, events):
    start = dt.datetime.fromisoformat(game["start"].replace("Z", "+00:00"))
    hn, an = norm(game["home"]["nick"]), norm(game["away"]["nick"])
    for e in events:
        for m in e.get("markets") or []:
            if m.get("sportsMarketType") != "moneyline":
                continue
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
                prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            except (ValueError, TypeError):
                continue
            if len(outcomes) != 2 or len(prices) != 2:
                continue
            names = [norm(o) for o in outcomes]
            if set(names) != {hn, an}:
                continue
            gt = m.get("gameStartTime") or e.get("endDate")
            if gt:
                t = parse_time(gt)
                if t and abs((t - start).total_seconds()) > 12 * 3600:
                    continue
            ph = prices[names.index(hn)]
            if not 0 < ph < 1 or (prices[0] + prices[1]) < 0.5:
                continue
            return {"home": ph / (prices[0] + prices[1]),
                    "url": f"https://polymarket.com/event/{e.get('slug')}"}
    return None


# ---------------------------------------------------------------- build
def build(league, cfg, kal, poly, ratings):
    games = []
    for g in espn_games(league, cfg):
        summ = espn_summary(league, cfg, g["id"])
        home, away = g["home"], g["away"]
        src = {}  # source -> home win probability (0-1)

        line = parse_line(g.pop("odds"), summ)
        vegas_score = None
        if line:
            ph, pa = american_to_prob(line["home_ml"]), american_to_prob(line["away_ml"])
            if ph and pa:
                src["vegas"] = ph / (ph + pa)
            elif line["spread"] is not None:
                src["vegas"] = phi(-line["spread"] / cfg["sd"])
            if line["spread"] is not None and line["total"]:
                vegas_score = ((line["total"] - line["spread"]) / 2, (line["total"] + line["spread"]) / 2)

        pred = summ.get("predictor") or {}
        eh = fnum((pred.get("homeTeam") or {}).get("gameProjection"))
        ea = fnum((pred.get("awayTeam") or {}).get("gameProjection"))
        if eh is not None and ea is not None and eh + ea > 0:
            src["espn"] = eh / (eh + ea)

        stats_score = None
        rh, ra = ratings.get(home["id"]), ratings.get(away["id"])
        if rh and ra and min(rh[2], ra[2]) >= 2:
            hs = (rh[0] + ra[1]) / 2 + cfg["hfa"] / 2
            as_ = (ra[0] + rh[1]) / 2 - cfg["hfa"] / 2
            stats_score = (hs, as_)
            src["stats"] = phi((hs - as_) / cfg["sd"])

        k = match_kalshi(g, kal)
        if k:
            src["kalshi"] = k["home"]
        p = match_poly(g, poly)
        if p:
            src["polymarket"] = p["home"]

        if not src:
            continue
        wsum = sum(WEIGHTS[s] for s in src)
        cons = sum(WEIGHTS[s] * v for s, v in src.items()) / wsum

        # Projected score: sportsbook line counts double vs the stats model
        if vegas_score and stats_score:
            proj = tuple((2 * v + s) / 3 for v, s in zip(vegas_score, stats_score))
        elif vegas_score:
            proj = vegas_score
        elif stats_score:
            # No line posted yet: keep the stats model's total, but take the margin from the consensus
            tot = sum(stats_score)
            m = cfg["sd"] * NormalDist().inv_cdf(min(max(cons, 0.01), 0.99))
            proj = ((tot + m) / 2, (tot - m) / 2)
        else:
            proj = None
        # Keep the projected winner consistent with the consensus pick
        if proj:
            margin = proj[0] - proj[1]
            if (cons >= 0.5) != (margin >= 0):
                mid = sum(proj) / 2
                m = max(abs(margin), 0.5) * (1 if cons >= 0.5 else -1)
                proj = (mid + m / 2, mid - m / 2)
            proj = [round(proj[0]), round(proj[1])]
            if proj[0] == proj[1]:
                proj[0 if cons >= 0.5 else 1] += 1

        pick = "home" if cons >= 0.5 else "away"
        pp = cons if pick == "home" else 1 - cons
        agree = sum(1 for v in src.values() if (v >= 0.5) == (pick == "home"))
        tier = "Lock" if pp >= 0.75 and agree == len(src) else "Strong" if pp >= 0.65 else "Lean" if pp >= 0.55 else "Toss-up"

        home_out = {kk: home[kk] for kk in ("abbr", "name", "short", "logo", "record", "score")}
        away_out = {kk: away[kk] for kk in ("abbr", "name", "short", "logo", "record", "score")}
        if k:
            home_out["kalshi_label"], away_out["kalshi_label"] = k["home_label"], k["away_label"]
        games.append({
            "id": g["id"], "league": league, "start": g["start"], "state": g["state"], "status": g["status"],
            "home": home_out, "away": away_out,
            "pick": pick, "pick_prob": round(pp * 100, 1), "home_prob": round(cons * 100, 1),
            "tier": tier, "agree": agree, "n_sources": len(src),
            "sources": [{"key": s, "name": SOURCE_NAMES[s], "home": round(v * 100, 1)}
                        for s, v in sorted(src.items(), key=lambda kv: list(WEIGHTS).index(kv[0]))],
            "proj": proj,
            "line": {"book": line["book"], "details": line["details"], "spread": line["spread"],
                     "total": line["total"], "home_ml": line["home_ml"], "away_ml": line["away_ml"]} if line else None,
            "fair_odds": prob_to_american(pp),
            "links": {"kalshi": k["url"] if k else None, "polymarket": p["url"] if p else None,
                      "espn": g["espn_url"]},
        })
        print(f"  {league} {away['abbr']:>4} @ {home['abbr']:<4} pick={(home if pick == 'home' else away)['abbr']:<4}"
              f" {pp * 100:5.1f}%  proj={proj}  sources={','.join(src)}")
    return games


def main():
    all_games = []
    for league, cfg in LEAGUES.items():
        print(f"{league}:")
        try:
            kal = kalshi_markets(cfg["kalshi"])
            poly = poly_events(cfg["poly"])
            ratings = team_ratings(cfg)
            print(f"  kalshi markets={len(kal)} polymarket events={len(poly)} rated teams={len(ratings)}")
            all_games += build(league, cfg, kal, poly, ratings)
        except Exception as e:  # never break the bet board because of predictions
            print(f"  {league} failed: {e!r}")
    all_games.sort(key=lambda g: g["start"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "weights": WEIGHTS,
        "games": all_games,
    }, indent=1))
    print(f"Wrote {len(all_games)} game predictions -> {OUT}")


if __name__ == "__main__":
    main()
