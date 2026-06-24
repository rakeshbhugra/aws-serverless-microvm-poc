"""FastAPI backend whose hit counter lives in Redis.

Keeping the counter in Redis (not in-process like POC 01) means it's shared
state across requests AND, once this whole stack is snapshotted, it will survive
suspend/resume — Redis's data sits in the VM's memory/disk that the snapshot
captures. That's the behaviour the later baked-snapshot track will lean on.
"""

import os
import socket

import redis
from fastapi import FastAPI

app = FastAPI()
r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379, decode_responses=True)


@app.get("/")
def root():
    count = r.incr("hits")  # atomic increment in Redis
    return {"status": "ok", "count": count, "served_by": socket.gethostname()}


@app.get("/health")
def health():
    r.ping()  # raises if Redis isn't reachable -> 500
    return {"ok": True}
