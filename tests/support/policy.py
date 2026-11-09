"""Test-only IP policy: the loopback media server counts as public."""
from ipaddress import IPv4Address

from app.netguard import IPAddress, is_public_ip

_TEST_SERVER = IPv4Address("127.0.0.1")


def allow_loopback(ip: IPAddress) -> bool:
    return ip == _TEST_SERVER or is_public_ip(ip)
