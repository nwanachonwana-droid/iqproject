#!/usr/bin/env python3
"""
The iQ Project — Master Pipeline
Usage:
  python3 iq_pipeline.py                    # full run: picks + settle + push
  python3 iq_pipeline.py --picks-only       # picks only, no settlement
  python3 iq_pipeline.py --no-sync          # skip git push
  python3 iq_pipeline.py --sport mlb        # one sport only

Setup:
  export ODDS_API_KEY=your_key_here
  Set SITE_DIR below to your site repo path
"""

import json, os, math, datetime, subprocess, sys, time, io, csv
from pathlib import Path
from collections import defaultdict

TODAY    = datetime.date.today().isoformat()
SITE_DIR = os.path.expanduser("~/Desktop/IQ_SITE")   # ← change to your site repo
DATA_DIR = os.path.expanduser("~/Desktop/iq_data")
ODDS_KEY = os.environ.get("ODDS_API_KEY", "")

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# ── Shared math ───────────────────────────────────────────────────────────────
def sigmoid(x): return 1 / (1 + math.exp(-x))

def log5(a, b):
    n = a - a * b; d = a + b - 2 * a * b
    return max(0.01, min(0.99, n / d)) if d else 0.5

def devig(h, a):
    t = h + a; return (h / t, a / t) if t else (0.5, 0.5)

def to_imp(line):
    return abs(line) / (abs(line) + 100) if line < 0 else 100 / (line + 100)

def tier(p):
    return "high" if p >= 0.65 else "medium" if p >= 0.60 else "low"

