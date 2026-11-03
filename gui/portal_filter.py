"""A quale portale appartiene una voce salvata, e quali voci mostrare.

Gemello di ``lib/data/sources/portal_filter.dart`` dell'app Android: stessa
regola, provata dalle due parti sugli stessi casi
(``gui/portal_filter_vectors.json``, copia identica di
``flutter_app/assets/portal_filter_vectors.json``).

Preferiti e cronologia restano **un archivio solo**, condiviso dai portali e
dalla sincronizzazione: scegliere un portale non sposta e non cancella nulla.
Qui si decide soltanto *cosa mostrare*, e la distinzione è tutto — nascondere
una voce si annulla cambiando portale, cancellarla no.
"""

from __future__ import annotations

from typing import Iterable

#: Il portale delle voci salvate quando l'app ne aveva uno solo.
#:
#: Quelle voci non hanno ``source_id`` perché all'epoca non c'era niente da
#: distinguere: venivano tutte da AnimeSaturn. Senza questo ripiego sarebbero
#: invisibili sotto **entrambi** i portali, cioè la cronologia di chi usa le app
#: da più tempo sparirebbe del tutto — ed è proprio la cronologia più lunga,
#: quella che vale di più.
LEGACY_PORTAL = "animesaturn"


def entry_portal(entry: dict) -> str:
    """Il portale di una voce: quello scritto dentro, o :data:`LEGACY_PORTAL`.

    Il campo si legge **come testo**, non per verità logica. Un ``source_id``
    che valesse ``0`` è falso in Python ma non è un campo vuoto, e l'app Android
    lo leggerebbe come la stringa ``"0"``: trattarlo qui come assente farebbe
    divergere le due liste. È il caso che il file dei vettori fissa apposta.
    """
    grezzo = entry.get("source_id")
    testo = "" if grezzo is None else str(grezzo)
    return testo.strip() or LEGACY_PORTAL


def entries_for_portal(entries: Iterable[dict], portal: str | None) -> list[dict]:
    """Le voci da mostrare con il portale ``portal``; con ``None``, tutte.

    ``None`` non significa «nessuna»: è lo stato di chi non ha ancora scelto.
    Lì l'elenco resta completo come è sempre stato.

    Restituisce sempre una lista nuova, anche quando non filtra niente: chi la
    riceve la ordina, e ordinare la collezione dell'archivio al posto suo
    riscriverebbe i dati di sotto.
    """
    if portal is None:
        return list(entries)
    return [e for e in entries if entry_portal(e) == portal]


def entries_outside_portal(entries: Iterable[dict], portal: str | None) -> int:
    """Quante voci restano fuori dall'elenco con il portale ``portal``.

    Serve a dirlo in chiaro all'utente. Un elenco che si accorcia senza
    spiegazioni si legge come una perdita di dati, ed è la paura giusta da
    togliere subito: quelle voci ci sono ancora, sono solo altrove.
    """
    if portal is None:
        return 0
    return sum(1 for e in entries if entry_portal(e) != portal)
