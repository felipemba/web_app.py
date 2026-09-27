import json
import logging
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
UPSTREAM_MAX_CONCURRENT = 4
UPSTREAM_QUEUE_TIMEOUT_SECONDS = 2
MAX_UPSTREAM_RESPONSE_BYTES = 2 * 1024 * 1024
GEOCODING_CACHE_SECONDS = 86400
GEOCODING_STALE_SECONDS = 86400
FORECAST_CACHE_SECONDS = 600
FORECAST_STALE_SECONDS = 3600
RADAR_CACHE_SECONDS = 900
RADAR_STALE_SECONDS = 3600
CURRENT_CACHE_SECONDS = 600
CURRENT_STALE_SECONDS = 3600
NEAR_LIMIT_CACHE_MULTIPLIER = 3
MAX_CACHE_ENTRIES = 512
MAX_CACHE_ENTRY_BYTES = 256 * 1024
MAX_CACHE_BYTES = 32 * 1024 * 1024

_logger = logging.getLogger("weather")
_cache_lock = threading.Lock()
_cache = {}
_stale_cache = {}
_in_flight = {}
_usage_lock = threading.Lock()
_upstream_requests = 0
_metrics = {
    "cache_hits": 0,
    "cache_misses": 0,
    "duplicate_requests_blocked": 0,
    "stale_cache_hits": 0,
    "upstream_errors": {},
    "timeouts": 0,
    "connection_errors": 0,
    "overload_rejections": 0,
    "upstream_duration_ms_total": 0,
    "upstream_duration_ms_max": 0,
}
_request_context = threading.local()
_upstream_semaphore = threading.BoundedSemaphore(UPSTREAM_MAX_CONCURRENT)
_rate_limit_lock = threading.Lock()
_rate_limit = {
    "blocked_until": None,
    "manual_retry_required": False,
    "manual_retry_after": None,
    "unknown_429_count": 0,
    "transient_blocked_until": None,
    "consecutive_failures": 0,
    "headers": {},
}
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

    def __init__(self, message, status=502, headers=None, retry_at=None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}
        self.retry_at = retry_at


class WeatherRateLimitError(WeatherUpstreamError):
    """Erro HTTP 429, incluindo o instante de nova tentativa quando informado."""

    def __init__(
        self,
        message,
        retry_at=None,
        headers=None,
        manual_retry_required=False,
    ):
        super().__init__(message, status=429, headers=headers)
        self.retry_at = retry_at
        self.manual_retry_required = manual_retry_required


class _StaleCacheValue(dict):
    pass


def _registrar_metrica(nome, quantidade=1):
    with _usage_lock:
        _metrics[nome] += quantidade


