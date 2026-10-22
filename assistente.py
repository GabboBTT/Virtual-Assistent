"""Assistente per presentazioni/esami orali online (Windows).

Audio di sistema (WASAPI loopback) -> Deepgram (streaming, italiano)
-> fine domanda -> Claude (streaming) -> finestra "Note".

Uso rapido:
    python assistente.py                       # modalità reale
    python assistente.py --lista-dispositivi
    python assistente.py --debug-audio
    python assistente.py --test-wav domanda.wav
    python assistente.py --test-domanda "Perché hai scelto questo modello?"
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import re
import time
import wave
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import numpy as np

QUI = Path(__file__).resolve().parent
FILE_CONTESTO = QUI / "contesto.txt"
FILE_ENV = QUI / ".env"
FILE_PAROLE_CHIAVE = QUI / "parole_chiave.txt"

MODELLO = "claude-haiku-5-5"
MODELLO_RIASSUNTO = "claude-sonnet-5-5"   # appunti di fine sessione: qui conta la qualità, non la velocità
CARTELLA_SESSIONI = QUI / "sessioni"
MAX_TOKENS = 250
SCAMBI_IN_MEMORIA = 3
PAROLE_MINIME = 4
PAUSA_UNIONE = 3.0             # pausa massima (s) a metà domanda per unire i pezzi
MAX_PAROLE_UNITE = 80          # parole massime tenute dai pezzi precedenti
CHUNK_SECONDI = 0.1
KEEPALIVE_SECONDI = 4.0
SILENZIO_SINTETICO_MAX = 3.0   # secondi di zeri inviati dopo l'ultimo audio reale
ATTESA_STALLO = 0.3            # se non arriva audio per 0,3 s, invio silenzio

LINGUA = "en"   # lingua dell'orale: "en" o "it" (impostata da --lingua)

LINGUE = {"en": "inglese", "it": "italiano"}

# Per l'orale in inglese le istruzioni sono in inglese: con istruzioni e materiale in
# italiano il modello ogni tanto risponde in italiano.
SYSTEM_PROMPT_EN = (
    "You assist a student during a live online oral exam. You receive the automatic "
    "transcription of the examiner's question (it may contain recognition errors, e.g. "
    "garbled technical terms, and fragments of unrelated speech). "
    "ALWAYS answer in English, even though the material below may be in Italian. "
    "Answer immediately, without long reasoning: the student is waiting live. "
    "Write 2-3 short, simple sentences that are easy to read aloud, in the first person, "
    "as the student would say them. The first sentence is the direct answer. "
    "Use the real choices, numbers and results from the student's material when relevant. "
    "If the question is unclear or off-topic, say so in one line and suggest what to ask "
    "the examiner to repeat. No bullet points, no markdown."
)

SYSTEM_PROMPT = (
    "Sei un assistente per uno studente durante un esame orale online. "
    "Ricevi la trascrizione automatica (può contenere errori di riconoscimento, "
    "ad esempio termini tecnici storpiati) di una domanda del docente. "
    "Rispondi in {lingua}, con 2-3 frasi brevi e semplici da leggere ad alta voce, in "
    "prima persona, come potrebbe dirlo lo studente. La prima frase è la "
    "risposta diretta alla domanda. Se la domanda non è chiara o è fuori contesto, "
    "dillo in una sola riga e suggerisci cosa far ripetere al docente. "
    "Non usare elenchi puntati né markdown."
)

SEGNAPOSTO_CONTESTO = """\
# Incolla qui sotto il materiale dell'esercizio che presenterai.
# Le righe che iniziano con # vengono ignorate.
#
# Cosa conviene includere (testo semplice, anche lungo):
#  - il testo dell'esercizio / della consegna
#  - il codice principale e i risultati ottenuti (numeri, metriche, grafici descritti a parole)
#  - la teoria che serve per spiegarlo
#  - le scelte fatte e perché
#  - i limiti della soluzione e le alternative possibili
"""


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

def console_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def carica_env():
    """Legge .env (KEY=VALUE) senza sovrascrivere variabili già impostate."""
    if not FILE_ENV.exists():
        return
    for riga in FILE_ENV.read_text(encoding="utf-8-sig").splitlines():
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        k, v = riga.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and not os.environ.get(k):
            os.environ[k] = v


def richiedi_chiavi(*nomi):
    mancanti = [n for n in nomi if not os.environ.get(n)]
    if not mancanti:
        return True
    print("ERRORE: variabili d'ambiente mancanti: " + ", ".join(mancanti), file=sys.stderr)
    print("Impostale una volta sola (poi apri un NUOVO terminale):", file=sys.stderr)
    for n in mancanti:
        print(f'    setx {n} "la-tua-chiave"', file=sys.stderr)
    print(f"oppure scrivile nel file {FILE_ENV} come  NOME=valore", file=sys.stderr)
    return False


def carica_contesto():
    if not FILE_CONTESTO.exists():
        FILE_CONTESTO.write_text(SEGNAPOSTO_CONTESTO, encoding="utf-8")
        print(f"Creato {FILE_CONTESTO.name}: incollaci il materiale dell'esercizio.")
    testo = FILE_CONTESTO.read_text(encoding="utf-8-sig")
    righe = [r for r in testo.splitlines() if not r.lstrip().startswith("#")]
    contesto = "\n".join(righe).strip()
    return contesto


ESTENSIONI_TESTO = {".py", ".txt", ".md", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".js",
                    ".ts", ".r", ".m", ".sql", ".tex", ".yaml", ".yml", ".json", ".csv", ".sh",
                    ".go", ".rs", ".kt", ".scala", ".html", ".css", ".xml", ".toml", ".ini"}
CARTELLE_IGNORATE = {".git", ".venv", "venv", "__pycache__", "node_modules", ".ipynb_checkpoints",
                     ".idea", ".vscode"}
MAX_CARATTERI_FILE = 60_000
MAX_CARATTERI_TOTALI = 400_000   # ~100k token: oltre, le risposte rallentano troppo


def testo_notebook(percorso):
    nb = json.loads(percorso.read_text(encoding="utf-8", errors="replace"))
    parti = []
    for cella in nb.get("cells", []):
        sorgente = "".join(cella.get("source", []))
        if cella.get("cell_type") == "markdown":
            parti.append(sorgente)
        elif cella.get("cell_type") == "code":
            parti.append(f"```\n{sorgente}\n```")
            for out in cella.get("outputs", []):
                t = out.get("text") or out.get("data", {}).get("text/plain") or ""
                t = "".join(t) if isinstance(t, list) else t
                if t.strip():
                    parti.append("Output:\n" + t[:2000])
    return "\n\n".join(parti)


def testo_pdf(percorso):
    import logging
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # avvisi sui font, irrilevanti
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(percorso)).pages)


def carica_cartella(cartella):
    """Legge i file di testo, notebook e PDF della cartella (e sottocartelle)."""
    cartella = Path(cartella).expanduser().resolve()
    if not cartella.is_dir():
        raise FileNotFoundError(f"cartella non trovata: {cartella}")
    blocchi, totale, saltati = [], 0, []
    file = []
    for radice, sottocartelle, nomi in os.walk(cartella):
        # Le cartelle ignorate (es. .venv) non vengono nemmeno aperte: possono contenere
        # collegamenti che Windows non riesce a leggere.
        sottocartelle[:] = [d for d in sottocartelle if d not in CARTELLE_IGNORATE]
        file += [Path(radice) / n for n in nomi]
    for f in sorted(file):
        est = f.suffix.lower()
        try:
            if est == ".ipynb":
                testo = testo_notebook(f)
            elif est == ".pdf":
                testo = testo_pdf(f)
            elif est in ESTENSIONI_TESTO:
                testo = f.read_text(encoding="utf-8", errors="replace")
            else:
                continue
        except Exception as e:
            saltati.append(f"{f.name} ({e})")
            continue
        testo = testo.replace("\x00", "").strip()
        if not testo:
            continue
        if len(testo) > MAX_CARATTERI_FILE:
            testo = testo[:MAX_CARATTERI_FILE] + "\n[… file troncato …]"
        if totale + len(testo) > MAX_CARATTERI_TOTALI:
            saltati.append(f"{f.relative_to(cartella)} (limite di dimensione totale)")
            continue
        totale += len(testo)
        blocchi.append(f"=== FILE: {f.relative_to(cartella)} ===\n{testo}")
        print(f"  + {f.relative_to(cartella)}  ({len(testo):,} caratteri)")
    for s in saltati:
        print(f"  - saltato: {s}")
    print(f"Cartella soluzioni: {len(blocchi)} file, ~{totale // 4:,} token.")
    if not blocchi:
        print("ATTENZIONE: nessun file leggibile nella cartella (.py, .ipynb, .pdf, .txt, .md…).")
    return "\n\n".join(blocchi)


def costruisci_system(contesto, materiale=""):
    if LINGUA == "en":
        prompt = SYSTEM_PROMPT_EN
        intest_contesto, intest_materiale = "EXERCISE CONTEXT", "STUDENT'S SOLUTIONS FOLDER"
    else:
        prompt = SYSTEM_PROMPT.format(lingua=LINGUE[LINGUA])
        intest_contesto = "CONTESTO DELL'ESERCIZIO"
        intest_materiale = ("MATERIALE DALLA CARTELLA DELLE SOLUZIONI (usalo come fonte "
                            "principale: cita numeri, scelte e risultati reali presi da qui)")
    if contesto:
        prompt += f"\n\n{intest_contesto}\n{contesto}"
    if materiale:
        prompt += f"\n\n{intest_materiale}\n{materiale}"
    if LINGUA == "en":
        prompt += "\n\nReminder: always answer in English, 2-3 short sentences."
    return prompt


# ---------------------------------------------------------------------------
# Audio: WASAPI loopback
# ---------------------------------------------------------------------------

class ErroreAudio(Exception):
    pass


def trova_loopback(p):
    """Restituisce (info_uscita_predefinita, info_loopback)."""
    import pyaudiowpatch as pyaudio
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        raise ErroreAudio("WASAPI non disponibile su questo sistema.")
    idx = wasapi.get("defaultOutputDevice", -1)
    if idx is None or idx < 0:
        raise ErroreAudio("Nessun dispositivo di uscita predefinito (cuffie/casse) trovato.")
    uscita = p.get_device_info_by_index(idx)
    if uscita.get("isLoopbackDevice"):
        return uscita, uscita
    for lb in p.get_loopback_device_info_generator():
        if uscita["name"] in lb["name"]:
            return uscita, lb
    raise ErroreAudio(f"Loopback non trovato per l'uscita predefinita «{uscita['name']}».")


def lista_dispositivi():
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    try:
        print("Dispositivi audio:")
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            api = p.get_host_api_info_by_index(d["hostApi"])["name"]
            lb = " [LOOPBACK]" if d.get("isLoopbackDevice") else ""
            print(f"  {i:3d}  {d['name']}{lb}\n"
                  f"       api={api}  in={d['maxInputChannels']}  "
                  f"out={d['maxOutputChannels']}  rate={int(d['defaultSampleRate'])}")
        print()
        try:
            uscita, lb = trova_loopback(p)
            print(f"Uscita predefinita : [{uscita['index']}] {uscita['name']}")
            print(f"Loopback usato     : [{lb['index']}] {lb['name']}  "
                  f"({int(lb['defaultSampleRate'])} Hz, {lb['maxInputChannels']} canali)")
        except ErroreAudio as e:
            print(f"ERRORE: {e}")
            return 1
    finally:
        p.terminate()
    return 0


def in_mono_int16(dati, canali):
    a = np.frombuffer(dati, dtype=np.int16)
    if canali > 1:
        a = a.reshape(-1, canali).mean(axis=1)
    return a.astype(np.int16).tobytes()


class CatturaLoopback:
    """Apre il loopback e spinge chunk mono int16 (~100 ms) in un asyncio.Queue."""

    def __init__(self, loop, coda):
        self.loop, self.coda = loop, coda
        self.p = self.stream = None
        self.rate = 0
        self.nome = ""

    def apri(self):
        import pyaudiowpatch as pyaudio
        self.p = pyaudio.PyAudio()
        try:
            _, lb = trova_loopback(self.p)
            self.rate = int(lb["defaultSampleRate"])
            canali = int(lb["maxInputChannels"]) or 2
            self.nome = lb["name"]

            def callback(in_data, frame_count, time_info, status):
                # Nessun lavoro pesante qui: conversione veloce e consegna al loop.
                chunk = in_mono_int16(in_data, canali)
                self.loop.call_soon_threadsafe(self.coda.put_nowait, chunk)
                return (None, pyaudio.paContinue)

            self.stream = self.p.open(format=pyaudio.paInt16, channels=canali,
                                      rate=self.rate, input=True,
                                      input_device_index=lb["index"],
                                      frames_per_buffer=int(self.rate * CHUNK_SECONDI),
                                      stream_callback=callback)
        except Exception:
            self.chiudi()
            raise

    def chiudi(self):
        try:
            if self.stream is not None:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        try:
            if self.p is not None:
                self.p.terminate()
        except Exception:
            pass
        self.stream = self.p = None


async def leggi_wav(percorso, coda, fine):
    """Invia un wav alla coda a velocità reale (mono int16, rate nativo del file)."""
    with wave.open(str(percorso), "rb") as w:
        canali, larg, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if larg != 2:
            raise ErroreAudio(f"Il wav deve essere PCM 16 bit (questo è {larg * 8} bit). "
                              "Riesportalo come WAV 16-bit.")
        per_chunk = int(rate * CHUNK_SECONDI)
        t0 = time.monotonic()
        n = 0
        while True:
            dati = w.readframes(per_chunk)
            if not dati:
                break
            await coda.put(in_mono_int16(dati, canali))
            n += 1
            ritardo = t0 + n * CHUNK_SECONDI - time.monotonic()
            if ritardo > 0:
                await asyncio.sleep(ritardo)
    fine.set()


def rate_wav(percorso):
    with wave.open(str(percorso), "rb") as w:
        return w.getframerate()


class MisuratoreRMS:
    def __init__(self):
        self.t = time.monotonic()
        self.somma, self.campioni, self.chunk = 0.0, 0, 0

    def aggiungi(self, chunk):
        a = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
        self.somma += float(np.sum(a * a))
        self.campioni += a.size
        self.chunk += 1
        self.forse_stampa()

    def forse_stampa(self):
        ora = time.monotonic()
        if ora - self.t < 1.0:
            return
        if self.campioni:
            rms = (self.somma / self.campioni) ** 0.5
            db = 20 * np.log10(max(rms, 1e-9) / 32768)
            barra = "#" * int(max(0, min(40, (db + 70) * 40 / 70)))
            print(f"[audio] RMS {rms:8.1f}  {db:6.1f} dBFS  chunk={self.chunk:2d}  {barra}",
                  flush=True)
        else:
            print("[audio] nessun dato (WASAPI loopback non invia nulla nel silenzio)", flush=True)
        self.t = ora
        self.somma, self.campioni, self.chunk = 0.0, 0, 0


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def messaggio_errore_api(e):
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "Chiave ANTHROPIC_API_KEY non valida."
    if isinstance(e, anthropic.NotFoundError):
        return f"Modello {MODELLO} non trovato o non disponibile per questa chiave."
    if isinstance(e, anthropic.RateLimitError):
        return "Limite di richieste Anthropic raggiunto: riprova tra poco."
    if isinstance(e, anthropic.APIConnectionError):
        return "Impossibile contattare Anthropic: rete assente?"
    if isinstance(e, anthropic.APIStatusError):
        return f"Errore API Anthropic {e.status_code}: {e.message}"
    return f"Errore Anthropic: {e}"


class Risponditore:
    def __init__(self, system, on_inizio, on_token, on_fine, on_errore):
        import anthropic
        self.client = anthropic.AsyncAnthropic(max_retries=1)
        # Il system prompt (con il contesto) viene messo in cache: risposte più rapide.
        self.system = [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}]
        self.storia = []   # [(domanda, risposta)]
        self.task = None
        self.ultimo_uso = 0.0
        self.on_inizio, self.on_token = on_inizio, on_token
        self.on_fine, self.on_errore = on_fine, on_errore

    async def riscalda(self):
        """Verifica la chiave e precarica la cache del contesto. Restituisce None o errore."""
        try:
            await self.client.messages.create(model=MODELLO, max_tokens=1, system=self.system,
                                              messages=[{"role": "user", "content": "ok"}])
            self.ultimo_uso = time.monotonic()
            return None
        except Exception as e:
            return messaggio_errore_api(e)

    async def mantieni_cache(self):
        # La cache dura ~5 minuti: la rinfresco se sono stato fermo per 4.
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - self.ultimo_uso > 240 and not (self.task and not self.task.done()):
                await self.riscalda()

    def chiedi(self, domanda):
        """Nuova domanda: abbandona l'eventuale risposta in corso."""
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._rispondi(domanda))
        return self.task

    async def _rispondi(self, domanda):
        messaggi = []
        for d, r in self.storia[-SCAMBI_IN_MEMORIA:]:
            messaggi += [{"role": "user", "content": d}, {"role": "assistant", "content": r}]
        messaggi.append({"role": "user", "content": domanda})
        t0 = time.monotonic()
        primo = None
        testo = []
        self.on_inizio(domanda)
        try:
            async with self.client.messages.stream(model=MODELLO, max_tokens=MAX_TOKENS,
                                                   system=self.system,
                                                   messages=messaggi) as stream:
                async for pezzo in stream.text_stream:
                    if primo is None:
                        primo = time.monotonic() - t0
                    testo.append(pezzo)
                    self.on_token(pezzo)
            risposta = "".join(testo).strip()
            if risposta:
                self.storia = (self.storia + [(domanda, risposta)])[-SCAMBI_IN_MEMORIA:]
            self.ultimo_uso = time.monotonic()
            self.on_fine(primo, time.monotonic() - t0, risposta)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.on_errore(messaggio_errore_api(e))

    async def genera(self, system, testo, max_tokens=2000):
        """Richiesta singola senza streaming (usata per il riassunto della sessione)."""
        r = await self.client.messages.create(model=MODELLO_RIASSUNTO, max_tokens=max_tokens,
                                              system=system,
                                              messages=[{"role": "user", "content": testo}])
        return "".join(b.text for b in r.content if b.type == "text").strip()


