#!/usr/bin/env python3
"""put_asset.py REPO TAG FILE [NAME] -- append-only release upload for the shell scripts.

Never replaces an existing asset (no --clobber): an identical asset already under NAME is
adopted, anything else under NAME makes the upload go to NAME.r2, .r3, ... Verifies size and
GitHub's sha256 digest. Prints the asset name that holds the bytes. Uses GH_TOKEN as given."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export as E  # noqa: E402

repo, tag, f = sys.argv[1:4]
path = Path(f)
name = sys.argv[4] if len(sys.argv) > 4 else path.name
E.WRITE_ENV = None  # the caller's GH_TOKEN is already the right one for REPO
print(E._upload_asset(tag, path, name, E.sha256_file(path), repo=repo))
