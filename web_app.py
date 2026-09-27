import argparse
import json
import math
import mimetypes
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import ceil
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from weather import (
    CityNotFoundError,
    ForecastUnavailableError,
    WeatherRateLimitError,
    WeatherError,
    WeatherUpstreamError,
    consultar_previsao,
    consultar_previsao_ponto,
    consultar_tempo_atual,
    liberar_tentativa_manual,
    obter_diagnostico,
    validar_data_previsao,
)


WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_REQUEST_BYTES = 8192
MAP_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="weather-map",
)
_map_lock = threading.Lock()
_map_in_flight = None
WEATHER_POINTS = (
    {"name": "Nova York", "lat": 40.7128, "lon": -74.0060, "country": "EUA"},
    {"name": "São Paulo", "lat": -23.5505, "lon": -46.6333, "country": "Brasil"},
    {"name": "Londres", "lat": 51.5074, "lon": -0.1278, "country": "Reino Unido"},
    {"name": "Dubai", "lat": 25.2048, "lon": 55.2708, "country": "Emirados"},
    {"name": "Tóquio", "lat": 35.6762, "lon": 139.6503, "country": "Japão"},
    {"name": "Sydney", "lat": -33.8688, "lon": 151.2093, "country": "Austrália"},
    {"name": "Cidade do Cabo", "lat": -33.9249, "lon": 18.4241, "country": "África do Sul"},
    {"name": "Buenos Aires", "lat": -34.6037, "lon": -58.3816, "country": "Argentina"},
)


def _previsao_mapa(ponto):
    data = consultar_tempo_atual(ponto["lat"], ponto["lon"])
    current = data.get("current")
    if not isinstance(current, dict) or not all(
        isinstance(current.get(key), (int, float))
        for key in (
            "temperature_2m",
            "precipitation",
            "wind_speed_10m",
            "weather_code",
        )
    ):
        raise WeatherError("o serviço não retornou condições atuais válidas.")
    return {
        **ponto,
        "current": {
            "temperature_2m": current["temperature_2m"],
            "precipitation": current["precipitation"],
            "wind_speed_10m": current["wind_speed_10m"],
            "weather_code": current["weather_code"],
        },
    }


def _obter_previsoes_mapa():
    global _map_in_flight
    with _map_lock:
        future = _map_in_flight
        is_owner = future is None
        if is_owner:
            future = Future()
            _map_in_flight = future

    if not is_owner:
        return deepcopy(future.result())

    try:
        requests = [
            MAP_EXECUTOR.submit(_previsao_mapa, ponto)
            for ponto in WEATHER_POINTS
        ]
        resultados = []
        primeiro_erro = None
        for ponto, request in zip(WEATHER_POINTS, requests):
            try:
                resultados.append(request.result())
            except WeatherUpstreamError as erro:
                if primeiro_erro is None or erro.status == 429:
                    primeiro_erro = erro
                resultados.append({**ponto, "erro": str(erro)})
            except (WeatherError, ValueError) as erro:
                resultados.append({**ponto, "erro": str(erro)})
        resultado = (resultados, primeiro_erro)
        future.set_result(resultado)
        return deepcopy(resultado)
    except BaseException as erro:
        future.set_exception(erro)
        raise
    finally:
        with _map_lock:
            if _map_in_flight is future:
                _map_in_flight = None


class WeatherRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        requisicao = urlsplit(self.path)
        caminho = requisicao.path
        if caminho == "/api/diagnostico":
            self._responder_json(200, obter_diagnostico())
            return

        if caminho == "/api/radar":
            parametros = parse_qs(requisicao.query)
            try:
                latitude = float(parametros["latitude"][0])
                longitude = float(parametros["longitude"][0])
                if (
                    not math.isfinite(latitude)
                    or not math.isfinite(longitude)
                    or not -90 <= latitude <= 90
                    or not -180 <= longitude <= 180
                ):
                    raise ValueError("coordenadas fora dos limites geográficos.")
                previsao = consultar_previsao_ponto(latitude, longitude)
            except (KeyError, ValueError, IndexError) as erro:
                self._responder_json(400, {"erro": "Informe coordenadas válidas."})
                return
            except WeatherUpstreamError as erro:
                self._responder_erro_meteorologico(erro)
                return
            except WeatherError as erro:
                self._responder_json(502, {"erro": str(erro)})
                return
            self._responder_json(200, previsao)
            return

        if caminho == "/api/mapa":
            resultados, primeiro_erro = _obter_previsoes_mapa()
            if primeiro_erro is not None:
                self._responder_erro_meteorologico(primeiro_erro)
                return
            if all("erro" in resultado for resultado in resultados):
                if primeiro_erro is not None:
                    self._responder_erro_meteorologico(primeiro_erro)
                    return
                self._responder_json(
                    502,
                    {
                        "erro": "Não foi possível carregar nenhuma condição do mapa.",
                        "pontos": resultados,
                    },
                )
                return
            self._responder_json(200, resultados)
            return

        if caminho.startswith("/api/"):
            self._responder_json(404, {"erro": "Rota não encontrada."})
            return

        if caminho == "/":
            caminho = "/index.html"

        arquivo = (WEB_DIR / unquote(caminho.lstrip("/"))).resolve()
        if WEB_DIR not in arquivo.parents or not arquivo.is_file():
            self.send_error(404, "Arquivo não encontrado.")
            return

        conteudo = arquivo.read_bytes()
        tipo = mimetypes.guess_type(arquivo.name)[0] or "application/octet-stream"
        if tipo.startswith("text/") or tipo in (
            "application/javascript",
            "application/manifest+json",
        ):
            tipo = f"{tipo}; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(conteudo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(conteudo)

    def do_POST(self):
        caminho = urlsplit(self.path).path
        if caminho == "/api/tentar-novamente":
            liberada = liberar_tentativa_manual()
            self._responder_json(
                200,
                {
                    "liberada": liberada,
                    "mensagem": (
                        "Uma nova consulta manual foi liberada."
                        if liberada
                        else "Não há bloqueio manual ativo."
                    ),
                },
            )
            return

        rotas_meteorologicas = {
            "/api/previsao",
            "/api/tempo/atual",
            "/api/tempo/radar",
        }
        if caminho not in rotas_meteorologicas:
            self._responder_json(404, {"erro": "Rota não encontrada."})
            return

        if self.headers.get_content_type() != "application/json":
            self._responder_json(415, {"erro": "Envie os dados no formato JSON."})
            return

        try:
            tamanho = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._responder_json(400, {"erro": "Tamanho da solicitação inválido."})
            return

        if tamanho < 0 or tamanho > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._responder_json(413, {"erro": "A solicitação excede o tamanho permitido."})
            return

        try:
            dados = json.loads(self.rfile.read(tamanho))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._responder_json(400, {"erro": "Envie um JSON válido."})
            return

        if not isinstance(dados, dict):
            self._responder_json(400, {"erro": "Informe uma cidade e uma data."})
            return

        if caminho in ("/api/tempo/atual", "/api/tempo/radar"):
            coordenadas = self._validar_coordenadas(dados)
            if coordenadas is None:
                self._responder_json(
                    400,
                    {"erro": "Informe latitude e longitude válidas."},
                )
                return
            try:
                if caminho == "/api/tempo/atual":
                    resposta = consultar_tempo_atual(*coordenadas)
                else:
                    resposta = consultar_previsao_ponto(*coordenadas)
            except WeatherUpstreamError as erro:
                self._responder_erro_meteorologico(erro)
                return
            except WeatherError as erro:
                self._responder_json(502, {"erro": str(erro)})
                return

            self._responder_json(200, resposta)
            return

        cidade = dados.get("cidade")
        data_texto = dados.get("data")
        if not isinstance(cidade, str) or not cidade.strip():
            self._responder_json(400, {"erro": "Informe o nome de uma cidade."})
            return
        if len(cidade.strip()) > 120:
            self._responder_json(400, {"erro": "O nome da cidade deve ter até 120 caracteres."})
            return
        if not isinstance(data_texto, str):
            self._responder_json(400, {"erro": "Informe uma data válida."})
            return

        try:
            data = validar_data_previsao(data_texto)
        except ValueError as erro:
            self._responder_json(400, {"erro": str(erro)})
            return

        try:
            previsao = consultar_previsao(cidade.strip(), data, escolha_cidade=1)
        except ForecastUnavailableError as erro:
            self._responder_json(422, {"erro": str(erro)})
            return
        except CityNotFoundError as erro:
            self._responder_json(404, {"erro": str(erro)})
            return
        except WeatherUpstreamError as erro:
            self._responder_erro_meteorologico(erro)
            return
        except WeatherError as erro:
            self._responder_json(502, {"erro": str(erro)})
            return

        self._responder_json(200, previsao)

    def _responder_erro_meteorologico(self, erro):
        dados = {"erro": str(erro), "http_status": erro.status}
        if erro.status == 429:
            retry_at = (
                erro.retry_at
                if isinstance(erro, WeatherRateLimitError)
                else None
            )
            dados["rate_limit"] = {
                "proxima_consulta_disponivel": (
                    retry_at.isoformat() if retry_at else None
                ),
                "tentativa_manual_necessaria": retry_at is None,
                "headers": erro.headers,
            }
        headers = {}
        if (
            erro.status == 429
            and isinstance(erro, WeatherRateLimitError)
            and erro.retry_at is not None
        ):
            segundos = max(
                0,
                ceil((erro.retry_at - datetime.now(timezone.utc)).total_seconds()),
            )
            headers["Retry-After"] = str(segundos)
        self._responder_json(erro.status, dados, headers=headers)

    @staticmethod
    def _validar_coordenadas(dados):
        latitude = dados.get("latitude")
        longitude = dados.get("longitude")
        if (
            isinstance(latitude, bool)
            or isinstance(longitude, bool)
            or not isinstance(latitude, (int, float))
            or not isinstance(longitude, (int, float))
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            return None
        return latitude, longitude

    def _responder_json(self, status, dados, headers=None):
        conteudo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(conteudo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for nome, valor in (headers or {}).items():
            self.send_header(nome, valor)
        self.end_headers()
        self.wfile.write(conteudo)


def main():
    parser = argparse.ArgumentParser(description="Aplicativo web de previsão do tempo.")
    porta_padrao = os.environ.get("PORT", "8000")
    host_padrao = os.environ.get("HOST")
    if host_padrao is None:
        host_padrao = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"

    parser.add_argument(
        "--host",
        default=host_padrao,
        help="Endereço de rede (em hospedagem, use 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=porta_padrao,
        help="Porta HTTP (usa PORT ou 8000 por padrão).",
    )
    argumentos = parser.parse_args()

    servidor = ThreadingHTTPServer(
        (argumentos.host, argumentos.port),
        WeatherRequestHandler,
    )
    print(f"Aplicativo disponível em http://{argumentos.host}:{argumentos.port}")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()
