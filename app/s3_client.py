import mimetypes
import os
import uuid

import boto3
from botocore.config import Config


S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
S3_REGION = os.getenv("S3_REGION", "ru-1")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET")
S3_PATH_STYLE = os.getenv("S3_PATH_STYLE", "").lower() == "true"

if not all([S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET]):
    raise RuntimeError("S3 env vars are not set")

config = Config(s3={"addressing_style": "path"}) if S3_PATH_STYLE else None

session = boto3.session.Session()
client_kwargs = dict(
    endpoint_url=S3_ENDPOINT_URL,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
)
if config is not None:
    client_kwargs["config"] = config

_s3 = session.client("s3", **client_kwargs)


def _guess_ct(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _key(name: str, prefix: str = "photos") -> str:
    extension = (os.path.splitext(name)[1] or ".jpg").lower()
    return f"{prefix}/{uuid.uuid4().hex}{extension}"


def upload_bytes(data: bytes, filename: str, content_type: str | None = None) -> str:
    key = _key(filename)
    _s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type or _guess_ct(filename),
        ACL="private",
    )
    return key


def get_presigned_url(key: str, expires: int = 3600) -> str:
    return _s3.generate_presigned_url(
        "get_object", Params={"Bucket":S3_BUCKET, "Key":key}, ExpiresIn=expires
    )


def delete_object(key: str) -> None:
    _s3.delete_object(Bucket=S3_BUCKET, Key=key)
