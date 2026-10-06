# cfl-pbp
CFL play by play data explorer for 2026

Scrapes offensive plays (no kicks/punts/FGs) from the Genius Sports feed behind cfl.ca game pages into a local
SQLite DB, with a small web UI for filtering, grouping and success-rate summaries. 2026 season only.

## Usage
```bash
python discover.py      # find completed 2026 games      -> data/games_2026.json
python load_db.py       # scrape + parse + load           -> data/cfl.db (raw responses cached in data/raw/)
python serve.py         # explorer at http://localhost:8765
python scrape.py <cfl.ca game URL | fixtureId>   # one game -> filterable report_<id>.html with validation
```
Python 3 standard library only.

## Data quality
Each game is checked against the feed's official team stats and the final score (`games.stat_check`,
`games.points_check`). Plays that parse but look off carry a `flags` value (`gap_before`, `spot_mismatch`, ...);
plays that can't be categorised at all go to `skipped_plays`. `data/` is not committed — rebuild it with the commands above.
