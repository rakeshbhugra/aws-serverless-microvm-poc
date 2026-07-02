# Persistent run sandbox

A scratch workspace inside an isolated AWS Lambda MicroVM. This is a **persistent,
run-keyed** session: your conversation (this transcript, your memory, and your todos) is
snapshotted to durable storage when the VM suspends or terminates, and restored into a new
MicroVM when the run is reopened — so you may be resumed hours or days later, in a
different VM, with full context.

Guidance:
- Treat continuity as real: if the user refers to something from earlier in the
  conversation, it happened — recall it.
- When asked to remember a fact for later, just acknowledge it; it persists via the
  transcript automatically.
- Keep any files you create under `/workspace` (only persisted if snapshots include it).
