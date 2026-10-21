#!/usr/bin/env python3
"""Окно-установщик русификатора Total War: Arena (Revival).

Находит игру, сам делает бэкап оригинальных паков, ставит перевод и умеет
вернуть всё как было. Запуск: «Запустить русификатор.bat» или
    python tools/twa_ru_gui.py
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import twa_ru  # noqa: E402

# в собранном .exe данные лежат рядом со скриптом внутри пакета
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
TRANSLATIONS = BASE / "translations" / "ru"
ABBR = BASE / "translations" / "abbreviations.tsv"
APPDATA = Path(os.environ.get("APPDATA", str(Path.home())))
CONFIG = APPDATA / "twa_ru" / "config.json"

COLORS = {"patched": "#1a7f37", "clean": "#8a6d00", "changed": "#b42318", "missing": "#b42318", "none": "#555555"}


def candidate_dirs():
    saved = None
    try:
        saved = json.loads(CONFIG.read_text(encoding="utf-8")).get("game")
    except (OSError, ValueError):
        pass
    if saved:
        yield Path(saved)
    try:  # лаунчер Revival хранит путь к игре здесь
        txt = (APPDATA / "com.twa.launcher" / "game_dir.txt").read_text(encoding="utf-8").strip()
        if txt:
            yield Path(txt)
    except OSError:
        pass
    for drive in "CDEFGH":
        for name in ("Total War Arena", "Games/Total War Arena", "TWA", "Total War Arena Revival"):
            yield Path(f"{drive}:/{name}")
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            yield Path(base) / "Total War Arena"


def detect_game():
    for p in candidate_dirs():
        if (p / "Arena.exe").exists():
            return p
    return None


def game_running():
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return "arena.exe" in out or "twa-launcher.exe" in out


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Русификатор Total War: Arena (Revival)")
        self.geometry("720x520")
        self.minsize(620, 420)
        self.q = queue.Queue()
        self.busy = False

        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Папка игры (где лежит Arena.exe):").pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(2, 6))
        self.path = tk.StringVar()
        ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", command=self.browse).pack(side="left", padx=(6, 0))
        self.status = tk.Label(top, text="", anchor="w", font=("Segoe UI", 11, "bold"))
        self.status.pack(fill="x")
        self.hint = ttk.Label(top, text="", wraplength=680, foreground="#555555")
        self.hint.pack(fill="x", pady=(2, 0))

        btns = ttk.Frame(self, padding=(10, 0))
        btns.pack(fill="x")
        self.b_install = ttk.Button(btns, text="Установить русификатор", command=self.install)
        self.b_restore = ttk.Button(btns, text="Вернуть английский", command=self.restore)
        self.b_launch = ttk.Button(btns, text="Запустить игру", command=self.launch)
        for b in (self.b_install, self.b_restore, self.b_launch):
            b.pack(side="left", padx=(0, 8), pady=6)

        self.log = scrolledtext.ScrolledText(self, height=14, state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        twa_ru.OUT = lambda *a: self.q.put(("log", " ".join(str(x) for x in a)))
        self.path.trace_add("write", lambda *_: self.refresh())
        found = detect_game()
        if found:
            self.path.set(str(found))
            self.say(f"Игра найдена: {found}")
        else:
            self.say("Игру найти не удалось: нажмите «Обзор…» и выберите папку с Arena.exe.")
        self.refresh()
        self.after(100, self.poll)

    # --- вспомогательное -----------------------------------------------------
    def say(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def game(self):
        p = Path(self.path.get().strip().strip('"'))
        return p if (p / "Arena.exe").exists() else None

    def browse(self):
        d = filedialog.askdirectory(title="Выберите папку игры (с Arena.exe)")
        if d:
            self.path.set(d)

    def refresh(self):
        g = self.game()
        if not g:
            self.set_status("Папка игры не выбрана", "none", "Выберите папку, в которой лежит Arena.exe.")
            for b in (self.b_install, self.b_restore, self.b_launch):
                b.state(["disabled"])
            return
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({"game": str(g)}), encoding="utf-8")
        except OSError:
            pass
        kind, info = twa_ru.game_status(g)
        texts = {
            "patched": ("Русификатор установлен", "Можно запускать игру. «Установить» обновит перевод до свежей версии."),
            "clean": ("Русификатор не установлен", "Нажмите «Установить»: бэкап оригиналов будет создан автоматически."),
            "changed": ("Файлы игры изменились", "Скорее всего, игра обновилась. «Установить» сохранит новые оригиналы и поставит перевод заново."),
            "missing": ("Не хватает файлов игры", info),
        }
        t, h = texts[kind]
        self.set_status(t, kind, h)
        self.b_install.state(["!disabled"] if kind != "missing" else ["disabled"])
        has_backup = twa_ru.read_state(g / "data" / twa_ru.BACKUP_DIR) is not None
        self.b_restore.state(["!disabled"] if has_backup and kind in ("patched", "clean") else ["disabled"])
        self.b_launch.state(["!disabled"])

    def set_status(self, text, kind, hint):
        self.status.configure(text=text, fg=COLORS.get(kind, "#555555"))
        self.hint.configure(text=hint)

    def run(self, fn, done_text):
        if self.busy:
            return
        g = self.game()
        if not g:
            return
        if game_running():
            messagebox.showwarning("Игра запущена", "Закройте игру и лаунчер, затем повторите.")
            return
        self.busy = True
        for b in (self.b_install, self.b_restore, self.b_launch):
            b.state(["disabled"])

        def work():
            try:
                fn(g)
                self.q.put(("done", done_text))
            except SystemExit as e:
                self.q.put(("error", str(e)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"{type(e).__name__}: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, text = self.q.get_nowait()
                if kind == "log":
                    self.say(text)
                elif kind == "done":
                    self.busy = False
                    self.say(text)
                    self.refresh()
                    messagebox.showinfo("Готово", text)
                elif kind == "error":
                    self.busy = False
                    self.say("ОШИБКА: " + text)
                    self.refresh()
                    messagebox.showerror("Не получилось", text)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    # --- действия ------------------------------------------------------------
    def install(self):
        self.say("— Установка —")
        self.run(lambda g: twa_ru.run_apply(g, TRANSLATIONS, ABBR),
                 "Русификатор установлен. Запустите игру.")

    def restore(self):
        if not messagebox.askyesno("Вернуть английский", "Восстановить оригинальные файлы игры из бэкапа?"):
            return
        self.say("— Откат —")
        self.run(lambda g: twa_ru.run_restore(g), "Оригинальные файлы восстановлены.")

    def launch(self):
        g = self.game()
        if not g:
            return
        for name in ("twa-launcher.exe", "Arena.exe"):
            exe = g / name
            if exe.exists():
                try:
                    os.startfile(str(exe))  # type: ignore[attr-defined]
                    self.say(f"Запущено: {exe}")
                except (OSError, AttributeError) as e:
                    messagebox.showerror("Не удалось запустить", str(e))
                return
        messagebox.showerror("Не найден лаунчер", "Запустите игру обычным способом.")


if __name__ == "__main__":
    App().mainloop()
