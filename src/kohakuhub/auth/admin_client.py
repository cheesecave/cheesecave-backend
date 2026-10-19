"""Who is the "client" of an admin request.

The only identity there is before authentication is the network address. Behind proxies the socket peer is the nearest
proxy, so the address comes from ``X-Forwarded-For``, counted from the right: only the addresses the operator's own
proxies appended can be trusted, the left-most ones are written by the caller (MDN, "X-Forwarded-For").
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache

UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClientIdentity:
    key: str  # what the throttle counts by: an IPv4 address, an IPv6 /64, or "unknown"
    address: str | None  # the address itself, for the allow-list
    trusted: bool  # False when the key may be shared by many different people


def _parse(value: str | None):
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip().split("%", 1)[0])
    except ValueError:
        return None


def _normalised(address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _key_of(address) -> str:
    if address.version == 6:
        # A subscriber usually holds a whole /64: counting single addresses would let one rotate through them.
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _forwarded_for(request) -> list[str]:
    lines = request.headers.getlist("x-forwarded-for") if hasattr(request, "headers") else []
    return [part.strip() for line in lines for part in line.split(",") if part.strip()]


def resolve_client(request, hops: int) -> ClientIdentity:
    """The client of ``request``, given how many proxies of ours stand in front of the API."""
    client = getattr(request, "client", None)
    peer = _parse(getattr(client, "host", None))
    peer = _normalised(peer) if peer is not None else None
    forwarded = _forwarded_for(request)

    if hops > 0 and len(forwarded) >= hops:
        chosen = _parse(forwarded[-hops])
        if chosen is not None:
            chosen = _normalised(chosen)
            return ClientIdentity(key=_key_of(chosen), address=str(chosen), trusted=True)

    if peer is None:
        return ClientIdentity(key=UNKNOWN, address=None, trusted=False)
    # Without trusted proxies a forwarded header cannot be believed; and then every client behind the proxy is the
    # same peer, so the key says nothing about one person. (The same when the promised proxy chain is not there.)
    trusted = not forwarded and hops == 0
    return ClientIdentity(key=_key_of(peer), address=str(peer), trusted=trusted)


@lru_cache(maxsize=8)
def _networks(entries: tuple[str, ...]):
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            continue
    return tuple(networks)


def in_allowlist(address: str | None, entries) -> bool:
    """Whether ``address`` is inside one of the allow-listed addresses or networks (garbage entries are skipped)."""
    parsed = _parse(address)
    if parsed is None:
        return False
    return any(parsed in network for network in _networks(tuple(entries)) if network.version == parsed.version)
