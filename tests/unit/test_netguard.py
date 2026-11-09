import socket, ipaddress, asyncio, pytest
from app.netguard import is_public_ip, check_url_syntax, validate_source_url
from app.errors import AppError

BLOCKED = ["127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.5.4", "192.168.1.1", "100.64.0.1",
           "169.254.169.254", "0.0.0.0", "224.0.0.1", "255.255.255.255", "192.0.2.1", "198.18.0.1",
           "::1", "::", "fe80::1", "fc00::1", "fd00:ec2::254", "ff02::1", "::ffff:127.0.0.1",
           "::ffff:10.0.0.1", "::127.0.0.1", "64:ff9b::a00:1", "2002:7f00:1::1", "2001:db8::1"]
ALLOWED = ["93.184.215.14", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"]

@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked_ips(ip): assert not is_public_ip(ipaddress.ip_address(ip))

@pytest.mark.parametrize("ip", ALLOWED)
def test_allowed_ips(ip): assert is_public_ip(ipaddress.ip_address(ip))

@pytest.mark.parametrize("raw", ["", "not a url", "example.com/video", "file:///etc/passwd",
    "ftp://example.com/a.mp4", "javascript:alert(1)", "data:text/html,hi", "http://",
    "http://user:pass@example.com/v", "http://example.com/\x00", "https://example.com/" + "a" * 2100,
    "http://example.com:99999/", "two https://a.com/x links https://b.com/y"])
def test_rejects_invalid(raw):
    with pytest.raises(AppError) as e: check_url_syntax(raw)
    assert e.value.code == "invalid_url"

def test_userinfo_message():
    with pytest.raises(AppError) as e: check_url_syntax("https://u:p@example.com/")
    assert e.value.message == "Links with login details aren't supported."

def test_strips_surrounding_whitespace():
    assert check_url_syntax("  https://example.com/v?id=1 \n") == "https://example.com/v?id=1"

def test_extracts_single_url_from_share_text():
    assert check_url_syntax("Look at this! https://x.com/a/status/1 ") == "https://x.com/a/status/1"

def stub(*ips):
    async def r(host, port): return [ipaddress.ip_address(i) for i in ips]
    return r

async def test_accepts_public(): assert await validate_source_url("https://example.com/v", resolver=stub("93.184.215.14")) == "https://example.com/v"

async def test_any_private_answer_blocks():
    with pytest.raises(AppError) as e: await validate_source_url("https://example.com/", resolver=stub("8.8.8.8", "10.0.0.1"))
    assert (e.value.code, e.value.status) == ("blocked_address", 403)

@pytest.mark.parametrize("raw", ["http://2130706433/", "http://127.1/", "http://[::1]:8000/", "http://169.254.169.254/latest/meta-data/"])
async def test_numeric_and_literal_hosts_blocked(raw):
    with pytest.raises(AppError) as e: await validate_source_url(raw)   # system resolver; numeric forms resolve offline
    assert e.value.code == "blocked_address"

async def test_unresolvable_and_empty():
    async def boom(h, p): raise socket.gaierror("nope")
    async def slow(h, p): await asyncio.sleep(10)
    for r in (boom, stub(), slow):
        with pytest.raises(AppError) as e: await validate_source_url("https://nx.example/", resolver=r, timeout=0.2)
        assert e.value.code == "unreachable"
