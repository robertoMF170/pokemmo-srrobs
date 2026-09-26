# PokeMMO SrRobs - Versao Beta 4
#
# Novidades v4:
#  - MODO RATO (F10): anda SO com as teclas WASD sinteticas (o clique na
#    direcao e agora OPCIONAL - checkbox "Tambem clicar p/ andar"; por
#    defeito OFF para nao clicar em NPCs a troco de nada)
#  - AUTO-OCR DO CHAT: loop que captura o chat do jogo e guarda no SQLite
#    (kind='chat', source='ocr') - precisa de pillow + pytesseract
#  - SCREENSHOTS AUTOMATICOS: quando o log deteta batalha, grava um PNG
#    da janela do jogo em D:\pokemmo\shots (materia p/ template matching)
#  - SNAPSHOT DE ESTADO: a cada 60s grava um evento 'state' (modos ativos,
#    zona do rato, titulo da janela) para reconstruir a sessao depois
#  - JANELA "ANALISE": estatisticas da BD (eventos por tipo/origem,
#    batalhas, chat OCR, screenshots) para vermos o que ha para explorar
#
# Novidades v3:

import ctypes
import ctypes.wintypes
import json
import logging
import math
import os
import queue
import sqlite3
import subprocess
import threading
import time
import urllib.request
import urllib.error
import http.server
import socket
import tkinter as tk
from tkinter import scrolledtext
import win32gui

user32 = ctypes.windll.user32

# ---------------- Paths ----------------
BASE_DIR = r"D:\pokemmo"
GAME_DIR = os.path.join(BASE_DIR, "PokeMMO")
LOG_FILE = os.path.join(BASE_DIR, "debug.txt")
DB_FILE = os.path.join(BASE_DIR, "events.db")
GAME_LOG = os.path.join(GAME_DIR, "log", "console.log")
GAME_MODS_LOG = os.path.join(GAME_DIR, "log", "mods.log")
CSV_EXPORT = os.path.join(BASE_DIR, "events_export.csv")
SHOTS_DIR = os.path.join(BASE_DIR, "shots")
GLM_PROPERTIES = os.path.join(BASE_DIR, "config", "glm.properties")
GLM_URL = "https://api.z.ai/api/paas/v4/chat/completions"
GLM_MODEL = "glm-4.6"


