import json
from datetime import date, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_FORECAST_DAYS = 16
TIMEOUT_SECONDS = 10

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
    return data


def _get_json(url, parametros):
    requisicao = Request(
        f"{url}?{urlencode(parametros)}",
        headers={"User-Agent": "CalculadoraPrevisaoTempo/1.0"},
    )
    try:
        with urlopen(requisicao, timeout=TIMEOUT_SECONDS) as resposta:
            dados = json.loads(resposta.read().decode("utf-8"))
    except HTTPError as erro:
        raise WeatherError(
            f"serviço meteorológico indisponível (HTTP {erro.code})."
        ) from erro
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
            "forecast_days": dias,
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
        raise WeatherError("a previsão para essa data não está disponível.") from erro

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