def trova_claude_code():
    import shutil
    for c in (shutil.which("claude"), Path.home() / ".local" / "bin" / "claude.exe"):
        if c and Path(c).is_file():
            return str(c)
    return None


class RisponditoreAbbonamento(Risponditore):
    """Come Risponditore, ma passa da Claude Code (abbonamento Pro) invece che dall'API.

    Ogni domanda usa un processo `claude -p` già avviato in anticipo ("riserva"),
    così il tempo di avvio non pesa sulla latenza. Una nuova domanda uccide il
    processo della risposta precedente.
    """

    def __init__(self, system, on_inizio, on_token, on_fine, on_errore):
        import tempfile
        self.exe = trova_claude_code()
        self.cartella = Path(tempfile.mkdtemp(prefix="assistente_"))
        self.file_system = self.cartella / "system.txt"
        self.file_system.write_text(system, encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        self.riserva = None
        self.storia = []
        self.task = None
        self.ultimo_uso = 0.0
        self.on_inizio, self.on_token = on_inizio, on_token
        self.on_fine, self.on_errore = on_fine, on_errore

    async def _avvia_processo(self):
        return await asyncio.create_subprocess_exec(
            self.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose", "--model", MODELLO,
            "--system-prompt-file", str(self.file_system), "--tools", "",
            "--no-session-persistence", "--safe-mode", "--effort", "low",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.cartella), env=self.env,
            limit=16 * 1024 * 1024)

    async def _prepara_riserva(self):
        if self.riserva is None or self.riserva.returncode is not None:
            self.riserva = await self._avvia_processo()

    async def riscalda(self):
        if not self.exe:
            return ("Claude Code non trovato. Installalo (https://claude.com/claude-code) "
                    "oppure usa una chiave API.")
        p = await asyncio.create_subprocess_exec(self.exe, "auth", "status",
                                                 stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.PIPE, env=self.env)
        out, _ = await p.communicate()
        try:
            if not json.loads(out.decode("utf-8", "replace")).get("loggedIn"):
                return "Claude Code non è collegato al tuo account: esegui  claude auth login"
        except json.JSONDecodeError:
            pass
        await self._prepara_riserva()
        return None

    async def riscalda_cache(self):
        """Richiesta minima che carica il materiale nella cache di Claude: senza, la prima
        domanda vera paga qualche secondo in più."""
        proc = await self._avvia_processo()
        msg = {"type": "user", "message": {"role": "user", "content": "Rispondi solo: ok"}}
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        await proc.communicate()
        self.ultimo_uso = time.monotonic()

    async def mantieni_cache(self):
        await self.riscalda_cache()
        print("[stato] Materiale caricato nella cache di Claude", flush=True)
        while True:
            await asyncio.sleep(30)
            if self.task and not self.task.done():
                continue
            await self._prepara_riserva()
            # La cache dura ~5 minuti: la rinfresco se sono stato fermo per 4.
            if time.monotonic() - self.ultimo_uso > 240:
                await self.riscalda_cache()

    async def chiudi(self):
        await asyncio.sleep(0.2)  # lascio partire un'eventuale riserva appena richiesta
        if self.riserva and self.riserva.returncode is None:
            self.riserva.kill()
            await self.riserva.wait()

    async def _rispondi(self, domanda):
        testo_utente = domanda
        if self.storia:
            righe = []
            for d, r in self.storia[-SCAMBI_IN_MEMORIA:]:
                righe += [f"Examiner: {d}", f"Student: {r}"]
            testo_utente = ("Previous exchanges (context only):\n" + "\n".join(righe)
                            + f"\n\nNew examiner question (automatic transcription): {domanda}")
        t0 = time.monotonic()
        primo = None
        testo = []
        self.on_inizio(domanda)
        await self._prepara_riserva()
        proc, self.riserva = self.riserva, None
        try:
            msg = {"type": "user", "message": {"role": "user", "content": testo_utente}}
            proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            asyncio.create_task(self._prepara_riserva())  # la prossima è già pronta
            errore = None
            async for riga in proc.stdout:
                try:
                    ev = json.loads(riga)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "stream_event":
                    e = ev.get("event", {})
                    if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                        if primo is None:
                            primo = time.monotonic() - t0
                        testo.append(e["delta"]["text"])
                        self.on_token(e["delta"]["text"])
                elif ev.get("type") == "assistant" and ev.get("message", {}).get("model") == "<synthetic>":
                    # Messaggi generati da Claude Code stesso: login mancante, limiti, ecc.
                    errore = "".join(b.get("text", "") for b in ev["message"].get("content", []))
                elif ev.get("type") == "result" and ev.get("is_error") and not errore:
                    errore = str(ev.get("result") or ev.get("subtype") or "errore sconosciuto")
            await proc.wait()
            if errore and not testo:
                self.on_errore(f"Claude Code: {errore}")
                return
            if proc.returncode and not testo:
                err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
                self.on_errore(f"Claude Code è terminato con errore: {err[-300:] or proc.returncode}")
                return
            risposta = "".join(testo).strip()
            if risposta:
                self.storia = (self.storia + [(domanda, risposta)])[-SCAMBI_IN_MEMORIA:]
            self.ultimo_uso = time.monotonic()
            self.on_fine(primo, time.monotonic() - t0, risposta)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.on_errore(f"Claude Code: {e}")
        finally:
            if proc.returncode is None:
                proc.kill()

    async def genera(self, system, testo, max_tokens=2000):
        file_sys = self.cartella / "system_riassunto.txt"
        file_sys.write_text(system, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            self.exe, "-p", "--output-format", "json", "--model", MODELLO_RIASSUNTO,
            "--system-prompt-file", str(file_sys), "--tools", "",
            "--no-session-persistence", "--safe-mode",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.cartella), env=self.env)
        out, err = await proc.communicate(testo.encode("utf-8"))
        try:
            esito = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise RuntimeError(err.decode("utf-8", "replace").strip()[-300:] or "risposta non valida")
        if esito.get("is_error"):
            raise RuntimeError(str(esito.get("result") or esito.get("subtype")))
        return str(esito.get("result", "")).strip()


