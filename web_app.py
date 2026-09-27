import argparse
import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from weather import WeatherError, consultar_previsao, validar_data_previsao


WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_REQUEST_BYTES = 8192


class WeatherRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        caminho = urlsplit(self.path).path
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
        if urlsplit(self.path).path != "/api/previsao":
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
        except WeatherError as erro:
            self._responder_json(502, {"erro": str(erro)})
            return

        self._responder_json(200, previsao)

    def _responder_json(self, status, dados):
        conteudo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(conteudo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
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
