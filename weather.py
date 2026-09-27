import json
import threading
import time
from concurrent.futures import Future
from calendar import monthrange
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_FORECAST_DAYS = 16
MAX_FUTURE_YEARS = 2
TIMEOUT_SECONDS = 10
FORECAST_CACHE_SECONDS = 600
GEOCODING_CACHE_SECONDS = 86400
RATE_LIMIT_CACHE_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 2
MAX_RETRY_DELAY_SECONDS = 5
MIN_REQUEST_INTERVAL_SECONDS = 0.2
MAX_CACHE_ENTRIES = 512

_cache_lock = threading.Lock()
_response_cache = {}
_in_flight_requests = {}
_provider_request_lock = threading.Lock()
_last_provider_request_at = 0

WEATHER_CODES = {
    0: "Céu limpo",
    1: "Predominantemente limpo",
    2: "Parcialmente nublado",
    3: "Nublado",
    45: "Nevoeiro",
    48: "Nevoeiro com geada",
    51: "Garoa fraca",
    53: "Garoa moderada",
    55: "Garoa intensa",
    56: "Garoa congelante fraca",
    57: "Garoa congelante intensa",
    61: "Chuva fraca",
    63: "Chuva moderada",
    65: "Chuva intensa",
    66: "Chuva congelante fraca",
    67: "Chuva congelante intensa",
    71: "Neve fraca",
    73: "Neve moderada",
    75: "Neve intensa",
    77: "Grãos de neve",
    80: "Pancadas de chuva fracas",
    81: "Pancadas de chuva moderadas",
    82: "Pancadas de chuva violentas",
    85: "Pancadas de neve fracas",
    86: "Pancadas de neve intensas",
    95: "Trovoada",
    96: "Trovoada com granizo fraco",
    99: "Trovoada com granizo intenso",
}


class WeatherError(Exception):
    """Erro esperado ao consultar ou interpretar dados meteorológicos."""


class ForecastUnavailableError(WeatherError):
    """Erro quando a data está fora do horizonte de previsão disponível."""


class WeatherRateLimitError(WeatherError):
    """Erro quando a API meteorológica limita temporariamente as consultas."""


def validar_data_previsao(texto, hoje=None):
    try:
        data = datetime.strptime(texto, "%Y-%m-%d").date()
    except ValueError as erro:
        raise ValueError("data inválida; use o formato AAAA-MM-DD.") from erro

    hoje = hoje or date.today()
    if data < hoje:
        raise ValueError(
            f"a data deve ser hoje ou uma data futura "
            f"({hoje.isoformat()} ou posterior)."
        )
    ano_limite = hoje.year + MAX_FUTURE_YEARS
    dia_limite = min(hoje.day, monthrange(ano_limite, hoje.month)[1])
    ultima_data = hoje.replace(year=ano_limite, day=dia_limite)
    if data > ultima_data:
        raise ValueError(
            f"a data deve ser até dois anos à frente "
            f"({ultima_data.isoformat()} ou anterior)."
        )
    return data


