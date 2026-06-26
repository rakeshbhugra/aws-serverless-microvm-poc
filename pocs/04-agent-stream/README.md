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

---

# External Redis — ElastiCache over a VPC egress connector

**This supersedes the in-VM `redis-server` above.** The stream buffer now lives in a
central **ElastiCache (Valkey)** cluster reached over a customer **VPC egress
connector**, with a **NAT gateway** giving the (now VPC-bound) MicroVM internet for the
Claude API. The in-VM `redis-server` install/startup was removed. This is the stepping
stone toward the many-VMs → one-central-cache design.

> ⚠️ Read this whole section before rebuilding — there are several non-obvious gotchas
> (dual egress, NAT, async connector updates, Valkey API quirks) that cost real time to
> rediscover.

## The data path

```
MicroVM ──VPC egress connector──┬─ (VPC local route 172.31/16) ─▶ ElastiCache :6379   (Redis, stays private)
                                └─ private subnet ─▶ NAT gateway ─▶ IGW ─▶ 🌐 api.anthropic.com  (Claude API)
```

Egress is a **single path** (see gotcha #1): everything leaves through the VPC connector.
Redis is a VPC neighbour (fast, local route); the Claude API is public, so it needs NAT.

## Runbook (command order)

```
uv run python microvm.py prereqs        # bucket + build role (unchanged)
uv run python microvm.py net-setup      # SGs, connector operator role, VPC connector, Valkey node, saves redis_host
uv run python microvm.py add-nat        # NAT gw + private subnets; repoints connector through NAT (see gotcha #3)
uv run python microvm.py package        # bakes redis_host into the image (image/redis_host, gitignored)
uv run python microvm.py build / wait-image
uv run python microvm.py run / wait / token
uv run python microvm.py probe          # in-VM check: ElastiCache PONG + curl api.anthropic.com (expect 401)
uv run python microvm.py demo-stream    # transport via ElastiCache (no token)
uv run python microvm.py agent-stream "..."   # real Claude; events buffered in ElastiCache
uv run python microvm.py clean          # VM + image only (keeps cache/connector)
uv run python microvm.py net-clean      # NAT, EIP, Valkey, connector, subnets, SGs, operator role
```

New commands vs the original: `net-setup`, `add-nat`, `probe`, `net-clean`. `run` now
attaches the VPC connector (VPC-only egress). Endpoint constants are env-overridable
(`SECRET_NAME` n/a here; `CACHE_NODE_TYPE`, `RUNTIME_ROLE_NAME`-style — see top of `microvm.py`).

## What we tested — every egress combination (same two ARN strings)

| egress connectors | launches? | internet | Redis |
|---|---|---|---|
| `[INTERNET_EGRESS]` | ✓ | ✓ (401) | ✗ |
| `[vpc_connector]` in **public** subnets, no NAT | ✓ | ✗ timeout | ✓ PONG |
| `[INTERNET_EGRESS, vpc_connector]` (both) | ✗ `InternalFailure` | — | — |
| `[vpc_connector]` in **private** subnets + NAT | ✓ | ✓ (401) | ✓ PONG |

`probe` runs the in-VM checks: a raw socket `PING` to the cache, and `curl` to
`api.anthropic.com` (401 = reachable, just unauthenticated).

## Gotchas / findings (the don't-miss list)

1. **Dual egress does not work.** Passing `INTERNET_EGRESS` + a VPC connector together
   fails with `InternalFailure` (reproducible, both orderings). Each ARN is valid alone,
   so it's the *combination*, not a bad string. Cause unconfirmed — could be a hard
   "one egress path" limit or a preview-service bug. **If it's a bug and gets fixed,
   the NAT becomes unnecessary** → worth a support ticket / CloudTrail check.
2. **IAM does not gate Redis.** Unlike S3/Secrets Manager (public API endpoints gated by
   IAM), ElastiCache is a private server gated by **VPC reachability + security group**.
   The only new IAM here is the connector **operator role** (lets Lambda create ENIs:
   `ec2:CreateNetworkInterface` + `CreateTags`) — *not* a "Redis access" policy.
3. **VPC egress costs the VM its internet → NAT required.** A VPC connector *replaces*
   the default internet path. The connector's ENIs get **no public IP**, so an internet
   gateway won't route them (even in a "public" subnet). Fix: NAT gateway in a public
   subnet + connector ENIs in **private** subnets routed `0.0.0.0/0 → NAT`. AWS docs do
   **not** mention this — it's standard VPC-Lambda behavior.
4. **MicroVMs aren't offered in every AZ.** `use1-az3` and `use1-az5` were rejected
   ("not available for compute type MicroVm"). The driver self-heals (drops a rejected AZ
   and retries) in both `net-setup` and `add-nat`.
5. **Connector subnet changes are async.** `UpdateNetworkConnector` returns while
   `State=ACTIVE` but `LastUpdateStatus=InProgress`. Launching a VM before it flips to
   `Successful` uses the OLD subnets (→ no internet). Always wait on `LastUpdateStatus`.
6. **Valkey needs `CreateReplicationGroup`,** not `CreateCacheCluster`. `NumCacheClusters=1`
   = single primary, **zero replicas**. `TransitEncryptionEnabled` is **required** — set
   `False` for the POC (SG-gated plaintext, so the redis client needs no TLS/auth).
7. **The cache endpoint is baked into the image** at `package` time (`image/redis_host`,
   gitignored, non-secret) and read by `agent-server.py`. POC4 keeps its token-in-request
   model, so **no execution role** was needed here.
8. **Confirmed-valid INTERNET_EGRESS ARN:**
   `arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:INTERNET_EGRESS`
   (launches + reaches the internet on its own).
9. **Same-VPC is enforced by construction:** the connector subnets, the cache subnet
   group, and the cache SG all come from the single default VPC; the VPC's built-in
   `172.31.0.0/16 → local` route is what makes Redis reachable.

## Cost (us-east-1, approx.)

| Item | Cost / mo | Note |
|---|---|---|
| **NAT gateway + its public IP** | **~$36.50** | always-on; biggest line; **shared by all VMs in the VPC** |
| Valkey `cache.t4g.micro` | ~$11.50 | smallest node; was `r7g.large` ~$160 before |
| connector, SGs, IAM role, subnets, subnet group | free | |
| **fixed total** | **~$48** | + NAT data ($0.045/GB), Claude tokens, VM compute |

Cheaper alternatives to the NAT *gateway*: a **NAT instance** (t4g.nano, ~$3–4/mo, you
manage it) keeps Redis on the fast path; or **tunneling** (SSH bastion / Tailscale subnet
router) lets the VM stay on internet egress and tunnel into the VPC for Redis — but that
moves overhead onto the high-frequency Redis hot path, so it's the wrong trade for streaming.

## Local-machine note

Running the driver here needs the Zscaler CA-bundle + `UV_PYTHON_DOWNLOADS=never`
workaround (corporate TLS interception). See the repo memory `zscaler-ca-bundle-workaround`.
