#!/usr/bin/env python3
"""Drive the secrets-token MicroVM (roadmap track 5 — credential from Secrets Manager)
with the external-ElastiCache networking carried over from POC 4.

A light Debian image (Node + Python + Claude CLI) running an agent-server on :9100
that runs `claude -p --output-format stream-json`, buffers each event into an external
ElastiCache (Valkey) Redis Stream over a VPC egress connector, and relays it over SSE.

    uv run python microvm.py check / prereqs / net-setup / add-nat
    uv run python microvm.py package / build / wait-image / run / wait / token / probe
    uv run python microvm.py demo-stream          # canned producer (no credential)
    uv run python microvm.py agent-stream "task"  # run Claude, stream live
    uv run python microvm.py shell / logs / clean / net-clean

The Claude credential is NOT handled by the operator: the VM assumes a runtime role
(executionRoleArn) and fetches it from Secrets Manager itself. The Redis buffer is an
external ElastiCache cluster reached over a VPC egress connector (+ NAT for the Claude
API / Secrets Manager). See README for the combined runbook.
"""

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

HERE = pathlib.Path(__file__).parent
IMAGE_DIR = HERE / "image"
WORKSPACE_DIR = HERE / "workspace"
STATE_FILE = HERE / ".state.json"

REGION = os.environ.get("REGION", "us-east-1")
IMAGE_NAME = os.environ.get("IMAGE_NAME", "ryu-secrets-token")
ROLE_NAME = os.environ.get("ROLE_NAME", "MicrovmBuildRole")
RUNTIME_ROLE_NAME = os.environ.get("RUNTIME_ROLE_NAME", "QhiveMicrovmRuntimeRole")
SECRET_NAME = os.environ.get("SECRET_NAME", "microvm/claude-api-key")
ZIP_KEY = "secrets-token.zip"
MEMORY_MIB = int(os.environ.get("MEMORY_MIB", "2048"))

# External Redis (ElastiCache) instead of an in-VM redis-server. The VM reaches it
# over a customer VPC egress connector; the endpoint is baked into the image (it is
# not a secret) by `package` and read by agent-server.py.
CACHE_CLUSTER_ID = os.environ.get("CACHE_CLUSTER_ID", "ryu-agent-cache")
CACHE_NODE_TYPE = os.environ.get("CACHE_NODE_TYPE", "cache.t4g.micro")  # smallest node (~$0.016/hr)
CACHE_SUBNET_GROUP = os.environ.get("CACHE_SUBNET_GROUP", "ryu-agent-cache-subnets")
VM_SG_NAME = os.environ.get("VM_SG_NAME", "ryu-microvm-egress")
CACHE_SG_NAME = os.environ.get("CACHE_SG_NAME", "ryu-cache-ingress")
CONNECTOR_NAME = os.environ.get("CONNECTOR_NAME", "ryu-vpc-egress")
CONNECTOR_ROLE_NAME = os.environ.get("CONNECTOR_ROLE_NAME", "MicrovmConnectorOperatorRole")
REDIS_HOST_FILE = IMAGE_DIR / "redis_host"  # baked into the image; gitignored

BASE_IMAGE_ARN = f"arn:aws:lambda:{REGION}:aws:microvm-image:al2023-1"
INGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:ALL_INGRESS"
EGRESS = f"arn:aws:lambda:{REGION}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
IDLE_POLICY = {"autoResumeEnabled": True, "maxIdleDurationSeconds": 1800, "suspendedDurationSeconds": 3600}

DEFAULT_TASK = "Add a `farewell(name)` function to hello.py that returns a goodbye string, then run it."


def state_load() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def state_save(**kw) -> dict:
    s = state_load()
    s.update(kw)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    return s


def need(key: str):
    v = state_load().get(key)
    if not v:
        sys.exit(f"missing '{key}' — run earlier steps first")
    return v


def mv():
    return boto3.client("lambda-microvms", region_name=REGION)


