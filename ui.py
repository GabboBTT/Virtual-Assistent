"""Finestra "Note": tkinter, aggiornata solo dal thread principale.

Gli altri thread non toccano mai tkinter: chiamano NoteWindow.post(...),
che mette un messaggio in una queue.Queue letta ogni 50 ms con root.after.
"""

import queue
import time
import tkinter as tk

BG = "#16181d"
FG = "#f2f2f2"
FG_STATUS = "#8a8f98"
FG_DOMANDA = "#7cc4ff"
FG_ANTEPRIMA = "#4f7ea6"
FG_ERRORE = "#ff7b72"

FONT_MIN, FONT_MAX = 12, 60
CHIUSURA_MAX = 120  # secondi massimi di attesa per gli appunti di fine sessione


class NoteWindow:
    def __init__(self, on_close=None, on_restart_audio=None):
        self._queue = queue.Queue()
        self._on_close = on_close
        self._on_restart_audio = on_restart_audio
        self._font_size = 24
        self._chiusura = None

        self.root = tk.Tk()
        self.root.title("Note")
        self.root.geometry("900x420")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)

        self.status = tk.Label(self.root, text="Avvio…", bg=BG, fg=FG_STATUS,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(6, 0))

        self.domanda = tk.Label(self.root, text="", bg=BG, fg=FG_DOMANDA,
                                font=("Segoe UI", 14), anchor="w", justify="left",
                                wraplength=870)
        self.domanda.pack(fill="x", padx=12, pady=(4, 6))

        self.risposta = tk.Text(self.root, bg=BG, fg=FG, wrap="word", bd=0,
                                highlightthickness=0, padx=12, pady=4,
                                font=("Segoe UI", self._font_size),
                                insertbackground=BG, cursor="arrow")
        self.risposta.tag_configure("errore", foreground=FG_ERRORE)
        self.risposta.pack(fill="both", expand=True)
        self.risposta.configure(state="disabled")

        self.root.bind("<Configure>", self._on_resize)
        self.root.bind("<Escape>", lambda e: self.close())
        self.root.bind("<Control-Up>", lambda e: self._zoom(+2))
        self.root.bind("<Control-Down>", lambda e: self._zoom(-2))
        self.root.bind("<F5>", lambda e: self._restart_audio())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self._poll)

    # --- API thread-safe -------------------------------------------------
    def post(self, kind, text=""):
        """kind: status | anteprima | domanda | nuova_risposta | token | errore"""
        self._queue.put((kind, text))

    # --- solo thread principale ------------------------------------------
    def mainloop(self):
        self.root.mainloop()

    def close(self):
        # Secondo Esc durante il salvataggio: chiudo subito.
        if self._chiusura is not None:
            self.root.destroy()
            return
        futuro = self._on_close() if self._on_close else None
        if futuro is None:
            self.root.destroy()
            return
        # Aspetto che gli appunti della sessione siano scritti (max CHIUSURA_MAX secondi).
        self._chiusura = (futuro, time.monotonic())
        self.status.configure(text="Salvo gli appunti della sessione… (Esc di nuovo per uscire subito)")
        self._attendi_chiusura()

    def _attendi_chiusura(self):
        futuro, t0 = self._chiusura
        if futuro.done() or time.monotonic() - t0 > CHIUSURA_MAX:
            self.root.destroy()
        else:
            self.root.after(200, self._attendi_chiusura)

    def _restart_audio(self):
        if self._on_restart_audio:
            self._on_restart_audio()

    def _zoom(self, delta):
        self._font_size = max(FONT_MIN, min(FONT_MAX, self._font_size + delta))
        self.risposta.configure(font=("Segoe UI", self._font_size))

    def _on_resize(self, event):
        if event.widget is self.root:
            self.domanda.configure(wraplength=max(200, event.width - 30))

    def _append(self, text, tag=None):
        self.risposta.configure(state="normal")
        self.risposta.insert("end", text, tag)
        self.risposta.see("end")
        self.risposta.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, text = self._queue.get_nowait()
                if kind == "status":
                    self.status.configure(text=text)
                elif kind == "anteprima":
                    self.domanda.configure(text=text + " …", fg=FG_ANTEPRIMA)
                elif kind == "domanda":
                    self.domanda.configure(text=text, fg=FG_DOMANDA)
                elif kind == "nuova_risposta":
                    self.risposta.configure(state="normal")
                    self.risposta.delete("1.0", "end")
                    self.risposta.configure(state="disabled")
                elif kind == "token":
                    self._append(text)
                elif kind == "errore":
                    self._append(("\n" if self.risposta.get("1.0", "end").strip() else "")
                                 + text, "errore")
        except queue.Empty:
            pass
        self.root.after(50, self._poll)
