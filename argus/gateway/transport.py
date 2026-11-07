"""SSRF and DNS-rebinding safe HTTP transport for Argus Gateway.

Enforces connection-time socket peer IP validation before any TLS handshake or HTTP
payload is transmitted, preventing DNS rebinding TOCTOU vulnerabilities.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Dict, Optional, Set
import anyio
import anyio.abc
import httpcore
from httpcore._backends.anyio import AnyIOBackend
import httpx

logger = logging.getLogger("argus.gateway.transport")

BLOCKED_METADATA_IPS: Set[str] = {
    "169.254.169.254",
    "100.100.100.200",
}


class SafeAnyIOBackend(AnyIOBackend):
    """AnyIO network backend that enforces peer IP safety at connection time."""

    def __init__(self, allowed_hosts: Optional[Set[str]] = None) -> None:
        super().__init__()
        # By default, local loopback runtimes (Ollama, vLLM) are only authorized
        # when their hostname is explicitly 127.0.0.1, localhost, or ::1.
        self.allowed_hosts: Set[str] = allowed_hosts if allowed_hosts is not None else {"127.0.0.1", "localhost", "::1"}

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        """Connect TCP socket and verify peer IP before returning the stream."""
        host_lower = host.lower().strip()
        if host_lower == "api.groq.com" or host_lower.endswith(".groq.com") or "groq.com" in host_lower:
            raise httpcore.ConnectError("Groq endpoints are strictly prohibited across all of Argus.")

        network_stream = await super().connect_tcp(
            host=host,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

        try:
            peer_ip_str: Optional[str] = None
            raw_stream = getattr(network_stream, "_stream", None)
            if raw_stream is not None and hasattr(raw_stream, "extra"):
                try:
                    peer = raw_stream.extra(anyio.abc.SocketAttribute.remote_address)
                    if peer and len(peer) > 0:
                        peer_ip_str = str(peer[0])
                except Exception:
                    pass

            if not peer_ip_str and raw_stream is not None and hasattr(raw_stream, "_raw_socket"):
                try:
                    peer = raw_stream._raw_socket.getpeername()
                    if peer and len(peer) > 0:
                        peer_ip_str = str(peer[0])
                except Exception:
                    pass

            if peer_ip_str:
                ip = ipaddress.ip_address(peer_ip_str)
                if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                    ip = ip.ipv4_mapped

                is_restricted = (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                    or ip.is_unspecified
                    or str(ip) in BLOCKED_METADATA_IPS
                )

                if is_restricted:
                    host_lower = host.lower().strip()
                    # Only allow connection if the target hostname is explicitly authorized
                    if host_lower not in self.allowed_hosts:
                        await network_stream.aclose()
                        logger.warning(
                            f"SSRF / DNS rebinding blocked at connection time: host '{host}' "
                            f"resolved to restricted peer IP '{peer_ip_str}'."
                        )
                        raise httpcore.ConnectError(
                            f"SSRF blocked at connection time: host '{host}' resolved to restricted IP '{peer_ip_str}'."
                        )
        except Exception:
            await network_stream.aclose()
            raise

        return network_stream


class SafeAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """AsyncHTTPTransport configured with SafeAnyIOBackend for rebinding protection."""

    def __init__(
        self,
        allowed_hosts: Optional[Set[str]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Re-initialize the underlying pool with our safe backend
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._pool._ssl_context,
            proxy=self._pool._proxy,
            max_connections=self._pool._max_connections,
            max_keepalive_connections=self._pool._max_keepalive_connections,
            keepalive_expiry=self._pool._keepalive_expiry,
            http1=self._pool._http1,
            http2=self._pool._http2,
            retries=self._pool._retries,
            local_address=self._pool._local_address,
            uds=self._pool._uds,
            network_backend=SafeAnyIOBackend(allowed_hosts=allowed_hosts),
            socket_options=self._pool._socket_options,
        )


def create_safe_async_client(
    allowed_hosts: Optional[Set[str]] = None,
    timeout: float = 120.0,
    follow_redirects: bool = False,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient backed by SafeAsyncHTTPTransport."""
    transport = kwargs.pop("transport", None)
    if transport is None:
        transport = SafeAsyncHTTPTransport(
            allowed_hosts=allowed_hosts,
            verify=kwargs.pop("verify", True),
            limits=kwargs.pop("limits", httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)),
        )
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=follow_redirects,
        **kwargs,
    )
