const form = document.querySelector("#weather-form");
const cityInput = document.querySelector("#city");
const dateInput = document.querySelector("#forecast-date");
const searchButton = document.querySelector("#search-button");
const buttonLabel = searchButton.querySelector(".button-label");
const message = document.querySelector("#form-message");
const quotaAvailable = document.querySelector("#quota-available");
const quotaUsed = document.querySelector("#quota-used");
const quotaLimit = document.querySelector("#quota-limit");
const quotaRenewal = document.querySelector("#quota-renewal");
const quotaNote = document.querySelector("#quota-note");
const quotaRetryButton = document.querySelector("#quota-retry");
const emptyState = document.querySelector("#empty-state");
const forecast = document.querySelector("#forecast");
const mapButtons = [...document.querySelectorAll(".map-mode")];
const mapStatus = document.querySelector("#map-status");
const openRadarButton = document.querySelector("#open-radar");
const radarDialog = document.querySelector("#radar-dialog");
const closeRadarButton = document.querySelector("#close-radar");
const radarStatus = document.querySelector("#radar-status");
const radarMapElement = document.querySelector("#radar-map");
const radarLocationSummary = document.querySelector("#radar-location-summary");
const radarWeek = document.querySelector("#radar-week");
const radarHourlyTitle = document.querySelector("#radar-hourly-title");
const radarTimeSlider = document.querySelector("#radar-time");
const radarHourly = document.querySelector("#radar-hourly");
const radarPlayButton = document.querySelector("#radar-play");
const radarLivePlayButton = document.querySelector("#radar-live-play");
const radarAlertList = document.querySelector("#radar-alert-list");
const radarLayerButtons = [...document.querySelectorAll(".radar-layer-button")];

const weatherPoints = [
  { name: "Nova York", lat: 40.7128, lon: -74.0060, country: "EUA" },
  { name: "São Paulo", lat: -23.5505, lon: -46.6333, country: "Brasil" },
  { name: "Londres", lat: 51.5074, lon: -0.1278, country: "Reino Unido" },
  { name: "Dubai", lat: 25.2048, lon: 55.2708, country: "Emirados" },
  { name: "Tóquio", lat: 35.6762, lon: 139.6503, country: "Japão" },
  { name: "Sydney", lat: -33.8688, lon: 151.2093, country: "Austrália" },
  { name: "Cidade do Cabo", lat: -33.9249, lon: 18.4241, country: "África do Sul" },
  { name: "Buenos Aires", lat: -34.6037, lon: -58.3816, country: "Argentina" },
];

let weatherMapInstance = null;
let weatherLayers = [];
let currentMapMode = "temperature";
let globalWeatherLoading = false;
let radarMap = null;
let radarRainLayer = null;
let radarRainRequestStarted = false;
let radarPointLayer = null;
let radarFrames = [];
let radarFrameIndex = 0;
let radarRainHost = "";
let radarForecastData = null;
let radarSelectedDate = null;
let radarSelectedHours = [];
let radarLayerMode = "rain";
let radarAnimationTimer = null;
let radarFrameTimer = null;
let radarRequestId = 0;
let radarRequestController = null;
let radarLastFocusedElement = null;
const RAINVIEWER_API_URL = "https://api.rainviewer.com/public/weather-maps.json";

