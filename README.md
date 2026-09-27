# Global Forecast

Aplicativo web mobile-first para consultar a previsão do tempo por cidade e data e acompanhar condições atuais em um mapa global. A interface é servida pelo Python e usa a lógica existente em `weather.py`, que consulta a API Open-Meteo. O programa de terminal em `main.py` continua disponível.

## Radar de chuva e ventos

Use **Radar** no canto superior esquerdo para abrir o painel. O mapa interativo permite selecionar qualquer ponto no mapa ou solicitar a sua localização pelo GPS do navegador. No ponto escolhido, o painel mostra a previsão por hora, de manhã à noite, para sete dias, com temperatura, chuva, vento, reprodução animada e avisos derivados da previsão. A geolocalização exige autorização do navegador e uma ligação HTTPS. As condições atuais do mapa global são carregadas quando a página abre; não há atualização meteorológica automática a cada 10 minutos. Consultas meteorológicas repetidas usam cache temporário no servidor para reduzir chamadas à API.

Quando disponível, a camada global mostra imagens recentes do radar RainViewer; cobertura e atualizações dependem dos radares locais, e essas imagens não são previsões futuras. O marcador e a previsão horária são dados do modelo Open-Meteo para o ponto selecionado. As cores indicam precipitação prevista nesse ponto (garoa, chuva, chuva forte e extrema), não uma medição contínua da superfície. Os avisos são orientativos, não oficiais, não detectam furacões e não substituem a Defesa Civil ou os serviços meteorológicos. Em situações de risco, siga as autoridades locais. A API RainViewer é destinada ao uso pessoal, educacional e comunitário de pequena escala; consulte os [termos de uso](https://www.rainviewer.com/api.html).

## Controle de consultas e limites da API

As consultas Open-Meteo passam pelo backend. O navegador carrega as oito cidades do mapa em uma única solicitação ao backend; quando os dados não estão em cache, o servidor consulta cada cidade separadamente. Cada cidade fica em cache por até 10 minutos. Previsões por cidade/data também ficam em cache por até 10 minutos (incluindo a busca geográfica por até 24 horas); cada ponto do radar fica em cache por até 15 minutos. Consultas simultâneas idênticas compartilham o mesmo pedido. O mapa não atualiza sozinho em intervalos; uma nova carga do mapa ou interação do usuário pode consultar o backend e aproveitar o cache.

O endpoint público `api.open-meteo.com` não usa uma API key. Para uso gratuito não comercial, os [termos da Open-Meteo](https://open-meteo.com/en/terms) publicados atualmente indicam menos de 10.000 chamadas por dia, 5.000 por hora e 600 por minuto. O limite do plano de cliente não está incluído no repositório. A busca de cidades continua usando `geocoding-api.open-meteo.com`; os termos consultados não esclarecem a cota desse endpoint nem identificam claramente o agrupamento exato das cotas (IP, chave ou endpoint). A API não fornece um saldo diário/horário nem um horário de renovação garantidos. Por isso, o diagnóstico não inventa saldo ou renovação: ele mostra os headers de limite quando o servidor os envia e contabiliza as tentativas de chamada iniciadas por esta instância desde a inicialização. Essa contagem em memória pode incluir tentativas falhas, não confirma consumo no provedor e não inclui outros clientes, instâncias ou períodos antes do reinício; não representa a cota total da Open-Meteo.

O servidor reutiliza um executor limitado a quatro trabalhadores para carregar o mapa e compartilha a mesma carga entre solicitações simultâneas ao endpoint `/api/mapa`. Os dados do mapa ainda são armazenados individualmente no cache de cada processo por até 10 minutos. Entradas expiradas são removidas ao acessar o cache ou ao gravar resultados; cache e coordenação não são compartilhados entre processos ou instâncias de hospedagem.

Em uma resposta HTTP 429, o backend respeita `Retry-After` ou headers de reset reconhecidos, bloqueia novas consultas upstream durante o prazo indicado e exibe a previsão de liberação. Se nenhum prazo for informado, ele não repete automaticamente a chamada e oferece uma liberação manual; a próxima consulta ainda poderá receber 429. Não há tentativas progressivas automáticas. Um bloqueio, os contadores e os caches em memória são reiniciados quando a instância do servidor reinicia. O uso gratuito da Open-Meteo é apenas para fins não comerciais; consulte os termos antes de publicar o serviço.

Opcionalmente, clientes com uma assinatura Open-Meteo podem definir a variável de ambiente `OPEN_METEO_API_KEY` **somente no servidor**. Com ela configurada, as chamadas de previsão usam `customer-api.open-meteo.com`; a busca de cidades continua usando a API pública. A chave segue apenas na requisição backend. Configure-a em **Render > serviço > Environment** ou no ambiente do processo local; não adicione a chave ao código, ao frontend ou ao Git. A chave não informa, por si só, o saldo da assinatura.

## Executar no computador

O servidor web é iniciado por `web_app.py` (o `main.py` continua sendo o programa de terminal):

```powershell
cd "C:\caminho\para\web_app.py"
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

Os endpoints de diagnóstico, mapa e radar estão disponíveis em `/api/diagnostico`, `/api/mapa` e `/api/radar?latitude=-8.05&longitude=-34.9`. O cache e a contagem são mantidos na memória do processo; não compare o contador da instância com o saldo global do provedor. Para confirmar os limites atuais, consulte os termos oficiais vinculados acima.

Com o Python do ambiente virtual:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

No celular, consulte uma cidade válida e confira condição, chuva, temperaturas, umidade e vento. Teste datas dentro dos próximos 16 dias, uma data mais distante para conferir o aviso da API e uma data além do limite de dois anos. Reenvie a mesma cidade/data e selecione o mesmo ponto do radar para conferir a reutilização do cache. O botão de consulta impede envios simultâneos; a interface não dispara atualizações meteorológicas automáticas. Para testar 429 sem consumir chamadas reais, simule `HTTPError(429)` nos testes e verifique o header `Retry-After`, o bloqueio e a recuperação manual quando o header não existir. A interface instalada mantém os arquivos da tela disponíveis offline; buscar uma nova previsão continua dependendo de conexão com a internet.
