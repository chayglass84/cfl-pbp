"""Serve the CFL play explorer: python serve.py [port]   ->  http://localhost:8765

Reads data/cfl.db, sends every analysable play to the page once (/plays.json); filtering, grouping and
summaries all happen in the browser.
"""
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

QUERY = """
SELECT p.game_id, g.cfl_id, g.round, p.qtr, p.clock, p.offense,
       CASE WHEN p.offense = g.home THEN g.away ELSE g.home END AS defense,
       p.down, p.distance, p.yardline, p.own_yd, p.play_type, p.qb, p.ballcarrier, p.bc_pos,
       p.yards, p.result, p.points, p.first_down, p.flags, p.team_stats_off, p.description
FROM plays p JOIN games g USING (game_id)
WHERE p.play_type IN ('Run', 'Pass', 'Sack', 'Fumbled snap')   -- real scrimmage plays: no kneels, 2-pt tries, penalty rows
  AND p.result != 'No play (penalty)' AND p.down IS NOT NULL AND p.distance IS NOT NULL AND p.yards IS NOT NULL
ORDER BY g.cfl_id, p.seq
"""
TURNOVERS = ("Interception", "Fumble lost", "Turnover on downs")


def load_plays():
    db = sqlite3.connect(ROOT / "data" / "cfl.db")
    db.row_factory = sqlite3.Row
    rows = []
    for r in db.execute(QUERY):
        d = dict(r)
        # success = the offence kept the ball AND got a first down (or scored): first-down flag, TD, or gain >= distance
        d["success"] = int(d["result"] not in TURNOVERS and (
            d["first_down"] or d["result"] == "Touchdown" and d["points"] > 0 or d["yards"] >= d["distance"]))
        rows.append(d)
    return rows


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/plays.json":
            body, ctype = json.dumps(load_plays()).encode(), "application/json"
        elif self.path.split("?")[0] in ("/", "/index.html"):
            body, ctype = (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"CFL play explorer on http://localhost:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
