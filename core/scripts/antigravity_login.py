from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import time
import uuid
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.core.integrations.antigravity_session_store import AntigravitySessionStore

AGY_STATE_DIR = Path.home() / ".gemini" / "antigravity-cli"
LEGACY_STATE_DIR = Path.home() / ".antigravity" / "antigravity-cli"
URL_RE = re.compile(r"https?://\S+")
CODE_RE = re.compile(r"\b[A-Z0-9]{6,}\b")
AUTH_PROMPT = "Start Antigravity account authorization. If OAuth is required, show the browser verification URL and wait for the console code."
WAITING_CODE_MARKERS = ("authorization code", "paste the authorization code", "enter code", "verification code", "login code")
WAITING_BROWSER_MARKERS = ("open the following url", "continue in your browser", "sign in", "oauth", "verify")


def _store() -> AntigravitySessionStore:
    return AntigravitySessionStore(state_dir=AGY_STATE_DIR, legacy_state_dir=LEGACY_STATE_DIR)


def _state_markers() -> list[Path]:
    return _store().auth_marker_paths()


def has_auth_marker() -> bool:
    return _store().auth_marker_present()


def _prefer_oauth_cli() -> bool:
    return os.getenv("AI_BRIDGE_ANTIGRAVITY_PREFER_OAUTH", "true").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_cli_command() -> list[str] | None:
    env_bin = os.getenv("ANTIGRAVITY_CLI_BIN", "").strip()
    if env_bin:
        resolved = shutil.which(env_bin) if not Path(env_bin).is_absolute() else env_bin
        if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK):
            return [resolved]

    home = Path.home()
    candidate_paths: list[str] = []
    search_roots = [home]
    var_home = Path("/var/home")
    if var_home.is_dir():
        for user_home in var_home.glob("*"):
            if user_home.is_dir() and user_home not in search_roots:
                search_roots.append(user_home)
    for candidate in ("agy", "antigravity", "gemini"):
        resolved = shutil.which(candidate)
        if resolved:
            candidate_paths.append(resolved)
        for search_root in search_roots:
            candidate_paths.extend([
                str(search_root / ".npm-packages" / "bin" / candidate),
                str(search_root / ".local" / "bin" / candidate),
            ])
    for resolved in candidate_paths:
        if resolved and Path(resolved).is_file() and os.access(resolved, os.X_OK):
            return [resolved]
    return None


def _cli_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    home = env.get("HOME", "")
    extra_bins = []
    if home:
        extra_bins.extend([
            str(Path(home) / ".npm-packages" / "bin"),
            str(Path(home) / ".local" / "bin"),
        ])
    var_home = Path("/var/home")
    if var_home.is_dir():
        for user_home in var_home.glob("*"):
            if user_home.is_dir():
                extra_bins.extend([
                    str(user_home / ".npm-packages" / "bin"),
                    str(user_home / ".local" / "bin"),
                ])
    path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    for part in [*extra_bins, *path_parts]:
        if part and part not in merged:
            merged.append(part)
    env["PATH"] = os.pathsep.join(merged)
    if _prefer_oauth_cli():
        for key in ("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            env.pop(key, None)
    return env


def _translate_cli_args(args: list[str]) -> list[str]:
    cmd_prefix = _resolve_cli_command()
    if not cmd_prefix:
        raise FileNotFoundError("antigravity_cli_not_found")
    cli_name = Path(cmd_prefix[0]).name.lower()
    if cli_name != "gemini":
        return [*cmd_prefix, *args]
    if args[:1] == ["models"]:
        return [*cmd_prefix, "--version"]
    if args[:1] == ["-p"]:
        prompt = args[1] if len(args) > 1 else ""
        return [*cmd_prefix, "-p", prompt, "--skip-trust"]
    if "--prompt-interactive" in args:
        index = args.index("--prompt-interactive")
        prompt = args[index + 1] if index + 1 < len(args) else AUTH_PROMPT
        return [*cmd_prefix, "--prompt-interactive", prompt, "--skip-trust"]
    return [*cmd_prefix, *args]


def _interactive_login_command(log_path: str) -> list[str]:
    return _translate_cli_args(["--log-file", log_path, "--prompt-interactive", AUTH_PROMPT])


def _run_capture(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    translated = _translate_cli_args(cmd)
    proc = subprocess.run(translated, check=False, capture_output=True, text=True, timeout=timeout, env=_cli_runtime_env(), cwd=str(ROOT))
    if cmd[:1] == ["models"] and Path(translated[0]).name.lower() == "gemini" and proc.returncode == 0:
        return subprocess.CompletedProcess(translated, proc.returncode, stdout="antigravity-cli\ngemini-cli\n", stderr=proc.stderr)
    return proc


def _classify_failure_text(raw: str) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return "unknown"
    if "not found" in text or "no such file" in text:
        return "cli_missing"
    if any(marker in text for marker in ["authentication required", "please sign in", "authorization code", "paste the authorization code", "unauthorized", "forbidden", "error: authentication", "error: please sign in"]):
        return "auth_required"
    if any(marker in text for marker in ["timed out", "timeout", "connection reset", "temporarily unavailable", "network", "dns", "econn", "refused", "unreachable"]):
        return "transient"
    return "unknown"


def _probe_models(timeout: int = 60) -> dict[str, Any]:
    proc = _run_capture(["models"], timeout=timeout)
    models = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    error_text = str(proc.stderr or proc.stdout or "")
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "models": models,
        "failure_kind": "" if proc.returncode == 0 else _classify_failure_text(error_text),
    }


def _probe_generation(timeout: int = 90) -> dict[str, Any]:
    proc = _run_capture(["-p", "healthcheck: reply with ok", "--print-timeout", "60s"], timeout=timeout)
    error_text = str(proc.stderr or proc.stdout or "")
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "exit_code": proc.returncode,
        "failure_kind": "" if proc.returncode == 0 else _classify_failure_text(error_text),
    }