def glm_api_key():
    key = os.environ.get("GLM_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(GLM_PROPERTIES, "r", encoding="utf-8") as f:
            for ln in f:
                if ln.strip().startswith("key="):
                    k = ln.split("=", 1)[1].strip()
                    if k:
                        return k
    except Exception:
        pass
    return ""

# ---------------- Log (ficheiro + widget GUI + BD) ----------------
GUI_LOG_QUEUE = queue.Queue()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("PokeMMO")


# ---------------- BD ----------------
class EventDB:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        with self.lock:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT
                )"""
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
            self.conn.commit()

    def insert(self, source, kind, message, ts=None):
        ts = ts or time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT INTO events (ts, source, kind, message) VALUES (?,?,?,?)",
                    (ts, source, kind, message[:2000]),
                )
                self.conn.commit()
        except Exception:
            logger.exception("falha a escrever na BD")

    def exists(self, ts, kind, message):
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM events WHERE ts=? AND kind=? AND message=? LIMIT 1",
                (ts, kind, message[:2000]),
            ).fetchone()
        return row is not None

    def count(self):
        with self.lock:
            (n,) = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return n

    def export_csv(self, path):
        import pandas as pd

        with self.lock:
            df = pd.read_sql_query("SELECT * FROM events ORDER BY id", self.conn)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return len(df)

    def stats(self):
        with self.lock:
            kinds = self.conn.execute(
                "SELECT kind, COUNT(*) c FROM events GROUP BY kind ORDER BY c DESC"
            ).fetchall()
            srcs = self.conn.execute(
                "SELECT source, COUNT(*) c FROM events GROUP BY source ORDER BY c DESC"
            ).fetchall()
        return kinds, srcs

    def recent(self, kind, n=20):
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, message FROM events WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, n),
            ).fetchall()
        return rows[::-1]


db = EventDB(DB_FILE)


def log(msg, level=logging.INFO, kind="info", source="panel", gui=True, db_save=True):
    logger.log(level, msg)
    if gui:
        GUI_LOG_QUEUE.put((time.strftime("%H:%M:%S"), kind, msg))
    if db_save:
        db.insert(source, kind, msg)


# ---------------- Constantes de movimento ----------------
VK = {"w": 0x57, "a": 0x41, "s": 0x53, "d": 0x44}
SC = {"w": 0x11, "a": 0x1E, "s": 0x1F, "d": 0x20}  # scancodes (DIK)
DIRS = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}
VK_F9 = 0x78
VK_F10 = 0x79
VK_RBUTTON = 0x02
BTN = {
    "left": (0x0002, 0x0004),   # LEFTDOWN, LEFTUP
    "right": (0x0008, 0x0010),  # RIGHTDOWN, RIGHTUP
}

# ---------------- Teclado sintetico (SendInput por scancode) ----------------
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1


class _KI(ctypes.Structure):
    _fields_ = (("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = (("ki", _KI), ("pad", ctypes.c_ubyte * 64))
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_ulong), ("u", _U))


def send_key(scancode, vk, up=False, mode="scan"):
    """mode='scan' -> KEYEVENTF_SCANCODE (DIK), mode='vk' -> virtual key.
    Alguns clientes Java so reagem a uma das variantes (ver Teste WASD)."""
    inp = _IN()
    inp.type = INPUT_KEYBOARD
    if mode == "vk":
        inp.ki.wVk = vk
    else:
        inp.ki.wScan = scancode
        inp.ki.dwFlags = KEYEVENTF_SCANCODE
    if up:
        inp.ki.dwFlags |= KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_IN))


# ---------------- Cores (dark mode) ----------------
BG = "#17171c"
CARD = "#22222c"
CARD2 = "#2a2a36"
FG = "#e8e8f0"
DIM = "#8b8b9e"
ACCENT = "#7aa2f7"
OK = "#4ade80"
WARN = "#fbbf24"
ERR = "#f87171"
BATTLE = "#c084fc"

KIND_COLORS = {
    "battle": BATTLE,
    "logout": ERR,
    "warn": WARN,
    "focus": ACCENT,
    "movement": "#9ecbff",
    "chat": "#86efac",
    "glm": "#f0abfc",
    "phone": "#67e8f9",
    "title": OK,
}


# ---------------- Input FISICO (hooks LL) ----------------
# GetAsyncKeyState mistura input fisico com o sintetico que NOS enviamos
# (era a causa da "metralhadora" Mover/Parar no log: o nosso clique direito
# sintetico desligava o gatilho direito e vice-versa, a cada 16ms).
# Estes hooks seguem SO o input fisico: eventos injetados sao ignorados
# pela flag LLMHF_INJECTED / LLKHF_INJECTED.
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01


class _MSLL(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class _KBDLL(ctypes.Structure):
    _fields_ = (("vkCode", ctypes.c_ulong), ("scanCode", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)))


class PhysicalInputTracker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.keys = set()                      # VKs fisicos premidos
        self.buttons = {"left": False, "right": False}
        self.ok = False
        self._mouse_proc = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p)(self._on_mouse)
        self._kbd_proc = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p)(self._on_kbd)

    def _on_mouse(self, ncode, wparam, lparam):
        if ncode == 0:
            try:
                info = ctypes.cast(lparam, ctypes.POINTER(_MSLL)).contents
                if not info.flags & LLMHF_INJECTED:
                    if wparam == WM_LBUTTONDOWN:
                        self.buttons["left"] = True
                    elif wparam == WM_LBUTTONUP:
                        self.buttons["left"] = False
                    elif wparam == WM_RBUTTONDOWN:
                        self.buttons["right"] = True
                    elif wparam == WM_RBUTTONUP:
                        self.buttons["right"] = False
            except Exception:
                pass
        return user32.CallNextHookEx(0, ncode, wparam, lparam)

    def _on_kbd(self, ncode, wparam, lparam):
        if ncode == 0 and wparam in (WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP):
            try:
                info = ctypes.cast(lparam, ctypes.POINTER(_KBDLL)).contents
                if not info.flags & LLKHF_INJECTED:
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        self.keys.add(info.vkCode)
                    else:
                        self.keys.discard(info.vkCode)
            except Exception:
                pass
        return user32.CallNextHookEx(0, ncode, wparam, lparam)

    def run(self):
        kbd = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kbd_proc, None, 0)
        mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
        if not kbd or not mouse:
            log("Hooks LL falhou - fallback GetAsyncKeyState (pode haver eco sintetico)",
                logging.WARNING)
            return
        self.ok = True
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass


# ---------------- Motor de movimento (thread) ----------------
class MovementEngine(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.enabled = True          # F9 (liga/desliga tudo)
        self.virtual = {k: False for k in VK}  # d-pad (toggle)
        self.holding = False
        self._last_pt = None
        self._prev_f9 = False
        self._prev_f10 = False
        self._last_dir_txt = ""
        self._synth_keys = set()     # teclas sinteticas actualmente enviadas
        self._hold_button = "left"   # botao usado no clique em curso
        self._last_click_down = 0.0  # anti-metralhadora (min 250ms entre cliques)
        self.zone_txt = "-"          # zona atual do rato (p/ UI)
        self.mouse_active = False    # modo rato a mover (p/ UI)
        self.diag = (0, 0, 0)        # (foco_jogo, cursor_dentro, botao_gatilho)

    def held_keys_physical(self):
        t = getattr(self.app, "tracker", None)
        if t is not None and t.ok:
            return [k for k, vk in VK.items() if vk in t.keys]
        return [k for k, vk in VK.items() if user32.GetAsyncKeyState(vk) & 0x8000]

    def _button_state(self, name):
        t = getattr(self.app, "tracker", None)
        if t is not None and t.ok:
            return t.buttons[name]
        vk = 0x02 if name == "right" else 0x01  # VK_RBUTTON / VK_LBUTTON
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("tick do motor falhou")
            time.sleep(0.016)

    # -- cliques --
    def _click_down(self, button):
        down, _ = BTN[button]
        user32.mouse_event(down, 0, 0, 0, 0)

    def _click_up(self, button):
        _, up = BTN[button]
        user32.mouse_event(up, 0, 0, 0, 0)

    # -- teclas sinteticas --
    def _update_synth(self, keys, send):
        want = set(keys) if send else set()
        mode = getattr(self.app, "key_mode", "scan")
        for k in want - self._synth_keys:
            send_key(SC[k], VK[k], up=False, mode=mode)
        for k in self._synth_keys - want:
            send_key(SC[k], VK[k], up=True, mode=mode)
        self._synth_keys = want

    def tick(self):
        app = self.app

        # ---- atalhos globais ----
        f9 = bool(user32.GetAsyncKeyState(VK_F9) & 0x8000)
        if f9 and not self._prev_f9:
            self.enabled = not self.enabled
            self.release_all()
            log(f"Modo WASD: {'ATIVADO' if self.enabled else 'DESATIVADO'} (F9)", kind="movement")
            app._dirty_ui = True
        self._prev_f9 = f9

        f10 = bool(user32.GetAsyncKeyState(VK_F10) & 0x8000)
        if f10 and not self._prev_f10:
            app.mouse_mode = not app.mouse_mode
            self.release_all()
            log(f"Modo RATO por zonas: {'ATIVADO' if app.mouse_mode else 'DESATIVADO'} (F10)", kind="movement")
            app._dirty_ui = True
        self._prev_f10 = f10

        if app.game_hwnd is None or not self.enabled:
            self.zone_txt = "-"
            self.mouse_active = False
            self.release_all()
            return

        game_fg = user32.GetForegroundWindow() == app.game_hwnd
        from_buttons = any(self.virtual.values())

        # ---- recolher direcoes ----
        keys = set()
        if from_buttons:
            keys.update(k for k, v in self.virtual.items() if v)
            # d-pad: garantir que o jogo recebe o clique (best-effort)
            if not game_fg:
                try:
                    win32gui.SetForegroundWindow(app.game_hwnd)
                    game_fg = True
                except Exception:
                    pass
        if game_fg:
            keys.update(self.held_keys_physical())

        # ---- modo rato por zonas ----
        inside = False
        if app.mouse_trigger == "none":
            trigger = True
        else:
            trigger = self._button_state(app.mouse_trigger)
        send_synth = False
        if app.mouse_mode:
            if game_fg:
                zone_keys, inside = app.cursor_zone_keys()
                if inside and trigger:
                    keys.update(zone_keys)
                    send_synth = True
                    self.mouse_active = True
                else:
                    self.mouse_active = False
            else:
                self.mouse_active = False
        else:
            self.mouse_active = False
        self.diag = (1 if game_fg else 0, 1 if inside else 0, 1 if trigger else 0)
        self.zone_txt = "+".join(sorted(k.upper() for k in keys)) if keys else "-"

        if not keys:
            self.release_all()
            self._update_synth([], False)
            return

        # ---- teclas WASD sinteticas para o cliente ----
        if send_synth and app.send_keys:
            self._update_synth(sorted(keys), True)
        else:
            self._update_synth([], False)

        # ---- clique p/ andar: SO por opcao (checkbox), teclado e rato ----
        # o clique pode interagir com NPCs/portas, por isso e opt-in
        if not app.mouse_click_move:
            self.release_all()
            return

        vx = sum(DIRS[k][0] for k in keys)
        vy = sum(DIRS[k][1] for k in keys)
        length = math.hypot(vx, vy)
        if length == 0:
            self.release_all()
            return

        center = app.client_center()
        if center is None:
            return
        dist = app.click_distance
        tx = int(center[0] + vx / length * dist)
        ty = int(center[1] + vy / length * dist)

        # REGRA DE OURO: o botao do clique NUNCA pode ser o do gatilho.
        # O clique sintetico desligaria o gatilho fisico -> metralhadora.
        button = app.move_button
        if app.mouse_mode and app.mouse_trigger != "none" and button == app.mouse_trigger:
            button = "right" if button == "left" else "left"

        dir_txt = "+".join(sorted(keys))
        if not self.holding:
            now = time.perf_counter()
            if now - self._last_click_down < 0.25:   # anti-metralhadora
                return
            self._last_click_down = now
            user32.SetCursorPos(tx, ty)
            self._hold_button = button
            self._click_down(button)
            self.holding = True
            self._last_dir_txt = dir_txt
            log(f"Mover [{dir_txt}] clique {button} em ({tx},{ty})", kind="movement", level=logging.DEBUG)
        elif dir_txt != self._last_dir_txt:
            # o jogo fixa o destino no momento do clique -> re-clique
            self._click_up(button)
            user32.SetCursorPos(tx, ty)
            self._click_down(button)
            self._last_dir_txt = dir_txt
            log(f"Direcao {dir_txt} (re-clique)", kind="movement", level=logging.DEBUG)
        elif (tx, ty) != self._last_pt:
            user32.SetCursorPos(tx, ty)
        self._last_pt = (tx, ty)

    def release_all(self):
        if self.holding:
            self._click_up(self._hold_button)
            self.holding = False
            self._last_pt = None
            log("Parar: botao do rato largado", kind="movement", level=logging.DEBUG)
        self._update_synth([], False)


# ---------------- Tail do log do cliente do jogo ----------------
class GameLogTailer(threading.Thread):
    def __init__(self, path, panel=None):
        super().__init__(daemon=True)
        self.path = path
        self.panel = panel
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0

    @staticmethod
    def classify(line):
        low = line.lower()
        if "monster-slot" in low or "battle.ttf" in low:
            return "battle"
        if "networking shutdown" in low:
            return "logout"
        if "saved config" in low:
            return "config"
        if low.startswith("[warn"):
            return "warn"
        return "client"

    def run(self):
        while True:
            try:
                if os.path.exists(self.path):
                    size = os.path.getsize(self.path)
                    if size < self.pos:
                        self.pos = 0
                    if size > self.pos:
                        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(self.pos)
                            chunk = f.read()
                            self.pos = f.tell()
                        for raw in chunk.splitlines():
                            line = raw.strip()
                            if not line:
                                continue
                            kind = self.classify(line)
                            if kind == "battle":
                                log("[JOGO] UI de batalha detetado!", kind="battle", source="game")
                                db.insert("game", "battle_line", line)
                                if self.panel is not None:
                                    try:
                                        self.panel.on_battle_event(line)
                                    except Exception:
                                        pass
                            elif kind == "logout":
                                log("[JOGO] Cliente desligou a rede (logout/fecho)", kind="logout", source="game")
                                db.insert("game", "logout_line", line)
                            elif kind == "warn":
                                log(f"[JOGO] aviso: {line[:120]}", kind="warn", source="game", level=logging.WARNING)
                            else:
                                db.insert("game", "clientlog", line)
            except Exception:
                logger.exception("tail falhou")
            time.sleep(0.5)


# ---------------- Auto-OCR do chat (loop, so com o jogo em 1o plano) ----------------
class ChatOCRTailer(threading.Thread):
    def __init__(self, panel, interval=5.0):
        super().__init__(daemon=True)
        self.panel = panel
        self.interval = interval
        self._recent = []      # ultimas linhas vistas (dedupe)
        self._warned = False

    def _chat_bbox(self):
        hwnd = self.panel.game_hwnd
        if not hwnd or not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return None
        try:
            l, t, r, b = win32gui.GetClientRect(hwnd)
            ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
            w, h = r - l, b - t
            if w < 100 or h < 100:
                return None
            # faixa inferior-esquerda da janela (onde o chat vive por defeito)
            return (ox + 4, oy + int(h * 0.70), ox + int(w * 0.58), oy + h - 6)
        except Exception:
            return None

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(self.interval)

    def tick(self):
        if not self.panel.game_hwnd:
            return
        if user32.GetForegroundWindow() != self.panel.game_hwnd:
            return
        bbox = self._chat_bbox()
        if not bbox:
            return
        try:
            from PIL import ImageGrab
            import pytesseract
        except ImportError:
            if not self._warned:
                self._warned = True
                log("Auto-OCR do chat INATIVO: pip install pillow pytesseract "
                    "+ instalar o Tesseract OCR", logging.WARNING)
            return
        try:
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
            text = pytesseract.image_to_string(img, lang="eng")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            new = [ln for ln in lines if ln not in self._recent]
            if new:
                for ln in new:
                    db.insert("ocr", "chat", ln)
                self._recent = (self._recent + new)[-40:]
                extra = f" (+{len(new) - 1})" if len(new) > 1 else ""
                log(f"[OCR] {new[0][:80]}{extra}", kind="chat")
        except Exception:
            pass


# ---------------- Sync: receber eventos/screenshots do telemovel ----------------
_sync_state = {"last": 0.0, "pending": 0}


def _sync_received():
    """Mostra 'synced + total' no log (agrupado: no max 1 linha a cada 5s)."""
    _sync_state["pending"] += 1
    now = time.time()
    if now - _sync_state["last"] >= 5.0:
        _sync_state["last"] = now
        n = _sync_state["pending"]
        _sync_state["pending"] = 0
        log(f"synced (telemovel: +{n}) | total na BD: {db.count()}", kind="phone", gui=True, db_save=False)


class PhoneSyncHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _ok(self, body=b"ok"):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n)
            if self.path == "/event":
                ts, kind, msg = data.decode("utf-8", "replace").split("\t", 2)
                if not db.exists(ts, kind, msg):
                    db.insert("phone", kind, msg[:2000], ts=ts)
                _sync_received()
            elif self.path == "/shot":
                name = self.headers.get("X-Name", time.strftime("phone_%Y%m%d_%H%M%S.png"))
                os.makedirs(SHOTS_DIR, exist_ok=True)
                path = os.path.join(SHOTS_DIR, os.path.basename(name))
                with open(path, "wb") as f:
                    f.write(data)
                db.insert("phone", "screenshot", path)
                _sync_received()
            self._ok()
        except Exception as e:
            log(f"[SYNC] erro no pedido: {e}", level=logging.WARNING)
            try:
                self.send_error(500)
            except Exception:
                pass

    def do_GET(self):
        self._ok(b"pokemmo-sync")


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def start_phone_sync():
    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8765), PhoneSyncHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        ip = local_ip()
        log(f"[SYNC] telemovel: ativo - na app PokeEvents poe o host {ip}:8765", kind="phone")
        return ip
    except Exception as e:
        log(f"[SYNC] falhou ao abrir porta 8765: {e}", logging.WARNING)
        return None


# ---------------- Overlay das zonas (click-through) ----------------
class ZoneOverlay:
    def __init__(self, root):
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.config(bg="black")
        self.top.attributes("-transparentcolor", "black")
        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.visible = False
        self._clickthrough_done = False
        self.top.withdraw()

    def _hwnd(self):
        return user32.GetParent(self.top.winfo_id())

    def _ensure_clickthrough(self):
        if self._clickthrough_done:
            return
        self.top.update_idletasks()
        hwnd = self._hwnd()
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | 0x80000 | 0x20)  # LAYERED|TRANSPARENT
        after = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self._clickthrough_done = True
        if after & 0x20:
            log("Overlay: click-through OK (nao bloqueia o jogo)", level=logging.DEBUG)
        else:
            log("Overlay: AVISO - click-through pode nao estar ativo!", logging.WARNING)

    def show(self, rect, deadzone):
        self._ensure_clickthrough()
        self.visible = True
        self.top.deiconify()
        self.update_for(rect, deadzone)

    def hide(self):
        self.visible = False
        self.top.withdraw()

    def update_for(self, rect, deadzone):
        if not self.visible:
            return
        l, t, r, b = rect
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return
        self.top.geometry(f"{w}x{h}+{l}+{t}")
        c = self.canvas
        c.delete("all")
        # grelha 3x3
        for i in (1, 2):
            x = l and (w * i // 3)
            c.create_line(x, 0, x, h, fill="#7aa2f7", dash=(4, 6))
            y = h * i // 3
            c.create_line(0, y, w, y, fill="#7aa2f7", dash=(4, 6))
        # zona morta central
        dz = deadzone * 2
        c.create_rectangle(w // 2 - dz, h // 2 - dz, w // 2 + dz, h // 2 + dz,
                           outline="#fbbf24", dash=(2, 4))
        # legendas
        fnt = ("Segoe UI", 9, "bold")
        c.create_text(w // 2, 14, text="W", fill="#7aa2f7", font=fnt)
        c.create_text(14, h // 2, text="A", fill="#7aa2f7", font=fnt)
        c.create_text(w - 14, h // 2, text="D", fill="#7aa2f7", font=fnt)
        c.create_text(w // 2, h - 14, text="S", fill="#7aa2f7", font=fnt)
        c.create_text(w // 2, h // 2, text="parar", fill="#fbbf24", font=("Segoe UI", 8))


# ---------------- Aplicacao ----------------
class PokeMMOPanel:

    def __init__(self, root):
        self.root = root
        self.root.title("PokeMMO SrRobs - Beta")
        self.root.geometry("340x820")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)

        self.game_hwnd = None
        self.game_title = ""
        self.click_distance = 160
        self.move_button = "left"
        self.mouse_mode = False        # modo rato por zonas (F10)
        self.mouse_trigger = "right"   # right | left | none (hover)
        self.send_keys = True          # enviar teclas WASD sinteticas ao cliente
        self.mouse_click_move = False  # modo rato: tambem clicar na direcao
        self.deadzone = 60             # meio-largura da zona morta (px)
        self._last_game_rect = None
        self._last_fg = 0
        self._dirty_ui = False
        self._last_state_snap = 0.0

        self.tracker = PhysicalInputTracker()
        self.tracker.start()
        self.key_mode = "scan"        # 'scan' | 'vk' (decidido pelo Teste WASD)

        self.engine = MovementEngine(self)
        self.overlay = ZoneOverlay(root)

        self._build_ui()

        log("=" * 50, gui=False)
        log(f"Painel iniciado | log: {LOG_FILE} | BD: {DB_FILE}")
        log("F9 = ligar/desligar WASD | F10 = modo RATO por zonas")
        log("Modo rato: escolhe o gatilho (Direito/Esquerdo/Só hover) e aponta para a zona.")
        db.insert("panel", "session", "painel iniciado")

        GameLogTailer(GAME_LOG, self).start()
        GameLogTailer(GAME_MODS_LOG, self).start()
        ChatOCRTailer(self).start()
        start_phone_sync()

        self.root.after(100, self._poll_log)
        self.root.after(150, self._poll_zone)
        self.root.after(1000, self._maintain)
        self.root.after(1500, self._auto_attach)

        self.engine.start()

    # ---------- UI ----------
    def _build_ui(self):
        f = tk.Frame(self.root, bg=BG, padx=12, pady=10)
        f.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(f, bg=BG)
        top.pack(fill=tk.X)
        tk.Label(top, text="POKEMMO  SRROBS", font=("Segoe UI", 12, "bold"),
                 bg=BG, fg=ACCENT).pack(side=tk.LEFT)
        self.lbl_mode = tk.Label(top, text="WASD ON", font=("Segoe UI", 9, "bold"),
                                 bg=BG, fg=OK)
        self.lbl_mode.pack(side=tk.RIGHT)

        card = tk.Frame(f, bg=CARD, padx=8, pady=6)
        card.pack(fill=tk.X, pady=(8, 4))
        self.lbl_status = tk.Label(card, text="Alvo: a procurar o jogo...",
                                   font=("Segoe UI", 9, "italic"), bg=CARD, fg=WARN, anchor="w")
        self.lbl_status.pack(fill=tk.X)

        row = tk.Frame(f, bg=BG)
        row.pack(fill=tk.X, pady=4)
        self._btn(row, "Abrir jogo", self.launch_game)
        self._btn(row, "Anexar (auto)", self.auto_attach_now)
        self._btn(row, "Janela...", self.open_picker)

        self.btn_wasd = self._btn(f, "Modo WASD: ATIVADO (F9)", self.toggle_wasd, full=True)
        self.btn_wasd.configure(bg="#1e3a5f")
        self.btn_mouse = self._btn(f, "Modo RATO por zonas: OFF (F10)", self.toggle_mouse, full=True)
        self.btn_mouse.configure(bg="#3a2a2a", fg=ERR)

        # zona atual (feedback + diagnostico)
        zc = tk.Frame(f, bg=CARD, padx=8, pady=4)
        zc.pack(fill=tk.X, pady=4)
        tk.Label(zc, text="Zona do rato:", font=("Segoe UI", 8), bg=CARD, fg=DIM).pack(side=tk.LEFT)
        self.lbl_zone = tk.Label(zc, text="-", font=("Consolas", 10, "bold"), bg=CARD, fg=FG)
        self.lbl_zone.pack(side=tk.LEFT, padx=8)
        self.lbl_diag = tk.Label(zc, text="modo:off foco:0 dentro:0 botao-dir:0",
                                 font=("Consolas", 7), bg=CARD, fg=DIM)
        self.lbl_diag.pack(side=tk.RIGHT)

        # opcoes do modo rato
        opt = tk.Frame(f, bg=CARD, padx=8, pady=6)
        opt.pack(fill=tk.X, pady=4)
        tk.Label(opt, text="Opcoes do modo rato", font=("Segoe UI", 8, "bold"),
                 bg=CARD, fg=ACCENT).pack(anchor="w")
        trig_row = tk.Frame(opt, bg=CARD)
        trig_row.pack(fill=tk.X, pady=(2, 0))
        tk.Label(trig_row, text="Andar quando:", font=("Segoe UI", 8),
                 bg=CARD, fg=DIM).pack(side=tk.LEFT)
        self.var_trigger = tk.StringVar(value="right")
        for txt, val in (("Direito", "right"), ("Esquerdo", "left"), ("Só hover", "none")):
            tk.Radiobutton(trig_row, text=txt, variable=self.var_trigger, value=val,
                           command=self._on_opts, bg=CARD, fg=FG, selectcolor=CARD2,
                           activebackground=CARD, activeforeground=FG,
                           font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=4)
        self.var_keys = tk.BooleanVar(value=True)
        self.ck_keys = tk.Checkbutton(
            opt, text="Enviar teclas WASD sinteticas ao cliente", variable=self.var_keys,
            command=self._on_opts, bg=CARD, fg=FG, activebackground=CARD,
            activeforeground=FG, selectcolor=CARD2, font=("Segoe UI", 8))
        self.ck_keys.pack(anchor="w")
        self.var_clickmove = tk.BooleanVar(value=False)
        self.ck_clickmove = tk.Checkbutton(
            opt, text="Tambem clicar p/ andar (pode acionar NPCs)", variable=self.var_clickmove,
            command=self._on_opts, bg=CARD, fg=FG, activebackground=CARD,
            activeforeground=FG, selectcolor=CARD2, font=("Segoe UI", 8))
        self.ck_clickmove.pack(anchor="w")
        self.var_overlay = tk.BooleanVar(value=False)
        self.ck_overlay = tk.Checkbutton(
            opt, text="Mostrar grelha de zonas sobre o jogo", variable=self.var_overlay,
            command=self._on_opts, bg=CARD, fg=FG, activebackground=CARD,
            activeforeground=FG, selectcolor=CARD2, font=("Segoe UI", 8))
        self.ck_overlay.pack(anchor="w")

        tk.Label(opt, text="Zona morta central (px)", font=("Segoe UI", 8),
                 bg=CARD, fg=DIM).pack(anchor="w", pady=(4, 0))
        self.dz_var = tk.IntVar(value=self.deadzone)
        tk.Scale(opt, from_=20, to=150, orient=tk.HORIZONTAL, variable=self.dz_var,
                 command=self._on_dz, bg=CARD, fg=FG, highlightthickness=0,
                 troughcolor=CARD2, length=280).pack(fill=tk.X)

        # botao do rato + distancia (modo teclado)
        mouse_card = tk.Frame(f, bg=CARD, padx=8, pady=4)
        mouse_card.pack(fill=tk.X, pady=4)
        tk.Label(mouse_card, text="Botao do rato p/ andar (teclado)", font=("Segoe UI", 8),
                 bg=CARD, fg=DIM).pack(side=tk.LEFT)
        self.btn_var = tk.StringVar(value="left")
        tk.Radiobutton(mouse_card, text="Esq", variable=self.btn_var, value="left",
                       command=self._on_button_change, bg=CARD, fg=FG, selectcolor=CARD2,
                       activebackground=CARD, activeforeground=FG,
                       font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(10, 4))
        tk.Radiobutton(mouse_card, text="Dir", variable=self.btn_var, value="right",
                       command=self._on_button_change, bg=CARD, fg=FG, selectcolor=CARD2,
                       activebackground=CARD, activeforeground=FG,
                       font=("Segoe UI", 8)).pack(side=tk.LEFT)

        dist_card = tk.Frame(f, bg=CARD, padx=8, pady=6)
        dist_card.pack(fill=tk.X, pady=4)
        tk.Label(dist_card, text="Distancia do clique (px)", font=("Segoe UI", 8),
                 bg=CARD, fg=DIM).pack(anchor="w")
        self.dist_var = tk.IntVar(value=self.click_distance)
        tk.Scale(dist_card, from_=80, to=300, orient=tk.HORIZONTAL, variable=self.dist_var,
                 command=self._on_dist, bg=CARD, fg=FG, highlightthickness=0,
                 troughcolor=CARD2, length=280).pack(fill=tk.X)

        # D-pad toggle
        tk.Label(f, text="D-Pad (clique liga / clique desliga)", font=("Segoe UI", 8),
                 bg=BG, fg=DIM).pack(anchor="w", pady=(6, 2))
        self._build_dpad(f)

        # Fila de perguntas -> GLM
        qcard = tk.Frame(f, bg=CARD, padx=8, pady=6)
        qcard.pack(fill=tk.X, pady=4)
        tk.Label(qcard, text="Perguntas (GLM)", font=("Segoe UI", 8, "bold"),
                 bg=CARD, fg=ACCENT).pack(anchor="w")
        self.q_entry = tk.Entry(qcard, font=("Segoe UI", 8), bg="#101014", fg=FG,
                                relief="flat", insertbackground=FG)
        self.q_entry.pack(fill=tk.X, pady=(3, 2))
        self.q_entry.bind("<Return>", lambda _e: self.q_add())
        self._btn(qcard, "+ Adicionar", self.q_add).pack(fill=tk.X)
        self.q_list = tk.Listbox(qcard, height=4, font=("Segoe UI", 8), bg="#101014",
                                 fg=FG, selectbackground=ACCENT, relief="flat")
        self.q_list.pack(fill=tk.X, pady=2)
        rowq = tk.Frame(qcard, bg=CARD)
        rowq.pack(fill=tk.X)
        self._btn(rowq, "Perguntar GLM", self.q_ask_selected)
        self._btn(rowq, "Remover", self.q_remove)

        # Captura / BD
        row2 = tk.Frame(f, bg=BG)
        row2.pack(fill=tk.X, pady=(6, 0))
        self._btn(row2, "OCR chat", self.capture_chat_ocr)
        self._btn(row2, "Analise", self.open_analysis)
        self._btn(row2, "CSV", self.export_csv)

        # Diagnostico de input (decide COMO o jogo aceita input sintetico)
        row3 = tk.Frame(f, bg=BG)
        row3.pack(fill=tk.X, pady=(2, 0))
        self._btn(row3, "Teste WASD", self.run_test_keys)
        self._btn(row3, "Teste clique", self.run_test_click)

        # Log
        tk.Label(f, text="Log", font=("Segoe UI", 8), bg=BG, fg=DIM).pack(anchor="w", pady=(8, 2))
        self.log_text = scrolledtext.ScrolledText(
            f, height=12, state=tk.DISABLED, font=("Consolas", 8),
            bg="#101014", fg="#c8c8d4", relief="flat", insertbackground=FG,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for kind, color in KIND_COLORS.items():
            self.log_text.tag_configure(kind, foreground=color)

    def _btn(self, parent, text, cmd, full=False):
        b = tk.Button(parent, text=text, command=cmd, font=("Segoe UI", 8, "bold"),
                      bg=CARD2, fg=FG, activebackground="#3a3a4a",
                      relief="flat", padx=8, pady=4, cursor="hand2")
        if full:
            b.pack(fill=tk.X, pady=2)
        else:
            b.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        return b

    def _build_dpad(self, parent):
        frame = tk.Frame(parent, bg=BG)
        frame.pack(pady=2)
        self.dpad_buttons = {}
        layout = [("w", 0, 1, "▲ W"), ("a", 1, 0, "◄ A"), ("s", 1, 1, "▼ S"), ("d", 1, 2, "► D")]
        for key, r, c, txt in layout:
            b = tk.Button(frame, text=txt, width=6, height=1, font=("Segoe UI", 9, "bold"),
                          bg=CARD2, fg=FG, relief="flat", cursor="hand2",
                          activebackground="#3a3a4a")
            b.grid(row=r, column=c, padx=2, pady=2)
            b.config(command=lambda k=key: self._dpad_toggle(k))
            self.dpad_buttons[key] = b

    def _dpad_toggle(self, key):
        state = not self.engine.virtual[key]
        self.engine.virtual[key] = state
        self.dpad_buttons[key].configure(
            bg=ACCENT if state else CARD2,
            fg="#111" if state else FG,
        )
        log(f"D-Pad: {key.upper()} {'ON' if state else 'OFF'}", kind="movement", level=logging.DEBUG)

    # ---------- opcoes ----------
    def _on_opts(self):
        self.mouse_trigger = self.var_trigger.get()
        self.send_keys = bool(self.var_keys.get())
        self.mouse_click_move = bool(self.var_clickmove.get())
        self.engine.release_all()
        self._apply_overlay()
        log(f"Opcoes: gatilho={self.mouse_trigger} | teclas={self.send_keys} | clique={self.mouse_click_move}",
            level=logging.DEBUG)

    def _on_dz(self, val):
        self.deadzone = int(val)
        try:
            if self.game_hwnd:
                self.overlay.update_for(win32gui.GetWindowRect(self.game_hwnd), self.deadzone)
        except Exception:
            pass

    def _on_button_change(self):
        self.move_button = self.btn_var.get()
        self.engine.release_all()
        aviso = ""
        if self.mouse_mode and self.mouse_trigger != "none" and self.move_button == self.mouse_trigger:
            aviso = " (igual ao gatilho! sera usado o OUTRO botao para nao haver eco)"
        log(f"Botao do rato para andar: {'esquerdo' if self.move_button == 'left' else 'direito'}{aviso}")

    def _on_dist(self, val):
        self.click_distance = int(val)

    # ---------- acoes ----------
    def launch_game(self):
        exe = os.path.join(GAME_DIR, "PokeMMO.exe")
        if os.path.exists(exe):
            log(f"A lancar o jogo: {exe}")
            # cwd = pasta do jogo, senao o cliente crasha (nao acha os ficheiros)
            subprocess.Popen([exe], cwd=GAME_DIR)
            if self.game_hwnd is None:
                self.root.after(4000, self._auto_attach)
        else:
            log(f"PokeMMO.exe nao encontrado em {exe}", logging.WARNING)

    def toggle_wasd(self):
        self.engine.enabled = not self.engine.enabled
        self.engine.release_all()
        log(f"Modo WASD: {'ATIVADO' if self.engine.enabled else 'DESATIVADO'}", kind="movement")
        self.refresh_wasd_button()

    def toggle_mouse(self):
        self.mouse_mode = not self.mouse_mode
        self.engine.release_all()
        log(f"Modo RATO por zonas: {'ATIVADO' if self.mouse_mode else 'DESATIVADO'}", kind="movement")
        self.refresh_mode_buttons()
        self._apply_overlay()

    def refresh_wasd_button(self):
        if not hasattr(self, "btn_wasd"):
            return
        if self.engine.enabled:
            self.btn_wasd.config(text="Modo WASD: ATIVADO (F9)", bg="#1e3a5f", fg=OK)
            self.lbl_mode.config(text="WASD ON", fg=OK)
        else:
            self.btn_wasd.config(text="Modo WASD: DESATIVADO (F9)", bg="#3a2a2a", fg=ERR)
            self.lbl_mode.config(text="WASD OFF", fg=ERR)

    def refresh_mode_buttons(self):
        self.refresh_wasd_button()
        if self.mouse_mode:
            self.btn_mouse.config(text="Modo RATO por zonas: ON (F10)", bg="#1e3a5f", fg=OK)
        else:
            self.btn_mouse.config(text="Modo RATO por zonas: OFF (F10)", bg="#3a2a2a", fg=ERR)

    def _apply_overlay(self):
        if not self.game_hwnd:
            return
        try:
            rect = win32gui.GetWindowRect(self.game_hwnd)
        except Exception:
            return
        if self.mouse_mode and self._overlay_wanted():
            self.overlay.show(rect, self.deadzone)
        else:
            self.overlay.hide()

    def _overlay_wanted(self):
        return bool(self.var_overlay.get())

    def export_csv(self):
        try:
            n = db.export_csv(CSV_EXPORT)
            log(f"BD exportada: {n} eventos -> {CSV_EXPORT}")
        except Exception as e:
            log(f"Falha ao exportar: {e}", logging.ERROR)

    def capture_chat_ocr(self):
        try:
            import pytesseract  # noqa: F401
            from PIL import ImageGrab
        except ImportError:
            log("OCR precisa de: pip install pytesseract + instalar o Tesseract "
                "(https://github.com/UB-Mannheim/tesseract/wiki)", logging.WARNING)
            return
        if not self.game_hwnd:
            log("OCR: jogo nao anexado", logging.WARNING)
            return
        try:
            left, top, right, bottom = win32gui.GetWindowRect(self.game_hwnd)
            chat_box = (left + 10, bottom - int((bottom - top) * 0.28),
                        left + int((right - left) * 0.55), bottom - 10)
            img = ImageGrab.grab(bbox=chat_box)
            import pytesseract
            text = pytesseract.image_to_string(img, lang="por+eng")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for ln in lines:
                db.insert("ocr", "chat", ln)
            log(f"OCR: {len(lines)} linhas capturadas -> BD (source=ocr)")
        except Exception as e:
            log(f"OCR falhou: {e}", logging.ERROR)

    def capture_game_png(self, prefix="shot"):
        """Screenshot da area cliente do jogo -> shots/ (p/ template matching futuro)."""
        if not self.game_hwnd or not win32gui.IsWindow(self.game_hwnd):
            return None
        if win32gui.IsIconic(self.game_hwnd):
            return None
        try:
            from PIL import ImageGrab
        except ImportError:
            return None
        try:
            l, t, r, b = win32gui.GetClientRect(self.game_hwnd)
            ox, oy = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            img = ImageGrab.grab(bbox=(ox, oy, ox + r - l, oy + b - t), all_screens=True)
            os.makedirs(SHOTS_DIR, exist_ok=True)
            path = os.path.join(SHOTS_DIR, time.strftime(f"{prefix}_%Y%m%d_%H%M%S") + ".png")
            img.save(path)
            db.insert("panel", "screenshot", path)
            return path
        except Exception as e:
            log(f"Screenshot falhou: {e}", logging.WARNING)
            return None

    def on_battle_event(self, line):
        path = self.capture_game_png("battle")
        if path:
            log(f"Screenshot: {os.path.basename(path)}", kind="battle",
                level=logging.DEBUG, db_save=False)

    # ---------- fila de perguntas (GLM) ----------
    def q_add(self):
        q = self.q_entry.get().strip()
        if q:
            self.q_list.insert(tk.END, q)
            self.q_entry.delete(0, tk.END)
            log(f"Pergunta em fila: {q[:70]}", level=logging.DEBUG)

    def q_remove(self):
        for i in self.q_list.curselection()[::-1]:
            self.q_list.delete(i)

    def q_ask_selected(self):
        sel = list(self.q_list.curselection())
        idxs = sel if sel else list(range(self.q_list.size()))
        if not idxs:
            log("Sem perguntas na fila", logging.WARNING)
            return
        if not glm_api_key():
            log("GLM: falta a API key -> cria config/glm.properties com key=... "
                "(ou variavel de ambiente GLM_API_KEY)", logging.WARNING)
            return
        threading.Thread(target=self._glm_worker, args=(idxs,), daemon=True).start()

    def _glm_worker(self, idxs):
        for i in sorted(idxs, reverse=True):
            try:
                q = self.q_list.get(i)
            except Exception:
                continue
            log(f"[GLM] a perguntar: {q[:70]}", kind="glm")
            ans = self.glm_ask(q)
            db.insert("panel", "glm_q", q)
            db.insert("panel", "glm_a", ans)
            log(f"[GLM] R: {ans[:400]}", kind="glm")
            self.root.after(0, lambda i=i: self.q_list.delete(i) if i < self.q_list.size() else None)

    def _glm_context(self):
        try:
            chat = [m for _ts, m in db.recent("chat", 12)]
            n_battles = len(db.recent("battle_line", 500))
            return (f"Janela: {self.game_title or 'jogo nao anexado'}. "
                    f"Batalhas registadas na sessao: {n_battles}. "
                    f"Ultimas linhas do chat do jogo: {chat}")
        except Exception:
            return ""

    def glm_ask(self, question):
        key = glm_api_key()
        if not key:
            return "(sem API key)"
        body = json.dumps({
            "model": GLM_MODEL,
            "messages": [
                {"role": "system", "content":
                    "Es o assistente do painel PokeMMO SrRobs. Responde em portugues "
                    "de Portugal, curto e pratico, a perguntas sobre o jogo e sobre os "
                    "dados recolhidos pelo painel.\nContexto capturado: " + self._glm_context()},
                {"role": "user", "content": question},
            ],
            "temperature": 0.6,
            "max_tokens": 600,
        }).encode("utf-8")
        req = urllib.request.Request(
            GLM_URL, data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            return f"(erro HTTP {e.code}: {e.read()[:200]!r})"
        except Exception as e:
            return f"(erro GLM: {e})"

    def run_test_keys(self):
        threading.Thread(target=self.test_synthetic_keys, daemon=True).start()

    def run_test_click(self):
        threading.Thread(target=self.test_click_move, daemon=True).start()

    def _focus_game(self, tries=6):
        """Foco a serio: clicar na barra de titulo e a forma fiavel de o roubar
        (SetForegroundWindow sozinho e bloqueado pelo Windows a maior parte das vezes)."""
        for _ in range(tries):
            try:
                if user32.GetForegroundWindow() == self.game_hwnd:
                    return True
                l, t, r, b = win32gui.GetWindowRect(self.game_hwnd)
                user32.SetCursorPos((l + r) // 2, t + 10)
                user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
                user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
            except Exception:
                pass
            time.sleep(0.3)
        return user32.GetForegroundWindow() == self.game_hwnd

    def _begin_test(self):
        if not self.game_hwnd or not win32gui.IsWindow(self.game_hwnd):
            log("Teste: jogo nao anexado", logging.WARNING)
            return False
        if win32gui.IsIconic(self.game_hwnd):
            log("Teste: a janela do jogo esta minimizada", logging.WARNING)
            return False
        try:
            from PIL import ImageGrab  # noqa: F401
        except ImportError:
            log("Teste precisa de: pip install pillow", logging.WARNING)
            return False
        self.engine.release_all()
        if not self._focus_game():
            log("Teste: NAO consegui meter o jogo em 1o plano "
                "(clica na janela do jogo e repete o teste)", logging.WARNING)
            return False
        log(f"Teste: foco confirmado (hwnd={self.game_hwnd})", level=logging.DEBUG)
        return True

    def _grab_client(self):
        from PIL import ImageGrab
        l, t, r, b = win32gui.GetClientRect(self.game_hwnd)
        ox, oy = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
        return ImageGrab.grab(bbox=(ox, oy, ox + r - l, oy + b - t), all_screens=True)

    @staticmethod
    def _img_diff(a, b):
        """Media da diferenca por pixel (0 = imagens iguais)."""
        small = (max(1, a.width // 4), max(1, a.height // 4))
        pa = a.resize(small).tobytes()
        pb = b.resize(small).tobytes()
        if len(pa) != len(pb):
            return 1e9
        return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)

    def test_synthetic_keys(self):
        """Envia W sintetico (scancode e depois vk) e mede se o ecra mudou.
        Decide automaticamente o modo que o jogo aceita."""
        if not self._begin_test():
            return
        results = {}
        for mode in ("scan", "vk"):
            time.sleep(0.6)
            a = self._grab_client()
            send_key(SC["w"], VK["w"], up=False, mode=mode)
            time.sleep(1.2)
            send_key(SC["w"], VK["w"], up=True, mode=mode)
            b = self._grab_client()
            results[mode] = self._img_diff(a, b)
            log(f"Teste WASD [{mode}]: diff de pixels = {results[mode]:.2f}")
        winner = max(results, key=results.get)
        if results[winner] >= 2.0:
            self.key_mode = winner
            log(f"VEREDICTO: o jogo REAGE a teclas sinteticas -> a usar modo '{winner}'",
                kind="movement")
            log("Modo rato (F10) deve agora andar. Testa e diz!", kind="movement")
        else:
            log("VEREDICTO: o jogo NAO reage a teclas sinteticas (nem scan nem vk)",
                kind="warn", level=logging.WARNING)
            log("-> experimenta o botao 'Teste clique' (andar por clique continuo)",
                kind="warn", level=logging.WARNING)

    def test_click_move(self):
        """Testa o click-to-move nativo: clique esquerdo CONTINUO 1.4s."""
        if not self._begin_test():
            return
        center = self.client_center()
        if center is None:
            return
        time.sleep(0.6)
        a = self._grab_client()
        user32.SetCursorPos(center[0] + 180, center[1])
        user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN (hold)
        time.sleep(1.4)
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
        b = self._grab_client()
        d = self._img_diff(a, b)
        log(f"Teste clique: diff de pixels = {d:.2f}")
        if d >= 2.0:
            log("VEREDICTO: o jogo ANDA com clique continuo -> podemos usar isto no modo rato",
                kind="movement")
        else:
            log("VEREDICTO: sem reacao ao clique continuo (ou nao ha click-to-move aqui)",
                kind="warn", level=logging.WARNING)

    def open_analysis(self):
        win = tk.Toplevel(self.root)
        win.title("Analise de eventos")
        win.configure(bg=BG, padx=10, pady=10)
        win.attributes("-topmost", True)
        win.geometry("660x540")

        txt = scrolledtext.ScrolledText(win, font=("Consolas", 9), bg="#101014",
                                        fg="#c8c8d4", relief="flat")
        txt.pack(fill=tk.BOTH, expand=True)
        txt.tag_configure("h", foreground=ACCENT, font=("Consolas", 9, "bold"))

        def section(title):
            txt.insert(tk.END, f"\n== {title} ".ljust(56, "=") + "\n", "h")

        kind_counts, src_counts = db.stats()
        section("Eventos por tipo")
        for k, c in kind_counts:
            txt.insert(tk.END, f"  {k:<14} {c:>7}\n")
        section("Eventos por origem")
        for s, c in src_counts:
            txt.insert(tk.END, f"  {s:<14} {c:>7}\n")
        section("Ultimas batalhas")
        for ts, msg in db.recent("battle_line", 15):
            txt.insert(tk.END, f"  {ts}  {msg[:60]}\n")
        section("Chat (OCR) - ultimas 30")
        for ts, msg in db.recent("chat", 30):
            txt.insert(tk.END, f"  {ts}  {msg[:72]}\n")
        section("Screenshots gravados")
        for ts, msg in db.recent("screenshot", 15):
            txt.insert(tk.END, f"  {ts}  {msg[:72]}\n")
        txt.config(state=tk.DISABLED)

    def open_picker(self):
        win = tk.Toplevel(self.root)
        win.title("Selecionar janela")
        win.configure(bg=BG, padx=10, pady=10)
        win.attributes("-topmost", True)
        win.geometry("360x400")

        tk.Label(win, text="Janelas visiveis", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=ACCENT).pack(anchor="w")
        lb = tk.Listbox(win, font=("Segoe UI", 8), bg="#101014", fg=FG,
                        selectbackground=ACCENT, relief="flat")
        lb.pack(fill=tk.BOTH, expand=True, pady=6)

        windows = {}

        def enum_cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    windows[title] = hwnd
                    lb.insert(tk.END, title)

        win32gui.EnumWindows(enum_cb, None)

        def choose(_=None):
            sel = lb.curselection()
            if sel:
                title = lb.get(sel[0])
                self.attach(windows[title], title, manual=True)
                win.destroy()

        lb.bind("<Double-Button-1>", choose)
        tk.Button(win, text="Definir alvo", command=choose, bg=CARD2, fg=FG,
                  relief="flat", padx=8, pady=4).pack(fill=tk.X)

    # ---------- zonas ----------
    def cursor_zone_keys(self):
        """Cursor -> teclas WASD da zona onde esta (grelha 3x3 + zona morta)."""
        if not self.game_hwnd or not win32gui.IsWindow(self.game_hwnd):
            return [], False
        try:
            px, py = win32gui.GetCursorPos()
            l, t, r, b = win32gui.GetClientRect(self.game_hwnd)
            ox, oy = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            x = px - ox
            y = py - oy
            if not (0 <= x <= (r - l) and 0 <= y <= (b - t)):
                return [], False
            cx = (r - l) / 2.0
            cy = (b - t) / 2.0
            dz = float(self.deadzone)
            keys = []
            if y < cy - dz:
                keys.append("w")
            if y > cy + dz:
                keys.append("s")
            if x < cx - dz:
                keys.append("a")
            if x > cx + dz:
                keys.append("d")
            return keys, True
        except Exception:
            return [], False

    # ---------- attach ----------
    def auto_attach_now(self):
        self._auto_attach(manual=True)

    def _auto_attach(self, manual=False):
        candidates = []
        try:
            def enum_cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title.lower() == "pokemmo":
                        candidates.append((hwnd, title))

            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            logger.exception("auto-attach falhou")

        found = candidates[0] if candidates else None
        if found:
            if found[0] != self.game_hwnd:
                self.attach(found[0], found[1])
        elif manual:
            log("Janela 'PokeMMO' nao encontrada - usa 'Janela...' para escolher", logging.WARNING)

        self.root.after(1500, self._auto_attach)

    def attach(self, hwnd, title, manual=False):
        self.game_hwnd = hwnd
        self.game_title = title
        self._last_game_rect = None
        log(f"Alvo definido: '{title}' (hwnd={hwnd}" + (", manual)" if manual else ", auto)"), kind="focus")
        self.lbl_status.config(text=f"Alvo: {title[:34]}", fg=OK)
        self.snap_beside_game()
        self._apply_overlay()

    def client_center(self):
        if not self.game_hwnd or not win32gui.IsWindow(self.game_hwnd):
            return None
        try:
            l, t, r, b = win32gui.GetClientRect(self.game_hwnd)
            pt = win32gui.ClientToScreen(self.game_hwnd, (0, 0))
            return (pt[0] + (r - l) // 2, pt[1] + (b - t) // 2)
        except Exception:
            return None

    def snap_beside_game(self):
        if not self.game_hwnd or not win32gui.IsWindow(self.game_hwnd):
            return
        try:
            rect = win32gui.GetWindowRect(self.game_hwnd)
        except Exception:
            return
        self.overlay.update_for(rect, self.deadzone)
        if rect == self._last_game_rect:
            return
        self._last_game_rect = rect
        l, t, r, b = rect
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        panel_w, panel_h = 350, 830
        x = r + 8
        if x + panel_w > sw:
            x = max(0, l - panel_w - 8)
        y = max(0, min(t, sh - panel_h))
        self.root.geometry(f"+{x}+{y}")
        log(f"Painel posicionado ao lado do jogo em ({x},{y})", level=logging.DEBUG)

    # ---------- loops ----------
    def _poll_log(self):
        try:
            while True:
                stamp, kind, msg = GUI_LOG_QUEUE.get_nowait()
                color = KIND_COLORS.get(kind, "#c8c8d4")
                tag = kind if kind in KIND_COLORS else "normal"
                self.log_text.tag_configure(tag, foreground=color)
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, f"{stamp}  {msg}\n", tag)
                lines = int(self.log_text.index("end-1c").split(".")[0])
                if lines > 400:
                    self.log_text.delete("1.0", "200.0")
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _poll_zone(self):
        eng = self.engine
        txt = eng.zone_txt if (self.mouse_mode and eng.mouse_active) else "-"
        self.lbl_zone.config(text=txt, fg=ACCENT if txt != "-" else DIM)
        fg, inside, trg = eng.diag
        state = "ATIVO" if self.mouse_mode else "off"
        self.lbl_diag.config(
            text=f"modo:{state} foco:{fg} dentro:{inside} gatilho:{trg}",
            fg=OK if (self.mouse_mode and fg and trg) else DIM,
        )
        self.root.after(150, self._poll_zone)

    def _maintain(self):
        self.root.attributes("-topmost", True)
        self.root.lift()
        if self._dirty_ui:
            self._dirty_ui = False
            self.refresh_mode_buttons()
            self._apply_overlay()
        if self.game_hwnd and not win32gui.IsWindow(self.game_hwnd):
            log(f"Janela do jogo fechada ({self.game_title})", logging.WARNING, kind="logout")
            self.game_hwnd = None
            self.game_title = ""
            self.lbl_status.config(text="Alvo: a procurar o jogo...", fg=WARN)
            self.overlay.hide()
        else:
            self.snap_beside_game()
            self._track_game_state()
            now = time.time()
            if self.game_hwnd and now - self._last_state_snap > 60:
                self._last_state_snap = now
                db.insert("panel", "state",
                          f"wasd={self.engine.enabled} mouse={self.mouse_mode} "
                          f"zona={self.engine.zone_txt} titulo={self.game_title}")
        self.root.after(1000, self._maintain)

    def _track_game_state(self):
        if not self.game_hwnd:
            return
        fg = user32.GetForegroundWindow()
        if fg != self._last_fg:
            if fg == self.game_hwnd:
                log("Foco: jogo em 1o plano", kind="focus", level=logging.DEBUG)
            elif self._last_fg == self.game_hwnd and fg:
                try:
                    t = win32gui.GetWindowText(fg) or "?"
                except Exception:
                    t = "?"
                log(f"Foco: saiu do jogo -> '{t[:30]}'", kind="focus", level=logging.DEBUG)
            self._last_fg = fg
        try:
            title = win32gui.GetWindowText(self.game_hwnd)
            if title and title != self.game_title:
                log(f"Titulo do jogo: '{title}' (antes '{self.game_title}')", kind="title")
                self.game_title = title
                self.lbl_status.config(text=f"Alvo: {title[:34]}")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # coords reais (DPI)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    app = PokeMMOPanel(root)
    root.mainloop()
