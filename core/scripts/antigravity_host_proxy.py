from __future__ import annotations

import argparse
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8765


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class Handler(BaseHTTPRequestHandler):
    server_version = "AntigravityHostProxy/1.0"

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
                rc, out, err = _run(['agy', 'models'], 60)
                models = [line.strip() for line in out.splitlines() if line.strip()]
                self._send(200, {'ok': rc == 0, 'models': models, 'stdout': out, 'stderr': err, 'exit_code': rc})
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
            if not prompt:
                self._send(400, {'ok': False, 'error': 'prompt_required'})
                return
            rc, out, err = _run(['agy', '-p', prompt, '--print-timeout', f'{max(5, timeout)}s'], max(10, timeout + 10))
            self._send(200, {'ok': rc == 0, 'stdout': out, 'stderr': err, 'exit_code': rc})
        except Exception as exc:
            self._send(500, {'ok': False, 'error': str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description='Host proxy for authorized Antigravity CLI')
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
