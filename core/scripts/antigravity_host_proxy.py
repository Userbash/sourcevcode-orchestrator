from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

HOST = "0.0.0.0"
PORT = 8765


def _normalize_base_url(url: str) -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return ""
    if raw.startswith("ws://"):
        return "http://" + raw[5:]
    if raw.startswith("wss://"):
        return "https://" + raw[6:]
    return raw


def _api_base_url() -> str:
    return _normalize_base_url(os.getenv("AI_BRIDGE_ANTIGRAVITY_API_BASE_URL", os.getenv("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")))


def _api_key() -> str:
    return (os.getenv("ANTIGRAVITY_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def _headers(base_url: str) -> dict[str, str]:
    key = _api_key()
    if not key:
        return {}
    if "generativelanguage.googleapis.com" in base_url:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _params(base_url: str) -> dict[str, str]:
    key = _api_key()
    if key and "generativelanguage.googleapis.com" in base_url:
        return {"key": key}
    return {}


def _request(method: str, path: str, *, json_body: dict | None = None, timeout: float = 30.0) -> httpx.Response:
    base_url = _api_base_url()
    if not base_url:
        raise RuntimeError("antigravity_api_base_url_missing")
    url = f"{base_url}/{path.lstrip('/')}"
    return httpx.request(method, url, headers=_headers(base_url), params=_params(base_url) or None, json=json_body, timeout=timeout)


def _models_from_payload(payload: dict) -> list[str]:
    models: list[str] = []
    rows = payload.get('models', []) if isinstance(payload, dict) else []
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict):
                name = str(item.get('name') or item.get('id') or item.get('model') or '').strip()
            else:
                name = str(item).strip()
            if name:
                models.append(name.rsplit('/', 1)[-1])
    seen: set[str] = set()
    out: list[str] = []
    for model in models:
        if model and model not in seen:
            seen.add(model)
            out.append(model)
    return out


def _generation_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ('stdout', 'text', 'output', 'response'):
        value = str(payload.get(key) or '').strip()
        if value:
            return value
    candidates = payload.get('candidates')
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get('content')
            if not isinstance(content, dict):
                continue
            parts = content.get('parts')
            if not isinstance(parts, list):
                continue
            chunks: list[str] = []
            for part in parts:
                if isinstance(part, dict):
                    text = str(part.get('text') or '').strip()
                    if text:
                        chunks.append(text)
            if chunks:
                return '\n'.join(chunks).strip()
    return ''


class Handler(BaseHTTPRequestHandler):
    server_version = "AntigravityHostProxy/2.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):
        return

    def do_GET(self):
        if self.path == '/health':
            self._send(200, {'status': 'ok'})
            return
        if self.path == '/models':
            try:
                response = _request('GET', 'models', timeout=30.0)
                payload = response.json() if response.content else {}
                models = _models_from_payload(payload)
                self._send(200, {'ok': response.status_code == 200, 'models': models, 'stdout': '\n'.join(models), 'stderr': '' if response.status_code == 200 else response.text[:500], 'status_code': response.status_code})
            except Exception as exc:
                self._send(500, {'ok': False, 'models': [], 'error': str(exc)})
            return
        self._send(404, {'ok': False, 'error': 'not_found'})

    def do_POST(self):
        if self.path != '/prompt':
            self._send(404, {'ok': False, 'error': 'not_found'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length) if length > 0 else b'{}'
            payload = json.loads(raw.decode('utf-8') or '{}')
            prompt = str(payload.get('prompt', '')).strip()
            timeout = int(payload.get('timeout_sec', 120))
            model = str(payload.get('model') or os.getenv('ANTIGRAVITY_DEFAULT_MODEL', os.getenv('GEMINI_DEFAULT_MODEL', 'antigravity-flash'))).strip() or 'antigravity-flash'
            if not prompt:
                self._send(400, {'ok': False, 'error': 'prompt_required'})
                return
            response = _request('POST', f'models/{model}:generateContent', json_body={'contents': [{'role': 'user', 'parts': [{'text': prompt}]}]}, timeout=max(10, timeout + 10))
            data = response.json() if response.content else {}
            stdout = _generation_text(data)
            self._send(200, {'ok': response.status_code == 200, 'stdout': stdout, 'stderr': '' if response.status_code == 200 else response.text[:500], 'status_code': response.status_code})
        except Exception as exc:
            self._send(500, {'ok': False, 'error': str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description='Host proxy for token-authenticated Antigravity API access')
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