def ec2():
    return boto3.client("ec2", region_name=REGION)


def ecache():
    return boto3.client("elasticache", region_name=REGION)


def core():
    return boto3.client("lambda-core", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


def bucket_name() -> str:
    return os.environ.get("BUCKET", f"ryu-microvm-poc-{account_id()}")


def image_arn() -> str:
    return state_load().get("image_arn") or f"arn:aws:lambda:{REGION}:{account_id()}:microvm-image:{IMAGE_NAME}"


def _hdr(port: str) -> dict:
    return {"X-aws-proxy-auth": need("token"), "X-aws-proxy-port": port}


# ---- lifecycle (same shape as POC 01-03) -----------------------------------
def cmd_check(_):
    boto3.client("sts").get_caller_identity()
    mv()
    sm = "ok"
    try:
        boto3.client("secretsmanager", region_name=REGION).describe_secret(SecretId=SECRET_NAME)
    except ClientError as e:
        sm = f"MISSING ({e.response.get('Error', {}).get('Code')})"
    print(f"  OK region={REGION} image={IMAGE_NAME} mem={MEMORY_MIB}MiB "
          f"runtime_role={RUNTIME_ROLE_NAME} secret={SECRET_NAME} [{sm}]")


TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
         "Principal": {"Service": "lambda.amazonaws.com"}, "Action": ["sts:AssumeRole", "sts:TagSession"]}]}


