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
SITE_DIR = os.path.expanduser("/workspaces/iqproject")   # ← change to your site repo
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
    # Archive previous day before overwriting
    if os.path.exists(path):
        try:
            with open(path) as _f: _old = json.load(_f)
            _old_date = _old.get("data_date","")
            if _old_date and _old_date != TODAY:
                _archive = os.path.join(DATA_DIR, f"{sport_id}_picks_{_old_date}.json")
                if not os.path.exists(_archive):
                    with open(_archive,"w") as _f: json.dump(_old,_f,indent=2)
        except: pass
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


# Soccer team name normalization — CSV name → Odds API name (exact, verified)
SOCCER_NAME_MAP = {
    # EPL (football-data → Odds API)
    "West Ham":       "West Ham United",
    "Wolves":         "Wolverhampton Wanderers",
    "Tottenham":      "Tottenham Hotspur",
    "Man United":     "Manchester United",
    "Man City":       "Manchester City",
    "Nott'm Forest":  "Nottingham Forest",
    "Sheffield Weds": "Sheffield Wednesday",
    "Leicester":      "Leicester City",
    "Leeds":          "Leeds United",
    "Brighton":       "Brighton and Hove Albion",
    "Luton":          "Luton Town",
    "Ipswich":        "Ipswich Town",
    # La Liga
    "Ath Bilbao":     "Athletic Bilbao",
    "Ath Madrid":     "Atlético Madrid",
    "Betis":          "Real Betis",
    "Celta":          "Celta Vigo",
    "Espanol":        "Espanyol",
    "Sociedad":       "Real Sociedad",
    "Vallecano":      "Rayo Vallecano",
    "Valladolid":     "Real Valladolid",
    "Alaves":         "Deportivo Alavés",
    "Getafe":         "Getafe",
    "Girona":         "Girona",
    "Villarreal":     "Villarreal",
    "Osasuna":        "CA Osasuna",
    "Sevilla":        "Sevilla",
    "Granada":        "Granada CF",
    "Las Palmas":     "UD Las Palmas",
    "Mallorca":       "RCD Mallorca",
    "Leganes":        "CD Leganés",
    "Levante":        "Levante",
    "Elche":          "Elche CF",
    # Bundesliga
    "Dortmund":       "Borussia Dortmund",
    "Ein Frankfurt":  "Eintracht Frankfurt",
    "FC Koln":        "1. FC Köln",
    "Leverkusen":     "Bayer Leverkusen",
    "Mainz":          "FSV Mainz 05",
    "M'gladbach":     "Borussia Mönchengladbach",
    "Wolfsburg":      "VfL Wolfsburg",
    "Hoffenheim":     "TSG Hoffenheim",
    "Stuttgart":      "VfB Stuttgart",
    "Hertha":         "Hertha BSC",
    "Heidenheim":     "1. FC Heidenheim",
    "St Pauli":       "FC St. Pauli",
    "Hamburg":        "Hamburger SV",
    "Schalke":        "FC Schalke 04",
    "Freiburg":       "SC Freiburg",
    "Greuther Furth": "SpVgg Greuther Fürth",
    "Paderborn":      "SC Paderborn 07",
    "Regensburg":     "Jahn Regensburg",
    # Serie A (football-data names match Odds API well — minimal mapping needed)
    "Inter":          "Inter Milan",
    "Milan":          "AC Milan",
    "Roma":           "AS Roma",
    "Atalanta":       "Atalanta BC",
    "Verona":         "Hellas Verona",
    "Empoli":         "Empoli",
    "Lecce":          "Lecce",
    "Sassuolo":       "Sassuolo",
    "Cremonese":      "Cremonese",
    # Ligue 1
    "Paris SG":       "Paris Saint Germain",
    "Lyon":           "Olympique Lyonnais",
    "Monaco":         "AS Monaco",
    "Lens":           "RC Lens",
    "Rennes":         "Stade Rennes",
    "Reims":          "Stade de Reims",
    "St Etienne":     "Saint-Etienne",
    "Nantes":         "Nantes",
    "Nice":           "Nice",
    "Lille":          "Lille",
    "Marseille":      "Marseille",
    "Toulouse":       "Toulouse",
    "Brest":          "Brest",
    "Metz":           "Metz",
    "Lorient":        "Lorient",
    "Strasbourg":     "Strasbourg",
    "Auxerre":        "Auxerre",
    "Angers":         "Angers",
    "Le Havre":       "Le Havre",
    "Paris FC":       "Paris FC",
}
# Reverse map: Odds API → CSV name
SOCCER_NAME_MAP_REV = {v:k for k,v in SOCCER_NAME_MAP.items()}

def find_team_isr(odds_name, isr_dict):
    """Map Odds API team name → CSV name → ISR value. No fuzzy — exact only."""
    # Direct hit (some names match exactly e.g. Serie A)
    if odds_name in isr_dict:
        return isr_dict[odds_name]
    # Reverse map: odds_name → csv_name
    csv_name = SOCCER_NAME_MAP_REV.get(odds_name)
    if csv_name and csv_name in isr_dict:
        return isr_dict[csv_name]
    # Forward map (shouldn't be needed but safety)
    mapped = SOCCER_NAME_MAP.get(odds_name)
    if mapped and mapped in isr_dict:
        return isr_dict[mapped]
    return None

