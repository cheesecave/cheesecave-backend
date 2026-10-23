import os

import boto3
from botocore.config import Config

s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], aws_access_key_id=os.environ["S3_ACCESS_KEY"], aws_secret_access_key=os.environ["S3_SECRET_KEY"],
                  region_name="us-east-1", config=Config(signature_version="s3v4"))
n = 0
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=os.environ.get("S3_BUCKET", "hub-storage"), Prefix="spike/"):
    for o in page.get("Contents", []):
        s3.delete_object(Bucket=os.environ.get("S3_BUCKET", "hub-storage"), Key=o["Key"])
        n += 1
for u in s3.list_multipart_uploads(Bucket=os.environ.get("S3_BUCKET", "hub-storage"), Prefix="spike/").get("Uploads", []):
    s3.abort_multipart_upload(Bucket=os.environ.get("S3_BUCKET", "hub-storage"), Key=u["Key"], UploadId=u["UploadId"])
print("deleted objects:", n)
