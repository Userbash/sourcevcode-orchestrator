import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000").rstrip("/")
BIND_HOST = os.getenv("MODEL_REGISTRY_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("MODEL_REGISTRY_PORT", "8090"))
TIMEOUT_SEC = float(os.getenv("MODEL_REGISTRY_TIMEOUT_SEC", "5"))


def fetch_json(path: str):
    url = ORCHESTRATOR_URL + path
    with urlopen(url, timeout=TIMEOUT_SEC) as response:
        return json.loads(response.read().decode("utf-8"))


def registry_payload():
    started = time.time()
    payload = {
        "status": "ok",
        "runtime": "go-core",
        "orchestrator_url": ORCHESTRATOR_URL,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    payload["provider_inventory"] = fetch_json("/providers/runtime_inventory")
    payload["model_index"] = fetch_json("/providers/models/index")
    payload["local_model_health"] = fetch_json("/health/local_models")
    payload["ai_kernel_gate"] = fetch_json("/providers/ai_kernel/gate")
    payload["duration_ms"] = int((time.time() - started) * 1000)
    return payload


class Handler(BaseHTTPRequestHandler):
    def _write(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            if self.path in ("/", "/registry", "/models"):
                self._write(200, registry_payload())
                return
            if self.path == "/health":
                self._write(200, {"status": "ok", "service": "model_registry"})
                return
            self._write(404, {"status": "not_found", "path": self.path})
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            self._write(503, {
                "status": "degraded",
                "service": "model_registry",
                "orchestrator_url": ORCHESTRATOR_URL,
                "error": str(exc),
            })

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    HTTPServer((BIND_HOST, BIND_PORT), Handler).serve_forever()