def _print_models() -> int:
    proc = _run_capture(["models"], timeout=60)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def _build_report() -> dict[str, Any]:
    models = _probe_models()
    generation = _probe_generation() if models.get("ok") else {"ok": False, "skipped": True, "stdout": "", "stderr": "models_probe_failed", "exit_code": 1, "failure_kind": models.get("failure_kind", "unknown")}
    ready = bool(models.get("ok") and generation.get("ok"))
    failure_kind = ""
    if not ready:
        failure_kind = str(generation.get("failure_kind") or models.get("failure_kind") or "unknown")
    report = {
        "ok": ready,
        "ready": ready,
        "auth_mode": "agy_oauth",
        "state_dir": str(AGY_STATE_DIR),
        "legacy_state_dir": str(LEGACY_STATE_DIR),
        "auth_marker_present": has_auth_marker(),
        "marker_paths": [str(path) for path in _state_markers()],
        "models": models.get("models", []),
        "models_probe": models,
        "generation_probe": generation,
        "failure_kind": failure_kind,
        "recorded_at": datetime.now(UTC).isoformat(),
        "session_store": _store().snapshot(),
    }
    if ready:
        _store().record_success(models=list(report["models"]), auth_mode="agy_oauth")
    else:
        _store().record_failure(
            str(generation.get("stderr") or models.get("stderr") or generation.get("stdout") or models.get("stdout") or "antigravity_not_ready"),
            failure_kind=failure_kind or "unknown",
        )
    report["session_store"] = _store().snapshot()
    return report


def _open_browser(url: str) -> str:
    if _store().browser_open_cooldown_active(cooldown_sec=max(60, int(os.getenv("AI_BRIDGE_ANTIGRAVITY_BROWSER_OPEN_COOLDOWN_SEC", "300") or "300")), browser_url=url):
        return "suppressed"
    if webbrowser.open(url, new=1, autoraise=True):
        _store().record_browser_opened(url)
        return "opened"
    for cmd in (["xdg-open", url], ["flatpak-spawn", "--host", "xdg-open", url]):
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.Popen(cmd)
            _store().record_browser_opened(url)
            return "opened"
        except Exception:
            continue
    return "failed"


def _handle_output(text: str, opened_urls: set[str]) -> None:
    print(text, end="", flush=True)
    result = _analyze_output(text, opened_urls, open_browser=True)
    for event in result.get("events", []):
        print()
        print(event)