def crea_risponditore(abbonamento, system, *callback):
    classe = RisponditoreAbbonamento if abbonamento else Risponditore
    return classe(system, *callback)


# ---------------------------------------------------------------------------
# Deepgram
# ---------------------------------------------------------------------------

class ErroreDeepgramFatale(Exception):
    pass


def carica_parole_chiave():
    """Termini tecnici da parole_chiave.txt (uno per riga): Deepgram li riconosce meglio."""
    if not FILE_PAROLE_CHIAVE.exists():
        return []
    righe = FILE_PAROLE_CHIAVE.read_text(encoding="utf-8-sig").splitlines()
    return [r.strip() for r in righe if r.strip() and not r.lstrip().startswith("#")][:100]


def url_deepgram(rate):
    # DEEPGRAM_LINGUA permette una variante d'accento, es. en-IN (inglese indiano).
    variante = os.environ.get("DEEPGRAM_LINGUA", "")
    lingua = variante if variante.startswith(LINGUA) else LINGUA
    parametri = [
        ("model", "nova-2"), ("language", lingua), ("encoding", "linear16"),
        ("sample_rate", rate), ("channels", 1), ("interim_results", "true"),
        ("endpointing", 900), ("utterance_end_ms", 1500), ("smart_format", "true"),
    ]
    parametri += [("keywords", f"{p}:2") for p in carica_parole_chiave()]
    return "wss://api.deepgram.com/v1/listen?" + urlencode(parametri)


