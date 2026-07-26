"""Bounded OpenAI-compatible BYOK client with SSRF controls."""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx
import httpcore


Resolver = Callable[..., list[tuple[Any, ...]]]


class ModelGatewayError(RuntimeError):
    """A stable, secret-free model gateway failure."""

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = str(code)[:64]
        self.public_message = str(public_message)[:240]


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class ResolvedModelEndpoint:
    """A normalized endpoint plus the public IPs approved for this call."""

    base_url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect only to IPs approved by the immediately preceding DNS check.

    TLS still receives the original hostname from httpcore, so certificate and
    SNI validation remain bound to the configured provider domain.  Replacing
    only the TCP destination closes the DNS-rebinding gap between validation
    and the actual outbound connection.
    """

    def __init__(self, hostname: str, port: int, addresses: Sequence[str]) -> None:
        self._hostname = _normalized_hostname(hostname)
        self._port = int(port)
        self._addresses = tuple(str(address) for address in addresses)
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.NetworkStream:
        requested_host = _normalized_hostname(
            host.decode("ascii") if isinstance(host, bytes) else host
        )
        if (
            requested_host != self._hostname
            or int(port) != self._port
            or not self._addresses
        ):
            raise httpcore.ConnectError("model endpoint is not pinned")
        last_error: Exception | None = None
        started = time.monotonic()
        for address in self._addresses:
            remaining_timeout = timeout
            if timeout is not None:
                remaining_timeout = max(
                    0.0, float(timeout) - (time.monotonic() - started)
                )
                if remaining_timeout <= 0:
                    raise httpcore.ConnectTimeout(
                        "model endpoint connection timed out"
                    )
            try:
                return self._backend.connect_tcp(
                    address,
                    port,
                    timeout=remaining_timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("model endpoint has no approved address")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed for model endpoints")

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport backed by a single DNS-pinned provider endpoint."""

    def __init__(self, endpoint: ResolvedModelEndpoint) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, trust_env=False),
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PinnedNetworkBackend(
                endpoint.hostname, endpoint.port, endpoint.addresses
            ),
        )


def _is_production(settings: Any) -> bool:
    return str(getattr(settings, "environment", "production") or "").strip().lower() in {
        "prod",
        "production",
    }


def _host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    hostname = str(host or "").strip(".").lower()
    for pattern in allowed_hosts:
        value = str(pattern or "").strip(".").lower()
        if not value or value == "*":
            continue
        if value.startswith("*."):
            suffix = value[1:]
            if hostname.endswith(suffix) and hostname != suffix.lstrip("."):
                return True
        elif hostname == value:
            return True
    return False


def _normalized_hostname(value: str) -> str:
    try:
        return str(value or "").strip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ModelGatewayError("endpoint_invalid", "模型服务域名格式无效") from exc


def resolve_model_base_url(
    value: str,
    settings: Any,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> ResolvedModelEndpoint:
    """Resolve and approve one public HTTPS endpoint for an outbound call."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 512 or any(ord(char) < 32 for char in raw):
        raise ModelGatewayError("endpoint_invalid", "请输入有效的模型服务地址")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ModelGatewayError("endpoint_invalid", "模型服务地址格式无效") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelGatewayError(
            "endpoint_invalid",
            "模型服务必须使用不含账号、查询参数和片段的 HTTPS 地址",
        )
    if port is not None and not 1 <= port <= 65535:
        raise ModelGatewayError("endpoint_invalid", "模型服务端口无效")

    host = _normalized_hostname(parsed.hostname)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ModelGatewayError("endpoint_ip_literal", "模型服务地址必须使用域名")

    allowed_hosts = tuple(getattr(settings, "ai_byok_allowed_hosts", ()) or ())
    if (_is_production(settings) or allowed_hosts) and not _host_allowed(
        host, allowed_hosts
    ):
        raise ModelGatewayError(
            "endpoint_not_allowlisted",
            "该模型服务域名未被系统管理员加入白名单",
        )
    if _is_production(settings) and port not in {None, 443}:
        raise ModelGatewayError(
            "endpoint_port_not_allowed", "正式环境只允许模型服务使用 HTTPS 标准端口"
        )

    try:
        addresses = tuple(
            dict.fromkeys(
                str(item[4][0])
                for item in resolver(host, port or 443, type=socket.SOCK_STREAM)
            )
        )
    except OSError as exc:
        raise ModelGatewayError(
            "endpoint_unresolvable", "模型服务域名当前无法解析"
        ) from exc
    if not addresses:
        raise ModelGatewayError("endpoint_unresolvable", "模型服务域名当前无法解析")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError as exc:
            raise ModelGatewayError(
                "endpoint_unresolvable", "模型服务域名解析结果无效"
            ) from exc
        if not address.is_global:
            raise ModelGatewayError(
                "endpoint_private_address",
                "模型服务域名不能解析到内网、本机或保留地址",
            )
    addresses = addresses[:8]

    path = parsed.path.rstrip("/")
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return ResolvedModelEndpoint(
        base_url=urlunsplit(("https", netloc, path, "", "")),
        hostname=host,
        port=port or 443,
        addresses=addresses,
    )


def validate_model_base_url(
    value: str,
    settings: Any,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Return a normalized public HTTPS base URL or raise a stable error."""

    return resolve_model_base_url(value, settings, resolver=resolver).base_url


def chat_completions_url(base_url: str) -> str:
    normalized = str(base_url or "").rstrip("/")
    return (
        normalized
        if normalized.endswith("/chat/completions")
        else f"{normalized}/chat/completions"
    )


def validate_api_key(value: str) -> str:
    key = str(value or "")
    if not 8 <= len(key) <= 8192 or key != key.strip():
        raise ModelGatewayError("api_key_invalid", "API Key 格式无效")
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in key):
        raise ModelGatewayError(
            "api_key_invalid", "API Key 只能包含可见 ASCII 字符且不能包含空格"
        )
    return key


