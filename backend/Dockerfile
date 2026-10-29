FROM python:3.13-slim

WORKDIR /app

# Install dependencies first: Docker caches this layer, so editing app.py
# doesn't trigger a full reinstall on the next build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py schema.sql ./

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
