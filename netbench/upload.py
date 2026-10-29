"""Upload saved runs to the NetBench backend.

The client always saves a run locally FIRST and uploads second. The network
being tested may be bad, and a measurement must never be lost just because
the upload failed. Anything still in results/*.json is "pending"; after a
successful upload the file moves to results/uploaded/.

Configure with environment variables:
  export NETBENCH_API_URL=http://localhost:8000
  export NETBENCH_API_KEY=<the key from your .env>

Then retry everything pending with:
  python -m netbench.upload
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def api_config():
    url, key = os.environ.get("NETBENCH_API_URL"), os.environ.get("NETBENCH_API_KEY")
    return (url.rstrip("/"), key) if url and key else None


def post_run(record, url, key, timeout_s=10):
    request = urllib.request.Request(
        f"{url}/runs",
        data=json.dumps(record).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)["status"]  # "stored" or "already stored"


def api_get(path, params=None, timeout_s=10):
    """GET a JSON endpoint of the backend, e.g. api_get("/runs", {"limit": 20})."""
    url, key = api_config()
    query = "?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}) if params else ""
    request = urllib.request.Request(f"{url}{path}{query}", headers={"X-API-Key": key})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.load(response)


def upload_pending(results_dir="results", log=print):
    """Upload every pending run. `log` receives one message per file (print by default)."""
    config = api_config()
    if config is None:
        log("Upload skipped: set NETBENCH_API_URL and NETBENCH_API_KEY to enable it.")
        return
    url, key = config
    done_dir = Path(results_dir) / "uploaded"
    done_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(Path(results_dir).glob("*.json")):
        try:
            status = post_run(json.loads(path.read_text()), url, key)
        except urllib.error.HTTPError as e:
            # The backend answered but refused (bad key, invalid record): retrying won't help.
            log(f"{path.name}: rejected with HTTP {e.code}: {e.read().decode()[:300]}")
            continue
        except (urllib.error.URLError, OSError) as e:
            # Backend unreachable: keep everything pending and try again later.
            log(f"Backend unreachable ({e}); {path.name} and later files stay pending.")
            return
        path.rename(done_dir / path.name)
        log(f"{path.name}: {status}")


if __name__ == "__main__":
    upload_pending()