function formatTimeUntil(timestamp) {
  const milliseconds = new Date(timestamp).getTime() - Date.now();
  if (!Number.isFinite(milliseconds) || milliseconds <= 0) return null;
  const totalMinutes = Math.ceil(milliseconds / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return {
    duration: `${hours} horas e ${minutes} minutos`,
    time: new Date(timestamp).toLocaleTimeString("pt-BR", {
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    }),
  };
}

function retryAfterDate(value) {
  if (!value) return null;
  const seconds = Number(value);
  const timestamp = Number.isFinite(seconds)
    ? Date.now() + Math.max(0, seconds) * 1000
    : Date.parse(value);
  const retryAt = new Date(timestamp);
  return Number.isFinite(retryAt.getTime()) ? retryAt.toISOString() : null;
}

async function refreshQuotaStatus() {
  try {
    const response = await fetch("/api/diagnostico", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    quotaAvailable.textContent = "Não informado pela API";
    quotaUsed.textContent = `${data.consultas_observadas_desde_inicializacao} tentativas iniciadas nesta instância; a API não confirma o consumo individual`;
    quotaLimit.textContent = data.limite_informado_pelo_servidor
      ? `${data.limite_informado_pelo_servidor} (janela não especificada)`
      : data.limite_diario_oficial;
    quotaRenewal.textContent = data.horario_de_renovacao
      ? new Date(data.horario_de_renovacao).toLocaleTimeString("pt-BR", {
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "short",
      })
      : "Não informado pela API";
    const parts = [
      `${data.api}; ${data.plano}.`,
      "O contador interno registra tentativas iniciadas desde que esta instância subiu; pode incluir falhas e não representa o consumo confirmado nem o uso de outros servidores/clientes.",
      "O horário de renovação só aparece quando informado pelos headers da API.",
    ];
    const retry = data.proxima_consulta_disponivel
      ? formatTimeUntil(data.proxima_consulta_disponivel)
      : null;
    if (retry) {
      parts.push(`Novas consultas suspensas até aproximadamente ${retry.time} (${retry.duration}).`);
    } else if (data.tentativa_manual_necessaria) {
      parts.push("A API não informou quando liberar. Nenhuma chamada será repetida automaticamente; libere uma tentativa manual quando desejar.");
    }
    if (data.headers_de_limite && Object.keys(data.headers_de_limite).length > 0) {
      parts.push("Headers de limite recebidos: " + Object.entries(data.headers_de_limite)
        .map(([name, value]) => `${name}: ${value}`)
        .join("; ") + ". A API não identificou a janela desses valores; eles não são um saldo diário.");
    }
    quotaNote.textContent = parts.join(" ");
    quotaRetryButton.hidden = !data.tentativa_manual_necessaria;
  } catch {
    quotaNote.textContent = "Não foi possível consultar o diagnóstico do serviço.";
  }
}

quotaRetryButton.addEventListener("click", async () => {
  quotaRetryButton.disabled = true;
  try {
    const response = await fetch("/api/tentar-novamente", { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.erro || "Não foi possível liberar a tentativa.");
    await refreshQuotaStatus();
    if (result.liberada) {
      quotaNote.textContent += " " + result.mensagem
        + " Nenhuma consulta meteorológica foi feita automaticamente.";
    }
  } catch (error) {
    quotaNote.textContent = error.message;
  } finally {
    quotaRetryButton.disabled = false;
  }
});

function getMapModeColor(mode, value) {
  if (mode === "rain") {
    if (value >= 1) return "#4b8dff";
    if (value >= 0.2) return "#6ecbff";
    return "#bfe6ff";
  }
  if (mode === "wind") {
    if (value >= 40) return "#ff8a5a";
    if (value >= 25) return "#f9d76c";
    return "#8de0bf";
  }
  if (mode === "storm") {
    return value ? "#ff6b57" : "#7ad9ff";
  }
  if (value >= 25) return "#ff8f66";
  if (value >= 15) return "#ffd166";
  return "#7ad9ff";
}

function updateMapModeButtons() {
  mapButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === currentMapMode);
  });
}

function initializeMap() {
  if (!window.L || !document.querySelector("#weather-map")) return;
  if (weatherMapInstance) return;

  weatherMapInstance = L.map("weather-map", {
    zoomControl: true,
    attributionControl: true,
    worldCopyJump: true,
  }).setView([20, 0], 2);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 7,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(weatherMapInstance);

  mapButtons.forEach((button) => {
    button.addEventListener("click", () => {
      currentMapMode = button.dataset.mode;
      updateMapModeButtons();
      renderWeatherMap();
    });
  });
}

function renderWeatherMap() {
  if (!weatherMapInstance) return;
  weatherLayers.forEach((layer) => weatherMapInstance.removeLayer(layer));
  weatherLayers = [];

  weatherPoints.forEach((point) => {
    const sample = point.current;
    if (!sample) return;

    const value = currentMapMode === "rain"
      ? sample.precipitation
      : currentMapMode === "wind"
        ? sample.wind
        : currentMapMode === "storm"
          ? sample.storm
          : sample.temperature;

    const markerColor = getMapModeColor(currentMapMode, value);

    const marker = L.circleMarker([point.lat, point.lon], {
      radius: currentMapMode === "storm" && !sample.storm ? 8 : 10 + Math.max(value / 4, 6),
      color: markerColor,
      weight: 2,
      opacity: 0.95,
      fillColor: markerColor,
      fillOpacity: 0.6,
      className: "weather-map-dot",
    });
    const popup = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = point.name;
    const details = document.createElement("div");
    details.textContent = `${sample.condition} · ${sample.temperature} °C · ${sample.precipitation} mm de chuva`;
    popup.append(name, details);
    marker.bindPopup(popup);

    marker.addTo(weatherMapInstance);
    weatherLayers.push(marker);

    const pulse = L.circle([point.lat, point.lon], {
      radius: 170000 + value * 2600,
      color: markerColor,
      weight: 1,
      opacity: 0.4,
      fillOpacity: 0.08,
      dashArray: "8 12",
    });

    pulse.setStyle({ animation: "weather-pulse 3s ease-in-out infinite" });
    pulse.addTo(weatherMapInstance);
    weatherLayers.push(pulse);
  });
}

