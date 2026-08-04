"""Smoke test for the run-progress WebSocket. Connects, prints events, exits."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import websockets

from db import get_session
from models import Run


async def main() -> int:
    s = get_session()
    try:
        run = s.query(Run).first()
        if run is None:
            print("no runs in the database")
            return 1
        rid = run.run_id
    finally:
        s.close()

    print(f"run: {rid[:8]}")
    uri = f"ws://127.0.0.1:8000/api/v1/ws/runs/{rid}"
    async with websockets.connect(uri) as ws:
        for _ in range(15):
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            kind = msg.get("t")
            if kind == "stage":
                print(f"  stage      {msg['stage']:<18} {msg['state']:<8} {msg.get('message') or ''}")
            elif kind == "done":
                print(f"  DONE       state={msg['state']}")
                return 0
            elif kind == "heartbeat":
                print(f"  heartbeat  run state={msg['state']}")
            elif kind == "error":
                print(f"  ERROR      {msg.get('message')}")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
