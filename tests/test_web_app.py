import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import web_app
import weather


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

    def setUp(self):
        with weather._cache_lock:
            weather._response_cache.clear()

    def test_serves_app_shell_and_manifest(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Global Forecast", body)
        self.assertIn(b'id="open-radar"', body)
        self.assertIn(b'id="radar-dialog"', body)
        self.assertIn(b"id=\"radar-alert-list\"", body)
        self.assertIn(b'id="radar-live-play"', body)
        self.assertIn(b"previs\xc3\xa3o hor\xc3\xa1ria", body)

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

    @patch("web_app.consultar_tempo_atual")
    def test_current_weather_endpoint_proxies_valid_coordinates(self, consultar):
        consulta = {"current": {"temperature_2m": 20}}
        consultar.return_value = consulta
        body = json.dumps({"latitude": -23.55, "longitude": -46.63}).encode()

        status, _, response_body = self.request(
            "/api/tempo/atual",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response_body), consulta)
        consultar.assert_called_once_with(-23.55, -46.63)

    @patch("web_app.consultar_previsao_ponto")
    def test_radar_endpoint_returns_friendly_rate_limit_error(self, consultar):
        consultar.side_effect = weather.WeatherRateLimitError(
            "o serviço meteorológico está temporariamente sobrecarregado."
        )
        body = json.dumps({"latitude": 40.7, "longitude": -74.0}).encode()

        status, _, response_body = self.request(
            "/api/tempo/radar",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 429)
        self.assertIn("temporariamente sobrecarregado", json.loads(response_body)["erro"])
        consultar.assert_called_once_with(40.7, -74.0)

    @patch("web_app.consultar_previsao")
    def test_city_forecast_returns_rate_limit_status(self, consultar):
        consultar.side_effect = weather.WeatherRateLimitError(
            "o serviço meteorológico está temporariamente sobrecarregado."
        )
        data = date.today().isoformat()
        body = json.dumps({"cidade": "Recife", "data": data}).encode()

        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 429)
        self.assertIn("temporariamente sobrecarregado", json.loads(response_body)["erro"])
        consultar.assert_called_once()

    @patch("web_app.consultar_tempo_atual")
    def test_weather_endpoint_rejects_invalid_coordinates(self, consultar):
        body = json.dumps({"latitude": 91, "longitude": 0}).encode()

        status, _, response_body = self.request(
            "/api/tempo/atual",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 400)
        self.assertIn("latitude e longitude", json.loads(response_body)["erro"])
        consultar.assert_not_called()

    @patch("web_app.consultar_previsao")
    def test_accepts_a_future_date_within_forecast_range(self, consultar):
        consultar.return_value = {"cidade": "Recife, Brasil"}
        data_futura = (date.today() + timedelta(days=10)).isoformat()
        body = json.dumps(
            {"cidade": "Recife", "data": data_futura}
        ).encode("utf-8")

        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response_body), consultar.return_value)
        consultar.assert_called_once()

    @patch("web_app.consultar_previsao")
    def test_explains_when_future_date_exceeds_forecast_range(self, consultar):
        consultar.side_effect = weather.ForecastUnavailableError(
            "previsões reais estão disponíveis somente para os próximos 16 dias."
        )
        data_futura = (date.today() + timedelta(days=16)).isoformat()
        body = json.dumps(
            {"cidade": "Recife", "data": data_futura}
        ).encode("utf-8")

        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 422)
        self.assertIn("próximos 16 dias", json.loads(response_body)["erro"])
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

    @patch("web_app.consultar_previsao")
    def test_rejects_past_date(self, consultar):
        data_passada = (date.today() - timedelta(days=1)).isoformat()
        body = json.dumps(
            {"cidade": "Recife", "data": data_passada}
        ).encode("utf-8")

        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 400)
        self.assertIn("data deve ser hoje", json.loads(response_body)["erro"])
        consultar.assert_not_called()

    def test_forecast_range_error_is_clear_and_avoids_external_lookup(self):
        data_futura = date.today() + timedelta(days=16)

        with patch("weather._buscar_cidade") as buscar_cidade:
            with self.assertRaises(weather.ForecastUnavailableError) as erro:
                weather.consultar_previsao("Recife", data_futura)

        self.assertIn("próximos 16 dias", str(erro.exception))
        buscar_cidade.assert_not_called()

    def test_requests_the_exact_forecast_date_from_weather_api(self):
        data_futura = date.today() + timedelta(days=3)
        local = {
            "name": "Recife",
            "country": "Brasil",
            "latitude": -8.05,
            "longitude": -34.9,
        }
        resposta_diaria = {
            "daily": {
                "time": [data_futura.isoformat()],
                "weather_code": [1],
                "temperature_2m_min": [23],
                "temperature_2m_max": [30],
                "precipitation_probability_max": [10],
                "relative_humidity_2m_mean": [70],
                "wind_speed_10m_max": [15],
            }
        }

        with (
            patch("weather._buscar_cidade", return_value=local),
            patch("weather._get_json", return_value=resposta_diaria) as get_json,
        ):
            previsao = weather.consultar_previsao("Recife", data_futura)

        self.assertEqual(previsao["data"], data_futura.strftime("%d/%m/%Y"))
        parametros = get_json.call_args.args[1]
        self.assertEqual(parametros["start_date"], data_futura.isoformat())
        self.assertEqual(parametros["end_date"], data_futura.isoformat())
        self.assertNotIn("forecast_days", parametros)

    def test_caches_weather_api_responses(self):
        resposta = io.BytesIO(b'{"current":{"temperature_2m":20}}')

        with patch("weather.urlopen", return_value=resposta) as abrir_url:
            primeira = weather.consultar_tempo_atual(-23.55, -46.63)
            segunda = weather.consultar_tempo_atual(-23.55, -46.63)

        self.assertEqual(primeira, segunda)
        abrir_url.assert_called_once()

    def test_deduplicates_simultaneous_identical_requests(self):
        requisicao_iniciada = threading.Event()
        liberar_resposta = threading.Event()
        resposta = {"current": {"temperature_2m": 20}}

        def buscar_resposta(_):
            requisicao_iniciada.set()
            self.assertTrue(liberar_resposta.wait(timeout=3))
            return resposta

        with patch("weather._solicitar_json", side_effect=buscar_resposta) as buscar:
            with ThreadPoolExecutor(max_workers=2) as executor:
                primeira = executor.submit(
                    weather.consultar_tempo_atual,
                    -23.55,
                    -46.63,
                )
                self.assertTrue(requisicao_iniciada.wait(timeout=3))
                segunda = executor.submit(
                    weather.consultar_tempo_atual,
                    -23.55,
                    -46.63,
                )
                liberar_resposta.set()
                self.assertEqual(primeira.result(timeout=3), resposta)
                self.assertEqual(segunda.result(timeout=3), resposta)

        buscar.assert_called_once()

    def test_retries_rate_limited_request_once_with_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "0"
        rate_limit = HTTPError(
            "https://api.open-meteo.com/v1/forecast",
            429,
            "Too Many Requests",
            headers,
            None,
        )
        response = io.BytesIO(b'{"current":{"temperature_2m":20}}')

        with (
            patch("weather.urlopen", side_effect=[rate_limit, response]) as abrir_url,
            patch("weather.time.sleep") as aguardar,
        ):
            resultado = weather.consultar_tempo_atual(-23.55, -46.63)

        self.assertEqual(resultado["current"]["temperature_2m"], 20)
        self.assertEqual(abrir_url.call_count, 2)
        self.assertTrue(
            any(chamada.args == (0.0,) for chamada in aguardar.call_args_list)
        )

    def test_stops_after_bounded_rate_limit_retries(self):
        headers = Message()
        headers["Retry-After"] = "0"
        rate_limit = HTTPError(
            "https://api.open-meteo.com/v1/forecast",
            429,
            "Too Many Requests",
            headers,
            None,
        )

        with (
            patch("weather.urlopen", side_effect=rate_limit) as abrir_url,
            patch("weather.time.sleep"),
            self.assertRaises(weather.WeatherRateLimitError),
        ):
            weather.consultar_tempo_atual(-23.55, -46.63)

        self.assertEqual(abrir_url.call_count, weather.MAX_RATE_LIMIT_RETRIES + 1)

    def test_accepts_dates_up_to_two_years_ahead(self):
        hoje = date(2026, 2, 28)

        self.assertEqual(
            weather.validar_data_previsao("2028-02-28", hoje=hoje),
            date(2028, 2, 28),
        )

    def test_two_year_limit_handles_leap_day(self):
        hoje = date(2024, 2, 29)

        self.assertEqual(
            weather.validar_data_previsao("2026-02-28", hoje=hoje),
            date(2026, 2, 28),
        )
        with self.assertRaisesRegex(ValueError, "até dois anos"):
            weather.validar_data_previsao("2026-03-01", hoje=hoje)

    def test_rejects_dates_more_than_two_years_ahead(self):
        hoje = date(2026, 9, 26)

        with self.assertRaisesRegex(ValueError, "até dois anos"):
            weather.validar_data_previsao("2028-09-27", hoje=hoje)

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
