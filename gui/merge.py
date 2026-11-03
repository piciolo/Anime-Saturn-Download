"""Merge rules for reconciling watch progress and favourites between two devices.

These are pure functions with no I/O: given the record this device holds and the record
another device holds, they return the one that should win — including when the last thing
the user did was *removing* the row (see :func:`is_removed`). The rules themselves are
documented in ``merge_vectors.json``, which both this app and the Android app run as
tests — so if the two implementations ever disagree, a test fails instead of the user's
history being silently corrupted.
"""

from __future__ import annotations

import math

# Metadata is descriptive, not progress: the winner keeps its own values but borrows any
# it is missing from the loser, so a poster fetched on one device is never lost.
# ``source_id`` e' il portale di provenienza: se un dispositivo lo conosce e
# l'altro no, chi lo sa lo completa per chi non lo sa.
_META_FIELDS = ("title", "slug", "poster", "watch_path", "source_id")
_META_NUMERIC = ("total_episodes",)

# Quanto resta in giro una lapide (vedi ``prune_removed``). Una lapide ha finito il suo
# lavoro quando ogni dispositivo l'ha vista: non potendo saperlo, si tiene tre mesi.
# Passati quelli la rimozione e' dimenticata da tutti, e un dispositivo che non si
# sincronizzava da allora ha ancora la riga viva: al suo ritorno la riporta su per tutti,
# non solo per se'. E' il male minore contro un archivio che non smette mai di crescere,
# ed e' il motivo per cui il termine e' lungo.
_TOMBSTONE_TTL_S = 90 * 24 * 60 * 60

# Cio' che una lapide conserva: l'identita' della riga, e nient'altro.
_TOMBSTONE_KEEP = ("title", "source_id")


