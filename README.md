<p align="center">
  <img src="assets/logo.png" width="110" alt="AnimeSaturn Downloader">
</p>

<h1 align="center">AnimeSaturn Downloader</h1>

<p align="center">
  App <b>desktop</b> per cercare e scaricare anime da
  <a href="https://www.animesaturn.net">AnimeSaturn</a> — tutto a click,
  <b>senza terminale e senza comandi Python</b>.
</p>

<p align="center">
  <sub><i>A desktop GUI to search, browse and download anime episodes from AnimeSaturn — no command line, Windows <code>.exe</code> included.</i></sub>
</p>

---

## ✨ Caratteristiche

- 🔎 **Ricerca** anime per titolo, collegata direttamente al sito.
- 🔥 **Sfoglia** il catalogo: *In corso*, *Ultimi aggiunti*, *Catalogo* — con
  ordinamento (rilevanza, ultime aggiunte, A–Z).
- 🇮🇹 Filtro **DUB (ITA)** per i soli anime doppiati in italiano.
- 📺 Scheda anime con **trama, copertina e lista episodi**.
- ✅ Selezione episodi: **singoli**, **tutti** o **intervallo** (es. dal 5 al 12).
- 📥 **Coda di download** con barra di avanzamento, velocità, download **simultanei**
  regolabili, **annulla** immediato e **cartella personalizzabile**.
- ⏯️ **Ripresa dei download interrotti**: se un download viene interrotto, riparte da
  dove si era fermato invece che da capo. Nella lista episodi vedi quali sono già
  scaricati (✓) e quali sono da completare (⏸).
- 💾 File salvati in modo ordinato: `Nome Anime - Ep 01.mp4`.

## ⬇️ Installazione e avvio

### Avvio con doppio clic (dal codice)

Fai doppio clic su **`Avvia AnimeSaturn.bat`** (avvia l'app senza finestra del
terminale). Richiede Python con le dipendenze installate (vedi sotto).

### Da sorgente

```bash
pip install -r requirements.txt
python app.py
```

## 🕹️ Come si usa

1. All'avvio vedi già gli anime **In corso**.
2. Scrivi un titolo nella barra e premi **Invio** (o **Cerca**). Puoi cambiare
   l'ordinamento e attivare **Solo DUB (ITA)**.
3. Clicca su una copertina per aprire la **scheda dell'anime**.
4. Spunta gli episodi desiderati. Per fare presto usa **Seleziona tutti** oppure imposta
   **Dal … al …** e premi **Seleziona intervallo**.
5. Premi **⬇ Scarica selezionati**: passi automaticamente alla scheda **Download**.
6. Nella scheda **Download** segui l'avanzamento; puoi **annullare** un download in corso
   o **rimuovere** quelli finiti, e regolare i download **simultanei**.

## 💾 Dove finiscono i file

I video vengono salvati nella cartella di download scelta (modificabile dall'app),
dentro una sottocartella con il nome dell'anime, ad esempio:

```
<cartella scelta>/
└── The Promised Neverland/
    ├── The Promised Neverland - Ep 01.mp4
    └── The Promised Neverland - Ep 02.mp4
```

Riavviando un download già presente, l'episodio viene riconosciuto e saltato.

## 🔨 Creare l'eseguibile

Per generare tu stesso il `.exe` (serve solo la prima volta):

```bash
pip install pyinstaller
python build_exe.py
```

Al termine trovi **`dist/AnimeSaturn Downloader.exe`**: un singolo file portabile,
lanciabile con un doppio clic anche su PC senza Python.

> Per un'icona personalizzata, metti un file `assets/logo.ico` prima di lanciare
> `build_exe.py`.

## ⚙️ Come funziona (in breve)

L'app è autonoma (pacchetto `gui/` + `app.py`) e comunica direttamente con AnimeSaturn
tramite scraping delle pagine (il sito non espone un'API pubblica):

- ricerca/catalogo tramite la pagina `/filter` (e `/ongoing`, `/newest`);
- lista episodi estratta dalla pagina dell'anime;
- link diretto al file `.mp4` risolto dalla pagina di visione: la pagina espone un
  player firmato `play.saturncdn.net/embed/<id>`, la cui *playlist* restituisce la
  sorgente offuscata (base64 + XOR con il token) che viene decodificata nel `.mp4`.

I download vengono eseguiti in thread separati per non bloccare l'interfaccia, con
ripresa automatica tramite richieste HTTP `Range`.

## 📦 Requisiti

- Per l'**eseguibile**: nessuno (Windows 64 bit).
- Per l'**avvio da sorgente**: Python 3.10+ con `PySide6` e `httpx`
  (`pip install -r requirements.txt`).

## ⚠️ Note

Strumento pensato per **uso personale**. Rispetta le leggi sul diritto d'autore e i
termini di servizio del sito: scarica solo contenuti per cui hai i relativi diritti.
