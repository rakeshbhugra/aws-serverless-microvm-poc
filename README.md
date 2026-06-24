# aws-serverless-microvm-poc

Learning AWS Lambda MicroVMs (GA June 2026) — the serverless Firecracker primitive
for isolated, stateful, suspend-resumable sandboxes. Motivation: a candidate
backend for [ryureflect](../../ryureflect)'s per-project sandbox `Runner` seam.

## Layout

- **`docs/`** — write-ups. Start at **`docs/index.html`** — the learning plan,
  mental model, interactive lifecycle/build/networking walkthroughs, and the
  numbered roadmap of follow-up docs + POCs.
- **`pocs/`** — runnable proofs-of-concept, one dir per track in the roadmap.

## The roadmap (see docs/index.html §09)

1. Hello-world MicroVM end to end — **done**
2. Run `docker compose up` inside a MicroVM — in progress
3. Bake a repo + compose stack into an image  ← the core question
4. Snapshot safety & the `/run` hook
5. Networking: multi-port + WebSockets
6. Run Claude Code inside a MicroVM
7. `MicroVMRunner` — implement the ryureflect seam
8. Cost & ops model

Open the index locally:

```bash
xdg-open docs/index.html   # or just double-click it
```
