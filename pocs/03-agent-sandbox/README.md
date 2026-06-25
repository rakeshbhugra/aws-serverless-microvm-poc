# POC 03 — agentic dev sandbox in a MicroVM

ryureflect's core loop, in a box: an isolated MicroVM where **Claude Code edits a
real app and visually verifies its work with a Playwright screenshot.** Merges
the "heavy tooling (Playwright)" and "Claude-in-VM" tracks into one demo.

Status: **Playwright path proven.** App runs flattened (no Docker), headless
Chromium screenshots the frontend on arm64, and the PNG round-trips out for
inspection. The Claude-driven step is wired and ready (needs your token).

## What's inside

```
workspace/                 # what the agent edits — baked to /workspace
  backend/app.py           # FastAPI counter (Redis), :8000
  frontend/index.html      # the button (blue), :3000
  docker-compose.yml       # the "Claude could use it" option
  CLAUDE.md                # how to run the app + the MicroVM quirks
image/                     # MicroVM image build context (flattened Debian, no Docker)
  Dockerfile               # node:22 + python venv + redis + playwright/chromium + claude CLI
  entrypoint.sh            # starts debug-exec, stays idle
  debug-exec.py            # :9000 control endpoint
  app-up.sh                # start redis+backend+frontend directly
  shot.cjs                 # playwright chromium -> screenshot (--no-sandbox)
microvm.py                 # driver: lifecycle + app-up / shot / pull / agent
```

The VM boots **idle** — it does not auto-run anything. We hand it files + tools
and drive it; the agent (or you) decides how to run the app.

## Credentials note (this box)

boto3 can't read the AWS CLI's SSO token cache here, so prefix AWS-touching
commands with a credential export (the CLI refreshes SSO for us):

```
eval "$(aws configure export-credentials --format env)"
```

## Run the Playwright proof

```
eval "$(aws configure export-credentials --format env)"
uv run python microvm.py check
uv run python microvm.py prereqs        # bucket + role (no-op if they exist)
uv run python microvm.py package        # zip image/ + workspace/ -> S3
uv run python microvm.py build          # heavy: node+python+redis+playwright+chromium+claude
uv run python microvm.py wait-image
uv run python microvm.py run
uv run python microvm.py wait
uv run python microvm.py token
uv run python microvm.py app-up         # start redis + backend + frontend in the VM
uv run python microvm.py shot           # playwright -> /workspace/screenshots/button.png
uv run python microvm.py pull /workspace/screenshots/button.png ./button.png
xdg-open ./button.png                    # the screenshot (gitignored, local only)
```

## Run the agent demo (the payoff — needs your token)

```
echo 'sk-ant-oat...' > .claude-token      # gitignored; never baked into the image
uv run python microvm.py agent "Change the button color to green, run the app, \
  and screenshot the frontend to /workspace/screenshots/button.png with Playwright."
uv run python microvm.py agent-log         # tail Claude's progress
uv run python microvm.py pull /workspace/screenshots/button.png ./button.png
xdg-open ./button.png                      # should now show a GREEN button
```

The token is injected at runtime into the VM (via the `:9000` channel), never
written into the image.

## Debugging — no SSH
```
uv run python microvm.py shell "docker ps"   # any command on the VM host
uv run python microvm.py shell               # interactive loop
uv run python microvm.py logs                # tail the VM's CloudWatch stdout
```

## Tear down
```
uv run python microvm.py terminate
uv run python microvm.py clean               # also deletes the image (bucket/role kept)
```

## Findings

- **Playwright + headless Chromium work on an arm64 MicroVM** (`--no-sandbox`,
  since Chromium runs as root). The browser launches and screenshots cleanly.
- **The first Chromium launch in a fresh VM is cold/slow** — `shot` retries once
  to absorb it. Run driver commands individually (don't chain many in one shell
  — they add up past the 2-min ceiling).
- **No Docker needed** — running the app flattened (redis + uvicorn + http.server)
  sidesteps the nested-DNS constraints from POC 02 entirely. Everything is a host
  process, so DNS via the `127.0.0.2` stub just works; the screenshot only hits
  localhost anyway.
- **Visual loop:** screenshot in VM -> `pull` to local PNG -> open / Read it. This
  is exactly how an agent would self-verify UI changes.