def _usage_value(usage: Mapping[str, Any], key: str) -> int | None:
    try:
        value = int(usage.get(key))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _message_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ModelGatewayError("invalid_model_response", "模型服务返回格式无效")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ModelGatewayError("invalid_model_response", "模型服务返回格式无效")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ModelGatewayError("invalid_model_response", "模型服务没有返回消息内容")
    content = message.get("content")
    if isinstance(content, str):
        result = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            text_value = item.get("text")
            if isinstance(text_value, str):
                parts.append(text_value)
        result = "".join(parts).strip()
    else:
        result = ""
    if not result:
        raise ModelGatewayError("empty_model_response", "模型没有返回可用文本")
    if len(result) > 32_000:
        raise ModelGatewayError("model_output_too_large", "模型返回内容超过允许长度")
    return result


class OpenAICompatibleGateway:
    def __init__(
        self,
        settings: Any,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.resolver = resolver

    def complete(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: Sequence[Mapping[str, str]],
        temperature: float,
        max_output_tokens: int,
    ) -> ModelCompletion:
        resolved_endpoint = resolve_model_base_url(
            base_url, self.settings, resolver=self.resolver
        )
        endpoint = chat_completions_url(resolved_endpoint.base_url)
        key = validate_api_key(api_key)
        normalized_model = str(model or "").strip()
        if not normalized_model or len(normalized_model) > 160:
            raise ModelGatewayError("model_invalid", "模型名称无效")
        payload = {
            "model": normalized_model,
            "messages": [dict(item) for item in messages],
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "max_tokens": max(1, min(int(max_output_tokens), 4096)),
            "stream": False,
        }
        started = time.monotonic()
        try:
            connect_timeout = float(
                getattr(self.settings, "ai_byok_connect_timeout_seconds", 10)
            )
            read_timeout = float(
                getattr(self.settings, "ai_byok_read_timeout_seconds", 60)
            )
            total_timeout = connect_timeout + read_timeout
            timeout = httpx.Timeout(
                read_timeout,
                connect=connect_timeout,
            )
            transport = self.transport or _PinnedHTTPTransport(resolved_endpoint)
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client:
                with client.stream(
                    "POST",
                    endpoint,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "User-Agent": "bbw-byok-model-runner/1",
                    },
                    json=payload,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise ModelGatewayError(
                            "provider_redirect_rejected",
                            "模型服务返回了不允许的重定向",
                        )
                    if response.status_code in {401, 403}:
                        raise ModelGatewayError(
                            "provider_auth_failed", "模型服务拒绝了当前 API Key"
                        )
                    if response.status_code == 429:
                        raise ModelGatewayError(
                            "provider_rate_limited", "模型服务当前请求过多，请稍后重试"
                        )
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ModelGatewayError(
                            "provider_rejected", "模型服务未接受本次请求"
                        )
                    limit = int(
                        getattr(self.settings, "ai_byok_max_response_bytes", 1024 * 1024)
                    )
                    try:
                        declared = int(response.headers.get("Content-Length") or 0)
                    except ValueError:
                        declared = 0
                    if declared > limit:
                        raise ModelGatewayError(
                            "provider_response_too_large", "模型服务返回内容超过系统限制"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > total_timeout:
                            raise ModelGatewayError(
                                "provider_timeout", "模型服务响应超时"
                            )
                        size += len(chunk)
                        if size > limit:
                            raise ModelGatewayError(
                                "provider_response_too_large",
                                "模型服务返回内容超过系统限制",
                            )
                        chunks.append(chunk)
        except ModelGatewayError:
            raise
        except httpx.TimeoutException as exc:
            raise ModelGatewayError("provider_timeout", "模型服务响应超时") from exc
        except httpx.HTTPError as exc:
            raise ModelGatewayError(
                "provider_connection_failed", "无法连接模型服务"
            ) from exc
        finally:
            key = ""

        try:
            decoded = json.loads(b"".join(chunks))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ModelGatewayError(
                "invalid_model_response", "模型服务返回了无法识别的内容"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise ModelGatewayError("invalid_model_response", "模型服务返回格式无效")
        text = _message_text(decoded)
        usage = decoded.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else {}
        return ModelCompletion(
            text=text,
            input_tokens=_usage_value(usage_map, "prompt_tokens"),
            output_tokens=_usage_value(usage_map, "completion_tokens"),
            total_tokens=_usage_value(usage_map, "total_tokens"),
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    def test_connection(
        self, *, base_url: str, api_key: str, model: str
    ) -> ModelCompletion:
        return self.complete(
            base_url=base_url,
            api_key=api_key,
            model=model,
            messages=(
                {
                    "role": "system",
                    "content": "Reply with the single word OK. Do not add punctuation.",
                },
                {"role": "user", "content": "Connection test"},
            ),
            temperature=0,
            max_output_tokens=4,
        )
