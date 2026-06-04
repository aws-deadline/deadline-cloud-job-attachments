#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Benchmark small-file upload and download throughput through job-attachments.

Creates N tiny files locally, uploads them via S3AssetManager (the same path
production uses), then downloads them via download_files_from_manifests, and
prints timings/throughput for each phase.

Usage:
    # Use existing bucket
    python scripts/benchmark_small_files.py --s3-bucket my-bucket

    # Or create a temp bucket for the run (deleted afterwards)
    python scripts/benchmark_small_files.py

Optional:
    --s3-bucket NAME       Use existing bucket (otherwise create+delete a temp bucket)
    --root-prefix STR      Job attachments rootPrefix (default: DeadlineCloud)
    --farm-id STR          (default: bench-farm)
    --queue-id STR         (default: bench-queue)
    --file-count 10000     Number of files to create (default: 10000)
    --file-size 1024       Bytes per file (default: 1024)
    --region us-west-2     AWS region for the boto3 session
    --keep-s3              Skip S3 cleanup at the end (and skip bucket deletion if we created it)
"""

import argparse
import secrets
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from deadline.job_attachments.asset_manifests.base_manifest import BaseAssetManifest
from deadline.job_attachments.download import download_files_from_manifests
from deadline.job_attachments.models import JobAttachmentS3Settings
from deadline.job_attachments.upload import S3AssetManager


def create_files(root: Path, count: int, size: int) -> None:
    """Create `count` files of `size` bytes each under `root`, with unique content."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        # Unique content so each file gets its own CAS object — exercises N PUTs / N GETs.
        # 8 random bytes prefix + filler; cheaper than secrets for the whole payload.
        prefix = secrets.token_bytes(min(8, size))
        filler = b"a" * max(0, size - len(prefix))
        (root / f"f_{i:07d}").write_bytes(prefix + filler)


def fmt_rate(count: int, seconds: float) -> str:
    if seconds <= 0:
        return "n/a"
    return f"{count / seconds:.1f}/s"


def fmt_bytes_rate(total_bytes: int, seconds: float) -> str:
    if seconds <= 0:
        return "n/a"
    mb = total_bytes / (1024 * 1024)
    return f"{mb / seconds:.2f} MB/s"


def create_bucket(s3_client, region: str) -> str:
    bucket_name = f"ja-bench-{uuid.uuid4().hex[:16]}"
    kwargs = {"Bucket": bucket_name}
    # us-east-1 is the default and rejects LocationConstraint; everywhere else needs it.
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3_client.create_bucket(**kwargs)
    s3_client.get_waiter("bucket_exists").wait(Bucket=bucket_name)
    print(f"Created temporary bucket: {bucket_name}")
    return bucket_name


