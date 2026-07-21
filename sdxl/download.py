import os, time, subprocess, boto3, urllib3
from botocore.config import Config
urllib3.disable_warnings()

KEY_ID     = os.environ['AWS_ACCESS_KEY_ID']
SECRET     = os.environ['AWS_SECRET_ACCESS_KEY']
ENDPOINT   = os.environ['AWS_S3_ENDPOINT']
BUCKET     = os.environ['AWS_S3_BUCKET']
S3_KEY     = 'stabilityai/stable-diffusion-xl-base-1.0/sd_xl_base_1.0.safetensors'
DEST       = '/tmp/sd_xl_base_1.0.safetensors'
TOTAL_SIZE = 6938078334
CHUNK      = 512 * 1024  # 512 KB — safely below any observed drop threshold

def make_client():
    return boto3.client(
        's3',
        aws_access_key_id=KEY_ID,
        aws_secret_access_key=SECRET,
        endpoint_url=ENDPOINT,
        region_name='us-east-1',
        verify=False,
        config=Config(s3={'addressing_style': 'path'}, signature_version='s3v4'),
    )

offset = os.path.getsize(DEST) if os.path.exists(DEST) else 0
print(f"Starting from offset {offset} / {TOTAL_SIZE}", flush=True)

with open(DEST, 'ab') as f:
    while offset < TOTAL_SIZE:
        end = min(offset + CHUNK - 1, TOTAL_SIZE - 1)
        s3 = make_client()
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET, 'Key': S3_KEY},
            ExpiresIn=300,
        )
        for attempt in range(20):
            try:
                result = subprocess.run(
                    ['curl', '-fsSL', '-k', '--http1.1', '--max-time', '30',
                     '-H', f'Range: bytes={offset}-{end}',
                     '-o', '/tmp/_chunk', url],
                    capture_output=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode())
                with open('/tmp/_chunk', 'rb') as chunk:
                    data = chunk.read()
                f.write(data)
                f.flush()
                offset += len(data)
                if offset % (100 * 1024 * 1024) < CHUNK:
                    pct = offset / TOTAL_SIZE * 100
                    print(f"  {offset/1024**3:.2f} GB ({pct:.1f}%)", flush=True)
                break
            except Exception as e:
                print(f"  Error at {offset}: {e} — retry {attempt+1}/20", flush=True)
                time.sleep(3)
        else:
            raise RuntimeError(f"Failed after 20 attempts at offset {offset}")

print("Download complete.", flush=True)
