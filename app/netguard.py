"""SSRF defence, layer 1: IP policy and URL pre-validation."""
import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlsplit

from app.errors import AppError

IPAddress = IPv4Address | IPv6Address
IpPolicy = Callable[[IPAddress], bool]
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_COMPAT = ipaddress.ip_network("::/96")
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")
_MAX_URL_LEN = 2048
_URL_TOKEN = re.compile(r"https?://\S+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _ok(ip: IPAddress) -> bool:
    return ip.is_global and not ip.is_multicast


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable, non-multicast addresses.

    Rule: IPv4 is checked directly. For IPv6 that embeds an IPv4:
    - IPv4-mapped (::ffff:a.b.c.d) and IPv4-compatible (::/96 except :: and ::1):
      decided on the embedded IPv4 alone (the outer form is not reliably
      classified by is_global on Python 3.12).
    - SIIT (::ffff:0:0:0/96): decided on the low 32 bits like the above.
    - Deprecated site-local fec0::/10 is always blocked.
    - 6to4 (2002::/16), Teredo and NAT64 (64:ff9b::/96, 64:ff9b:1::/48, low 32 bits):
      the outer address AND the embedded IPv4 must both pass.
    Every other IPv6 address must pass is_global and not be multicast.
    """
    if isinstance(ip, IPv4Address):
        return _ok(ip)
    if ip in _SITE_LOCAL:
        return False
    if ip.ipv4_mapped is not None:
        return _ok(ip.ipv4_mapped)
    if ip in _SIIT:
        return _ok(IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip in _COMPAT and int(ip) > 1:
        return _ok(IPv4Address(int(ip) & 0xFFFFFFFF))
    embedded: IPv4Address | None = None
    if ip.sixtofour is not None:
        embedded = ip.sixtofour
    elif ip.teredo is not None:
        embedded = ip.teredo[1]
    elif any(ip in n for n in _NAT64):
        embedded = IPv4Address(int(ip) & 0xFFFFFFFF)
    if embedded is not None and not _ok(embedded):
        return False
    return _ok(ip)


def _invalid(message: str | None = None) -> AppError:
    return AppError("invalid_url", message)


def _trim_trailing(token: str) -> str:
    while token:
        if token[-1] in ".,;:!?'\">]}":
            token = token[:-1]
        elif token[-1] == ")" and token.count(")") > token.count("("):
            token = token[:-1]
        else:
            break
    return token


def check_url_syntax(raw: str) -> str:
    text = raw.strip()
    if re.search(r"\s", text):
        tokens = _URL_TOKEN.findall(text)
        if len(tokens) != 1:
            raise _invalid()
        text = _trim_trailing(tokens[0])
    if not text or len(text) > _MAX_URL_LEN or _CONTROL.search(text):
        raise _invalid()
    try:
        parts = urlsplit(text)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise _invalid()
        if parts.username is not None or parts.password is not None:
            raise _invalid("Links with login details aren't supported.")
        parts.hostname.encode("idna")
        parts.port  # noqa: B018 - raises ValueError when out of range
    except ValueError:
        raise _invalid() from None
    return text


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[IPAddress] = []
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if addr not in out:
            out.append(addr)
    return out


async def validate_source_url(raw: str, *, policy: IpPolicy = is_public_ip,
                              resolver: Resolver = system_resolver, timeout: float = 5.0) -> str:
    url = check_url_syntax(raw)
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    try:
        ips = await asyncio.wait_for(resolver(parts.hostname, port), timeout)
    except (asyncio.TimeoutError, socket.gaierror, OSError, UnicodeError, ValueError):
        raise AppError("unreachable") from None
    if not ips:
        raise AppError("unreachable")
    if not all(policy(ip) for ip in ips):
        raise AppError("blocked_address")
    return url
