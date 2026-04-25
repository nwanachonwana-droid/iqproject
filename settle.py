"""
settle.py — Daily performance settlement script
Run each morning after pipeline: python3 settle.py
Tracks ALL high and medium confidence picks + props
Output: performance_record.csv
"""
import json, os, csv, datetime, urllib.request

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent":"iqproject/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
yesterday_compact = yesterday.replace("-","")
print(f"\n{'='*60}")
print(f"  iQ Performance Settlement — {yesterday}")
print(f"{'='*60}\n")

CSV_FILE = "/workspaces/iqproject/performance_record.csv"
fieldnames = ["date","sport","game","pick","pick_side","model_prob",
              "market_prob","edge_pp","confidence_tier","outcome",
              "actual_value","line","verified"]

# Load existing to avoid duplicates
existing = set()
if os.path.exists(CSV_FILE):
    with open(CSV_FILE) as f:
        for row in csv.DictReader(f):
            existing.add(row.get("date","") + "|" + row.get("sport","") + "|" + row.get("pick",""))

new_rows = []

# ── ESPN SCORE FETCHERS ────────────────────────────────────────
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
                out[hn.lower()] = {"h":h,"a":a,"home":hn}
        return out
    except Exception as e:
        print(f"  ESPN error ({path}): {e}")
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
                if h or a:
                    out[hn.lower()] = {"h":h,"a":a,"home":hn}
        except: pass
    return out

def fuzzy_find(name, scores):
    nl = name.lower()
    if nl in scores: return scores[nl]
    for k,v in scores.items():
        if nl in k or k in nl: return v
    return None

def ml_outcome(score, pick_side):
    if not score: return None
    h, a = score["h"], score["a"]
    if pick_side == "home": return "WIN" if h > a else ("LOSS" if a > h else "PUSH")
    else: return "WIN" if a > h else ("LOSS" if h > a else "PUSH")

# ── GAME PICKS ─────────────────────────────────────────────────
SPORT_PATHS = {
    "mlb":          "baseball/mlb",
    "nba":          "basketball/nba",
    "nhl":          "hockey/nhl",
    "ncaa_baseball":"baseball/college-baseball",
}

for sport, path in SPORT_PATHS.items():
    archive = f"/workspaces/iqproject/{sport}_picks_{yesterday}.json"
    if not os.path.exists(archive):
        print(f"  {sport.upper()}: no archive for {yesterday}")
        continue

    with open(archive) as f:
        d = json.load(f)

    # Only picks from yesterday's games
    picks = [p for p in d.get("picks",[])
             if (p.get("game_time_utc","") or "")[:10] == yesterday
             and p.get("confidence_tier") in ("high","medium")]

    if not picks:
        all_picks = d.get("picks",[])
        yest_picks = [p for p in all_picks if (p.get("game_time_utc","") or "")[:10] == yesterday]
        print(f"  {sport.upper()}: no high/medium picks on {yesterday} ({len(yest_picks)} total picks that day)")
        continue

    scores = espn_scores(path, yesterday_compact) if sport != "soccer" else soccer_scores(yesterday_compact)
    print(f"\n  {sport.upper()} — {len(picks)} high/medium picks on {yesterday}:")

    for p in picks:
        tier = p.get("confidence_tier","")
        pick_team = p.get("pick","")
        pick_side = p.get("pick_side","home")
        home_team = p.get("home_team","")
        away_team = p.get("away_team","")
        game = f"{away_team} @ {home_team}"
        model_p = p.get("model_prob_home") if pick_side=="home" else p.get("model_prob_away")
        market_p = p.get("market_prob_home") if pick_side=="home" else p.get("market_prob_away")

        uid = f"{yesterday}|{sport}|{pick_team}"
        if uid in existing:
            continue

        score = fuzzy_find(home_team, scores)
        outcome = ml_outcome(score, pick_side)
        h_score = score["h"] if score else ""
        a_score = score["a"] if score else ""

        flag = "★" if tier=="high" else "·"
        status = f"{outcome} ({a_score}-{h_score})" if score else "PENDING"
        print(f"    {flag} {pick_team:30} | {tier:6} | {status}")

        new_rows.append({
            "date": yesterday, "sport": sport, "game": game,
            "pick": pick_team, "pick_side": pick_side,
            "model_prob": round(model_p or 0, 4),
            "market_prob": round(market_p, 4) if market_p else "",
            "edge_pp": p.get("edge_pp",""),
            "confidence_tier": tier,
            "outcome": outcome or "PENDING",
            "actual_value": f"{a_score}-{h_score}" if score else "",
            "line": "", "verified": "ESPN" if score else "MANUAL"
        })

