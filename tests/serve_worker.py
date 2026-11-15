"""Launch one uvicorn worker that exits when its parent closes stdin.

The test harness runs two of these against one database file.  Waiting for
stdin EOF instead of relying on signals keeps teardown working in sandboxes
where a parent cannot signal its children.
"""

from __future__ import annotations

import os
import sys
import threading

import uvicorn

from app.main import app


def _watch_parent() -> None:
    try:
        sys.stdin.buffer.read()
    finally:
        os._exit(0)


def main() -> None:
    port = int(sys.argv[1])
    threading.Thread(target=_watch_parent, daemon=True).start()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
