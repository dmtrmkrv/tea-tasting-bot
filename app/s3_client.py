import os, mimetypes, uuid
import boto3
from botocore.client import Config
S3_ENDPOINT_URL=os.getenv("S3_ENDPOINT_URL")
S3_REGION=os.getenv("S3_REGION","ru-1")
S3_ACCESS_KEY=os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY=os.getenv("S3_SECRET_KEY")
S3_BUCKET=os.getenv("S3_BUCKET")
if not all([S3_ENDPOINT_URL,S3_ACCESS_KEY,S3_SECRET_KEY,S3_BUCKET]):
    raise RuntimeError("S3 env vars are not set")
_s3=boto3.session.Session().client("s3", endpoint_url=S3_ENDPOINT_URL, region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY, aws_secret_access_key=S3_SECRET_KEY, config=Config(s3={"addressing_style":"virtual"}))
def _guess_ct(name): return mimetypes.guess_type(name)[0] or "application/octet-stream"
def _key(name,prefix="photos"): return f"{prefix}/{uuid.uuid4().hex}{(os.path.splitext(name)[1] or '.jpg').lower()}"
def upload_bytes(data:bytes, filename:str, content_type:str|None=None)->str:
    key=_key(filename); _s3.put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type or _guess_ct(filename), ACL="private"); return key
def get_presigned_url(key:str, expires:int=3600)->str:
    return _s3.generate_presigned_url("get_object", Params={"Bucket":S3_BUCKET,"Key":key}, ExpiresIn=expires)
def delete_object(key:str)->None: _s3.delete_object(Bucket=S3_BUCKET, Key=key)
