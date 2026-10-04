#!/usr/bin/env python3
"""Upload an already-encrypted log to the private archive repo's runner-logs-YYYYMMDD release."""
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export as E  # noqa: E402
os.environ["GH_TOKEN"] = os.environ.get("ARCHIVE_WRITE_TOKEN") or os.environ.get("GH_TOKEN", "")
p = Path(sys.argv[1])
tag = E._ensure_release("runner-logs")
subprocess.run(["gh", "release", "upload", tag, str(p), "--repo", E.RELEASE_REPO, "--clobber"],
               check=True, capture_output=True, timeout=300)
p.unlink(missing_ok=True)
