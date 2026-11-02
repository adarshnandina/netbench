# Image for the NetBench measurement server (UDP echo + TCP tests on port 9000).
# The server uses only the Python standard library, so nothing is pip-installed.
FROM python:3.13-slim

WORKDIR /app
COPY netbench/ netbench/

# Print log lines immediately instead of buffering them (so `docker compose logs` shows them).
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "netbench.server", "--host", "0.0.0.0", "--port", "9000"]