def _retry_delay(erro, tentativa):
    retry_after = erro.headers.get("Retry-After") if erro.headers else None
    if retry_after:
        try:
            return max(0, float(retry_after))
        except ValueError:
            try:
                instante = parsedate_to_datetime(retry_after)
                return max(0, (instante - datetime.now(instante.tzinfo)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2**tentativa


def _abrir_url_com_intervalo(requisicao):
    global _last_provider_request_at
    with _provider_request_lock:
        agora = time.monotonic()
        espera = MIN_REQUEST_INTERVAL_SECONDS - (
            agora - _last_provider_request_at
        )
        if espera > 0:
            time.sleep(espera)
        _last_provider_request_at = time.monotonic()
        return urlopen(requisicao, timeout=TIMEOUT_SECONDS)


def _solicitar_json(url_completo):
    requisicao = Request(
        url_completo,
        headers={"User-Agent": "CalculadoraPrevisaoTempo/1.0"},
    )
    try:
        for tentativa in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                with _abrir_url_com_intervalo(requisicao) as resposta:
                    dados = json.loads(resposta.read().decode("utf-8"))
                break
            except HTTPError as erro:
                if erro.code != 429:
                    raise WeatherError(
                        f"serviço meteorológico indisponível (HTTP {erro.code})."
                    ) from erro

                espera = _retry_delay(erro, tentativa)
                erro.close()
                if (
                    tentativa >= MAX_RATE_LIMIT_RETRIES
                    or espera > MAX_RETRY_DELAY_SECONDS
                ):
                    raise WeatherRateLimitError(
                        "o serviço meteorológico está temporariamente sobrecarregado "
                        "(limite de consultas atingido). Aguarde alguns minutos e tente novamente."
                    ) from erro
                time.sleep(espera)
    except (URLError, TimeoutError, OSError) as erro:
        raise WeatherError(
            "não foi possível conectar ao serviço meteorológico; "
            "verifique sua conexão com a internet."
        ) from erro
    except (UnicodeDecodeError, json.JSONDecodeError) as erro:
        raise WeatherError("o serviço retornou uma resposta inválida.") from erro

    if not isinstance(dados, dict):
        raise WeatherError("o serviço retornou uma resposta inválida.")
    if dados.get("error"):
        detalhe = dados.get("reason", "erro não especificado")
        raise WeatherError(f"erro retornado pela API: {detalhe}")
    return dados


def _limpar_cache_expirado(agora):
    expiradas = [
        chave
        for chave, (expiracao, _, _) in _response_cache.items()
        if expiracao <= agora
    ]
    for chave in expiradas:
        del _response_cache[chave]


def _get_json(url, parametros):
    url_completo = f"{url}?{urlencode(parametros)}"
    chave = url_completo
    tempo_cache = (
        GEOCODING_CACHE_SECONDS
        if url == GEOCODING_URL
        else FORECAST_CACHE_SECONDS
    )

    with _cache_lock:
        agora = time.monotonic()
        _limpar_cache_expirado(agora)
        cacheado = _response_cache.get(chave)
        if cacheado:
            _, dados, erro = cacheado
            if erro:
                raise erro
            return dados

        requisicao_em_andamento = _in_flight_requests.get(chave)
        if requisicao_em_andamento is None:
            requisicao_em_andamento = Future()
            _in_flight_requests[chave] = requisicao_em_andamento
            lider = True
        else:
            lider = False

    if not lider:
        return requisicao_em_andamento.result()

    try:
        dados = _solicitar_json(url_completo)
    except Exception as erro:
        if isinstance(erro, WeatherRateLimitError):
            with _cache_lock:
                _response_cache[chave] = (
                    time.monotonic() + RATE_LIMIT_CACHE_SECONDS,
                    None,
                    erro,
                )
        requisicao_em_andamento.set_exception(erro)
        raise
    else:
        with _cache_lock:
            _response_cache[chave] = (
                time.monotonic() + tempo_cache,
                dados,
                None,
            )
            if len(_response_cache) > MAX_CACHE_ENTRIES:
                _limpar_cache_expirado(time.monotonic())
                while len(_response_cache) > MAX_CACHE_ENTRIES:
                    del _response_cache[next(iter(_response_cache))]
        requisicao_em_andamento.set_result(dados)
        return dados
    finally:
        with _cache_lock:
            _in_flight_requests.pop(chave, None)


def _buscar_cidade(nome, escolha=None):
    dados = _get_json(
        GEOCODING_URL,
        {
            "name": nome,
            "count": 10,
            "language": "pt",
            "format": "json",
        },
    )
    resultados = dados.get("results")
    if not resultados:
        raise WeatherError(f"nenhuma cidade encontrada para “{nome}”.")

    if len(resultados) == 1:
        return resultados[0]

    if escolha is None:
        print("\nForam encontradas várias cidades:")
        for indice, local in enumerate(resultados, start=1):
            detalhes = [
                local.get("admin1"),
                local.get("country"),
            ]
            localidade = ", ".join(item for item in detalhes if item)
            print(f"{indice} - {local['name']} ({localidade})")

        resposta = input("Escolha o número da cidade: ").strip()
        try:
            indice_escolhido = int(resposta)
        except ValueError as erro:
            raise WeatherError("escolha inválida para a cidade.") from erro
    else:
        indice_escolhido = escolha

    if not 1 <= indice_escolhido <= len(resultados):
        raise WeatherError("escolha inválida para a cidade.")
    return resultados[indice_escolhido - 1]


def _formatar_medida(valor, unidade):
    if valor is None:
        return "Indisponível"
    return f"{valor:g} {unidade}"


def consultar_tempo_atual(latitude, longitude):
    return _get_json(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,precipitation,wind_speed_10m,weather_code"
            ),
            "timezone": "auto",
            "forecast_days": 1,
        },
    )


