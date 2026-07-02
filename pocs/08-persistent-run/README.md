# POC 08 — persisting a Claude run across MicroVM lifecycles (S3-backed)

An agent works a ticket inside a MicroVM and replies. The user doesn't respond within the
VM's **8-hour hard lifetime**. The next day they reopen the ticket, a **fresh MicroVM**
spins up, and it **rehydrates that run's filesystem from S3** — so the agent continues with
full context, as if it never left.

Status: **proven end-to-end (2026-07-02).** Chat-only fork of POC 7 (GitHub broker removed).
Adds deterministic session ids + run-keyed S3 snapshot/restore driven by MicroVM lifecycle
hooks. The multi-cycle flush test passed: one run (sid `073c2e81…`) recorded fact A, then
`/suspend`→resume→B→`/suspend`→resume→C→`/terminate` (S3 `state.tgz` timestamps monotonic
T1<T2<T3, sizes 6260→6576→6849 as facts accumulated); a **brand-new VM** restored via its
`/run` hook (`restored: True, bytes 6849`) and Claude answered `A: 42, B: 7, C: 99` — proving
each suspend *and* terminate flushed, and a fresh VM resumes cumulative state.

## The core idea

Two facts make this necessary and possible:
- **A MicroVM lives ≤8h, non-resettable** (running+suspended time both count; terminate wipes
  local disk). Suspend/resume preserves state on the *same* VM, but nothing survives terminate
  or the cap. There is **no snapshot-a-running-VM API** — durability must be app-level.
- **`claude --session-id <uuid>` / `--resume <uuid>`** gives a **deterministic, caller-chosen**
  session id. The transcript is a file on disk (`/home/node/.claude/projects/-workspace/<uuid>.jsonl`).

