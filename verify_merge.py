"""Esegue i casi di ``gui/merge_vectors.json`` contro le regole di fusione.

Lo stesso file di casi lo esegue anche l'app Android (``tool/verify_merge.dart``): e' cosi'
che le due app restano d'accordo su chi vince quando lo stesso anime e' stato guardato su
due dispositivi, invece di divergere silenziosamente col passare degli aggiornamenti. I due
verificatori sono scritti con la stessa struttura di proposito: se uno controlla meno
dell'altro, il file e' verde da una parte e rosso dall'altra, che e' il modo peggiore di
scoprire una divergenza.

Oltre al risultato atteso si verifica che **l'ordine non conti**: fondere A con B o B con A
deve dare lo stesso esito, altrimenti il risultato dipenderebbe da quale dispositivo si
sincronizza per primo.

I gruppi sono quattro:

* ``progress`` e ``favourites`` — chi vince fra due record. Con ``expect_removed`` si
  controlla anche se la riga risulta rimossa: non si legge da un campo solo, ma dal
  confronto fra i due orari, quindi va chiesto a ``is_removed``.
* ``pruning`` — la potatura delle lapidi: un archivio, un istante, e le chiavi che devono
  sopravvivere. Le righe sopravvissute devono tornare **identiche**, perche' il risultato
  della potatura e' quello che finisce nel PUT e sul disco: se si svuotassero, i casi
  resterebbero verdi mentre la cronologia sparisce.
* ``stamps`` — gli orari che la rimozione e la ripresa scrivono sul record. Stanno nel
  contratto condiviso perche' le due app devono difendersi allo stesso modo da un orologio
  sfasato: una sola delle due che lo facesse non servirebbe a niente.

Fallisce anche se nel file c'e' un gruppo che non esegue: un contratto che nessuno
controlla non e' un contratto, ed e' esattamente il modo in cui le due app divergerebbero
senza che nessuno se ne accorga.
"""

from __future__ import annotations

import json
import pathlib
import sys

from gui.merge import (
    as_tombstone,
    as_watched,
    is_removed,
    merge_favourite,
    merge_progress,
    prune_removed,
)

VETTORI = pathlib.Path(__file__).with_name("gui") / "merge_vectors.json"

# Chiavi del file che non sono gruppi di casi.
_NON_GRUPPI = ("_comment", "version")


def _uguali(uno: object, due: object) -> bool:
    """Confronto per stringa, non per valore.

    Lo stesso numero arriva dal JSON come intero o come decimale, e Python e Dart non li
    considerano uguali allo stesso modo: confrontare i tipi renderebbe rosso un lato e
    verde l'altro **sullo stesso file**, che e' precisamente cio' che questi casi
    esistono per impedire.
    """
    return str(uno) == str(due)


def _impronta(record: dict) -> str:
    """Il contenuto di un record in forma confrontabile, senza dipendere dall'ordine."""
    return "|".join(sorted(f"{k}={v}" for k, v in record.items()))


def _scostamenti(atteso: dict, ottenuto: dict, rovescio: dict | None) -> list[str]:
    problemi = []
    for chiave in atteso:
        if not _uguali(ottenuto.get(chiave), atteso[chiave]):
            problemi.append(
                f"{chiave}: atteso {atteso[chiave]!r}, ottenuto {ottenuto.get(chiave)!r}"
            )
        elif rovescio is not None and not _uguali(
            ottenuto.get(chiave), rovescio.get(chiave)
        ):
            problemi.append(
                f"{chiave}: dipende dall'ordine "
                f"({ottenuto.get(chiave)!r} contro {rovescio.get(chiave)!r})"
            )
    return problemi


def _riporta(nome: str, problemi: list[str]) -> int:
    print(f"  [{'OK  ' if not problemi else 'FAIL'}] {nome}")
    for problema in problemi:
        print(f"         {problema}")
    return 1 if problemi else 0


def esegui(gruppo: str, fondi, casi: list[dict]) -> int:
    falliti = 0
    print(f"\n=== {gruppo} ===")
    for caso in casi:
        nome = caso.get("name", "(caso senza nome)")
        atteso = caso.get("expect")
        if not isinstance(atteso, dict) or not atteso:
            falliti += _riporta(nome, ["caso senza 'expect': non verifica niente"])
            continue
        a, b = caso.get("a") or {}, caso.get("b") or {}
        ottenuto, rovescio = fondi(a, b), fondi(b, a)
        problemi = _scostamenti(atteso, ottenuto, rovescio)
        if "expect_removed" in caso:
            for esito, come in ((ottenuto, "fuso"), (rovescio, "al rovescio")):
                if is_removed(esito) != caso["expect_removed"]:
                    problemi.append(
                        f"rimossa ({come}): atteso {caso['expect_removed']}, "
                        f"ottenuto {is_removed(esito)}"
                    )
        falliti += _riporta(nome, problemi)
    return falliti


def esegui_potatura(casi: list[dict]) -> int:
    falliti = 0
    print("\n=== potatura delle lapidi ===")
    for caso in casi:
        nome = caso.get("name", "(caso senza nome)")
        archivio = caso["records"]
        ottenuto = prune_removed(archivio, caso["now"])
        chiavi, attese = sorted(ottenuto), sorted(caso["expect_keys"])
        problemi = []
        if chiavi != attese:
            problemi.append(f"sopravvissute {chiavi}, attese {attese}")
        for chiave in chiavi:
            if chiave in archivio and _impronta(ottenuto[chiave]) != _impronta(
                archivio[chiave]
            ):
                problemi.append(f"{chiave}: la riga e' tornata cambiata dalla potatura")
        falliti += _riporta(nome, problemi)
    return falliti


def esegui_orari(casi: list[dict]) -> int:
    falliti = 0
    print("\n=== orari di rimozione e ripresa ===")
    for caso in casi:
        nome = caso.get("name", "(caso senza nome)")
        record, ora = caso["record"], caso["now"]
        if caso["action"] == "remove":
            ottenuto = as_tombstone(record, ora)
        else:
            ottenuto = dict(record, updated_at=as_watched(record, ora))
        falliti += _riporta(nome, _scostamenti(caso["expect"], ottenuto, None))
    return falliti


def main() -> int:
    dati = json.loads(VETTORI.read_text(encoding="utf-8"))
    eseguiti: list[str] = []

    def gruppo(nome: str) -> list:
        """Prende un gruppo e si segna che e' stato eseguito."""
        eseguiti.append(nome)
        return dati[nome]

    falliti = esegui("punto di ripresa", merge_progress, gruppo("progress"))
    falliti += esegui("preferiti", merge_favourite, gruppo("favourites"))
    falliti += esegui_potatura(gruppo("pruning"))
    falliti += esegui_orari(gruppo("stamps"))

    gruppi = sorted(set(dati) - set(_NON_GRUPPI))
    ignorati = sorted(set(gruppi) - set(eseguiti))
    if ignorati:
        print(f"\nGRUPPI PRESENTI NEL FILE E NON ESEGUITI: {ignorati}")
        falliti += len(ignorati)

    totale = sum(len(dati[g]) for g in gruppi if isinstance(dati[g], list))
    print(f"\n{totale - falliti}/{totale} casi superati")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
