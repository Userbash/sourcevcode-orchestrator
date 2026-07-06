from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

DEFAULT_WS_SUBPROTOCOLS: tuple[str, ...] = ("chat.v1", "chat.json")


class WebSocketLike(Protocol):
    headers: Mapping[str, str]

    async def accept(self, subprotocol: str | None = None) -> None: ...

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class SessionError(RuntimeError):
    pass


class SessionAuthError(SessionError):
    pass


class SessionProtocolError(SessionError):
    pass


class SessionBackpressureError(SessionError):
    pass


AuthHandler = Callable[[WebSocketLike], Awaitable[dict[str, Any]] | dict[str, Any] | None]
SessionHook = Callable[..., Awaitable[None] | None]


def negotiate_subprotocol(
    headers_or_websocket: Mapping[str, str] | WebSocketLike,
    *,
    supported_subprotocols: tuple[str, ...] = DEFAULT_WS_SUBPROTOCOLS,
) -> str | None:
    headers = headers_or_websocket.headers if hasattr(headers_or_websocket, "headers") else headers_or_websocket
    requested = str(headers.get("sec-websocket-protocol", "") or "")
    supported = {item.strip() for item in supported_subprotocols if item and item.strip()}
    for item in requested.split(","):
        candidate = item.strip()
        if candidate in supported:
            return candidate
    return None


def build_ws_envelope(
    *,
    type: str,
    request_id: str | None = None,
    correlation_id: str | None = None,
    action: str | None = None,
    data: Any = None,
    error: Any = None,
    final: bool = False,
) -> dict[str, Any]:
    return {
        "type": type,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "action": action,
        "data": data,
        "error": error,
        "final": bool(final),
    }


@dataclass(slots=True)
class HandshakeResult:
    session_id: str
    subprotocol: str | None
    principal: dict[str, Any]
    accepted_at: str


@dataclass(slots=True)
class SubscriptionBinding:
    subscription_id: str
    topic: str
    metadata: dict[str, Any] = field(default_factory=dict)
    unsubscribe: Callable[[], Awaitable[None] | None] | None = None


async def _maybe_await(result: Awaitable[Any] | Any) -> Any:
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


async def default_auth_handler(websocket: WebSocketLike) -> dict[str, Any]:
    authorization = str(websocket.headers.get("authorization", "") or "").strip()
    if not authorization:
        return {"subject": "anonymous", "authenticated": False}
    scheme, _, token = authorization.partition(" ")
    return {
        "subject": token or scheme or "anonymous",
        "authenticated": bool(token or scheme),
        "auth_scheme": scheme.lower() if scheme else None,
    }


