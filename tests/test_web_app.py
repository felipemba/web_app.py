import json
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import web_app


class WeatherWebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            web_app.WeatherRequestHandler,
        )
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join()

    def request(self, path, method="GET", data=None, content_type=None):
        headers = {}
        if data is not None:
            headers["Content-Type"] = content_type or "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as response:
            with response:
                return response.code, response.headers, response.read()

    def test_serves_app_shell_and_manifest(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Global Forecast", body)

        status, headers, body = self.request("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertIn("application/manifest+json", headers["Content-Type"])
        self.assertEqual(json.loads(body)["display"], "standalone")

    @patch("web_app.consultar_previsao")
    def test_returns_existing_forecast_payload(self, consultar):
        previsao = {
            "cidade": "Recife, Brasil",
            "data": "26/09/2026",
            "condicao": "Céu limpo",
            "temperatura_minima": "22 °C",
            "temperatura_maxima": "29 °C",
            "probabilidade_chuva": "10 %",
            "umidade": "70 %",
            "vento": "15 km/h",
        }
        consultar.return_value = previsao
        hoje = date.today()
        body = json.dumps(
            {"cidade": "Recife", "data": hoje.isoformat()}
        ).encode("utf-8")

        status, headers, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response_body), previsao)
        self.assertEqual(headers["Cache-Control"], "no-store")
        consultar.assert_called_once()

    @patch("web_app.consultar_previsao")
    def test_rejects_invalid_date_before_calling_weather_service(self, consultar):
        body = json.dumps(
            {"cidade": "Recife", "data": "not-a-date"}
        ).encode("utf-8")

        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 400)
        self.assertIn("data", json.loads(response_body)["erro"])
        consultar.assert_not_called()

    def test_rejects_non_json_request(self):
        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=b"{}",
            content_type="text/plain",
        )

        self.assertEqual(status, 415)
        self.assertIn("JSON", json.loads(response_body)["erro"])


if __name__ == "__main__":
    unittest.main()
