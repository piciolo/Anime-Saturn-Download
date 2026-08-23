"""Esegue i casi di ``gui/dedupe_vectors.json`` contro la fusione dei risultati.

Lo stesso file di casi lo esegue anche l'app Android (``tool/verify_dedupe.dart``): è così
che le due app restano d'accordo su quando due risultati di portali diversi sono lo stesso
anime, invece di divergere silenziosamente col passare degli aggiornamenti.
"""

from __future__ import annotations

import json
import pathlib
import sys

from gui.sources.registry import _dedupe

VETTORI = pathlib.Path(__file__).with_name("gui") / "dedupe_vectors.json"


def gruppi_prodotti(records: list[dict]) -> list[list[int]]:
    """Esegue la fusione e riporta i gruppi come indici dei record in ingresso.

    A ogni record si dà un ``source_id`` diverso, cioè il suo indice: la fusione elenca una
    voce per portale, e con identificativi distinti quell'elenco diventa esattamente il
    gruppo. Non altera ciò che si sta provando, perché il raggruppamento dipende solo dai
    nomi, mai dal portale di provenienza.
    """
    ingresso = [
        {**rec, "source_id": str(i), "source_label": rec.get("source_id", "")}
        for i, rec in enumerate(records)
    ]
    return [
        sorted(int(s["source_id"]) for s in gruppo["sources"])
        for gruppo in _dedupe(ingresso)
    ]


def main() -> int:
    casi = json.loads(VETTORI.read_text(encoding="utf-8"))["cases"]
    falliti = 0
    for caso in casi:
        atteso = [sorted(g) for g in caso["expected"]]
        ottenuto = gruppi_prodotti(caso["records"])
        ok = ottenuto == atteso
        print(f"  [{'OK  ' if ok else 'FAIL'}] {caso['name']}")
        if not ok:
            falliti += 1
            print(f"         atteso  : {atteso}")
            print(f"         ottenuto: {ottenuto}")
    print(f"\n{len(casi) - falliti}/{len(casi)} casi superati")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
