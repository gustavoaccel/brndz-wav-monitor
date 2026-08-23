# brndz.wav Monitor

Monitor para técnico de AV/eventos: analisador de espectro de áudio ao vivo
(loopback do sistema) + CPU/RAM/GPU/disco/rede/processos, numa janela dark
separada. Windows apenas.

## Instalação

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

`pynvml` só funciona com GPU NVIDIA — em outras GPUs o painel mostra
"indisponível" sem quebrar nada.

## Uso

```bash
python main.py
```

Abre e já começa a monitorar sozinho. Fecha a janela, aperta ESC ou clica em
"Parar & Salvar" pra encerrar — ao encerrar, gera um resumo em Markdown ao
lado do CSV completo.

### Configuração

Copie `config.example.json` para `config.json` (mesma pasta do `main.py`)
e ajuste. Também dá pra passar tudo via linha de comando:

```bash
python main.py --ping-target 8.8.8.8 --process obs64 --process vMix64 --ram-alert-mb 3072 --eq-bands 24
```

Veja `python main.py --help` pra lista completa.

### Gerar um .exe único

```bash
pip install pyinstaller
pyinstaller brndzwav_monitor.spec
```

Gera `dist/brndz.wav Monitor.exe` (arquivo único, ~27MB, sem precisar de Python
instalado na máquina que for rodar). Se der crash, aparece uma janela com o
traceback em vez de simplesmente fechar — é assim que dá pra descobrir o
que houve mesmo sem console.

### Onde os logs vão parar

Procura um HD com o volume nomeado `brndz.wav` e salva em
`AV_TOOLKIT/07_DOCUMENTATION/Logs_Evento/` nele. Se não achar, cai num
fallback local em `Documentos/AV_Monitor_Logs/`.

## Estrutura

```
main.py                     loop principal (pygame) + wiring dos threads
network_mapper.py           janela separada: scan de rede (botão "Mapear Rede")
avmonitor/
  config.py                 defaults + config.json + CLI args
  audio_spectrum.py         captura WASAPI loopback + FFT + bandas log
  system_stats.py           CPU/RAM/GPU(NVIDIA)/disco/top processos
  network_monitor.py        ping + taxa de perda em janela deslizante
  network_scan.py           ping sweep /24 + hostname/MAC + detecção de câmera
  process_watch.py          crash/hang/RAM de processos monitorados
  session_log.py            CSV + resumo em Markdown + detecção de HD
  win_native.py              ctypes: label de volume, IsHungAppWindow
  util.py                   helpers de fila thread-safe
  ui/
    renderer.py              desenho do EQ + painéis (pygame)
    theme.py                 cores/fontes do dark theme
```

Cada fonte de dados roda na sua própria thread e publica numa
`queue.Queue`; o loop de renderização nunca bloqueia esperando ping, WMI ou
leitura de áudio.

## Mapear Rede

Botão dentro do painel REDE. Abre uma **janela separada** (processo
próprio — `brndz.wav Monitor.exe --network-map` por baixo dos panos) que
varre a rede local (`/24`, baseado no seu IP atual), lista IP, hostname,
MAC e sonda um punhado de portas relevantes pra AV (80/8080/8000 web,
554 RTSP, 5678 VISCA, 23 Telnet). Linhas destacadas na cor da marca =
provável câmera/PTZ (nome do host ou porta RTSP/VISCA aberta). Botão
"Salvar mapa (.md)" grava no mesmo HD/pasta que o `network_scan.ps1` do
toolkit já usava (`AV_TOOLKIT/07_DOCUMENTATION/Mapas_de_Rede/`), então os
dois convivem sem conflito.

## Marca d'água do EQ

Fonte customizada opcional: qualquer `.ttf`/`.otf` colocado em
`avmonitor/ui/fonts/` é usado automaticamente pro texto "brndz.wav" atrás
das barras (cai de volta pra fonte padrão se a pasta estiver vazia). Hoje
está usando a fonte **Hunters** — atenção: a licença gratuita dela no
dafont é **só para uso pessoal**, não para uso comercial. Se for usar isso
profissionalmente, considere comprar a licença comercial (link vem no
próprio zip da fonte) — isso não tem nada a ver com o código, é decisão
sobre a fonte em si.

## Notas de implementação

- **Cor das barras do EQ**: por amplitude (vinho escuro → dourado → laranja
  → vermelho = perto de "clipar"), não por frequência — mais acionável de
  bater o olho durante um evento ao vivo, e no tom da marca. Ver
  `avmonitor/ui/theme.py` e `renderer.py`.
- **Detecção de hang**: usa `IsHungAppWindow` via ctypes em vez de
  `pywin32`, pra não puxar uma dependência a mais (o app já é Windows-only).
- **Ping**: `ping.exe` do Windows via `subprocess`, não socket ICMP cru —
  evita precisar rodar como admin.
- **Volume master no EQ**: o loopback do WASAPI captura o áudio *antes* do
  ganho do volume master ser aplicado — abaixar/mutar o volume geral do
  Windows não muda nada no que o loopback entrega (só o volume de cada app
  individualmente é refletido). `avmonitor/master_volume.py` lê o volume
  master real via COM (`pycaw`) e escala a captura manualmente por isso.
- **Troca de dispositivo de saída**: o `AudioSpectrumAnalyzer` confere a
  cada `audio_device_poll_s` (padrão 1.5s) se o dispositivo de saída padrão
  do Windows mudou (ex: caixas → headset) e reabre o stream de loopback
  automaticamente nesse caso, fechando o anterior sem vazar handle. O
  "padrão atual" é lido via `pycaw`/COM (`AudioUtilities.GetSpeakers()`),
  não pela própria enumeração do `pyaudiowpatch` — essa ficava presa no
  dispositivo que era padrão quando o host WASAPI foi inicializado e nunca
  detectava a troca sozinha.
- **Seletor de saída manual**: botão "Saída" no topo lista todos os
  dispositivos com loopback disponível; escolher um fixa a captura nele
  (ignora o padrão do Windows até voltar pra "Automático").
- **Leitura de stream com timeout**: `stream.read()` do PortAudio não tem
  timeout nativo e foi observado travando *para sempre* contra um
  dispositivo sem sessão de áudio ativa (ex: a saída "Chat" de um headset,
  sem nada tocando nela). A captura agora faz polling em
  `get_read_available()` com um timeout de 3s antes de desistir e tentar
  outro dispositivo, em vez de bloquear a thread indefinidamente.
- **Modo compacto**: janela sem moldura (`pygame.NOFRAME`) com transparência
  por colorkey (`WS_EX_LAYERED` + `SetLayeredWindowAttributes`, tudo via
  ctypes em `win_native.py` — mesma decisão de evitar `pywin32` do resto do
  projeto). Arrastar sem barra de título usa `GetCursorPos`/`SetWindowPos`
  diretamente. Sempre tem um botão "×" visível pra voltar ao modo normal,
  além do Esc.