def consultar_previsao_ponto(latitude, longitude):
    return _get_json(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "daily": "weather_code,temperature_2m_min,temperature_2m_max",
            "hourly": (
                "temperature_2m,precipitation,wind_speed_10m,"
                "wind_direction_10m,wind_gusts_10m,weather_code"
            ),
            "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
            "forecast_days": 7,
            "timezone": "auto",
        },
    )


def consultar_previsao(nome_cidade, data, escolha_cidade=None):
    dias = (data - date.today()).days + 1
    if dias > MAX_FORECAST_DAYS:
        raise ForecastUnavailableError(
            f"previsões reais estão disponíveis somente para os próximos "
            f"{MAX_FORECAST_DAYS} dias; selecione uma data dentro desse período."
        )

    local = _buscar_cidade(nome_cidade, escolha_cidade)
    dados = _get_json(
        FORECAST_URL,
        {
            "latitude": local["latitude"],
            "longitude": local["longitude"],
            "daily": (
                "weather_code,temperature_2m_min,temperature_2m_max,"
                "precipitation_probability_max,relative_humidity_2m_mean,"
                "wind_speed_10m_max"
            ),
            "timezone": "auto",
            "start_date": data.isoformat(),
            "end_date": data.isoformat(),
        },
    )

    diario = dados.get("daily")
    if not isinstance(diario, dict) or not isinstance(diario.get("time"), list):
        raise WeatherError("a API não retornou a previsão solicitada.")
    try:
        indice = diario["time"].index(data.isoformat())
        codigo = diario["weather_code"][indice]
        minima = diario["temperature_2m_min"][indice]
        maxima = diario["temperature_2m_max"][indice]
        chuva = diario["precipitation_probability_max"][indice]
        umidade = diario["relative_humidity_2m_mean"][indice]
        vento = diario["wind_speed_10m_max"][indice]
    except (KeyError, IndexError, ValueError, TypeError) as erro:
        raise WeatherError(
            f"a API meteorológica não retornou dados para "
            f"{data.strftime('%d/%m/%Y')}; tente novamente mais tarde."
        ) from erro

    try:
        condicao = WEATHER_CODES.get(int(codigo), "Condição desconhecida")
    except (TypeError, ValueError):
        condicao = "Indisponível"

    return {
        "cidade": ", ".join(
            parte
            for parte in (local.get("name"), local.get("country"))
            if parte
        ),
        "data": data.strftime("%d/%m/%Y"),
        "condicao": condicao,
        "temperatura_minima": _formatar_medida(minima, "°C"),
        "temperatura_maxima": _formatar_medida(maxima, "°C"),
        "probabilidade_chuva": _formatar_medida(chuva, "%"),
        "umidade": _formatar_medida(umidade, "%"),
        "vento": _formatar_medida(vento, "km/h"),
    }