def _analyze_output(text: str, opened_urls: set[str], *, open_browser: bool) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    events: list[str] = []
    stripped = text.strip()
    if stripped:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            updates["last_prompt"] = lines[-1][:500]
            updates["message"] = lines[-1][:500]
    lower = text.lower()
    for url in URL_RE.findall(text):
        url = url.rstrip(".,);]")
        if url not in opened_urls:
            opened_urls.add(url)
        updates["browser_url"] = url
        updates["state"] = "waiting_browser"
        updates["message"] = "Browser authorization is in progress. Finish the login flow in the opened page."
        updates["user_input_required"] = False
        updates["input_hint"] = "Finish the browser login flow."
        if open_browser:
            browser_state = _open_browser(url)
            if browser_state == "opened":
                events.append(f"[antigravity-login] opened browser: {url}")
            elif browser_state == "suppressed":
                events.append(f"[antigravity-login] browser login was already opened recently: {url}")
            else:
                events.append(f"[antigravity-login] open this URL manually: {url}")
    if any(marker in lower for marker in WAITING_CODE_MARKERS):
        updates["state"] = "waiting_code"
        updates["message"] = "Antigravity is waiting for the authorization code. Submit the code through the bridge or directly in the CLI flow."
        updates["user_input_required"] = True
        updates["input_hint"] = "Submit the authorization code shown by Antigravity."
    elif any(marker in lower for marker in WAITING_BROWSER_MARKERS) and "state" not in updates:
        updates["state"] = "login_pending"
        updates["message"] = "Interactive login is running. Finish the current browser step and wait for verification."
        updates["user_input_required"] = False
        updates["input_hint"] = "Complete the browser step."
    match = CODE_RE.search(text)
    if match:
        updates["last_code_hint"] = match.group(0)
        if open_browser:
            events.append(f"[antigravity-login] detected code-like token: {match.group(0)}")
    return {"updates": updates, "events": events}


def _interactive_pty(cmd: list[str], timeout_sec: int) -> int:
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(cmd[0], cmd, _cli_runtime_env())

    opened_urls: set[str] = set()
    deadline = time.time() + timeout_sec
    ready_checked_at = 0.0

    if not sys.stdin.isatty():
        print("[antigravity-login] stdin is not a TTY; code input must happen in the agy/browser flow.")

    while True:
        if time.time() > deadline:
            print()
            print("[antigravity-login] timed out waiting for authorization", file=sys.stderr)
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            return 1

        now = time.time()
        if now - ready_checked_at > 10:
            ready_checked_at = now
            report = _build_report()
            if report.get("ready"):
                print()
                print(f"[antigravity-login] Antigravity state detected: {AGY_STATE_DIR}")
                return 0

        read_fds = [fd]
        if sys.stdin.isatty():
            read_fds.append(sys.stdin.fileno())
        readable, _, _ = select.select(read_fds, [], [], 0.5)

        if fd in readable:
            try:
                data = os.read(fd, 4096)
            except OSError:
                break
            if not data:
                break
            _handle_output(data.decode(errors="replace"), opened_urls)

        if sys.stdin.isatty() and sys.stdin.fileno() in readable:
            data = os.read(sys.stdin.fileno(), 4096)
            if data:
                os.write(fd, data)

        try:
            finished_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return 0
        if finished_pid == pid:
            return os.waitstatus_to_exitcode(status)

    try:
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        return 0


def _login_log_path() -> Path:
    log_dir = AGY_STATE_DIR / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"login-helper-{stamp}.log"


def _helper_path() -> str:
    return str(Path(__file__).resolve())


def _new_session_id() -> str:
    return f"agy-login-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def managed_login_start(wait_timeout_sec: int = 600, force: bool = False) -> dict[str, Any]:
    store = _store()
    if not force:
        report = _build_report()
        if report.get("ready"):
            return {
                "ok": True,
                "ready": True,
                "started": False,
                "message": "Antigravity is already authorized.",
                "session": store.load_interactive_session(),
            }
    current = store.load_interactive_session()
    if current.get("session_id") and store.interactive_session_active(session_id=str(current.get("session_id") or "")):
        return {
            "ok": True,
            "ready": False,
            "started": False,
            "message": "Interactive login session is already active.",
            "session": current,
        }
    session_id = _new_session_id()
    log_file = _login_log_path()
    store.record_login_started()
    session = store.start_interactive_session(session_id, owner="AntigravityManager", control_mode="bridge", log_path=str(log_file))
    cmd = [sys.executable, _helper_path(), "--managed-login-run", "--session-id", session_id, "--timeout", str(max(30, wait_timeout_sec))]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True)
    session = store.update_interactive_session(
        session_id,
        pid=proc.pid,
        state="starting",
        message="Managed interactive login bridge started.",
        user_input_required=False,
        pending_input=False,
    )
    return {
        "ok": True,
        "ready": False,
        "started": True,
        "session_id": session_id,
        "session": session,
        "message": "Managed interactive login bridge started.",
    }


