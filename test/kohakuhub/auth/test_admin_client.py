"""Who is the "client" of an admin request: the address the server can trust, not one the caller wrote."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kohakuhub.auth.admin_client import ClientIdentity, in_allowlist, resolve_client


class Headers:
    """Starlette-like headers: several lines of one name are kept apart."""

    def __init__(self, **lines):
        self._lines = {name.replace("_", "-").lower(): values for name, values in lines.items()}

    def getlist(self, name):
        return list(self._lines.get(name.lower(), []))


def request(peer="10.0.0.9", **forwarded):
    headers = Headers(**forwarded)
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None, headers=headers)


def test_without_trusted_proxies_the_peer_is_the_client_and_the_header_is_ignored():
    identity = resolve_client(request(x_forwarded_for=["6.6.6.6"]), hops=0)
    # A forwarded header with no trusted proxy means every client shares the peer: not trustworthy enough to wait on.
    assert identity == ClientIdentity(key="10.0.0.9", address="10.0.0.9", trusted=False)

    plain = resolve_client(request(), hops=0)
    assert plain == ClientIdentity(key="10.0.0.9", address="10.0.0.9", trusted=True)


def test_one_trusted_proxy_the_last_address_is_the_client():
    # The proxy appends the address it saw; whatever the caller put in front is theirs to forge.
    identity = resolve_client(request(x_forwarded_for=["1.2.3.4, 203.0.113.7"]), hops=1)
    assert identity == ClientIdentity(key="203.0.113.7", address="203.0.113.7", trusted=True)


def test_two_trusted_proxies_count_from_the_right():
    identity = resolve_client(request(x_forwarded_for=["forged, 198.51.100.4, 172.16.0.2"]), hops=2)
    assert identity.address == "198.51.100.4"


def test_several_forwarded_header_lines_are_read_as_one_list():
    # Two header lines: the proxy appended its address to the last one only.
    identity = resolve_client(request(x_forwarded_for=["9.9.9.9", "8.8.8.8, 203.0.113.7"]), hops=1)
    assert identity.address == "203.0.113.7"
    identity = resolve_client(request(x_forwarded_for=["9.9.9.9", "8.8.8.8, 203.0.113.7"]), hops=3)
    assert identity.address == "9.9.9.9"


def test_a_chain_shorter_than_the_hops_falls_back_to_the_peer_and_is_not_trusted():
    identity = resolve_client(request(x_forwarded_for=["203.0.113.7"]), hops=2)
    assert identity == ClientIdentity(key="10.0.0.9", address="10.0.0.9", trusted=False)

    identity = resolve_client(request(), hops=1)  # a proxy was promised but sent nothing
    assert identity.trusted is False and identity.address == "10.0.0.9"


def test_an_address_that_is_not_an_address_falls_back_to_the_peer():
    identity = resolve_client(request(x_forwarded_for=["1.2.3.4, not-an-ip"]), hops=1)
    assert identity == ClientIdentity(key="10.0.0.9", address="10.0.0.9", trusted=False)


def test_ipv6_clients_are_keyed_by_their_64_and_ipv4_mapped_ones_by_their_ipv4():
    one = resolve_client(request(x_forwarded_for=["2001:db8:abcd:12:aaaa::1"]), hops=1)
    other = resolve_client(request(x_forwarded_for=["2001:db8:abcd:12:bbbb::99"]), hops=1)
    elsewhere = resolve_client(request(x_forwarded_for=["2001:db8:abcd:13::1"]), hops=1)
    assert one.key == other.key == "2001:db8:abcd:12::/64"
    assert one.key != elsewhere.key
    assert one.address == "2001:db8:abcd:12:aaaa::1"  # the allow-list compares the real address

    mapped = resolve_client(request(x_forwarded_for=["::ffff:203.0.113.7"]), hops=1)
    assert mapped.key == "203.0.113.7" and mapped.address == "203.0.113.7"


def test_a_zone_id_is_dropped_and_a_missing_peer_is_unknown():
    identity = resolve_client(request(peer="fe80::1%eth0"), hops=0)
    assert identity.key == "fe80::/64"

    nobody = resolve_client(request(peer=None), hops=0)
    assert nobody == ClientIdentity(key="unknown", address=None, trusted=False)

    bare = resolve_client(SimpleNamespace(), hops=0)  # a stand-in request without client or headers
    assert bare.key == "unknown"


def test_the_allowlist_matches_addresses_and_networks_and_ignores_garbage():
    assert in_allowlist("203.0.113.7", ["203.0.113.0/24"]) is True
    assert in_allowlist("203.0.113.7", ["198.51.100.0/24", "203.0.113.7"]) is True
    assert in_allowlist("2001:db8::5", ["2001:db8::/32"]) is True
    assert in_allowlist("203.0.113.7", ["198.51.100.0/24"]) is False
    assert in_allowlist("203.0.113.7", ["garbage", ""]) is False
    assert in_allowlist(None, ["203.0.113.0/24"]) is False
    assert in_allowlist("203.0.113.7", []) is False
