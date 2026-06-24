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

1. Hello-world MicroVM end to end
2. Bake a repo + compose stack into an image  ← the core question
3. Snapshot safety & the `/run` hook
4. Networking: multi-port + WebSockets
5. Run Claude Code inside a MicroVM
6. `MicroVMRunner` — implement the ryureflect seam
7. Cost & ops model

Open the index locally:

```bash
xdg-open docs/index.html   # or just double-click it
```