def _read_new_input(session_id: str, offset: int) -> tuple[str, int]:
    path = _store().session_input_file(session_id)
    if not path.exists():
        return "", offset
    data = path.read_text(encoding="utf-8")
    if offset >= len(data):
        return "", len(data)
    return data[offset:], len(data)


def managed_login_run(session_id: str, wait_timeout_sec: int = 600) -> int:
    store = _store()
    session = store.load_interactive_session(session_id)
    if not session.get("session_id"):
        session = store.start_interactive_session(session_id, owner="AntigravityManager", control_mode="bridge", log_path=str(_login_log_path()))
    log_path = str(session.get("log_path") or _login_log_path())
    store.update_interactive_session(
        session_id,
        pid=os.getpid(),
        state="running",
        message="Managed interactive login session is running.",
        user_input_required=False,
        pending_input=False,
        transcript_path=str(store.session_transcript_file(session_id)),
    )

    cmd = _interactive_login_command(log_path)
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(cmd[0], cmd, _cli_runtime_env())

    opened_urls: set[str] = set()
    deadline = time.time() + max(30, wait_timeout_sec)
    ready_checked_at = 0.0
    input_offset = 0

    while True:
        if time.time() > deadline:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
            store.finish_interactive_session(session_id, state="timed_out", message="Interactive authorization timed out before completion.")
            return 1

        now = time.time()
        if now - ready_checked_at > 10:
            ready_checked_at = now
            report = _build_report()
            if report.get("ready"):
                store.finish_interactive_session(session_id, state="ready", message="Antigravity authorization confirmed.")
                return 0

        readable, _, _ = select.select([fd], [], [], 0.5)
        if fd in readable:
            try:
                data = os.read(fd, 4096)
            except OSError:
                data = b""
            if data:
                text = data.decode(errors="replace")
                store.append_interactive_transcript(session_id, text)
                analyzed = _analyze_output(text, opened_urls, open_browser=True)
                updates = analyzed.get("updates") or {}
                if updates:
                    updates["last_event_at"] = datetime.now(UTC).isoformat()
                    store.update_interactive_session(session_id, **updates)
            else:
                break

        pending_text, input_offset = _read_new_input(session_id, input_offset)
        if pending_text:
            try:
                os.write(fd, pending_text.encode())
                store.update_interactive_session(
                    session_id,
                    state="running",
                    message="Submitted user input to the active Antigravity login session.",
                    pending_input=False,
                    user_input_required=False,
                    last_event_at=datetime.now(UTC).isoformat(),
                )
            except OSError as exc:
                store.finish_interactive_session(session_id, state="failed", message=f"Failed to forward bridge input: {exc}")
                return 1

        try:
            finished_pid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            finished_pid, status = pid, 0
        if finished_pid == pid:
            exit_code = os.waitstatus_to_exitcode(status)
            report = _build_report()
            if report.get("ready"):
                store.finish_interactive_session(session_id, state="ready", message="Antigravity authorization confirmed.")
                return 0
            active = store.load_interactive_session(session_id)
            state = str(active.get("state") or "failed")
            if state in {"waiting_browser", "waiting_code", "login_pending", "running", "starting"}:
                state = "failed"
            store.finish_interactive_session(session_id, state=state, message=str(active.get("message") or "Interactive authorization ended before a ready session was detected."))
            return exit_code or 1

    report = _build_report()
    if report.get("ready"):
        store.finish_interactive_session(session_id, state="ready", message="Antigravity authorization confirmed.")
        return 0
    store.finish_interactive_session(session_id, state="failed", message="Interactive authorization session exited unexpectedly.")
    return 1


