"""Esegue i casi di ``gui/portal_filter_vectors.json`` contro ``gui/portal_filter.py``.

Lo stesso file di casi lo esegue anche l'app Android
(``tool/verify_portal_filter.dart``): è così che le due app restano d'accordo su
quali voci salvate mostrare per ogni portale, invece di divergere in silenzio col
passare degli aggiornamenti.

Il caso che pesa più di tutti è la voce senza ``source_id``: viene da quando le
app avevano un portale solo, quindi vale AnimeSaturn. Sbagliato quel ripiego, la
cronologia più lunga diventerebbe invisibile sotto *entrambi* i portali e si
leggerebbe come cancellata.

Uso: python verify_portal_filter.py
"""

from __future__ import annotations

import json
import pathlib
import sys

from gui.portal_filter import entries_for_portal, entries_outside_portal

VETTORI = pathlib.Path(__file__).with_name("gui") / "portal_filter_vectors.json"


def main() -> int:
    vettori = json.loads(VETTORI.read_text(encoding="utf-8"))
    falliti = 0
    prove = 0

    print("la regola, voce per voce:")
    for caso in vettori["entry"]:
        atteso = caso["expect"]
        ottenuto = len(entries_for_portal([caso["entry"]], caso["portal"])) == 1
        ok = ottenuto == atteso
        prove += 1
        if not ok:
            falliti += 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {caso['name']}")
        if not ok:
            print(f"         voce={caso['entry']} portale={caso['portal']}")
            print(f"         atteso  : {'in elenco' if atteso else 'fuori'}")
            print(f"         ottenuto: {'in elenco' if ottenuto else 'fuori'}")

    print("\nsull'insieme:")
    for caso in vettori["set"]:
        voci = caso["entries"]
        portale = caso["portal"]
        atteso = caso["expect"]
        mostrate = len(entries_for_portal(voci, portale))
        fuori = entries_outside_portal(voci, portale)
        ok = mostrate == atteso["shown"] and fuori == atteso["outside"]
        prove += 1
        if not ok:
            falliti += 1
        print(f"  [{'OK  ' if ok else 'FAIL'}] {caso['name']}")
        if not ok:
            print(f"         atteso  : mostrate={atteso['shown']} fuori={atteso['outside']}")
            print(f"         ottenuto: mostrate={mostrate} fuori={fuori}")

        # Vale per ogni caso, non solo per quelli scritti: ciò che si mostra e
        # ciò che resta fuori sono le due parti dello stesso archivio, e se non
        # tornano una voce è stata contata due volte o persa per strada.
        prove += 1
        if mostrate + fuori != len(voci):
            falliti += 1
            print(f"  [FAIL] mostrate + fuori != {len(voci)} ({caso['name']})")

    # Chi riceve la lista la ordina. Se non fosse una copia, ordinarla
    # riscriverebbe l'ordine dentro l'archivio.
    archivio = [
        {"title": "a", "source_id": "animesaturn"},
        {"title": "b", "source_id": "animeunity"},
    ]
    entries_for_portal(archivio, None).clear()
    prove += 1
    if len(archivio) != 2:
        falliti += 1
        print("\n  [FAIL] senza filtro restituisce l'archivio stesso, non una copia")

    print()
    print("tutto a posto" if falliti == 0 else f"{falliti} prove su {prove} fallite")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
