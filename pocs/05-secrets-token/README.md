# POC 05 — credential from Secrets Manager (VM-side fetch)

Take POC 4's live agent streaming and remove the credential from the operator's
hands entirely. The Claude credential is **no longer read from `.claude-token`**
or sent in the `POST /run` body. Instead the **MicroVM fetches it itself from AWS
Secrets Manager** at runtime, using a least-privilege runtime role.

Status: **proven end-to-end (2026-06-26).** A VM with a runtime role fetched the
Claude credential from Secrets Manager, ran Claude on a task, and streamed the edit
live — with no credential on the operator's machine or in the request. Built on
POC 4; only the credential path changed (Redis-buffered SSE streaming, `/run` +
`/stream`, and lifecycle are identical).

## Why

In POC 4 the token sat in a plaintext file on the operator's laptop and travelled
to the VM in the request body. For a multi-user tool that's the wrong model: every
user would need IAM access to the credential and the raw token would leave each
user's machine. Here, **users get permission only to run a VM** — never to read the
secret. The credential's blast radius collapses to the VM.

## How it works

```
image/
  Dockerfile        # node:22 + python venv(redis + boto3) + redis-server + claude CLI
  entrypoint.sh     # exports SECRET_NAME/region; starts redis + debug-exec(:9000) + agent-server(:9100)
  agent-server.py   # POST /run -> fetch_credential() from Secrets Manager -> claude; each event -> Redis -> SSE
  debug-exec.py     # :9000 shell channel (debugging)
workspace/          # what Claude edits (hello.py + CLAUDE.md)
microvm.py          # driver: lifecycle + demo-stream / agent-stream / shell / logs
```

Credential path: `run_microvm(executionRoleArn=...)` attaches a runtime IAM role to
the VM → boto3's default credential chain inside the VM picks up the ambient role
creds → `agent-server.py` `fetch_credential()` does `GetSecretValue` → exports it as
the right env var for `claude` (auto-detected by prefix: `sk-ant-oat…` →
`CLAUDE_CODE_OAUTH_TOKEN`, else `ANTHROPIC_API_KEY`). The operator never sees the
credential.

Event path (unchanged from POC 4): `claude --output-format stream-json` →
`agent-server` → `XADD agent:events:<run>` (Redis) → `GET /stream` `XREAD BLOCK` →
SSE out → driver prints live; reconnect/replay via `?from=<id>`.

## Prerequisites (AWS-side, one-time)

These are provisioned **outside** this driver (the driver only reads them):

- A **secret** holding the Claude credential as a plain string — either an OAuth
  token (`sk-ant-oat…`) or an Anthropic API key (`sk-ant-api…`); the VM detects which
  by prefix. Default name `microvm/claude-api-key` (override with `SECRET_NAME`).
- A **runtime role** (default `QhiveMicrovmRuntimeRole`, override with
  `RUNTIME_ROLE_NAME`) that:
  - trusts `lambda.amazonaws.com` (`sts:AssumeRole` + `sts:TagSession`), and
  - grants `secretsmanager:GetSecretValue` on that secret's ARN (`…:secret:NAME-*`).
- The **operator** running the driver needs `iam:GetRole` + `iam:PassRole` on the
  runtime role (to resolve and pass `executionRoleArn`), plus POC-4's build perms.

`uv run python microvm.py check` reports whether the secret is reachable.

## Run it

The driver now integrates POC 4's external-ElastiCache networking (no in-VM
`redis-server`), so the runbook includes the network/cache steps:

```
eval "$(aws configure export-credentials --format env)"   # boto3/SSO workaround on this box
uv run python microvm.py check                 # secret reachable? runtime role?
uv run python microvm.py prereqs               # bucket + build role + resolve runtime role
uv run python microvm.py net-setup             # SGs, connector, Valkey node, saves redis_host
uv run python microvm.py add-nat               # NAT + private subnets (needed: see below)
uv run python microvm.py package / build / wait-image   # bakes redis_host into the image
uv run python microvm.py run / wait / token    # run attaches BOTH executionRoleArn + VPC connector
uv run python microvm.py probe                 # in-VM: STS identity, Secrets Manager, ElastiCache, Claude API
uv run python microvm.py demo-stream                              # transport via ElastiCache
uv run python microvm.py agent-stream "Add a farewell(name) ..."  # real Claude, VM fetches the secret
uv run python microvm.py clean / net-clean     # teardown (net-clean stops the ~$48/mo)
```

No `.claude-token` file. No credential in any local file or request body. The Redis
buffer is external ElastiCache, reached over the VPC egress connector.

## Verify the VM can authenticate (the key new capability)

Before trusting the fetch, confirm the runtime role's creds actually reach the guest:

```
uv run python microvm.py shell "/opt/venv/bin/python -c \"import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])\""
# expect: arn:aws:sts::<acct>:assumed-role/QhiveMicrovmRuntimeRole/...
```

If that prints an assumed-role ARN, the Secrets Manager fetch will work.

