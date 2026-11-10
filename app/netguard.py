"""SSRF defence: the IP policy, URL pre-validation (layer 1) and the
connect-time socket guard (layer 2, installed only in worker processes)."""
import asyncio
import functools
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlsplit

from app.errors import BLOCKED_MARKER, AppError

IPAddress = IPv4Address | IPv6Address
IpPolicy = Callable[[IPAddress], bool]
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_COMPAT = ipaddress.ip_network("::/96")
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")
_MAX_URL_LEN = 2048
# Pasted share text may surround the link, but the whole input is capped before any work.
_MAX_INPUT_LEN = 4 * _MAX_URL_LEN
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
    if len(raw) > _MAX_INPUT_LEN:
        raise _invalid()
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
    await check_source_address(url, policy=policy, resolver=resolver, timeout=timeout)
    return url


async def check_source_address(url: str, *, policy: IpPolicy = is_public_ip,
                               resolver: Resolver = system_resolver, timeout: float = 5.0) -> None:
    """Resolve a URL that passed check_url_syntax; every address must pass `policy`."""
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


# --- Layer 2: connect-time guard ------------------------------------------

_INET = (socket.AF_INET, socket.AF_INET6)
_installed = False


class BlockedAddressError(ConnectionError):
    """A connection the IP policy refuses. str() starts with BLOCKED_MARKER."""


def _blocked(what: object) -> BlockedAddressError:
    return BlockedAddressError(f"{BLOCKED_MARKER} {what} is not a public address")


def _parse_ip(host: object) -> IPAddress | None:
    """The IP a host string stands for literally (zone id ignored), else None."""
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(host, str):
        return None
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def install_connect_guard(policy: IpPolicy = is_public_ip, *,
                          resolve: Callable | None = None) -> None:
    """Make every connection this process opens pass `policy` on the IP actually
    connected. Process-wide and permanent; a second call does nothing.

    Patches the Python-level socket.socket.connect/connect_ex (which every
    instance, SSLSocket included, goes through), socket.getaddrinfo and
    socket.create_connection. Hostnames are resolved through `resolve` (default:
    the unpatched getaddrinfo), every answer is checked, and the socket connects
    to the checked IP itself, so nothing re-resolves after the check.
    """
    global _installed
    if _installed:
        return

    orig_getaddrinfo = socket.getaddrinfo
    orig_create_connection = socket.create_connection
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    lookup = resolve or orig_getaddrinfo

    def require_public(ip: IPAddress | None, raw: object) -> IPAddress:
        if ip is None or not policy(ip):
            raise _blocked(raw if ip is None else ip)
        return ip

    def checked_answers(host: object, port: object) -> list[tuple[int, IPAddress]]:
        literal = _parse_ip(host)
        if literal is not None:
            family = socket.AF_INET if literal.version == 4 else socket.AF_INET6
            return [(family, require_public(literal, host))]
        answers = []
        for family, _type, _proto, _canon, sockaddr in lookup(host, port, 0, socket.SOCK_STREAM):
            ip = _parse_ip(sockaddr[0]) if family in _INET else None
            answers.append((family, require_public(ip, sockaddr[0])))
        if not answers:
            raise socket.gaierror(socket.EAI_NONAME, f"no addresses for {host!r}")
        return answers

    def checked_target(sock: socket.socket, address: object) -> tuple:
        family = sock.family
        if family not in _INET:
            raise _blocked(f"{getattr(family, 'name', family)} address {address!r}")
        if not isinstance(address, tuple) or len(address) < 2:
            raise _blocked(repr(address))
        host, port, *rest = address  # rest: IPv6 flowinfo and scope_id, kept as given
        ip = _parse_ip(host)
        if ip is not None:
            require_public(ip, host)
        else:
            ip = next((a for fam, a in checked_answers(host, port) if fam == family), None)
            if ip is None:
                raise socket.gaierror(socket.EAI_NONAME,
                                      f"no {family.name} address for {host!r}")
        return (str(ip), port, *rest)

    @functools.wraps(orig_connect)
    def connect(self, address):
        return orig_connect(self, checked_target(self, address))

    @functools.wraps(orig_connect_ex)
    def connect_ex(self, address):
        return orig_connect_ex(self, checked_target(self, address))

    @functools.wraps(orig_getaddrinfo)
    def getaddrinfo(*args, **kwargs):
        answers = orig_getaddrinfo(*args, **kwargs)
        for family, _type, _proto, _canon, sockaddr in answers:
            if family in _INET:
                require_public(_parse_ip(sockaddr[0]), sockaddr[0])
        return answers

    @functools.wraps(orig_create_connection)
    def create_connection(address, *args, **kwargs):
        # Fail early, before any socket exists, using the guard's own resolver.
        # The original then still goes through the guarded getaddrinfo and connect.
        host, port = address
        checked_answers(host, port)
        return orig_create_connection(address, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection
    _installed = True
