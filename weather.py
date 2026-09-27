import json
import math
import os
import threading
import time
from calendar import monthrange
from concurrent.futures import Future
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = (
    "https://customer-api.open-meteo.com/v1/forecast"
    if os.environ.get("OPEN_METEO_API_KEY")
    else "https://api.open-meteo.com/v1/forecast"
)
MAX_FORECAST_DAYS = 16
MAX_FUTURE_YEARS = 2
TIMEOUT_SECONDS = 10
GEOCODING_CACHE_SECONDS = 86400
FORECAST_CACHE_SECONDS = 600
RADAR_CACHE_SECONDS = 900
CURRENT_CACHE_SECONDS = 600
MAX_CACHE_ENTRIES = 512

_cache_lock = threading.Lock()
_cache = {}
_in_flight = {}
_usage_lock = threading.Lock()
_upstream_requests = 0
_rate_limit_lock = threading.Lock()
_rate_limit = {"blocked_until": None, "manual_retry_required": False, "headers": {}}
_RATE_LIMIT_HEADERS = {
    "retry-after",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}

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


class CityNotFoundError(WeatherError):
    """Erro quando a busca geográfica não encontra a cidade solicitada."""


class WeatherUpstreamError(WeatherError):
    """Erro da API externa que mantém o seu status HTTP e metadados úteis."""

    def __init__(self, message, status=502, headers=None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


class WeatherRateLimitError(WeatherUpstreamError):
    """Erro HTTP 429, incluindo o instante de nova tentativa quando informado."""

    def __init__(self, message, retry_at=None, headers=None):
        super().__init__(message, status=429, headers=headers)
        self.retry_at = retry_at


def _cache_result(key, ttl_seconds, loader):
    now = time.monotonic()
    with _cache_lock:
        _limpar_cache_expirado(now)
        cached = _cache.get(key)
        if cached is not None and cached[0] > now:
            return deepcopy(cached[1])
        future = _in_flight.get(key)
        is_owner = future is None
        if is_owner:
            future = Future()
            _in_flight[key] = future

    if not is_owner:
        return deepcopy(future.result())

    try:
        result = loader()
        with _cache_lock:
            expired = [
                cached_key
                for cached_key, (expires_at, _) in _cache.items()
                if expires_at <= time.monotonic()
            ]
            for cached_key in expired:
                _cache.pop(cached_key, None)
            while len(_cache) >= MAX_CACHE_ENTRIES:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (time.monotonic() + ttl_seconds, deepcopy(result))
        future.set_result(result)
        return deepcopy(result)
    except BaseException as error:
        future.set_exception(error)
        raise
    finally:
        with _cache_lock:
            _in_flight.pop(key, None)


def _limpar_cache_expirado(now=None):
    now = time.monotonic() if now is None else now
    expired = [
        cached_key
        for cached_key, (expires_at, _) in _cache.items()
        if expires_at <= now
    ]
    for cached_key in expired:
        _cache.pop(cached_key, None)


def _response_rate_headers(headers):
    return {
        name.lower(): str(value)
        for name, value in headers.items()
        if name.lower() in _RATE_LIMIT_HEADERS
    }


def _retry_time(headers, now=None):
    now = now or datetime.now(timezone.utc)
    headers = {name.lower(): value for name, value in headers.items()}
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return now + timedelta(seconds=max(0, int(retry_after)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass

    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            return datetime.fromtimestamp(float(reset), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass

    reset = headers.get("ratelimit-reset")
    if reset:
        try:
            return now + timedelta(seconds=max(0, float(reset)))
        except (ValueError, OverflowError):
            pass
    return None


def _check_rate_limit_locked():
    blocked_until = _rate_limit["blocked_until"]
    if blocked_until is None and not _rate_limit["manual_retry_required"]:
        return
    if blocked_until is not None and datetime.now(timezone.utc) >= blocked_until:
        _rate_limit.update(
            blocked_until=None,
            manual_retry_required=False,
            headers={},
        )
        return
    raise WeatherRateLimitError(
        "consultas pausadas após limite HTTP 429; "
        "aguarde o horário indicado ou libere uma tentativa manual.",
        retry_at=blocked_until,
        headers=_rate_limit["headers"],
    )


def _iniciar_solicitacao_upstream():
    global _upstream_requests
    with _rate_limit_lock:
        _check_rate_limit_locked()
        _rate_limit["headers"] = {}
        with _usage_lock:
            _upstream_requests += 1


def liberar_tentativa_manual():
    with _rate_limit_lock:
        if not _rate_limit["manual_retry_required"]:
            return False
        _rate_limit.update(
            blocked_until=None,
            manual_retry_required=False,
            headers={},
        )
        return True


def obter_diagnostico():
    with _usage_lock:
        used = _upstream_requests
    with _rate_limit_lock:
        blocked_until = _rate_limit["blocked_until"]
        if blocked_until is not None and datetime.now(timezone.utc) >= blocked_until:
            _rate_limit.update(
                blocked_until=None,
                manual_retry_required=False,
                headers={},
            )
            blocked_until = None
        manual_retry_required = _rate_limit["manual_retry_required"]
        rate_headers = dict(_rate_limit["headers"])

    remaining = next(
        (
            rate_headers[name]
            for name in ("ratelimit-remaining", "x-ratelimit-remaining")
            if name in rate_headers
        ),
        None,
    )
    reset_at = _retry_time(
        {
            name: value
            for name, value in rate_headers.items()
            if name in ("ratelimit-reset", "x-ratelimit-reset")
        }
    )
    limit = next(
        (
            rate_headers[name]
            for name in ("ratelimit-limit", "x-ratelimit-limit")
            if name in rate_headers
        ),
        None,
    )
    return {
        "api": "Open-Meteo",
        "plano": (
            "Previsões pela API de cliente; busca de cidades pela API pública"
            if os.environ.get("OPEN_METEO_API_KEY")
            else "Previsões pela API pública gratuita; uso não comercial"
        ),
        "limite_diario_oficial": (
            (
                "Limite da API de cliente não consta na configuração; "
                "a busca de cidades usa a API pública."
            )
            if os.environ.get("OPEN_METEO_API_KEY")
            else (
                "Previsões públicas: menos de 10.000 chamadas por dia, 5.000 por hora "
                "e 600 por minuto (uso não comercial; termos oficiais Open-Meteo). "
                "A cota da busca de cidades não está especificada aqui."
            )
        ),
        "consultas_restantes_informadas": remaining,
        "limite_informado_pelo_servidor": limit,
        "consultas_observadas_desde_inicializacao": used,
        "contagem_observada_global": False,
        "headers_de_limite": rate_headers,
        "proxima_consulta_disponivel": (
            blocked_until.isoformat() if blocked_until is not None else None
        ),
        "horario_de_renovacao": reset_at.isoformat() if reset_at else None,
        "tentativa_manual_necessaria": manual_retry_required,
    }


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


def _solicitar_json(url_completo):
    requisicao = Request(
        url_completo,
        headers={"User-Agent": "CalculadoraPrevisaoTempo/1.0"},
    )
    try:
        _iniciar_solicitacao_upstream()
        with urlopen(requisicao, timeout=TIMEOUT_SECONDS) as resposta:
            rate_headers = _response_rate_headers(
                getattr(resposta, "headers", {}) or {}
            )
            dados = json.loads(resposta.read().decode("utf-8"))
        with _rate_limit_lock:
            if (
                _rate_limit["blocked_until"] is None
                and not _rate_limit["manual_retry_required"]
            ):
                _rate_limit["headers"] = rate_headers
    except HTTPError as erro:
        headers = _response_rate_headers(erro.headers or {})
        if erro.code == 429:
            retry_at = _retry_time(erro.headers)
            with _rate_limit_lock:
                _rate_limit.update(
                    blocked_until=retry_at,
                    manual_retry_required=retry_at is None,
                    headers=headers,
                )
            message = "Limite de consultas da API meteorológica atingido."
            if retry_at is None:
                message += " A API não informou quando será possível consultar novamente."
            erro.close()
            raise WeatherRateLimitError(message, retry_at, headers) from erro
        with _rate_limit_lock:
            if (
                _rate_limit["blocked_until"] is None
                and not _rate_limit["manual_retry_required"]
            ):
                _rate_limit["headers"] = headers
        erro.close()
        raise WeatherUpstreamError(
            f"serviço meteorológico indisponível (HTTP {erro.code}).",
            status=erro.code,
            headers=headers,
        ) from erro
    except (URLError, TimeoutError, OSError) as erro:
        is_timeout = isinstance(erro, TimeoutError) or isinstance(
            getattr(erro, "reason", None), TimeoutError
        )
        status = 504 if is_timeout else 503
        message = (
            "tempo limite ao consultar o serviço meteorológico."
            if is_timeout
            else "não foi possível conectar ao serviço meteorológico; "
            "verifique sua conexão com a internet."
        )
        raise WeatherUpstreamError(message, status=status) from erro
    except (UnicodeDecodeError, json.JSONDecodeError) as erro:
        raise WeatherUpstreamError(
            "o serviço retornou uma resposta inválida.", status=502
        ) from erro

    if not isinstance(dados, dict):
        raise WeatherError("o serviço retornou uma resposta inválida.")
    if dados.get("error"):
        detalhe = dados.get("reason", "erro não especificado")
        raise WeatherUpstreamError(
            f"erro retornado pela API: {detalhe}.",
            status=400,
        )
    return dados


def _get_json(url, parametros):
    if url == FORECAST_URL and os.environ.get("OPEN_METEO_API_KEY"):
        parametros = {**parametros, "apikey": os.environ["OPEN_METEO_API_KEY"]}
    url_completo = f"{url}?{urlencode(parametros)}"
    ttl_seconds = (
        GEOCODING_CACHE_SECONDS
        if url == GEOCODING_URL
        else FORECAST_CACHE_SECONDS
    )
    return _cache_result(
        ("api", url_completo),
        ttl_seconds,
        lambda: _solicitar_json(url_completo),
    )


def _buscar_cidade(nome, escolha=None):
    dados = _cache_result(
        ("geocoding", nome.strip().casefold()),
        GEOCODING_CACHE_SECONDS,
        lambda: _get_json(
            GEOCODING_URL,
            {
                "name": nome,
                "count": 10,
                "language": "pt",
                "format": "json",
            },
        ),
    )
    resultados = dados.get("results")
    if resultados is None:
        raise CityNotFoundError(f"nenhuma cidade encontrada para “{nome}”.")
    if not isinstance(resultados, list) or any(
        not isinstance(local, dict) or not isinstance(local.get("name"), str)
        for local in resultados
    ):
        raise WeatherError("o serviço de busca retornou cidades em formato inválido.")
    if not resultados:
        raise CityNotFoundError(f"nenhuma cidade encontrada para “{nome}”.")

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

    chave = (
        "forecast",
        nome_cidade.strip().casefold(),
        data.isoformat(),
        escolha_cidade,
    )
    return _cache_result(
        chave,
        FORECAST_CACHE_SECONDS,
        lambda: _consultar_previsao_sem_cache(
            nome_cidade, data, escolha_cidade
        ),
    )


def _consultar_previsao_sem_cache(nome_cidade, data, escolha_cidade):
    local = _buscar_cidade(nome_cidade, escolha_cidade)
    try:
        latitude = float(local["latitude"])
        longitude = float(local["longitude"])
    except (KeyError, TypeError, ValueError) as erro:
        raise WeatherError("o serviço de busca retornou coordenadas inválidas.") from erro
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise WeatherError("o serviço de busca retornou coordenadas inválidas.")
    dados = _get_json(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
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


def consultar_previsao_ponto(latitude, longitude):
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("coordenadas inválidas.")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("coordenadas fora dos limites geográficos.")

    latitude = round(latitude, 2)
    longitude = round(longitude, 2)
    parametros = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "weather_code,temperature_2m_min,temperature_2m_max",
        "hourly": (
            "temperature_2m,precipitation,wind_speed_10m,"
            "wind_direction_10m,wind_gusts_10m,weather_code"
        ),
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "forecast_days": "7",
        "timezone": "auto",
    }
    return _cache_result(
        ("radar", latitude, longitude),
        RADAR_CACHE_SECONDS,
        lambda: _get_json(FORECAST_URL, parametros),
    )


def consultar_tempo_atual(latitude, longitude):
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("coordenadas inválidas.")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("coordenadas fora dos limites geográficos.")

    latitude = round(latitude, 2)
    longitude = round(longitude, 2)
    parametros = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "timezone": "auto",
        "forecast_days": 1,
    }
    return _cache_result(
        ("current", latitude, longitude),
        CURRENT_CACHE_SECONDS,
        lambda: _get_json(FORECAST_URL, parametros),
    )
