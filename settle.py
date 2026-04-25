"""
settle.py — Daily performance settlement
Run each morning: python3 settle.py
Covers: MLB, NBA, NHL, NCAA Baseball, Soccer game picks + MLB/NBA props
High and medium confidence only
"""
import json, os, csv, datetime, urllib.request

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent":"iqproject/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
yesterday_compact = yesterday.replace("-","")
# NBA props use UTC so games at 7pm ET = next UTC date
# Accept yesterday OR day before yesterday for props
prev2 = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()

print(f"\n{'='*60}")
print(f"  iQ Performance Settlement — {yesterday}")
print(f"{'='*60}\n")

CSV_FILE = "/workspaces/iqproject/performance_record.csv"
fieldnames = ["date","sport","game","pick","pick_side","model_prob",
              "market_prob","edge_pp","confidence_tier","outcome",
              "actual_value","line","verified"]

existing = set()
if os.path.exists(CSV_FILE):
    with open(CSV_FILE) as f:
        for row in csv.DictReader(f):
            existing.add(row.get("date","") + "|" + row.get("sport","") + "|" + row.get("pick",""))

new_rows = []

def espn_scores(path, date):
    try:
        d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={date}")
        out = {}
        for ev in d.get("events",[]):
            comp = ev.get("competitions",[{}])[0]
            teams = {c["homeAway"]: c for c in comp.get("competitors",[])}
            hn = teams.get("home",{}).get("team",{}).get("displayName","")
            h = int(teams.get("home",{}).get("score",0) or 0)
            a = int(teams.get("away",{}).get("score",0) or 0)
            if h or a:
                out[hn.lower()] = {"h":h,"a":a}
        return out
    except Exception as e:
        print(f"    ESPN error: {e}")
        return {}

