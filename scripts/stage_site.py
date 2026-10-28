"""Allowlist public files into _site; admin tools, tests and workflows are never published."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = ["index.html", "opportunities/index.html", "assets/osp.css", "assets/opportunities.js",
                "robots.txt", ".nojekyll", "data/grants.json"]

stage = ROOT / "_site"
if stage.exists():
    shutil.rmtree(stage)
for name in PUBLIC_FILES:
    dest = stage / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / name, dest)
print(f"Staged {len(PUBLIC_FILES)} public files.")