def _cache_result(key, ttl_seconds, loader, stale_ttl_seconds=0):
    now = time.monotonic()
    with _cache_lock:
        _limpar_cache_expirado(now)
        cached = _cache.get(key)
        if cached is not None and cached[0] > now:
            _registrar_metrica("cache_hits")
            return deepcopy(cached[1])
        future = _in_flight.get(key)
        is_owner = future is None
        if is_owner:
            _registrar_metrica("cache_misses")
            future = Future()
            _in_flight[key] = future
        else:
            _registrar_metrica("duplicate_requests_blocked")

    if not is_owner:
        result = future.result()
        if isinstance(result, _StaleCacheValue):
            _request_context.used_stale_cache = True
        return deepcopy(result)

    try:
        result = loader()
        stale_result = isinstance(result, _StaleCacheValue) or getattr(
            _request_context,
            "used_stale_cache",
            False,
        )
        if stale_result:
            _request_context.used_stale_cache = True
            if isinstance(result, dict) and not isinstance(result, _StaleCacheValue):
                result = _StaleCacheValue(result)
        else:
            with _cache_lock:
                expired = [
                    cached_key
                    for cached_key, (expires_at, _) in _cache.items()
                    if expires_at <= time.monotonic()
                ]
                for cached_key in expired:
                    _cache.pop(cached_key, None)
                while len(_cache) >= MAX_CACHE_ENTRIES:
                    oldest_key = next(iter(_cache))
                    _cache.pop(oldest_key)
                    _stale_cache.pop(oldest_key, None)
                cache_value = deepcopy(result)
                cache_size = len(
                    json.dumps(
                        cache_value,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                if cache_size <= MAX_CACHE_ENTRY_BYTES:
                    fresh_ttl = _ttl_cache_efetivo(ttl_seconds)
                    expires_at = time.monotonic() + fresh_ttl
                    _cache[key] = (expires_at, cache_value)
                    if stale_ttl_seconds > 0:
                        _stale_cache.pop(key, None)
                        _stale_cache[key] = (
                            expires_at + stale_ttl_seconds,
                            cache_value,
                        )
                    while len(_stale_cache) > MAX_CACHE_ENTRIES:
                        _stale_cache.pop(next(iter(_stale_cache)))
                    cache_bytes = sum(_cache_value_bytes(value) for _, value in _cache.values())
                    stale_bytes = sum(
                        _cache_value_bytes(value)
                        for _, value in _stale_cache.values()
                    )
                    while cache_bytes + stale_bytes > MAX_CACHE_BYTES:
                        if _stale_cache:
                            removed_key = next(iter(_stale_cache))
                            _, removed = _stale_cache.pop(removed_key)
                            stale_bytes -= _cache_value_bytes(removed)
                        elif _cache:
                            removed_key = next(iter(_cache))
                            removed = _cache.pop(removed_key)
                            cache_bytes -= _cache_value_bytes(removed[1])
                        else:
                            break
                else:
                    _cache.pop(key, None)
        future.set_result(result)
        return deepcopy(result)
    except BaseException as error:
        stale = None
        if (
            isinstance(error, Exception)
            and isinstance(error, WeatherError)
            and not isinstance(error, (CityNotFoundError, ForecastUnavailableError))
        ):
            with _cache_lock:
                stale_entry = _stale_cache.get(key)
                if stale_entry is not None and stale_entry[0] > time.monotonic():
                    stale = deepcopy(stale_entry[1])
        if stale is not None:
            _registrar_metrica("stale_cache_hits")
            _request_context.used_stale_cache = True
            _logger.warning(
                "Using stale weather cache after %s",
                type(error).__name__,
            )
            if isinstance(stale, dict):
                stale = _StaleCacheValue(stale)
            future.set_result(stale)
            return stale
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
    stale_expired = [
        cached_key
        for cached_key, (expires_at, _) in _stale_cache.items()
        if expires_at <= now
    ]
    for cached_key in stale_expired:
        _stale_cache.pop(cached_key, None)


def _cache_value_bytes(value):
    serialized_size = len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    return serialized_size * 4


def _ttl_cache_efetivo(ttl_seconds):
    with _rate_limit_lock:
        headers = _rate_limit["headers"]
    remaining = next(
        (
            headers[name]
            for name in ("ratelimit-remaining", "x-ratelimit-remaining")
            if name in headers
        ),
        None,
    )
    limit = next(
        (
            headers[name]
            for name in ("ratelimit-limit", "x-ratelimit-limit")
            if name in headers
        ),
        None,
    )
    try:
        remaining_value = float(remaining)
        limit_value = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        return ttl_seconds
    if remaining_value <= 0 or (
        limit_value is not None
        and limit_value > 0
        and remaining_value / limit_value <= 0.1
    ):
        return ttl_seconds * NEAR_LIMIT_CACHE_MULTIPLIER
    return ttl_seconds


def limpar_estado_cache_da_requisicao():
    _request_context.used_stale_cache = False


def requisicao_usou_cache_antigo():
    return getattr(_request_context, "used_stale_cache", False)


def registrar_rejeicao_sobrecarga():
    _registrar_metrica("overload_rejections")


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
    if _rate_limit["manual_retry_required"]:
        manual_retry_after = _rate_limit["manual_retry_after"]
        if manual_retry_after is not None and datetime.now(timezone.utc) < manual_retry_after:
            raise WeatherRateLimitError(
                "nova tentativa manual temporariamente pausada para respeitar o limite da API.",
                retry_at=manual_retry_after,
                headers=_rate_limit["headers"],
                manual_retry_required=True,
            )
        raise WeatherRateLimitError(
            "limite de consultas atingido; libere uma tentativa manual para consultar novamente.",
            retry_at=manual_retry_after,
            headers=_rate_limit["headers"],
            manual_retry_required=True,
        )
    if blocked_until is None:
        return
    if blocked_until is not None and datetime.now(timezone.utc) >= blocked_until:
        _rate_limit.update(
            blocked_until=None,
            manual_retry_required=False,
            manual_retry_after=None,
            unknown_429_count=0,
            headers={},
        )
        return
    raise WeatherRateLimitError(
        "consultas pausadas após limite HTTP 429; "
        "aguarde o horário indicado ou libere uma tentativa manual.",
        retry_at=blocked_until,
        headers=_rate_limit["headers"],
    )


def _check_transient_backoff_locked():
    blocked_until = _rate_limit["transient_blocked_until"]
    if blocked_until is None:
        return
    if datetime.now(timezone.utc) >= blocked_until:
        _rate_limit.update(
            transient_blocked_until=None,
        )
        return
    raise WeatherUpstreamError(
        "consultas pausadas temporariamente após falhas no serviço meteorológico.",
        status=503,
        headers=_rate_limit["headers"],
        retry_at=blocked_until,
    )


def _iniciar_solicitacao_upstream():
    global _upstream_requests
    with _rate_limit_lock:
        _check_rate_limit_locked()
        _check_transient_backoff_locked()
        with _usage_lock:
            _upstream_requests += 1


def _registrar_falha_transitoria():
    with _rate_limit_lock:
        failures = _rate_limit["consecutive_failures"] + 1
        delay_seconds = min(2 ** min(failures, 6), 60)
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        _rate_limit.update(
            consecutive_failures=failures,
            transient_blocked_until=retry_at,
        )
    return retry_at


def _registrar_headers_limite(headers):
    with _rate_limit_lock:
        if (
            _rate_limit["blocked_until"] is None
            and not _rate_limit["manual_retry_required"]
        ):
            _rate_limit["headers"] = headers


def liberar_tentativa_manual():
    with _rate_limit_lock:
        if not _rate_limit["manual_retry_required"]:
            return False
        retry_at = _rate_limit["manual_retry_after"]
        if retry_at is not None and datetime.now(timezone.utc) < retry_at:
            raise WeatherRateLimitError(
                "aguarde o intervalo de proteção antes de liberar uma nova tentativa.",
                retry_at=retry_at,
                headers=_rate_limit["headers"],
                manual_retry_required=True,
            )
        _rate_limit.update(
            blocked_until=None,
            manual_retry_required=False,
            manual_retry_after=None,
            headers={},
        )
        return True


def obter_diagnostico():
    with _usage_lock:
        used = _upstream_requests
        metrics = deepcopy(_metrics)
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
        manual_retry_after = _rate_limit["manual_retry_after"]
        unknown_429_count = _rate_limit["unknown_429_count"]
        rate_headers = dict(_rate_limit["headers"])
        transient_blocked_until = _rate_limit["transient_blocked_until"]
        consecutive_failures = _rate_limit["consecutive_failures"]

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
    near_limit = False
    try:
        remaining_value = float(remaining)
        limit_value = float(limit) if limit is not None else None
        near_limit = remaining_value <= 0 or (
            limit_value is not None
            and limit_value > 0
            and remaining_value / limit_value <= 0.1
        )
    except (TypeError, ValueError):
        pass
    metrics["upstream_errors"] = dict(metrics["upstream_errors"])
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
        "proxima_tentativa_manual": (
            manual_retry_after.isoformat()
            if manual_retry_after is not None
            else None
        ),
        "429_sem_prazo_informado": unknown_429_count,
        "proxima_tentativa_transitoria": (
            transient_blocked_until.isoformat()
            if transient_blocked_until is not None
            else None
        ),
        "falhas_transitorias_consecutivas": consecutive_failures,
        "limite_proximo_informado": near_limit,
        "ttl_cache_multiplicador": (
            NEAR_LIMIT_CACHE_MULTIPLIER if near_limit else 1
        ),
        "concorrencia_upstream_maxima": UPSTREAM_MAX_CONCURRENT,
        "timeouts_upstream": TIMEOUT_SECONDS,
        "limite_memoria_cache_bytes": MAX_CACHE_BYTES,
        "limite_entrada_cache_bytes": MAX_CACHE_ENTRY_BYTES,
        "metricas": metrics,
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


def _rejeitar_constante_json(valor):
    raise ValueError(f"constante JSON inválida: {valor}")


def _solicitar_json(url_completo):
    requisicao = Request(
        url_completo,
        headers={"User-Agent": "CalculadoraPrevisaoTempo/1.0"},
    )
    if not _upstream_semaphore.acquire(
        timeout=UPSTREAM_QUEUE_TIMEOUT_SECONDS
    ):
        _registrar_metrica("overload_rejections")
        _logger.warning("Weather upstream concurrency limit reached")
        raise WeatherUpstreamError(
            "o serviço meteorológico está ocupado; aguarde um instante e tente novamente.",
            status=503,
        )

    request_started = time.monotonic()
    try:
        _iniciar_solicitacao_upstream()
        try:
            with urlopen(requisicao, timeout=TIMEOUT_SECONDS) as resposta:
                rate_headers = _response_rate_headers(
                    getattr(resposta, "headers", {}) or {}
                )
                corpo = resposta.read(MAX_UPSTREAM_RESPONSE_BYTES + 1)
            if len(corpo) > MAX_UPSTREAM_RESPONSE_BYTES:
                raise WeatherUpstreamError(
                    "o serviço meteorológico retornou uma resposta acima do limite permitido.",
                    status=502,
                )
            dados = json.loads(
                corpo.decode("utf-8"),
                parse_constant=_rejeitar_constante_json,
            )
            _registrar_headers_limite(rate_headers)
            with _rate_limit_lock:
                transient_until = _rate_limit["transient_blocked_until"]
                if (
                    transient_until is None
                    or datetime.now(timezone.utc) >= transient_until
                ):
                    _rate_limit.update(
                        transient_blocked_until=None,
                        consecutive_failures=0,
                    )
                if (
                    _rate_limit["blocked_until"] is None
                    and not _rate_limit["manual_retry_required"]
                ):
                    _rate_limit["unknown_429_count"] = 0
        except HTTPError as erro:
            headers = _response_rate_headers(erro.headers or {})
            if erro.code == 429:
                retry_at = _retry_time(erro.headers)
                with _rate_limit_lock:
                    manual_retry_after = None
                    unknown_429_count = _rate_limit["unknown_429_count"]
                    manual_retry_required = retry_at is None
                    if retry_at is None:
                        unknown_429_count += 1
                        manual_retry_after = datetime.now(timezone.utc) + timedelta(
                            seconds=min(60 * 2 ** min(unknown_429_count - 1, 6), 3600)
                        )
                    else:
                        unknown_429_count = 0
                    _rate_limit.update(
                        blocked_until=retry_at,
                        manual_retry_required=manual_retry_required,
                        manual_retry_after=manual_retry_after,
                        unknown_429_count=unknown_429_count,
                        headers=headers,
                    )
                with _usage_lock:
                    _metrics["upstream_errors"]["429"] = (
                        _metrics["upstream_errors"].get("429", 0) + 1
                    )
                message = "Limite de consultas da API meteorológica atingido."
                if retry_at is None:
                    message += " A API não informou quando será possível consultar novamente."
                _logger.warning("Open-Meteo rate limit reached (HTTP 429)")
                erro.close()
                raise WeatherRateLimitError(
                    message,
                    retry_at or manual_retry_after,
                    headers,
                    manual_retry_required=manual_retry_required,
                ) from erro
            _registrar_headers_limite(headers)
            retry_at = None
            if erro.code == 408 or 500 <= erro.code <= 599:
                retry_at = _registrar_falha_transitoria()
            with _usage_lock:
                _metrics["upstream_errors"][str(erro.code)] = (
                    _metrics["upstream_errors"].get(str(erro.code), 0) + 1
                )
            _logger.warning("Open-Meteo returned HTTP %s", erro.code)
            erro.close()
            raise WeatherUpstreamError(
                f"serviço meteorológico indisponível (HTTP {erro.code}).",
                status=erro.code,
                headers=headers,
                retry_at=retry_at,
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
            retry_at = _registrar_falha_transitoria()
            with _usage_lock:
                metric = "timeouts" if is_timeout else "connection_errors"
                _metrics[metric] += 1
                _metrics["upstream_errors"][str(status)] = (
                    _metrics["upstream_errors"].get(str(status), 0) + 1
                )
            _logger.warning("Open-Meteo request failed (%s)", message)
            raise WeatherUpstreamError(
                message,
                status=status,
                retry_at=retry_at,
            ) from erro
        except (UnicodeDecodeError, ValueError) as erro:
            retry_at = _registrar_falha_transitoria()
            with _usage_lock:
                _metrics["upstream_errors"]["502"] = (
                    _metrics["upstream_errors"].get("502", 0) + 1
                )
            _logger.warning("Open-Meteo returned an invalid response")
            raise WeatherUpstreamError(
                "o serviço retornou uma resposta inválida.",
                status=502,
                retry_at=retry_at,
            ) from erro
        except WeatherUpstreamError as erro:
            if erro.status >= 500 and not isinstance(erro, WeatherRateLimitError):
                erro.retry_at = _registrar_falha_transitoria()
                with _usage_lock:
                    _metrics["upstream_errors"][str(erro.status)] = (
                        _metrics["upstream_errors"].get(str(erro.status), 0) + 1
                    )
            raise
    finally:
        elapsed_ms = (time.monotonic() - request_started) * 1000
        with _usage_lock:
            _metrics["upstream_duration_ms_total"] += int(elapsed_ms)
            _metrics["upstream_duration_ms_max"] = max(
                _metrics["upstream_duration_ms_max"],
                int(elapsed_ms),
            )
        _upstream_semaphore.release()

    if not isinstance(dados, dict):
        retry_at = _registrar_falha_transitoria()
        with _usage_lock:
            _metrics["upstream_errors"]["502"] = (
                _metrics["upstream_errors"].get("502", 0) + 1
            )
        _logger.warning("Open-Meteo returned a non-object response")
        raise WeatherUpstreamError(
            "o serviço retornou uma resposta inválida.",
            status=502,
            retry_at=retry_at,
        )
    if dados.get("error"):
        detalhe = dados.get("reason", "erro não especificado")
        raise WeatherUpstreamError(
            f"erro retornado pela API: {detalhe}.",
            status=400,
        )
    return dados


def _validar_geocodificacao(dados):
    resultados = dados.get("results")
    if resultados is None:
        return
    if not isinstance(resultados, list) or any(
        not isinstance(local, dict)
        or not isinstance(local.get("name"), str)
        or isinstance(local.get("latitude"), bool)
        or not isinstance(local.get("latitude"), (int, float))
        or not -90 <= local["latitude"] <= 90
        or not _numero_finito(local["latitude"])
        or isinstance(local.get("longitude"), bool)
        or not isinstance(local.get("longitude"), (int, float))
        or not -180 <= local["longitude"] <= 180
        or not _numero_finito(local["longitude"])
        for local in resultados
    ):
        raise WeatherError("o serviço de busca retornou cidades em formato inválido.")


def _numero_finito(valor):
    try:
        return math.isfinite(valor)
    except OverflowError:
        return False


def _valor_meteorologico_valido(valor):
    return valor is None or (
        not isinstance(valor, bool)
        and isinstance(valor, (int, float))
        and _numero_finito(valor)
    )


def _validar_tempo_atual(dados):
    current = dados.get("current")
    required = (
        "temperature_2m",
        "precipitation",
        "wind_speed_10m",
        "weather_code",
    )
    if not isinstance(current, dict) or any(
        isinstance(current.get(key), bool)
        or not isinstance(current.get(key), (int, float))
        or not _numero_finito(current[key])
        for key in required
    ):
        raise WeatherError("o serviço não retornou condições atuais válidas.")


def _validar_previsao_radar(dados):
    hourly = dados.get("hourly")
    daily = dados.get("daily")
    hourly_fields = (
        "time",
        "temperature_2m",
        "precipitation",
        "wind_speed_10m",
        "wind_direction_10m",
        "wind_gusts_10m",
        "weather_code",
    )
    daily_fields = (
        "time",
        "weather_code",
        "temperature_2m_min",
        "temperature_2m_max",
    )
    if (
        not isinstance(hourly, dict)
        or not isinstance(daily, dict)
        or any(not isinstance(hourly.get(field), list) for field in hourly_fields)
        or any(not isinstance(daily.get(field), list) for field in daily_fields)
        or not hourly["time"]
        or not daily["time"]
        or any(len(hourly[field]) != len(hourly["time"]) for field in hourly_fields)
        or any(len(daily[field]) != len(daily["time"]) for field in daily_fields)
        or any(
            not isinstance(value, str)
            for value in (*hourly["time"], *daily["time"])
        )
        or any(
            not _valor_meteorologico_valido(value)
            for field in hourly_fields[1:]
            for value in hourly[field]
        )
        or any(
            not _valor_meteorologico_valido(value)
            for field in daily_fields[1:]
            for value in daily[field]
        )
    ):
        raise WeatherError("o serviço não retornou uma previsão válida para o ponto.")


def _validar_previsao_diaria(dados, data):
    daily = dados.get("daily")
    fields = (
        "time",
        "weather_code",
        "temperature_2m_min",
        "temperature_2m_max",
        "precipitation_probability_max",
        "relative_humidity_2m_mean",
        "wind_speed_10m_max",
    )
    if (
        not isinstance(daily, dict)
        or any(not isinstance(daily.get(field), list) for field in fields)
        or any(len(daily[field]) != len(daily["time"]) for field in fields)
        or any(not isinstance(value, str) for value in daily["time"])
        or data.isoformat() not in daily["time"]
        or any(
            not _valor_meteorologico_valido(value)
            for field in fields[1:]
            for value in daily[field]
        )
    ):
        raise WeatherError("a API não retornou a previsão solicitada.")


def _get_json(url, parametros, validator=None):
    if url == FORECAST_URL and os.environ.get("OPEN_METEO_API_KEY"):
        parametros = {**parametros, "apikey": os.environ["OPEN_METEO_API_KEY"]}
    url_completo = f"{url}?{urlencode(parametros)}"
    ttl_seconds = (
        GEOCODING_CACHE_SECONDS
        if url == GEOCODING_URL
        else FORECAST_CACHE_SECONDS
    )
    def carregar():
        dados = _solicitar_json(url_completo)
        if validator is not None:
            try:
                validator(dados)
            except WeatherError as error:
                retry_at = _registrar_falha_transitoria()
                with _usage_lock:
                    _metrics["upstream_errors"]["502"] = (
                        _metrics["upstream_errors"].get("502", 0) + 1
                    )
                _logger.warning("Open-Meteo returned malformed weather data")
                raise WeatherUpstreamError(
                    "o serviço meteorológico retornou dados inválidos.",
                    status=502,
                    retry_at=retry_at,
                ) from error
        return dados

    return _cache_result(
        ("api", url_completo),
        ttl_seconds,
        carregar,
        stale_ttl_seconds=(
            GEOCODING_STALE_SECONDS
            if url == GEOCODING_URL
            else FORECAST_STALE_SECONDS
        ),
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
            validator=_validar_geocodificacao,
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
        validator=_validar_previsao_radar,
    )


def consultar_previsao(nome_cidade, data, escolha_cidade=None):
    limpar_estado_cache_da_requisicao()
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
        stale_ttl_seconds=FORECAST_STALE_SECONDS,
    )


def _consultar_previsao_sem_cache(nome_cidade, data, escolha_cidade):
    local = _buscar_cidade(nome_cidade, escolha_cidade)
    try:
        latitude = float(local["latitude"])
        longitude = float(local["longitude"])
    except (KeyError, OverflowError, TypeError, ValueError) as erro:
        raise WeatherError("o serviço de busca retornou coordenadas inválidas.") from erro
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("coordenadas fora dos limites geográficos.")
    if not _numero_finito(latitude) or not _numero_finito(longitude):
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
        validator=lambda response: _validar_previsao_diaria(response, data),
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
    limpar_estado_cache_da_requisicao()
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("coordenadas fora dos limites geográficos.")
    if not _numero_finito(latitude) or not _numero_finito(longitude):
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
        lambda: _get_json(
            FORECAST_URL,
            parametros,
            validator=_validar_previsao_radar,
        ),
        stale_ttl_seconds=RADAR_STALE_SECONDS,
    )


def consultar_tempo_atual(latitude, longitude):
    limpar_estado_cache_da_requisicao()
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("coordenadas fora dos limites geográficos.")
    if not _numero_finito(latitude) or not _numero_finito(longitude):
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
        lambda: _get_json(
            FORECAST_URL,
            parametros,
            validator=_validar_tempo_atual,
        ),
        stale_ttl_seconds=CURRENT_STALE_SECONDS,
    )