def empty_and_delete_bucket(s3_client, bucket: str) -> None:
    print(f"Emptying bucket {bucket}...")
    paginator = s3_client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objs = []
        for v in page.get("Versions", []) or []:
            objs.append({"Key": v["Key"], "VersionId": v["VersionId"]})
        for m in page.get("DeleteMarkers", []) or []:
            objs.append({"Key": m["Key"], "VersionId": m["VersionId"]})
        for i in range(0, len(objs), 1000):
            s3_client.delete_objects(
                Bucket=bucket, Delete={"Objects": objs[i : i + 1000], "Quiet": True}
            )
    s3_client.delete_bucket(Bucket=bucket)
    print(f"Deleted bucket: {bucket}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--s3-bucket", default=None, help="Existing bucket; otherwise a temp bucket is created"
    )
    parser.add_argument("--root-prefix", default="DeadlineCloud")
    parser.add_argument("--farm-id", default="bench-farm")
    parser.add_argument("--queue-id", default="bench-queue")
    parser.add_argument("--file-count", type=int, default=10_000)
    parser.add_argument("--file-size", type=int, default=1024)
    parser.add_argument("--region", default=None, help="AWS region (otherwise from env/profile)")
    parser.add_argument(
        "--keep-s3", action="store_true", help="Skip S3 cleanup (and skip temp-bucket deletion)"
    )
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region) if args.region else boto3.Session()
    s3_client = session.client("s3")
    region = session.region_name

    bucket_was_created = False
    bucket_name = args.s3_bucket
    if bucket_name is None:
        if not region:
            print(
                "ERROR: --region or AWS_REGION/profile region required to create a temp bucket",
                file=sys.stderr,
            )
            return 1
        bucket_name = create_bucket(s3_client, region)
        bucket_was_created = True

    s3_settings = JobAttachmentS3Settings(
        s3BucketName=bucket_name,
        rootPrefix=args.root_prefix,
    )

    asset_manager = S3AssetManager(
        farm_id=args.farm_id,
        queue_id=args.queue_id,
        job_attachment_settings=s3_settings,
        session=session,
    )

    work_dir = Path(tempfile.mkdtemp(prefix="ja-bench-"))
    upload_root = work_dir / "src"
    download_root = work_dir / "dst"
    hash_cache_dir = work_dir / "hash-cache"
    s3_check_cache_dir = work_dir / "s3-check-cache"
    manifest_write_dir = work_dir / "manifests"
    for d in (hash_cache_dir, s3_check_cache_dir, manifest_write_dir, download_root):
        d.mkdir(parents=True, exist_ok=True)

    total_bytes = args.file_count * args.file_size
    print(f"Working dir: {work_dir}")
    print(f"Files: {args.file_count} x {args.file_size}B = {total_bytes / 1024:.1f} KiB total\n")

    uploaded_cas_keys: list[str] = []
    uploaded_manifest_keys: list[str] = []

    try:
        # Create local files
        t0 = time.perf_counter()
        create_files(upload_root, args.file_count, args.file_size)
        t_create = time.perf_counter() - t0
        print(f"[create] {t_create:.2f}s  {fmt_rate(args.file_count, t_create)}")

        input_paths = [str(p) for p in sorted(upload_root.iterdir())]

        # Group + plan
        upload_group = asset_manager.prepare_paths_for_upload(
            input_paths=input_paths,
            output_paths=[str(upload_root)],
            referenced_paths=[],
        )

        # Hash + manifest
        t0 = time.perf_counter()
        _, asset_root_manifests = asset_manager.hash_assets_and_create_manifest(
            asset_groups=upload_group.asset_groups,
            total_input_files=upload_group.total_input_files,
            total_input_bytes=upload_group.total_input_bytes,
            hash_cache_dir=str(hash_cache_dir),
        )
        t_hash = time.perf_counter() - t0
        print(
            f"[hash]   {t_hash:.2f}s  {fmt_rate(args.file_count, t_hash)}  {fmt_bytes_rate(total_bytes, t_hash)}"
        )

        # Upload
        t0 = time.perf_counter()
        upload_summary, _attachments = asset_manager.upload_assets(
            manifests=asset_root_manifests,
            s3_check_cache_dir=str(s3_check_cache_dir),
            manifest_write_dir=str(manifest_write_dir),
        )
        t_upload = time.perf_counter() - t0
        print(
            f"[upload] {t_upload:.2f}s  {fmt_rate(args.file_count, t_upload)}  {fmt_bytes_rate(total_bytes, t_upload)}"
        )

        # Build manifests-by-root for download
        manifests_by_root: dict[str, BaseAssetManifest] = {}
        for arm in asset_root_manifests:
            if arm.asset_manifest is None:
                continue
            manifests_by_root[str(download_root)] = arm.asset_manifest

        if not manifests_by_root:
            print("ERROR: no manifest produced; nothing to download", file=sys.stderr)
            return 1

        # Track keys for cleanup
        cas_prefix = s3_settings.full_cas_prefix()
        for manifest in manifests_by_root.values():
            for entry in manifest.paths:
                uploaded_cas_keys.append(f"{cas_prefix}/{entry.hash}.{manifest.hashAlg}")

        # Track manifest objects under farm/queue Inputs prefix for cleanup
        manifest_prefix = s3_settings.partial_manifest_prefix(args.farm_id, args.queue_id)
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=bucket_name, Prefix=f"{args.root_prefix}/Manifests/{manifest_prefix}"
        ):
            for obj in page.get("Contents", []):
                uploaded_manifest_keys.append(obj["Key"])

        # Download
        t0 = time.perf_counter()
        download_summary = download_files_from_manifests(
            s3_bucket=bucket_name,
            manifests_by_root=manifests_by_root,
            cas_prefix=cas_prefix,
            session=session,
        )
        t_download = time.perf_counter() - t0
        print(
            f"[dwnld]  {t_download:.2f}s  {fmt_rate(args.file_count, t_download)}  {fmt_bytes_rate(total_bytes, t_download)}"
        )

        print()
        print(
            f"upload summary:   {upload_summary.processed_files} files / {upload_summary.processed_bytes} bytes"
        )
        print(
            f"download summary: {download_summary.processed_files} files / {download_summary.processed_bytes} bytes"
        )
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

        if args.keep_s3:
            print("\n--keep-s3: leaving S3 objects (and bucket, if temp) in place")
        else:
            if bucket_was_created:
                # We made the bucket — wipe everything in it and drop the bucket itself.
                try:
                    empty_and_delete_bucket(s3_client, bucket_name)
                except ClientError as e:
                    print(f"WARN: bucket cleanup failed: {e}", file=sys.stderr)
            else:
                keys_to_delete = list({*uploaded_cas_keys, *uploaded_manifest_keys})
                if keys_to_delete:
                    print(f"\nDeleting {len(keys_to_delete)} S3 objects...")
                    for i in range(0, len(keys_to_delete), 1000):
                        batch = keys_to_delete[i : i + 1000]
                        s3_client.delete_objects(
                            Bucket=bucket_name,
                            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
                        )


if __name__ == "__main__":
    sys.exit(main())