class Frasi:
    """Accumula i transcript finali e decide quando la domanda è finita.

    Se il docente riprende a parlare entro PAUSA_UNIONE secondi dalla frase precedente,
    i pezzi vengono uniti: una domanda con una pausa a metà resta una domanda sola.
    """

    def __init__(self, on_anteprima, on_domanda, on_scartata):
        self.finali = []
        self.prefisso = ""          # frase precedente da unire a quella in corso
        self.precedente = ("", 0.0)  # (testo, istante di chiusura)
        self._in_corso = False
        self.on_anteprima, self.on_domanda, self.on_scartata = on_anteprima, on_domanda, on_scartata

    def _inizio_frase(self):
        testo, t = self.precedente
        # Se il docente parla a lungo senza pause, tengo solo la parte finale (dove sta la domanda).
        testo = " ".join(testo.split()[-MAX_PAROLE_UNITE:])
        self.prefisso = testo if testo and time.monotonic() - t < PAUSA_UNIONE else ""

    def _anteprima(self, parti):
        self.on_anteprima(" ".join(([self.prefisso] if self.prefisso else []) + parti))

    def gestisci(self, msg):
        tipo = msg.get("type")
        if tipo == "Results":
            alt = msg.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "").strip()
            if alt and not self._in_corso:
                self._in_corso = True
                self._inizio_frase()
            if msg.get("is_final"):
                if alt:
                    self.finali.append(alt)
                if self.finali:
                    self._anteprima(self.finali)
                if msg.get("speech_final"):
                    self.chiudi()
            elif alt:
                self._anteprima(self.finali + [alt])
        elif tipo == "UtteranceEnd":
            self.chiudi()

    def chiudi(self):
        self._in_corso = False
        nuovo = " ".join(self.finali).strip()
        self.finali = []
        if not nuovo:
            return
        precedente = self.prefisso
        testo = f"{precedente} {nuovo}" if precedente else nuovo
        self.prefisso = ""
        self.precedente = (testo, time.monotonic())
        if len(testo.split()) < PAROLE_MINIME:
            self.on_scartata(testo)
        else:
            # `precedente` dice all'app quale domanda (incompleta) questa sostituisce.
            self.on_domanda(testo, precedente)


