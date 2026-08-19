import base64
import socket
import unittest
from unittest.mock import patch

import app


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        return self.content


class PortalTests(unittest.TestCase):
    def test_replace_exact_ip_in_url(self):
        source = "https://198.51.100.12:2096/sub/token?host=198.51.100.12"
        self.assertEqual(
            app.replace_host_in_url(source, "198.51.100.12", "10.20.30.40"),
            "https://10.20.30.40:2096/sub/token?host=10.20.30.40",
        )

    def test_replace_does_not_touch_other_ip(self):
        source = "https://203.0.113.10:2096/sub/198.51.100.120"
        self.assertEqual(app.replace_host_in_url(source, "198.51.100.12", "10.20.30.40"), source)

    @patch("app.urllib.request.urlopen")
    def test_fetches_base64_3xui_subscription(self, urlopen):
        links = "vless://first\nvless://second\n"
        urlopen.return_value = FakeResponse(base64.b64encode(links.encode("utf-8")))
        self.assertEqual(app.fetch_vless("https://example.test/sub/id"), ["vless://first", "vless://second"])

    @patch("app.urllib.request.urlopen")
    def test_fetches_plain_subscription(self, urlopen):
        urlopen.return_value = FakeResponse(b"vless://first\nignored://entry\n")
        self.assertEqual(app.fetch_vless("https://example.test/sub/id"), ["vless://first"])

    def test_client_payload_is_base64_lines(self):
        payload = "vless://first\nvless://second\n"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), payload)

    @patch("app.fetch_vless", side_effect=TimeoutError("source timeout"))
    @patch("app.subscription_rows")
    def test_unavailable_source_is_omitted_without_cache(self, subscription_rows, fetch_vless):
        subscription_rows.return_value = [{
            "id": 1,
            "label": "Недоступный сервер",
            "source_url": "https://198.51.100.10/sub/token",
            "cached_links": '["vless://stale"]',
        }]
        groups, errors = app.collect_links(1, use_cache=False)
        self.assertEqual(groups[0]["links"], [])
        self.assertEqual(errors[0]["message"], "Источник временно недоступен")

    @patch("app.socket.create_connection")
    def test_vless_endpoint_availability_uses_uri_host_and_port(self, create_connection):
        create_connection.return_value.__enter__.return_value = object()
        link = "vless://uuid@198.51.100.11:443?security=reality"
        self.assertTrue(app.vless_endpoint_available(link))
        create_connection.assert_called_once_with(("198.51.100.11", 443), timeout=2)

    @patch("app.socket.create_connection", side_effect=socket.timeout)
    def test_unavailable_vless_endpoint_is_rejected(self, create_connection):
        self.assertFalse(app.vless_endpoint_available("vless://uuid@198.51.100.11:443"))

    def test_subscription_url_is_built_from_server_and_user_id(self):
        self.assertEqual(
            app.subscription_url("198.51.100.11:2096", "abcdef1234567890"),
            "https://198.51.100.11:2096/sub/abcdef1234567890",
        )

    def test_subscription_id_is_extracted_from_existing_url(self):
        self.assertEqual(
            app.subscription_id_from_url("https://198.51.100.11:2096/sub/abcdef1234567890"),
            "abcdef1234567890",
        )


if __name__ == "__main__":
    unittest.main()
