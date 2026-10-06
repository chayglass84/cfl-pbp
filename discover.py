"""List completed 2026 CFL games by walking cfl.ca game ids. Writes data/games_2026.json."""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scrape import BASE, CUSTOMER, ROOT, get


def probe(cfl_id):
    try:
        m = re.search(r"fixtureId=(\d+)", get(f"https://cfl.ca/games/{cfl_id}/x/"))
        if not m:
            return None
        fid = m.group(1)
        d = json.loads(get(f"{BASE}/multisportscoreboardwidget/customer/{CUSTOMER}/locale/en-US/geolocale/CA-MB?fixtureId={fid}"))["data"]["details"]
        ms = d["matchSummaries"]
        return {"cfl_id": cfl_id, "fixture_id": fid, "season": d["seasonName"], "round": d["roundName"],
                "status": ms["matchStatus"], "start": d["scheduledStartTime"],
                "home": d["homeTeam"]["abbreviation"], "away": d["awayTeam"]["abbreviation"],
                "home_score": d["homeTeam"]["score"], "away_score": d["awayTeam"]["score"]}
    except Exception as e:  # unknown / not-yet-published ids
        return {"cfl_id": cfl_id, "error": str(e)[:80]}


if __name__ == "__main__":
    lo, hi = (int(x) for x in sys.argv[1:3]) if len(sys.argv) > 2 else (6540, 6700)
    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(probe, range(lo, hi + 1)) if r]
    ok = [r for r in res if "error" not in r]
    print(f"{len(ok)} probed ok, {len(res) - len(ok)} errors")
    games = sorted((r for r in ok if r["season"].startswith("2026")), key=lambda r: r["cfl_id"])
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "games_2026.json").write_text(json.dumps(games, indent=1))
    for r in games:
        print(r["cfl_id"], r["fixture_id"], r["round"], r["status"], r["start"][:10], r["away"], r["away_score"], "@", r["home"], r["home_score"])
