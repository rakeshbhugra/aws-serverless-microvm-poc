"""Counter backend — count lives in Redis. The frontend button hits /increment."""

import os

import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"), port=6379, decode_responses=True)


@app.get("/count")
def get_count():
    return {"count": int(r.get("count") or 0)}


@app.post("/increment")
def increment():
    return {"count": r.incr("count")}