async def sessione_deepgram(rate, coda, frasi, stato, interrompi, rms=None):
    """Una connessione WebSocket. Ritorna quando `interrompi` è impostato o se cade."""
    import websockets

    chiave = os.environ["DEEPGRAM_API_KEY"]
    try:
        ws = await websockets.connect(url_deepgram(rate),
                                      additional_headers={"Authorization": f"Token {chiave}"},
                                      open_timeout=10, max_size=None)
    except websockets.exceptions.InvalidStatus as e:
        codice = e.response.status_code
        if codice in (401, 403):
            raise ErroreDeepgramFatale("Chiave DEEPGRAM_API_KEY non valida o senza credito.")
        raise ConnectionError(f"Deepgram ha rifiutato la connessione (HTTP {codice}).")

    # Scarto l'audio accumulato mentre ero disconnesso, se è troppo (latenza).
    while coda.qsize() > int(2 / CHUNK_SECONDI):
        coda.get_nowait()

    stato(f"In ascolto · {rate} Hz")

    async def invia():
        ultimo_audio = time.monotonic()
        ultimo_invio = time.monotonic()
        while True:
            try:
                chunk = await asyncio.wait_for(coda.get(), ATTESA_STALLO)
                if rms:
                    rms.aggiungi(chunk)
                await ws.send(chunk)
                ultimo_audio = ultimo_invio = time.monotonic()
            except asyncio.TimeoutError:
                if rms:
                    rms.forse_stampa()
                ora = time.monotonic()
                # Il loopback tace nel silenzio: invio qualche secondo di zeri così
                # Deepgram può rilevare la fine della frase (endpointing).
                if ora - ultimo_audio < SILENZIO_SINTETICO_MAX:
                    await ws.send(bytes(2 * int(rate * ATTESA_STALLO)))
                    ultimo_invio = ora
                elif ora - ultimo_invio >= KEEPALIVE_SECONDI:
                    await ws.send(json.dumps({"type": "KeepAlive"}))
                    ultimo_invio = ora

    async def ricevi():
        async for testo in ws:
            if isinstance(testo, bytes):
                continue
            try:
                frasi.gestisci(json.loads(testo))
            except json.JSONDecodeError:
                pass

    compiti = [asyncio.create_task(invia()), asyncio.create_task(ricevi()),
               asyncio.create_task(interrompi.wait())]
    try:
        fatti, _ = await asyncio.wait(compiti, return_when=asyncio.FIRST_COMPLETED)
        for t in fatti:
            if t is not compiti[2] and t.exception():
                raise t.exception()
        if not compiti[2].done():
            raise ConnectionError(f"Deepgram ha chiuso la connessione ({ws.close_code}).")
    finally:
        for t in compiti:
            t.cancel()
        try:
            await ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        await ws.close()


# ---------------------------------------------------------------------------
# Verbale della sessione (.md)
# ---------------------------------------------------------------------------

SYSTEM_RIASSUNTO = (
    "Sei un tutor che aiuta uno studente universitario italiano a rivedere una sessione "
    "d'esame orale appena conclusa. Ricevi il verbale automatico della sessione: trascrizioni "
    "(con possibili errori di riconoscimento) delle frasi del docente e le risposte suggerite "
    "da un assistente. Scrivi in italiano, in Markdown, in modo sintetico e concreto."
)

