"""Write web/plays.json so the explorer works as a static site (Netlify). Run after load_db.py."""
import json

from serve import ROOT, load_plays

if __name__ == "__main__":
    rows = load_plays()
    out = ROOT / "web" / "plays.json"
    out.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    print(f"{len(rows)} plays -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
