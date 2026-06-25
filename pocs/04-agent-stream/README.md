# POC 04 — live agent streaming (Redis-buffered SSE)

Stream `claude -p`'s `stream-json` events **out of the VM in real time** — the
chat-pane half of ryureflect's loop. POC 3 proved Claude can *do* work; this
proves we can *watch it happen live*.

Status: **transport proven.** An in-VM `agent-server` runs the producer, buffers
every event into a **Redis Stream**, and relays it over **SSE**; a consumer sees
events one-by-one as they happen, and can reconnect/replay via `from=<id>`. The
real Claude producer (`agent-stream`) is wired and ready (needs your token).

## How it works

```
image/
  Dockerfile        # light: node:22 + python venv(redis) + redis-server + claude CLI
  entrypoint.sh     # starts redis + debug-exec(:9000) + agent-server(:9100)
  agent-server.py   # POST /run -> claude (or demo); each event -> Redis Stream; GET /stream -> SSE
  debug-exec.py     # :9000 shell channel (debugging)
workspace/          # what Claude edits (hello.py + CLAUDE.md)
microvm.py          # driver: lifecycle + demo-stream / agent-stream / shell / logs
```

Event path: `claude -p --output-format stream-json` → `agent-server` reads each
line → `XADD agent:events` (Redis Stream) → `GET /stream` does `XREAD BLOCK` →
SSE out the MicroVM endpoint → driver prints live. Redis is the buffer, so
`GET /stream?from=<id>` (or `Last-Event-ID`) replays for a reconnecting consumer.

## Run it

```
eval "$(aws configure export-credentials --format env)"   # boto3/SSO workaround on this box
uv run python microvm.py check / prereqs / package / build / wait-image
uv run python microvm.py run / wait / token

# prove the transport — canned producer, NO token needed:
uv run python microvm.py demo-stream
#   start: demo
#   {"event":"thinking","step":1} ... step 5
#   done (exit 0)

# the real thing — Claude streaming (needs your token):
echo 'sk-ant-oat...' > .claude-token          # gitignored; sent at runtime, never baked
uv run python microvm.py agent-stream "Add a farewell(name) function to hello.py and run it."
```

## Debug / teardown
```
uv run python microvm.py shell "tail -n 40 /var/log/agent-server.log"
uv run python microvm.py logs
uv run python microvm.py clean                 # terminate VM + delete image
```

## Findings

- **Live streaming out of a MicroVM works** over SSE on the ingress endpoint —
  no extra infra, just another port (`:9100`) on the same authenticated channel.
- **Redis Stream is a clean buffer**: producer `XADD`s, consumer `XREAD BLOCK`s;
  reconnect with `from=<id>` replays. This is the durability/fan-out primitive
  that POC 5 promotes to a **central ElastiCache** Redis over a VPC egress
  connector (so many VMs publish to one store a control plane subscribes to).
- **Token handling** mirrors POC 3: read from `.claude-token`, sent in the
  `POST /run` body at runtime, never written into the image.
- The driver's SSE consumer is plain `urllib` line-iteration — proof that any
  client (browser EventSource, control plane) can subscribe the same way.
