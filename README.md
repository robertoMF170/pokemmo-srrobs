# PokeMMO SrRobs — Controller + OCR

> Companion de mesa para PokeMMO: **selector de janelas win32gui**, **WASD sintético** (modo rato opcional), **OCR de chat → SQLite**, **screenshots** em batalha e **snapshot de estado a cada 60s**.

![Tkinter](https://img.shields.io/badge/Tkinter-always--on--top-blue) ![OCR](https://img.shields.io/badge/OCR-pytesseract-green) ![SQLite](https://img.shields.io/badge/SQLite-events.db-black)

## O que faz

- **PokeMMOController** (Tkinter **260×420 always-on-top**): pesquisa de janelas `win32gui`, selector dinâmico e log local.
- **Beta (WASD)**: sintético via `pyautogui` + `win32`, modo **F10** (rato opcional com checkbox de clique), **auto-OCR do chat → `events.db` (kind='chat')**, screenshots automáticos em batalha e snapshot `kind='state'` a cada 60s.
- **Janela “Análise”**: contadores por tipo/origem, batalhas, chat OCR, export **CSV**, pasta `shots/` e config GLM em `config/glm.properties`.
- Integração **GLM-4.6 (Z.AI)** via `glm_api_key()` (env ou `config/glm.properties`) para enriquecer análise; `PokeEvents` capturado de APK.

## Como funciona

```
[ Tkinter 260×420 ] ─► win32gui (EnumWindows PokeMMO) ─► selector
       │                └─► pyautogui + pillow (screenshots shots/)
       ├─► WASD sintético (SCAN, F10 rato) ─► PokeMMO window
       ├─► pytesseract (OCR chat) ─► events.db (kind='chat'/'state', 60s snapshot)
       └─► glm.properties / GLM_API_KEY ─► GLM-4.6 (Z.AI) ─► janela Análise + CSV export
```

## Stack

Python, Tkinter, win32gui, pyautogui, pillow, pytesseract (Tesseract OCR), SQLite, GLM-4.6

## Passo a passo — instalar e correr

```bash
# 1. Clone
git clone https://github.com/robertoMF170/pokemmo-srrobs.git
cd pokemmo-srrobs

# 2. Ambiente
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# Reqs: pyautogui, pillow, pytesseract, pywin32
# + Tesseract OCR instalado no SO (https://github.com/UB-Mannheim/tesseract/wiki)

# 3. Tesseract
# Windows: instala e adiciona ao PATH, ou seta no .py:
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# 4. Configuração GLM (opcional)
# Escolhe um:
# a) Variável de ambiente
set GLM_API_KEY=sk-...
# b) Ficheiro config/glm.properties
# glm_api_key=sk-...

# 5. Correr
python pokemmoversaoalpha.py   # Alpha — base
python pokemmoversaobeta.py    # Beta — WASD + OCR + snapshots

# 6. Dentro da app
# - Escolhe a janela PokeMMO no selector
# - Beta: F10 para modo rato, checkbox clique
# - Botão "Análise" → ver contadores + export CSV
# - Shots em shots/, DB em events.db
```

## Estrutura

```
pokemmoversaoalpha.py      # Alpha — controller base
pokemmoversaobeta.py       # Beta — WASD + OCR + state snapshot (~500 linhas)
config/glm.properties      # glm_api_key
events.db                  # SQLite (kind='chat'/'state')
shots/                     # screenshots automáticos
PokeEvents.apk             # captura de eventos
```

## Avisos SrRobs

- Sempre em cima (`always-on-top`) para não perder a janela.
- OCR depende de Tesseract instalado — sem ele, o chat não é capturado.
- `events.db` e `shots/` estão no `.gitignore` — não sobe dados de sessão.

## Autor

Roberto Marques (SrRobs)
