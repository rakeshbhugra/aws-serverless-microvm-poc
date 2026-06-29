# POC 06 — Claude builds & hosts a full-stack app inside a MicroVM, viewable in a browser

**What it proves:** an AI agent (Claude Code) running *inside* an AWS Lambda MicroVM can
**scaffold a fresh full-stack app, `docker build` it from source in the VM, run it, and expose
it to a normal browser** over a public tunnel — with the Claude credential fetched from Secrets
Manager by the VM itself (never handled by the operator).

It composes the earlier POCs: **02** (Docker-in-MicroVM) · **04** (event streaming) · **05**
(Secrets-Manager credential via `executionRoleArn`) · **NEW** here: in-VM `docker build` from
source + a `cloudflared` tunnel for the browser view.

```
operator ── run-microvm (executionRoleArn) ──▶ MicroVM
                                                 ├─ agent-server :9100  (fetches secret, runs Claude, streams)
                                                 ├─ dockerd            (builds + runs the app)
                                                 ├─ the app            (Claude-built; served on a port)
                                                 └─ cloudflared        ──▶ https://<random>.trycloudflare.com ──▶ your browser
secret: Secrets Manager (microvm/claude-api-key)   ·   Claude API + registries + tunnel = INTERNET_EGRESS
```

## The headline finding — in-VM `docker build` works

