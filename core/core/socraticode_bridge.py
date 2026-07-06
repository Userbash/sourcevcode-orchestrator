from __future__ import annotations

import json
import os
import re
import shlex
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from core.core.models import Task
from core.core.provider_credentials import sync_provider_env_aliases


class SocratiCodeBridgeError(RuntimeError):
    pass


class SocratiCodeBridgeDisabled(SocratiCodeBridgeError):
    pass


class SocratiCodeBridgeUnavailable(SocratiCodeBridgeError):
    pass


class MCPTransport(Protocol):
    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...
    def close(self) -> None: ...


def _safe_json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _task_text(task: Task, query: str | None = None) -> str:
    if isinstance(query, str) and query.strip():
        return query.strip()
    description = str(getattr(getattr(task, "input", None), "description", "") or "").strip()
    if description:
        return description
    files = getattr(getattr(task, "input", None), "files", []) or []
    if files:
        return " ".join(str(item).strip() for item in files if str(item).strip())
    return str(getattr(task, "task_id", "") or "").strip()


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        chunks = [_flatten_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return str(value.get("text") or "").strip()
        if isinstance(value.get("content"), list):
            return _flatten_text(value.get("content"))
        if isinstance(value.get("content"), str):
            return str(value.get("content") or "").strip()
        if isinstance(value.get("structuredContent"), dict):
            try:
                return json.dumps(value["structuredContent"], ensure_ascii=True, sort_keys=True)
            except Exception:
                return str(value["structuredContent"])
    return ""


def _truncate(text: str, limit: int) -> str:
    raw = str(text or "").strip()
    if limit <= 0 or len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)].rstrip() + "..."


@dataclass(slots=True)
class MCPToolResult:
    name: str
    payload: dict[str, Any]
    text: str
    structured: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        if self.payload.get("isError") is True:
            return True
        return bool(self.structured.get("error"))