def soccer_scores(date):
    out = {}
    for league in ["eng.1","esp.1","ger.1","ita.1","fra.1","uefa.champions","uefa.europa"]:
        try:
            d = fetch(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={date}")
            for ev in d.get("events",[]):
                comp = ev.get("competitions",[{}])[0]
                teams = {c["homeAway"]: c for c in comp.get("competitors",[])}
                hn = teams.get("home",{}).get("team",{}).get("displayName","")
                h = int(teams.get("home",{}).get("score",0) or 0)
                a = int(teams.get("away",{}).get("score",0) or 0)
                if h or a: out[hn.lower()] = {"h":h,"a":a}
        except: pass
    return out

def fuzzy(name, scores):
    nl = name.lower()
    if nl in scores: return scores[nl]
    for k,v in scores.items():
        if nl in k or k in nl: return v
    return None

def ml_outcome(score, pick_side):
    if not score: return None
    h,a = score["h"],score["a"]
    if pick_side=="home": return "WIN" if h>a else ("LOSS" if a>h else "PUSH")
    else: return "WIN" if a>h else ("LOSS" if h>a else "PUSH")

def add_row(sport, game, pick, pick_side, model_p, market_p, edge, tier, outcome, actual, line, verified):
    uid = f"{yesterday}|{sport}|{pick}"
    if uid in existing: return False
    flag = "★" if tier=="high" else "·"
    status = f"{outcome} ({actual})" if outcome and outcome!="PENDING" else "PENDING"
    print(f"    {flag} {pick:35} | {tier:6} | {status}")
    new_rows.append({
        "date":yesterday,"sport":sport,"game":game,"pick":pick,
        "pick_side":pick_side,"model_prob":round(model_p or 0,4),
        "market_prob":round(market_p,4) if market_p else "",
        "edge_pp":edge,"confidence_tier":tier,"outcome":outcome or "PENDING",
        "actual_value":actual or "","line":line or "","verified":verified
    })
    return True

# ── GAME PICKS ─────────────────────────────────────────────────
SPORT_PATHS = {
    "mlb":          "baseball/mlb",
    "nba":          "basketball/nba",
    "nhl":          "hockey/nhl",
    "ncaa_baseball":"baseball/college-baseball",
    "soccer":       None,
}

for sport, path in SPORT_PATHS.items():
    archive = f"/workspaces/iqproject/{sport}_picks_{yesterday}.json"
    if not os.path.exists(archive):
        print(f"  {sport.upper()}: no archive for {yesterday}")
        continue

    with open(archive) as f:
        d = json.load(f)

    all_picks = d.get("picks",[])
    # Filter: game on yesterday AND high/medium confidence
    picks = [p for p in all_picks
             if (p.get("game_time_utc","") or "")[:10] == yesterday
             and p.get("confidence_tier") in ("high","medium")]

    other_dates = [p for p in all_picks if (p.get("game_time_utc","") or "")[:10] != yesterday]
    yest_total = [p for p in all_picks if (p.get("game_time_utc","") or "")[:10] == yesterday]

    if not picks:
        print(f"  {sport.upper()}: no high/medium picks on {yesterday} "
              f"({len(yest_total)} total picks that day, {len(other_dates)} on other dates)")
        continue

    # Get scores
    if sport == "soccer":
        scores = soccer_scores(yesterday_compact)
    else:
        scores = espn_scores(path, yesterday_compact)

    print(f"\n  {sport.upper()} — {len(picks)} high/medium picks on {yesterday}:")
    for p in picks:
        tier = p.get("confidence_tier","")
        pick_team = p.get("pick","")
        pick_side = p.get("pick_side","home")
        game = f"{p.get('away_team','')} @ {p.get('home_team','')}"
        model_p = p.get("model_prob_home") if pick_side=="home" else p.get("model_prob_away")
        market_p = p.get("market_prob_home") if pick_side=="home" else p.get("market_prob_away")

        score = fuzzy(p.get("home_team",""), scores)
        outcome = ml_outcome(score, pick_side)
        actual = f"{score['a']}-{score['h']}" if score else None

        add_row(sport, game, pick_team, pick_side, model_p, market_p,
                p.get("edge_pp",""), tier, outcome, actual, None,
                "ESPN" if score else "MANUAL")

# ── MLB PROPS ──────────────────────────────────────────────────
mlb_props_archive = f"/workspaces/iqproject/mlb_props_{yesterday}.json"
if os.path.exists(mlb_props_archive):
    with open(mlb_props_archive) as f:
        d = json.load(f)
    props = [p for p in d.get("props",[])
             if p.get("confidence_tier") in ("high","medium")]
    if props:
        print(f"\n  MLB PROPS — {len(props)} high/medium props:")
        # Get pitcher Ks
        pitcher_ks = {}
        try:
            sched = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={yesterday}&hydrate=boxscore")
            for de in sched.get("dates",[]):
                for game in de.get("games",[]):
                    gid = game.get("gamePk")
                    try:
                        box = fetch(f"https://statsapi.mlb.com/api/v1/game/{gid}/boxscore")
                        for side in ["home","away"]:
                            players = box.get("teams",{}).get(side,{}).get("players",{})
                            pitchers = box.get("teams",{}).get(side,{}).get("pitchers",[])
                            for pid in pitchers:
                                pd = players.get(f"ID{pid}",{})
                                name = pd.get("person",{}).get("fullName","")
                                ks = pd.get("stats",{}).get("pitching",{}).get("strikeOuts",0)
                                if name: pitcher_ks[name.lower()] = int(ks or 0)
                    except: pass
        except Exception as e:
            print(f"    MLB API error: {e}")

        for p in props:
            player = p.get("player","")
            line = p.get("line",0)
            direction = (p.get("pick_side","Over")).lower()
            tier = p.get("confidence_tier","")
            actual_k = pitcher_ks.get(player.lower())
            if actual_k is not None:
                outcome = "WIN" if (direction=="over" and actual_k>line) or (direction=="under" and actual_k<line) else "LOSS"
                actual = f"{actual_k}K"
            else:
                outcome = "PENDING"; actual = None
            add_row("mlb_props", p.get("game",""),
                    f"{player} {direction.upper()} {line}K",
                    direction, p.get("model_prob",0), p.get("market_prob",0),
                    p.get("edge_pp",""), tier, outcome, actual, line,
                    "MLB API" if actual_k is not None else "MANUAL")

# ── NBA PROPS ──────────────────────────────────────────────────
# NBA props are archived as nba_props_{TODAY}.json
# Game times are in UTC so yesterday ET = could be yesterday or prev2 UTC
for try_date in [yesterday, prev2]:
    nba_props_archive = f"/workspaces/iqproject/nba_props_{try_date}.json"
    if not os.path.exists(nba_props_archive): continue
    with open(nba_props_archive) as f:
        d = json.load(f)
    # Filter to picks where game was yesterday ET (UTC date = yesterday or yesterday+1)
    props = [p for p in d.get("picks",[])
             if p.get("confidence_tier") in ("high","medium")
             and (p.get("game_time_utc","") or "")[:10] in (yesterday, yesterday.replace(yesterday[-2:], str(int(yesterday[-2:])+1).zfill(2)))]
    if not props: continue
    print(f"\n  NBA PROPS (from {try_date} archive) — {len(props)} high/medium props:")

    # Get player stats from ESPN box scores
    player_stats = {}
    try:
        sc = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={yesterday_compact}")
        for ev in sc.get("events",[]):
            gid = ev.get("id")
            try:
                box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={gid}")
                for team in box.get("boxscore",{}).get("players",[]):
                    for sg in team.get("statistics",[]):
                        keys = sg.get("keys",[])
                        for ath in sg.get("athletes",[]):
                            name = ath.get("athlete",{}).get("displayName","").lower()
                            player_stats[name] = dict(zip(keys, ath.get("stats",[])))
            except: pass
    except Exception as e:
        print(f"    ESPN error: {e}")

    stat_map = {"PTS":"points","REB":"rebounds","AST":"assists",
                "STL":"steals","BLK":"blocks","TOV":"turnovers"}
    for p in props:
        player = p.get("player","")
        stat = p.get("stat","")
        line = p.get("line",0)
        direction = p.get("direction","over").lower()
        tier = p.get("confidence_tier","")
        pdata = player_stats.get(player.lower(),{})
        espn_key = stat_map.get(stat)
        actual = None
        if espn_key:
            try: actual = float(pdata.get(espn_key,0) or 0)
            except: pass
        if actual is not None and actual > 0:
            outcome = "WIN" if (direction=="over" and actual>line) or (direction=="under" and actual<line) else "LOSS"
            actual_str = str(actual)
        else:
            outcome = "PENDING"; actual_str = None
        add_row("nba_props", p.get("matchup",""),
                f"{player} {stat} {direction.upper()} {line}",
                direction, p.get("model_prob",0), p.get("market_prob",0),
                p.get("edge_pp",""), tier, outcome, actual_str, line,
                "ESPN" if actual_str else "MANUAL")
    break

# ── WRITE CSV ──────────────────────────────────────────────────
if new_rows:
    write_header = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header: writer.writeheader()
        writer.writerows(new_rows)
    print(f"\n✓ {len(new_rows)} rows written to performance_record.csv")
else:
    print("\n~ No new rows (already recorded or no picks)")

# ── SUMMARY ────────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  SUMMARY — {yesterday}")
print(f"{'='*60}")
settled = [r for r in new_rows if r["outcome"] in ("WIN","LOSS")]
for sport in ["mlb","nba","nhl","ncaa_baseball","soccer","mlb_props","nba_props"]:
    sub = [r for r in settled if r["sport"]==sport]
    if not sub: continue
    for tier in ["high","medium"]:
        t = [r for r in sub if r["confidence_tier"]==tier]
        if not t: continue
        w = sum(1 for r in t if r["outcome"]=="WIN")
        print(f"  {sport.upper():15} {tier.upper():6}: {w}W-{len(t)-w}L ({w/len(t)*100:.1f}%)")
pending = [r for r in new_rows if r["outcome"]=="PENDING"]
if pending:
    print(f"\n  PENDING (manual verify needed): {len(pending)}")
    for r in pending: print(f"    {r['sport']:12} {r['pick']}")
print(f"\nCSV: {CSV_FILE}")
print(f"Daily command: python3 /workspaces/iqproject/settle.py\n")

# ── WRITE PERF.JSON FOR WEBSITE ────────────────────────────────
import json as _json

# Load full CSV for running totals
all_rows = []
if os.path.exists(CSV_FILE):
    with open(CSV_FILE) as f:
        all_rows = list(csv.DictReader(f))

all_settled = [r for r in all_rows if r['outcome'] in ('WIN','LOSS')]
yest_settled = [r for r in all_settled if r['date'] == yesterday]

def calc_stats(rows):
    if not rows: return {"w":0,"l":0,"pct":None,"n":0}
    w = sum(1 for r in rows if r['outcome']=='WIN')
    l = len(rows)-w
    return {"w":w,"l":l,"pct":round(w/len(rows)*100,1),"n":len(rows)}

def build_breakdown(rows):
    out = {}
    for sport in ['mlb','nba','nhl','ncaa_baseball','soccer','mlb_props','nba_props']:
        sub = [r for r in rows if r['sport']==sport]
        if not sub: continue
        out[sport] = {}
        for tier in ['high','medium']:
            t = [r for r in sub if r['confidence_tier']==tier]
            if t: out[sport][tier] = calc_stats(t)
    return out

perf = {
    "generated_at": datetime.datetime.now().isoformat(),
    "yesterday": yesterday,
    "yesterday_results": [
        {"sport":r['sport'],"pick":r['pick'],"tier":r['confidence_tier'],
         "outcome":r['outcome'],"actual":r.get('actual_value',''),
         "line":r.get('line',''),"edge":r.get('edge_pp','')}
        for r in sorted(yest_settled, key=lambda x: x['sport'])
    ],
    "yesterday_summary": build_breakdown(yest_settled),
    "yesterday_overall": calc_stats([r for r in yest_settled if r['confidence_tier'] in ('high','medium')]),
    "alltime_summary": build_breakdown(all_settled),
    "alltime_overall": calc_stats([r for r in all_settled if r['confidence_tier'] in ('high','medium')]),
    "alltime_by_tier": {
        "high": calc_stats([r for r in all_settled if r['confidence_tier']=='high']),
        "medium": calc_stats([r for r in all_settled if r['confidence_tier']=='medium']),
    },
    "pending": [
        {"sport":r['sport'],"pick":r['pick'],"tier":r['confidence_tier']}
        for r in all_rows if r['outcome']=='PENDING'
    ]
}

perf_path = "/workspaces/iqproject/perf.json"
with open(perf_path, 'w') as f:
    _json.dump(perf, f, indent=2)
print(f"\n✓ perf.json written")
print(f"  Yesterday (H+M): {perf['yesterday_overall']['w']}W-{perf['yesterday_overall']['l']}L ({perf['yesterday_overall']['pct']}%)")
print(f"  All-time  (H+M): {perf['alltime_overall']['w']}W-{perf['alltime_overall']['l']}L ({perf['alltime_overall']['pct']}%)")
