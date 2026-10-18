"""Private S3 storage adapter (SigV4, path-style; never returns public URLs)."""

import os
from functools import lru_cache

import boto3
from botocore.config import Config


class ObjectStorage:
    def __init__(self):
        self.backend = os.getenv("STORAGE_BACKEND", "s3")
        if self.backend != "s3":
            raise RuntimeError("Only the configured private S3 storage backend is supported")
        self.bucket = os.environ["S3_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
            region_name=os.getenv("S3_REGION", "us-east-1"),
            aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def put(self, key, stream, size, content_type):
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=stream, ContentLength=size, ContentType=content_type
        )

    def head(self, key):
        return self.client.head_object(Bucket=self.bucket, Key=key)

    def get(self, key, start=None, end=None):
        args = {"Bucket": self.bucket, "Key": key}
        if start is not None:
            args["Range"] = f"bytes={start}-{end}"
        return self.client.get_object(**args)

    def check(self):
        self.client.head_bucket(Bucket=self.bucket)

    def create_bucket(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except self.client.exceptions.ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            self.client.create_bucket(Bucket=self.bucket)


@lru_cache(maxsize=1)
def get_storage():
    return ObjectStorage()