# Soccer
soccer_archive = f"/workspaces/iqproject/soccer_picks_{yesterday}.json"
if os.path.exists(soccer_archive):
    with open(soccer_archive) as f:
        d = json.load(f)
    picks = [p for p in d.get("picks",[])
             if (p.get("game_time_utc","") or "")[:10] == yesterday
             and p.get("confidence_tier") in ("high","medium")]
    if picks:
        scores = soccer_scores(yesterday_compact)
        print(f"\n  SOCCER — {len(picks)} high/medium picks on {yesterday}:")
        for p in picks:
            tier = p.get("confidence_tier","")
            pick_team = p.get("pick","")
            pick_side = p.get("pick_side","home")
            home_team = p.get("home_team","")
            away_team = p.get("away_team","")
            game = f"{away_team} @ {home_team}"
            model_p = p.get("model_prob_home") if pick_side=="home" else p.get("model_prob_away")
            market_p = p.get("market_prob_home") if pick_side=="home" else p.get("market_prob_away")
            uid = f"{yesterday}|soccer|{pick_team}"
            if uid in existing: continue
            score = fuzzy_find(home_team, scores)
            outcome = ml_outcome(score, pick_side)
            flag = "★" if tier=="high" else "·"
            print(f"    {flag} {pick_team:30} | {tier:6} | {outcome or 'PENDING'}")
            new_rows.append({
                "date": yesterday, "sport": "soccer", "game": game,
                "pick": pick_team, "pick_side": pick_side,
                "model_prob": round(model_p or 0,4),
                "market_prob": round(market_p,4) if market_p else "",
                "edge_pp": p.get("edge_pp",""), "confidence_tier": tier,
                "outcome": outcome or "PENDING",
                "actual_value": f"{score['a']}-{score['h']}" if score else "",
                "line": "", "verified": "ESPN" if score else "MANUAL"
            })

# ── MLB PROPS ──────────────────────────────────────────────────
mlb_props_archive = f"/workspaces/iqproject/mlb_props_{yesterday}.json"
if os.path.exists(mlb_props_archive):
    with open(mlb_props_archive) as f:
        d = json.load(f)
    props = [p for p in d.get("props",[])
             if (p.get("game_time_utc","") or p.get("date","") or yesterday)[:10] == yesterday
             and p.get("confidence_tier") in ("high","medium")]

    if props:
        print(f"\n  MLB PROPS — {len(props)} high/medium props on {yesterday}:")
        # Get pitcher Ks from MLB Stats API
        pitcher_ks = {}
        try:
            sched = fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={yesterday}&hydrate=boxscore")
            for date_entry in sched.get("dates",[]):
                for game in date_entry.get("games",[]):
                    gid = game.get("gamePk")
                    try:
                        box = fetch(f"https://statsapi.mlb.com/api/v1/game/{gid}/boxscore")
                        for side in ["home","away"]:
                            players = box.get("teams",{}).get(side,{}).get("players",{})
                            pitchers = box.get("teams",{}).get(side,{}).get("pitchers",[])
                            for pid in pitchers:
                                pdata = players.get(f"ID{pid}",{})
                                name = pdata.get("person",{}).get("fullName","")
                                ks = pdata.get("stats",{}).get("pitching",{}).get("strikeOuts",0)
                                if name: pitcher_ks[name.lower()] = int(ks or 0)
                    except: pass
        except Exception as e:
            print(f"  MLB Stats error: {e}")

        for p in props:
            player = p.get("player","")
            line = p.get("line",0)
            direction = (p.get("pick_side","Over")).lower()
            tier = p.get("confidence_tier","")
            uid = f"{yesterday}|mlb_props|{player}_{line}"
            if uid in existing: continue

            actual = pitcher_ks.get(player.lower())
            if actual is not None:
                outcome = "WIN" if (direction=="over" and actual>line) or (direction=="under" and actual<line) else "LOSS"
                status = f"{outcome} (actual:{actual}K line:{line})"
            else:
                outcome = "PENDING"; status = "PENDING"

            flag = "★" if tier=="high" else "·"
            print(f"    {flag} {player:25} {direction.upper()} {line}K | {tier:6} | {status}")
            new_rows.append({
                "date": yesterday, "sport": "mlb_props",
                "game": p.get("game",""),
                "pick": f"{player} {direction.upper()} {line}K",
                "pick_side": direction,
                "model_prob": round(p.get("model_prob",0),4),
                "market_prob": round(p.get("market_prob",0),4),
                "edge_pp": p.get("edge_pp",""), "confidence_tier": tier,
                "outcome": outcome, "actual_value": actual or "",
                "line": line, "verified": "MLB API" if actual is not None else "MANUAL"
            })