def edge_pp(model_p, market_p):
    return round((model_p - market_p) * 100, 2) if market_p else None

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def fetch(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "iqproject/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def fetch_text(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "iqproject/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")

# ── Odds API ──────────────────────────────────────────────────────────────────
def get_odds(sport_key):
    if not ODDS_KEY:
        return {}
    url = (f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets=h2h&oddsFormat=american")
    try:
        data = fetch(url)
        odds = {}
        for g in data:
            for bk in g.get("bookmakers", []):
                if bk["key"] not in ("pinnacle", "draftkings", "fanduel", "bovada"):
                    continue
                for mk in bk.get("markets", []):
                    if mk["key"] != "h2h":
                        continue
                    oc = {o["name"]: o["price"] for o in mk["outcomes"]}
                    ht = g["home_team"]; at = g["away_team"]
                    ih = to_imp(oc.get(ht, -110)); ia = to_imp(oc.get(at, -110))
                    nh, na = devig(ih, ia)
                    entry = {"home_team": ht, "away_team": at,
                             "home_line": oc.get(ht),
                             "nv_home": round(nh, 4), "nv_away": round(na, 4)}
                    odds[g["id"]] = entry
                    # Key by exact team names for reliable lookup
                    odds[f"{ht}|{at}"] = entry
                break
        print(f"  Odds: {len(data)} games from API")
        return odds
    except Exception as e:
        print(f"  ! Odds API ({sport_key}): {e}")
        return {}

def lookup_odds(odds, hn, an, gid=None):
    """Match odds to a game. Exact names first, then first-word fallback."""
    if gid and gid in odds:
        return odds[gid]
    if f"{hn}|{an}" in odds:
        return odds[f"{hn}|{an}"]
    # First word of team name (university name, not mascot)
    h1 = hn.split()[0].lower(); a1 = an.split()[0].lower()
    for k, v in odds.items():
        if not isinstance(k, str) or "|" not in k:
            continue
        parts = k.lower().split("|")
        if len(parts) == 2 and parts[0].startswith(h1) and parts[1].startswith(a1):
            return v
    return {}

# ── Output helpers ────────────────────────────────────────────────────────────
def write_picks(sport_id, picks, status="PROVEN", model_version="v1.0"):
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    out = {
        "schema_version": "1.0",
        "sport": sport_id,
        "generated_at": ts,
        "data_date": TODAY,
        "model_version": model_version,
        "status": status,
        "picks": picks,
        "summary": {
            "total_picks": len(picks),
            "high_confidence": sum(1 for p in picks if p.get("confidence_tier") == "high"),
            "avg_edge_pp": round(
                sum(p.get("edge_pp") or 0 for p in picks) / len(picks), 2
            ) if picks else 0,
        }
    }
    path = os.path.join(DATA_DIR, f"{sport_id}_picks_today.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"  -> {len(picks)} picks  [{status}]  {sport_id}_picks_today.json")
    return picks

def load_perf(sport_id):
    path = os.path.join(DATA_DIR, f"{sport_id}_performance_log.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f), path
    return {
        "schema_version": "1.0", "sport": sport_id,
        "launch_date": TODAY, "last_updated": TODAY,
        "overall": {"wins": 0, "losses": 0, "pushes": 0, "total": 0,
                    "hit_rate": 0.0, "roi_flat": 0.0},
        "by_confidence": {
            "high":   {"wins": 0, "total": 0, "hit_rate": 0.0},
            "medium": {"wins": 0, "total": 0, "hit_rate": 0.0},
            "low":    {"wins": 0, "total": 0, "hit_rate": 0.0},
        },
        "recent": [],
    }, path

# ── Name maps ─────────────────────────────────────────────────────────────────
# MLB: schedule uses full names, standings API uses short names
MLB_NAME_MAP = {
    "Arizona Diamondbacks": "D-backs",    "Atlanta Braves": "Braves",
    "Baltimore Orioles": "Orioles",        "Boston Red Sox": "Red Sox",
    "Chicago Cubs": "Cubs",                "Chicago White Sox": "White Sox",
    "Cincinnati Reds": "Reds",             "Cleveland Guardians": "Guardians",
    "Colorado Rockies": "Rockies",         "Detroit Tigers": "Tigers",
    "Houston Astros": "Astros",            "Kansas City Royals": "Royals",
    "Los Angeles Angels": "Angels",        "Los Angeles Dodgers": "Dodgers",
    "Miami Marlins": "Marlins",            "Milwaukee Brewers": "Brewers",
    "Minnesota Twins": "Twins",            "New York Mets": "Mets",
    "New York Yankees": "Yankees",         "Oakland Athletics": "Athletics",
    "Athletics": "Athletics",              "Philadelphia Phillies": "Phillies",
    "Pittsburgh Pirates": "Pirates",       "San Diego Padres": "Padres",
    "San Francisco Giants": "Giants",      "Seattle Mariners": "Mariners",
    "St. Louis Cardinals": "Cardinals",    "Tampa Bay Rays": "Rays",
    "Texas Rangers": "Rangers",            "Toronto Blue Jays": "Blue Jays",
    "Washington Nationals": "Nationals",
}

# ═══════════════════════════════════════════════════════════════════════════════
# SPORT MODELS
# ═══════════════════════════════════════════════════════════════════════════════

def run_mlb():
    print("\n[MLB — Pythagenpat + ISR K=20]")
    # Team Pythagorean win% from standings
    st = fetch("https://statsapi.mlb.com/api/v1/standings"
               "?leagueId=103,104&season=2026&standingsTypes=regularSeason")
    wp = {}
    for rec in st.get("records", []):
        for tr in rec.get("teamRecords", []):
            n = tr["team"]["name"]
            gp = tr.get("gamesPlayed", 1) or 1
            rs = tr.get("runsScored", 0) or 0
            ra = tr.get("runsAllowed", 0) or 0
            if rs > 0 and ra > 0:
                rpg = rs / gp; rapg = ra / gp
                exp = (rpg + rapg) ** 0.285   # Pythagenpat
                w = (rpg ** exp) / (rpg ** exp + rapg ** exp)
            else:
                w = 0.500
            r = gp / (gp + 20)               # ISR credibility K=20
            wp[n] = r * w + (1 - r) * 0.500
    print(f"  Standings: {len(wp)} teams")

    sched = fetch(f"https://statsapi.mlb.com/api/v1/schedule"
                  f"?sportId=1&date={TODAY}&hydrate=probablePitcher,team,linescore")
    odds = get_odds("baseball_mlb")
    picks = []

    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Preview":
                continue
            hn_full = g["teams"]["home"]["team"]["name"]
            an_full = g["teams"]["away"]["team"]["name"]
            hn = MLB_NAME_MAP.get(hn_full, hn_full)
            an = MLB_NAME_MAP.get(an_full, an_full)
            wh = wp.get(hn); wa = wp.get(an)
            if wh is None or wa is None:
                continue
            p_home = round(max(0.01, min(0.99, log5(wh, wa) + 0.04)), 4)
            p_away = round(1 - p_home, 4)
            ps = "home" if p_home >= p_away else "away"
            pp = p_home if ps == "home" else p_away
            gid = str(g.get("gamePk", ""))
            mkt = lookup_odds(odds, hn_full, an_full, gid)
            mp = mkt.get("nv_home") if ps == "home" else mkt.get("nv_away")
            e = edge_pp(pp, mp)
            if mp and e is not None and abs(e) < 3:
                continue
            picks.append({
                "pick_id": f"mlb-{TODAY}-{gid}",
                "game_time_utc": g.get("gameDate"),
                "home_team": hn_full, "away_team": an_full,
                "pick": hn_full if ps == "home" else an_full,
                "pick_side": ps,
                "model_prob_home": p_home, "model_prob_away": p_away,
                "market_prob_home": mkt.get("nv_home"),
                "market_prob_away": mkt.get("nv_away"),
                "market_line_home": mkt.get("home_line"),
                "market_source": "pinnacle",
                "edge_pp": e, "confidence_tier": tier(pp),
                "result": None, "outcome": None, "settled_at": None,
            })
    write_picks("mlb", picks, "PROVEN")


def run_nhl():
    print("\n[NHL — xGF ISR K=15 + Log5]")
    st = fetch("https://api-web.nhle.com/v1/standings/now")
    isr_by_abbrev = {}; isr_by_name = {}
    for t in st.get("standings", []):
        n = t.get("teamName", {}).get("default", "")
        abbrev = t.get("teamAbbrev", {}).get("default", "") or t.get("teamAbbrev", "")
        gp = t.get("gamesPlayed") or 1
        gf = t.get("goalFor") or 0
        ga = t.get("goalAgainst") or 0
        wp2 = (gf ** 2) / (gf ** 2 + ga ** 2) if gf > 0 and ga > 0 else 0.500
        r = gp / (gp + 15)
        val = r * wp2 + (1 - r) * 0.500
        if n: isr_by_name[n] = val
        if abbrev: isr_by_abbrev[abbrev] = val
    print(f"  Standings: {len(isr_by_name)} teams")

    sched = fetch(f"https://api-web.nhle.com/v1/schedule/{TODAY}")
    odds = get_odds("icehockey_nhl")
    picks = []; seen = set()

    today_block = next(
        (wk for wk in sched.get("gameWeek", []) if wk.get("date") == TODAY), None
    )
    games_today = today_block.get("games", []) if today_block else []
    print(f"  Games today: {len(games_today)}")

    for g in games_today:
        gid = str(g.get("id", ""))
        if gid in seen: continue
        seen.add(gid)
        ht_d = g.get("homeTeam", {}); at_d = g.get("awayTeam", {})
        h_abb = ht_d.get("abbrev", ""); a_abb = at_d.get("abbrev", "")
        hn = (ht_d.get("placeName", {}).get("default", "") + " " +
              ht_d.get("commonName", {}).get("default", "")).strip()
        an = (at_d.get("placeName", {}).get("default", "") + " " +
              at_d.get("commonName", {}).get("default", "")).strip()
        ih = isr_by_abbrev.get(h_abb) or isr_by_name.get(hn)
        ia = isr_by_abbrev.get(a_abb) or isr_by_name.get(an)
        if not ih or not ia: continue
        p_home = round(max(0.01, min(0.99, log5(ih, ia) + 0.03)), 4)
        p_away = round(1 - p_home, 4)
        ps = "home" if p_home >= p_away else "away"
        pp = p_home if ps == "home" else p_away
        if pp < 0.55: continue
        mkt = lookup_odds(odds, hn, an, gid)
        mp = mkt.get("nv_home") if ps == "home" else mkt.get("nv_away")
        e = edge_pp(pp, mp)
        if mp and e is not None and abs(e) < 3: continue
        picks.append({
            "pick_id": f"nhl-{TODAY}-{gid}",
            "game_time_utc": g.get("startTimeUTC"),
            "home_team": hn, "away_team": an,
            "pick": hn if ps == "home" else an,
            "pick_side": ps,
            "model_prob_home": p_home, "model_prob_away": p_away,
            "market_prob_home": mkt.get("nv_home"),
            "market_prob_away": mkt.get("nv_away"),
            "market_line_home": mkt.get("home_line"),
            "market_source": "pinnacle",
            "edge_pp": e, "confidence_tier": tier(pp),
            "result": None, "outcome": None, "settled_at": None,
        })
    write_picks("nhl", picks, "ACTIVE")


def run_ncaa_baseball():
    print("\n[NCAA Baseball — Pythagenpat ISR K=25]")
    try:
        sched = fetch("https://site.api.espn.com/apis/site/v2/sports/"
                      "baseball/college-baseball/scoreboard")
    except Exception as e:
        print(f"  ! ESPN unavailable: {e}")
        return write_picks("ncaa_baseball", [], "PROVEN")

    odds = get_odds("baseball_ncaa")
    picks = []

    for ev in sched.get("events", []):
        cs = ev.get("competitions", [{}])[0].get("competitors", [])
        if len(cs) < 2: continue
        home = next((c for c in cs if c["homeAway"] == "home"), cs[0])
        away = next((c for c in cs if c["homeAway"] == "away"), cs[1])
        hn = home["team"]["displayName"]
        an = away["team"]["displayName"]

        def parse_record(c):
            for rec in c.get("records", []):
                if rec.get("type") == "total" or rec.get("name") == "overall":
                    parts = rec.get("summary", "0-0").split("-")
                    try: return int(parts[0]), int(parts[1])
                    except: return 0, 1
            return 0, 1

        hw, hl = parse_record(home); aw, al = parse_record(away)
        gp_h = hw + hl or 1; gp_a = aw + al or 1
        isr_h = gp_h / (gp_h + 25) * (hw / gp_h) + (1 - gp_h / (gp_h + 25)) * 0.500
        isr_a = gp_a / (gp_a + 25) * (aw / gp_a) + (1 - gp_a / (gp_a + 25)) * 0.500
        p_home = round(max(0.01, min(0.99, log5(isr_h, isr_a) + 0.04)), 4)
        p_away = round(1 - p_home, 4)
        ps = "home" if p_home >= p_away else "away"
        pp = p_home if ps == "home" else p_away
        if pp < 0.60: continue

        gid = ev.get("id", "")
        mkt = lookup_odds(odds, hn, an, gid)
        mp = mkt.get("nv_home") if ps == "home" else mkt.get("nv_away")
        e = edge_pp(pp, mp)
        if mp and e is not None and abs(e) < 3: continue

        picks.append({
            "pick_id": f"ncaabb-{TODAY}-{gid}",
            "game_time_utc": ev.get("date"),
            "home_team": hn, "away_team": an,
            "pick": hn if ps == "home" else an,
            "pick_side": ps,
            "model_prob_home": p_home, "model_prob_away": p_away,
            "market_prob_home": mkt.get("nv_home"),
            "market_prob_away": mkt.get("nv_away"),
            "market_line_home": mkt.get("home_line"),
            "market_source": "pinnacle" if mkt else None,
            "edge_pp": e, "confidence_tier": tier(pp),
            "result": None, "outcome": None, "settled_at": None,
        })
    write_picks("ncaa_baseball", picks, "PROVEN")


def run_soccer():
    # Suppressed until August 2026 — EPL end of season, ISR unreliable on 1 game/week
    print("\n[Soccer — suppressed until Aug 2026 new season]")
    write_picks("soccer", [], "ACTIVE")


def run_nfl():
    # Off-season until September 2026
    print("\n[NFL — off-season]")
    write_picks("nfl", [], "RESEARCH")


# ═══════════════════════════════════════════════════════════════════════════════
# SETTLEMENT — fetch yesterday's results, update performance logs
# ═══════════════════════════════════════════════════════════════════════════════

def settle_all():
    print("\n[Settling yesterday's picks]")
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    yesterday_compact = yesterday.replace("-", "")

    espn_paths = {
        "mlb":          "baseball/mlb",
        "nhl":          "hockey/nhl",
        "ncaa_baseball": "baseball/college-baseball",
        "nba":          "basketball/nba",
    }

    for sport_id, espn_path in espn_paths.items():
        picks_path = os.path.join(DATA_DIR, f"{sport_id}_picks_today.json")
        if not os.path.exists(picks_path): continue
        with open(picks_path) as f:
            picks_data = json.load(f)

        pending = [p for p in picks_data.get("picks", [])
                   if p.get("outcome") is None and
                   (p.get("data_date") == yesterday or
                    (p.get("game_time_utc") or "").startswith(yesterday))]
        if not pending: continue

        try:
            url = (f"https://site.api.espn.com/apis/site/v2/sports/{espn_path}"
                   f"/scoreboard?dates={yesterday_compact}")
            scores = fetch(url)
        except Exception as e:
            print(f"  ! {sport_id}: {e}"); continue

        # Build winner map: team_name -> won
        winners = {}
        for event in scores.get("events", []):
            for comp in event.get("competitions", [{}]):
                for c in comp.get("competitors", []):
                    winners[c["team"]["displayName"]] = c.get("winner", False)

        perf, perf_path = load_perf(sport_id)
        settled = 0
        for pick in pending:
            team = pick["pick"]
            if team not in winners: continue
            won = winners[team]
            pick["outcome"]    = "WIN" if won else "LOSS"
            pick["result"]     = 1.0 if won else 0.0
            pick["settled_at"] = datetime.datetime.utcnow().isoformat() + "Z"

            ov = perf["overall"]
            ov["total"] += 1
            if won: ov["wins"] = ov.get("wins", 0) + 1
            else:   ov["losses"] = ov.get("losses", 0) + 1
            s = ov["wins"] + ov["losses"]
            ov["hit_rate"] = round(ov["wins"] / s * 100, 2) if s > 0 else 0.0
            ov["roi_flat"] = round(
                ((ov["wins"] * 0.909) - ov["losses"]) / ov["total"] * 100, 2
            ) if ov["total"] > 0 else 0.0

            t = pick.get("confidence_tier", "low")
            bc = perf["by_confidence"].setdefault(
                t, {"wins": 0, "total": 0, "hit_rate": 0.0}
            )
            bc["total"] += 1
            if won: bc["wins"] += 1
            bc["hit_rate"] = round(bc["wins"] / bc["total"] * 100, 2)

            perf["recent"].insert(0, {**pick})
            perf["recent"] = perf["recent"][:50]
            settled += 1

        if settled:
            perf["last_updated"] = TODAY
            with open(picks_path, "w") as f: json.dump(picks_data, f, indent=2)
            with open(perf_path, "w") as f: json.dump(perf, f, indent=2)
            print(f"  ✓ {sport_id}: settled {settled} picks")

    # ── MLB Props settlement (pitcher strikeouts via MLB Stats API) ────────────
    _settle_mlb_props(yesterday)

    # ── NBA Props settlement (player stats via ESPN box scores) ───────────────
    _settle_nba_props(yesterday)


def _settle_mlb_props(yesterday):
    """Settle pitcher strikeout props using MLB Stats API box scores."""
    picks_path = os.path.join(DATA_DIR, "mlb_props_today.json")
    if not os.path.exists(picks_path): return
    with open(picks_path) as f: data = json.load(f)

    pending = [p for p in data.get("props", [])
               if p.get("outcome") is None and data.get("data_date") == yesterday]
    if not pending:
        return

    # Fetch yesterday's MLB games with box scores
    yesterday_compact = yesterday.replace("-","")
    try:
        sched = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={yesterday}"
                      f"&hydrate=decisions,pitchers")
        games = [g for d in sched.get("dates",[]) for g in d.get("games",[])]
    except Exception as e:
        print(f"  ! mlb_props settle: {e}"); return

    # Build pitcher K map: pitcher_name -> actual_k
    pitcher_ks = {}
    for game in games:
        gid = game.get("gamePk")
        try:
            box = fetch(f"https://statsapi.mlb.com/api/v1/game/{gid}/boxscore")
            for side in ["home","away"]:
                pitchers = box.get("teams",{}).get(side,{}).get("pitchers",[])
                all_players = box.get("teams",{}).get(side,{}).get("players",{})
                for pid in pitchers:
                    pdata = all_players.get(f"ID{pid}",{})
                    name = pdata.get("person",{}).get("fullName","")
                    stats = pdata.get("stats",{}).get("pitching",{})
                    ks = int(stats.get("strikeOuts",0))
                    ip_str = str(stats.get("inningsPitched","0.0"))
                    parts = ip_str.split(".")
                    ip = int(parts[0]) + (int(parts[1]) if len(parts)>1 else 0)/3
                    # Only count starting pitchers (IP >= 1.0)
                    if name and (ip >= 1.0 or gs_flag):
                        pitcher_ks[name] = ks
                        # Also store last name for fuzzy match
                        pitcher_ks[name.split()[-1]] = ks
        except: continue

    if not pitcher_ks:
        print(f"  ! mlb_props: no pitcher stats found"); return

    perf, perf_path = load_perf("mlb_props")
    settled = 0
    for pick in pending:
        player = pick.get("player","")
        line   = pick.get("line", 0)
        side   = pick.get("pick_side","")
        # Find actual Ks
        actual_k = pitcher_ks.get(player) or pitcher_ks.get(player.split()[-1])
        if actual_k is None: continue

        if side == "Over":
            won = actual_k > line
        else:
            won = actual_k < line

        pick["outcome"]    = "WIN" if won else "LOSS"
        pick["result"]     = 1.0 if won else 0.0
        pick["actual_k"]   = actual_k
        pick["settled_at"] = datetime.datetime.utcnow().isoformat() + "Z"

        ov = perf["overall"]
        ov["total"] += 1
        if won: ov["wins"] = ov.get("wins",0)+1
        else:   ov["losses"] = ov.get("losses",0)+1
        s = ov["wins"]+ov["losses"]
        ov["hit_rate"] = round(ov["wins"]/s*100,2) if s>0 else 0.0

        t = pick.get("confidence_tier","low")
        bc = perf["by_confidence"].setdefault(t,{"wins":0,"total":0,"hit_rate":0.0})
        bc["total"]+=1
        if won: bc["wins"]+=1
        bc["hit_rate"] = round(bc["wins"]/bc["total"]*100,2)

        perf["recent"].insert(0,{**pick})
        perf["recent"] = perf["recent"][:50]
        settled += 1

    if settled:
        perf["last_updated"] = TODAY
        with open(picks_path,"w") as f: json.dump(data,f,indent=2)
        with open(perf_path,"w") as f: json.dump(perf,f,indent=2)
        print(f"  ✓ mlb_props: settled {settled} picks (actual Ks matched)")
    else:
        print(f"  ~ mlb_props: {len(pending)} pending, no matches found")
        if pitcher_ks:
            print(f"    Available pitchers: {list(pitcher_ks.keys())[:5]}")


def _settle_nba_props(yesterday):
    """Settle NBA player props using ESPN box scores."""
    picks_path = os.path.join(DATA_DIR, "nba_props_today.json")
    if not os.path.exists(picks_path): return
    with open(picks_path) as f: data = json.load(f)

    pending = [p for p in data.get("picks", [])
               if p.get("outcome") is None]
    if not pending: return

    # Fetch ESPN NBA scoreboard for yesterday
    yesterday_compact = yesterday.replace("-","")
    try:
        scores = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
                       f"/scoreboard?dates={yesterday_compact}")
    except Exception as e:
        print(f"  ! nba_props settle: {e}"); return

    # Get box score stats per player
    player_stats = {}
    for event in scores.get("events",[]):
        eid = event.get("id","")
        try:
            box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
                        f"/summary?event={eid}")
            for box_team in box.get("boxscore",{}).get("players",[]):
                for stat_group in box_team.get("statistics",[]):
                    keys = stat_group.get("keys",[])
                    for athlete in stat_group.get("athletes",[]):
                        name = athlete.get("athlete",{}).get("displayName","")
                        vals = athlete.get("stats",[])
                        if not name or not vals: continue
                        stat_map = dict(zip(keys,vals))
                        def safe_float(v):
                            try: return float(v)
                            except: return 0.0
                        player_stats[name] = {
                            "points":   safe_float(stat_map.get("PTS",0)),
                            "rebounds": safe_float(stat_map.get("REB",0)),
                            "assists":  safe_float(stat_map.get("AST",0)),
                            "threes":   safe_float(stat_map.get("3PM",0)),
                        }
                        # Last name shortcut
                        player_stats[name.split()[-1]] = player_stats[name]
        except: continue

    if not player_stats:
        print(f"  ~ nba_props: no box score data found"); return

    perf, perf_path = load_perf("nba_props")
    settled = 0
    STAT_MAP = {"points":"points","rebounds":"rebounds","assists":"assists","threes":"threes"}

    for pick in pending:
        player = pick.get("player","")
        stat   = pick.get("stat","")
        line   = pick.get("line",0)
        side   = pick.get("direction","over")

        pstats = player_stats.get(player) or player_stats.get(player.split()[-1])
        if not pstats: continue

        stat_key = STAT_MAP.get(stat)
        if not stat_key: continue
        actual = pstats.get(stat_key,0)

        won = actual > line if side=="over" else actual < line
        pick["outcome"]    = "WIN" if won else "LOSS"
        pick["result"]     = 1.0 if won else 0.0
        pick["actual"]     = actual
        pick["settled_at"] = datetime.datetime.utcnow().isoformat()+"Z"

        ov = perf["overall"]
        ov["total"]+=1
        if won: ov["wins"]=ov.get("wins",0)+1
        else:   ov["losses"]=ov.get("losses",0)+1
        s=ov["wins"]+ov["losses"]
        ov["hit_rate"]=round(ov["wins"]/s*100,2) if s>0 else 0.0

        t=pick.get("confidence_tier","low")
        bc=perf["by_confidence"].setdefault(t,{"wins":0,"total":0,"hit_rate":0.0})
        bc["total"]+=1
        if won: bc["wins"]+=1
        bc["hit_rate"]=round(bc["wins"]/bc["total"]*100,2)
        perf["recent"].insert(0,{**pick})
        perf["recent"]=perf["recent"][:50]
        settled+=1

    if settled:
        perf["last_updated"]=TODAY
        with open(picks_path,"w") as f: json.dump(data,f,indent=2)
        with open(perf_path,"w") as f: json.dump(perf,f,indent=2)
        print(f"  ✓ nba_props: settled {settled} picks")
    else:
        print(f"  ~ nba_props: {len(pending)} pending, no matches found")


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC TO SITE
# ═══════════════════════════════════════════════════════════════════════════════

def sync_to_site():
    print("\n[Syncing to site repo]")
    if not os.path.exists(SITE_DIR):
        print(f"  ! SITE_DIR not found: {SITE_DIR}")
        print("  Set SITE_DIR at top of iq_pipeline.py to your site repo path")
        return

    import shutil
    count = 0
    for fname in os.listdir(DATA_DIR):
        if fname.endswith(".json"):
            shutil.copy2(
                os.path.join(DATA_DIR, fname),
                os.path.join(SITE_DIR, fname)
            )
            count += 1
    print(f"  Copied {count} JSON files")

    try:
        subprocess.run(["git", "-C", SITE_DIR, "add", "-A"],
                       check=True, capture_output=True)
        msg = f"{TODAY}: picks + performance update"
        subprocess.run(["git", "-C", SITE_DIR, "commit", "-m", msg],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", SITE_DIR, "push"],
                       check=True, capture_output=True)
        print("  ✓ Pushed — site deploys in ~30s")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if e.stderr else str(e)
        if "nothing to commit" in stderr:
            print("  Nothing to commit")
        else:
            print(f"  ! Git error: {stderr}")


# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY PRINT
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary():
    print("\n" + "=" * 68)
    print(f"  iQ PROJECT — PICKS FOR {TODAY}")
    print("=" * 68)
    grand_total = 0; grand_high = 0; grand_edge = 0

    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith("_picks_today.json"): continue
        with open(os.path.join(DATA_DIR, fname)) as f:
            d = json.load(f)
        picks = d.get("picks", [])
        if not picks: continue
        high      = [p for p in picks if p.get("confidence_tier") == "high"]
        with_edge = [p for p in picks if p.get("edge_pp") is not None]

        print(f"\n  {d.get('sport','').upper().replace('_',' ')} [{d.get('status')}]")
        print(f"  {len(picks)} picks | {len(high)} high confidence | {len(with_edge)} with market edge")
        print(f"  {'PICK':<32} {'MODEL':>6} {'MKT':>7} {'EDGE':>8}  TIER")
        print(f"  {'-'*62}")

        for p in sorted(picks, key=lambda x: (
            x.get("confidence_tier") != "high",
            -(x.get("edge_pp") or 0)
        )):
            pv = (p.get("model_prob_home") if p["pick_side"] == "home"
                  else p.get("model_prob_away", 0))
            mv = (p.get("market_prob_home") if p["pick_side"] == "home"
                  else p.get("market_prob_away"))
            e  = p.get("edge_pp")
            star  = "★" if p.get("confidence_tier") == "high" else " "
            mstr  = f"{mv*100:.1f}%" if mv else "  —  "
            estr  = (f"+{e:.1f}pp" if e and e > 0
                     else f"{e:.1f}pp" if e else "  —  ")
            print(f"  {star} {p['pick']:<31} {pv*100:.1f}% {mstr:>7} {estr:>8}"
                  f"  {p.get('confidence_tier','')}")

        grand_total += len(picks)
        grand_high  += len(high)
        grand_edge  += len(with_edge)

    print(f"\n{'='*68}")
    print(f"  TOTAL: {grand_total} picks | {grand_high} high confidence"
          f" | {grand_edge} with market edge")
    print(f"{'='*68}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

UMP_K_ADJ = {
    "Angel Hernandez":+1.4,"CB Bucknor":+1.2,"Lance Barksdale":+1.1,
    "James Hoye":+1.0,"Andy Fletcher":+0.9,"Jordan Baker":+0.9,
    "Phil Cuzzi":+0.8,"Alfonso Marquez":+0.8,"Mike Everitt":+0.8,
    "Bill Miller":+0.7,"Rob Drake":+0.7,"Dan Bellino":+0.7,
    "Tripp Gibson":+0.6,"Ron Kulpa":+0.6,"Chris Segal":+0.6,
    "Pat Hoberg":+0.5,"Brian Knight":+0.5,"Nic Lentz":+0.5,
    "Ryan Additon":+0.4,"Cory Blaser":+0.4,"Shane Livensparger":+0.4,
    "Willie Traynor":+0.2,"Scott Barry":+0.1,"Joe West":+0.1,
    "Mark Carlson":+0.1,"Chad Fairchild":+0.1,
    "John Libka":0.0,"Mike Muchlinski":0.0,"Manny Gonzalez":0.0,
    "Brennan Miller":0.0,"Nick Mahrley":0.0,"Brock Ballou":0.0,
    "Jim Reynolds":-0.1,"Ted Barrett":-0.1,"Hunter Wendelstedt":-0.1,
    "Tony Randazzo":-0.2,"Fieldin Culbreth":-0.2,
    "Doug Eddings":-0.5,"Ed Hickox":-0.5,"Mark Wegner":-0.5,
    "Tim Timmons":-0.6,"Tom Hallion":-0.6,
    "Jerry Meals":-0.7,"Sam Holbrook":-0.7,"Dan Iassogna":-0.8,
}

def poisson_over(lam, line):
    k = int(line)
    prob = sum((math.exp(-lam)*lam**i)/math.factorial(i) for i in range(k+1))
    return round(1-prob, 4)



def run_mlb_props():
    print("\n[MLB Props — Pitcher Strikeouts]")
    LEAGUE_K_PCT = 0.224

    # Schedule with umpires
    sched = fetch(f"https://statsapi.mlb.com/api/v1/schedule"
                  f"?sportId=1&date={TODAY}&hydrate=probablePitcher,team,officials")
    games = []
    for d in sched.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Preview": continue
            officials = g.get("officials", [])
            hp = next((o for o in officials if o.get("officialType") == "Home Plate"), None)
            ump = hp.get("official", {}).get("fullName", "Unknown") if hp else "Unknown"
            games.append({
                "game_id":  str(g.get("gamePk", "")),
                "home":     g["teams"]["home"]["team"]["name"],
                "home_id":  g["teams"]["home"]["team"]["id"],
                "away":     g["teams"]["away"]["team"]["name"],
                "away_id":  g["teams"]["away"]["team"]["id"],
                "home_sp":  g["teams"]["home"].get("probablePitcher", {}),
                "away_sp":  g["teams"]["away"].get("probablePitcher", {}),
                "umpire":   ump,
                "ump_adj":  UMP_K_ADJ.get(ump, 0.0),
            })

    # SP stats
    sp_stats = {}
    sp_ids = set()
    for g in games:
        if g["home_sp"].get("id"): sp_ids.add((g["home_sp"]["id"], g["home_sp"].get("fullName", "")))
        if g["away_sp"].get("id"): sp_ids.add((g["away_sp"]["id"], g["away_sp"].get("fullName", "")))
    for pid, pname in sp_ids:
        try:
            data = fetch(f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                         f"?stats=season&group=pitching&season=2026")
            splits = data.get("stats", [{}])[0].get("splits", [])
            if splits:
                s = splits[0].get("stat", {})
                ip_str = str(s.get("inningsPitched", "0.0"))
                parts = ip_str.split(".")
                ip = int(parts[0]) + (int(parts[1]) if len(parts) > 1 else 0) / 3
                gs = int(s.get("gamesStarted", 0))
                k  = int(s.get("strikeOuts", 0))
                k9 = round(k / ip * 9, 2) if ip > 0 else 8.0
                sp_stats[pid] = {"name": pname, "ip": ip, "gs": gs,
                                 "k9": k9, "avg_ip": round(ip/gs, 1) if gs > 0 else 5.0}
        except: pass

    # Team K%
    team_k = {}
    for g in games:
        for tid in [g["home_id"], g["away_id"]]:
            if tid in team_k: continue
            try:
                data = fetch(f"https://statsapi.mlb.com/api/v1/teams/{tid}/stats"
                             f"?stats=season&group=hitting&season=2026")
                s = data.get("stats", [{}])[0].get("splits", [{}])
                if s:
                    st = s[0].get("stat", {})
                    ab = int(st.get("atBats", 0) or 0)
                    so = int(st.get("strikeOuts", 0) or 0)
                    team_k[tid] = round(so/ab, 4) if ab > 0 else LEAGUE_K_PCT
            except:
                team_k[tid] = LEAGUE_K_PCT

    # Odds events
    events = fetch(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events"
                   f"?apiKey={ODDS_KEY}")
    event_map = {}
    for ev in events:
        ht = ev["home_team"]; at = ev["away_team"]
        event_map[f"{ht.split()[-1].lower()}_{at.split()[-1].lower()}"] = ev["id"]

    all_props = []
    for g in games:
        key = f"{g['home'].split()[-1].lower()}_{g['away'].split()[-1].lower()}"
        event_id = event_map.get(key)
        if not event_id: continue
        try:
            odds_data = fetch(f"https://api.the-odds-api.com/v4/sports/baseball_mlb"
                              f"/events/{event_id}/odds?apiKey={ODDS_KEY}"
                              f"&regions=us&markets=pitcher_strikeouts&oddsFormat=american")
        except: continue

        pitcher_lines = {}
        for bk in odds_data.get("bookmakers", []):
            if bk["key"] not in ("fanduel","draftkings","betmgm","betonlineag","fanatics"): continue
            for mk in bk.get("markets", []):
                if mk["key"] != "pitcher_strikeouts": continue
                for o in mk.get("outcomes", []):
                    pn = o.get("description", "")
                    if pn not in pitcher_lines:
                        pitcher_lines[pn] = {"over":[], "under":[], "line": o.get("point", 4.5)}
                    if o["name"] == "Over":  pitcher_lines[pn]["over"].append(o["price"])
                    if o["name"] == "Under": pitcher_lines[pn]["under"].append(o["price"])
                    pitcher_lines[pn]["line"] = o.get("point", 4.5)

        for sp_key, opp_id in [("home_sp", g["away_id"]), ("away_sp", g["home_id"])]:
            sp = g[sp_key]
            if not sp.get("id"): continue
            pname = sp.get("fullName", "")
            stats = sp_stats.get(sp["id"])
            odds_entry = next(
                (e for n, e in pitcher_lines.items()
                 if pname.split()[-1].lower() in n.lower() or n.split()[-1].lower() in pname.lower()),
                None
            )
            if not odds_entry or not odds_entry["over"] or not odds_entry["under"]: continue
            if len(odds_entry["over"]) < 2: continue

            line = odds_entry["line"]
            avg_oi = sum(to_imp(p) for p in odds_entry["over"]) / len(odds_entry["over"])
            avg_ui = sum(to_imp(p) for p in odds_entry["under"]) / len(odds_entry["under"])
            nv_o, nv_u = devig(avg_oi, avg_ui)

            k9      = stats["k9"] if stats and stats["gs"] >= 2 else 8.0
            proj_ip = min(stats["avg_ip"], 6.0) if stats and stats["gs"] >= 2 else 5.0
            if stats and stats["gs"] < 4: proj_ip = min(proj_ip, 5.5)

            opp_kpct = team_k.get(opp_id, LEAGUE_K_PCT)
            adj_k9   = k9 * (0.70 + 0.30 * (opp_kpct / LEAGUE_K_PCT))
            proj_k   = (adj_k9 * proj_ip / 9) + g["ump_adj"]

            mo = poisson_over(proj_k, line)
            mu = 1 - mo
            eo = round((mo - nv_o) * 100, 2)
            eu = round((mu - nv_u) * 100, 2)

            best_side  = "Over" if eo >= eu else "Under"
            best_model = mo if best_side == "Over" else mu
            best_edge  = eo if best_side == "Over" else eu
            best_mkt   = nv_o if best_side == "Over" else nv_u

            if abs(best_edge) > 20 or abs(best_edge) < 3: continue

            # Structural filter: no overs on low lines (<4.5K) unless SP has
            # demonstrated ability to pitch deep (avg_ip >= 5.0, gs >= 5)
            # Prevents early-KO losses on low over lines
            if best_side == "Over" and line < 4.5:
                gs  = stats["gs"]  if stats else 0
                avg_ip = stats["avg_ip"] if stats else 0
                if gs < 5 or avg_ip < 5.0:
                    continue

            all_props.append({
                "pick_id":   f"mlb-props-{TODAY}-{sp['id']}-k",
                "sport":     "mlb", "market": "pitcher_strikeouts",
                "game":      f"{g['away']} @ {g['home']}",
                "player":    pname,
                "pick":      f"{pname} {best_side} {line} Ks",
                "pick_side": best_side, "line": line,
                "proj_k":    round(proj_k, 2),
                "umpire":    g["umpire"], "ump_adj": g["ump_adj"],
                "opp_k_pct": round(opp_kpct, 3),
                "model_prob":  round(best_model, 4),
                "market_prob": round(best_mkt, 4),
                "edge_pp":   best_edge,
                "confidence_tier": "high" if best_model>=0.65 else "medium" if best_model>=0.60 else "low",
                "result": None, "outcome": None,
            })

    ts = datetime.datetime.utcnow().isoformat() + "Z"
    out = {"schema_version":"1.0","sport":"mlb","market_type":"player_props",
           "generated_at":ts,"data_date":TODAY,
           "props":sorted(all_props,key=lambda x:-abs(x["edge_pp"])),
           "summary":{"total_props":len(all_props),
                      "high_confidence":sum(1 for p in all_props if p["confidence_tier"]=="high")}}
    # Write today file + dated archive
    path = os.path.join(DATA_DIR, "mlb_props_today.json")
    with open(path,"w") as f: json.dump(out,f,indent=2)
    archive = os.path.join(DATA_DIR, f"mlb_props_{TODAY}.json")
    with open(archive,"w") as f: json.dump(out,f,indent=2)
    print(f"  -> {len(all_props)} props written")


# ═══════════════════════════════════════════════════════════════════════════════
# GOLF — Masters Tournament (DataGolf + Odds API)
# ═══════════════════════════════════════════════════════════════════════════════

DG_KEY = os.environ.get("DATAGOLF_KEY", "26c9f2ab8405d589166a8e2fb214")

AUGUSTA_FIT = {
    "Scheffler, Scottie":  +0.40,
    "McIlroy, Rory":       +0.35,
    "Rahm, Jon":           +0.30,
    "Aberg, Ludvig":       +0.28,
    "Matsuyama, Hideki":   +0.25,
    "Schauffele, Xander":  +0.22,
    "Spieth, Jordan":      +0.20,
    "Rose, Justin":        +0.18,
    "Young, Cameron":      +0.15,
    "Lee, Min Woo":        +0.15,
    "DeChambeau, Bryson":  +0.12,
    "Kim, Si Woo":         +0.12,
    "Conners, Corey":      +0.10,
    "Fleetwood, Tommy":    +0.10,
    "MacIntyre, Robert":   +0.08,
    "Straka, Sepp":        +0.08,
    "Scott, Adam":         +0.08,
    "Fitzpatrick, Matt":   +0.06,
    "Reed, Patrick":       +0.06,
    "Koepka, Brooks":      +0.05,
    "Day, Jason":          +0.05,
    "Morikawa, Collin":    +0.02,
    "Hovland, Viktor":     -0.08,
    "Hatton, Tyrrell":     -0.10,
    "Thomas, Justin":      -0.03,
    "Lowry, Shane":        -0.05,
    "Cantlay, Patrick":    -0.03,
}

def run_golf_masters():
    print("\n[Golf — Masters Tournament]")
    if not DG_KEY:
        print("  ! No DATAGOLF_KEY — set env var")
        return

    def dg_to_std(s):
        parts = s.split(", ")
        return f"{parts[1]} {parts[0]}" if len(parts) == 2 else s

    try:
        skills_raw = fetch(f"https://feeds.datagolf.com/preds/skill-ratings?tour=pga&file_format=json&key={DG_KEY}")
        skills = {p["dg_id"]: p for p in skills_raw.get("players", [])}

        pt = fetch(f"https://feeds.datagolf.com/preds/pre-tournament?tour=pga&odds_format=percent&file_format=json&key={DG_KEY}")
        dg_preds = {p["dg_id"]: p for p in pt.get("baseline", [])}

        ip = fetch(f"https://feeds.datagolf.com/preds/in-play?tour=pga&dead_heat=no&odds_format=percent&file_format=json&key={DG_KEY}")
        ip_preds = {p["dg_id"]: p for p in ip.get("data", [])}

        field_data = fetch(f"https://feeds.datagolf.com/field-updates?tour=pga&file_format=json&key={DG_KEY}")
        field = field_data.get("field", [])
        print(f"  Field: {len(field)} players | Event: {field_data.get('event_name','?')}")
    except Exception as e:
        print(f"  ! DataGolf fetch error: {e}")
        return

    # Market odds
    market_prob = {}; best_price = {}
    try:
        odds_raw = fetch(f"https://api.the-odds-api.com/v4/sports/golf_masters_tournament_winner/odds"
                         f"?apiKey={ODDS_KEY}&regions=us,uk&markets=outrights&oddsFormat=american")
        raw_imp = {}
        for market in odds_raw:
            for bk in market.get("bookmakers", []):
                if bk["key"] not in ("pinnacle","draftkings","fanduel","betmgm","williamhill"): continue
                for mk in bk.get("markets", []):
                    for o in mk.get("outcomes", []):
                        n = o["name"]; p = o["price"]
                        if n not in raw_imp: raw_imp[n] = []
                        raw_imp[n].append(to_imp(p))
                        if n not in best_price or (p > 0 and (best_price[n] < 0 or p > best_price[n])):
                            best_price[n] = p
        total_imp = sum(sum(v)/len(v) for v in raw_imp.values())
        market_prob = {n: sum(v)/len(v)/total_imp for n, v in raw_imp.items()}
        print(f"  Odds: {len(market_prob)} players priced")
    except Exception as e:
        print(f"  ! Odds fetch error: {e}")

    results = []
    for p in field:
        dg_id = p["dg_id"]; dg_name = p["player_name"]
        std_name = dg_to_std(dg_name)
        ip_data = ip_preds.get(dg_id, {}); pt_data = dg_preds.get(dg_id, {})
        sk = skills.get(dg_id, {})

        dg_base = ip_data.get("win", pt_data.get("win", 0)) or 0
        fit_adj = AUGUSTA_FIT.get(dg_name, 0.0)
        raw_model = max(0.0001, dg_base + fit_adj * 0.03)

        mkt = market_prob.get(std_name)
        results.append({
            "pick_id":     f"golf-masters-2026-{dg_id}",
            "player":      std_name,
            "dg_name":     dg_name,
            "dg_id":       dg_id,
            "model_prob":  raw_model,
            "dg_base":     dg_base,
            "market_prob": mkt,
            "best_price":  best_price.get(std_name),
            "edge_pp":     round((raw_model - mkt)*100, 2) if mkt else None,
            "aug_fit":     fit_adj,
            "sg_total":    sk.get("sg_total", 0),
            "sg_app":      sk.get("sg_app", 0),
            "sg_ott":      sk.get("sg_ott", 0),
            "sg_arg":      sk.get("sg_arg", 0),
            "sg_putt":     sk.get("sg_putt", 0),
            "dg_top10":    ip_data.get("top_10", pt_data.get("top_10", 0)) or 0,
            "thru":        ip_data.get("thru", 0),
            "score":       ip_data.get("current_score", 0),
            "today":       ip_data.get("today", 0),
            "pos":         ip_data.get("current_pos", "--"),
            "confidence_tier": "high" if (mkt and raw_model > mkt*1.2) else "medium",
            "result": None, "outcome": None,
        })

    # Renormalize
    total = sum(r["model_prob"] for r in results)
    for r in results:
        r["model_prob"] = round(r["model_prob"]/total, 4)
        if r["market_prob"] and r["market_prob"] > 0:
            r["edge_pp"] = round((r["model_prob"] - r["market_prob"])*100, 2)

    results.sort(key=lambda x: -x["model_prob"])

    ts = datetime.datetime.utcnow().isoformat() + "Z"
    out = {
        "schema_version": "1.0",
        "sport": "golf",
        "tournament": "Masters Tournament 2026",
        "generated_at": ts,
        "data_date": TODAY,
        "status": "ACTIVE",
        "model_version": "v0.3-dg-augmented",
        "current_round": ip.get("info", {}).get("current_round", 1),
        "last_updated":  ip.get("info", {}).get("last_update", ""),
        "methodology": {
            "base": "DataGolf in-play win probability",
            "adjustment": "Augusta fit × 0.03 win prob delta",
            "market": "Devigged Pinnacle/DK/FD/BetMGM outrights",
            "note": "Run pre-round for best edge — market lags during round, model lags live scores",
        },
        "picks": [r for r in results if r.get("edge_pp") and r["edge_pp"] >= 1.0],
        "field": results,
        "summary": {
            "total_players": len(results),
            "positive_edge": sum(1 for r in results if r.get("edge_pp") and r["edge_pp"] > 0),
            "top_picks": [r["player"] for r in results if r.get("edge_pp") and r["edge_pp"] >= 1.0][:5],
        }
    }
    path = os.path.join(DATA_DIR, "golf_masters_picks.json")
    with open(path, "w") as f: json.dump(out, f, indent=2)

    picks = out["picks"]
    print(f"  -> {len(picks)} picks with ≥1.0pp edge | {len(results)} players modeled")
    for r in picks[:5]:
        p = f"+{r['best_price']}" if r.get('best_price') and r['best_price']>0 else "?"
        print(f"     ★ {r['player']:<26} model={r['model_prob']*100:.1f}% mkt={r['market_prob']*100:.1f}% edge=+{r['edge_pp']:.1f}pp @ {p}")
def run_nba_props():
    """Call the existing validated nba_props_model.py and copy output to DATA_DIR."""
    print("\n[NBA Props — backtested model]")
    import subprocess, shutil
    model_path = os.path.expanduser("~/Desktop/basketball_iq/nba_props_model.py")
    if not os.path.exists(model_path):
        print("  ! nba_props_model.py not found at ~/Desktop/basketball_iq/")
        return
    result = subprocess.run(
        ["python3", model_path],
        capture_output=True, text=True,
        cwd=os.path.expanduser("~/Desktop/basketball_iq"),
        env={**os.environ, "ODDS_API_KEY": ODDS_KEY}
    )
    if result.returncode != 0:
        print(f"  ! Model error: {result.stderr[-300:]}")
        return
    src = os.path.expanduser("~/Desktop/basketball_iq/output/nba_props_today.json")
    if os.path.exists(src):
        dst = os.path.join(DATA_DIR, "nba_props_today.json")
        shutil.copy2(src, dst)
        with open(dst) as f: d = json.load(f)
        picks = d.get("picks", [])
        print(f"  -> {len(picks)} props written (from {len(d.get('projections',[]))} projections)")
    else:
        print("  ! No output file generated")

SPORT_RUNNERS = {
    "mlb":          run_mlb,
    "nhl":          run_nhl,
    "ncaa_baseball": run_ncaa_baseball,
    "soccer":       run_soccer,
    "nfl":          run_nfl,
    "mlb_props":   run_mlb_props,
    "nba_props":   run_nba_props,
    "golf_masters": run_golf_masters,
}

def main():
    import argparse
    parser = argparse.ArgumentParser(description="iQ Project Master Pipeline")
    parser.add_argument("--picks-only",  action="store_true")
    parser.add_argument("--settle-only", action="store_true")
    parser.add_argument("--no-sync",     action="store_true")
    parser.add_argument("--sport",       default="all")
    args = parser.parse_args()

    start = time.time()
    print(f"iQ Project Pipeline — {TODAY}")
    print("=" * 68)

    if not args.settle_only:
        print("\n── GENERATING PICKS ──")
        if args.sport == "all":
            for name, fn in SPORT_RUNNERS.items():
                try:    fn()
                except Exception as e:
                    import traceback
                    print(f"  ✗ {name}: {e}")
                    traceback.print_exc()
        else:
            fn = SPORT_RUNNERS.get(args.sport)
            if fn:
                try:    fn()
                except Exception as e:
                    import traceback; print(f"  ✗ {args.sport}: {e}"); traceback.print_exc()
            else:
                print(f"Unknown sport: {args.sport}")
                print(f"Available: {', '.join(SPORT_RUNNERS.keys())}")

        print_summary()

    if not args.picks_only:
        settle_all()

    if not args.no_sync:
        sync_to_site()

    elapsed = round(time.time() - start, 1)
    print(f"\n✓ Done in {elapsed}s")

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# MLB PROPS — Pitcher Strikeouts
# ═══════════════════════════════════════════════════════════════════════════════

# Umpire K-rate adjustment (extra Ks per game vs league average)
