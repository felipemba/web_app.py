import io
import json
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from email.message import Message
from http.server import ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError, URLError
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
            weather._cache.clear()
            weather._stale_cache.clear()
            weather._in_flight.clear()
        with weather._rate_limit_lock:
            weather._rate_limit.update(
                blocked_until=None,
                manual_retry_required=False,
                manual_retry_after=None,
                unknown_429_count=0,
                transient_blocked_until=None,
                consecutive_failures=0,
                headers={},
            )
        with weather._usage_lock:
            weather._upstream_requests = 0
            weather._metrics.update(
                cache_hits=0,
                cache_misses=0,
                duplicate_requests_blocked=0,
                stale_cache_hits=0,
                upstream_errors={},
                timeouts=0,
                connection_errors=0,
                overload_rejections=0,
                upstream_duration_ms_total=0,
                upstream_duration_ms_max=0,
            )
        weather.limpar_estado_cache_da_requisicao()

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
        for latitude in (91, 10**1000):
            with self.subTest(latitude_digits=len(str(latitude))):
                body = json.dumps({"latitude": latitude, "longitude": 0}).encode()
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
        resposta = io.BytesIO(
            b'{"current":{"temperature_2m":20,"precipitation":0,'
            b'"wind_speed_10m":5,"weather_code":1}}'
        )

        with patch("weather.urlopen", return_value=resposta) as abrir_url:
            primeira = weather.consultar_tempo_atual(-23.55, -46.63)
            segunda = weather.consultar_tempo_atual(-23.55, -46.63)

        self.assertEqual(primeira, segunda)
        abrir_url.assert_called_once()

    def test_deduplicates_simultaneous_identical_requests(self):
        requisicao_iniciada = threading.Event()
        liberar_resposta = threading.Event()
        resposta = {
            "current": {
                "temperature_2m": 20,
                "precipitation": 0,
                "wind_speed_10m": 5,
                "weather_code": 1,
            }
        }

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

    def test_rate_limited_request_uses_retry_after_without_automatic_retry(self):
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
            patch("weather.time.sleep") as aguardar,
            self.assertRaises(weather.WeatherRateLimitError),
        ):
            weather.consultar_tempo_atual(-23.55, -46.63)

        abrir_url.assert_called_once()
        aguardar.assert_not_called()

    def test_stops_after_the_first_http_429_response(self):
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

        abrir_url.assert_called_once()

    def test_rate_limit_pauses_requests_for_other_locations(self):
        headers = Message()
        headers["Retry-After"] = "60"
        rate_limit = HTTPError(
            "https://api.open-meteo.com/v1/forecast",
            429,
            "Too Many Requests",
            headers,
            None,
        )

        with (
            patch("weather.urlopen", side_effect=rate_limit) as abrir_url,
            self.assertRaises(weather.WeatherRateLimitError),
        ):
            weather.consultar_tempo_atual(-23.55, -46.63)

        with self.assertRaises(weather.WeatherRateLimitError):
            weather.consultar_tempo_atual(40.7, -74.0)

        abrir_url.assert_called_once()

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

    def test_reports_quota_without_inventing_remaining_or_reset(self):
        status, _, response_body = self.request("/api/diagnostico")

        self.assertEqual(status, 200)
        diagnosis = json.loads(response_body)
        self.assertEqual(diagnosis["api"], "Open-Meteo")
        self.assertIsNone(diagnosis["consultas_restantes_informadas"])
        self.assertIsNone(diagnosis["horario_de_renovacao"])
        self.assertFalse(diagnosis["contagem_observada_global"])

    def test_rate_limit_response_preserves_status_and_retry_after(self):
        retry_at = weather.datetime.now(weather.timezone.utc) + timedelta(minutes=2)
        error = weather.WeatherRateLimitError(
            "Limite de consultas atingido.",
            retry_at=retry_at,
            headers={"retry-after": "120"},
        )
        body = json.dumps(
            {"cidade": "Recife", "data": date.today().isoformat()}
        ).encode("utf-8")

        with patch("web_app.consultar_previsao", side_effect=error):
            status, headers, response_body = self.request(
                "/api/previsao",
                method="POST",
                data=body,
            )

        self.assertEqual(status, 429)
        self.assertTrue(int(headers["Retry-After"]) > 0)
        response = json.loads(response_body)
        self.assertEqual(response["http_status"], 429)
        self.assertEqual(response["rate_limit"]["headers"]["retry-after"], "120")

    def test_upstream_http_errors_preserve_their_status(self):
        data = json.dumps(
            {"cidade": "Recife", "data": date.today().isoformat()}
        ).encode("utf-8")

        for upstream_status in (400, 401, 403, 404, 408, 500, 502, 503, 504):
            with self.subTest(status=upstream_status):
                error = weather.WeatherUpstreamError(
                    f"HTTP {upstream_status}",
                    status=upstream_status,
                )
                with patch("web_app.consultar_previsao", side_effect=error):
                    status, _, response_body = self.request(
                        "/api/previsao",
                        method="POST",
                        data=data,
                    )
                self.assertEqual(status, upstream_status)
                self.assertEqual(
                    json.loads(response_body)["http_status"],
                    upstream_status,
                )

    @patch("web_app.consultar_previsao")
    def test_unknown_city_returns_not_found(self, consultar):
        consultar.side_effect = weather.CityNotFoundError(
            "nenhuma cidade encontrada."
        )
        body = json.dumps(
            {"cidade": "Cidade inexistente", "data": date.today().isoformat()}
        ).encode("utf-8")

        status, _, response_body = self.request(
            "/api/previsao",
            method="POST",
            data=body,
        )

        self.assertEqual(status, 404)
        self.assertIn("cidade", json.loads(response_body)["erro"])

    def test_weather_429_opens_circuit_and_manual_release_does_not_retry(self):
        with weather._rate_limit_lock:
            previous = dict(weather._rate_limit)
            weather._rate_limit.update(
                blocked_until=None,
                manual_retry_required=False,
                headers={},
            )
        self.addCleanup(
            lambda: weather._rate_limit.update(previous)
        )
        response_headers = Message()
        response_headers["Retry-After"] = "120"
        error = HTTPError(
            weather.FORECAST_URL,
            429,
            "Too Many Requests",
            response_headers,
            BytesIO(b'{"reason":"rate limit"}'),
        )

        with patch("weather.urlopen", side_effect=error) as upstream:
            with self.assertRaises(weather.WeatherRateLimitError) as caught:
                weather._get_json(weather.FORECAST_URL, {})
            self.assertGreater(caught.exception.retry_at, weather.datetime.now(weather.timezone.utc))
            with self.assertRaises(weather.WeatherRateLimitError):
                weather._get_json(weather.FORECAST_URL, {})
            self.assertEqual(upstream.call_count, 1)

    def test_429_without_retry_after_delays_manual_release(self):
        error = HTTPError(
            weather.FORECAST_URL,
            429,
            "Too Many Requests",
            Message(),
            BytesIO(b""),
        )
        with patch("weather.urlopen", side_effect=error) as upstream:
            with self.assertRaises(weather.WeatherRateLimitError) as caught:
                weather._solicitar_json("https://example.invalid/first")
            self.assertGreater(
                caught.exception.retry_at,
                weather.datetime.now(weather.timezone.utc),
            )
            self.assertTrue(caught.exception.manual_retry_required)
            with self.assertRaises(weather.WeatherRateLimitError):
                weather.liberar_tentativa_manual()
            with self.assertRaises(weather.WeatherRateLimitError):
                weather._solicitar_json("https://example.invalid/second")
            upstream.assert_called_once()

        with weather._rate_limit_lock:
            weather._rate_limit["manual_retry_after"] = (
                weather.datetime.now(weather.timezone.utc) - timedelta(seconds=1)
            )
        self.assertTrue(weather.liberar_tentativa_manual())
        self.assertEqual(upstream.call_count, 1)

    def test_weather_timeout_and_connection_failure_have_gateway_statuses(self):
        for failure, expected_status in (
            (TimeoutError("timed out"), 504),
            (URLError(TimeoutError("timed out")), 504),
            (URLError("network unavailable"), 503),
        ):
            with self.subTest(status=expected_status, failure=type(failure).__name__):
                with weather._rate_limit_lock:
                    weather._rate_limit.update(
                        transient_blocked_until=None,
                        consecutive_failures=0,
                    )
                with patch("weather.urlopen", side_effect=failure):
                    with self.assertRaises(weather.WeatherUpstreamError) as caught:
                        weather._get_json(weather.FORECAST_URL, {})
                self.assertEqual(caught.exception.status, expected_status)

    def test_manual_release_only_clears_unknown_retry_state(self):
        with weather._rate_limit_lock:
            previous = dict(weather._rate_limit)
            weather._rate_limit.update(
                blocked_until=None,
                manual_retry_required=True,
                headers={},
            )
        self.addCleanup(
            lambda: weather._rate_limit.update(previous)
        )

        status, _, response_body = self.request(
            "/api/tentar-novamente",
            method="POST",
        )

        self.assertEqual(status, 200)
        self.assertTrue(json.loads(response_body)["liberada"])
        with weather._rate_limit_lock:
            self.assertFalse(weather._rate_limit["manual_retry_required"])

    def test_map_weather_is_served_through_backend(self):
        def current_weather(latitude, longitude):
            return {
                "current": {
                    "temperature_2m": 20,
                    "precipitation": 0,
                    "wind_speed_10m": 5,
                    "weather_code": 1,
                }
            }

        with patch(
            "web_app.consultar_tempo_atual",
            side_effect=current_weather,
        ) as consultar:
            status, _, response_body = self.request("/api/mapa")

        self.assertEqual(status, 200)
        points = json.loads(response_body)
        self.assertEqual(len(points), len(web_app.WEATHER_POINTS))
        self.assertEqual(points[0]["current"]["weather_code"], 1)
        self.assertEqual(consultar.call_count, len(web_app.WEATHER_POINTS))

    def test_simultaneous_map_requests_share_one_upstream_batch(self):
        def current_weather(latitude, longitude):
            time.sleep(0.05)
            return {
                "current": {
                    "temperature_2m": 20,
                    "precipitation": 0,
                    "wind_speed_10m": 5,
                    "weather_code": 1,
                }
            }

        with patch(
            "web_app.consultar_tempo_atual",
            side_effect=current_weather,
        ) as consultar:
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(
                    executor.map(lambda _: self.request("/api/mapa"), range(2))
                )

        self.assertEqual([response[0] for response in responses], [200, 200])
        self.assertEqual(json.loads(responses[0][2]), json.loads(responses[1][2]))
        self.assertEqual(consultar.call_count, len(web_app.WEATHER_POINTS))

    def test_radar_forecast_is_proxied_and_rejects_invalid_coordinates(self):
        forecast = {"hourly": {"time": []}, "daily": {"time": []}}
        with patch(
            "web_app.consultar_previsao_ponto",
            return_value=forecast,
        ) as consultar:
            status, _, response_body = self.request(
                "/api/radar?latitude=-8.05&longitude=-34.9"
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(response_body), forecast)
            consultar.assert_called_once_with(-8.05, -34.9)

            status, _, _ = self.request("/api/radar?latitude=91&longitude=0")
        self.assertEqual(status, 400)
        consultar.assert_called_once()

    def test_cache_coalesces_simultaneous_requests(self):
        calls = []
        key = ("test-single-flight", time.monotonic())

        def load():
            calls.append(True)
            time.sleep(0.03)
            return {"value": [1]}

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _: weather._cache_result(key, 60, load),
                    range(8),
                )
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"value": [1]}] * 8)
        results[0]["value"].append(2)
        self.assertEqual(weather._cache_result(key, 60, load), {"value": [1]})

    def test_expired_cache_falls_back_to_recent_data_on_upstream_failure(self):
        key = ("stale-fallback", time.monotonic())
        weather._cache_result(
            key,
            1,
            lambda: {"value": "recent"},
            stale_ttl_seconds=30,
        )
        with weather._cache_lock:
            weather._cache[key] = (time.monotonic() - 1, {"value": "recent"})
        weather.limpar_estado_cache_da_requisicao()

        result = weather._cache_result(
            key,
            1,
            lambda: (_ for _ in ()).throw(
                weather.WeatherUpstreamError("temporarily unavailable", status=503)
            ),
            stale_ttl_seconds=30,
        )

        self.assertEqual(result, {"value": "recent"})
        self.assertTrue(weather.requisicao_usou_cache_antigo())
        self.assertEqual(weather.obter_diagnostico()["metricas"]["stale_cache_hits"], 1)

    def test_expired_stale_cache_is_not_returned(self):
        key = ("expired-stale-fallback", time.monotonic())
        with weather._cache_lock:
            weather._stale_cache[key] = (
                time.monotonic() - 1,
                {"value": "too old"},
            )

        with self.assertRaises(weather.WeatherUpstreamError):
            weather._cache_result(
                key,
                1,
                lambda: (_ for _ in ()).throw(
                    weather.WeatherUpstreamError("unavailable", status=503)
                ),
                stale_ttl_seconds=30,
            )

        with weather._cache_lock:
            self.assertNotIn(key, weather._stale_cache)

    def test_oversized_cache_values_are_not_retained(self):
        key = ("oversized-cache", time.monotonic())
        value = {"data": "x" * weather.MAX_CACHE_ENTRY_BYTES}

        self.assertEqual(weather._cache_result(key, 60, lambda: value), value)
        with weather._cache_lock:
            self.assertNotIn(key, weather._cache)
            self.assertNotIn(key, weather._stale_cache)

    def test_upstream_requests_are_globally_limited_without_real_network(self):
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        class SlowResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                nonlocal active
                with lock:
                    active -= 1

            def read(self, _size):
                time.sleep(0.04)
                return b'{"ok":true}'

        def fake_urlopen(*_args, **_kwargs):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            return SlowResponse()

        with patch("weather.urlopen", side_effect=fake_urlopen):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda index: weather._solicitar_json(
                            f"https://example.invalid/{index}"
                        ),
                        range(8),
                    )
                )

        self.assertEqual(results, [{"ok": True}] * 8)
        self.assertLessEqual(maximum_active, weather.UPSTREAM_MAX_CONCURRENT)
        self.assertGreater(maximum_active, 1)

    def test_transient_server_error_opens_progressive_backoff_without_retry(self):
        error = HTTPError(
            "https://api.open-meteo.com/v1/forecast",
            503,
            "Service Unavailable",
            Message(),
            BytesIO(b""),
        )
        with patch("weather.urlopen", side_effect=error) as upstream:
            with self.assertRaises(weather.WeatherUpstreamError) as first:
                weather._solicitar_json("https://example.invalid/first")
            with self.assertRaises(weather.WeatherUpstreamError) as second:
                weather._solicitar_json("https://example.invalid/second")

        self.assertEqual(first.exception.status, 503)
        self.assertEqual(second.exception.status, 503)
        self.assertIsNotNone(second.exception.retry_at)
        upstream.assert_called_once()

    def test_upstream_500_502_503_504_open_circuit_without_retry(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                with weather._rate_limit_lock:
                    weather._rate_limit.update(
                        transient_blocked_until=None,
                        consecutive_failures=0,
                    )
                error = HTTPError(
                    "https://api.open-meteo.com/v1/forecast",
                    status,
                    "Upstream failure",
                    Message(),
                    BytesIO(b""),
                )
                with patch("weather.urlopen", side_effect=error) as upstream:
                    with self.assertRaises(weather.WeatherUpstreamError) as caught:
                        weather._solicitar_json(
                            f"https://example.invalid/{status}"
                        )

                self.assertEqual(caught.exception.status, status)
                self.assertIsNotNone(caught.exception.retry_at)
                upstream.assert_called_once()

    def test_repeated_transient_failures_increase_backoff(self):
        errors = [
            HTTPError(
                "https://api.open-meteo.com/v1/forecast",
                503,
                "Service Unavailable",
                Message(),
                BytesIO(b""),
            )
            for _ in range(2)
        ]
        with patch("weather.urlopen", side_effect=errors):
            with self.assertRaises(weather.WeatherUpstreamError) as first:
                weather._solicitar_json("https://example.invalid/first")
            with weather._rate_limit_lock:
                weather._rate_limit["transient_blocked_until"] = (
                    weather.datetime.now(weather.timezone.utc) - timedelta(seconds=1)
                )
            with self.assertRaises(weather.WeatherUpstreamError) as second:
                weather._solicitar_json("https://example.invalid/second")

        first_wait = (first.exception.retry_at - weather.datetime.now(weather.timezone.utc)).total_seconds()
        second_wait = (second.exception.retry_at - weather.datetime.now(weather.timezone.utc)).total_seconds()
        self.assertGreater(first_wait, 1)
        self.assertGreater(second_wait, first_wait)

    def test_provider_low_remaining_header_extends_cache_ttl(self):
        class QuotaResponse(io.BytesIO):
            headers = Message()

        response = QuotaResponse(b'{"current":{"temperature_2m":20}}')
        response.headers["RateLimit-Limit"] = "100"
        response.headers["RateLimit-Remaining"] = "5"
        started = time.monotonic()
        with patch("weather.urlopen", return_value=response):
            weather._get_json(weather.FORECAST_URL, {"latitude": 12})

        key = ("api", f"{weather.FORECAST_URL}?latitude=12")
        with weather._cache_lock:
            remaining_ttl = weather._cache[key][0] - started
        diagnosis = weather.obter_diagnostico()

        self.assertGreaterEqual(remaining_ttl, weather.FORECAST_CACHE_SECONDS * 2.9)
        self.assertTrue(diagnosis["limite_proximo_informado"])
        self.assertEqual(
            diagnosis["ttl_cache_multiplicador"],
            weather.NEAR_LIMIT_CACHE_MULTIPLIER,
        )

    def test_invalid_upstream_json_is_reported_and_tracked(self):
        for response in (b"not-json", b'{"current":NaN}'):
            with self.subTest(response=response):
                with weather._rate_limit_lock:
                    weather._rate_limit.update(
                        transient_blocked_until=None,
                        consecutive_failures=0,
                    )
                with patch("weather.urlopen", return_value=io.BytesIO(response)):
                    with self.assertRaises(weather.WeatherUpstreamError) as caught:
                        weather._solicitar_json(
                            f"https://example.invalid/invalid-{len(response)}"
                        )
                self.assertEqual(caught.exception.status, 502)
        self.assertEqual(
            weather.obter_diagnostico()["metricas"]["upstream_errors"]["502"],
            2,
        )

    def test_stale_radar_response_is_identified_to_the_browser(self):
        key = ("radar", -8.05, -34.9)
        cached = {"daily": {"time": []}, "hourly": {"time": []}}
        with weather._cache_lock:
            weather._stale_cache[key] = (time.monotonic() + 60, cached)

        with patch(
            "weather._get_json",
            side_effect=weather.WeatherRateLimitError("limite atingido"),
        ):
            status, headers, body = self.request(
                "/api/radar?latitude=-8.05&longitude=-34.9"
            )

        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Weather-Cache"], "stale")
        self.assertEqual(json.loads(body), cached)

    def test_malformed_forecast_uses_valid_stale_radar_data(self):
        key = ("radar", -8.05, -34.9)
        cached = {
            "daily": {
                "time": ["2026-09-27"],
                "weather_code": [1],
                "temperature_2m_min": [20],
                "temperature_2m_max": [30],
            },
            "hourly": {
                "time": ["2026-09-27T12:00"],
                "temperature_2m": [25],
                "precipitation": [0],
                "wind_speed_10m": [5],
                "wind_direction_10m": [180],
                "wind_gusts_10m": [8],
                "weather_code": [1],
            },
        }
        with weather._cache_lock:
            weather._stale_cache[key] = (time.monotonic() + 60, cached)

        with patch(
            "weather._solicitar_json",
            return_value={"hourly": {"time": []}, "daily": {"time": []}},
        ):
            result = weather.consultar_previsao_ponto(-8.05, -34.9)

        self.assertEqual(result, cached)
        self.assertTrue(weather.requisicao_usou_cache_antigo())
        with weather._cache_lock:
            self.assertFalse(any(key_part[0] == "api" for key_part in weather._cache))

    def test_map_keeps_successful_points_when_another_location_fails(self):
        def current_weather(latitude, longitude):
            if latitude == web_app.WEATHER_POINTS[0]["lat"]:
                raise weather.WeatherUpstreamError("temporary failure", status=503)
            return {
                "current": {
                    "temperature_2m": 20,
                    "precipitation": 0,
                    "wind_speed_10m": 5,
                    "weather_code": 1,
                }
            }

        with patch("web_app.consultar_tempo_atual", side_effect=current_weather):
            status, _, body = self.request("/api/mapa")

        points = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(points[0].get("erro"))
        self.assertIn("current", points[1])

    def test_http_server_rejects_connections_over_its_limit(self):
        server = web_app.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0),
            web_app.WeatherRequestHandler,
            max_connections=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        slot_held = server._request_slots.acquire(blocking=False)
        try:
            with socket.create_connection(server.server_address, timeout=3) as client:
                client.settimeout(3)
                client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = client.recv(256)
        finally:
            if slot_held:
                server._request_slots.release()
            server.shutdown()
            server.server_close()
            thread.join()

        self.assertIn(b"503 Service Unavailable", response)

    def test_expired_cache_entries_are_removed_during_reads(self):
        expired_key = ("expired-entry", time.monotonic())
        with weather._cache_lock:
            weather._cache[expired_key] = (time.monotonic() - 1, {"old": True})

        self.assertEqual(
            weather._cache_result(("fresh-entry",), 60, lambda: {"new": True}),
            {"new": True},
        )
        with weather._cache_lock:
            self.assertNotIn(expired_key, weather._cache)

    def test_identical_city_and_date_reuses_forecast_cache(self):
        data = date.today()
        with patch(
            "weather._consultar_previsao_sem_cache",
            return_value={"cidade": "Recife"},
        ) as consultar:
            self.assertEqual(
                weather.consultar_previsao("Cache test city", data),
                {"cidade": "Recife"},
            )
            self.assertEqual(
                weather.consultar_previsao("Cache test city", data),
                {"cidade": "Recife"},
            )

        consultar.assert_called_once()

    def test_different_cities_and_dates_use_distinct_forecast_cache_entries(self):
        today = date.today()
        first_date = today + timedelta(days=1)
        second_date = today + timedelta(days=2)
        with patch(
            "weather._consultar_previsao_sem_cache",
            side_effect=lambda city, forecast_date, choice: {
                "cidade": city,
                "data": forecast_date.isoformat(),
            },
        ) as consultar:
            first = weather.consultar_previsao("Cache test city A", first_date)
            second = weather.consultar_previsao("Cache test city A", second_date)
            third = weather.consultar_previsao("Cache test city B", first_date)

        self.assertEqual(first["data"], first_date.isoformat())
        self.assertEqual(second["data"], second_date.isoformat())
        self.assertEqual(third["cidade"], "Cache test city B")
        self.assertEqual(consultar.call_count, 3)


if __name__ == "__main__":
    unittest.main()