def run_soccer():
    import io, csv as _csv
    LEAGUE_CSVS = [
        ("E0",  "soccer_epl",               "EPL"),
        ("SP1", "soccer_spain_la_liga",      "La Liga"),
        ("D1",  "soccer_germany_bundesliga", "Bundesliga"),
        ("I1",  "soccer_italy_serie_a",      "Serie A"),
        ("F1",  "soccer_france_ligue_one",   "Ligue 1"),
    ]
    UCL_KEYS = {"soccer_uefa_champs_league", "soccer_uefa_europa_league"}
    ALL_ODDS = LEAGUE_CSVS + [
        ("UCL", "soccer_uefa_champs_league",          "UEFA Champions League"),
        ("UEL", "soccer_uefa_europa_league",           "UEFA Europa League"),
        ("MLS", "soccer_usa_mls",                      "MLS"),
        ("NED", "soccer_netherlands_eredivisie",        "Eredivisie"),
        ("POR", "soccer_portugal_primeira_liga",        "Primeira Liga"),
    ]
    print("\n[Soccer — Dixon-Coles ISR + Pythagenpat multi-league]")

    def fetch_csv(code):
        url = f"https://www.football-data.co.uk/mmz4281/2526/{code}.csv"
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent":"iqproject/1.0"})
        r = urllib.request.urlopen(req, timeout=15)
        return list(_csv.DictReader(io.StringIO(r.read().decode("utf-8","replace"))))

    def build_isr(rows):
        stats = {}
        for row in rows:
            try:
                ht = row.get("HomeTeam","").strip()
                at = row.get("AwayTeam","").strip()
                hg = int(row.get("FTHG") or row.get("HG") or 0)
                ag = int(row.get("FTAG") or row.get("AG") or 0)
            except: continue
            if not ht or not at: continue
            for t in (ht,at):
                if t not in stats: stats[t]={"gf":0,"ga":0,"gp":0}
            stats[ht]["gf"]+=hg; stats[ht]["ga"]+=ag; stats[ht]["gp"]+=1
            stats[at]["gf"]+=ag; stats[at]["ga"]+=hg; stats[at]["gp"]+=1
        isr = {}
        for t,s in stats.items():
            if s["gp"]<3: continue
            gf=s["gf"]/s["gp"]; ga=s["ga"]/s["gp"]
            exp=(gf+ga)**0.285
            pyth=gf**exp/(gf**exp+ga**exp) if (gf+ga)>0 else 0.5
            K=10
            isr[t]=(s["gp"]*pyth+K*0.5)/(s["gp"]+K)
        return isr

    def wp(isr_h, isr_a, neutral=False):
        # ISR ratio method: relative strength directly gives win probability
        # p_home = isr_h / (isr_h + isr_a) then apply home advantage
        ha = 0.00 if neutral else 0.06
        raw = isr_h / (isr_h + isr_a) if (isr_h + isr_a) > 0 else 0.5
        # Apply home advantage as additive boost compressed toward extremes
        p = raw + ha * raw * (1 - raw) * 2
        p = max(0.05, min(0.90, p))
        # Empirical draw rate — higher when teams are evenly matched
        d = 0.28 - 0.10 * abs(p - 0.5)
        p_win  = p * (1 - d)
        p_loss = (1 - p) * (1 - d)
        return p_win, p_loss, d

    # League strength coefficients — UEFA club coefficients 2025/26, EPL=1.0 baseline
    LEAGUE_STRENGTH = {
        "soccer_epl":                1.000,
        "soccer_spain_la_liga":      0.980,
        "soccer_germany_bundesliga": 0.950,
        "soccer_italy_serie_a":      0.940,
        "soccer_france_ligue_one":   0.880,
    }

    # Build ISR per league, normalize to EPL baseline, build UCL pool
    league_isr={}; ucl_pool={}
    for code,odds_key,label in LEAGUE_CSVS:
        try:
            rows=fetch_csv(code)
            isr_raw=build_isr(rows)
            strength=LEAGUE_STRENGTH.get(odds_key,0.90)
            # Normalize: compress ISR toward 0.5 by league strength factor
            isr={t: 0.5+(v-0.5)*strength for t,v in isr_raw.items()}
            league_isr[odds_key]=isr
            for t,r in isr.items(): ucl_pool[t]=r
            print(f"  {label}: {len(isr)} teams | {len(rows)} matches")
        except Exception as e:
            print(f"  {label}: FAILED — {e}")

    all_picks=[]
    for code,odds_key,label in ALL_ODDS:
        isr=league_isr.get(odds_key, ucl_pool)
        neutral=odds_key in UCL_KEYS
        url=(f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
             f"?apiKey={ODDS_KEY}&regions=us&markets=h2h&oddsFormat=american")
        try: games=fetch(url)
        except: continue
        matched=0
        for g in games:
            ht=g["home_team"]; at=g["away_team"]
            isr_h=find_team_isr(ht,isr)
            isr_a=find_team_isr(at,isr)
            if isr_h is None or isr_a is None: continue
            matched+=1
            model_h,model_a,model_d=wp(isr_h,isr_a,neutral)
            home_imps,away_imps,draw_imps=[],[],[]
            for bk in g.get("bookmakers",[]):
                for mk in bk.get("markets",[]):
                    if mk["key"]!="h2h": continue
                    oc={o["name"]:o["price"] for o in mk["outcomes"]}
                    if ht in oc: home_imps.append(to_imp(oc[ht]))
                    if at in oc: away_imps.append(to_imp(oc[at]))
                    dr=[k for k in oc if k not in (ht,at)]
                    if dr: draw_imps.append(to_imp(oc[dr[0]]))
            if not home_imps: continue
            raw_h=sum(home_imps)/len(home_imps)
            raw_a=sum(away_imps)/len(away_imps)
            raw_d=sum(draw_imps)/len(draw_imps) if draw_imps else 0.265
            tot=raw_h+raw_a+raw_d
            nv_h=raw_h/tot; nv_a=raw_a/tot
            ep_h=round((model_h-nv_h)*100,2)
            ep_a=round((model_a-nv_a)*100,2)
            for pick_team,pick_side,model_p,nv_p,ep in [
                (ht,"home",model_h,nv_h,ep_h),
                (at,"away",model_a,nv_a,ep_a),
            ]:
                if ep<3.0 or model_p<0.38: continue
                all_picks.append({
                    "home_team":ht,"away_team":at,"league":label,
                    "pick":pick_team,"pick_side":pick_side,
                    "model_prob_home":round(model_h,4),
                    "model_prob_away":round(model_a,4),
                    "market_prob_home":round(nv_h,4),
                    "market_prob_away":round(nv_a,4),
                    "draw_prob":round(raw_d/tot,4),
                    "edge_pp":ep,
                    "confidence_tier":tier(model_p),
                    "game_time_utc":g.get("commence_time"),
                    "outcome":None,"result":None,
                })
        if matched>0 or label in ("EPL","La Liga","Bundesliga","Serie A","Ligue 1"):
            print(f"  {label}: {len(games)} games | {matched} matched")
    print(f"  -> {len(all_picks)} picks with ≥3.0pp edge")
    write_picks("soccer", all_picks, "ACTIVE")


def run_nfl():
    # Off-season until September 2026
    print("\n[NFL — off-season]")
    write_picks("nfl", [], "RESEARCH")


# ═══════════════════════════════════════════════════════════════════════════════
# SETTLEMENT — fetch yesterday's results, update performance logs
# ═══════════════════════════════════════════════════════════════════════════════