# ── NBA PROPS ──────────────────────────────────────────────────
nba_props_archive = f"/workspaces/iqproject/nba_props_{yesterday}.json"
if os.path.exists(nba_props_archive):
    with open(nba_props_archive) as f:
        d = json.load(f)
    props = [p for p in d.get("picks",[])
             if (p.get("game_time_utc","") or "")[:10] == yesterday
             and p.get("confidence_tier") in ("high","medium")]

    if props:
        print(f"\n  NBA PROPS — {len(props)} high/medium props on {yesterday}:")
        # Get player stats from ESPN box scores
        player_stats = {}
        try:
            sc = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={yesterday_compact}")
            for ev in sc.get("events",[]):
                gid = ev.get("id")
                try:
                    box = fetch(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={gid}")
                    for team in box.get("boxscore",{}).get("players",[]):
                        for stat_grp in team.get("statistics",[]):
                            keys = stat_grp.get("keys",[])
                            for ath in stat_grp.get("athletes",[]):
                                name = ath.get("athlete",{}).get("displayName","").lower()
                                vals = ath.get("stats",[])
                                player_stats[name] = dict(zip(keys, vals))
                except: pass
        except Exception as e:
            print(f"  ESPN NBA box error: {e}")

        stat_map = {"PTS":"points","REB":"rebounds","AST":"assists",
                    "STL":"steals","BLK":"blocks","TOV":"turnovers"}

        for p in props:
            player = p.get("player","")
            stat = p.get("stat","")
            line = p.get("line",0)
            direction = p.get("direction","over").lower()
            tier = p.get("confidence_tier","")
            uid = f"{yesterday}|nba_props|{player}_{stat}_{line}"
            if uid in existing: continue

            pdata = player_stats.get(player.lower(),{})
            espn_key = stat_map.get(stat)
            actual = None
            if espn_key:
                try: actual = float(pdata.get(espn_key,0) or 0)
                except: pass

            if actual is not None and actual > 0:
                outcome = "WIN" if (direction=="over" and actual>line) or (direction=="under" and actual<line) else "LOSS"
                status = f"{outcome} (actual:{actual} line:{line})"
            else:
                outcome = "PENDING"; status = "PENDING"

            flag = "★" if tier=="high" else "·"
            print(f"    {flag} {player:25} {stat} {direction.upper()} {line} | {tier:6} | {status}")
            new_rows.append({
                "date": yesterday, "sport": "nba_props",
                "game": p.get("matchup",""),
                "pick": f"{player} {stat} {direction.upper()} {line}",
                "pick_side": direction,
                "model_prob": round(p.get("model_prob",0),4),
                "market_prob": round(p.get("market_prob",0),4),
                "edge_pp": p.get("edge_pp",""), "confidence_tier": tier,
                "outcome": outcome, "actual_value": actual or "",
                "line": line, "verified": "ESPN" if (actual and actual>0) else "MANUAL"
            })

# ── WRITE CSV ──────────────────────────────────────────────────
if new_rows:
    write_header = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header: writer.writeheader()
        writer.writerows(new_rows)
    print(f"\n✓ {len(new_rows)} rows written to performance_record.csv")
else:
    print("\n~ No new rows to add (already recorded or no picks)")

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
    print(f"\n  PENDING (verify manually): {len(pending)} picks")
    for r in pending:
        print(f"    {r['sport']:15} {r['pick']}")

print(f"\nCSV: {CSV_FILE}")
print(f"Run daily: python3 /workspaces/iqproject/settle.py\n")
