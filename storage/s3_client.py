"""Helpers for configuring an S3 client from environment variables."""
from __future__ import annotations

import os
from typing import List, Mapping, Sequence

import boto3

REQUIRED_ENV_VARS: Sequence[str] = (
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
)


def _env_mapping(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    if env is None:
        return os.environ
    return env


def missing_s3_env_vars(env: Mapping[str, str] | None = None) -> List[str]:
    """Return a list of S3 env vars that are missing or empty."""
    current = _env_mapping(env)
    return [name for name in REQUIRED_ENV_VARS if not current.get(name)]


def is_s3_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True if all required S3 environment variables are present."""
    return not missing_s3_env_vars(env)


def create_s3_client(env: Mapping[str, str] | None = None):
    """Create a boto3 S3 client using environment configuration."""
    current = _env_mapping(env)
    endpoint_url = current.get("S3_ENDPOINT_URL")
    region_name = current.get("S3_REGION")
    access_key = current.get("S3_ACCESS_KEY")
    secret_key = current.get("S3_SECRET_KEY")

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


__all__ = [
    "REQUIRED_ENV_VARS",
    "create_s3_client",
    "is_s3_enabled",
    "missing_s3_env_vars",
]
