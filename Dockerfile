# Use a specific slim Python image
FROM python:3.10-slim-bullseye

WORKDIR /app

# Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app.py .

# Entrypoint
CMD ["python", "app.py"]
