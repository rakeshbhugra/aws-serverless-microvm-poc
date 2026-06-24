#!/usr/bin/env python3
"""Drive the full Lambda MicroVM lifecycle for the hello-world POC (track 1).

Each subcommand is one step of docs/index.html §03, so you can walk the flow
by hand and build CLI/SDK muscle memory:

    python microvm.py check        # verify creds + that boto3 knows lambda-microvms
    python microvm.py prereqs      # create S3 bucket + IAM build role
    python microvm.py package      # zip app.py + Dockerfile -> S3
    python microvm.py build        # create-microvm-image (slow, one-time)
    python microvm.py wait-image   # poll until CREATED
    python microvm.py run          # run-microvm  (saves id + endpoint)
    python microvm.py wait         # poll until RUNNING
    python microvm.py token        # mint a 30-min JWE auth token
    python microvm.py curl         # hit the endpoint (run twice: watch `hits`)
    python microvm.py suspend      # suspend; then `curl` again to auto-resume
    python microvm.py resume       # explicit resume
    python microvm.py terminate    # stop the VM
    python microvm.py clean        # terminate + delete image + role + zip
    python microvm.py all          # prereqs..curl in one shot

State (ids, endpoint, token) persists in .state.json between calls.

NOTE: lambda-microvms is brand new (June 2026). Request/response shapes below
were VERIFIED against botocore 1.43.36 (the version `uv add boto3` pulls), so
the param names are real, not guessed. You still need AWS credentials and a
boto3 new enough to carry the client — `check` confirms both.
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request
import zipfile

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).parent
STATE_FILE = HERE / ".state.json"

REGION = os.environ.get("REGION", "us-east-1")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "ryu-hello-world")
ROLE_NAME = os.environ.get("ROLE_NAME", "MicrovmBuildRole")
ZIP_KEY = "hello-world.zip"

BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"

IDLE_POLICY = {
    "autoResumeEnabled": True,
    "maxIdleDurationSeconds": 900,
    "suspendedDurationSeconds": 1800,
}


# ---- tiny state helpers -----------------------------------------------------
def state_load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def state_save(**kw) -> dict:
    s = state_load()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    return s


def need(key: str):
    val = state_load().get(key)
    if not val:
        sys.exit(f"missing '{key}' in state — run the earlier steps first")
    return val


# ---- clients (lazy so `check` can report nicely) ---------------------------
def mv():
    return boto3.client("lambda-microvms", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return os.environ.get("BUCKET", f"ryu-microvm-poc-{account_id()}")


# ---- steps ------------------------------------------------------------------
def cmd_check(_):
    try:
        ident = boto3.client("sts").get_caller_identity()
        print(f"  creds OK   acct={ident['Account']} arn={ident['Arn']}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"  no creds: {e}\n  -> configure AWS access on this box first")
    try:
        mv()
        print("  boto3 knows 'lambda-microvms' client")
    except Exception as e:  # noqa: BLE001
        sys.exit(
            f"  boto3 has NO lambda-microvms client: {e}\n"
            "  -> upgrade: uv add boto3   (need botocore >= 1.43.36)"
        )
    print(f"  region={REGION}  image={IMAGE_NAME}  bucket={bucket_name()}")
    print("  all good — run: python microvm.py prereqs")


TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
        }
    ],
}


def perm_policy(bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/*"},
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "arn:aws:logs:*:*:*",
            },
        ],
    }


def cmd_prereqs(_):
    """Create the two resources a MicroVM image build requires, idempotently.

    1. An S3 bucket (`ryu-microvm-poc-<account>`) — Lambda pulls the code zip
       from S3 at build time; it can't read local disk. `package` uploads here.
    2. An IAM build role (`MicrovmBuildRole`) that Lambda *assumes* during the
       build. It carries two policies:
         - trust policy (TRUST): lets the Lambda service assume the role.
         - permissions policy (perm_policy): s3:GetObject on the bucket (fetch
           the zip) + logs:* (write build logs to CloudWatch).

    Both creates are guarded by an existence check, so re-running is safe. The
    bucket name and role ARN are saved to .state.json for `build` to read, then
    we sleep 10s for IAM eventual consistency (a brand-new role isn't instantly
    assumable). Torn back down by `clean`.
    """
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam")
    bucket = bucket_name()

    print(f"==> S3 bucket: {bucket}")
    try:
        s3.head_bucket(Bucket=bucket)
        print("    exists")
    except ClientError:
        # us-east-1 must NOT pass a LocationConstraint
        kw = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=bucket, **kw)
        print("    created")

    print(f"==> IAM build role: {ROLE_NAME}")
    try:
        iam.get_role(RoleName=ROLE_NAME)
        print("    exists")
    except ClientError:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST))
        print("    created")
    iam.put_role_policy(
        RoleName=ROLE_NAME, PolicyName="microvm-build", PolicyDocument=json.dumps(perm_policy(bucket))
    )
    role_arn = f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}"
    state_save(bucket=bucket, role_arn=role_arn)
    print("    policy attached; sleeping 10s for IAM propagation")
    time.sleep(10)
    print(f"==> role: {role_arn}")


def cmd_package(_):
    bucket = state_load().get("bucket") or bucket_name()
    zip_path = HERE / ZIP_KEY
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "app.py", "app.py")
        z.write(HERE / "Dockerfile", "Dockerfile")
    boto3.client("s3", region_name=REGION).upload_file(str(zip_path), bucket, ZIP_KEY)
    print(f"==> uploaded s3://{bucket}/{ZIP_KEY}")


def cmd_build(_):
    s = state_load()
    bucket = s.get("bucket") or bucket_name()
    resp = mv().create_microvm_image(
        name=IMAGE_NAME,
        codeArtifact={"uri": f"s3://{bucket}/{ZIP_KEY}"},
        baseImageArn=BASE_IMAGE_ARN,
        buildRoleArn=s["role_arn"],
    )
    print(f"==> build started: {resp.get('imageArn', IMAGE_NAME)}")
    print("    poll with: python microvm.py wait-image")


def cmd_wait_image(_):
    while True:
        img = mv().get_microvm_image(imageIdentifier=IMAGE_NAME)
        st = img.get("state")
        print(f"    image state: {st}")
        if st in ("CREATED", "UPDATED"):
            state_save(image_arn=img.get("imageArn", IMAGE_NAME))
            print(f"==> image ready: {img.get('imageArn', IMAGE_NAME)}")
            return
        if st and "FAILED" in st:
            sys.exit(f"build failed — check CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        time.sleep(10)


def cmd_run(_):
    s = state_load()
    resp = mv().run_microvm(
        imageIdentifier=s.get("image_arn", IMAGE_NAME),
        ingressNetworkConnectors=[INGRESS],
        egressNetworkConnectors=[EGRESS],
        idlePolicy=IDLE_POLICY,
    )
    state_save(microvm_id=resp["microvmId"], endpoint=resp["endpoint"])
    print(f"==> microvm {resp['microvmId']}")
    print(f"    endpoint https://{resp['endpoint']}")
    print("    poll with: python microvm.py wait")


def cmd_wait(_):
    mid = need("microvm_id")
    while True:
        st = mv().get_microvm(microvmIdentifier=mid).get("state")
        print(f"    microvm state: {st}")
        if st == "RUNNING":
            return
        if st in ("TERMINATED",) or (st and "FAILED" in st):
            sys.exit(f"unexpected state: {st}")
        time.sleep(5)


def cmd_token(_):
    mid = need("microvm_id")
    resp = mv().create_microvm_auth_token(
        microvmIdentifier=mid, expirationInMinutes=30, allowedPorts=[{"allPorts": {}}]
    )
    token = resp["authToken"]["X-aws-proxy-auth"]
    state_save(token=token)
    print("==> token minted (valid 30 min)")


def cmd_curl(_):
    s = state_load()
    endpoint, token = need("endpoint"), need("token")
    req = urllib.request.Request(
        f"https://{endpoint}/",
        headers={"X-aws-proxy-auth": token, "X-aws-proxy-port": "8080"},
    )
    print(f"==> GET https://{endpoint}/")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.read().decode())


def _simple(method: str, label: str):
    def fn(_):
        mid = need("microvm_id")
        getattr(mv(), method)(microvmIdentifier=mid)
        print(f"==> {label}: {mid}")

    return fn


def cmd_clean(_):
    s = state_load()
    if s.get("microvm_id"):
        try:
            mv().terminate_microvm(microvmIdentifier=s["microvm_id"])
            print("    terminated microvm")
        except ClientError as e:
            print(f"    (terminate skipped: {e})")
    try:
        mv().delete_microvm_image(imageIdentifier=IMAGE_NAME)
        print("    deleted image")
    except ClientError as e:
        print(f"    (image delete skipped: {e})")
    iam = boto3.client("iam")
    try:
        iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="microvm-build")
        iam.delete_role(RoleName=ROLE_NAME)
        print("    deleted IAM role")
    except ClientError as e:
        print(f"    (role delete skipped: {e})")
    if s.get("bucket"):
        try:
            boto3.client("s3", region_name=REGION).delete_object(Bucket=s["bucket"], Key=ZIP_KEY)
            print("    deleted zip (bucket kept)")
        except ClientError:
            pass
    STATE_FILE.unlink(missing_ok=True)
    print("==> cleaned (S3 bucket itself left in place)")


def cmd_all(args):
    for fn in (cmd_prereqs, cmd_package, cmd_build, cmd_wait_image, cmd_run, cmd_wait, cmd_token, cmd_curl):
        fn(args)


COMMANDS = {
    "check": cmd_check,
    "prereqs": cmd_prereqs,
    "package": cmd_package,
    "build": cmd_build,
    "wait-image": cmd_wait_image,
    "run": cmd_run,
    "wait": cmd_wait,
    "token": cmd_token,
    "curl": cmd_curl,
    "suspend": _simple("suspend_microvm", "suspended"),
    "resume": _simple("resume_microvm", "resumed"),
    "terminate": _simple("terminate_microvm", "terminated"),
    "clean": cmd_clean,
    "all": cmd_all,
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    args = p.parse_args()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