async function loadGlobalWeather() {
  if (!weatherPoints.length || globalWeatherLoading) return;

  globalWeatherLoading = true;
  mapStatus.textContent = "Atualizando condições meteorológicas...";
  try {
    const response = await fetch("/api/mapa", { cache: "no-store" });
    const results = await response.json();
    if (!response.ok) {
      const error = new Error(results.erro || "Não foi possível atualizar o mapa.");
      error.rateLimit = results.rate_limit;
      throw error;
    }
    const conditionMap = {
      0: "céu limpo",
      1: "predominantemente limpo",
      2: "parcialmente nublado",
      3: "nublado",
      45: "nevoeiro",
      48: "nevoeiro com geada",
      51: "garoa leve",
      53: "garoa moderada",
      55: "garoa intensa",
      61: "chuva leve",
      63: "chuva moderada",
      65: "chuva intensa",
      80: "pancadas de chuva",
      81: "pancadas de chuva moderadas",
      82: "pancadas de chuva fortes",
      95: "trovoada",
      96: "trovoada com granizo",
      99: "trovoada com granizo intenso",
    };
    const failures = results.filter((result) => result.erro).length;
    results.forEach((result) => {
      const point = weatherPoints.find((item) => item.name === result.name);
      if (!point || result.erro) return;
      const current = result.current;
      const weatherCode = Number(current.weather_code);
      point.current = {
        temperature: Math.round(Number(current.temperature_2m)),
        precipitation: Number(current.precipitation),
        wind: Math.round(Number(current.wind_speed_10m)),
        storm: [95, 96, 99].includes(weatherCode),
        condition: conditionMap[weatherCode] || "condição variável",
      };
    });
    renderWeatherMap();
    if (failures > 0) {
      const availablePoints = weatherPoints.filter((point) => point.current).length;
      mapStatus.textContent = availablePoints
        ? `Dados parciais: ${failures} de ${weatherPoints.length} cidades sem atualização. Leituras anteriores foram preservadas.`
        : "Não foi possível carregar os dados meteorológicos.";
    } else {
      mapStatus.textContent = "Dados consultados. Respostas repetidas são reutilizadas pelo cache do servidor por até 10 minutos.";
    }
  } catch (error) {
    mapStatus.textContent = error.rateLimit
      ? `${error.message} Consulte o diagnóstico de limite abaixo.`
      : `${error.message} Leituras anteriores foram preservadas.`;
    if (error.rateLimit) {
      const retry = error.rateLimit.proxima_consulta_disponivel;
      if (retry) {
        const remaining = formatTimeUntil(retry);
        if (remaining) {
          mapStatus.textContent += ` Próxima consulta disponível após ${remaining.duration}, por volta de ${remaining.time}.`;
        }
      } else {
        mapStatus.textContent += " A API não informou quando liberar; use a opção de tentativa manual no diagnóstico.";
      }
    }
  } finally {
    globalWeatherLoading = false;
    await refreshQuotaStatus();
  }
}

function initializeMapWeather() {
  initializeMap();
  updateMapModeButtons();
  if (!weatherMapInstance) {
    mapStatus.textContent = "Não foi possível carregar o mapa. Verifique sua conexão e tente novamente.";
    return;
  }
  void loadGlobalWeather();
}

function rainIntensity(precipitation) {
  if (precipitation >= 20) return { color: "#e53935", label: "chuva extrema" };
  if (precipitation >= 7.5) return { color: "#f28c28", label: "chuva forte" };
  if (precipitation >= 2.5) return { color: "#2584d8", label: "chuva" };
  if (precipitation > 0.1) return { color: "#42a66c", label: "garoa" };
  return { color: "#91b1c4", label: "sem chuva significativa" };
}

function formatRadarCoordinates(latitude, longitude) {
  return `${latitude.toFixed(3)}°, ${longitude.toFixed(3)}°`;
}

function stopRadarAnimation() {
  if (radarAnimationTimer === null) return;
  window.clearInterval(radarAnimationTimer);
  radarAnimationTimer = null;
  radarPlayButton.textContent = "▶ Animar";
  radarPlayButton.setAttribute("aria-pressed", "false");
}

