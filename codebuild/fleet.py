"""CodeBuild batch: one amd64 image per harness×industry pair."""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "mivas-bench-image"
BUCKET = "mivas-bench-codebuild"
REGION = "us-west-1"
SERVICE_ROLE = "codebuild-mivas-bench-service"
BATCH_ROLE = "codebuild-mivas-bench-batch"
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "graphify-out",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    "node_modules",
}
EXCLUDE_FILES = {".env", ".DS_Store"}


def _region() -> str:
    return (
        os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or REGION
    )


def _bucket() -> str:
    return os.environ.get("MIVAS_CODEBUILD_BUCKET", BUCKET).strip() or BUCKET


def _project() -> str:
    return os.environ.get("MIVAS_CODEBUILD_PROJECT", PROJECT).strip() or PROJECT


def _prefix() -> str:
    prefix = os.environ.get("MIVAS_IMAGE_PREFIX", "").strip().rstrip("/")
    if not prefix:
        raise ValueError("MIVAS_IMAGE_PREFIX is required for --codebuild")
    return prefix


def _account(sts=None) -> str:
    sts = sts or boto3.client("sts", region_name=_region())
    return sts.get_caller_identity()["Account"]


def _batch_id(harness: str, industry: str) -> str:
    raw = f"{harness}-{industry}".replace("/", "-").replace(".", "-").replace("_", "-")
    ident = "".join(ch if ch.isalnum() else "_" for ch in raw)
    if ident[0].isdigit():
        ident = "p_" + ident
    return ident[:127]


def _ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except ClientError:
        pass
    kwargs: dict = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    print(f"created s3://{bucket}")


def _put_role(iam, name: str, policy: dict, inline_name: str, inline: dict) -> str:
    assume = json.dumps(policy)
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=assume,
            Description="mivas-bench CodeBuild",
        )["Role"]["Arn"]
        print(f"created iam role {name}")
        time.sleep(8)
    iam.put_role_policy(
        RoleName=name,
        PolicyName=inline_name,
        PolicyDocument=json.dumps(inline),
    )
    return arn


def _ensure_roles(account: str, region: str, bucket: str, project: str) -> tuple[str, str]:
    iam = boto3.client("iam")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "codebuild.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    ecr = f"arn:aws:ecr:{region}:{account}:repository/mivas-bench"
    logs = f"arn:aws:logs:{region}:{account}:log-group:/aws/codebuild/{project}*"
    src = f"arn:aws:s3:::{bucket}/*"
    service_inline = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": [logs, f"{logs}:*"],
            },
            {
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:PutImage",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                ],
                "Resource": ecr,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                "Resource": src,
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                "Resource": f"arn:aws:s3:::{bucket}",
            },
        ],
    }
    # Resource "*" is what AWS documents for the batch service role.
    # A scoped project ARN fails StartBuild for DOWNLOAD_SOURCE children.
    batch_inline = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "codebuild:StartBuild",
                    "codebuild:StopBuild",
                    "codebuild:RetryBuild",
                    "codebuild:BatchGetBuilds",
                    "codebuild:BatchGetBuildBatches",
                ],
                "Resource": "*",
            }
        ],
    }
    service_arn = _put_role(iam, SERVICE_ROLE, trust, "mivas-bench-build", service_inline)
    batch_arn = _put_role(iam, BATCH_ROLE, trust, "mivas-bench-batch", batch_inline)
    return service_arn, batch_arn