ISTRUZIONI_RIASSUNTO = """\
Leggi il verbale qui sotto e scrivi gli appunti della sessione esattamente in questo formato:

TITOLO: <titolo breve che identifica la sessione, max 8 parole, es. "Orale Adv. Cyber - DPA su AES">

### Identificazione della sessione
Di che esame/corso si tratta, quali esercizi o lab sono stati discussi (indica i file del
materiale a cui si riferiscono, se riconoscibili) e com'è andato l'orale in generale.

### Domande del docente
Elenco numerato: per ogni domanda, la domanda riformulata in modo pulito e in una riga il
punto chiave della risposta attesa.

### Punti deboli e cose da ripassare
Dove le risposte erano incerte, incomplete o dove il docente ha insistito o corretto.

### Note
Altro di utile: richieste del docente, commenti, cose da preparare per la prossima volta.
Se il verbale è troppo scarno per dire qualcosa, scrivilo onestamente.

{materiale}
--- VERBALE ---
{verbale}
"""


def ora():
    return datetime.now().strftime("%H:%M:%S")


class Verbale:
    """Scrive il verbale in tempo reale (append + flush): se il programma si chiude male,
    quello che è già successo resta salvato."""

    def __init__(self, info):
        CARTELLA_SESSIONI.mkdir(exist_ok=True)
        self.inizio = datetime.now()
        self.percorso = CARTELLA_SESSIONI / f"sessione_{self.inizio:%Y-%m-%d_%H-%M}.md"
        if self.percorso.exists():
            self.percorso = CARTELLA_SESSIONI / f"sessione_{self.inizio:%Y-%m-%d_%H-%M-%S}.md"
        righe = [f"# Sessione del {self.inizio:%d/%m/%Y}, ore {self.inizio:%H:%M}", ""]
        righe += [f"- **{k}:** {v}" for k, v in info.items() if v]
        righe += ["", "## Cronologia", ""]
        self.ora_domanda = ""
        self._scrivi("\n".join(righe) + "\n")

    def _scrivi(self, testo):
        try:
            with open(self.percorso, "a", encoding="utf-8") as f:
                f.write(testo)
        except OSError as e:
            print(f"[verbale] impossibile scrivere {self.percorso}: {e}", flush=True)

    def commento(self, testo):
        self._scrivi(f"*`{ora()}` commento breve del docente:* {testo}\n\n")

    def domanda(self, testo, completa_precedente=False):
        self.ora_domanda = ora()
        nota = " (completa la precedente)" if completa_precedente else ""
        self._scrivi(f"\n### `{self.ora_domanda}` Domanda{nota}\n\n> {testo}\n\n")

    def risposta(self, testo, primo):
        p = f"{primo:.1f} s" if primo is not None else "?"
        self._scrivi(f"**Risposta suggerita alla domanda delle {self.ora_domanda}** "
                     f"(prima riga dopo {p}):\n\n{testo}\n\n")

    def errore(self, msg):
        self._scrivi(f"*`{ora()}` errore:* {msg}\n\n")

    def fine(self):
        durata = datetime.now() - self.inizio
        self._scrivi(f"\n---\nSessione chiusa alle {ora()} (durata {str(durata).split('.')[0]}).\n")


def slug(testo):
    t = re.sub(r"[^\w\s-]", "", testo.lower(), flags=re.UNICODE)
    return re.sub(r"[\s_-]+", "-", t).strip("-")[:50]


async def riassumi_sessione(percorso, risp, elenco_materiale=""):
    """Aggiunge in testa al verbale titolo e appunti generati da Claude; rinomina il file
    col titolo. Restituisce il percorso finale."""
    percorso = Path(percorso)
    testo = percorso.read_text(encoding="utf-8")
    if "## Cronologia" in testo:
        intestazione, cronologia = testo.split("## Cronologia", 1)
    else:
        intestazione, cronologia = "", testo
    if "Domanda" not in cronologia and "commento" not in cronologia:
        print("[verbale] nessuna frase del docente registrata: niente riassunto.", flush=True)
        return percorso
    materiale = (f"Materiale dello studente (nomi dei file):\n{elenco_materiale}\n"
                 if elenco_materiale else "")
    risultato = await risp.genera(SYSTEM_RIASSUNTO, ISTRUZIONI_RIASSUNTO.format(
        materiale=materiale, verbale=cronologia[-120_000:]))
    titolo = ""
    m = re.match(r"\s*TITOLO:\s*(.+)", risultato)
    if m:
        titolo = m.group(1).strip().strip('"')
        risultato = risultato[m.end():].strip()
    righe = intestazione.rstrip().splitlines()
    if titolo and righe and righe[0].startswith("# "):
        righe[0] = f"# {titolo}"
        righe.insert(2, f"*{intestazione.splitlines()[0][2:]}*\n")
    nuovo = ("\n".join(righe) + "\n\n## Appunti della sessione\n\n" + risultato
             + "\n\n## Cronologia" + cronologia)
    percorso.write_text(nuovo, encoding="utf-8")
    if titolo and slug(titolo):
        finale = percorso.with_name(f"{percorso.stem}_{slug(titolo)}.md")
        try:
            percorso = percorso.rename(finale)
        except OSError:
            pass
    return percorso


def elenco_file_materiale(materiale):
    return "\n".join(r[len("=== FILE: "):-len(" ===")] for r in materiale.splitlines()
                     if r.startswith("=== FILE: ") and r.endswith(" ==="))


# ---------------------------------------------------------------------------
# Applicazione con finestra
# ---------------------------------------------------------------------------