## Debug / teardown
```
uv run python microvm.py shell "tail -n 40 /var/log/agent-server.log"
uv run python microvm.py logs
uv run python microvm.py clean
```

## Findings (verified live, 2026-06-26)

- **MicroVMs can assume a runtime IAM role** via `RunMicrovm`'s `executionRoleArn`.
  Confirmed: `boto3 sts.get_caller_identity()` *inside* the VM returned
  `assumed-role/QhiveMicrovmRuntimeRole/...`. Notably the guest has **no
  `AWS_ACCESS_KEY`/`SESSION` env vars** — AWS delivers the creds via a metadata
  endpoint that boto3's default chain finds automatically. No extra wiring needed.
- **The credential never touches the operator.** `agent-stream` posts only
  `{"task": ...}`; the VM did the `GetSecretValue` itself. Users need only "run a VM"
  + `iam:PassRole` on the runtime role — nothing on Secrets Manager. This is the
  multi-tenant-safe shape POC 4's file-based token could not provide.
- **The `claude` event stream reported `"apiKeySource":"ANTHROPIC_API_KEY"`** (then
  `CLAUDE_CODE_OAUTH_TOKEN` after the fix below) — direct proof the credential came
  from the in-VM env set by the secret fetch, not from the request.
- **Don't trust the secret's name for its type.** `microvm/claude-api-key` is named
  "api-key" but actually holds an **OAuth token** (`sk-ant-oat…`). Exporting it as
  `ANTHROPIC_API_KEY` gave HTTP 401 "Invalid API key"; the fix was to **auto-detect
  by prefix** (`credential_env` in `agent-server.py`) — `sk-ant-oat…` →
  `CLAUDE_CODE_OAUTH_TOKEN`, else `ANTHROPIC_API_KEY`. Works for either type now.
- **Fetched per-run, never cached** → rotating the secret takes effect on the next
  run with no rebuild and no VM restart. The secret *name* is non-secret config
  (env default); only the *value* is protected.
- On this machine, runs need the Zscaler CA bundle + `UV_PYTHON_DOWNLOADS=never`
  workaround (SSL interception); unrelated to the POC.

---

## Carry-over: external ElastiCache over a VPC egress connector

POC 5 **now composes** the networking work from **POC 4** (external ElastiCache instead
of in-VM Redis) — the driver and image carry the `net-setup` / `add-nat` / `probe` /
`net-clean` commands, drop the local `redis-server`, and point `agent-server.py` at the
baked ElastiCache endpoint. The full runbook, the egress-combination test matrix, and the
resource/cost breakdown live in **`pocs/04-agent-stream/README.md` → "External Redis —
ElastiCache over a VPC egress connector"** — read it for the gotchas. What's wired in
here: a `lambda-core` **VPC egress connector** (subnets + SG + operator role), a **Valkey**
node via `CreateReplicationGroup` (`NumCacheClusters=1`, `TransitEncryptionEnabled=False`
for the POC), and a **NAT gateway** because the VPC connector replaces the VM's default
internet path.

> **Not yet live-verified.** These are code-only changes mirrored from POC 4 (proven
> there); the combined POC 5 path — runtime role + VPC egress + Secrets-Manager-via-NAT —
> hasn't been run end-to-end yet (infra was torn down to stop billing). Provision with the
> runbook above and use `probe` to verify before trusting it.

### ⚠️ POC 5 + VPC egress: Secrets Manager also goes through the VPC

This is the combine-point that bites: POC 5's whole model is the VM calling
`secretsmanager:GetSecretValue` itself. Today that works because the VM has **default
internet egress** and reaches the public Secrets Manager endpoint. **The moment you add a
VPC egress connector (for ElastiCache), egress becomes VPC-only** — and Secrets Manager,
STS, etc. are reached over the *public internet* too, so those calls now need either:

- a **NAT gateway** (what POC 4 uses — covers Secrets Manager *and* the Claude API), **or**
- a **Secrets Manager VPC interface endpoint** (PrivateLink) — keeps the secret fetch
  fully private, no NAT needed *for that call*; but the Claude API (`api.anthropic.com`,
  not an AWS service) still needs NAT. So with VPC egress you almost always still need NAT
  for Claude; a PrivateLink endpoint is only worth it to keep the secret traffic off NAT.

What does **not** break: the **executionRole credential delivery** (the IMDS-style
metadata endpoint) is link-local and works regardless of egress — so the VM keeps its
identity; it's only the outbound *API calls* to AWS services that route through the VPC.

### Combined runbook sketch (POC 5 ⊕ POC 4 networking)

`prereqs` → `net-setup` (connector + Valkey) → `add-nat` → `package`/`build`/`run` with
**both** `executionRoleArn` (POC 5, for the secret) **and** the VPC egress connector
(POC 4, for the cache). Verify in this order: (a) `sts get-caller-identity` in-VM
(identity), (b) `secretsmanager get-secret-value` in-VM (needs NAT or PrivateLink),
(c) ElastiCache `PING` (VPC local route), (d) `curl api.anthropic.com` (needs NAT).