POC 2 punted on building images in the VM because the **default BuildKit can't resolve DNS**
(it ignores the VM's `127.0.0.2` stub resolver). POC 6's first milestone (the **build gate**)
cracked it. Verified live:

> **`DOCKER_BUILDKIT=0 docker build --network=host …` succeeds** — the **legacy builder** runs
> each `RUN` step as a normal container that honors `daemon.json` DNS + host networking, so
> `RUN pip install` (`PIP_OK`) and `RUN npm install` (`NPM_OK`) resolve and build in-VM.

So the whole premise holds: Claude can clone/scaffold and build real images from source inside
the MicroVM. The `build-gate` command tries approaches in order and stops at the first success;
the winner (`a: legacy builder + --network=host`) is recorded in state and used for the app build.

## Milestones (built incrementally; each independently verified)

| | What | Result |
|---|---|---|
| **M0** | In-VM `docker build` gate | ✅ passes via `DOCKER_BUILDKIT=0 --network=host` |
| **M1** | POC-5 base: `executionRoleArn` → VM fetches secret → Claude runs, streamed | ✅ `assumed-role/QhiveMicrovmRuntimeRole`; Claude ran, `exit 0` |
| **M2** | Claude scaffolds a **single-port** app (FastAPI serves UI + `/api`) + Postgres, builds + runs it | ✅ same-origin `/api`, data persists |
| **M2b** | Claude scaffolds a **two-port** app (React+nginx `:3000`, FastAPI `:8000`) + **two** tunnels + CORS | ✅ cross-origin works |
| **M3** | `cloudflared` tunnel → open the app in a real browser, no token | ✅ public URL serves the app |
| **M4** | Central ElastiCache + control-plane dashboard over VPC+NAT | ⏭️ **not built** (deliberately skipped) |

## Single-port vs two-port (we built both — the contrast matters)

| | **M2 single-port** | **M2b two-port** |
|---|---|---|
| Browser-facing ports | 1 (`:8000`, FastAPI serves UI **and** `/api`) | 2 (`:3000` frontend, `:8000` backend) |
| Frontend server | FastAPI `StaticFiles` (no nginx) | **nginx** serves the built React assets |
| Frontend → API | relative `/api` (same origin) | absolute base, **injected at runtime** into `config.js` |
| CORS / preflight | none | **required** (`allow_origins=["*"]`, browser sends `OPTIONS` preflight) |
| Tunnels | 1 | **2** (one per port) — must gate **both** if locking down |

**Why nginx appears in the two-port version:** React compiles to static files; *something* must
serve them. Single-port lets FastAPI do it (no nginx); two-port gives the frontend its own
server → `nginx:alpine`. (A Next.js app would replace nginx with its own Node server and, via API
routes or a `/api` rewrite-proxy, collapse back to single-origin — no nginx, no CORS.)

**Dynamic-URL injection (two-port):** the frontend can't bake the backend's tunnel URL at build
time (it's a random `*.trycloudflare.com`, known only after the tunnel starts) and can't use
`localhost` (that's the viewer's machine). Solution: `index.html` loads `/config.js` (which sets
`window.API_BASE`) *before* the bundle; the driver writes the real backend URL into `config.js`
**after** starting the backend tunnel. The same-origin single-port design avoids this entirely.

## Exposing it to a browser — the tunnel

The MicroVM ingress endpoint **rejects any request without the `X-aws-proxy-auth`/`X-aws-proxy-port`
headers**, which browsers can't add — so the AWS endpoint can't be opened directly. Instead a
**`cloudflared` quick tunnel** started *inside* the VM dials out and returns a public
`*.trycloudflare.com` URL with **no auth header**:

- Install at runtime: the VM **host** has working DNS/egress, so
  `curl … cloudflared-linux-arm64` downloads fine.
- **`--protocol http2`** is needed: QUIC/UDP 7844 is blocked, so cloudflared falls back to
  HTTP/2 over TCP 443 (it does this automatically; we force it to skip the QUIC probe).
- The URL is **ephemeral** (tied to the running VM + the `cloudflared` process) and **public**.

## Authorizing the tunnel (who can open it)

Quick tunnels are public by design. To restrict access:
- **App-level Basic Auth** (nginx `auth_basic` / a FastAPI dependency) — shared password, works
  on the quick tunnel now; in two-port you must gate **both** tunnels.
- **Cloudflare Access** (SSO / email allow-list, enforced at the edge) — needs a **named** tunnel
  on a domain in a Cloudflare account (store the tunnel credential in Secrets Manager).
- **ngrok `--oauth`/`--basic-auth`** — gating built into the tunnel, least setup for per-email auth.
- **Tailscale `serve`** — reachable only by your tailnet members.

## Inherited MicroVM facts (from POC 2/5)
- **Docker hook:** `additionalOsCapabilities=["ALL"]` on `create/update_microvm_image`; ~4 GB RAM.
- Nested containers must use `daemon.json {"dns":["127.0.0.2"]}` + `network_mode: host`
  (the VM blocks bridge/public DNS; `127.0.0.2` is loopback so host networking is required).
- The VM **host** resolves DNS fine (image pulls, cloudflared download work); only nested-container
  and **BuildKit** DNS is constrained — hence the legacy builder.
- **Credential (POC 5):** `run_microvm(executionRoleArn=QhiveMicrovmRuntimeRole)`; in-VM boto3 finds
  creds via the IMDS metadata endpoint; `agent-server` reads `microvm/claude-api-key` from Secrets
  Manager and exports `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` (prefix auto-detect). Claude runs
  as a non-root `agent` user (in the `docker` group; it refuses `--dangerously-skip-permissions` as root).
- **Image builds run in AWS** (normal DNS) — so the *image* Dockerfile can freely `dnf`/`npm`/`pip`;
  the `127.0.0.2` constraint only bites the in-VM *runtime* (nested docker).

## Files
```
microvm.py            # driver: lifecycle + build-gate + agent (run+stream) + url + shell/logs
stack/
  Dockerfile          # al2023 + docker + compose + buildx + Node + Claude CLI + venv(redis,boto3) + agent user
  entrypoint.sh       # debug-exec(:9000) + dockerd + redis(container) + agent-server(:9100), then idle
  agent-server.py     # POST /run -> fetch secret -> claude (stream-json) -> per-run Redis stream -> GET /stream SSE
  debug-exec.py       # :9000 shell channel (drives docker/build/compose in-VM)
  daemon.json         # {"dns":["127.0.0.2"]}
  buildgate/Dockerfile.{pip,npm}   # M0 gate: network RUN steps
```

## Run it
```
eval "$(aws configure export-credentials --format env)"     # creds (SSO workaround on this box)
uv run python microvm.py check / prereqs                     # bucket + build role + resolve runtime role
uv run python microvm.py package / build / wait-image        # build the image (ALL caps)
uv run python microvm.py run / wait / token                  # launch (executionRoleArn), mint token

uv run python microvm.py build-gate                          # M0: prove in-VM docker build (do first)
uv run python microvm.py agent "scaffold + build + run a full-stack app …"   # M2/M2b: Claude builds it
uv run python microvm.py shell "docker ps"                   # inspect in-VM
# tunnel (M3): install + run cloudflared in-VM, then open the printed *.trycloudflare.com URL
uv run python microvm.py url                                 # surface the tunnel URL
uv run python microvm.py clean                               # terminate VM + delete image
```

## Findings (verified live, 2026-06-29)
1. **In-VM `docker build` works** with the legacy builder + host net (the POC's premise). BuildKit
   still can't resolve DNS — don't use it for builds here.
2. **Claude builds either topology in-VM** — single-port (no nginx, no CORS) or two-port (nginx +
   CORS + runtime `config.js` injection). Single-origin is simpler; prefer it unless you need split servers.
3. **cloudflared quick tunnel = browser access with no auth header**, `--protocol http2` (UDP blocked),
   ephemeral URL, and it sidesteps the port-heterogeneity problem (the tunnel targets whatever port
   the app uses, and Claude — having built it — knows the port).
4. **Credential never touches the operator** (POC 5): the VM fetches it from Secrets Manager via the
   runtime role. The driver sends only the task.
5. **Idle policy still suspends the VM** (~15 min no ingress traffic), which drops the tunnel/app —
   bump the idle policy for a long-lived demo, or re-run.

## Not built: M4 (central ElastiCache + dashboard)
The plan's final milestone — many build-VMs publish their Claude streams to a **central
ElastiCache** that a **control-plane dashboard** aggregates — was deliberately skipped. It would
flip egress to a **VPC connector + NAT** (the POC 4-ElastiCache networking) so the VM can reach the
VPC-private cache; Secrets Manager + Claude API + tunnel would then route via the NAT. See the
approved plan for the design.

## Teardown / cost
`clean` terminates the VM + deletes the image (the S3 bucket + IAM build role are shared across
POCs, left in place). While running, you pay MicroVM compute + Claude API tokens; the cloudflared
quick tunnel and Secrets Manager read are free. No ElastiCache/NAT here (M4 skipped), so nothing
else accrues.
