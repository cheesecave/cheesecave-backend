#!/usr/bin/env python3
"""Run the API on SQLite without LakeFS, S3 or Valkey, for prototypes that only touch the database.

usage: CHEESE_CAVE_DB_BACKEND=sqlite CHEESE_CAVE_DATABASE_URL=sqlite:////tmp/x/hub.db PYTHONPATH=src \\
       python scripts/dev/run_without_infra.py [port]
Anything that needs file contents (trees, downloads, uploads) fails, as it has nothing to read.
"""
import sys

import uvicorn

import kohakuhub.main as hub

hub.init_storage = lambda: None  # the bucket check at startup is the only thing that needs S3 to be up

if __name__ == "__main__":
    uvicorn.run(hub.app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 48920, log_level="warning")
