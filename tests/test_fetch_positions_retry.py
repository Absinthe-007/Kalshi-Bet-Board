"""Retry behavior for Kalshi requests. No network, no real API key."""
import email.utils
import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def _load_fetch_positions():
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    os.environ["KALSHI_API_KEY_ID"] = "test-key-id"
    os.environ["KALSHI_PRIVATE_KEY"] = pem.decode()
    path = Path(__file__).resolve().parents[1] / "scripts" / "fetch_positions.py"
    spec = importlib.util.spec_from_file_location("fetch_positions", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_fetch_positions()


def response(status, payload=None, headers=None, reason=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps({} if payload is None else payload).encode()
    resp.headers = requests.structures.CaseInsensitiveDict(headers or {})
    resp.url = "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance"
    resp.reason = reason or ("Too Many Requests" if status == 429 else "Error")
    resp.encoding = "utf-8"
    return resp


class RetryTests(unittest.TestCase):
    def test_429_then_success_uses_backoff(self):
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] < 3:
                return response(429)
            return response(200, {"balance": 10})

        with patch.object(MOD.time, "sleep") as sleep:
            data = MOD._get_json(send)
        self.assertEqual(data, {"balance": 10})
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_429_after_all_attempts_still_raises(self):
        def send():
            return response(429, reason="Too Many Requests")

        with patch.object(MOD.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError) as caught:
                MOD._get_json(send)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0, 4.0])
        self.assertIn("429", str(caught.exception))
        self.assertIn("portfolio/balance", str(caught.exception))

    def test_client_errors_are_not_retried(self):
        for status in (401, 403, 404):
            calls = {"n": 0}

            def send(status=status):
                calls["n"] += 1
                return response(status)

            with patch.object(MOD.time, "sleep") as sleep:
                with self.assertRaises(requests.HTTPError):
                    MOD._get_json(send)
            self.assertEqual(calls["n"], 1, status)
            sleep.assert_not_called()

    def test_retry_after_beats_backoff_and_rate_limit_reset(self):
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] == 1:
                return response(429, headers={"Retry-After": "2.5", "X-RateLimit-Reset": "0.2"})
            return response(200, {"ok": 1})

        with patch.object(MOD.time, "sleep") as sleep:
            self.assertEqual(MOD._get_json(send), {"ok": 1})
        self.assertEqual(sleep.call_args_list[0].args[0], 2.5)

    def test_kalshi_rate_limit_reset_when_retry_after_is_absent(self):
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] == 1:
                return response(503, headers={"X-RateLimit-Reset": "0.75"})
            return response(200, {"markets": []})

        with patch.object(MOD.time, "sleep") as sleep:
            self.assertEqual(MOD._get_json(send), {"markets": []})
        self.assertEqual(sleep.call_args_list[0].args[0], 0.75)

    def test_retry_after_is_capped(self):
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] == 1:
                return response(429, headers={"Retry-After": "120"})
            return response(200, {"ok": True})

        with patch.object(MOD.time, "sleep") as sleep:
            MOD._get_json(send)
        self.assertEqual(sleep.call_args_list[0].args[0], MOD.MAX_RETRY_WAIT)

    def test_retry_after_http_date(self):
        when = email.utils.formatdate(time.time() + 5, usegmt=True)
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] == 1:
                return response(429, headers={"Retry-After": when})
            return response(200, {"ok": True})

        with patch.object(MOD.time, "sleep") as sleep:
            MOD._get_json(send)
        self.assertAlmostEqual(sleep.call_args_list[0].args[0], 5.0, delta=1.5)

    def test_unusable_retry_after_falls_back_to_backoff(self):
        def send():
            return response(429, headers={"Retry-After": "soon", "X-RateLimit-Reset": "nope"})

        with patch.object(MOD.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                MOD._get_json(send)
        self.assertEqual(sleep.call_args_list[0].args[0], 1.0)

    def test_connection_error_then_success(self):
        calls = {"n": 0}

        def send():
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("connection reset")
            return response(200, {"ok": True})

        with patch.object(MOD.time, "sleep") as sleep:
            self.assertEqual(MOD._get_json(send), {"ok": True})
        self.assertEqual(sleep.call_args_list[0].args[0], 1.0)

    def test_timeout_still_raises_after_retries(self):
        def send():
            raise requests.Timeout("timed out")

        with patch.object(MOD.time, "sleep") as sleep:
            with self.assertRaises(requests.Timeout):
                MOD._get_json(send)
        self.assertEqual(sleep.call_count, MOD.MAX_ATTEMPTS - 1)

    def test_signed_get_resigns_each_attempt(self):
        seen = []

        def fake_get(url, params=None, headers=None, timeout=None):
            seen.append(dict(headers))
            if len(seen) == 1:
                return response(429, headers={"Retry-After": "1"})
            return response(200, {"balance": 1})

        with patch.object(MOD.time, "time", side_effect=[1_700_000_000.0, 1_700_000_002.0]):
            with patch.object(MOD.time, "sleep"):
                with patch.object(MOD.S, "get", side_effect=fake_get):
                    data = MOD.signed_get("/portfolio/balance")
        self.assertEqual(data, {"balance": 1})
        self.assertEqual(seen[0]["KALSHI-ACCESS-TIMESTAMP"], "1700000000000")
        self.assertEqual(seen[1]["KALSHI-ACCESS-TIMESTAMP"], "1700000002000")
        self.assertNotEqual(seen[0]["KALSHI-ACCESS-SIGNATURE"], seen[1]["KALSHI-ACCESS-SIGNATURE"])
        self.assertEqual(seen[0]["KALSHI-ACCESS-KEY"], "test-key-id")

    def test_public_get_retries_server_error(self):
        answers = [response(502), response(200, {"markets": [{"ticker": "X"}]})]
        with patch.object(MOD.time, "sleep") as sleep:
            with patch.object(MOD.S, "get", side_effect=answers):
                data = MOD.public_get("/markets", {"limit": 1})
        self.assertEqual(data["markets"][0]["ticker"], "X")
        self.assertEqual(sleep.call_args_list[0].args[0], 1.0)


if __name__ == "__main__":
    unittest.main()