def episode_ordinal(label: object) -> float | None:
    """Return the comparable episode number, or ``None`` if it is not numeric.

    Handles ``"01"`` -> 1.0 and ``"7.5"`` -> 7.5. Labels such as ``"OVA"`` or
    ``"Speciale"`` are not orderable and return ``None``.
    """
    text = str(label or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    # NaN e infinito non sono orari: possono arrivare solo da un dato corrotto o scritto
    # a mano, e se entrassero nei confronti non ne uscirebbero piu'. NaN perde ogni
    # confronto, quindi renderebbe la fusione dipendente dall'ordine; infinito li vince
    # tutti, quindi inchioderebbe la riga per sempre. Peggio: la fusione li riscrive
    # dentro il record, ``json.dumps`` li salva come token ``NaN`` e il ``jsonDecode`` di
    # Dart li rifiuta — la cronologia diventerebbe illeggibile all'app Android. Qui
    # valgono quanto un campo assente, che e' anche cio' che fa il gemello Dart, dove
    # ``double.tryParse("nan")`` non legge nulla.
    return number if math.isfinite(number) else default


def _has_progress(record: dict) -> bool:
    return bool(str(record.get("episode_number") or "").strip())


def _removed_stamp(record: dict) -> float:
    return _to_float(record.get("removed_at"))


def _watched_stamp(record: dict) -> float:
    return _to_float(record.get("updated_at"))


def is_removed(record: dict) -> bool:
    """True when the row was removed and nothing newer has happened to it since.

    Una riga non porta un interruttore "rimossa" ma due orari: l'ultima volta che e'
    stata guardata (``updated_at``) e l'ultima volta che e' stata rimossa
    (``removed_at``). Vince il piu' recente, cosi' una rimozione fatta sul telefono e una
    ripresa fatta sul PC si ordinano fra loro senza che i due dispositivi si parlino. Un
    contatore non basterebbe: ripartirebbe da zero a ogni avvio, e la rimozione di oggi
    perderebbe contro la visione di un mese fa.

    A parita' esatta vince la riga: come per i preferiti, a parita' si sceglie di non far
    sparire niente.
    """
    return _removed_stamp(record) > _watched_stamp(record)


def _tombstone(record: dict, *, removed_at: float, updated_at: float) -> dict:
    """Reduce a record to the little that a removal needs in order to travel.

    Senza punto di ripresa (``episode_number`` e ``position_ms`` azzerati: una lapide non
    deve poter riproporre l'episodio che l'utente ha appena cancellato) e senza metadati
    (poster, slug, totale episodi: tenerli per mesi gonfierebbe il file e ogni
    sincronizzazione per righe che nessuno vede). Restano il titolo e il portale, che sono
    l'identita' della riga, e i due orari su cui si decide.
    """
    kept = {field: record[field] for field in _TOMBSTONE_KEEP if record.get(field)}
    kept.update(
        {
            "episode_number": "",
            "position_ms": 0,
            "updated_at": updated_at,
            "removed_at": removed_at,
        }
    )
    return kept


def as_tombstone(record: dict, now: float) -> dict:
    """Tombstone for a record the user has just removed on this device.

    L'orario della rimozione non e' mai piu' vecchio dell'ultima visione della riga: se
    l'orologio e' tornato indietro (fuso orario, NTP, o un altro dispositivo che ha
    scritto un orario nel futuro) un ``now`` piu' piccolo renderebbe la rimozione
    perdente in partenza, e premere la X non farebbe sparire nulla.
    """
    watched = _watched_stamp(record)
    return _tombstone(record, removed_at=max(now, watched + 1.0), updated_at=watched)


def as_watched(record: dict, now: float) -> float:
    """The ``updated_at`` to stamp on a record the user is watching right now.

    Simmetrica di ``as_tombstone``, e per la stessa ragione: un'azione dell'utente deve
    vedersi subito. Se la riga porta una rimozione con un orario nel futuro — l'ha
    scritta un dispositivo con l'orologio avanti, oppure questo e' indietro — un ``now``
    piu' piccolo la lascerebbe "rimossa" anche mentre la si guarda: non comparirebbe in
    elenco e la ripresa ripartirebbe da zero a ogni episodio, per tutto lo scarto fra i
    due orologi, senza che nessun comando dell'interfaccia possa uscirne.

    Cancellare ``removed_at`` invece di scavalcarlo non basterebbe: la lapide vive ancora
    sugli altri dispositivi e tornerebbe alla prima sincronizzazione.
    """
    return max(now, _removed_stamp(record) + 1.0)


def prune_removed(records: dict, now: float) -> dict:
    """Drop tombstones older than the TTL. Live records are returned untouched.

    Il numero di righe e' gia' limitato dagli anime che l'utente ha davvero aperto,
    perche' una rimozione non aggiunge una riga: svuota quella che c'era (vedi
    ``_tombstone``). L'unica dimensione che cresce senza fine e' quindi il tempo, e la
    chiude questa potatura: una lapide di tre mesi ha gia' raggiunto qualunque
    dispositivo che si sincronizzi, e tenerla non serve piu' a niente.

    Di proposito non c'e' nessun tetto al numero di lapidi. "Tieni solo le N piu'
    recenti" butterebbe via meta' delle rimozioni proprio quando servono tutte: subito
    dopo uno Svuota di una cronologia lunga.
    """
    kept: dict = {}
    for key, record in records.items():
        entry = record if isinstance(record, dict) else {}
        if is_removed(entry) and now - _removed_stamp(entry) > _TOMBSTONE_TTL_S:
            continue
        kept[key] = record
    return kept


def _fill_metadata(winner: dict, loser: dict) -> dict:
    """Winner's fields win, but anything empty is filled in from the loser."""
    merged = dict(winner)
    for field in _META_FIELDS:
        if not merged.get(field):
            value = loser.get(field)
            if value:
                merged[field] = value
    for field in _META_NUMERIC:
        if not _to_int(merged.get(field)):
            value = _to_int(loser.get(field))
            if value:
                merged[field] = value
    return merged


def merge_progress(a: dict, b: dict) -> dict:
    """Return whichever watch-progress record should win, with metadata filled in.

    Order of decision (first one that settles it wins):
      0. the removal, when it is the most recent thing that happened to the row;
      1. the further-along episode — a finished episode never goes backwards;
      2. same episode: the further-along position;
      3. otherwise: the more recent timestamp.
    Episode labels that are not numeric cannot be ordered, so those fall through to the
    timestamp — the one case where a wrong device clock still matters.
    """
    if not a:
        return dict(b or {})
    if not b:
        return dict(a or {})

    # I due orari della riga si fondono ciascuno per conto suo, prendendo il piu' recente
    # dei due lati: e' questo che rende il risultato indipendente dall'ordine della
    # fusione e dal numero di dispositivi. "Rimossa" non e' uno stato che si copia dal
    # vincitore, e' il confronto fra questi due orari.
    removal = max(_removed_stamp(a), _removed_stamp(b))
    watched = max(_watched_stamp(a), _watched_stamp(b))
    if removal > watched:
        # La rimozione e' piu' recente dell'ultima visione: l'altro lato non ha ripreso a
        # guardare, e' solo rimasto indietro. Il controllo sta prima delle regole sul
        # progresso, altrimenti un episodio "piu' avanti" ancora in circolazione farebbe
        # ricomparire la riga al primo giro di sincronizzazione — cioe' esattamente il
        # difetto che le lapidi esistono per chiudere.
        winner, loser = (a, b) if _removed_stamp(a) >= _removed_stamp(b) else (b, a)
        return _tombstone(
            _fill_metadata(winner, loser), removed_at=removal, updated_at=watched
        )

    winner, loser = _progress_winner(a, b)
    merged = _fill_metadata(winner, loser)
    if watched:
        # L'orario del risultato e' il piu' recente dei due anche quando vince il
        # progresso dell'altro lato. Serve perche' una rimozione piu' vecchia di una
        # visione resti perdente in tutte le fusioni successive (altrimenti la riga
        # risparirebbe al giro dopo), e intanto ordina la lista per quando l'anime e'
        # stato guardato davvero, su qualunque dispositivo.
        merged["updated_at"] = watched
    if removal:
        # Anche se ha perso, la rimozione viaggia con la riga: un terzo dispositivo che
        # ne tiene una copia non deve poterla riapplicare a un progresso piu' recente.
        merged["removed_at"] = removal
    return merged


def _progress_winner(a: dict, b: dict) -> tuple[dict, dict]:
    """Which record keeps its progress, and which one only donates metadata."""
    # A record with no episode at all is not progress; never let it erase real progress.
    a_has, b_has = _has_progress(a), _has_progress(b)
    if a_has != b_has:
        return (a, b) if a_has else (b, a)

    ord_a, ord_b = episode_ordinal(a.get("episode_number")), episode_ordinal(
        b.get("episode_number")
    )
    if ord_a is not None and ord_b is not None and ord_a != ord_b:
        return (a, b) if ord_a > ord_b else (b, a)

    if ord_a is not None and ord_b is not None:  # same episode: further position wins
        pos_a, pos_b = _to_int(a.get("position_ms")), _to_int(b.get("position_ms"))
        if pos_a != pos_b:
            return (a, b) if pos_a > pos_b else (b, a)

    # Not comparable (or identical): the most recent write wins.
    if _watched_stamp(a) >= _watched_stamp(b):
        return (a, b)
    return (b, a)


def merge_favourite(a: dict, b: dict) -> dict:
    """Return the favourite record that should win: the user's most recent decision.

    On an exact tie the conservative state wins (it stays a favourite), so a clock
    collision can never silently drop something from the list.
    """
    if not a:
        return dict(b or {})
    if not b:
        return dict(a or {})
    ts_a, ts_b = _to_float(a.get("toggled_at")), _to_float(b.get("toggled_at"))
    if ts_a == ts_b:
        winner, loser = (a, b) if a.get("is_favourite") else (b, a)
    else:
        winner, loser = (a, b) if ts_a > ts_b else (b, a)
    return _fill_metadata(winner, loser)
