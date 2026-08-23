"""Esegue i casi di ``gui/merge_vectors.json`` contro le regole di fusione.

Lo stesso file di casi lo esegue anche l'app Android (``tool/verify_merge.dart``): e' cosi'
che le due app restano d'accordo su chi vince quando lo stesso anime e' stato guardato su
due dispositivi, invece di divergere silenziosamente col passare degli aggiornamenti.

Oltre al risultato atteso si verifica che **l'ordine non conti**: fondere A con B o B con A
deve dare lo stesso esito, altrimenti il risultato dipenderebbe da quale dispositivo si
sincronizza per primo.
"""

from __future__ import annotations

import json
import pathlib
import sys

from gui.merge import merge_favourite, merge_progress

VETTORI = pathlib.Path(__file__).with_name("gui") / "merge_vectors.json"


def esegui(gruppo: str, fondi, casi: list[dict]) -> int:
    falliti = 0
    print(f"\n=== {gruppo} ===")
    for caso in casi:
        a, b, atteso = caso.get("a", {}), caso.get("b", {}), caso["expect"]
        ottenuto = fondi(a, b)
        rovescio = fondi(b, a)

        sbagliati = [k for k in atteso if str(ottenuto.get(k)) != str(atteso[k])]
        asimmetrici = [k for k in atteso if str(ottenuto.get(k)) != str(rovescio.get(k))]

        ok = not sbagliati and not asimmetrici
        print(f"  [{'OK  ' if ok else 'FAIL'}] {caso['name']}")
        if sbagliati:
            falliti += 1
            for k in sbagliati:
                print(f"         {k}: atteso {atteso[k]!r}, ottenuto {ottenuto.get(k)!r}")
        elif asimmetrici:
            falliti += 1
            for k in asimmetrici:
                print(
                    f"         {k}: dipende dall'ordine "
                    f"({ottenuto.get(k)!r} contro {rovescio.get(k)!r})"
                )
    return falliti


def main() -> int:
    dati = json.loads(VETTORI.read_text(encoding="utf-8"))
    falliti = esegui("punto di ripresa", merge_progress, dati["progress"])
    falliti += esegui("preferiti", merge_favourite, dati["favourites"])
    totale = len(dati["progress"]) + len(dati["favourites"])
    print(f"\n{totale - falliti}/{totale} casi superati")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
