"""Test 16: Strict configuration validation for routes (mandatory allow, wildcard, methods typo, duplicate prefix, non-loopback http)."""

import json
import os
import tempfile
import unittest

from keybroker.config import load_routes


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_and_load(self, routes_data: list[dict]):
        path = os.path.join(self.tmpdir.name, "routes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"routes": routes_data}, f)
        return load_routes(path)

    def test_missing_allow_raises_error(self):
        # 1. Missing allow field -> ValueError with route prefix
        bad_routes = [
            {
                "prefix": "/no_allow",
                "upstream": "https://api.example.com",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            self._write_and_load(bad_routes)
        self.assertIn("/no_allow", str(ctx.exception))
        self.assertIn("allow", str(ctx.exception).lower())

    def test_wildcard_allow_opens_route(self):
        # 2. "allow": "*" opens the route
        routes = [
            {
                "prefix": "/open_route",
                "upstream": "https://api.example.com",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": "*",
            }
        ]
        loaded = self._write_and_load(routes)
        self.assertEqual(len(loaded), 1)
        self.assertTrue(loaded[0].is_allowed("GET", "/any/path"))
        self.assertTrue(loaded[0].is_allowed("DELETE", "/anything"))

    def test_method_instead_of_methods_raises_error(self):
        # 3. Typo: 'method' instead of 'methods' in allow rule
        bad_routes = [
            {
                "prefix": "/typo_route",
                "upstream": "https://api.example.com",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": [{"path": "^/test$", "method": ["GET"]}],
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            self._write_and_load(bad_routes)
        self.assertIn("/typo_route", str(ctx.exception))

    def test_duplicate_prefix_raises_error(self):
        # 4. Duplicate prefix across routes
        dup_routes = [
            {
                "prefix": "/dup",
                "upstream": "https://api1.example.com",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": "*",
            },
            {
                "prefix": "/dup",
                "upstream": "https://api2.example.com",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": "*",
            },
        ]
        with self.assertRaises(ValueError) as ctx:
            self._write_and_load(dup_routes)
        self.assertIn("Duplicate route prefix", str(ctx.exception))

    def test_non_loopback_http_raises_error(self):
        # 5. http:// upstream is forbidden for non-loopback
        bad_http = [
            {
                "prefix": "/insecure",
                "upstream": "http://api.remote.com",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": "*",
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            self._write_and_load(bad_http)
        self.assertIn("http:// upstream is only allowed for loopback", str(ctx.exception))

        # But http:// for 127.0.0.1 or localhost is allowed
        allowed_loopback = [
            {
                "prefix": "/local1",
                "upstream": "http://127.0.0.1:8080",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": "*",
            },
            {
                "prefix": "/local2",
                "upstream": "http://localhost:8080",
                "kind": "generic",
                "auth": {"header": "Authorization", "format": "{key}", "key_env": "K"},
                "allow": "*",
            },
        ]
        loaded = self._write_and_load(allowed_loopback)
        self.assertEqual(len(loaded), 2)