class App:
    def __init__(self, ui, system, wav=None, debug_audio=False, abbonamento=False,
                 verbale=None, elenco_materiale=""):
        self.ui = ui
        self.wav = wav
        self.debug_audio = debug_audio
        self.verbale = verbale
        self.elenco_materiale = elenco_materiale
        self.loop = None
        self.riavvia = None
        self.t_domanda = 0.0
        self.risp = crea_risponditore(abbonamento, system, self._inizio, self._token,
                                      self._fine, self._errore)
        self.frasi = Frasi(self._anteprima, self._domanda, self._scartata)

    # callback (girano nel thread asyncio, parlano con la UI solo via post)
    def stato(self, s):
        self.ui.post("status", s)
        print(f"[stato] {s}", flush=True)

    def _anteprima(self, t):
        self.ui.post("anteprima", t)

    def _scartata(self, t):
        self.stato(f"Ignorata (meno di {PAROLE_MINIME} parole): «{t}»")
        if self.verbale:
            self.verbale.commento(t)

    def _domanda(self, t, sostituisce=""):
        print(f"[domanda] {t}", flush=True)
        if self.verbale:
            # "Completa la precedente" solo se il pezzo prima era una domanda, non un "Okay".
            self.verbale.domanda(t, completa_precedente=len(sostituisce.split()) >= PAROLE_MINIME)
        # Se completa una domanda spezzata, la risposta al pezzo incompleto esce dalla storia.
        if sostituisce and self.risp.storia and self.risp.storia[-1][0] == sostituisce:
            self.risp.storia.pop()
        self.t_domanda = time.monotonic()
        self.ui.post("domanda", t)
        self.risp.chiedi(t)

    def _inizio(self, d):
        self.ui.post("nuova_risposta")
        self.ui.post("status", "Sto rispondendo…")

    def _token(self, t):
        self.ui.post("token", t)

    def _fine(self, primo, totale, risposta=""):
        p = f"{primo:.1f}" if primo is not None else "?"
        self.stato(f"Risposta: prima riga dopo {p} s dalla fine domanda, completa in {totale:.1f} s")
        if self.verbale and risposta:
            self.verbale.risposta(risposta, primo)

    def _errore(self, msg):
        self.ui.post("errore", msg)
        self.stato("Errore Anthropic")
        print(f"[errore] {msg}", flush=True)
        if self.verbale:
            self.verbale.errore(msg)

    # thread asyncio
    def avvia_thread(self):
        threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True).start()

    def richiedi_riavvio_audio(self):
        if self.loop and self.riavvia:
            self.loop.call_soon_threadsafe(self.riavvia.set)

    def chiudi(self):
        """Chiamata dalla UI: restituisce un Future che finisce quando gli appunti sono salvati."""
        if not (self.verbale and self.loop):
            return None
        return asyncio.run_coroutine_threadsafe(self._chiudi_sessione(), self.loop)

    async def _chiudi_sessione(self):
        if self.risp.task and not self.risp.task.done():
            self.risp.task.cancel()
        self.verbale.fine()
        self.stato("Scrivo gli appunti della sessione…")
        try:
            percorso = await riassumi_sessione(self.verbale.percorso, self.risp,
                                               self.elenco_materiale)
            print(f"\nAppunti della sessione salvati in:\n  {percorso}\n", flush=True)
        except Exception as e:
            print(f"\n[verbale] riassunto non riuscito ({e}). La cronologia è comunque in:\n"
                  f"  {self.verbale.percorso}\n"
                  f"Puoi rigenerarlo con:  python assistente.py --riassumi \"{self.verbale.percorso}\"",
                  flush=True)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.riavvia = asyncio.Event()
        self._compiti = [asyncio.create_task(self._verifica_anthropic())]
        if self.wav:
            await self._ciclo_wav()
        else:
            await self._ciclo_live()

    async def _verifica_anthropic(self):
        # In parallelo all'avvio dell'audio: controlla la chiave e precarica la cache.
        errore = await self.risp.riscalda()
        if errore:
            self.ui.post("errore", errore)
            self.stato(f"Claude: {errore}")
        else:
            print("[stato] Claude pronto", flush=True)
        await self.risp.mantieni_cache()

    async def _ciclo_live(self):
        attesa = 1
        rms = MisuratoreRMS() if self.debug_audio else None
        while True:
            coda = asyncio.Queue()
            cattura = CatturaLoopback(self.loop, coda)
            try:
                cattura.apri()
            except Exception as e:
                self.stato(f"Audio: {e} · riprovo tra 3 s (F5 per forzare)")
                await self._pausa_o_riavvio(3)
                continue
            self.stato(f"Loopback: {cattura.nome} · connessione a Deepgram…")
            try:
                while not self.riavvia.is_set():
                    try:
                        await sessione_deepgram(cattura.rate, coda, self.frasi, self.stato,
                                                self.riavvia, rms)
                        attesa = 1
                    except ErroreDeepgramFatale as e:
                        self.ui.post("errore", str(e))
                        self.stato(str(e))
                        await self._pausa_o_riavvio(3600)
                    except Exception as e:
                        self.stato(f"Deepgram: {descrivi_errore_rete(e)} · riconnessione tra {attesa} s")
                        await self._pausa_o_riavvio(attesa)
                        attesa = min(attesa * 2, 10)
            finally:
                cattura.chiudi()
            self.riavvia.clear()
            self.stato("Riavvio cattura audio…")

    async def _pausa_o_riavvio(self, secondi):
        try:
            await asyncio.wait_for(self.riavvia.wait(), secondi)
        except asyncio.TimeoutError:
            pass

    async def _ciclo_wav(self):
        try:
            rate = rate_wav(self.wav)
        except Exception as e:
            self.ui.post("errore", f"Impossibile leggere il wav: {e}")
            return
        coda = asyncio.Queue()
        fine = asyncio.Event()
        rms = MisuratoreRMS() if self.debug_audio else None

        async def lettore():
            await asyncio.sleep(0.5)  # lascio aprire la connessione
            self.stato(f"Riproduco {Path(self.wav).name} a velocità reale…")
            try:
                await leggi_wav(self.wav, coda, fine)
            except Exception as e:
                self.ui.post("errore", str(e))
            self.stato("File wav terminato: attendo le ultime trascrizioni.")
            await asyncio.sleep(SILENZIO_SINTETICO_MAX + 2)
            self.frasi.chiudi()  # chiude un'eventuale frase rimasta aperta
            self.stato("Test wav completato. Esc per chiudere.")

        t_lettore = asyncio.create_task(lettore())
        try:
            await sessione_deepgram(rate, coda, self.frasi, self.stato, self.riavvia, rms)
        except Exception as e:
            t_lettore.cancel()
            self.ui.post("errore", f"Deepgram: {descrivi_errore_rete(e)}")
            self.stato("Errore Deepgram")


def descrivi_errore_rete(e):
    import socket
    if isinstance(e, socket.gaierror):
        return "rete assente o DNS non raggiungibile"
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return "timeout di connessione"
    return str(e) or type(e).__name__


# ---------------------------------------------------------------------------
# Modalità da console
# ---------------------------------------------------------------------------

