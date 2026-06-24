# POC 01 — Hello-world MicroVM, end to end

Roadmap track 1. Walks the full lifecycle (docs/index.html §03) by hand against
the POC account: build an image, run a VM, mint a token, hit it, suspend/resume,
terminate. Goal — CLI/SDK muscle memory + confirm the IAM + connector setup.

This is a **uv project** (`uv init` + `uv add boto3`). Run everything with
`uv run` — it manages the venv and deps for you.

## Files

| File | What |
|---|---|
| `app.py` | stdlib HTTP server on :8080. Returns `hits` (proves state survives suspend/resume) and `booted_at` (same across all VMs — the snapshot gotcha). |
| `Dockerfile` | AWS snapshot-safe AL2023 base + `python3 app.py`. |
| `microvm.py` | boto3 driver — one subcommand per lifecycle step. Shapes verified against botocore 1.43.36. Doubles as the reference for the eventual `MicroVMRunner`. |
| `pyproject.toml` / `uv.lock` | uv project + pinned deps (boto3). |

## Before you can run it — one prerequisite left

`uv` already solves the "is the client new enough?" problem: `uv add boto3`
pulls botocore 1.43.36, which **does** carry the `lambda-microvms` client
(confirmed). The only thing missing on this box is:

- **AWS credentials.** `aws sts get-caller-identity` currently fails. Wire this
  box to the POC account `276562184584` (`AdministratorAccess`) via SSO or
  access keys. From the Claude Code prompt you can run it inline:

  ```
  ! aws configure sso      # or: aws configure
  ```

`uv run python microvm.py check` verifies creds + client before you spend time
on a build.

## Run it (step through the lifecycle)

```
uv run python microvm.py check        # creds + client present?
uv run python microvm.py prereqs      # S3 bucket + IAM build role (waits 10s)
uv run python microvm.py package      # zip app.py + Dockerfile -> S3
uv run python microvm.py build        # create-microvm-image  (slow: minutes)
uv run python microvm.py wait-image   # poll until CREATED
uv run python microvm.py run          # run-microvm -> saves id + endpoint
uv run python microvm.py wait         # poll until RUNNING
uv run python microvm.py token        # 30-min JWE auth token
uv run python microvm.py curl         # -> {"hits":1,...}   run again -> hits:2
```

### See the two teaching behaviours

- **State survives suspend/resume:** `curl` a few times (watch `hits` climb),
  then `suspend`, then `curl` again. Auto-resume fires on the inbound request;
  `hits` keeps counting — RAM was preserved, not reset.
- **Shared initial state:** `booted_at` is identical on every request and would
  be identical across a second VM from the same image — it's baked into the
  snapshot. This is why §05 says: generate anything unique in the `/run` hook.

### Tear down (stop charges)

```
uv run python microvm.py terminate    # stop this VM
uv run python microvm.py clean        # terminate + delete image + IAM role + zip
```

Compute only bills while `RUNNING`; suspended state is ~cents; the image is
~$0.08/GB-month. `clean` leaves the S3 bucket itself in place (delete by hand
if you want it gone).

## Caveats

- The boto3 request/response shapes in `microvm.py` were verified against
  botocore 1.43.36 (`codeArtifact={"uri":...}`, connector ARN lists,
  `idlePolicy`, `authToken["X-aws-proxy-auth"]`). If a `ValidationException`
  still appears at runtime, note the fix here so track 6 inherits it.
- Bonus ops seen in the client for later tracks: `CreateMicrovmShellAuthToken`
  (shell into a VM — track 5), and image-build accepts `additionalOsCapabilities`
  + `egressNetworkConnectors` (the Docker hook — track 2).
- ARM64-only: the build runs on AWS so arch is handled by `--base-image-arn`.
- This POC uses the AWS-managed `ALL_INGRESS` / `INTERNET_EGRESS` connectors.