def _ensure_project(cb, *, service_arn: str, batch_arn: str, bucket: str, prefix: str) -> None:
    region = _region()
    project = _project()
    env = {
        "type": "LINUX_CONTAINER",
        "image": "aws/codebuild/amazonlinux-x86_64-standard:5.0",
        "computeType": "BUILD_GENERAL1_LARGE",
        "privilegedMode": True,
        "environmentVariables": [
            {"name": "MIVAS_IMAGE_PREFIX", "value": prefix, "type": "PLAINTEXT"},
            {"name": "AWS_DEFAULT_REGION", "value": region, "type": "PLAINTEXT"},
        ],
    }
    source = {
        "type": "S3",
        "location": f"{bucket}/src/latest.zip",
        "buildspec": "buildspec.yml",
    }
    batch_cfg = {
        "serviceRole": batch_arn,
        "restrictions": {"maximumBuildsAllowed": 50},
        "timeoutInMins": 60,
    }
    body = {
        "name": project,
        "description": "mivas-bench harness×industry images (amd64)",
        "source": source,
        "artifacts": {"type": "NO_ARTIFACTS"},
        "environment": env,
        "serviceRole": service_arn,
        "timeoutInMinutes": 60,
        "queuedTimeoutInMinutes": 60,
        "cache": {"type": "LOCAL", "modes": ["LOCAL_DOCKER_LAYER_CACHE"]},
        "buildBatchConfig": batch_cfg,
        "concurrentBuildLimit": 40,
    }
    try:
        cb.create_project(**body)
        print(f"created CodeBuild project {project}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        cb.update_project(**body)


def _zip_repo() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if any(part in EXCLUDE_DIRS for part in rel.parts):
                continue
            if path.name in EXCLUDE_FILES or path.name.endswith(".pyc"):
                continue
            if rel.as_posix() == "buildspec.yml":
                continue
            zf.write(path, rel.as_posix())
    return buf.getvalue()


def _batch_buildspec(pairs: list[tuple[str, str]]) -> str:
    phases = (ROOT / "codebuild" / "buildspec.yml").read_text()
    # Drop the child-only header comments; keep version/phases.
    if phases.lstrip().startswith("version:"):
        child = phases
    else:
        idx = phases.find("\nversion:")
        child = phases[idx + 1 :] if idx >= 0 else phases
    lines = [
        "version: 0.2",
        "batch:",
        "  fast-fail: false",
        "  build-list:",
    ]
    seen: set[str] = set()
    for harness, industry in pairs:
        ident = _batch_id(harness, industry)
        if ident in seen:
            ident = ident[:120] + "_x"
        seen.add(ident)
        lines += [
            f"    - identifier: {ident}",
            "      env:",
            "        variables:",
            f"          HARNESS: {harness}",
            f"          INDUSTRY: {industry}",
        ]
    # Child phases from codebuild/buildspec.yml (skip its version: line).
    rest = child.split("version: 0.2", 1)[-1].lstrip("\n")
    return "\n".join(lines) + "\n" + rest


def _upload_source(s3, bucket: str, pairs: list[tuple[str, str]]) -> str:
    key = f"src/{int(time.time())}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        raw = _zip_repo()
        with zipfile.ZipFile(io.BytesIO(raw)) as src:
            for info in src.infolist():
                zf.writestr(info, src.read(info.filename))
        zf.writestr("buildspec.yml", _batch_buildspec(pairs))
    body = buf.getvalue()
    s3.put_object(Bucket=bucket, Key=key, Body=body)
    s3.put_object(Bucket=bucket, Key="src/latest.zip", Body=body)
    print(f"uploaded s3://{bucket}/{key} ({len(body)} bytes, {len(pairs)} pair(s))")
    return key


def start_fleet(pairs: list[tuple[str, str]], *, wait: bool) -> str:
    if not pairs:
        raise ValueError("no pairs to build")
    region = _region()
    bucket = _bucket()
    project = _project()
    prefix = _prefix()
    session_region = region
    s3 = boto3.client("s3", region_name=session_region)
    cb = boto3.client("codebuild", region_name=session_region)
    sts = boto3.client("sts", region_name=session_region)
    account = _account(sts)
    _ensure_bucket(s3, bucket, region)
    service_arn, batch_arn = _ensure_roles(account, region, bucket, project)
    time.sleep(12)
    _ensure_project(
        cb, service_arn=service_arn, batch_arn=batch_arn, bucket=bucket, prefix=prefix
    )
    key = _upload_source(s3, bucket, pairs)
    resp = cb.start_build_batch(
        projectName=project,
        sourceLocationOverride=f"{bucket}/{key}",
        sourceTypeOverride="S3",
        buildspecOverride="buildspec.yml",
        environmentVariablesOverride=[
            {"name": "MIVAS_IMAGE_PREFIX", "value": prefix, "type": "PLAINTEXT"},
        ],
    )
    batch = resp["buildBatch"]
    batch_id = batch["id"]
    print(f"CodeBuild batch {batch_id} ({len(pairs)} images, linux/amd64)")
    print(
        f"https://{_region()}.console.aws.amazon.com/codesuite/codebuild/projects/{project}/batch/{batch_id}"
    )
    if wait:
        _wait(cb, batch_id)
    return batch_id


def _wait(cb, batch_id: str) -> None:
    terminal = {
        "SUCCEEDED",
        "FAILED",
        "FAULT",
        "STOPPED",
        "TIMED_OUT",
        "STOPPED_BATCH",
    }
    while True:
        info = cb.batch_get_build_batches(ids=[batch_id])["buildBatches"][0]
        status = info["buildBatchStatus"]
        groups = info.get("buildGroups") or []
        done = sum(
            1
            for g in groups
            for s in (g.get("currentBuildSummary") or {},)
            if (s.get("buildStatus") or "") in terminal
        )
        print(f"batch {status}  groups={len(groups)}", flush=True)
        if status in terminal:
            if status != "SUCCEEDED":
                for phase in info.get("phases") or []:
                    ctxs = phase.get("contexts") or []
                    msg = (ctxs[0].get("message") if ctxs else "") or ""
                    if phase.get("phaseStatus") not in (None, "SUCCEEDED"):
                        print(
                            f"  {phase.get('phaseType')} {phase.get('phaseStatus')}: {msg}",
                            flush=True,
                        )
                for group in groups:
                    summary = group.get("currentBuildSummary") or {}
                    print(
                        f"  {group.get('identifier')} {summary.get('buildStatus')}",
                        flush=True,
                    )
                raise SystemExit(f"CodeBuild batch {status}: {batch_id}")
            print("CodeBuild batch succeeded")
            return
        time.sleep(20)