class StdioMCPTransport:
    def __init__(
        self,
        *,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        startup_timeout_sec: float = 10.0,
    ) -> None:
        if not command:
            raise SocratiCodeBridgeUnavailable("missing_mcp_command")
        self.command = list(command)
        self._next_id = 1
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            self.command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        if self._process.stdin is None or self._process.stdout is None:
            raise SocratiCodeBridgeUnavailable("mcp_stdio_pipe_unavailable")
        self.stdin = self._process.stdin
        self.stdout = self._process.stdout
        self.startup_timeout_sec = startup_timeout_sec
        self._read_buffer = b""

    def _parse_buffered_message(self) -> dict[str, Any] | None:
        if self._read_buffer.startswith(b"Content-Length:"):
            header_end = self._read_buffer.find(b"\r\n\r\n")
            if header_end == -1:
                return None
            header_block = self._read_buffer[:header_end].decode("utf-8", errors="replace")
            content_length: int | None = None
            for raw_line in header_block.splitlines():
                header, _, value = raw_line.partition(":")
                if header.lower() == "content-length":
                    try:
                        content_length = int(value.strip())
                    except ValueError as exc:
                        raise SocratiCodeBridgeUnavailable("invalid_content_length") from exc
                    break
            if content_length is None:
                raise SocratiCodeBridgeUnavailable("missing_content_length")
            body_start = header_end + 4
            body_end = body_start + content_length
            if len(self._read_buffer) < body_end:
                return None
            payload = self._read_buffer[body_start:body_end]
            self._read_buffer = self._read_buffer[body_end:]
        else:
            line_end = self._read_buffer.find(b"\n")
            if line_end == -1:
                return None
            payload = self._read_buffer[:line_end].rstrip(b"\r")
            self._read_buffer = self._read_buffer[line_end + 1 :]
        if not payload:
            raise SocratiCodeBridgeUnavailable("empty_mcp_payload")
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise SocratiCodeBridgeUnavailable("invalid_mcp_json") from exc

    def _read_message(self, *, timeout_sec: float | None = None) -> dict[str, Any]:
        deadline = None if timeout_sec is None or timeout_sec <= 0 else time.monotonic() + timeout_sec
        while True:
            parsed = self._parse_buffered_message()
            if parsed is not None:
                return parsed
            wait_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.stdout], [], [], wait_timeout)
            if not ready:
                raise SocratiCodeBridgeUnavailable("mcp_request_timeout")
            chunk = os.read(self.stdout.fileno(), 4096)
            if not chunk:
                raise SocratiCodeBridgeUnavailable("mcp_server_closed_stdout")
            self._read_buffer += chunk

    def _write_message(self, payload: dict[str, Any]) -> None:
        wire = _safe_json_dumps(payload) + b"\n"
        self.stdin.write(wire)
        self.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._write_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            while True:
                response = self._read_message(timeout_sec=self.startup_timeout_sec)
                if response.get("id") != request_id:
                    continue
                if isinstance(response.get("error"), dict):
                    message = str(response["error"].get("message") or "mcp_request_failed")
                    raise SocratiCodeBridgeError(message)
                result = response.get("result")
                return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._write_message({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self) -> None:
        try:
            if self._process.stdin:
                self._process.stdin.close()
        except Exception:
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except Exception:
                self._process.kill()


@dataclass(slots=True)
class SocratiCodeBridge:
    command: list[str] | None = None
    repo_path: str | None = None
    env_overrides: dict[str, str] | None = None
    startup_timeout_sec: float = 10.0
    transport: MCPTransport | None = None
    initialized: bool = False
    tool_names: set[str] = field(default_factory=set)

    TOOLSET: ClassVar[tuple[str, ...]] = (
        "codebase_status",
        "codebase_search",
        "codebase_context",
        "codebase_context_search",
        "codebase_graph_status",
        "codebase_impact",
        "codebase_symbols",
    )

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        repo_path: str | None = None,
        env_overrides: dict[str, str] | None = None,
        startup_timeout_sec: float = 10.0,
        transport: MCPTransport | None = None,
    ) -> None:
        self.command = command if command is not None else self._command_from_env()
        self.repo_path = repo_path or os.getenv("SOCRATICODE_REPO_PATH") or os.getenv("AI_BRIDGE_WORKSPACE_ROOT") or "."
        self.env_overrides = dict(env_overrides or {})
        self.startup_timeout_sec = startup_timeout_sec
        self.transport = transport
        self.initialized = False
        self.tool_names = set()

    @staticmethod
    def _enabled() -> bool:
        return os.getenv("SOCRATICODE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _command_from_env() -> list[str] | None:
        raw = str(os.getenv("SOCRATICODE_MCP_COMMAND") or "").strip()
        if raw:
            return shlex.split(raw)
        if SocratiCodeBridge._enabled():
            return ["npx", "-y", "socraticode"]
        return None

    @staticmethod
    def runtime_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(sync_provider_env_aliases(os.environ.copy()))
        env.update({str(key): str(value) for key, value in (overrides or {}).items()})
        return env

    @staticmethod
    def _has_positive_signal(text: str, *, empty_markers: tuple[str, ...]) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return False
        return not any(marker in normalized for marker in empty_markers)

    @staticmethod
    def _coverage_status(score: float) -> str:
        if score >= 0.9:
            return "strong"
        if score >= 0.72:
            return "good"
        if score >= 0.45:
            return "partial"
        return "low"

    @staticmethod
    def _extract_match_count(text: str) -> int:
        normalized = str(text or "").strip()
        if not normalized:
            return 0
        match = re.search(r"\((\d+)\s+match", normalized, flags=re.IGNORECASE)
        if match:
            try:
                return max(0, int(match.group(1)))
            except ValueError:
                return 0
        if "no results found" in normalized.lower():
            return 0
        return 1

    @staticmethod
    def _parallel_cap() -> int:
        raw = str(os.getenv("AI_BRIDGE_PARALLEL_CODE_BRANCHES_MAX", "10") or "10").strip()
        try:
            return max(2, min(10, int(raw)))
        except ValueError:
            return 10

    @staticmethod
    def _safe_parallel_branches(hints: dict[str, Any]) -> int | None:
        try:
            value = int(hints.get("parallel_branches"))
        except (TypeError, ValueError):
            return None
        return max(1, value)

    def _project_arguments(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if self.repo_path:
            arguments["projectPath"] = self.repo_path
        if isinstance(extra, dict):
            arguments.update(extra)
        return arguments

    def connect(self) -> "SocratiCodeBridge":
        if self.transport is None:
            if not self._enabled() and self.command is None:
                raise SocratiCodeBridgeDisabled("socraticode_disabled")
            if self.command is None:
                raise SocratiCodeBridgeUnavailable("missing_socraticode_mcp_command")
            self.transport = StdioMCPTransport(
                command=self.command,
                cwd=self.repo_path,
                env=self.runtime_env(self.env_overrides),
                startup_timeout_sec=self.startup_timeout_sec,
            )
        if self.initialized:
            return self
        initialize_result = self.transport.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sourcevcode-orchestrator", "version": "1.0"},
            },
        )
        self.transport.notify("notifications/initialized", {})
        tools = self.transport.request("tools/list", {})
        seen: set[str] = set()
        for item in tools.get("tools") or []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    seen.add(name)
        self.tool_names = seen
        self.initialized = True
        _ = initialize_result
        return self

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
        self.transport = None
        self.initialized = False
        self.tool_names = set()

    def __enter__(self) -> "SocratiCodeBridge":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def available_tools(self) -> list[str]:
        self.connect()
        return sorted(name for name in self.tool_names if name in self.TOOLSET)

    def supports(self, tool_name: str) -> bool:
        self.connect()
        return tool_name in self.tool_names

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> MCPToolResult:
        self.connect()
        if tool_name not in self.tool_names:
            raise SocratiCodeBridgeUnavailable(f"unsupported_tool:{tool_name}")
        payload = self.transport.request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        structured = _coerce_dict(payload.get("structuredContent"))
        return MCPToolResult(
            name=tool_name,
            payload=payload,
            text=_flatten_text(payload.get("content") or payload),
            structured=structured,
        )

    def probe(self) -> dict[str, Any]:
        try:
            tools = self.available_tools()
            return {
                "enabled": self._enabled() or self.command is not None or self.transport is not None,
                "connected": True,
                "repo_path": self.repo_path,
                "tools": tools,
            }
        except SocratiCodeBridgeError as exc:
            return {
                "enabled": self._enabled() or self.command is not None or self.transport is not None,
                "connected": False,
                "repo_path": self.repo_path,
                "tools": [],
                "error": str(exc),
            }

    def codebase_status(self) -> MCPToolResult:
        return self.call_tool("codebase_status", self._project_arguments())

    def codebase_context(self) -> MCPToolResult:
        return self.call_tool("codebase_context", self._project_arguments())

    def codebase_search(self, *, query: str, limit: int = 5) -> MCPToolResult:
        return self.call_tool("codebase_search", self._project_arguments({"query": query, "limit": limit}))

    def codebase_context_search(self, *, query: str, limit: int = 5) -> MCPToolResult:
        return self.call_tool("codebase_context_search", self._project_arguments({"query": query, "limit": limit}))

    def codebase_graph_status(self) -> MCPToolResult:
        return self.call_tool("codebase_graph_status", self._project_arguments())

    def codebase_impact(self, *, target: str, depth: int = 3) -> MCPToolResult:
        return self.call_tool("codebase_impact", self._project_arguments({"target": target, "depth": depth}))

    def codebase_symbols(self, *, query: str, limit: int = 8) -> MCPToolResult:
        return self.call_tool("codebase_symbols", self._project_arguments({"query": query, "limit": limit}))

    def build_compact_context(
        self,
        task: Task,
        *,
        query: str | None = None,
        search_limit: int = 4,
        include_symbols: bool = True,
        include_impact: bool = True,
        text_limit: int = 1200,
    ) -> dict[str, Any]:
        self.connect()
        task_query = _task_text(task, query=query)
        files = [str(item).strip() for item in (getattr(task.input, "files", []) or []) if str(item).strip()]
        results: dict[str, Any] = {
            "repo_path": self.repo_path,
            "query": task_query,
            "files": files,
            "tools_used": [],
        }
        summary_lines: list[str] = []

        if self.supports("codebase_status"):
            status = self.codebase_status()
            results["status"] = {"text": status.text, "structured": status.structured}
            results["tools_used"].append("codebase_status")
            if status.text:
                summary_lines.append(f"Status: {_truncate(status.text, 220)}")

        if self.supports("codebase_graph_status"):
            graph = self.codebase_graph_status()
            results["graph_status"] = {"text": graph.text, "structured": graph.structured}
            results["tools_used"].append("codebase_graph_status")
            graph_text = graph.text or _flatten_text(graph.structured)
            if graph_text:
                summary_lines.append(f"Graph: {_truncate(graph_text, 180)}")

        if task_query and self.supports("codebase_search"):
            search = self.codebase_search(query=task_query, limit=search_limit)
            results["search"] = {"text": search.text, "structured": search.structured}
            results["tools_used"].append("codebase_search")
            if search.text:
                summary_lines.append(f"Search: {_truncate(search.text, 320)}")

        if self.supports("codebase_context"):
            context_inventory = self.codebase_context()
            results["context"] = {"text": context_inventory.text, "structured": context_inventory.structured}
            results["tools_used"].append("codebase_context")
            if context_inventory.text:
                summary_lines.append(f"Artifacts: {_truncate(context_inventory.text, 220)}")

        if task_query and self.supports("codebase_context_search"):
            context_search = self.codebase_context_search(query=task_query, limit=search_limit)
            results["context_search"] = {"text": context_search.text, "structured": context_search.structured}
            results["tools_used"].append("codebase_context_search")
            if context_search.text:
                summary_lines.append(f"Context: {_truncate(context_search.text, 360)}")

        if include_impact and self.supports("codebase_impact") and (files or task_query):
            impacts: list[dict[str, Any]] = []
            impact_targets = files[:3] if files else [task_query]
            for target in impact_targets:
                impact = self.codebase_impact(target=target)
                impacts.append({"target": target, "text": impact.text, "structured": impact.structured})
                results["tools_used"].append("codebase_impact")
                if impact.text:
                    summary_lines.append(f"Impact[{target}]: {_truncate(impact.text, 220)}")
            results["impact"] = impacts

        if include_symbols and self.supports("codebase_symbols") and task_query:
            symbols = self.codebase_symbols(query=task_query, limit=max(3, search_limit))
            results["symbols"] = {"text": symbols.text, "structured": symbols.structured}
            results["tools_used"].append("codebase_symbols")
            if symbols.text:
                summary_lines.append(f"Symbols: {_truncate(symbols.text, 220)}")

        task_meta = [
            f"Task: {task.input.description}",
            f"Repo: {getattr(task.context, 'repo_path', '') or self.repo_path}",
        ]
        if getattr(task.context, "branch", None):
            task_meta.append(f"Branch: {task.context.branch}")
        if files:
            task_meta.append(f"Files: {', '.join(files[:6])}")

        compact_text = "\n".join([*task_meta, *summary_lines]).strip()
        results["text"] = _truncate(compact_text, text_limit)
        return results

    def analyze_task(
        self,
        *,
        task: Task,
        context: dict[str, Any] | None = None,
        description: str | None = None,
        task_type: str | None = None,
        routing_hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = context
        _ = task_type
        hints = routing_hints if isinstance(routing_hints, dict) else (task.routing_hints if isinstance(task.routing_hints, dict) else {})
        snapshot = self.build_compact_context(task, query=description, search_limit=4)

        requested_files = [str(item).strip() for item in list(getattr(task.input, "files", []) or []) if str(item).strip()]
        evidence_texts: list[str] = []
        for key in ("status", "graph_status", "search", "context", "context_search", "symbols"):
            section = snapshot.get(key)
            if isinstance(section, dict):
                text = str(section.get("text") or "").strip()
                if text:
                    evidence_texts.append(text)
        impact_rows = snapshot.get("impact") if isinstance(snapshot.get("impact"), list) else []
        for row in impact_rows:
            if isinstance(row, dict) and str(row.get("text") or "").strip():
                evidence_texts.append(str(row.get("text") or "").strip())
        combined = "\n".join(evidence_texts)

        covered_files = [file_path for file_path in requested_files if file_path in combined]
        missing_files = [file_path for file_path in requested_files if file_path not in covered_files]
        file_ratio = (len(covered_files) / len(requested_files)) if requested_files else 0.0

        status_text = str(((snapshot.get("status") or {}) if isinstance(snapshot.get("status"), dict) else {}).get("text") or "")
        graph_text = str(((snapshot.get("graph_status") or {}) if isinstance(snapshot.get("graph_status"), dict) else {}).get("text") or "")
        search_text = str(((snapshot.get("search") or {}) if isinstance(snapshot.get("search"), dict) else {}).get("text") or "")
        context_search_text = str(((snapshot.get("context_search") or {}) if isinstance(snapshot.get("context_search"), dict) else {}).get("text") or "")
        symbol_text = str(((snapshot.get("symbols") or {}) if isinstance(snapshot.get("symbols"), dict) else {}).get("text") or "")
        artifact_catalog_text = str(((snapshot.get("context") or {}) if isinstance(snapshot.get("context"), dict) else {}).get("text") or "")

        indexed = self._has_positive_signal(status_text, empty_markers=("no index found", "run codebase_index", "qdrant is not available"))
        graph_ready = self._has_positive_signal(graph_text, empty_markers=("no graph data available", "graph build started", "call codebase_graph_status"))
        search_hits = self._extract_match_count(search_text)
        context_hits = self._extract_match_count(context_search_text)
        symbol_hits = self._extract_match_count(symbol_text)
        impact_hits = sum(1 for row in impact_rows if isinstance(row, dict) and self._has_positive_signal(str(row.get("text") or ""), empty_markers=("no impact", "not found")))
        artifact_catalog_ready = self._has_positive_signal(artifact_catalog_text, empty_markers=("no context artifacts configured",))

        score = 0.0
        score += 0.18 if indexed else 0.0
        score += 0.12 if graph_ready else 0.0
        score += 0.18 if search_hits > 0 else 0.0
        score += 0.14 if context_hits > 0 else 0.0
        score += 0.10 if symbol_hits > 0 else 0.0
        score += 0.10 if impact_hits > 0 else 0.0
        score += 0.10 if artifact_catalog_ready else 0.0
        score += 0.08 * file_ratio
        score = round(min(1.0, score), 4)
        coverage_status = self._coverage_status(score)

        current_parallel = self._safe_parallel_branches(hints)
        parallel_cap = self._parallel_cap()
        if requested_files:
            if score >= 0.88:
                recommended_parallel = min(parallel_cap, max(2, len(requested_files)))
            elif score >= 0.65:
                recommended_parallel = min(parallel_cap, max(2, min(len(requested_files), current_parallel or len(requested_files))))
            else:
                recommended_parallel = min(parallel_cap, max(2, min(len(requested_files), 3)))
        else:
            recommended_parallel = 1
        reduce_to = recommended_parallel if current_parallel is not None and recommended_parallel < current_parallel else None

        prefer_low_cost = score >= 0.72 and indexed
        preferred_provider: str | None = None
        if prefer_low_cost:
            preferred_provider = "local" if score >= 0.9 else "mistral"

        return {
            "bridge": "socraticode",
            "repo_path": self.repo_path,
            "context_coverage": {
                "score": score,
                "coverage_ratio": score,
                "ratio": score,
                "status": coverage_status,
                "covered_files": covered_files,
                "missing_files": missing_files,
                "summary": snapshot.get("text") or "",
                "indexed": indexed,
                "graph_ready": graph_ready,
                "artifact_catalog_ready": artifact_catalog_ready,
                "search_hits": search_hits,
                "context_hits": context_hits,
                "symbol_hits": symbol_hits,
                "impact_hits": impact_hits,
            },
            "cost_downgrade": {
                "eligible": prefer_low_cost,
                "target_cost_tier": "economy" if prefer_low_cost else "balanced",
                "preferred_provider": preferred_provider,
                "reason": "SocratiCode precomputed code/context coverage is strong enough to route toward cheaper lanes." if prefer_low_cost else "Keep existing routing because SocratiCode coverage is still partial.",
                "confidence": score,
            },
            "parallelism": {
                "current_parallel_branches": current_parallel,
                "recommended_parallel_branches": recommended_parallel,
                "reduce_by": max(0, (current_parallel or recommended_parallel) - recommended_parallel) if current_parallel is not None else 0,
                "should_reduce": bool(current_parallel is not None and recommended_parallel < current_parallel),
                "reason": "Parallel fanout sized from file boundaries and SocratiCode context readiness.",
                "confidence": score,
            },
            "routing_recommendations": {
                "prefer_low_cost_lanes": prefer_low_cost,
                "reduce_parallel_branches_to": reduce_to,
                "target_parallel_branches": recommended_parallel,
                "prefer_provider": preferred_provider,
                "shared_index_ready": indexed,
            },
            "compact_context": snapshot,
        }
