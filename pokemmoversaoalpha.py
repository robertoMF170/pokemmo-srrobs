import tkinter as tk
from tkinter import messagebox
import pyautogui
import win32gui
import os
import logging

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.01

# --- Configuração do debug/log ---
LOG_DIR = r"D:\pokemmo"
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    LOG_FILE = os.path.join(LOG_DIR, "debug.txt")
except Exception:
    LOG_FILE = "debug.txt"  # fallback se D:\pokemmo não puder ser criada

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
        logging.StreamHandler(),  # aparece também na consola
    ],
)
logger = logging.getLogger("PokeMMO")
logger.info("=" * 60)
logger.info("Aplicação iniciada - log a ser gravado em: %s", LOG_FILE)


class PokeMMOController:

  def __init__(self, root):
    self.root = root
    self.root.title("PokeMMO SrRobs - Versão Alpha")
    self.root.geometry("260x420")
    self.root.attributes("-topmost", True)  # Mantém sempre visível por cima

    self.game_hwnd = None
    self.windows_dict = {}

    # Se nenhum clique aparecer no log (nem este genérico), o clique não
    # está sequer a chegar à janela do painel — normalmente porque o jogo
    # ficou por cima dela.
    self.root.bind_all("<Button>", self._debug_click)

    control_frame = tk.Frame(root)
    control_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    # --- Secção de Seleção de Janela ---
    tk.Label(
        control_frame,
        text="Selecionar Janela do Jogo",
        font=("Arial", 9, "bold"),
    ).pack(pady=2)

    self.search_var = tk.StringVar()
    self.search_entry = tk.Entry(
        control_frame, textvariable=self.search_var, font=("Arial", 9)
    )
    self.search_entry.pack(fill=tk.X, pady=2)
    self.search_entry.insert(0, "Pesquisar (ex: PokeMMO)...")
    self.search_entry.bind("<FocusIn>", lambda e: self.clear_placeholder())

    list_frame = tk.Frame(control_frame)
    list_frame.pack(fill=tk.BOTH, expand=True, pady=5)

    self.window_listbox = tk.Listbox(list_frame, font=("Arial", 8), height=6)
    self.window_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    scrollbar = tk.Scrollbar(
        list_frame, orient="vertical", command=self.window_listbox.yview
    )
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    self.window_listbox.config(yscrollcommand=scrollbar.set)

    # O trace só é ligado depois da window_listbox existir
    self.search_var.trace_add("write", self.update_window_list)

    btn_refresh = tk.Button(
        control_frame, text="Atualizar Lista", command=self.load_windows, font=("Arial", 8)
    )
    btn_refresh.pack(fill=tk.X, pady=1)

    self.btn_select = tk.Button(
        control_frame,
        text="Definir Alvo Ativo",
        command=self.set_active_target,
        bg="#4CAF50",
        fg="white",
        font=("Arial", 8, "bold"),
    )
    self.btn_select.pack(fill=tk.X, pady=3)

    self.lbl_status = tk.Label(
        control_frame,
        text="Alvo: Nenhum selecionado",
        font=("Arial", 8, "italic"),
        fg="red",
    )
    self.lbl_status.pack(pady=2)

    # --- Controlo D-Pad (WASD) ---
    tk.Label(
        control_frame, text="Controlo Rato (WASD)", font=("Arial", 9, "bold")
    ).pack(pady=5)
    self.create_dpad(control_frame)

    self.load_windows()
    self._reforcar_topmost()

  def _debug_click(self, event):
    logger.debug(
        "Clique detetado no painel | widget=%s | pos_ecrã=(%s,%s)",
        event.widget, event.x_root, event.y_root,
    )

  def _reforcar_topmost(self):
    # O jogo (ou o próprio Windows) pode "roubar" o topo da pilha de janelas.
    # Reforçamos o topmost a cada 1s para o painel não ficar escondido atrás dele.
    self.root.attributes("-topmost", True)
    self.root.lift()
    self.root.after(1000, self._reforcar_topmost)

  def clear_placeholder(self):
    if self.search_entry.get() == "Pesquisar (ex: PokeMMO)...":
      self.search_entry.delete(0, tk.END)

  def load_windows(self):
    self.windows_dict.clear()
    self.window_listbox.delete(0, tk.END)

    def enum_windows_callback(hwnd, extra):
      if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if title:
          self.windows_dict[title] = hwnd

    win32gui.EnumWindows(enum_windows_callback, None)
    logger.info("Janelas encontradas: %d", len(self.windows_dict))
    self.update_window_list()

  def update_window_list(self, *args):
    search_term = self.search_var.get().lower()
    if search_term == "pesquisar (ex: pokemmo)...":
      search_term = ""

    self.window_listbox.delete(0, tk.END)
    for title in sorted(self.windows_dict.keys()):
      if search_term in title.lower():
        self.window_listbox.insert(tk.END, title)

  def set_active_target(self):
    try:
      selected_index = self.window_listbox.curselection()
      if not selected_index:
        messagebox.showwarning("Aviso", "Seleciona uma janela da lista primeiro!")
        return

      selected_title = self.window_listbox.get(selected_index)
      self.game_hwnd = self.windows_dict[selected_title]
      self.lbl_status.config(
          text=f"Alvo: {selected_title[:20]}...", fg="green"
      )
      logger.info("Alvo definido: '%s' (hwnd=%s)", selected_title, self.game_hwnd)
    except Exception as e:
      logger.exception("Erro ao definir alvo")
      messagebox.showerror("Erro", f"Erro ao definir alvo: {str(e)}")

  def create_dpad(self, parent):
    frame = tk.Frame(parent)
    frame.pack(pady=2)

    btn_up = tk.Button(frame, text="▲ (W)", width=7, height=1)
    btn_up.grid(row=0, column=1, padx=2, pady=2)
    self.bind_hold_key(btn_up, "w")

    btn_left = tk.Button(frame, text="◄ (A)", width=7, height=1)
    btn_left.grid(row=1, column=0, padx=2, pady=2)
    self.bind_hold_key(btn_left, "a")

    btn_down = tk.Button(frame, text="▼ (S)", width=7, height=1)
    btn_down.grid(row=1, column=1, padx=2, pady=2)
    self.bind_hold_key(btn_down, "s")

    btn_right = tk.Button(frame, text="► (D)", width=7, height=1)
    btn_right.grid(row=1, column=2, padx=2, pady=2)
    self.bind_hold_key(btn_right, "d")

  def bind_hold_key(self, button, key):
    def on_press(event):
      if not self.game_hwnd:
        logger.warning("Tecla '%s' premida sem nenhum alvo selecionado", key)
        return
      try:
        fg_antes = win32gui.GetForegroundWindow()
        win32gui.SetForegroundWindow(self.game_hwnd)
        fg_depois = win32gui.GetForegroundWindow()
        logger.debug(
            "[%s] DOWN | foco antes=%s ('%s') | alvo=%s | foco depois=%s ('%s') | foco_ok=%s",
            key, fg_antes, win32gui.GetWindowText(fg_antes),
            self.game_hwnd, fg_depois, win32gui.GetWindowText(fg_depois),
            fg_depois == self.game_hwnd,
        )
        if fg_depois != self.game_hwnd:
          logger.warning(
              "[%s] O Windows NAO deu foco a janela do jogo -> as teclas estao a ir "
              "para '%s'. Prova quase certa de porque o PokeMMO nao se mexe.",
              key, win32gui.GetWindowText(fg_depois),
          )
        pyautogui.keyDown(key)
        logger.debug("[%s] pyautogui.keyDown enviado com sucesso", key)
        button.config(bg="#a0a0a0")
      except Exception:
        logger.exception("[%s] Falhou ao premir a tecla", key)

    def on_press_release(event):
      if not self.game_hwnd:
        return
      try:
        pyautogui.keyUp(key)
        logger.debug("[%s] UP | pyautogui.keyUp enviado", key)
        button.config(bg="#d9d9d9")
      except Exception:
        logger.exception("[%s] Falhou ao largar a tecla", key)

    button.bind("<ButtonPress-1>", on_press)
    button.bind("<ButtonRelease-1>", on_press_release)


if __name__ == "__main__":
  root = tk.Tk()
  app = PokeMMOController(root)
  root.mainloop()