import urllib.request, json, math

DATAGOLF_KEY = "26c9f2ab8405d589166a8e2fb214"

SCREEN_ODDS = {
    "Scottie Scheffler":  {"outright": 400,  "top5": -110, "top10": -250, "top20": -750},
    "Rory McIlroy":       {"outright": 850,  "top5": 180,  "top10": -110, "top20": -250},
    "Cameron Young":      {"outright": 1100, "top5": 230,  "top10": 120,  "top20": -185},
    "Jon Rahm":           {"outright": 1400, "top5": 250,  "top10": 125,  "top20": -185},
    "Xander Schauffele":  {"outright": 1600, "top5": 300,  "top10": 140,  "top20": -165},
    "Ludvig Aberg":       {"outright": 1800, "top5": None, "top10": None, "top20": None},
    "Bryson DeChambeau":  {"outright": 2000, "top5": None, "top10": None, "top20": None},
    "Matt Fitzpatrick":   {"outright": 2500, "top5": None, "top10": None, "top20": None},
    "Tommy Fleetwood":    {"outright": 2800, "top5": None, "top10": None, "top20": None},
    "Brooks Koepka":      {"outright": 4000, "top5": None, "top10": None, "top20": None},
    "Patrick Cantlay":    {"outright": 4000, "top5": None, "top10": None, "top20": None},
    "Collin Morikawa":    {"outright": 4500, "top5": None, "top10": None, "top20": None},
}

def ap(a):
    if a is None: return None
    return 100/(a+100) if a > 0 else abs(a)/(abs(a)+100)

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent":"iqproject/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

# Use pre-tournament for baseline, in-play for live updates if round started
print("Fetching DataGolf pre-tournament predictions...")
pt = fetch(f"https://feeds.datagolf.com/preds/pre-tournament?tour=pga&dead_heat=no&odds_format=percent&file_format=json&key={DATAGOLF_KEY}")
baseline = pt.get("baseline", [])

print("Fetching DataGolf in-play predictions...")
ip_resp = fetch(f"https://feeds.datagolf.com/preds/in-play?tour=pga&dead_heat=no&odds_format=percent&file_format=json&key={DATAGOLF_KEY}")
inplay  = ip_resp.get("data", [])
info    = ip_resp.get("info", {})

# Build lookups: "Last, First" -> player
def build_lookup(lst):
    return {p["player_name"].lower(): p for p in lst}

pt_lookup = build_lookup(baseline)
ip_lookup = build_lookup(inplay)

def find(name, lookup):
    # Convert "First Last" -> "Last, First"
    parts = name.strip().split()
    dg_key = f"{parts[-1]}, {' '.join(parts[:-1])}".lower()
    if dg_key in lookup: return lookup[dg_key]
    # last name only fallback
    last = parts[-1].lower()
    for k, v in lookup.items():
        if k.split(",")[0].strip() == last: return v
    return None

# Prefer in-play if round is underway (score != 0), else use pre-tournament
round_started = any(p.get("current_score", 0) != 0 for p in inplay)

print(f"Round started: {round_started} | Round: {info.get('current_round',1)} | Updated: {info.get('last_update','')}")
print(f"Using: {'in-play model' if round_started else 'pre-tournament model'}\n")

def get_probs(name):
    """Return (win, top5, top10, top20, make_cut, pos, score, thru) for a player."""
    if round_started:
        p = find(name, ip_lookup)
        if p:
            # in-play: values are fractions already (0-1), need * 100 check
            w = float(p.get("win", 0))
            # If values look like percentages (e.g. 13.2 not 0.132), divide by 100
            if w > 1: w /= 100
            t5  = float(p.get("top_5",  0)); t5  = t5/100  if t5  > 1 else t5
            t10 = float(p.get("top_10", 0)); t10 = t10/100 if t10 > 1 else t10
            t20 = float(p.get("top_20", 0)); t20 = t20/100 if t20 > 1 else t20
            mc  = float(p.get("make_cut", 0)); mc = mc/100 if mc > 1 else mc
            return w, t5, t10, t20, mc, str(p.get("current_pos","-")), str(p.get("current_score","-")), str(p.get("thru","-"))
    # Pre-tournament: values are already 0-1 decimals
    p = find(name, pt_lookup)
    if p:
        return (float(p.get("win",0)), float(p.get("top_5",0)),
                float(p.get("top_10",0)), float(p.get("top_20",0)),
                float(p.get("make_cut",0)), "--", "E", "--")
    return None, None, None, None, None, "--", "--", "--"