def _settle_soccer(yesterday):
    """Settle soccer picks using ESPN soccer scoreboards."""
    picks_path = os.path.join(DATA_DIR, "soccer_picks_today.json")
    archive_path = os.path.join(DATA_DIR, f"soccer_picks_{yesterday}.json")
    use_path = archive_path if os.path.exists(archive_path) else picks_path
    if not os.path.exists(use_path): return
    with open(use_path) as f: data = json.load(f)
    pending = [p for p in data.get("picks",[])
               if p.get("outcome") is None and
               (p.get("game_time_utc","") or "").startswith(yesterday)]
    if not pending:
        print(f"  ~ soccer: no pending picks for {yesterday}")
        return
    leagues = ["eng.1","esp.1","ger.1","ita.1","fra.ligue_1",
               "uefa.champions","uefa.europa","usa.1","eng.championship"]
    winners = {}
    yesterday_compact = yesterday.replace("-","")
    for league in leagues:
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={yesterday_compact}"
            scores = fetch(url)
            for event in scores.get("events",[]):
                for comp in event.get("competitions",[{}]):
                    for c in comp.get("competitors",[]):
                        name = c["team"]["displayName"]
                        winners[name] = c.get("winner", False)
        except: pass
    settled = 0
    perf_path = os.path.join(DATA_DIR, "soccer_performance_log.json")
    perf = json.load(open(perf_path)) if os.path.exists(perf_path) else {"sport":"soccer","last_updated":yesterday,"overall":{"total":0,"wins":0,"losses":0,"hit_rate":0.0,"roi_flat":0.0},"by_confidence":{},"recent":[]}
    for pick in pending:
        team = pick["pick"]
        if team in winners:
            won = winners[team]
            pick["outcome"] = "WIN" if won else "LOSS"
            pick["result"] = 1.0 if won else 0.0
            pick["settled_at"] = datetime.datetime.utcnow().isoformat() + "Z"
            ov = perf["overall"]
            ov["total"] += 1
            ov["wins"] = ov.get("wins",0) + (1 if won else 0)
            ov["losses"] = ov.get("losses",0) + (0 if won else 1)
            s = ov["wins"] + ov["losses"]
            ov["hit_rate"] = round(ov["wins"]/s*100,2) if s else 0
            ov["roi_flat"] = round(((ov["wins"]*0.909)-ov["losses"])/ov["total"]*100,2)
            t = pick.get("confidence_tier","low")
            bc = perf["by_confidence"].setdefault(t,{"wins":0,"total":0,"hit_rate":0.0})
            bc["total"] += 1
            if won: bc["wins"] += 1
            bc["hit_rate"] = round(bc["wins"]/bc["total"]*100,2)
            perf["recent"].insert(0, dict(pick))
            settled += 1
    if settled:
        perf["recent"] = perf["recent"][:50]
        perf["last_updated"] = yesterday
        with open(perf_path,"w") as f: json.dump(perf,f,indent=2)
        with open(use_path,"w") as f: json.dump(data,f,indent=2)
        print(f"  ✓ soccer: settled {settled} picks")
    else:
        print(f"  ~ soccer: {len(pending)} pending, no matches found in {len(winners)} results")

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
        archive_path = os.path.join(DATA_DIR, f"{sport_id}_picks_{yesterday}.json")
        picks_path = os.path.join(DATA_DIR, f"{sport_id}_picks_today.json")
        if os.path.exists(archive_path):
            use_path = archive_path
        elif os.path.exists(picks_path):
            use_path = picks_path
        else:
            continue
        with open(use_path) as f:
            picks_data = json.load(f)

        pending = [p for p in picks_data.get("picks", [])
                   if p.get("outcome") is None and
                   (p.get("game_time_utc") or "").startswith(yesterday)]
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
            with open(use_path, "w") as f: json.dump(picks_data, f, indent=2)
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
def run_nba():
    """NBA game picks — Pythagenpat ISR from ESPN standings + B2B adjustment."""
    print("\n[NBA — Pythagenpat ISR + B2B adjustment]")

    # Fetch standings
    url = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings?season=2026&type=0"
    try:
        d = fetch(url)
    except Exception as e:
        print(f"  ! Standings fetch failed: {e}")
        write_picks("nba", [], "EXPERIMENTAL")
        return

    # Build ISR from point differential
    isr = {}
    for conf in d.get("children", []):
        for e in conf.get("standings", {}).get("entries", []):
            name = e["team"]["displayName"]
            stats = {}
            for s in e.get("stats", []):
                try: stats[s["name"]] = float(s.get("value") or 0)
                except: stats[s["name"]] = 0
            ppg  = stats.get("avgPointsFor", 0)
            papg = stats.get("avgPointsAgainst", 0)
            if ppg + papg == 0:
                continue
            exp  = (ppg + papg) ** 0.285
            pyth = ppg**exp / (ppg**exp + papg**exp)
            K    = 10
            gp   = stats.get("wins", 0) + stats.get("losses", 0)
            isr[name] = (gp * pyth + K * 0.5) / (gp + K)

    print(f"  Ratings: {len(isr)} teams")

    # Check B2B schedule from ESPN scoreboard
    def get_b2b():
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={yesterday}"
        try:
            d = fetch(url)
            played = set()
            for ev in d.get("events", []):
                for comp in ev.get("competitions", []):
                    for team in comp.get("competitors", []):
                        played.add(team["team"]["displayName"])
            return played
        except:
            return set()

    b2b_teams = get_b2b()
    if b2b_teams:
        print(f"  B2B teams (played yesterday): {', '.join(sorted(b2b_teams))}")

    # Get odds
    url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets=h2h&oddsFormat=american")
    try:
        games = fetch(url)
    except Exception as e:
        print(f"  ! Odds fetch failed: {e}")
        write_picks("nba", [], "EXPERIMENTAL")
        return

    NBA_NAME_MAP = {
        "LA Clippers": "Los Angeles Clippers",
        "GS Warriors": "Golden State Warriors",
        "OKC Thunder": "Oklahoma City Thunder",
        "NY Knicks":   "New York Knicks",
        "NJ Nets":     "Brooklyn Nets",
    }

    all_picks = []
    for g in games:
        ht = NBA_NAME_MAP.get(g["home_team"], g["home_team"])
        at = NBA_NAME_MAP.get(g["away_team"], g["away_team"])
        isr_h = isr.get(ht)
        isr_a = isr.get(at)
        if isr_h is None or isr_a is None:
            continue

        # B2B penalty: -0.03 ISR for team on back-to-back
        isr_h_adj = isr_h * (0.93 if ht in b2b_teams else 1.0)
        isr_a_adj = isr_a * (0.93 if at in b2b_teams else 1.0)

        # Win probability via ISR ratio + home court
        home_adv = 0.035
        raw = isr_h_adj / (isr_h_adj + isr_a_adj)
        p_home = min(0.92, raw + home_adv * raw * (1 - raw) * 2)
        p_away = 1 - p_home

        # Market odds
        home_imps, away_imps = [], []
        for bk in g.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] != "h2h": continue
                oc = {o["name"]: o["price"] for o in mk["outcomes"]}
                if g["home_team"] in oc: home_imps.append(to_imp(oc[g["home_team"]]))
                if g["away_team"] in oc: away_imps.append(to_imp(oc[g["away_team"]]))
        if not home_imps: continue
        raw_h = sum(home_imps)/len(home_imps)
        raw_a = sum(away_imps)/len(away_imps)
        nv_h, nv_a = devig(raw_h, raw_a)

        ep_h = round((p_home - nv_h) * 100, 2)
        ep_a = round((p_away - nv_a) * 100, 2)

        flags = []
        if ht in b2b_teams: flags.append("H-B2B")
        if at in b2b_teams: flags.append("A-B2B")
        flag_str = " ".join(flags) if flags else "—"

        for pick_team, pick_side, model_p, nv_p, ep in [
            (ht, "home", p_home, nv_h, ep_h),
            (at, "away", p_away, nv_a, ep_a),
        ]:
            if ep < 3.0 or model_p < 0.50: continue
            all_picks.append({
                "home_team":        ht,
                "away_team":        at,
                "pick":             pick_team,
                "pick_side":        pick_side,
                "model_prob_home":  round(p_home, 4),
                "model_prob_away":  round(p_away, 4),
                "market_prob_home": round(nv_h, 4),
                "market_prob_away": round(nv_a, 4),
                "edge_pp":          ep,
                "confidence_tier":  tier(model_p),
                "flags":            flag_str,
                "game_time_utc":    g.get("commence_time"),
                "outcome":          None,
                "result":           None,
            })

    print(f"  -> {len(all_picks)} picks | {len(games)} games")
    write_picks("nba", all_picks, "EXPERIMENTAL")

