"""Scrape CFL play-by-play (Genius Sports / Betgenius widget feed behind cfl.ca game pages).

Usage:  python scrape.py <cfl.ca game URL | fixtureId> [...]
Writes: data/raw/<fixtureId>/*.json   (untouched API responses)
        data/plays_<fixtureId>.json   (one row per offensive play)
        report_<fixtureId>.html       (filterable table + validation vs official team stats)
"""
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
BASE = "https://gsm-widgets.betstream.betgenius.com/v1/widget"
CUSTOMER = "democfl_light"
FIELD = 110  # CFL field: goal line to goal line
KICKS = ("Kickoff", "Punt", "OnePoint", "FieldGoal")  # kicks are not offensive plays

POS = {
    "OFFENSIVE_QUARTERBACK": "QB", "OFFENSIVE_RUNNINGBACK": "RB", "OFFENSIVE_FULLBACK": "FB",
    "OFFENSIVE_WIDERECEIVER": "WR", "OFFENSIVE_TIGHTEND": "TE", "OFFENSIVE_OFFENSIVELINEMAN": "OL",
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def resolve_fixture(arg):
    if arg.isdigit():
        return arg
    m = re.search(r"fixtureId=(\d+)", get(arg))
    if not m:
        raise SystemExit(f"no fixtureId found on {arg}")
    return m.group(1)


def fetch_raw(fid, refresh=False):
    out = ROOT / "data" / "raw" / fid
    out.mkdir(parents=True, exist_ok=True)
    raw = {}
    for name, path in [
        ("playByPlay", f"playByPlay/customer/{CUSTOMER}/locale/any/geolocale/any-"),
        ("lineups", f"lineups/customer/{CUSTOMER}/locale/any/geolocale/any-"),
        ("teamStats", f"teamStats/customer/{CUSTOMER}/locale/any/geolocale/any-"),
        ("scoreboard", f"multisportscoreboardwidget/customer/{CUSTOMER}/locale/en-US/geolocale/CA-MB"),
    ]:
        f = out / f"{name}.json"
        if refresh or not f.exists():
            f.write_text(get(f"{BASE}/{path}?fixtureId={fid}"), encoding="utf-8")
        text = f.read_text(encoding="utf-8")
        raw[name] = json.loads(text)["data"]["details"]
    return raw


# ---------------------------------------------------------------- parsing

def build_rosters(lineups):
    """(teamId, jersey#) -> (full name, position). Offensive positions win jersey-number clashes."""
    roster = {}
    for side in ("homeTeam", "awayTeam"):
        for group in lineups[side].values():
            if not isinstance(group, list):
                continue
            for p in group:
                key = (p["teamId"], p["number"])
                pos = POS.get(p["position"], p["position"].split("_")[0][:3])
                if key not in roster or p["position"] in POS:
                    roster[key] = (p["name"], pos)
    return roster


def clock_secs(c):
    m, s = c.split(":")
    return int(m) * 60 + int(s)


def parse_start(s):
    """'3rd & 23 at WPG 23' -> (3, 23, 'WPG', 23)"""
    m = re.match(r"(\d)\w\w & (\d+|Goal) at (\w+) (-?\d+)", s or "")
    if not m:
        return None, None, None, None
    dist = m.group(2)
    return int(m.group(1)), (None if dist == "Goal" else int(dist)), m.group(3), int(m.group(4))


def parse_yards(text):
    if re.search(r"for no gain", text):
        return 0
    m = re.search(r"for gain of (\d+) yards?", text)
    if m:
        return int(m.group(1))
    m = re.search(r"for loss of (\d+) yards?", text) or re.search(r"for (\d+) yards? loss", text)
    if m:
        return -int(m.group(1))
    m = re.search(r"for (-?\d+) yards?(?: gain)?(?: to the|\b)", text) or re.search(r"rush [a-z]+ for (-?\d+) yards", text)
    return int(m.group(1)) if m else None


def special_teams_td(desc, teams):
    """Scoring team (abbr) of a touchdown on a kick/punt row, else None."""
    txt = desc.split(" PENALTY ")[0]
    if "TOUCHDOWN" not in txt or "nullified" in txt or "NO PLAY" in desc:
        return None
    mz = re.search(r"to the ([A-Z]{2,3})-?\d\d TOUCHDOWN", txt)
    return next((t for t in teams.values() if t != mz.group(1)), None) if mz else None


def parse_plays(raw, fid):
    sb = raw["scoreboard"]
    teams = {sb[k]["competitorId"]: sb[k]["abbreviation"] for k in ("homeTeam", "awayTeam")}
    roster = build_rosters(raw["lineups"])
    info = raw["playByPlay"]["playByPlayInfo"]

    # The feed lists each quarter newest-first. Game clock is the primary order (feed timestamps can run
    # backwards); some plays lack a clock, so borrow a neighbour's for sorting; ties fall back to feed order.
    feed = []
    for q in ("Q1", "Q2", "Q3", "Q4", "OT"):
        qn = 5 if q == "OT" else int(q[1])  # overtime is reported as period 5
        ps = info.get(q, [])
        for i, p in enumerate(ps):
            near = [ps[j] for j in (i - 1, i + 1) if 0 <= j < len(ps) and "clock" in ps[j]]
            secs = clock_secs(p["clock"]) if "clock" in p else (clock_secs(near[0]["clock"]) if near else 0)
            feed.append(((qn, -secs, -i), dict(p, _qtr=qn)))
    feed = [p for _, p in sorted(feed, key=lambda t: t[0])]

    def person(team, num, abbr_name):
        name, pos = roster.get((team, int(num)), (abbr_name, ""))
        return name, pos

    rows = []
    def one(p):
        if p["type"] in KICKS:
            return None  # kicks and 2-pt converts are not scrimmage plays
        team = p["teamId"]
        off = teams[team]
        desc = p["description"]
        # on a reviewed play the text before "The previous play is under review" is the final ruling;
        # anything after "Original Play:" is what was called on the field -- ignore it
        play_txt = re.split(r"\s*The previous play is under|\s*\(Original Play:", desc)[0]
        play_part, _, pen_part = play_txt.partition(" PENALTY ")
        down, dist, side, yd = parse_start(p["playStartPosition"])
        own_yd = None if yd is None else (yd if side == off else FIELD - yd)

        qb = qb_pos = carrier = carrier_pos = formation = direction = None
        m = re.match(r"(.*?)#(\d+) ", play_part)
        formation = (m.group(1).strip() or None) if m else None

        if p["type"] == "Pass":
            m = re.search(r"#(\d+) (.+?) pass (complete|incomplete|intercepted)(?: (deep|short))?(?: (left|middle|right))?", play_part)
            qb = person(team, m.group(1), m.group(2))[0]
            direction = " ".join(g for g in (m.group(4), m.group(5)) if g) or None
            m2 = re.search(r"to #(\d+) (.+?) (?:caught|thrown)", play_part)
            if m2:
                carrier, carrier_pos = person(team, m2.group(1), m2.group(2))
            ptype = "Pass"
        elif p["type"] == "Sack":
            m = re.search(r"#(\d+) (.+?) sacked", play_part)
            qb = person(team, m.group(1), m.group(2))[0]
            ptype = "Sack"
        elif p["type"] == "Kneel":
            m = re.search(r"by #(\d+) (.+?) at", play_part)
            carrier, carrier_pos = person(team, m.group(1), m.group(2))
            ptype = "Kneel"
        elif p["type"] == "Run":
            m = re.search(r"#(\d+) (.+?) rush(?: (left|middle|right))?", play_part)
            carrier, carrier_pos = person(team, m.group(1), m.group(2))
            direction = m.group(3)
            ptype = "Run"
        elif p["type"] == "Fumble":
            m = re.search(r"#(\d+) (.+?) fumbled snap", play_part)
            carrier, carrier_pos = person(team, m.group(1), m.group(2))
            ptype = "Fumbled snap"
            mr = re.search(r"#(\d+) ([^#]+?) rush(?: (left|middle|right))?", play_part)
            if mr:  # snap fumbled, recovered and run: official stats score it as a rush
                carrier, carrier_pos = person(team, mr.group(1), mr.group(2))
                direction, ptype = mr.group(3), "Run"
        elif p["type"] == "TwoPoints":
            m = re.search(r"#(\d+) (.+?) (pass|rush) attempt", play_part)
            ptype = f"2pt {m.group(3)}" if m else "2pt conv"
            if m:
                name, pos = person(team, m.group(1), m.group(2))
                if pos == "QB" and m.group(3) == "pass":
                    qb = name  # only the passer is named; the target isn't in the feed
                else:
                    carrier, carrier_pos = name, pos
        else:  # pre-snap penalty rows etc.
            ptype = p["type"]

        if carrier_pos == "QB" and ptype in ("Run", "Kneel"):
            qb = carrier

        no_play = "NO PLAY" in desc
        yards = None if no_play else (0 if p.get("subType") in ("IncompletePass", "Interception") or " pass intercepted by " in play_part or " pass incomplete " in play_part else parse_yards(play_part))
        fumble_lost = False
        mrec = re.search(r"recovered by (\w+) #", play_part)
        if mrec and mrec.group(1) != off:
            fumble_lost = True

        # result (first match wins)
        if no_play:
            result = "No play (penalty)"
        elif p["type"] == "TwoPoints":
            result = "2pt good" if p.get("subType") == "Success" else "2pt failed"
        elif re.search(r"intercept", play_part, re.I):
            result = "Interception"
        elif fumble_lost:
            result = "Fumble lost"
        elif "TOUCHDOWN" in play_part:
            result = "Touchdown"
        elif "TURNOVER ON DOWNS" in play_part:
            result = "Turnover on downs"
        elif "fumble" in play_part.lower():
            result = "Fumble (offence recovered)"
        elif ptype == "Sack":
            result = "Sack"
        elif p.get("subType") == "IncompletePass" or (ptype == "Pass" and "incomplete" in play_part):
            result = "Incomplete"
        elif "1ST DOWN" in play_part or "1ST DOWN" in pen_part:
            result = "1st down"
        else:
            result = "Complete" if ptype == "Pass" else ""

        if p["type"] == "TwoPoints" and not no_play:
            yards = 3 if p.get("subType") == "Success" else 0  # derived: conversions are snapped from the 3, official stats credit 3

        # points scored on the play, from the offence's perspective (kicks are not offensive plays)
        points = 0
        if p["type"] == "TwoPoints" and not no_play:
            points = 2 if p.get("subType") == "Success" else 0
        elif not no_play:
            if "TOUCHDOWN" in play_part and "nullified" not in play_part:
                # the end zone named in the description belongs to the team that was scored on
                mz = re.search(r"to the ([A-Z]{2,3})-?\d\d TOUCHDOWN", play_part)
                if mz:
                    points = 6 if mz.group(1) != off else -6
                else:
                    points = -6 if result in ("Interception", "Fumble lost") else 6
            elif re.search(r"safety", play_part, re.I):
                points = -2

        # validation helper: end spot named in description vs start + yards
        end_ok = None
        me = re.search(r"to the (\w+?)(-?\d{2})\b", play_part)
        if me and yards is not None and own_yd is not None and ptype in ("Run", "Pass", "Sack", "Kneel") and result not in ("Interception", "Fumble lost"):
            e_yd = int(me.group(2))
            end_own = e_yd if me.group(1) == off else FIELD - e_yd
            end_ok = (end_own - own_yd) == yards

        return {
            "game": fid,
            "qtr": p["_qtr"],
            "clock": p.get("clock"),
            "offense": off,
            "down": down,
            "distance": dist,
            "yardline": f"{side} {yd}" if side else "",
            "own_yd": own_yd,            # yards from offence's own goal line (0-110)
            "yds_to_goal": None if own_yd is None else FIELD - own_yd,
            "play_type": ptype,
            "qb": qb,
            "ballcarrier": carrier,       # rusher, or pass target
            "bc_pos": carrier_pos,
            "direction": direction,
            "formation": formation,
            "yards": yards,
            "result": result,
            "points": points,             # +6 TD, -6 defensive TD, -2 safety (offence's perspective)
            "reviewed": "under automatic review" in desc or "challenge" in desc.lower(),
            "first_down": "1ST DOWN" in desc and not no_play,
            "penalty": pen_part or None,
            "end_spot_matches": end_ok,
            "description": desc,
            "play_id": p["id"],
        }

    after_kick = False
    for p in feed:
        try:
            row = one(p)
        except Exception as e:  # keep the play so nothing silently disappears
            row = {"game": fid, "qtr": p["_qtr"], "clock": p.get("clock"), "offense": teams[p["teamId"]],
                   "play_type": "UNPARSED", "result": "", "points": 0, "yards": None, "description": p["description"],
                   "play_id": p["id"], "penalty": f"{type(e).__name__}: {e}"}
        if row:
            row["_after_kick"] = after_kick
            after_kick = False
            rows.append(row)
        elif p["type"] in KICKS:
            after_kick = True
    flag_gaps(rows)
    for r in rows:
        r.pop("_after_kick", None)

    # Plays we can't categorise at all are set aside (not analysed); the rest get a `flags` column.
    skipped = []
    for r in rows:
        if r["play_type"] == "UNPARSED":
            r["skip_reason"] = f"unparsed: {r['penalty']}"
        elif r["play_type"] not in CATEGORIES:
            r["skip_reason"] = f"unrecognised play type '{r['play_type']}'"
    skipped = [r for r in rows if r.get("skip_reason")]
    rows = [r for r in rows if not r.get("skip_reason")]

    # QB for plays where the feed doesn't name one (RB runs, penalties): nearest previous stated QB for that team
    last = {}
    for r in rows:
        if r["qb"]:
            last[r["offense"]] = r["qb"]
            r["qb_src"] = "stated"
        else:
            r["qb"] = last.get(r["offense"])
            r["qb_src"] = "inferred"
    # plays before a team's first stated QB: back-fill from that first one
    first = {}
    for r in rows:
        if r["qb_src"] == "stated":
            first.setdefault(r["offense"], r["qb"])
    for r in rows:
        if r["qb"] is None:
            r["qb"] = first.get(r["offense"])
    for r in rows:
        r["flags"] = play_flags(r)
    return rows, teams, skipped


CATEGORIES = ("Run", "Pass", "Sack", "Kneel", "Fumbled snap", "Penalty", "2pt pass", "2pt rush", "2pt conv")


def play_flags(r):
    """Comma-separated reasons a categorised play looks sketchy (empty = looks fine). No-play rows aren't judged."""
    if r["result"] == "No play (penalty)":
        return ""
    f = []
    is2 = r["play_type"].startswith("2pt")
    if r.get("chain_gap"):
        f.append("gap_before")          # start doesn't follow from the previous play: feed probably missing one
    if r.get("end_spot_matches") is False:
        f.append("spot_mismatch")       # parsed yards disagree with the end spot in the description
    if r["down"] is None and not is2:
        f.append("no_down")
    if r["yards"] is None and r["play_type"] != "Penalty":
        f.append("no_yards")
    if r["play_type"] == "Pass" and r["result"] != "Interception" and not r["ballcarrier"] and r["result"] != "Incomplete":
        f.append("no_target")
    if r["play_type"] in ("Run", "Kneel", "Fumbled snap") and not r["ballcarrier"]:
        f.append("no_ballcarrier")
    if r["play_type"] in ("Pass", "Sack") and not r["qb"]:
        f.append("no_qb")
    if (r["ballcarrier"] and not r["bc_pos"]):
        f.append("player_not_in_roster")
    return ",".join(f)


def flag_gaps(rows):
    """Mark plays whose start (spot, down) can't follow from the previous play on the same drive -- i.e. the
    feed is probably missing a play before it. Sets row["chain_gap"] = True/False."""
    TURNOVER = ("Touchdown", "Interception", "Fumble lost", "Turnover on downs")
    for r in rows:
        r["chain_gap"] = False
    for prev, cur in zip(rows, rows[1:]):
        if (prev["offense"] != cur["offense"] or prev["qtr"] != cur["qtr"] or prev["play_type"] == "UNPARSED"
                or cur["play_type"] == "UNPARSED" or prev.get("own_yd") is None or cur.get("own_yd") is None
                or prev["result"] in TURNOVER or cur.get("_after_kick")):
            continue
        no_play = prev["result"] == "No play (penalty)"
        pen_ok = not prev["penalty"] or "declined" in prev["penalty"] or no_play
        if not pen_ok:
            continue  # accepted penalty on a live play moves the ball in ways we don't model
        if no_play:
            m = re.search(r"from (\w+?)(-?\d+) to (\w+?)(-?\d+)", prev["penalty"] or prev["description"])
            if not m:
                continue
            side, n = m.group(3), int(m.group(4))
            end = n if side == prev["offense"] else FIELD - n
        else:
            end = prev["own_yd"] + (prev["yards"] or 0)
        gap = cur["own_yd"] != end
        if not no_play and not gap:
            made_first = "1ST DOWN" in prev["description"]
            exp_ok = (cur["down"] == 1) if made_first else (cur["down"] == (prev["down"] or 0) + 1)
            gap = not exp_ok
        cur["chain_gap"] = gap


# ---------------------------------------------------------------- validation

def official_stats(raw, teams):
    home = raw["scoreboard"]["homeTeam"]["abbreviation"]
    away = raw["scoreboard"]["awayTeam"]["abbreviation"]
    st = {s["id"]: s for s in raw["teamStats"]["stats"]}
    out = {}
    for side, ab in (("home", home), ("away", away)):
        out[ab] = {
            "pass_yds": st["passing_detailed"][side][0], "pass_att": st["passing_detailed"][side][1],
            "rush_yds": st["rushing_detailed"][side][0], "rush_att": st["rushing_detailed"][side][1],
            "sacks": st["sacks_for_detailed"]["away" if side == "home" else "home"][0],  # sacks BY the other team
            "net_plays": st["net_offense_detailed"][side][1], "net_yds": st["net_offense_detailed"][side][0],
        }
    return out


def computed_stats(rows):
    out = {}
    for r in rows:
        if r["result"] == "No play (penalty)" or r["play_type"] == "Penalty":
            continue
        s = out.setdefault(r["offense"], dict(pass_yds=0, pass_att=0, comp=0, rush_yds=0, rush_att=0, sacks=0, sack_yds=0, net_plays=0, net_yds=0))
        y = r["yards"] or 0
        s["net_plays"] += 1
        s["net_yds"] += y
        if r["play_type"] in ("Pass", "2pt pass"):
            s["pass_att"] += 1
            if r["result"] not in ("Incomplete", "2pt failed"):
                s["comp"] += 1
                s["pass_yds"] += y
        elif r["play_type"] in ("Run", "2pt rush", "2pt conv"):
            s["rush_att"] += 1
            s["rush_yds"] += y
        elif r["play_type"] == "Sack":
            s["sacks"] += 1
            s["sack_yds"] += y
    return out


# ---------------------------------------------------------------- html

COLS = [
    ("qtr", "Q"), ("clock", "Clock"), ("offense", "Off"), ("down", "Dn"), ("distance", "Dist"),
    ("yardline", "Yardline"), ("yds_to_goal", "To goal"), ("play_type", "Type"), ("qb", "QB"),
    ("ballcarrier", "Ballcarrier / target"), ("bc_pos", "Pos"), ("yards", "Yds"), ("result", "Result"), ("points", "Pts"),
    ("penalty", "Penalty"), ("flags", "Flags"), ("end_spot_matches", "Spot chk"), ("description", "Description"),
]
FILTER_SELECT = {"qtr", "offense", "down", "play_type", "qb", "ballcarrier", "bc_pos", "result"}


def render_html(rows, teams, official, title):
    data = json.dumps(rows)
    head = "".join(f"<th data-k='{k}'>{html.escape(h)}</th>" for k, h in COLS)
    filt = "".join(
        f"<th><select data-f='{k}'><option value=''>all</option></select></th>" if k in FILTER_SELECT
        else (f"<th><input data-f='{k}' placeholder='filter'></th>" if k in ("description", "yardline", "penalty", "flags") else "<th></th>")
        for k, _ in COLS
    )
    return f"""<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>
<style>
body{{font:13px system-ui,sans-serif;margin:16px}} table{{border-collapse:collapse}}
th,td{{border:1px solid #ccc;padding:2px 6px;text-align:left;vertical-align:top}}
thead th{{position:sticky;top:0;background:#f3f3f3;cursor:pointer}} tr.f2 th{{top:24px;cursor:default}}
td.num{{text-align:right}} td.d{{max-width:520px}} .bad{{background:#fdd}} .ok{{color:#2a7}}
tr.np td{{color:#888}} #sum td,#sum th{{padding:2px 10px}} .chk-bad{{background:#fdd}} .chk-ok{{background:#dfd}}
</style>
<h2>{html.escape(title)}</h2>
<h3>Validation: computed from plays vs official team stats</h3><div id=val></div>
<h3>Filtered selection <small id=cnt></small></h3><table id=sum></table><p>
<button onclick="resetF()">reset filters</button>
<table id=t><thead><tr>{head}</tr><tr class=f2>{filt}</tr></thead><tbody></tbody></table>
<script>
const ROWS={data}, COLS={json.dumps([k for k,_ in COLS])}, OFF={json.dumps(official)};
const COMP={json.dumps(computed_stats(rows))};
const sels=[...document.querySelectorAll('[data-f]')];
for(const s of sels.filter(s=>s.tagName=='SELECT')){{
  const vals=[...new Set(ROWS.map(r=>r[s.dataset.f]).filter(v=>v!==null&&v!==''))].sort((a,b)=>a>b?1:-1);
  for(const v of vals) s.add(new Option(v,v));
}}
let sortK=null,sortDir=1;
document.querySelectorAll('thead tr:first-child th').forEach(th=>th.onclick=()=>{{
  sortDir=sortK==th.dataset.k?-sortDir:1; sortK=th.dataset.k; render()}});
sels.forEach(s=>s.oninput=render);
function resetF(){{sels.forEach(s=>s.value='');render()}}
function esc(x){{return String(x).replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]))}}
function render(){{
  let rows=ROWS.filter(r=>sels.every(s=>{{
    const v=s.value.trim(); if(!v) return true; const x=r[s.dataset.f];
    return s.tagName=='SELECT' ? String(x)===v : String(x??'').toLowerCase().includes(v.toLowerCase())}}));
  if(sortK) rows=[...rows].sort((a,b)=>((a[sortK]??-1e9)>(b[sortK]??-1e9)?1:-1)*sortDir);
  document.querySelector('#t tbody').innerHTML=rows.map(r=>'<tr class="'+(r.result.startsWith('No play')||r.play_type=='Penalty'?'np':'')+'">'+COLS.map(k=>{{
    let v=r[k]; if(k=='end_spot_matches') v=v===null?'':(v?'ok':'MISMATCH');
    const cls=(k=='description'?'d':(typeof v=='number'?'num':''))+(k=='end_spot_matches'&&v=='MISMATCH'?' bad':'');
    return '<td class="'+cls+'">'+esc(v??'')+'</td>'}}).join('')+'</tr>').join('');
  const g=(f)=>rows.filter(f); const sum=a=>a.reduce((s,r)=>s+(r.yards||0),0);
  const real=rows.filter(r=>!r.result.startsWith('No play')&&r.play_type!='Penalty');
  document.getElementById('cnt').textContent='('+rows.length+' rows, '+real.length+' real plays, '+sum(real)+' net yds, avg '+(real.length?(sum(real)/real.length).toFixed(2):'-')+')';
  document.getElementById('sum').innerHTML='';
}}
function validation(){{
  const lines=[];
  for(const t of Object.keys(OFF)){{
    const o=OFF[t], c=COMP[t]||{{}};
    const chk=(label,mine,theirs)=>'<tr><td>'+t+'</td><td>'+label+'</td><td>'+mine+'</td><td>'+theirs+'</td><td class="'+(mine==theirs?'chk-ok':'chk-bad')+'">'+(mine==theirs?'match':'diff '+(mine-theirs))+'</td></tr>';
    lines.push(chk('Pass att',c.pass_att,o.pass_att),chk('Pass yds (completions)',c.pass_yds,o.pass_yds),
      chk('Rush att',c.rush_att,o.rush_att),chk('Rush yds',c.rush_yds,o.rush_yds),
      chk('Sacks against',c.sacks,o.sacks),
      chk('Net plays (incl. sacks, kneels, fumbled snap)',c.net_plays,o.net_plays),chk('Net yds',c.net_yds,o.net_yds));
  }}
  document.getElementById('val').innerHTML='<table><tr><th>Team</th><th>Stat</th><th>From plays</th><th>Official</th><th></th></tr>'+lines.join('')+'</table>';
}}
validation(); render();
</script>"""


def main(args):
    for arg in args:
        fid = resolve_fixture(arg)
        raw = fetch_raw(fid)
        rows, teams, skipped = parse_plays(raw, fid)
        (ROOT / "data").mkdir(exist_ok=True)
        (ROOT / "data" / f"plays_{fid}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
        sb = raw["scoreboard"]
        title = f"{sb['awayTeam']['displayName']} {sb['awayTeam']['score']} @ {sb['homeTeam']['displayName']} {sb['homeTeam']['score']} (fixture {fid})"
        out = ROOT / f"report_{fid}.html"
        out.write_text(render_html(rows, teams, official_stats(raw, teams), title), encoding="utf-8")
        print(f"{fid}: {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["https://cfl.ca/games/6659/montreal-alouettes-vs-winnipeg-blue%20bombers/"])
