from __future__ import annotations

from pathlib import Path

from core.core.host_bridge import HostBridge, HostBridgeError


def test_whitelist_init_and_validate(tmp_path: Path):
    wl = tmp_path / "whitelist.txt"
    bridge = HostBridge(whitelist_file=wl)
    bridge.ensure_whitelist()

    assert wl.exists()
    bridge.validate(["podman", "ps"])


def test_validate_rejects_unknown_command(tmp_path: Path):
    wl = tmp_path / "whitelist.txt"
    wl.write_text("podman\n")
    bridge = HostBridge(whitelist_file=wl)

    try:
        bridge.validate(["rm", "-rf", "/"])
    except HostBridgeError as exc:
        assert "not in host bridge whitelist" in str(exc)
    else:
        raise AssertionError("Expected HostBridgeError")


def test_translate_gh_via_distrobox_fallback(monkeypatch):
    bridge = HostBridge()

    monkeypatch.setattr(HostBridge, "detect_mode", lambda self: "flatpak-spawn")
    monkeypatch.setattr(HostBridge, "_host_has_binary", lambda self, name: False)
    monkeypatch.setattr(type(bridge.distrobox_bridge), "ensure_gh_ready", lambda self, mode: "gh-dev")

    translated = bridge.translate(["gh", "repo", "view"])
    assert translated == [
        "flatpak-spawn",
        "--host",
        "distrobox",
        "enter",
        "gh-dev",
        "--",
        "gh",
        "repo",
        "view",
    ]


def test_whitelist_includes_common_diagnostics(tmp_path: Path):
    wl = tmp_path / "whitelist.txt"
    bridge = HostBridge(whitelist_file=wl)
    bridge.ensure_whitelist()

    allowed = bridge.allowlist()
    assert {"ss", "lsof", "ps", "free", "df", "du", "hostname", "whoami"}.issubset(allowed)



def test_whitelist_includes_sourcecraft_tools(tmp_path: Path):
    wl = tmp_path / "whitelist.txt"
    bridge = HostBridge(whitelist_file=wl)
    bridge.ensure_whitelist()

    allowed = bridge.allowlist()
    assert {"src", "dh", "git", "gh"}.issubset(allowed)


def test_execute_forwards_cwd(monkeypatch, tmp_path: Path):
    bridge = HostBridge(whitelist_file=tmp_path / "whitelist.txt")
    monkeypatch.setattr(HostBridge, "validate", lambda self, command: None)
    monkeypatch.setattr(HostBridge, "translate", lambda self, command: command)

    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr("core.core.host_bridge.subprocess.run", fake_run)
    result = bridge.execute(["git", "status"], cwd=str(tmp_path), timeout=5)

    assert result.returncode == 0
    assert captured["command"] == ["git", "status"]
    assert captured["cwd"] == str(tmp_path)



def test_translate_dh_via_distrobox(monkeypatch):
    bridge = HostBridge()

    monkeypatch.setattr(HostBridge, "detect_mode", lambda self: "flatpak-spawn")
    monkeypatch.setattr(type(bridge.distrobox_bridge), "ensure_gh_ready", lambda self, mode: "ghbox")

    translated = bridge.translate(["dh", "git", "push", "origin", "main"])
    assert translated == [
        "flatpak-spawn",
        "--host",
        "distrobox",
        "enter",
        "ghbox",
        "--",
        "git",
        "push",
        "origin",
        "main",
    ]