def run_nba_props():
    """NBA props — rolling average projection vs Odds API lines."""
    print("\n[NBA Props — rolling projection model]")
    import math, time, shutil

    def poisson_over(lam, line):
        k = int(math.floor(line))
        cum = sum((lam**i * math.exp(-lam)) / math.factorial(i) for i in range(k+1))
        return 1 - cum

    # Step 1: Get today's NBA game IDs from ESPN scoreboard
    today_str = datetime.date.today().strftime("%Y%m%d")
    try:
        sboard = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today_str}")
        events = sboard.get("events", [])
    except Exception as e:
        print(f"  ! Scoreboard fetch failed: {e}")
        return

    # Use Odds API events directly — ESPN scoreboard only shows live/completed games
    key = ODDS_KEY
    try:
        odds_events = fetch(f"https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey={key}&dateFormat=iso")
    except Exception as e:
        print(f"  ! Odds events fetch failed: {e}")
        return

    if not odds_events:
        print("  -> No NBA games in odds today")
        write_picks("nba_props", [], "ACTIVE")
        return

    print(f"  Games today: {len(odds_events)}")
    # Override events with odds events for prop fetching
    events = odds_events

    # Step 2: For each game, pull last 10 box scores for each player
    # Use ESPN game summary API
    player_logs = {}  # player_name -> list of {pts, reb, ast, fg3}

    # Get recent games (last 14 days) from ESPN scoreboard
    for days_back in range(1, 15):
        date = (datetime.date.today() - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            sb = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}")
            for ev in sb.get("events", []):
                gid = ev["id"]
                try:
                    box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={gid}")
                    for team in box.get("boxscore", {}).get("players", []):
                        for sg in team.get("statistics", []):
                            keys = sg.get("keys", [])
                            for p in sg.get("athletes", []):
                                if p.get("didNotPlay"): continue
                                name = p.get("athlete", {}).get("displayName", "")
                                stats = p.get("stats", [])
                                if not name or not stats: continue
                                d = dict(zip(keys, stats))
                                if name not in player_logs:
                                    player_logs[name] = []
                                if len(player_logs[name]) < 10:
                                    try:
                                        player_logs[name].append({
                                            "pts": float(d.get("points", 0) or 0),
                                            "reb": float(d.get("rebounds", 0) or 0),
                                            "ast": float(d.get("assists", 0) or 0),
                                            "fg3": float(d.get("threePointFieldGoalsMade", 0) or 0),
                                        })
                                    except: pass
                except: pass
        except: pass

    print(f"  Player logs: {len(player_logs)} players tracked")

    # Fetch team defensive ratings (PA/G) from ESPN standings
    def_ratings = {}
    try:
        standings = fetch("https://site.api.espn.com/apis/v2/sports/basketball/nba/standings?season=2026&type=0")
        for conf in standings.get("children", []):
            for e in conf.get("standings", {}).get("entries", []):
                name = e["team"]["displayName"]
                for s in e.get("stats", []):
                    if s["name"] == "avgPointsAgainst":
                        try: def_ratings[name] = float(s.get("value", 113.0) or 113.0)
                        except: pass
        print(f"  Defensive ratings: {len(def_ratings)} teams")
    except:
        print("  ! Defensive ratings unavailable")

    # Step 3: Get prop lines from Odds API
    PROP_MARKETS = [
        ("player_points",   "pts", "PTS"),
        ("player_rebounds", "reb", "REB"),
        ("player_assists",  "ast", "AST"),
        ("player_threes",   "fg3", "3PM"),
    ]

    all_props = []
    for g in events[:6]:
        odds_game_id = g["id"]
        ht = g.get("home_team", "")
        at = g.get("away_team", "")
        gt = g.get("commence_time", "")

        for market_key, stat_key, stat_label in PROP_MARKETS:
            url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/events"
                   f"/{odds_game_id}/odds?apiKey={ODDS_KEY}&regions=us"
                   f"&markets={market_key}&oddsFormat=american")
            try:
                d = fetch(url)
            except: continue

            player_lines = {}
            for bk in d.get("bookmakers", []):
                for mk in bk.get("markets", []):
                    if mk["key"] != market_key: continue
                    for outcome in mk.get("outcomes", []):
                        player = outcome.get("description") or outcome.get("name","")
                        name_lower = outcome.get("name","").lower()
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if not player or point is None: continue
                        if player not in player_lines:
                            player_lines[player] = {"over":[],"under":[],"line":point}
                        if "over" in name_lower:
                            player_lines[player]["over"].append(price)
                        elif "under" in name_lower:
                            player_lines[player]["under"].append(price)

            for player, data in player_lines.items():
                if not data["over"] or not data["under"]: continue
                line = data["line"]

                # Find player in logs
                logs = player_logs.get(player, [])
                # Try partial name match
                if not logs:
                    for logged_name, logged_data in player_logs.items():
                        last = player.split()[-1].lower()
                        if last in logged_name.lower() and len(last) > 3:
                            logs = logged_data
                            break

                if len(logs) < 5:
                    continue  # not enough data

                import math as _math
                n = min(10, len(logs))
                recent = logs[-n:]
                # Exponential recency weights
                ew = [_math.exp(i/n) for i in range(1, n+1)]
                tot_w = sum(ew)
                vals = [g[stat_key] for g in recent]
                rolling_avg = sum(v*w for v,w in zip(vals,ew)) / tot_w
                # Recency gate — role change detection
                if len(logs) >= 5:
                    last5 = [g[stat_key] for g in logs[-5:]]
                    last5_avg = sum(last5) / 5
                    if rolling_avg > 0 and abs(last5_avg - rolling_avg) / rolling_avg > 0.30:
                        rolling_avg = 0.80 * last5_avg + 0.20 * rolling_avg
                # Season average anchor (65/35 blend)
                season_avg = sum(g[stat_key] for g in logs) / len(logs)
                lam = 0.65 * rolling_avg + 0.35 * season_avg
                # Opponent defensive rating adjustment
                # Use average of both teams' defense (player team unknown)
                def_h = def_ratings.get(ht, 113.0)
                def_a = def_ratings.get(at, 113.0)
                opp_def = (def_h + def_a) / 2
                league_avg_def = 113.0
                def_scale = 0.15 if stat_key == "pts" else 0.05
                lam = lam * (1 + (opp_def - league_avg_def) / league_avg_def * def_scale)
                lam = max(0.1, lam)
                # Skip if no signal, role-change artifact, or degenerate prob
                if abs(lam - line) < 0.5 or abs(lam - line) > 6.0:
                    continue
                # Skip near-certain outcomes — not actionable
                model_over = poisson_over(lam, line)
                model_under = 1 - model_over
                if max(model_over, model_under) > 0.85:
                    continue

                # Devig
                avg_over  = sum(to_imp(p) for p in data["over"])  / len(data["over"])
                avg_under = sum(to_imp(p) for p in data["under"]) / len(data["under"])
                tot = avg_over + avg_under
                nv_over  = avg_over  / tot
                nv_under = avg_under / tot

                model_over  = poisson_over(lam, line)
                model_under = 1 - model_over
                ep_over  = round((model_over  - nv_over)  * 100, 2)
                ep_under = round((model_under - nv_under) * 100, 2)

                for direction, model_p, nv_p, ep in [
                    ("over",  model_over,  nv_over,  ep_over),
                    ("under", model_under, nv_under, ep_under),
                ]:
                    if ep < 4.0: continue
                    all_props.append({
                        "player":          player,
                        "stat":            stat_label,
                        "line":            line,
                        "projection":      round(lam, 1),
                        "direction":       direction,
                        "model_prob":      round(model_p, 4),
                        "market_prob":     round(nv_p, 4),
                        "edge_pp":         ep,
                        "games_used":      len(vals),
                        "confidence_tier": tier(model_p),
                        "matchup":         f"{at} @ {ht}",
                        "game_time_utc":   gt,
                        "outcome":         None,
                        "result":          None,
                    })

    # Deduplicate
    seen = set()
    deduped = []
    for p in sorted(all_props, key=lambda x: -x["edge_pp"]):
        key = (p["player"], p["stat"])
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    print(f"  -> {len(deduped)} props with ≥4.0pp edge")

    out = {
        "schema_version": "1.0",
        "sport":          "nba_props",
        "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "data_date":      TODAY,
        "status":         "ACTIVE",
        "picks":          deduped,
    }
    path = os.path.join(DATA_DIR, "nba_props_today.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    site_path = os.path.join(SITE_DIR, "nba_props_today.json")
    shutil.copy2(path, site_path)

def run_nba_props_old():
    """NBA props — Poisson edge model using Odds API player props."""
    print("\n[NBA Props — Poisson edge model]")
    import math

    def poisson_over(lam, line):
        """P(X > line) using Poisson CDF."""
        k = int(math.floor(line))
        cum = 0.0
        for i in range(k + 1):
            cum += (lam ** i * math.exp(-lam)) / math.factorial(i)
        # Handle half-lines: P(X > k.5) = 1 - P(X <= k)
        if line != int(line):
            return 1 - cum
        # Whole line: P(X > k) = 1 - P(X <= k)
        return 1 - cum

    # Get today's NBA games
    odds_url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
                f"?apiKey={ODDS_KEY}&regions=us&markets=h2h&oddsFormat=american")
    try:
        games = fetch(odds_url)
    except Exception as e:
        print(f"  ! Games fetch failed: {e}")
        return

    if not games:
        print("  -> No NBA games today")
        write_picks("nba_props", [], "ACTIVE")
        return

    # Fetch player props for each market
    PROP_MARKETS = [
        ("player_points",        "PTS"),
        ("player_rebounds",      "REB"),
        ("player_assists",       "AST"),
        ("player_threes",        "3PM"),
    ]

    all_props = []
    for g in games[:6]:  # limit to save API calls
        game_id = g["id"]
        ht = g["home_team"]; at = g["away_team"]
        gt = g.get("commence_time", "")

        for market_key, stat_label in PROP_MARKETS:
            url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/events"
                   f"/{game_id}/odds?apiKey={ODDS_KEY}&regions=us"
                   f"&markets={market_key}&oddsFormat=american")
            try:
                d = fetch(url)
            except:
                continue

            # Collect lines per player
            player_lines = {}
            for bk in d.get("bookmakers", []):
                for mk in bk.get("markets", []):
                    if mk["key"] != market_key: continue
                    for outcome in mk.get("outcomes", []):
                        player = outcome.get("description") or outcome.get("name")
                        name   = outcome.get("name","").lower()
                        point  = outcome.get("point")
                        price  = outcome.get("price")
                        if not player or point is None: continue
                        if player not in player_lines:
                            player_lines[player] = {"over":[],"under":[],"line":point}
                        if "over" in name:
                            player_lines[player]["over"].append(price)
                        elif "under" in name:
                            player_lines[player]["under"].append(price)

            for player, data in player_lines.items():
                if not data["over"] or not data["under"]: continue
                line = data["line"]

                # Devig over/under
                avg_over  = sum(to_imp(p) for p in data["over"])  / len(data["over"])
                avg_under = sum(to_imp(p) for p in data["under"]) / len(data["under"])
                tot = avg_over + avg_under
                nv_over  = avg_over  / tot
                nv_under = avg_under / tot

                # Model: use Poisson with lambda = line + 0.5 as projection baseline
                # This is conservative — treats market line as a fair estimate
                # then looks for mispricing via Poisson distribution shape
                lam = line + 0.3  # slight over-projection (historical NBA scoring trends)
                model_over  = poisson_over(lam, line)
                model_under = 1 - model_over

                ep_over  = round((model_over  - nv_over)  * 100, 2)
                ep_under = round((model_under - nv_under) * 100, 2)

                # Only take edges >= 4pp (props market is efficient)
                for direction, model_p, nv_p, ep in [
                    ("over",  model_over,  nv_over,  ep_over),
                    ("under", model_under, nv_under, ep_under),
                ]:
                    if ep < 4.0: continue
                    all_props.append({
                        "player":           player,
                        "stat":             stat_label,
                        "line":             line,
                        "direction":        direction,
                        "model_prob":       round(model_p, 4),
                        "market_prob":      round(nv_p, 4),
                        "edge_pp":          ep,
                        "confidence_tier":  tier(model_p),
                        "matchup":          f"{at} @ {ht}",
                        "game_time_utc":    gt,
                        "outcome":          None,
                        "result":           None,
                    })

    # Sort by edge, deduplicate players
    seen = set()
    deduped = []
    for p in sorted(all_props, key=lambda x: -x["edge_pp"]):
        key = (p["player"], p["stat"])
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    print(f"  -> {len(deduped)} props with ≥4.0pp edge")

    out = {
        "schema_version": "1.0",
        "sport":          "nba_props",
        "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "data_date":      TODAY,
        "status":         "ACTIVE",
        "picks":          deduped,
    }
    path = os.path.join(DATA_DIR, "nba_props_today.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    site_path = os.path.join(SITE_DIR, "nba_props_today.json")
    import shutil
    shutil.copy2(path, site_path)


def run_mma():
    """MMA/UFC — win-rate ISR model vs market odds."""
    import re, time
    print("\n[MMA — Win-rate ISR + market edge model]")

    # Step 1: Build fighter win rates from UFCStats
    fighter_stats = {}
    for char in 'abcdefghijklmnopqrstuvwxyz':
        try:
            url = f'http://ufcstats.com/statistics/fighters?char={char}&page=all'
            req = __import__('urllib.request', fromlist=['Request','urlopen']).Request(
                url, headers={"User-Agent":"iqproject/1.0"})
            html = __import__('urllib.request', fromlist=['urlopen']).urlopen(req, timeout=10).read().decode('utf-8','replace')
            rows = re.findall(r'<tr class="b-statistics__table-row">(.*?)</tr>', html, re.DOTALL)
            for row in rows:
                cols = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(cols) < 10: continue
                first = re.sub(r'<[^>]+>','',cols[0]).strip()
                last  = re.sub(r'<[^>]+>','',cols[1]).strip()
                w     = re.sub(r'<[^>]+>','',cols[7]).strip()
                l     = re.sub(r'<[^>]+>','',cols[8]).strip()
                d     = re.sub(r'<[^>]+>','',cols[9]).strip()
                if not first or not last: continue
                try:
                    wins,losses,draws = int(w),int(l),int(d)
                    total = wins+losses+draws
                    if total >= 3:
                        fighter_stats[f"{first} {last}"] = {
                            'wins':wins,'losses':losses,'total':total,
                            'win_rate': wins/total
                        }
                except: continue
            time.sleep(0.03)
        except: continue

    print(f"  Fighters rated: {len(fighter_stats)}")

    def find_fighter(name):
        """Fuzzy match fighter name to UFCStats record."""
        if name in fighter_stats: return fighter_stats[name]
        name_parts = name.lower().split()
        best, best_score = None, 0
        for k, v in fighter_stats.items():
            k_parts = k.lower().split()
            score = sum(1 for p in name_parts if any(p in kp for kp in k_parts))
            if score > best_score and score >= 1:
                best_score = score
                best = v
        return best

    def win_prob(wr_a, wr_b):
        """Log5 win probability from win rates."""
        if wr_a + wr_b == 0: return 0.5
        p = (wr_a * (1 - wr_b)) / (wr_a * (1 - wr_b) + wr_b * (1 - wr_a))
        return max(0.05, min(0.95, p))

    # Step 2: Get MMA odds
    if not ODDS_KEY:
        write_picks("mma", [], "EXPERIMENTAL")
        return

    url = (f"https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets=h2h&oddsFormat=american")
    try:
        games = fetch(url)
    except Exception as e:
        print(f"  ! Odds fetch failed: {e}")
        write_picks("mma", [], "EXPERIMENTAL")
        return

    # Filter to next event only (next 8 days)
    import datetime as _dt
    cutoff = (_dt.date.today() + _dt.timedelta(days=8)).isoformat()
    games = [g for g in games if g.get("commence_time","") <= cutoff + "T99:99:99Z"]
    print(f"  Fights in next 8 days: {len(games)}")

    all_picks = []
    matched = 0
    for g in games:
        f1 = g["home_team"]  # UFC uses home/away for fighter A/B
        f2 = g["away_team"]

        s1 = find_fighter(f1)
        s2 = find_fighter(f2)
        if not s1 or not s2: continue
        matched += 1

        wr1 = s1["win_rate"]
        wr2 = s2["win_rate"]

        # Regress to mean (K=10 fights)
        K = 10
        wr1_r = (s1["total"] * wr1 + K * 0.5) / (s1["total"] + K)
        wr2_r = (s2["total"] * wr2 + K * 0.5) / (s2["total"] + K)

        model_f1 = win_prob(wr1_r, wr2_r)
        model_f2 = 1 - model_f1

        # Market odds
        home_imps, away_imps = [], []
        for bk in g.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] != "h2h": continue
                oc = {o["name"]: o["price"] for o in mk["outcomes"]}
                if f1 in oc: home_imps.append(to_imp(oc[f1]))
                if f2 in oc: away_imps.append(to_imp(oc[f2]))

        if not home_imps: continue
        raw_h = sum(home_imps)/len(home_imps)
        raw_a = sum(away_imps)/len(away_imps)
        nv_f1, nv_f2 = devig(raw_h, raw_a)

        ep_f1 = round((model_f1 - nv_f1) * 100, 2)
        ep_f2 = round((model_f2 - nv_f2) * 100, 2)

        for fighter, model_p, nv_p, ep, pick_side in [
            (f1, model_f1, nv_f1, ep_f1, "home"),
            (f2, model_f2, nv_f2, ep_f2, "away"),
        ]:
            if ep < 4.0 or ep > 15.0 or model_p < 0.45: continue
            all_picks.append({
                "home_team":        f1,
                "away_team":        f2,
                "pick":             fighter,
                "pick_side":        pick_side,
                "model_prob_home":  round(model_f1, 4),
                "model_prob_away":  round(model_f2, 4),
                "market_prob_home": round(nv_f1, 4),
                "market_prob_away": round(nv_f2, 4),
                "edge_pp":          ep,
                "confidence_tier":  tier(model_p),
                "record":           f"{s1['wins']}-{s1['losses']}" if pick_side=="home" else f"{s2['wins']}-{s2['losses']}",
                "game_time_utc":    g.get("commence_time"),
                "outcome":          None,
                "result":           None,
            })

    print(f"  Matched: {matched}/{len(games)} fights | Picks: {len(all_picks)}")
    write_picks("mma", all_picks, "EXPERIMENTAL")


