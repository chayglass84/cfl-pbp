"""Load 2026 CFL regular-season plays into SQLite (data/cfl.db).

Usage: python load_db.py [cfl_id ...]     (no args = every completed 2026 regular-season game)
Needs data/games_2026.json from `python discover.py`.
"""
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import scrape
from scrape import ROOT

DB = ROOT / "data" / "cfl.db"
PLAY_COLS = [
    ("qtr", "INTEGER"), ("clock", "TEXT"), ("offense", "TEXT"), ("down", "INTEGER"), ("distance", "INTEGER"),
    ("yardline", "TEXT"), ("own_yd", "INTEGER"), ("yds_to_goal", "INTEGER"), ("play_type", "TEXT"),
    ("qb", "TEXT"), ("qb_src", "TEXT"), ("ballcarrier", "TEXT"), ("bc_pos", "TEXT"), ("direction", "TEXT"),
    ("formation", "TEXT"), ("yards", "INTEGER"), ("result", "TEXT"), ("points", "INTEGER"),
    ("reviewed", "INTEGER"), ("first_down", "INTEGER"), ("penalty", "TEXT"), ("end_spot_matches", "INTEGER"), ("chain_gap", "INTEGER"), ("flags", "TEXT"), ("team_stats_off", "INTEGER"), ("description", "TEXT"),
    ("play_id", "TEXT"),
]
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY,      -- Genius Sports fixture id
  cfl_id INTEGER, round TEXT, start TEXT, home TEXT, away TEXT, home_score INTEGER, away_score INTEGER,
  n_plays INTEGER, n_skipped INTEGER, spot_mismatches INTEGER, chain_gaps INTEGER,
  stat_check TEXT,               -- 'ok' or the official-vs-computed differences
  points_check TEXT              -- 'ok' or computed-vs-final score differences
);
CREATE TABLE IF NOT EXISTS plays (
  game_id TEXT NOT NULL REFERENCES games(game_id), seq INTEGER NOT NULL,  -- seq = order within game
  {", ".join(f"{c} {t}" for c, t in PLAY_COLS)},
  PRIMARY KEY (game_id, seq)
);
CREATE TABLE IF NOT EXISTS skipped_plays (   -- plays we could not categorise at all
  game_id TEXT NOT NULL REFERENCES games(game_id), qtr INTEGER, clock TEXT, offense TEXT, play_type TEXT,
  reason TEXT, description TEXT, play_id TEXT
);
CREATE INDEX IF NOT EXISTS plays_qb ON plays(qb);
CREATE INDEX IF NOT EXISTS plays_bc ON plays(ballcarrier);
"""


def team_points(raw, rows, teams):
    """Final score implied by the feed: kicks + offensive-play points (defensive scores go to the other team)."""
    pts = {t: 0 for t in teams.values()}
    kick = {("FieldGoal", "Success"): 3, ("OnePoint", "Success"): 1, ("Punt", "Single"): 1, ("Kickoff", "Single"): 1}  # rouges
    for q, ps in raw["playByPlay"]["playByPlayInfo"].items():
        if q == "HL":
            continue
        for p in ps:
            d = p["description"]
            if "nullified" not in d:  # e.g. a good field goal wiped out by a penalty
                if not (p["type"] == "OnePoint" and "attempt failed" in d):  # feed mislabels some blocked converts
                    pts[teams[p["teamId"]]] += kick.get((p["type"], p.get("subType")), 0)
                if p["type"] == "FieldGoal" and p.get("subType") == "Failed" and "SINGLE" in d:
                    pts[teams[p["teamId"]]] += 1  # missed FG conceded as a single
            if p["type"] in ("Kickoff", "Punt", "FieldGoal"):
                scorer = scrape.special_teams_td(p["description"], teams)
                if scorer:
                    pts[scorer] += 6
    for r in rows:
        if r["points"]:
            other = next(t for t in pts if t != r["offense"])
            pts[r["offense"] if r["points"] > 0 else other] += abs(r["points"])
    return pts


def validate(raw, rows, teams):
    off = scrape.official_stats(raw, teams)
    comp = scrape.computed_stats(rows)
    diffs = []
    for t, o in off.items():
        c = comp.get(t, {})
        for k in ("pass_att", "pass_yds", "rush_att", "rush_yds", "sacks", "net_plays", "net_yds"):
            if c.get(k, 0) != o[k]:
                diffs.append(f"{t} {k}: plays={c.get(k, 0)} official={int(o[k])}")
    sb = raw["scoreboard"]
    final = {sb[k]["abbreviation"]: int(sb[k]["score"]) for k in ("homeTeam", "awayTeam")}
    got = team_points(raw, rows, teams)
    pdiff = [f"{t}: plays+kicks={got[t]} final={final[t]}" for t in final if got[t] != final[t]]
    off_teams = {d.split()[0] for d in diffs}
    return "; ".join(diffs) or "ok", "; ".join(pdiff) or "ok", final, off_teams


def load_game(g):
    fid = g["fixture_id"]
    raw = scrape.fetch_raw(fid)
    rows, teams, skipped = scrape.parse_plays(raw, fid)
    stat_check, points_check, final, off_teams = validate(raw, rows, teams)
    for r in rows:
        r["team_stats_off"] = int(r["offense"] in off_teams)
    return g, rows, skipped, stat_check, points_check, final


def main(ids):
    games = json.loads((ROOT / "data" / "games_2026.json").read_text())
    todo = [g for g in games if g["round"].startswith("Week") and g["status"] == "PostMatch"
            and (not ids or g["cfl_id"] in ids)]
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(load_game, todo))
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    for g, rows, skipped, stat_check, points_check, final in results:
        db.execute("DELETE FROM plays WHERE game_id=?", (g["fixture_id"],))
        db.execute("DELETE FROM skipped_plays WHERE game_id=?", (g["fixture_id"],))
        db.executemany("INSERT INTO skipped_plays VALUES (?,?,?,?,?,?,?,?)", [
            (g["fixture_id"], r["qtr"], r.get("clock"), r["offense"], r["play_type"], r["skip_reason"], r["description"], r["play_id"])
            for r in skipped])
        db.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            g["fixture_id"], g["cfl_id"], g["round"], g["start"], g["home"], g["away"], final[g["home"]], final[g["away"]],
            len(rows), len(skipped),
            sum(r.get("end_spot_matches") is False for r in rows), sum(bool(r.get("chain_gap")) for r in rows), stat_check, points_check))
        names = [c for c, _ in PLAY_COLS]
        db.executemany(
            f"INSERT INTO plays (game_id, seq, {', '.join(names)}) VALUES (?,?,{','.join('?' * len(names))})",
            [(g["fixture_id"], i, *[r.get(c) for c in names]) for i, r in enumerate(rows, 1)])
    db.commit()
    print(f"loaded {len(results)} games into {DB}")
    n = sum(len(r[1]) for r in results)
    flagged = sum(bool(x["flags"]) for r in results for x in r[1])
    print(f"{n} plays, {flagged} flagged ({flagged / max(n, 1):.1%}), {sum(len(r[2]) for r in results)} skipped")


if __name__ == "__main__":
    main({int(a) for a in sys.argv[1:]})
