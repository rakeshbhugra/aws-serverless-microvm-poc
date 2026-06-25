# Counter app — workspace notes for the agent

A tiny three-part app. The hit counter lives in Redis.

- `backend/` — FastAPI (`app.py`): `GET /count`, `POST /increment`. Runs on **:8000**.
- `frontend/` — static `index.html`: a **button** that calls the backend and shows
  the count. Served as static files on **:3000**.
- Redis — holds the `count` key, on **:6379**.

## Running it

You decide how. Two options, both fine:

- **Directly** (simplest here): `redis-server --daemonize yes`, then
  `cd backend && REDIS_HOST=localhost uvicorn app:app --port 8000`, then
  `cd frontend && python -m http.server 3000`.
- **Docker** (only if installed): `docker compose up`. Note this is a MicroVM —
  nested-container DNS only works via `127.0.0.2` + host networking (already set
  in `docker-compose.yml`).

## Environment

This runs inside an isolated Lambda MicroVM (arm64). `playwright` + `chromium`
are installed for browser automation/screenshots. Outbound DNS works only via
the `127.0.0.2` stub (public resolvers are blocked), but the internet is
otherwise reachable.
