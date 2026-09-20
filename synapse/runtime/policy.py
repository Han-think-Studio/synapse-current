"""Deterministic endpoint and data-egress policy checks."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from synapse.runtime.contracts import DATA_EGRESS_POLICIES, NETWORK_SCOPES


class RuntimePolicyError(ValueError):
    """Raised when a runtime endpoint violates its declared boundary."""


def network_scope_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme == "pipe":
        if not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise RuntimePolicyError("local IPC endpoint 형식이 올바르지 않습니다.")
        return "none"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimePolicyError("runtime endpoint는 http(s) URL이어야 합니다.")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimePolicyError("runtime endpoint에 userinfo를 포함할 수 없습니다.")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost":
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "remote"
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "lan"
    return "remote"


def validate_endpoint_policy(
    url: str,
    *,
    network_scope: str,
    data_egress: str,
) -> str:
    """Validate an endpoint against adapter and request egress declarations."""

    if network_scope not in NETWORK_SCOPES:
        raise RuntimePolicyError(f"지원하지 않는 network_scope입니다: {network_scope}")
    if data_egress not in DATA_EGRESS_POLICIES:
        raise RuntimePolicyError(f"지원하지 않는 data_egress 정책입니다: {data_egress}")
    actual = network_scope_for_url(url)
    allowed = {"none": {"none"}, "loopback": {"loopback"}, "lan": {"loopback", "lan"}, "remote": {"loopback", "lan", "remote"}}[network_scope]
    if actual not in allowed:
        raise RuntimePolicyError(
            f"endpoint network_scope 위반입니다: declared={network_scope}, actual={actual}"
        )
    if data_egress == "prohibited" and actual != "none":
        raise RuntimePolicyError("HTTP provider는 data_egress=prohibited를 사용할 수 없습니다.")
    if data_egress == "local_only" and actual not in {"none", "loopback"}:
        raise RuntimePolicyError("local_only 요청은 loopback endpoint만 사용할 수 있습니다.")
    return actual


__all__ = ["RuntimePolicyError", "network_scope_for_url", "validate_endpoint_policy"]
