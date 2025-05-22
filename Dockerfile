# syntax=docker/dockerfile:1
FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py ./

# Make `python app.py [flags]` the default container command
ENTRYPOINT ["python", "app.py"]