class OrchestratorWebSocketSession:
    def __init__(
        self,
        websocket: WebSocketLike,
        *,
        session_id: str | None = None,
        supported_subprotocols: tuple[str, ...] = DEFAULT_WS_SUBPROTOCOLS,
        auth_handler: AuthHandler | None = None,
        on_subscribe: SessionHook | None = None,
        on_unsubscribe: SessionHook | None = None,
        on_disconnect: SessionHook | None = None,
        send_timeout_sec: float = 5.0,
        heartbeat_interval_sec: float = 30.0,
    ) -> None:
        self.websocket = websocket
        self.session_id = session_id or f"ws-{uuid4().hex}"
        self.supported_subprotocols = supported_subprotocols
        self.auth_handler = auth_handler or default_auth_handler
        self.on_subscribe = on_subscribe
        self.on_unsubscribe = on_unsubscribe
        self.on_disconnect = on_disconnect
        self.send_timeout_sec = float(send_timeout_sec)
        self.heartbeat_interval_sec = float(heartbeat_interval_sec)
        self._accepted = False
        self._closed = False
        self._principal: dict[str, Any] = {"subject": "anonymous", "authenticated": False}
        self._send_lock = asyncio.Lock()
        self._request_tasks: dict[str, asyncio.Task[Any]] = {}
        self._subscriptions: dict[str, SubscriptionBinding] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None

    @property
    def principal(self) -> dict[str, Any]:
        return dict(self._principal)

    @property
    def subscriptions(self) -> dict[str, SubscriptionBinding]:
        return dict(self._subscriptions)

    @property
    def active_request_ids(self) -> set[str]:
        return set(self._request_tasks)

    def negotiated_subprotocol(self) -> str | None:
        return negotiate_subprotocol(self.websocket, supported_subprotocols=self.supported_subprotocols)

    async def accept(self) -> HandshakeResult:
        if self._accepted:
            return HandshakeResult(
                session_id=self.session_id,
                subprotocol=self.negotiated_subprotocol(),
                principal=self.principal,
                accepted_at=datetime.now(UTC).isoformat(),
            )
        try:
            principal = await _maybe_await(self.auth_handler(self.websocket))
        except SessionAuthError:
            await self.close(code=4401, reason="unauthorized")
            raise
        self._principal = dict(principal or {"subject": "anonymous", "authenticated": False})
        protocol = self.negotiated_subprotocol()
        await self.websocket.accept(subprotocol=protocol)
        self._accepted = True
        return HandshakeResult(
            session_id=self.session_id,
            subprotocol=protocol,
            principal=self.principal,
            accepted_at=datetime.now(UTC).isoformat(),
        )

    async def send_frame(self, frame: dict[str, Any], *, timeout_sec: float | None = None) -> None:
        if self._closed:
            raise SessionError("websocket session is closed")
        async with self._send_lock:
            try:
                await asyncio.wait_for(
                    self.websocket.send_json(frame),
                    timeout=self.send_timeout_sec if timeout_sec is None else timeout_sec,
                )
            except TimeoutError as exc:
                await self.close(code=1011, reason="send_timeout")
                raise SessionBackpressureError("websocket send timed out") from exc

    async def send_response(
        self,
        *,
        type: str = "response",
        request_id: str | None = None,
        correlation_id: str | None = None,
        action: str | None = None,
        data: Any = None,
        error: Any = None,
        final: bool = False,
    ) -> dict[str, Any]:
        frame = build_ws_envelope(
            type=type,
            request_id=request_id,
            correlation_id=correlation_id,
            action=action,
            data=data,
            error=error,
            final=final,
        )
        await self.send_frame(frame)
        return frame

    async def send_ack(
        self,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
        action: str | None = None,
        data: Any = None,
        final: bool = False,
    ) -> dict[str, Any]:
        return await self.send_response(
            type="ack",
            request_id=request_id,
            correlation_id=correlation_id,
            action=action,
            data=data,
            final=final,
        )

    async def send_error(
        self,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
        action: str | None = None,
        error: Any = None,
        final: bool = True,
    ) -> dict[str, Any]:
        return await self.send_response(
            type="error",
            request_id=request_id,
            correlation_id=correlation_id,
            action=action,
            error=error,
            final=final,
        )

    async def send_heartbeat(
        self,
        *,
        type: str = "heartbeat",
        request_id: str | None = None,
        correlation_id: str | None = None,
        action: str = "heartbeat",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {"session_id": self.session_id, "ts": datetime.now(UTC).isoformat()}
        if isinstance(data, dict):
            payload.update(data)
        return await self.send_response(
            type=type,
            request_id=request_id,
            correlation_id=correlation_id,
            action=action,
            data=payload,
            final=False,
        )

    def track_request(self, request_id: str, task: asyncio.Task[Any]) -> None:
        normalized = str(request_id or "").strip()
        if not normalized:
            raise SessionProtocolError("request_id is required for request tracking")
        self._request_tasks[normalized] = task
        task.add_done_callback(lambda _: self._request_tasks.pop(normalized, None))

    async def cancel_request(self, request_id: str) -> bool:
        normalized = str(request_id or "").strip()
        task = self._request_tasks.get(normalized)
        if task is None:
            return False
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._request_tasks.pop(normalized, None)
        return True

    async def cancel_all_requests(self) -> None:
        request_ids = list(self._request_tasks)
        for request_id in request_ids:
            await self.cancel_request(request_id)

    async def register_subscription(
        self,
        subscription_id: str,
        *,
        topic: str,
        metadata: dict[str, Any] | None = None,
        unsubscribe: Callable[[], Awaitable[None] | None] | None = None,
    ) -> SubscriptionBinding:
        binding = SubscriptionBinding(
            subscription_id=str(subscription_id),
            topic=str(topic),
            metadata=dict(metadata or {}),
            unsubscribe=unsubscribe,
        )
        self._subscriptions[binding.subscription_id] = binding
        if self.on_subscribe is not None:
            await _maybe_await(self.on_subscribe(self, binding))
        return binding

    async def remove_subscription(self, subscription_id: str) -> bool:
        binding = self._subscriptions.pop(str(subscription_id), None)
        if binding is None:
            return False
        if binding.unsubscribe is not None:
            await _maybe_await(binding.unsubscribe())
        if self.on_unsubscribe is not None:
            await _maybe_await(self.on_unsubscribe(self, binding))
        return True

    async def clear_subscriptions(self) -> None:
        for subscription_id in list(self._subscriptions):
            await self.remove_subscription(subscription_id)

    async def handle_control_frame(self, frame: Mapping[str, Any]) -> bool:
        frame_type = str(frame.get("type") or "").strip().lower()
        request_id = str(frame.get("request_id") or "").strip() or None
        correlation_id = str(frame.get("correlation_id") or "").strip() or None
        action = str(frame.get("action") or frame_type or "").strip() or None
        if frame_type == "ping":
            await self.send_heartbeat(
                type="pong",
                request_id=request_id,
                correlation_id=correlation_id,
                action=action or "ping",
            )
            return True
        if frame_type == "heartbeat":
            await self.send_heartbeat(
                type="heartbeat",
                request_id=request_id,
                correlation_id=correlation_id,
                action=action or "heartbeat",
                data={"ack": True},
            )
            return True
        if frame_type == "cancel":
            target = request_id or correlation_id
            cancelled = await self.cancel_request(target or "")
            await self.send_ack(
                request_id=request_id,
                correlation_id=correlation_id,
                action=action or "cancel",
                data={"cancelled": cancelled, "target_request_id": target},
                final=cancelled,
            )
            return True
        if frame_type == "unsubscribe":
            payload = frame.get("data") if isinstance(frame.get("data"), Mapping) else {}
            subscription_id = str(payload.get("subscription_id") or frame.get("subscription_id") or "").strip()
            removed = await self.remove_subscription(subscription_id)
            await self.send_ack(
                request_id=request_id,
                correlation_id=correlation_id,
                action=action or "unsubscribe",
                data={"subscription_id": subscription_id, "unsubscribed": removed},
                final=removed,
            )
            return True
        return False

    def start_heartbeat(self) -> asyncio.Task[None]:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self._heartbeat_task

    async def stop_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._heartbeat_task
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.heartbeat_interval_sec)
                if self._closed:
                    return
                await self.send_heartbeat()
        except asyncio.CancelledError:
            raise
        except SessionError:
            return

    async def close(self, *, code: int = 1000, reason: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        await self.stop_heartbeat()
        await self.cancel_all_requests()
        await self.clear_subscriptions()
        with suppress(Exception):
            await self.websocket.close(code=code, reason=reason)
        if self.on_disconnect is not None:
            await _maybe_await(self.on_disconnect(self))