print(f"{'='*72}")
print(f"  PGA Championship — DataGolf vs Fanatics Odds")
print(f"  Event: {pt.get('event_name','PGA Championship')} | Updated: {pt.get('last_updated','')}")
print(f"{'='*72}")
print(f"  {'PLAYER':<22} {'POS':>4} {'SCORE':>6} | {'DG WIN':>7} {'MKT WIN':>7} {'EDGE':>8} | {'T5 EDG':>7} {'T10 EDG':>8} {'T20 EDG':>8}")
print("  " + "-"*88)

best_bets = []

for player, odds in SCREEN_ODDS.items():
    dg_win, dg_t5, dg_t10, dg_t20, dg_mc, pos, score, thru = get_probs(player)
    if dg_win is None:
        print(f"  {player:<22} NOT FOUND"); continue

    mkt_win = ap(odds.get("outright"))
    mkt_t5  = ap(odds.get("top5"))
    mkt_t10 = ap(odds.get("top10"))
    mkt_t20 = ap(odds.get("top20"))

    def ep(dg, mkt): return round((dg-mkt)*100,1) if dg is not None and mkt is not None else None
    def fe(v): return f"{v:+6.1f}pp" if v is not None else "     N/A"
    def fp(v): return f"{v*100:6.1f}%" if v is not None else "    N/A"

    we = ep(dg_win, mkt_win)
    score_str = f"{score}({thru})" if thru not in ("--","0","") else score

    print(f"  {player:<22} {pos:>4} {score_str:>7} | {fp(dg_win)} {fp(mkt_win)} {fe(we)} | {fe(ep(dg_t5,mkt_t5))} {fe(ep(dg_t10,mkt_t10)):>9} {fe(ep(dg_t20,mkt_t20)):>9}")

    for label, dg, mkt, odd in [
        ("OUTRIGHT WIN", dg_win, mkt_win, odds.get("outright")),
        ("TOP 5",        dg_t5,  mkt_t5,  odds.get("top5")),
        ("TOP 10",       dg_t10, mkt_t10, odds.get("top10")),
        ("TOP 20",       dg_t20, mkt_t20, odds.get("top20")),
    ]:
        e = ep(dg, mkt)
        if e is not None and e >= 3:
            best_bets.append({"player":player,"market":label,"odds":odd,
                               "dg":dg,"mkt":mkt,"edge":e,"pos":pos,"score":score})

best_bets.sort(key=lambda x: x["edge"], reverse=True)

print(f"\n{'='*72}")
print("  BEST BETS (>=3pp edge, ranked)")
print(f"{'='*72}")
if not best_bets:
    print("  No edges >=3pp found vs current market odds.")
else:
    for b in best_bets:
        o = f"+{b['odds']}" if b['odds'] and b['odds'] > 0 else str(b['odds'])
        tier = "HIGH" if b["edge"]>=6 else "MED" if b["edge"]>=4 else "LOW"
        print(f"\n  [{tier}] {b['player']} (pos:{b['pos']}, {b['score']}) — {b['market']}")
        print(f"         Odds:{o}  DG:{b['dg']*100:.1f}%  Mkt:{b['mkt']*100:.1f}%  Edge:+{b['edge']}pp")

print(f"\n{'='*72}")
print("  FANATICS BOOST: All 4 make cut @ +100")
print(f"{'='*72}")
boost = ["Scottie Scheffler","Rory McIlroy","Cameron Young","Bryson DeChambeau"]
probs = []
for name in boost:
    _, _, _, _, mc, pos, score, _ = get_probs(name)
    if mc is not None:
        probs.append((name, mc))
        print(f"  {name:<25} DG make-cut: {mc*100:.1f}%  pos:{pos}  score:{score}")
    else:
        print(f"  {name:<25} NOT FOUND")

if len(probs) == 4:
    joint = math.prod(v for _,v in probs)
    mkt   = ap(100)
    edge  = (joint - mkt) * 100
    print(f"\n  Joint DG prob: {joint*100:.1f}%  |  Market (+100): 50.0%  |  Edge: {edge:+.1f}pp")
    print(f"  -> {'BET THE BOOST ✓' if edge >= 3 else 'PASS'}")
print()


