FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mem0_client.py prompts.py pruner.py run.py ./

CMD ["python", "run.py"]