def _series_prob(isr_a, isr_b, a_home, home_adv=0.035):
    def wp(is_a_home):
        if is_a_home:
            raw = isr_a/(isr_a+isr_b)
            p = raw + home_adv*raw*(1-raw)*2
            return max(0.05, min(0.95, p))
        else:
            raw = isr_b/(isr_a+isr_b)
            p = raw + home_adv*raw*(1-raw)*2
            return 1 - max(0.05, min(0.95, p))
    locs = [True,True,False,False,True,False,True] if a_home else [False,False,True,True,False,True,False]
    probs = [wp(loc) for loc in locs]
    dp = [[0.0]*5 for _ in range(5)]
    dp[0][0] = 1.0
    for wa in range(4):
        for wb in range(4):
            if dp[wa][wb] < 1e-12: continue
            g = wa+wb
            if g >= 7: continue
            p = probs[g]
            dp[wa+1][wb] += dp[wa][wb]*p
            dp[wa][wb+1] += dp[wa][wb]*(1-p)
    return sum(dp[4][wb] for wb in range(4))

def _sim_series(a, b, home_adv):
    a_home = a["seed"] < b["seed"]
    p = _series_prob(a["isr"], b["isr"], a_home, home_adv)
    return a if __import__('random').random() < p else b

def _sim_bracket(teams, home_adv):
    import random as _r
    bracket = sorted(teams[:], key=lambda x: x["seed"])
    while len(bracket) > 1:
        next_r = []
        n = len(bracket)
        for i in range(n//2):
            next_r.append(_sim_series(bracket[i], bracket[n-1-i], home_adv))
        bracket = sorted(next_r, key=lambda x: x["seed"])
    return bracket[0]

def _futures_market(odds_key):
    if not ODDS_KEY: return {}, {}, 0
    url = (f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
           f"?apiKey={ODDS_KEY}&regions=us&markets=outrights&oddsFormat=american")
    try:
        data = fetch(url)
    except:
        return {}, {}, 0
    player_odds = {}
    for bk in data[0].get("bookmakers",[]):
        for mk in bk.get("markets",[]):
            for o in mk.get("outcomes",[]):
                nm = o["name"]
                if nm not in player_odds: player_odds[nm]=[]
                player_odds[nm].append(o["price"])
    raw = {n: sum(to_imp(p) for p in prices)/len(prices) for n,prices in player_odds.items()}
    tot = sum(raw.values())
    nv = {n: raw[n]/tot for n in raw}
    return player_odds, nv, round((tot-1)*100,1)

def _build_futures_results(teams, model_probs, player_odds, nv_prob):
    results = []
    for t in teams:
        name = t["name"]
        mkt_p = nv_prob.get(name)
        model_p = model_probs.get(name, 0)
        if not mkt_p or model_p == 0: continue
        edge_pp = round((model_p - mkt_p)*100, 1)
        best_ml = max(player_odds.get(name, [0]))
        best_str = f"+{best_ml}" if best_ml > 0 else str(best_ml)
        b = best_ml/100 if best_ml > 0 else 100/abs(best_ml)
        kelly = max(0, (model_p*b-(1-model_p))/b * 0.25)
        results.append({
            "name": name, "seed": t.get("seed",0),
            "isr": round(t["isr"],4),
            "model_prob": round(model_p,4),
            "market_prob": round(mkt_p,4),
            "edge_pp": edge_pp,
            "best_ml": best_ml, "best_str": best_str,
            "kelly_pct": round(kelly*100,2),
            "value": edge_pp >= 3.0,
        })
    return sorted(results, key=lambda x: -x["edge_pp"])

def run_futures():
    import random, shutil
    print("\n[Futures — Monte Carlo bracket simulations]")
    random.seed(42)

    def build_isr(ppg, papg, gp, K=10):
        if ppg+papg == 0: return 0.5
        exp = (ppg+papg)**0.285
        pyth = ppg**exp/(ppg**exp+papg**exp)
        return (gp*pyth + K*0.5)/(gp+K)
    METHOD = "Pythagenpat ISR → best-of-7 series DP → 50k Monte Carlo bracket simulation. Edge = model_prob − devigged market prob. Kelly = quarter-Kelly (25%)."

    # ── NBA ──────────────────────────────────────────────────────
    EAST = [
        {"name":"Detroit Pistons","seed":1,"ppg":117.8,"papg":109.6,"gp":82},
        {"name":"Boston Celtics","seed":2,"ppg":114.9,"papg":107.2,"gp":82},
        {"name":"New York Knicks","seed":3,"ppg":116.5,"papg":110.1,"gp":82},
        {"name":"Cleveland Cavaliers","seed":4,"ppg":119.5,"papg":115.4,"gp":82},
        {"name":"Toronto Raptors","seed":5,"ppg":114.6,"papg":111.8,"gp":82},
        {"name":"Atlanta Hawks","seed":6,"ppg":118.5,"papg":116.0,"gp":82},
        {"name":"Philadelphia 76ers","seed":7,"ppg":115.9,"papg":116.1,"gp":82},
        {"name":"Orlando Magic","seed":8,"ppg":115.7,"papg":115.1,"gp":82},
    ]
    WEST = [
        {"name":"Oklahoma City Thunder","seed":1,"ppg":119.0,"papg":107.9,"gp":82},
        {"name":"San Antonio Spurs","seed":2,"ppg":119.8,"papg":111.5,"gp":82},
        {"name":"Denver Nuggets","seed":3,"ppg":122.1,"papg":116.9,"gp":82},
        {"name":"Los Angeles Lakers","seed":4,"ppg":116.3,"papg":114.6,"gp":82},
        {"name":"Houston Rockets","seed":5,"ppg":115.2,"papg":110.0,"gp":82},
        {"name":"Minnesota Timberwolves","seed":6,"ppg":118.0,"papg":114.6,"gp":82},
        {"name":"Phoenix Suns","seed":7,"ppg":112.6,"papg":111.1,"gp":82},
        {"name":"Portland Trail Blazers","seed":8,"ppg":115.5,"papg":115.8,"gp":82},
    ]
    for t in EAST+WEST:
        t["isr"] = build_isr(t["ppg"],t["papg"],t["gp"])
        t["conf"] = "E" if t in EAST else "W"

    def nba_sim(teams):
        east = sorted([t for t in teams if t["conf"]=="E"], key=lambda x:x["seed"])
        west = sorted([t for t in teams if t["conf"]=="W"], key=lambda x:x["seed"])
        ec = _sim_bracket(east, 0.035)
        wc = _sim_bracket(west, 0.035)
        p = _series_prob(ec["isr"], wc["isr"], True, 0.035)
        return ec if random.random() < p else wc

    wins = {t["name"]:0 for t in EAST+WEST}
    for _ in range(50000):
        wins[nba_sim(EAST+WEST)["name"]] += 1
    nba_model = {n:c/50000 for n,c in wins.items()}
    nba_odds, nba_nv, nba_over = _futures_market("basketball_nba_championship_winner")
    nba_results = _build_futures_results(EAST+WEST, nba_model, nba_odds, nba_nv)
    nba_value = [r for r in nba_results if r["value"]]
    print(f"  NBA: {len(nba_value)} value picks | overround:{nba_over}%")

    # ── NHL ──────────────────────────────────────────────────────
    try:
        nhl_stand = fetch("https://site.api.espn.com/apis/v2/sports/hockey/nhl/standings?season=2026&type=0")
        nhl_teams = []
        for conf in nhl_stand.get("children",[]):
            entries = conf.get("standings",{}).get("entries",[])
            entries_s = sorted(entries, key=lambda e: next(
                (int(float(s.get("value",99))) for s in e.get("stats",[]) if s["name"]=="playoffSeed"),99))
            for e in entries_s[:8]:
                name = e["team"]["displayName"]
                stats = {s["name"]:float(s.get("value",0) or 0) for s in e.get("stats",[])}
                gp = stats.get("gamesPlayed",82)
                gf = stats.get("pointsFor",0)
                ga = stats.get("pointsAgainst",0)
                ps = int(stats.get("playoffSeed",1))
                if gp == 0: continue
                nhl_teams.append({"name":name,"seed":ps,"isr":build_isr(gf/gp,ga/gp,gp),
                                   "gp":int(gp),"wins":int(stats.get("wins",0))})
        wins = {t["name"]:0 for t in nhl_teams}
        for _ in range(50000):
            wins[_sim_bracket(nhl_teams, 0.025)["name"]] += 1
        nhl_model = {n:c/50000 for n,c in wins.items()}
        nhl_odds, nhl_nv, nhl_over = _futures_market("icehockey_nhl_championship_winner")
        nhl_results = _build_futures_results(nhl_teams, nhl_model, nhl_odds, nhl_nv)
        nhl_value = [r for r in nhl_results if r["value"]]
        print(f"  NHL: {len(nhl_value)} value picks | overround:{nhl_over}%")
    except Exception as e:
        print(f"  NHL futures failed: {e}")
        nhl_results, nhl_over = [], 0

    # ── MLB ──────────────────────────────────────────────────────
    try:
        mlb_stand = fetch("https://site.api.espn.com/apis/v2/sports/baseball/mlb/standings?season=2026&type=0")
        mlb_teams = []
        for conf in mlb_stand.get("children",[]):
            entries = conf.get("standings",{}).get("entries",[])
            entries_s = sorted(entries, key=lambda e: next(
                (int(float(s.get("value",99))) for s in e.get("stats",[]) if s["name"]=="playoffSeed"),99))
            for e in entries_s[:5]:
                name = e["team"]["displayName"]
                stats = {s["name"]:float(s.get("value",0) or 0) for s in e.get("stats",[])}
                gp = stats.get("gamesPlayed",0)
                rf = stats.get("pointsFor",0)
                ra = stats.get("pointsAgainst",0)
                ps = int(stats.get("playoffSeed",1))
                if gp == 0: continue
                mlb_teams.append({"name":name,"seed":ps,"isr":build_isr(rf/gp,ra/gp,gp,K=20),
                                   "gp":int(gp),"wins":int(stats.get("wins",0))})
        wins = {t["name"]:0 for t in mlb_teams}
        for _ in range(50000):
            wins[_sim_bracket(mlb_teams, 0.03)["name"]] += 1
        mlb_model = {n:c/50000 for n,c in wins.items()}
        mlb_odds, mlb_nv, mlb_over = _futures_market("baseball_mlb_world_series_winner")
        mlb_results = _build_futures_results(mlb_teams, mlb_model, mlb_odds, mlb_nv)
        mlb_value = [r for r in mlb_results if r["value"]]
        print(f"  MLB: {len(mlb_value)} value picks | overround:{mlb_over}% (early season — high variance)")
    except Exception as e:
        print(f"  MLB futures failed: {e}")
        mlb_results, mlb_over = [], 0

    # ── SAVE ─────────────────────────────────────────────────────
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    futures_out = {
        "generated_at": now,
        "data_date": TODAY,
        "markets": [
            {"key":"nba","label":"NBA Championship 2026","simulations":50000,
             "overround":nba_over,"methodology":METHOD,"picks":nba_results},
            {"key":"nhl","label":"NHL Stanley Cup 2026","simulations":50000,
             "overround":nhl_over,"methodology":METHOD,"picks":nhl_results},
            {"key":"mlb","label":"MLB World Series 2026","simulations":50000,
             "overround":mlb_over,"methodology":METHOD,
             "note":"Early season — high variance, picks regressed to mean (K=20).",
             "picks":mlb_results},
        ]
    }
    path = os.path.join(DATA_DIR, "futures.json")
    with open(path,"w") as f: json.dump(futures_out,f,indent=2)
    shutil.copy2(path, os.path.join(SITE_DIR,"futures.json"))
    print(f"  Saved futures.json")

SPORT_RUNNERS = {
    "mlb":          run_mlb,
    "nhl":          run_nhl,
    "ncaa_baseball": run_ncaa_baseball,
    "mma":          run_mma,
    "nba":          run_nba,
    "soccer":       run_soccer,
    "nfl":          run_nfl,
    "mlb_props":   run_mlb_props,
    "nba_props":   run_nba_props,
    "golf_masters": run_golf_masters,
    "futures":       run_futures,
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

# ═══════════════════════════════════════════════════════════════════════════════
# FUTURES ENGINE — Monte Carlo bracket simulation + devigged market comparison
# ═══════════════════════════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════════════════════════
# FUTURES ENGINE — Monte Carlo bracket simulation + devigged market comparison
# ═══════════════════════════════════════════════════════════════════════════════
