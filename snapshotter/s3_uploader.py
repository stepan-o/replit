import os
import json
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from snapshotter.utils import sha256_bytes


def get_s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def upload_file(
    s3_client,
    local_path: str,
    bucket: str,
    s3_key: str,
    content_type: Optional[str] = None,
) -> dict:
    with open(local_path, "rb") as f:
        data = f.read()

    sha256 = sha256_bytes(data)
    extra_args = {"ServerSideEncryption": "AES256"}
    if content_type:
        extra_args["ContentType"] = content_type

    s3_client.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=data,
        **extra_args,
    )

    return {
        "s3_key": s3_key,
        "size_bytes": len(data),
        "sha256": sha256,
    }


def upload_bytes(
    s3_client,
    data: bytes,
    bucket: str,
    s3_key: str,
    content_type: Optional[str] = None,
) -> dict:
    sha256 = sha256_bytes(data)
    extra_args = {"ServerSideEncryption": "AES256"}
    if content_type:
        extra_args["ContentType"] = content_type

    s3_client.put_object(
        Bucket=bucket,
        Key=s3_key,
        Body=data,
        **extra_args,
    )

    return {
        "s3_key": s3_key,
        "size_bytes": len(data),
        "sha256": sha256,
    }


def upload_json(
    s3_client,
    obj: dict,
    bucket: str,
    s3_key: str,
) -> dict:
    data = json.dumps(obj, indent=2, default=str).encode("utf-8")
    return upload_bytes(s3_client, data, bucket, s3_key, content_type="application/json")
