#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(command: list[str]) -> dict[str, object]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except FileNotFoundError as exc:
        return {
            "command": command,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
        }


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def parse_proc_status() -> dict[str, str]:
    wanted = {"NoNewPrivs", "Seccomp", "Seccomp_filters", "CapPrm", "CapEff", "CapBnd"}
    result: dict[str, str] = {}
    content = read_text("/proc/self/status")
    if not content:
        return result
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in wanted:
            result[key] = value.strip()
    return result


def find_bwrap() -> str | None:
    for candidate in ("/usr/bin/bwrap", "/bin/bwrap"):
        if Path(candidate).exists():
            return candidate
    return None


def probe_apply_patch_like_io() -> dict[str, object]:
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            probe = Path(tmpdir) / "apply_patch_probe.txt"
            probe.write_text("before\n", encoding="utf-8")
            created = probe.exists() and probe.read_text(encoding="utf-8") == "before\n"
            probe.write_text("after\n", encoding="utf-8")
            updated = probe.read_text(encoding="utf-8") == "after\n"
            probe.unlink()
            deleted = not probe.exists()
            return {
                "returncode": 0 if created and updated and deleted else 1,
                "created": created,
                "updated": updated,
                "deleted": deleted,
                "stderr": "",
            }
    except OSError as exc:
        return {
            "returncode": 1,
            "created": False,
            "updated": False,
            "deleted": False,
            "stderr": str(exc),
        }


def main() -> int:
    bwrap = find_bwrap()
    report = {
        "cwd": os.getcwd(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "proc_status": parse_proc_status(),
        "user_max_user_namespaces": read_text("/proc/sys/user/max_user_namespaces"),
        "kernel_unprivileged_userns_clone": read_text("/proc/sys/kernel/unprivileged_userns_clone"),
        "checks": {
            "id": run(["id"]),
            "unshare_userns": run(["unshare", "-Ur", "true"]),
            "bwrap_version": run([bwrap, "--version"]) if bwrap else {
                "command": ["bwrap", "--version"],
                "returncode": 127,
                "stdout": "",
                "stderr": "bwrap not found",
            },
            "bwrap_userns": run([bwrap, "--unshare-user", "--ro-bind", "/", "/", "true"]) if bwrap else {
                "command": ["bwrap", "--unshare-user", "--ro-bind", "/", "/", "true"],
                "returncode": 127,
                "stdout": "",
                "stderr": "bwrap not found",
            },
            "apply_patch_like_io": probe_apply_patch_like_io(),
        },
        "classification": "BLOCKED",
        "next_steps": [],
    }

    unshare_ok = report["checks"]["unshare_userns"]["returncode"] == 0
    bwrap_ok = report["checks"]["bwrap_userns"]["returncode"] == 0
    io_ok = report["checks"]["apply_patch_like_io"]["returncode"] == 0
    seccomp = report["proc_status"].get("Seccomp")
    no_new_privs = report["proc_status"].get("NoNewPrivs")

    if unshare_ok and bwrap_ok and io_ok:
        report["classification"] = "READY"
        report["next_steps"] = [
            "Run targeted repository tests in the same runtime.",
            "Keep the same container security profile for all agent work.",
        ]
    else:
        report["next_steps"] = [
            "If running on Bazzite, Silverblue, Fedora CoreOS, or another immutable host, prefer an unconfined or privileged container profile instead of tuning kernel.unprivileged_userns_clone.",
            "If running in podman, retry with --security-opt seccomp=unconfined --security-opt label=disable.",
            "If running in docker, retry with --security-opt seccomp=unconfined and --security-opt apparmor=unconfined.",
            "If that still fails, use --privileged as a temporary unblocker.",
            "Do not continue to code generation until unshare and bwrap probes pass in the same runtime.",
        ]
        if seccomp == "2":
            report["next_steps"].insert(
                0,
                "The process is under seccomp filtering; investigate container security policy first.",
            )
        if no_new_privs == "1":
            report["next_steps"].insert(
                1,
                "The process has no_new_privileges enabled; this confirms a constrained runtime.",
            )
        if not bwrap_ok:
            report["next_steps"].insert(
                2,
                "bubblewrap is still blocked; the current runtime cannot safely execute sandboxed shell commands or stable apply_patch updates.",
            )

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["classification"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
