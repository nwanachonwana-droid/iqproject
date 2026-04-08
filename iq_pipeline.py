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

SPORT_RUNNERS = {
    "mlb":          run_mlb,
    "nhl":          run_nhl,
    "ncaa_baseball": run_ncaa_baseball,
    "soccer":       run_soccer,
    "nfl":          run_nfl,
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
