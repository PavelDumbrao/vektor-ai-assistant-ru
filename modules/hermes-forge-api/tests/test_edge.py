from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("forge_edge_install", ROOT / "install_edge.py")
edge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(edge)


def test_hostname_validation():
    assert edge.hostname("Forge.Srv1250550.Hstgr.Cloud.") == "forge.srv1250550.hstgr.cloud"
    for value in ("", "https://forge.example.com", "bad..example.com", "-bad.example.com"):
        with pytest.raises(ValueError, match="hostname_invalid"):
            edge.hostname(value)


def test_bridge_is_private_docker_gateway_only():
    unit = (ROOT / "proai-hermes-forge-bridge.service").read_text()
    assert "bind=172.18.0.1" in unit
    assert "TCP:127.0.0.1:8650" in unit
    assert "DynamicUser=yes" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=172.18.0.0/16" in unit


def test_edge_compose_has_tls_router_without_public_port_mapping():
    compose = (ROOT / "deploy" / "edge" / "docker-compose.yml").read_text()
    assert "ports:" not in compose
    assert "n8n_default" in compose
    assert "external: true" in compose
    assert "host.docker.internal:172.18.0.1" in compose
    assert "Host(`${FORGE_HOSTNAME}`)" in compose
    assert "tls.certresolver=mytlschallenge" in compose
    assert "loadbalancer.server.port=8080" in compose


def test_nginx_edge_bounds_body_rate_and_upstream():
    nginx = (ROOT / "deploy" / "edge" / "nginx.conf").read_text()
    assert "listen 8080;" in nginx
    assert "client_max_body_size 64k;" in nginx
    assert "rate=20r/s" in nginx
    assert "burst=40" in nginx
    assert "proxy_pass http://host.docker.internal:8650;" in nginx
    assert "proxy_read_timeout 95s;" in nginx
