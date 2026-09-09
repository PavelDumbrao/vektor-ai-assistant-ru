from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import egress_proxy


def test_direct_private_and_metadata_ips_are_rejected():
    for host in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "::1", "fc00::1", "fe80::1"):
        with pytest.raises(ValueError, match="non_public_host"):
            egress_proxy.resolve_public(host, 443)


def test_only_http_https_default_ports_are_allowed(monkeypatch):
    monkeypatch.setattr(egress_proxy, "resolve_public", lambda host, port: ["93.184.216.34"])
    assert egress_proxy.validate_public_url("https://example.com/a") == "https://example.com/a"
    assert egress_proxy.validate_public_url("http://example.com/a") == "http://example.com/a"
    for url in ("file:///etc/passwd", "ftp://example.com/a", "https://u:p@example.com/", "https://example.com:8443/"):
        with pytest.raises(ValueError):
            egress_proxy.validate_public_url(url)


def _ai(ip: str, port: int = 443):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    addr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", addr)


def test_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(egress_proxy.socket, "getaddrinfo", lambda *args, **kwargs: [
        _ai("93.184.216.34"), _ai("127.0.0.1")
    ])
    with pytest.raises(ValueError, match="non_public_host"):
        egress_proxy.resolve_public("rebind.example", 443)


def test_dns_is_revalidated_instead_of_cached(monkeypatch):
    calls = {"n": 0}
    def fake_dns(*args, **kwargs):
        calls["n"] += 1
        return [_ai("93.184.216.34")] if calls["n"] == 1 else [_ai("10.0.0.7")]
    monkeypatch.setattr(egress_proxy.socket, "getaddrinfo", fake_dns)
    assert egress_proxy.resolve_public("flip.example", 443) == ["93.184.216.34"]
    with pytest.raises(ValueError, match="non_public_host"):
        egress_proxy.resolve_public("flip.example", 443)
    assert calls["n"] == 2


def test_loopback_proxy_refuses_private_connect_target():
    with egress_proxy.PublicEgressProxy() as proxy:
        port = proxy.server.server_port
        sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        try:
            sock.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n")
            response = sock.recv(512)
        finally:
            sock.close()
    assert b" 403 " in response


def test_egress_budget_is_fail_closed():
    budget = egress_proxy.EgressBudget(1024 * 1024)
    assert budget.consume(800_000) is True
    assert budget.consume(300_000) is False
    assert budget.exceeded is True


def test_capture_runtime_installs_proxy_and_wrapper(tmp_path):
    import importlib.util
    module_dir = tmp_path / "module"
    target = tmp_path / "capture"
    module_dir.mkdir()
    (module_dir / "safe_capture.py").write_text("# safe\n")
    (module_dir / "egress_proxy.py").write_text("# proxy\n")
    spec = importlib.util.spec_from_file_location(
        "prepare_enrichment_runtime_test",
        MODULE / "prepare_enrichment_runtime.py",
    )
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    runtime.install_capture_wrappers(module_dir, target)
    assert (target / "safe_capture.py").is_file()
    assert (target / "egress_proxy.py").is_file()
    assert (target / "safe_capture.py").stat().st_mode & 0o111
    assert not ((target / "egress_proxy.py").stat().st_mode & 0o111)
