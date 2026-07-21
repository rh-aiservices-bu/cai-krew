"""
Download nomic-embed-text-v1.5 model files from the team S3 bucket.
Run this on the host (not inside a container) before building the modelcar image.

Usage:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_S3_ENDPOINT=...
    export AWS_S3_BUCKET=...
    pip install boto3
    python3 download_host.py
"""
import os, boto3, urllib3
from botocore.config import Config

urllib3.disable_warnings()

AWS_ACCESS_KEY_ID     = os.environ['AWS_ACCESS_KEY_ID']
AWS_SECRET_ACCESS_KEY = os.environ['AWS_SECRET_ACCESS_KEY']
AWS_S3_ENDPOINT       = os.environ['AWS_S3_ENDPOINT']
BUCKET                = os.environ['AWS_S3_BUCKET']
PREFIX                = 'nomic-ai/nomic-embed-text-v1.5/'
DEST_DIR              = os.path.join(os.path.dirname(__file__), 'model')

s3 = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    endpoint_url=AWS_S3_ENDPOINT,
    region_name='us-east-1',
    verify=False,
    config=Config(s3={'addressing_style': 'path'}, signature_version='s3v4'),
)

paginator = s3.get_paginator('list_objects_v2')
for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX):
    for obj in page.get('Contents', []):
        key = obj['Key']
        rel = key[len(PREFIX):]
        dest = os.path.join(DEST_DIR, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size_mb = obj['Size'] / 1024 ** 2
        print(f"Downloading {rel} ({size_mb:.1f} MB)...", flush=True)
        s3.download_file(BUCKET, key, dest)

print("Done — model files are in ./model/")