def login_interactive(wait_timeout_sec: int = 600, force: bool = False) -> int:
    print("Starting Antigravity authorization session.")
    print("If a browser URL appears, this helper will try to open it.")
    print("If Antigravity asks for a code, paste it in this console and press Enter.")
    print(f"State directory: {AGY_STATE_DIR}")

    if not force:
        report = _build_report()
        if report.get("ready"):
            print("Antigravity is already authorized and ready; browser login is not required.")
            _print_models()
            return 0

    _store().record_login_started()
    log_file = _login_log_path()
    print(f"Log file: {log_file}")
    cmd = _interactive_login_command(str(log_file))
    status = _interactive_pty(cmd, wait_timeout_sec)

    print()
    print("Checking Antigravity readiness...")
    report = _build_report()
    if report.get("ready"):
        _store().record_success(models=list(report.get("models") or []), auth_mode="agy_oauth")
        print("Antigravity is authorized and ready.")
        return 0
    _store().record_login_failure(str(report.get("generation_probe", {}).get("stderr") or report.get("models_probe", {}).get("stderr") or "authorization_not_confirmed"), failure_kind=str(report.get("failure_kind") or "unknown"))
    print(f"Antigravity authorization was not confirmed. Check log: {log_file}", file=sys.stderr)
    return status or 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antigravity login helper")
    parser.add_argument("--check", action="store_true", help="Check auth marker, models, and generation readiness")
    parser.add_argument("--login", action="store_true", help="Start browser/console authorization flow")
    parser.add_argument("--force-login", action="store_true", help="Start authorization flow even if agy already works")
    parser.add_argument("--managed-login-start", action="store_true", help="Start managed background authorization flow")
    parser.add_argument("--managed-login-run", action="store_true", help="Run the managed background authorization worker")
    parser.add_argument("--session-status", action="store_true", help="Return the active managed session status")
    parser.add_argument("--session-input", help="Append user input to the active managed session")
    parser.add_argument("--session-id", help="Managed interactive session id")
    parser.add_argument("--models", action="store_true", help="Print available models")
    parser.add_argument("--verify", action="store_true", help="Run a small agy generation probe")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output for automation")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for auth during login")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.managed_login_run:
        if not args.session_id:
            print("missing --session-id for managed login run", file=sys.stderr)
            return 2
        return managed_login_run(args.session_id, wait_timeout_sec=max(30, args.timeout))

    if args.managed_login_start:
        payload = managed_login_start(wait_timeout_sec=max(30, args.timeout), force=args.force_login)
        if args.json:
            print(json.dumps(payload, ensure_ascii=True))
        else:
            session = payload.get("session") or {}
            print(payload.get("message") or "managed login started")
            if session.get("session_id"):
                print(f"session_id: {session.get('session_id')}")
        return 0 if payload.get("ok") else 1

    if args.session_status:
        payload = _store().load_interactive_session(args.session_id)
        if args.json:
            print(json.dumps(payload, ensure_ascii=True))
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    if args.session_input is not None:
        session_id = str(args.session_id or _store().load().get("interactive_session_id") or "").strip()
        if not session_id:
            payload = {"ok": False, "error": "missing_active_session", "message": "No active Antigravity managed session found."}
            if args.json:
                print(json.dumps(payload, ensure_ascii=True))
            else:
                print(payload["message"], file=sys.stderr)
            return 1
        session = _store().append_interactive_input(session_id, args.session_input)
        payload = {"ok": True, "session": session, "message": "Input forwarded to the managed Antigravity session queue."}
        if args.json:
            print(json.dumps(payload, ensure_ascii=True))
        else:
            print(payload["message"])
        return 0

    if args.login or args.force_login:
        return login_interactive(wait_timeout_sec=max(30, args.timeout), force=args.force_login)

    if args.models:
        if args.json:
            report = _build_report()
            print(json.dumps({"ok": report.get("models_probe", {}).get("ok", False), "models": report.get("models", []), "models_probe": report.get("models_probe", {}), "session_store": report.get("session_store", {})}, ensure_ascii=True))
            return 0 if report.get("models_probe", {}).get("ok") else 1
        return _print_models()

    if args.verify:
        report = _build_report()
        if args.json:
            print(json.dumps(report, ensure_ascii=True))
        else:
            models_probe = report.get("models_probe", {})
            generation_probe = report.get("generation_probe", {})
            if models_probe.get("stdout"):
                print(str(models_probe.get("stdout")).rstrip())
            if models_probe.get("stderr"):
                print(str(models_probe.get("stderr")).rstrip(), file=sys.stderr)
            if generation_probe.get("stdout"):
                print(str(generation_probe.get("stdout")).rstrip())
            if generation_probe.get("stderr"):
                print(str(generation_probe.get("stderr")).rstrip(), file=sys.stderr)
        return 0 if report.get("ok") else 1

    report = _build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=True))
    else:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
