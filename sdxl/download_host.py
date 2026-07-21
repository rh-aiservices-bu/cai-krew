"""
Download sd_xl_base_1.0.safetensors from the team S3 bucket.
Run this on the host (not inside a container) before building the modelcar image.
The file is ~6.5 GB — do not run this inside a container (container networking is flaky with this endpoint).

Usage:
    export AWS_ACCESS_KEY_ID=...
    export AWS_SECRET_ACCESS_KEY=...
    export AWS_S3_ENDPOINT=...
    export AWS_S3_BUCKET=...
    pip install boto3
    python3 download_host.py
"""
import os, time, boto3, urllib3
from botocore.config import Config

urllib3.disable_warnings()

AWS_ACCESS_KEY_ID     = os.environ['AWS_ACCESS_KEY_ID']
AWS_SECRET_ACCESS_KEY = os.environ['AWS_SECRET_ACCESS_KEY']
AWS_S3_ENDPOINT       = os.environ['AWS_S3_ENDPOINT']
BUCKET                = os.environ['AWS_S3_BUCKET']
S3_KEY                = 'stabilityai/stable-diffusion-xl-base-1.0/sd_xl_base_1.0.safetensors'
DEST                  = os.path.join(os.path.dirname(__file__), 'sd_xl_base_1.0.safetensors')
TOTAL_SIZE            = 6938078334
CHUNK                 = 8 * 1024 * 1024  # 8 MB

def make_client():
    return boto3.client(
        's3',
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        endpoint_url=AWS_S3_ENDPOINT,
        region_name='us-east-1',
        verify=False,
        config=Config(s3={'addressing_style': 'path'}, signature_version='s3v4'),
    )

s3 = make_client()
offset = os.path.getsize(DEST) if os.path.exists(DEST) else 0
print(f"Starting from {offset/1024**3:.2f} GB / {TOTAL_SIZE/1024**3:.2f} GB", flush=True)

with open(DEST, 'ab') as f:
    while offset < TOTAL_SIZE:
        end = min(offset + CHUNK - 1, TOTAL_SIZE - 1)
        for attempt in range(10):
            try:
                response = s3.get_object(Bucket=BUCKET, Key=S3_KEY, Range=f'bytes={offset}-{end}')
                data = response['Body'].read()
                f.write(data)
                f.flush()
                offset += len(data)
                break
            except Exception as e:
                print(f"  Error at {offset/1024**2:.0f} MB: {e} — retry {attempt+1}", flush=True)
                time.sleep(2)
                s3 = make_client()
        else:
            raise RuntimeError(f"Failed after 10 attempts at offset {offset}")

        if offset % (500 * 1024 * 1024) < CHUNK:
            print(f"  {offset/1024**3:.2f} GB ({offset/TOTAL_SIZE*100:.0f}%)", flush=True)

print("Done — sd_xl_base_1.0.safetensors is ready.")