So: persist that file (and the rest of the run's dirs) to S3, keyed by the run id, and restore
it into whatever VM currently hosts the run.

## One id per run

The caller passes a UUID **`SID`** (via `SID=…` env). It is simultaneously:
- Claude's session id — `--session-id SID` the first time, `--resume SID` after; and
- the S3 key prefix — `s3://<bucket>/microvm-runs/<SID>/state.tgz`.

The agent-server picks `--resume` vs `--session-id` by whether the transcript file exists.

## How it works

```
image/
  Dockerfile        # node:22 + venv(boto3) + claude CLI (no gh/git/PyJWT)
  entrypoint.sh     # exports SECRET_NAME, region, SNAPSHOT_* , HOOKS_PORT; starts 3 servers
  agent-server.py   # :9100  /run{task,sid} -> claude --session-id/--resume ; /stream (SSE) ; /snapshot ; /restore
  hooks.py          # :HOOKS_PORT  /aws/lambda-microvms/runtime/v1/{run,resume,suspend,terminate}
  persist.py        # shared: tar SNAPSHOT_PATHS <-> s3://<bucket>/microvm-runs/<sid>/state.tgz
  debug-exec.py     # :9000 shell channel (debug + verification)
workspace/CLAUDE.md # tells Claude this is a persistent, resumable run
microvm.py          # driver: run(SID payload) / agent / chat / snapshot / restore / suspend / resume / …
```

**Lifecycle hooks** (declared at image build via `hooks={port, microvmHooks:{run,suspend,
terminate = ENABLED}}`; Lambda POSTs to them *inside* the guest):
- **`/run`** — reads `runHookPayload={"sid":…}`, `restore(sid)` from S3, returns 200. Because
  traffic only flows after `/run` returns 200, the FS is hydrated before the first message.
- **`/suspend`** and **`/terminate`** — `snapshot(sid)` to S3 (flush before the VM goes away;
  `/terminate` covers the 8h cap). Each runtime hook must finish in **≤60s**, so `SNAPSHOT_PATHS`
  defaults to just `.claude/projects` (small); `/workspace` is opt-in with a size guard.

**Two-day flow:**
```
Day 1:  run(sid) --/run--> S3 miss (fresh) ; claude --session-id sid ; …work… ;
        suspend --/suspend--> flush to S3 ; terminate --/terminate--> final flush ; VM gone
Day 2:  run(sid) --/run--> S3 hit -> restore ; claude --resume sid -> full prior context
```

**Reliability:** the same `persist.snapshot/restore` is also exposed as driver-triggered
`POST /snapshot` / `POST /restore` (`microvm.py snapshot|restore`) — no 60s limit, the safety net
if a hook misbehaves or state is large.

## Prerequisites (operator-provided)
- POC 5's `microvm/claude-api-key` + `QhiveMicrovmRuntimeRole`.
- Grant the runtime role **`s3:GetObject`+`s3:PutObject`** (and `s3:ListBucket` on the prefix) on
  `arn:aws:s3:::ryu-microvm-poc-<acct>/microvm-runs/*`. The driver only resolves the role (POC 5/7
  convention); this grant is operator-added, like the Secrets-Manager grant.

## Run it (the two-day scenario)
```
# Zscaler: UV_SYSTEM_CERTS=true, UV_PYTHON_DOWNLOADS=never, SSL_CERT_FILE=<keychain bundle>
export SID=$(uuidgen)
uv run python microvm.py check / prereqs / package / build / wait-image
SID=$SID uv run python microvm.py run / wait / token     # /run hook: S3 miss -> fresh
SID=$SID uv run python microvm.py chat                   # claude --session-id $SID
uv run python microvm.py suspend                         # /suspend hook -> state.tgz to S3
uv run python microvm.py clean                           # terminate; local disk gone; S3 keeps it
# --- next day ---
SID=$SID uv run python microvm.py run / wait / token     # NEW VM; /run hook -> restore
SID=$SID uv run python microvm.py chat                   # claude --resume $SID; full context
```
`agent "<task>"` is the one-shot equivalent of a single chat turn.

## Verification

**1. Deterministic session.** `SID=<uuid> agent "remember X=42"`; `shell` shows the transcript
`…/-workspace/<uuid>.jsonl`; `SID=<uuid> agent "what is X"` → 42.

**2. Multi-cycle flush test (headline — proves flush on *every* suspend and on terminate).**
Interleave Claude work with the lifecycle, accumulating facts, then confirm all survived in a new VM:
```
VM #1:
  SID=$SID agent "Remember fact A: the sky code is 42."
  suspend                       # /suspend -> flush A (note state.tgz timestamp T1)
  resume
  SID=$SID agent "Remember fact B: the sea code is 7. What was fact A?"   # recalls A, adds B
  suspend                       # /suspend -> flush A+B (T2 > T1)
  resume
  SID=$SID agent "Remember fact C: the sun code is 99."
  terminate                     # /terminate -> final flush A+B+C (T3 > T2)
VM #2 (fresh, same SID):
  run / wait / token            # /run -> restore
  SID=$SID agent "List every fact and code you remember."   # MUST return A=42, B=7, C=99
```
A missing fact pinpoints the lost flush (e.g. C missing ⇒ `/terminate` didn't finish in 60s → use
driver `snapshot` before terminate). Monotonic timestamps prove each suspend re-flushed.

**3. Hook (not driver) did it.** `shell "cat /var/log/hooks.log"` shows `/run`, `/suspend`,
`/terminate` handled with restore/snapshot results — proving Lambda invoked the in-guest hooks.

**4. Restore within budget.** Time the `/run` restore (in `hooks.log`); confirm ≪60s for the
`.claude/projects`-only snapshot.

## Notes / caveats
- **60s runtime-hook cap** — keep snapshots lean; `/workspace` opt-in with `SNAPSHOT_MAX_MIB` guard;
  driver `snapshot`/`restore` has no limit.
- **8h cap is absolute** — durability = restore into a fresh VM, never extending one.
- **One live VM per SID** — last-writer-wins on `state.tgz`; don't run two VMs for one run at once.