function stopRadarFrameAnimation() {
  if (radarFrameTimer !== null) {
    window.clearInterval(radarFrameTimer);
    radarFrameTimer = null;
  }
  radarLivePlayButton.textContent = "▶ Animar radar";
  radarLivePlayButton.setAttribute("aria-pressed", "false");
}

function startRadarFrameAnimation() {
  if (
    radarDialog.hidden
    || radarLayerMode !== "rain"
    || !radarMap
    || !radarRainLayer
    || radarFrames.length < 2
    || radarFrameTimer !== null
  ) return;
  radarLivePlayButton.textContent = "❚❚ Pausar radar";
  radarLivePlayButton.setAttribute("aria-pressed", "true");
  radarFrameTimer = window.setInterval(() => {
    radarFrameIndex = (radarFrameIndex + 1) % radarFrames.length;
    const frame = radarFrames[radarFrameIndex];
    radarRainLayer.setUrl(
      `${radarRainHost}${frame.path}/256/{z}/{x}/{y}/2/1_1.png`,
    );
  }, 700);
}

function initializeRadarMap() {
  if (!window.L || !radarMapElement) {
    radarStatus.textContent = "O mapa não pôde ser carregado. Verifique sua conexão.";
    return;
  }
  if (radarMap) return;

  radarMap = L.map(radarMapElement, { worldCopyJump: true, minZoom: 2 }).setView([15, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 10,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(radarMap);

  radarPointLayer = L.layerGroup().addTo(radarMap);
  radarMap.on("click", (event) => {
    void loadRadarForecast(event.latlng.lat, event.latlng.lng);
  });
  window.setTimeout(() => radarMap.invalidateSize(), 100);
}

async function loadRainViewerFrames() {
  if (!radarMap || radarRainRequestStarted) return;
  radarRainRequestStarted = true;

  try {
    const response = await fetch(RAINVIEWER_API_URL, { cache: "no-store" });
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      error.retryAt = response.status === 429
        ? retryAfterDate(response.headers.get("Retry-After"))
        : null;
      throw error;
    }
    const data = await response.json();
    const frames = data?.radar?.past;
    if (!Array.isArray(frames) || frames.length === 0 || typeof data.host !== "string") {
      throw new Error("O radar de chuva não está disponível neste momento.");
    }

    stopRadarFrameAnimation();
    radarFrames = frames.filter((item) => (
      typeof item?.path === "string" && Number.isFinite(Number(item.time))
    ));
    if (radarFrames.length === 0) throw new Error("Não há imagens recentes disponíveis.");
    radarRainHost = data.host;
    radarFrameIndex = radarFrames.length - 1;
    const frame = radarFrames[radarFrameIndex];
    const tileUrl = `${data.host}${frame.path}/256/{z}/{x}/{y}/2/1_1.png`;
    if (radarRainLayer) radarMap.removeLayer(radarRainLayer);
    radarRainLayer = L.tileLayer(tileUrl, {
      opacity: 0.72,
      maxZoom: 7,
      maxNativeZoom: 7,
      attribution: '&copy; <a href="https://www.rainviewer.com/">RainViewer</a>',
    });
    radarLivePlayButton.disabled = radarLayerMode !== "rain" || radarFrames.length < 2;
    if (radarLayerMode === "rain") radarRainLayer.addTo(radarMap);
    const observedAt = new Date(frame.time * 1000);
    radarStatus.textContent = `Imagem recente de radar, gerada às ${observedAt.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}. Clique no mapa para consultar a previsão desse local.`;
  } catch (error) {
    const retry = error.retryAt ? formatTimeUntil(error.retryAt) : null;
    const retryMessage = error.status === 429
      ? retry
        ? ` O servidor informou nova tentativa por volta de ${retry.time} (em ${retry.duration}).`
        : " A API não informou quando liberar; não haverá nova tentativa automática. Recarregue a página para tentar manualmente."
      : "";
    radarStatus.textContent = `Radar recente indisponível: ${error.message}.${retryMessage} A previsão no local selecionado continua disponível.`;
  }
}

function renderRadarWeek(data) {
  radarWeek.replaceChildren();
  data.daily.time.forEach((day, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "radar-day";
    button.dataset.date = day;
    const weekday = document.createElement("span");
    weekday.className = "radar-day-name";
    weekday.textContent = index === 0
      ? "Hoje"
      : new Date(`${day}T12:00:00`).toLocaleDateString("pt-BR", { weekday: "short" });
    const icon = document.createElement("span");
    icon.className = "radar-day-icon";
    icon.textContent = iconForCondition(weatherCondition(data.daily.weather_code[index]));
    const temperatures = document.createElement("span");
    temperatures.className = "radar-day-temperatures";
    temperatures.textContent = `${Math.round(data.daily.temperature_2m_max[index])}° / ${Math.round(data.daily.temperature_2m_min[index])}°`;
    button.append(weekday, icon, temperatures);
    button.addEventListener("click", () => selectRadarDay(day));
    radarWeek.append(button);
  });
}

function weatherCondition(code) {
  const conditions = {
    0: "céu limpo",
    1: "predominantemente limpo",
    2: "parcialmente nublado",
    3: "nublado",
    45: "nevoeiro",
    48: "nevoeiro com geada",
    51: "garoa fraca",
    53: "garoa moderada",
    55: "garoa intensa",
    61: "chuva fraca",
    63: "chuva moderada",
    65: "chuva intensa",
    80: "pancadas de chuva",
    81: "pancadas de chuva moderadas",
    82: "pancadas de chuva fortes",
    95: "trovoada",
    96: "trovoada com granizo",
    99: "trovoada com granizo intenso",
  };
  return conditions[code] || "condição variável";
}

function selectedForecastHour() {
  return radarSelectedHours[Number(radarTimeSlider.value)];
}

function updateRadarPoint(hour) {
  if (!radarMap || !hour) return;
  radarPointLayer.clearLayers();
  const precipitation = Number(hour.precipitation ?? 0);
  const wind = Number(hour.wind_speed_10m ?? 0);
  const direction = (Number(hour.wind_direction_10m ?? 0) + 180) % 360;
  const intensity = rainIntensity(precipitation);

  if (radarLayerMode === "rain") {
    L.circleMarker([radarForecastData.latitude, radarForecastData.longitude], {
      radius: precipitation > 0.1 ? 17 : 9,
      color: intensity.color,
      fillColor: intensity.color,
      fillOpacity: precipitation > 0.1 ? 0.62 : 0.2,
      weight: 3,
      className: precipitation > 0.1 ? "radar-rain-pulse" : "",
    }).addTo(radarPointLayer);
  } else {
    const arrow = document.createElement("span");
    arrow.className = "radar-wind-arrow";
    arrow.style.setProperty("--wind-direction", `${direction}deg`);
    arrow.textContent = "➤";
    const icon = L.divIcon({
      className: "radar-wind-icon",
      html: arrow,
      iconSize: [42, 42],
      iconAnchor: [21, 21],
    });
    L.marker([radarForecastData.latitude, radarForecastData.longitude], { icon })
      .addTo(radarPointLayer);
  }

  const localTime = hour.time.slice(11, 16);
  const temperature = Math.round(Number(hour.temperature_2m));
  const detail = radarLayerMode === "rain"
    ? `${intensity.label}: ${precipitation.toFixed(1)} mm · ${temperature} °C · ${wind.toFixed(0)} km/h`
    : `Vento ${wind.toFixed(0)} km/h · rajada ${Number(hour.wind_gusts_10m ?? 0).toFixed(0)} km/h · ${temperature} °C`;
  radarLocationSummary.textContent = `${formatRadarCoordinates(radarForecastData.latitude, radarForecastData.longitude)} · ${localTime} · ${detail}`;

  radarHourly.querySelectorAll(".radar-hour").forEach((card, index) => {
    card.classList.toggle("selected", index === Number(radarTimeSlider.value));
  });
}

function selectRadarDay(day) {
  if (!radarForecastData) return;
  radarSelectedDate = day;
  radarSelectedHours = radarForecastData.hourly.time
    .map((time, index) => ({
      time,
      temperature_2m: radarForecastData.hourly.temperature_2m[index],
      precipitation: radarForecastData.hourly.precipitation[index],
      wind_speed_10m: radarForecastData.hourly.wind_speed_10m[index],
      wind_direction_10m: radarForecastData.hourly.wind_direction_10m[index],
      wind_gusts_10m: radarForecastData.hourly.wind_gusts_10m[index],
    }))
    .filter((hour) => hour.time.startsWith(day) && Number(hour.time.slice(11, 13)) >= 6 && Number(hour.time.slice(11, 13)) <= 22);

  radarWeek.querySelectorAll(".radar-day").forEach((button) => {
    const selected = button.dataset.date === day;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  radarHourlyTitle.textContent = `Manhã à noite · ${new Date(`${day}T12:00:00`).toLocaleDateString("pt-BR", { day: "numeric", month: "long" })}`;
  radarHourly.replaceChildren();
  radarSelectedHours.forEach((hour, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "radar-hour";
    const time = document.createElement("span");
    time.textContent = hour.time.slice(11, 16);
    const rain = document.createElement("span");
    rain.className = "radar-hour-rain";
    const intensity = rainIntensity(Number(hour.precipitation ?? 0));
    rain.style.setProperty("--rain-color", intensity.color);
    rain.textContent = `${Number(hour.precipitation ?? 0).toFixed(1)} mm`;
    const temperature = document.createElement("strong");
    temperature.textContent = `${Math.round(Number(hour.temperature_2m))}°`;
    card.append(time, rain, temperature);
    card.addEventListener("click", () => {
      radarTimeSlider.value = String(index);
      updateRadarPoint(hour);
    });
    radarHourly.append(card);
  });

  if (radarSelectedHours.length === 0) {
    radarHourly.textContent = "Não há horários disponíveis para este dia.";
    radarTimeSlider.disabled = true;
    radarPlayButton.disabled = true;
    stopRadarAnimation();
    return;
  }

  radarTimeSlider.min = "0";
  radarTimeSlider.max = String(radarSelectedHours.length - 1);
  radarTimeSlider.value = "0";
  radarTimeSlider.disabled = false;
  radarPlayButton.disabled = false;
  updateRadarPoint(radarSelectedHours[0]);
}

function renderRadarAlerts(data) {
  if (!Array.isArray(data.hourly.precipitation) || !Array.isArray(data.hourly.wind_gusts_10m)) {
    radarAlertList.textContent = "A fonte de dados não forneceu informação suficiente para os alertas.";
    return;
  }
  const currentTime = data.current?.time || data.hourly.time[0];
  const nextDay = data.hourly.time
    .map((time, index) => ({ time, index }))
    .filter(({ time }) => time >= currentTime)
    .slice(0, 24);
  const alerts = [];

  nextDay.forEach(({ index }) => {
    const precipitation = Number(data.hourly.precipitation[index] ?? 0);
    const gusts = Number(data.hourly.wind_gusts_10m[index] ?? 0);
    const weatherCode = Number(data.hourly.weather_code[index] ?? 0);
    const hour = data.hourly.time[index].slice(11, 16);
    if (precipitation >= 20) {
      alerts.push({ level: "danger", text: `${hour}: precipitação extrema prevista (${precipitation.toFixed(1)} mm/h). Evite áreas alagadas e procure abrigo seguro.` });
    } else if (precipitation >= 7.5) {
      alerts.push({ level: "warning", text: `${hour}: chuva forte prevista (${precipitation.toFixed(1)} mm/h). Reduza a velocidade e evite vias inundadas.` });
    }
    if (gusts >= 80) {
      alerts.push({ level: "danger", text: `${hour}: rajadas muito fortes previstas (${gusts.toFixed(0)} km/h). Afaste-se de árvores e estruturas frágeis.` });
    } else if (gusts >= 60) {
      alerts.push({ level: "warning", text: `${hour}: vento forte previsto (${gusts.toFixed(0)} km/h). Tenha cuidado ao permanecer ao ar livre.` });
    }
    if ([95, 96, 99].includes(weatherCode)) {
      alerts.push({ level: "warning", text: `${hour}: trovoada prevista. Procure abrigo fechado e evite áreas abertas.` });
    }
  });

  radarAlertList.replaceChildren();
  if (alerts.length === 0) {
    radarAlertList.textContent = "Nenhuma condição de chuva intensa, rajadas fortes ou trovoada detectada na previsão das próximas 24 horas. Isto não garante ausência de risco.";
    return;
  }
  alerts.slice(0, 5).forEach((alert) => {
    const item = document.createElement("p");
    item.className = `radar-alert radar-alert-${alert.level}`;
    item.textContent = alert.text;
    radarAlertList.append(item);
  });
}

async function loadRadarForecast(latitude, longitude) {
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return;
  const requestId = ++radarRequestId;
  radarRequestController?.abort();
  radarRequestController = new AbortController();
  radarForecastData = null;
  radarSelectedHours = [];
  radarWeek.replaceChildren();
  radarHourly.replaceChildren();
  radarPointLayer?.clearLayers();
  radarTimeSlider.disabled = true;
  radarPlayButton.disabled = true;
  radarStatus.textContent = "Carregando previsão de sete dias para o ponto selecionado...";
  radarLocationSummary.textContent = `Carregando ${formatRadarCoordinates(latitude, longitude)}...`;
  radarAlertList.textContent = "Consultando as próximas 24 horas...";
  stopRadarAnimation();
  try {
    const parameters = new URLSearchParams({
      latitude: String(latitude),
      longitude: String(longitude),
    });
    const response = await fetch(`/api/radar?${parameters}`, {
      cache: "no-store",
      signal: radarRequestController.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(data.erro || `serviço meteorológico indisponível (HTTP ${response.status}).`);
      error.rateLimit = data.rate_limit;
      throw error;
    }
    const hourlyVariables = [
      "time",
      "temperature_2m",
      "precipitation",
      "wind_speed_10m",
      "wind_direction_10m",
      "wind_gusts_10m",
      "weather_code",
    ];
    const dailyVariables = [
      "time",
      "weather_code",
      "temperature_2m_min",
      "temperature_2m_max",
    ];
    if (
      !data.hourly
      || hourlyVariables.some((variable) => !Array.isArray(data.hourly[variable]))
      || !data.daily
      || dailyVariables.some((variable) => !Array.isArray(data.daily[variable]))
    ) {
      throw new Error("O serviço meteorológico não retornou dados para este local.");
    }
    if (requestId !== radarRequestId) return;

    radarForecastData = { ...data, latitude, longitude };
    radarLocationSummary.textContent = `${formatRadarCoordinates(latitude, longitude)} · ${data.timezone || "fuso horário local"}`;
    radarStatus.textContent = `Previsão para sete dias em ${formatRadarCoordinates(latitude, longitude)}. Clique noutro ponto para atualizar.`;
    renderRadarWeek(data);
    renderRadarAlerts(data);
    selectRadarDay(data.daily.time[0]);
  } catch (error) {
    if (requestId !== radarRequestId) return;
    radarStatus.textContent = error.message;
    if (error.rateLimit) {
      const retry = error.rateLimit.proxima_consulta_disponivel;
      const remaining = retry ? formatTimeUntil(retry) : null;
      radarStatus.textContent += remaining
        ? ` Próxima consulta disponível após ${remaining.duration}, por volta de ${remaining.time}.`
        : " A API não informou quando liberar; use a opção de tentativa manual no diagnóstico.";
    }
    radarAlertList.textContent = "Não foi possível consultar alertas sem os dados da previsão.";
    radarLocationSummary.textContent = formatRadarCoordinates(latitude, longitude);
  } finally {
    if (requestId === radarRequestId) radarRequestController = null;
    await refreshQuotaStatus();
  }
}

function setRadarLayer(mode) {
  radarLayerMode = mode;
  radarLayerButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.radarLayer === mode);
  });
  if (radarRainLayer && radarMap) {
    if (mode === "rain") {
      radarRainLayer.addTo(radarMap);
      radarLivePlayButton.disabled = radarFrames.length < 2;
    } else {
      radarMap.removeLayer(radarRainLayer);
      stopRadarFrameAnimation();
      radarLivePlayButton.disabled = true;
    }
  } else if (mode === "wind") {
    radarLivePlayButton.disabled = true;
  }
  const selectedHour = selectedForecastHour();
  if (selectedHour) updateRadarPoint(selectedHour);
}

function openRadar() {
  radarLastFocusedElement = document.activeElement;
  radarDialog.hidden = false;
  document.body.classList.add("radar-open");
  openRadarButton.setAttribute("aria-expanded", "true");
  closeRadarButton.focus();
  initializeRadarMap();
  if (radarMap) window.setTimeout(() => radarMap.invalidateSize(), 100);
  if (!radarRainLayer) void loadRainViewerFrames();
  if (!radarForecastData) {
    radarStatus.textContent = "Clique no mapa para consultar um local ou use «Usar minha localização».";
  }
}

function closeRadar() {
  radarDialog.hidden = true;
  document.body.classList.remove("radar-open");
  openRadarButton.setAttribute("aria-expanded", "false");
  stopRadarAnimation();
  stopRadarFrameAnimation();
  if (radarLastFocusedElement instanceof HTMLElement) radarLastFocusedElement.focus();
}

openRadarButton.addEventListener("click", openRadar);
closeRadarButton.addEventListener("click", closeRadar);
radarDialog.addEventListener("click", (event) => {
  if (event.target === radarDialog) closeRadar();
});
document.addEventListener("keydown", (event) => {
  if (radarDialog.hidden) return;
  if (event.key === "Escape") {
    closeRadar();
    return;
  }
  if (event.key === "Tab") {
    const focusable = [...radarDialog.querySelectorAll(
      'button:not(:disabled), input:not(:disabled), a[href]',
    )];
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
radarLayerButtons.forEach((button) => {
  button.addEventListener("click", () => setRadarLayer(button.dataset.radarLayer));
});
document.querySelector("#radar-location").addEventListener("click", () => {
  if (!navigator.geolocation) {
    radarStatus.textContent = "Este navegador não oferece geolocalização. Clique no mapa para escolher um local.";
    return;
  }
  radarStatus.textContent = "Aguardando permissão para acessar sua localização...";
  navigator.geolocation.getCurrentPosition(
    ({ coords }) => {
      const { latitude, longitude } = coords;
      if (radarMap) radarMap.setView([latitude, longitude], 7);
      void loadRadarForecast(latitude, longitude);
    },
    (error) => {
      radarStatus.textContent = error.code === error.PERMISSION_DENIED
        ? "A permissão de localização foi negada. Clique no mapa para escolher um local."
        : "Não foi possível obter sua localização. Clique no mapa para escolher um local.";
    },
    { enableHighAccuracy: true, timeout: 15000, maximumAge: 300000 },
  );
});
radarTimeSlider.addEventListener("input", () => {
  const hour = selectedForecastHour();
  if (hour) updateRadarPoint(hour);
});
radarPlayButton.addEventListener("click", () => {
  if (radarAnimationTimer !== null) {
    stopRadarAnimation();
    return;
  }
  if (radarSelectedHours.length < 2) return;
  radarPlayButton.textContent = "❚❚ Pausar";
  radarPlayButton.setAttribute("aria-pressed", "true");
  radarAnimationTimer = window.setInterval(() => {
    const nextHour = (Number(radarTimeSlider.value) + 1) % radarSelectedHours.length;
    radarTimeSlider.value = String(nextHour);
    updateRadarPoint(radarSelectedHours[nextHour]);
  }, 850);
});
radarLivePlayButton.addEventListener("click", () => {
  if (radarFrameTimer !== null) stopRadarFrameAnimation();
  else startRadarFrameAnimation();
});

function localDateString(date) {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

const today = new Date();
dateInput.min = localDateString(today);
const latestDate = new Date(
  today.getFullYear() + 2,
  today.getMonth(),
  today.getDate(),
);
if (latestDate.getMonth() !== today.getMonth()) latestDate.setDate(0);
dateInput.max = localDateString(latestDate);
dateInput.value = dateInput.min;

initializeMapWeather();

function iconForCondition(condition) {
  const normalized = condition.toLocaleLowerCase("pt-BR");
  if (normalized.includes("trovoada")) return "⛈";
  if (normalized.includes("neve")) return "❄";
  if (normalized.includes("chuva") || normalized.includes("garoa")) return "🌧";
  if (normalized.includes("nevoeiro")) return "🌫";
  if (normalized.includes("nublado")) return "☁";
  return "☀";
}

function showForecast(data) {
  document.querySelector("#forecast-city").textContent = data.cidade;
  document.querySelector("#forecast-date-label").textContent = data.data;
  document.querySelector("#condition").textContent = data.condicao;
  document.querySelector("#condition-icon").textContent = iconForCondition(data.condicao);
  document.querySelector("#rain").textContent = data.probabilidade_chuva;
  document.querySelector("#temperature-max").textContent = data.temperatura_maxima;
  document.querySelector("#temperature-min").textContent = data.temperatura_minima;
  document.querySelector("#humidity").textContent = data.umidade;
  document.querySelector("#wind").textContent = data.vento;
  emptyState.hidden = true;
  forecast.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  message.textContent = "";
  forecast.hidden = true;
  emptyState.hidden = true;
  searchButton.disabled = true;
  buttonLabel.textContent = "Buscando previsão...";

  try {
    const response = await fetch("/api/previsao", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cidade: cityInput.value.trim(), data: dateInput.value }),
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.erro || "Não foi possível consultar a previsão.");
      error.rateLimit = result.rate_limit;
      throw error;
    }
    showForecast(result);
  } catch (error) {
    const retry = error.rateLimit?.proxima_consulta_disponivel;
    const remaining = retry ? formatTimeUntil(retry) : null;
    message.textContent = error instanceof TypeError
      ? "Não foi possível conectar ao servidor. Confira sua conexão e tente novamente."
      : `${error.message}${remaining
        ? ` Próxima consulta disponível após ${remaining.duration}, por volta de ${remaining.time}.`
        : error.rateLimit?.tentativa_manual_necessaria
          ? " A API não informou quando liberar; use a opção de tentativa manual no diagnóstico."
          : ""}`;
    emptyState.hidden = false;
  } finally {
    searchButton.disabled = false;
    buttonLabel.textContent = "Consultar previsão";
    await refreshQuotaStatus();
  }
});

void refreshQuotaStatus();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((error) => {
      console.error("Não foi possível registrar o modo instalável:", error);
    });
  });
}