def perm_policy(b):
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{b}/*"},
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": "arn:aws:logs:*:*:*"}]}


def cmd_prereqs(_):
    s3 = boto3.client("s3", region_name=REGION)
    iam = boto3.client("iam")
    b = bucket_name()
    try:
        s3.head_bucket(Bucket=b)
    except ClientError:
        kw = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
        s3.create_bucket(Bucket=b, **kw)
    try:
        iam.get_role(RoleName=ROLE_NAME)
    except ClientError:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST))
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="microvm-build", PolicyDocument=json.dumps(perm_policy(b)))
    # Runtime role is pre-provisioned by the operator (grants the VM secretsmanager:GetSecretValue
    # on the Claude credential). We only resolve its ARN here — never create/modify it.
    try:
        runtime_arn = iam.get_role(RoleName=RUNTIME_ROLE_NAME)["Role"]["Arn"]
    except ClientError:
        sys.exit(f"runtime role '{RUNTIME_ROLE_NAME}' not found — create it (trust lambda.amazonaws.com, "
                 f"grant secretsmanager:GetSecretValue on {SECRET_NAME}) or set RUNTIME_ROLE_NAME")
    state_save(bucket=b, role_arn=f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}", runtime_role_arn=runtime_arn)
    print(f"==> bucket + build role ready; runtime role {runtime_arn}; sleeping 10s for IAM")
    time.sleep(10)


# ---- external Redis (ElastiCache) over a VPC egress connector ----------------
CONNECTOR_TRUST = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "network-connectors.lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}]}

CONNECTOR_PERMS = {"Version": "2012-10-17", "Statement": [
    {"Sid": "ManageENI", "Effect": "Allow",
     "Action": ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces"],
     "Resource": ["arn:aws:ec2:*:*:network-interface/*", "arn:aws:ec2:*:*:subnet/*", "arn:aws:ec2:*:*:security-group/*"]},
    {"Sid": "TagENI", "Effect": "Allow", "Action": "ec2:CreateTags",
     "Resource": "arn:aws:ec2:*:*:network-interface/*",
     "Condition": {"StringEquals": {"ec2:ManagedResourceOperator": "network-connectors.lambda.amazonaws.com"}}}]}


# MicroVMs aren't offered in every AZ (e.g. use1-az3); exclude unsupported ones so
# the connector's ENIs land only where a MicroVM can attach.
EXCLUDE_AZ_IDS = set(filter(None, os.environ.get("EXCLUDE_AZ_IDS", "use1-az3").split(",")))


def _default_vpc_subnets() -> tuple[str, list[dict]]:
    vpcs = ec2().describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        sys.exit("no default VPC — set VPC_ID/subnets via a dedicated VPC instead")
    vpc = vpcs[0]["VpcId"]
    subnets = [{"id": s["SubnetId"], "az": s.get("AvailabilityZoneId")}
               for s in ec2().describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
               if s.get("AvailabilityZoneId") not in EXCLUDE_AZ_IDS]
    if not subnets:
        sys.exit("no usable subnets after AZ filter")
    return vpc, subnets


def _create_connector(op_role: str, subnets: list[dict], vm_sg: str) -> tuple[str, list[str]]:
    """Create the VPC egress connector, dropping any AZ the MicroVM compute type
    rejects (the supported-AZ set isn't published, so we discover it from the error)."""
    pool = list(subnets)
    while pool:
        ids = [s["id"] for s in pool]
        print(f"    trying connector in AZs {[s['az'] for s in pool]}")
        try:
            arn = core().create_network_connector(
                Name=CONNECTOR_NAME, OperatorRole=op_role,
                Configuration={"VpcEgressConfiguration": {
                    "SubnetIds": ids, "SecurityGroupIds": [vm_sg],
                    "NetworkProtocol": "IPv4", "AssociatedComputeResourceTypes": ["MicroVm"]}})["Arn"]
            return arn, ids
        except ClientError as e:
            msg = str(e)
            bad = next((s for s in pool if s["az"] and s["az"] in msg), None)
            if "not available for compute type" in msg and bad:
                print(f"    dropping unsupported AZ {bad['az']}")
                pool.remove(bad)
                continue
            raise
    sys.exit("no MicroVM-supported AZ among the VPC subnets")


def _ensure_sg(name: str, vpc: str, desc: str) -> str:
    found = ec2().describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if found:
        return found[0]["GroupId"]
    return ec2().create_security_group(GroupName=name, Description=desc, VpcId=vpc)["GroupId"]


def cmd_net_setup(_):
    """Create the VPC egress path + ElastiCache: security groups, connector operator
    role, the lambda-core network connector, and a Redis node cluster. Idempotent-ish."""
    iam = boto3.client("iam")
    vpc, subnet_info = _default_vpc_subnets()
    print(f"==> default VPC {vpc} with {len(subnet_info)} candidate subnets "
          f"(AZs {[s['az'] for s in subnet_info]})")

    # Security groups: VM egress SG (source) + cache SG (allows 6379 from VM SG).
    vm_sg = _ensure_sg(VM_SG_NAME, vpc, "MicroVM VPC egress")
    cache_sg = _ensure_sg(CACHE_SG_NAME, vpc, "ElastiCache ingress from MicroVMs")
    try:
        ec2().authorize_security_group_ingress(GroupId=cache_sg, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": 6379, "ToPort": 6379,
            "UserIdGroupPairs": [{"GroupId": vm_sg, "Description": "redis from microvm egress"}]}])
    except ClientError as e:
        if "Duplicate" not in str(e):
            raise
    print(f"    sg vm={vm_sg} cache={cache_sg} (6379 cache<-vm)")

    # Connector operator role (lets Lambda create ENIs in the VPC).
    try:
        iam.get_role(RoleName=CONNECTOR_ROLE_NAME)
    except ClientError:
        iam.create_role(RoleName=CONNECTOR_ROLE_NAME, AssumeRolePolicyDocument=json.dumps(CONNECTOR_TRUST))
    iam.put_role_policy(RoleName=CONNECTOR_ROLE_NAME, PolicyName="manage-eni",
                        PolicyDocument=json.dumps(CONNECTOR_PERMS))
    op_role = f"arn:aws:iam::{account_id()}:role/{CONNECTOR_ROLE_NAME}"
    print(f"    connector operator role {op_role}; sleeping 10s for IAM"); time.sleep(10)

    # Network connector (VPC egress). Reused across runs; wait until ACTIVE.
    # usable_subnets = the AZ-filtered set the connector actually accepted.
    existing = {c["Name"]: c for c in core().list_network_connectors().get("NetworkConnectors", [])}
    if CONNECTOR_NAME in existing:
        conn_arn = existing[CONNECTOR_NAME]["Arn"]
        usable_subnets = [s["id"] for s in subnet_info]
    else:
        conn_arn, usable_subnets = _create_connector(op_role, subnet_info, vm_sg)
    while True:
        st = core().get_network_connector(Identifier=conn_arn).get("State")
        print(f"    connector state: {st}")
        if st == "ACTIVE":
            break
        if st in ("FAILED", "DELETE_FAILED"):
            sys.exit(f"connector {st}")
        time.sleep(10)

    # ElastiCache: subnet group + single-node Valkey cluster, SG-gated (no TLS/auth for POC).
    # Same AZ subset the connector accepted, so the VM and cache share AZs.
    try:
        ecache().create_cache_subnet_group(CacheSubnetGroupName=CACHE_SUBNET_GROUP,
                                            CacheSubnetGroupDescription="ryu microvm cache", SubnetIds=usable_subnets)
    except ClientError as e:
        if "already exists" not in str(e).lower():
            raise
    # Valkey requires CreateReplicationGroup (not CreateCacheCluster). Single node,
    # cluster-mode disabled, no replica, no TLS/auth — SG-gated for the POC.
    try:
        ecache().create_replication_group(
            ReplicationGroupId=CACHE_CLUSTER_ID, ReplicationGroupDescription="ryu microvm valkey",
            Engine="valkey", CacheNodeType=CACHE_NODE_TYPE, NumCacheClusters=1,
            CacheSubnetGroupName=CACHE_SUBNET_GROUP, SecurityGroupIds=[cache_sg],
            TransitEncryptionEnabled=False)  # POC: SG-gated, plaintext client
        print(f"    creating valkey replication group {CACHE_CLUSTER_ID} ({CACHE_NODE_TYPE})")
    except ClientError as e:
        if "already exists" not in str(e).lower():
            raise
    while True:
        rg = ecache().describe_replication_groups(ReplicationGroupId=CACHE_CLUSTER_ID)["ReplicationGroups"][0]
        st = rg["Status"]
        print(f"    cache state: {st}")
        if st == "available":
            host = rg["NodeGroups"][0]["PrimaryEndpoint"]["Address"]
            break
        time.sleep(15)
    state_save(connector_arn=conn_arn, vm_sg=vm_sg, cache_sg=cache_sg, redis_host=host,
               vpc=vpc, subnets=usable_subnets)
    print(f"==> ready. connector={conn_arn}\n    redis_host={host}")


def cmd_add_nat(_):
    """Give the VPC egress path internet access (for the Claude API). MicroVM ENIs get
    no public IP, so a VPC connector alone has no internet — add a NAT gateway and move
    the connector's ENIs into private subnets routed through it. ElastiCache stays put
    (reached over the VPC's local route)."""
    s = state_load()
    vpc = need("vpc")
    all_subnets = ec2().describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["Subnets"]
    # NAT goes in a public (default) subnet — one NOT used by the connector, so it keeps
    # its IGW route. Prefer an excluded-AZ default subnet.
    used = set(s.get("subnets", []))
    pub = next((x for x in all_subnets if x.get("DefaultForAz") and x["SubnetId"] not in used), all_subnets[0])
    print(f"==> NAT in public subnet {pub['SubnetId']} ({pub['AvailabilityZoneId']})")

    if not s.get("nat_gw"):
        eip = ec2().allocate_address(Domain="vpc")["AllocationId"]
        nat = ec2().create_nat_gateway(SubnetId=pub["SubnetId"], AllocationId=eip)["NatGateway"]["NatGatewayId"]
        print(f"    NAT {nat} (eip {eip}); waiting available")
        ec2().get_waiter("nat_gateway_available").wait(NatGatewayIds=[nat])
        s = state_save(nat_gw=nat, eip_alloc=eip)
    nat = s["nat_gw"]

    # Private subnets in the connector's AZs, routed 0.0.0.0/0 -> NAT.
    conn_azs = {x["AvailabilityZoneId"]: x["AvailabilityZone"]
                for x in all_subnets if x["SubnetId"] in used}
    if not s.get("private_subnets"):
        rt = ec2().create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
        ec2().create_route(RouteTableId=rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat)
        priv = []
        for i, az in enumerate(sorted(conn_azs.values())):
            sn = ec2().create_subnet(VpcId=vpc, CidrBlock=f"172.31.{96 + i}.0/24",
                                     AvailabilityZone=az)["Subnet"]["SubnetId"]
            ec2().associate_route_table(RouteTableId=rt, SubnetId=sn)
            priv.append(sn)
        s = state_save(private_rt=rt, private_subnets=priv)
        print(f"    private subnets {priv} -> NAT")
    priv = s["private_subnets"]

    # Repoint the connector at the private subnets (in place). Terminate any VM first.
    if s.get("microvm_id"):
        try:
            mv().terminate_microvm(microvmIdentifier=s["microvm_id"]); print("    terminated old VM")
        except ClientError:
            pass
    # Repoint at the private subnets, dropping any AZ MicroVM rejects (e.g. az5).
    az_of = {x["SubnetId"]: x["AvailabilityZoneId"]
             for x in ec2().describe_subnets(SubnetIds=priv)["Subnets"]}
    pool = list(priv)
    while pool:
        try:
            core().update_network_connector(Identifier=need("connector_arn"),
                Configuration={"VpcEgressConfiguration": {
                    "SubnetIds": pool, "SecurityGroupIds": [need("vm_sg")],
                    "NetworkProtocol": "IPv4", "AssociatedComputeResourceTypes": ["MicroVm"]}})
            break
        except ClientError as e:
            bad = next((sn for sn in pool if az_of.get(sn) and az_of[sn] in str(e)), None)
            if "not available for compute type" in str(e) and bad:
                print(f"    dropping connector subnet in {az_of[bad]}")
                pool.remove(bad)
                continue
            raise
    # Subnet migration is async: State stays ACTIVE while LastUpdateStatus cycles
    # InProgress -> Successful. Wait for the migration to actually finish.
    while True:
        c = core().get_network_connector(Identifier=need("connector_arn"))
        st, us = c.get("State"), c.get("LastUpdateStatus")
        print(f"    connector state={st} update={us}")
        if st == "ACTIVE" and us in (None, "Successful"):
            break
        if "FAIL" in ((us or "") + (st or "")).upper():
            sys.exit(f"connector update {us or st}: {c.get('LastUpdateStatusReason')}")
        time.sleep(10)
    print("==> NAT wired; connector now egresses via NAT. Re-run + probe.")


def cmd_package(_):
    import zipfile
    b = state_load().get("bucket") or bucket_name()
    host = state_load().get("redis_host")
    if not host:
        sys.exit("no redis_host in state — run 'net-setup' first")
    REDIS_HOST_FILE.write_text(host + "\n")  # baked into the image (non-secret)
    zp = HERE / ZIP_KEY
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(IMAGE_DIR.iterdir()):
            if f.is_file():
                z.write(f, f.name)
        for p in sorted(WORKSPACE_DIR.rglob("*")):
            if p.is_file():
                z.write(p, f"workspace/{p.relative_to(WORKSPACE_DIR)}")
    boto3.client("s3", region_name=REGION).upload_file(str(zp), b, ZIP_KEY)
    print(f"==> uploaded s3://{b}/{ZIP_KEY}")


def cmd_build(_):
    s = state_load()
    b = s.get("bucket") or bucket_name()
    common = dict(codeArtifact={"uri": f"s3://{b}/{ZIP_KEY}"}, baseImageArn=BASE_IMAGE_ARN,
                  buildRoleArn=s["role_arn"], resources=[{"minimumMemoryInMiB": MEMORY_MIB}])
    try:
        resp = mv().create_microvm_image(name=IMAGE_NAME, **common)
        print("==> create-microvm-image")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("ConflictException", "ResourceConflictException") or "exist" in str(e).lower():
            resp = mv().update_microvm_image(imageIdentifier=image_arn(), **common)
            print("==> update-microvm-image")
        else:
            raise
    if resp.get("imageArn"):
        state_save(image_arn=resp["imageArn"])
    state_save(image_version=resp.get("imageVersion"))  # track THIS build's version
    print(f"    building version {resp.get('imageVersion')}; poll: uv run python microvm.py wait-image")


def cmd_wait_image(_):
    """Poll the specific version we just built (not image-level state, which can
    show a stale UPDATED from a prior successful version while a new one fails)."""
    want = state_load().get("image_version")
    while True:
        vers = {v.get("imageVersion"): v for v in
                mv().list_microvm_image_versions(imageIdentifier=image_arn()).get("items", [])}
        if want and want in vers:
            st = vers[want].get("state")
            print(f"    version {want} state: {st}")
            if st == "SUCCESSFUL":
                return
            if st == "FAILED":
                sys.exit(f"version {want} build FAILED — CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        else:
            st = mv().get_microvm_image(imageIdentifier=image_arn()).get("state")
            print(f"    image state: {st}")
            if st in ("CREATED", "UPDATED"):
                return
            if st and "FAILED" in st:
                sys.exit(f"build failed — CloudWatch /aws/lambda/microvms/{IMAGE_NAME}")
        time.sleep(10)


def cmd_run(_):
    # VPC-only egress: the platform rejects INTERNET_EGRESS + a VPC connector together,
    # so all egress goes through the VPC connector. The VPC must therefore provide
    # internet (Claude API) via a NAT gateway — see 'add-nat'. ElastiCache is reached
    # privately within the VPC.
    # executionRoleArn (POC 5): VM assumes the runtime role to fetch the secret.
    # VPC egress connector (POC 4): reaches ElastiCache; Secrets Manager + Claude API
    # also egress through the VPC, so this needs the NAT from `add-nat`.
    egress = [need("connector_arn")]
    resp = mv().run_microvm(imageIdentifier=image_arn(), ingressNetworkConnectors=[INGRESS],
                            egressNetworkConnectors=egress, idlePolicy=IDLE_POLICY,
                            executionRoleArn=need("runtime_role_arn"))
    state_save(microvm_id=resp["microvmId"], endpoint=resp["endpoint"])
    print(f"==> microvm {resp['microvmId']} (VPC egress + runtime role)\n    https://{resp['endpoint']}")


def cmd_probe(_):
    """From inside the VM (:9000 shell), check reachability of ElastiCache (6379)
    and the public internet (Claude API) — decides whether a NAT gateway is needed."""
    host = need("redis_host")
    print("  --- ElastiCache reachability (expect PONG) ---")
    cmd_shell(_mk(f"/opt/venv/bin/python -c \"import socket,sys; s=socket.create_connection(('{host}',6379),5); "
                  f"s.sendall(b'PING\\r\\n'); print(s.recv(64))\""))
    print("  --- internet/Claude API reachability (expect HTTP 200/401) ---")
    cmd_shell(_mk("curl -sS -o /dev/null -w 'api.anthropic.com -> %{http_code}\\n' --max-time 8 https://api.anthropic.com/v1/messages -X POST || echo 'NO INTERNET'"))


def _mk(cmd: str):
    import argparse as _a
    ns = _a.Namespace(); ns.rest = [cmd]; return ns


def cmd_wait(_):
    mid = need("microvm_id")
    while True:
        st = mv().get_microvm(microvmIdentifier=mid).get("state")
        print(f"    microvm state: {st}")
        if st == "RUNNING":
            return
        if st == "TERMINATED" or (st and "FAILED" in st):
            sys.exit(f"unexpected state: {st}")
        time.sleep(5)


def cmd_token(_):
    mid = need("microvm_id")
    resp = mv().create_microvm_auth_token(microvmIdentifier=mid, expirationInMinutes=60, allowedPorts=[{"allPorts": {}}])
    state_save(token=resp["authToken"]["X-aws-proxy-auth"])
    print("==> token minted (60 min)")


# ---- the streaming bits -----------------------------------------------------
def _post_run(body: dict) -> str:
    endpoint = need("endpoint")
    req = urllib.request.Request(f"https://{endpoint}/run", data=json.dumps(body).encode(),
                                 headers={**_hdr("9100"), "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    print(f"  run #{resp['run']} ({resp['stream']})")
    return str(resp["run"])


def _consume_stream(run: str, from_id: str = "0"):
    """Open the SSE stream for one run and print events live until 'done'."""
    endpoint = need("endpoint")
    req = urllib.request.Request(f"https://{endpoint}/stream?run={run}&from={from_id}", headers=_hdr("9100"))
    print(f"  --- streaming run #{run} ---")
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode(errors="replace").rstrip("\n")
            if line.startswith("data:"):
                data = line[5:].strip()
                try:
                    obj = json.loads(data)
                except ValueError:
                    print("   ", data)
                    continue
                t = obj.get("type")
                if t == "start":
                    print(f"    ▶ start: {obj.get('task')}")
                elif t == "claude":
                    print(f"    · {obj.get('line')}")
                elif t == "done":
                    print(f"    ■ done (exit {obj.get('exit')})")
                    return


def cmd_demo_stream(_):
    """Prove the transport with a canned producer — no Claude token needed."""
    run = _post_run({"demo": True})
    _consume_stream(run)


def cmd_agent_stream(args):
    """Run Claude on a task inside the VM and stream its events live.

    No credential is handled here — the VM fetches it from Secrets Manager via its
    runtime role. We only send the task.
    """
    task = " ".join(args.rest) if args and args.rest else DEFAULT_TASK
    run = _post_run({"task": task})
    _consume_stream(run)


def cmd_shell(args):
    endpoint = need("endpoint")
    cmd = " ".join(args.rest) if args and args.rest else "echo specify a command"
    req = urllib.request.Request(f"https://{endpoint}/", data=cmd.encode(), headers=_hdr("9000"), method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read())
    print(out.get("stdout", ""), end="")
    print(out.get("stderr", ""), end="")


def cmd_logs(_):
    logs = boto3.client("logs", region_name=REGION)
    group = f"/aws/lambda-microvms/{IMAGE_NAME}"
    streams = logs.describe_log_streams(logGroupName=group, orderBy="LastEventTime", descending=True, limit=1).get("logStreams", [])
    if not streams:
        sys.exit(f"no log streams in {group} yet")
    for e in logs.get_log_events(logGroupName=group, logStreamName=streams[0]["logStreamName"], limit=200, startFromHead=False)["events"]:
        print(e["message"].rstrip())


def _simple(method, label):
    def fn(_):
        mid = need("microvm_id")
        getattr(mv(), method)(microvmIdentifier=mid)
        print(f"==> {label}: {mid}")
    return fn


def cmd_clean(_):
    """Tear down the VM + image only (cheap, frequent). Leaves the connector/cache
    so you don't re-wait ~15min for ElastiCache. Use 'net-clean' for that infra."""
    s = state_load()
    if s.get("microvm_id"):
        try:
            mv().terminate_microvm(microvmIdentifier=s["microvm_id"])
            print("    terminated")
        except ClientError as e:
            print(f"    (terminate skipped: {e})")
    try:
        mv().delete_microvm_image(imageIdentifier=image_arn())
        print("    image deleted")
    except ClientError as e:
        print(f"    (image delete skipped: {e})")
    for k in ("microvm_id", "endpoint", "image_arn", "image_version", "token"):
        s.pop(k, None)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    print("==> cleaned (connector/cache kept — run net-clean to remove)")


def cmd_net_clean(_):
    """Tear down the billable infra: ElastiCache cluster, connector, SGs, operator role."""
    s = state_load()
    try:
        ecache().delete_replication_group(ReplicationGroupId=CACHE_CLUSTER_ID)
        print("    cache deleting (wait for it before SG delete)")
        ecache().get_waiter("replication_group_deleted").wait(ReplicationGroupId=CACHE_CLUSTER_ID)
    except ClientError as e:
        print(f"    (cache delete skipped: {e})")
    try:
        ecache().delete_cache_subnet_group(CacheSubnetGroupName=CACHE_SUBNET_GROUP)
    except ClientError as e:
        print(f"    (subnet group skipped: {e})")
    if s.get("connector_arn"):
        try:
            core().delete_network_connector(Identifier=s["connector_arn"])
            print("    connector deleting")
            while True:
                try:
                    core().get_network_connector(Identifier=s["connector_arn"])
                    time.sleep(10)
                except ClientError:
                    break
        except ClientError as e:
            print(f"    (connector delete skipped: {e})")
    # NAT teardown (stops the hourly NAT charge): NAT gw, EIP, private subnets, route table.
    if s.get("nat_gw"):
        try:
            ec2().delete_nat_gateway(NatGatewayId=s["nat_gw"])
            print("    NAT deleting; waiting")
            ec2().get_waiter("nat_gateway_deleted").wait(NatGatewayIds=[s["nat_gw"]])
        except ClientError as e:
            print(f"    (NAT delete skipped: {e})")
    if s.get("eip_alloc"):
        try:
            ec2().release_address(AllocationId=s["eip_alloc"])
        except ClientError as e:
            print(f"    (EIP release skipped: {e})")
    for sn in s.get("private_subnets", []):
        try:
            ec2().delete_subnet(SubnetId=sn)
        except ClientError as e:
            print(f"    (subnet {sn} skipped: {e})")
    if s.get("private_rt"):
        try:
            ec2().delete_route_table(RouteTableId=s["private_rt"])
        except ClientError as e:
            print(f"    (route table skipped: {e})")
    for sg in (s.get("cache_sg"), s.get("vm_sg")):
        if sg:
            try:
                ec2().delete_security_group(GroupId=sg)
            except ClientError as e:
                print(f"    (sg {sg} skipped: {e})")
    iam = boto3.client("iam")
    try:
        iam.delete_role_policy(RoleName=CONNECTOR_ROLE_NAME, PolicyName="manage-eni")
        iam.delete_role(RoleName=CONNECTOR_ROLE_NAME)
    except ClientError as e:
        print(f"    (operator role skipped: {e})")
    REDIS_HOST_FILE.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    print("==> net-clean done")


COMMANDS = {
    "check": cmd_check, "prereqs": cmd_prereqs, "net-setup": cmd_net_setup, "add-nat": cmd_add_nat,
    "package": cmd_package, "build": cmd_build,
    "wait-image": cmd_wait_image, "run": cmd_run, "wait": cmd_wait, "token": cmd_token, "probe": cmd_probe,
    "demo-stream": cmd_demo_stream, "agent-stream": cmd_agent_stream, "shell": cmd_shell, "logs": cmd_logs,
    "suspend": _simple("suspend_microvm", "suspended"), "resume": _simple("resume_microvm", "resumed"),
    "terminate": _simple("terminate_microvm", "terminated"), "clean": cmd_clean, "net-clean": cmd_net_clean,
}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=list(COMMANDS))
    p.add_argument("rest", nargs="*")
    args = p.parse_args()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
