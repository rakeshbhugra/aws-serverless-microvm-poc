# POC 02 — `docker compose up` inside a MicroVM

The step between hello-world and the baked snapshot. POC 01 proved a single
Dockerfile app runs in a MicroVM. **This proves a full Docker daemon + a
`docker compose` stack runs inside one** — the prerequisite for ever
snapshotting ryureflect's real compose stack.

Status: **proven.** A Redis + FastAPI stack runs via `docker compose up` inside
the VM and serves the Redis-backed counter through the MicroVM endpoint
(`{"count":1}`, `{"count":2}`, …). We do *not* snapshot the running stack warm
yet — that's the next track.

## The app

```
stack/
  Dockerfile          # the MicroVM image: Docker engine + compose plugin + debug-exec
  docker-compose.yml  # redis + backend, host networking, deps installed at start
  daemon.json         # dockerd DNS -> 127.0.0.2 (the only resolver that works)
  entrypoint.sh       # start debug-exec + dockerd -> docker compose up -> stay alive
  debug-exec.py       # :9000 exec endpoint backing `microvm.py shell`
  backend/
    app.py            # FastAPI: GET / -> redis.incr("hits") -> {count}
    requirements.txt
microvm.py            # boto3 driver (ALL caps, zips stack/, logs + shell)
```

## What it took to make Docker-in-MicroVM work

`docker compose up` runs, but nested-container **networking** has two hard
constraints we discovered the hard way (see "What we learned" below):

- `additionalOsCapabilities=["ALL"]` on `create-microvm-image` — grants dockerd
  the namespace/mount/network privileges it needs (inside the VM boundary).
- 4 GB memory baseline (`MEMORY_MIB=...` to override) — dockerd + pip want room.
- **DNS only via `127.0.0.2`** — `daemon.json` points containers at the VM's
  local stub resolver; public resolvers (8.8.8.8) are blocked.
- **Host networking** — `network_mode: host` on both services, so containers can
  reach that `127.0.0.2` loopback stub; the backend talks to Redis over localhost.
- **No `docker build` in-VM** — buildkit ignores the `127.0.0.2` resolver, so we
  run a stock `python` image and `pip install` at container start (plain
  `docker run` DOES get the resolver).

## Run it

```
uv run python microvm.py check
uv run python microvm.py prereqs      # no-op if POC 01 already made the bucket/role
uv run python microvm.py package      # zip stack/ -> S3
uv run python microvm.py build        # create/update image with ALL caps (slow)
uv run python microvm.py wait-image
uv run python microvm.py run
uv run python microvm.py wait
uv run python microvm.py token
# the backend pip-installs at container start, so give it ~30-45s on a fresh VM:
uv run python microvm.py curl         # -> {"count":1}; again -> {"count":2}
```

### Debugging — no SSH needed
```
uv run python microvm.py logs              # tail the VM's CloudWatch stdout
uv run python microvm.py shell "docker ps" # run a command on the VM host (:9000)
uv run python microvm.py shell             # interactive loop (docker logs, exec, ...)
```

### Tear down
```
uv run python microvm.py terminate
uv run python microvm.py clean        # also deletes the image (bucket/role kept)
```

## What we learned (the hard way)

Debugged live with `shell` + `logs` rather than rebuild cycles:

1. **`docker compose up` works in a MicroVM** — dockerd starts, images pull,
   containers run, and the stack serves through the endpoint. The capability is
   real.
2. **Outbound DNS is locked to the VM's stub at `127.0.0.2`.** The host resolves
   fine (image pulls worked), but containers pointed at `8.8.8.8/1.1.1.1` get
   `Temporary failure in name resolution`. Forcing `127.0.0.2` resolved instantly.
   Egress itself is fine — raw TCP to `1.1.1.1:443` connects; it's specifically
   public-resolver DNS that's blocked.
3. **`127.0.0.2` is the host loopback**, so a bridge container can't reach it —
   you need `network_mode: host`.
4. **buildkit won't use `127.0.0.2`** even with `--network=host` (its build RUN
   steps inherit `daemon.json` dns but still fail to resolve), so `pip install`
   during `docker build` dies. Installing at container *start* (a plain
   `docker run`) works. → for the baked-snapshot track, pre-pull/pre-build images
   out-of-band (e.g. ECR) rather than building in-VM.
5. **No `/ready` hook = snapshot taken mid-init**, so the stack isn't baked warm
   and the entrypoint re-runs on each launch. Gating with `/ready` is the next
   track's job.

These findings feed directly into the baked-snapshot track and `MicroVMRunner`.
