# Global Forecast

Aplicativo web mobile-first para consultar a previsão do tempo por cidade e data e acompanhar condições atuais em um mapa global. A interface é servida pelo Python e usa a lógica existente em `weather.py`, que consulta a API Open-Meteo. O programa de terminal em `main.py` continua disponível.

## Radar de chuva e ventos

Use **Radar** no canto superior esquerdo para abrir o painel. O mapa interativo permite selecionar qualquer ponto no mapa ou solicitar a sua localização pelo GPS do navegador. No ponto escolhido, o painel mostra a previsão por hora, de manhã à noite, para sete dias, com temperatura, chuva, vento, reprodução animada e avisos derivados da previsão. A geolocalização exige autorização do navegador e uma ligação HTTPS.

Quando disponível, a camada global mostra imagens recentes do radar RainViewer; cobertura e atualizações dependem dos radares locais, e essas imagens não são previsões futuras. O marcador e a previsão horária são dados do modelo Open-Meteo para o ponto selecionado. As cores indicam precipitação prevista nesse ponto (garoa, chuva, chuva forte e extrema), não uma medição contínua da superfície. Os avisos são orientativos, não oficiais, não detectam furacões e não substituem a Defesa Civil ou os serviços meteorológicos. Em situações de risco, siga as autoridades locais. A API RainViewer é destinada ao uso pessoal, educacional e comunitário de pequena escala; consulte os [termos de uso](https://www.rainviewer.com/api.html).

## Executar no computador

O servidor web é iniciado por `web_app.py` (o `main.py` continua sendo o programa de terminal):

```powershell
cd "C:\VS pyton"
.\.venv\Scripts\python.exe web_app.py
```

Abra <http://localhost:8000>. Para usar o menu de terminal, execute `.\.venv\Scripts\python.exe main.py`.

## Abrir no celular pela rede Wi-Fi

1. No computador, execute `.\.venv\Scripts\python.exe web_app.py --host 0.0.0.0 --port 8000`.
2. No Windows, rode `ipconfig` e anote o endereço IPv4 da conexão Wi-Fi (por exemplo, `192.168.1.25`).
3. Com o celular na mesma rede, abra `http://192.168.1.25:8000` no navegador. Se o Windows pedir, permita o acesso à rede privada no firewall.

O servidor precisa continuar aberto no computador; a consulta da previsão também precisa de internet. É possível escolher datas de hoje até dois anos à frente, mas previsões meteorológicas reais estão disponíveis somente para os próximos 16 dias. Para datas mais distantes, o site explica essa limitação sem inventar resultados. O endereço de rede local em HTTP permite testar a tela no celular, mas navegadores Android exigem HTTPS para oferecer a instalação de um PWA. Para instalar, publique o app em um servidor com HTTPS e abra esse endereço no Chrome para Android; escolha **Instalar app** ou **Adicionar à tela inicial** no menu do navegador. O app abre em tela própria, com o nome e o ícone definidos no manifesto.

## Publicar para acesso pela internet

O endereço `localhost` e o IP do Wi-Fi só funcionam no computador ou na mesma rede local. Para que qualquer pessoa acesse de outra rede, publique o projeto num serviço de hospedagem web. O arquivo `render.yaml` configura uma implantação no Render: o serviço fornece o link público HTTPS e a porta de rede; o aplicativo usa automaticamente a variável `PORT` da hospedagem. O site não fica publicado nem ganha um link até a implantação ser concluída.

1. Envie a pasta do projeto para um repositório GitHub. Inclua `web_app.py`, `weather.py`, a pasta `web` e `render.yaml`.
2. No Render, crie uma conta, escolha **New > Blueprint** e conecte o repositório. Confirme a criação do serviço `global-forecast` definido em `render.yaml`.
3. Aguarde a implantação concluir. Na página do serviço, abra o endereço público `*.onrender.com`; esse é o link para compartilhar e acessar em qualquer rede.

Com hospedagem pública, o computador pessoal e o VS Code não precisam ficar ligados. A disponibilidade, o domínio e eventuais limites dependem do plano do serviço de hospedagem. Não publique dados privados no site: qualquer pessoa com o endereço poderá consultar a aplicação.

## Testar

Com o Python do ambiente virtual:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

No celular, consulte uma cidade válida e confira condição, chuva, temperaturas, umidade e vento. Teste datas dentro dos próximos 16 dias, uma data mais distante para conferir o aviso da API e uma data além do limite de dois anos. A interface instalada mantém os arquivos da tela disponíveis offline; buscar uma nova previsão continua dependendo de conexão com a internet.
