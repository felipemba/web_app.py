const form = document.querySelector("#weather-form");
const cityInput = document.querySelector("#city");
const dateInput = document.querySelector("#forecast-date");
const searchButton = document.querySelector("#search-button");
const buttonLabel = searchButton.querySelector(".button-label");
const message = document.querySelector("#form-message");
const emptyState = document.querySelector("#empty-state");
const forecast = document.querySelector("#forecast");
const mapButtons = [...document.querySelectorAll(".map-mode")];
const mapStatus = document.querySelector("#map-status");

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
  if (!weatherPoints.length) return;

  mapStatus.textContent = "Atualizando condições meteorológicas...";
  const results = await Promise.allSettled(
    weatherPoints.map(async (point) => {
      const url = `https://api.open-meteo.com/v1/forecast?latitude=${point.lat}&longitude=${point.lon}&current=temperature_2m,precipitation,wind_speed_10m,weather_code&timezone=auto&forecast_days=1`;
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const current = data.current;
      if (!current || ![
        current.temperature_2m,
        current.precipitation,
        current.wind_speed_10m,
        current.weather_code,
      ].every((value) => Number.isFinite(Number(value)))) {
        throw new Error("Resposta meteorológica inválida.");
      }

      const weatherCode = Number(current.weather_code);
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

      point.current = {
        temperature: Math.round(Number(current.temperature_2m)),
        precipitation: Number(current.precipitation),
        wind: Math.round(Number(current.wind_speed_10m)),
        storm: [95, 96, 99].includes(weatherCode),
        condition: conditionMap[weatherCode] || "condição variável",
      };
    })
  );

  const failures = results.filter((result) => result.status === "rejected").length;
  const availablePoints = weatherPoints.filter((point) => point.current).length;
  renderWeatherMap();
  if (failures === weatherPoints.length) {
    mapStatus.textContent = availablePoints
      ? "Falha na atualização. Exibindo as últimas leituras disponíveis."
      : "Não foi possível carregar os dados meteorológicos. Verifique sua conexão e tente novamente.";
  } else if (failures > 0) {
    mapStatus.textContent = `Dados parciais: ${failures} de ${weatherPoints.length} cidades sem atualização. Leituras anteriores foram preservadas.`;
  } else {
    mapStatus.textContent = `Dados atualizados às ${new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" })}.`;
  }
}

function initializeMapWeather() {
  initializeMap();
  updateMapModeButtons();
  if (!weatherMapInstance) {
    mapStatus.textContent = "Não foi possível carregar o mapa. Verifique sua conexão e tente novamente.";
    return;
  }
  const refresh = async () => {
    await loadGlobalWeather();
    setTimeout(refresh, 120000);
  };
  refresh();
}

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
    if (!response.ok) throw new Error(result.erro || "Não foi possível consultar a previsão.");
    showForecast(result);
  } catch (error) {
    message.textContent = error instanceof TypeError
      ? "Não foi possível conectar ao servidor. Confira sua conexão e tente novamente."
      : error.message;
    emptyState.hidden = false;
  } finally {
    searchButton.disabled = false;
    buttonLabel.textContent = "Consultar previsão";
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((error) => {
      console.error("Não foi possível registrar o modo instalável:", error);
    });
  });
}