async def test_domanda(domanda, system, abbonamento=False):
    esito = {"ok": False}

    def inizio(d):
        print(f"Domanda: {d}\n\nRisposta: ", end="", flush=True)

    def token(t):
        print(t, end="", flush=True)

    def fine(primo, totale, risposta=""):
        esito["ok"] = True
        p = f"{primo:.2f}" if primo is not None else "?"
        print(f"\n\n[prima riga dopo {p} s, completa in {totale:.2f} s]")

    def errore(msg):
        print(f"\nERRORE: {msg}", file=sys.stderr)

    r = crea_risponditore(abbonamento, system, inizio, token, fine, errore)
    if abbonamento:
        problema = await r.riscalda()
        if problema:
            print(f"ERRORE: {problema}", file=sys.stderr)
            return 1
        print("(via Claude Code / abbonamento)")
    await r.chiedi(domanda)
    if abbonamento:
        await r.chiudi()
    return 0 if esito["ok"] else 1


async def solo_audio_debug():
    """--debug-audio senza chiave Deepgram: cattura e stampa il volume."""
    coda = asyncio.Queue()
    cattura = CatturaLoopback(asyncio.get_running_loop(), coda)
    cattura.apri()
    print(f"Ascolto il loopback «{cattura.nome}» a {cattura.rate} Hz. Ctrl+C per uscire.")
    rms = MisuratoreRMS()
    try:
        while True:
            try:
                rms.aggiungi(await asyncio.wait_for(coda.get(), 0.5))
            except asyncio.TimeoutError:
                rms.forse_stampa()
    finally:
        cattura.chiudi()


def main():
    console_utf8()
    ap = argparse.ArgumentParser(description="Assistente per l'orale online.")
    ap.add_argument("--test-wav", metavar="FILE", help="usa un file wav invece del loopback")
    ap.add_argument("--test-domanda", metavar="TESTO", help="manda subito una domanda a Claude")
    ap.add_argument("--lista-dispositivi", action="store_true", help="elenca i dispositivi audio")
    ap.add_argument("--debug-audio", action="store_true", help="stampa il volume RMS ogni secondo")
    ap.add_argument("--abbonamento", action="store_true",
                    help="usa Claude Code con l'abbonamento Pro invece della chiave API "
                         "(automatico se ANTHROPIC_API_KEY non è impostata)")
    ap.add_argument("--lingua", choices=sorted(LINGUE), default=None,
                    help="lingua dell'orale: en (default) o it; anche da variabile LINGUA")
    ap.add_argument("--cartella", metavar="PERCORSO",
                    help="cartella con le soluzioni da discutere (anche da CARTELLA_SOLUZIONI)")
    ap.add_argument("--senza-verbale", action="store_true",
                    help="non salvare il verbale .md della sessione")
    ap.add_argument("--riassumi", metavar="FILE_MD",
                    help="rigenera titolo e appunti di un verbale già salvato")
    args = ap.parse_args()

    if args.lista_dispositivi:
        return lista_dispositivi()

    carica_env()
    global LINGUA
    LINGUA = args.lingua or os.environ.get("LINGUA", LINGUA).lower()
    if LINGUA not in LINGUE:
        print(f"ERRORE: lingua «{LINGUA}» non supportata (usa en o it).", file=sys.stderr)
        return 2
    print(f"Lingua dell'orale: {LINGUE[LINGUA]}")

    if args.debug_audio and not args.test_wav and not os.environ.get("DEEPGRAM_API_KEY"):
        print("DEEPGRAM_API_KEY non impostata: modalità solo audio.")
        try:
            asyncio.run(solo_audio_debug())
        except ErroreAudio as e:
            print(f"ERRORE: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            pass
        return 0

    contesto = carica_contesto()
    materiale = ""
    cartella = args.cartella or os.environ.get("CARTELLA_SOLUZIONI")
    if cartella:
        try:
            materiale = carica_cartella(cartella)
        except FileNotFoundError as e:
            print(f"ERRORE: {e}", file=sys.stderr)
            return 2
    if not contesto and not materiale:
        print(f"ATTENZIONE: {FILE_CONTESTO.name} è vuoto e nessuna cartella indicata: "
              "le risposte saranno generiche.")
    system = costruisci_system(contesto, materiale)
    abbonamento = args.abbonamento or not os.environ.get("ANTHROPIC_API_KEY")
    if abbonamento:
        print("Risposte tramite Claude Code (abbonamento), non tramite chiave API.")
        if not trova_claude_code():
            print("ERRORE: Claude Code non trovato. Installalo oppure imposta ANTHROPIC_API_KEY.",
                  file=sys.stderr)
            return 2

    if args.test_domanda:
        return asyncio.run(test_domanda(args.test_domanda, system, abbonamento))

    elenco = elenco_file_materiale(materiale)
    if args.riassumi:
        if not Path(args.riassumi).is_file():
            print(f"ERRORE: file non trovato: {args.riassumi}", file=sys.stderr)
            return 2
        risp = crea_risponditore(abbonamento, system, *[lambda *a: None] * 4)
        print("Scrivo gli appunti della sessione…")
        try:
            finale = asyncio.run(riassumi_sessione(args.riassumi, risp, elenco))
        except Exception as e:
            print(f"ERRORE: riassunto non riuscito: {e}", file=sys.stderr)
            return 1
        print(f"Appunti salvati in:\n  {finale}")
        return 0

    if not richiedi_chiavi("DEEPGRAM_API_KEY"):
        return 2
    if args.test_wav and not Path(args.test_wav).is_file():
        print(f"ERRORE: file non trovato: {args.test_wav}", file=sys.stderr)
        return 2

    verbale = None
    if not args.senza_verbale:
        verbale = Verbale({
            "Modalità": f"test con {Path(args.test_wav).name}" if args.test_wav else "live",
            "Lingua": LINGUE[LINGUA],
            "Materiale": f"{cartella} ({len(elenco.splitlines())} file)" if cartella else "",
        })
        print(f"Verbale della sessione: {verbale.percorso}")

    from ui import NoteWindow
    app = None
    ui = NoteWindow(on_close=lambda: app and app.chiudi(),
                    on_restart_audio=lambda: app and app.richiedi_riavvio_audio())
    app = App(ui, system, wav=args.test_wav, debug_audio=args.debug_audio,
              abbonamento=abbonamento, verbale=verbale, elenco_materiale=elenco)
    app.avvia_thread()
    try:
        ui.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
