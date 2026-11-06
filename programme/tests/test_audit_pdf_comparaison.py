"""Audit de securite, piste 2 : la comparaison replie Unicode et tirets.

Propagation, correction et verification comparaient des textes sans blancs et
sans casse, rien de plus : une cesure de fin de ligne, un trait d'union
conditionnel, un caractere de largeur nulle ou un accent decompose (colle
depuis macOS) suffisaient a laisser un identifiant en clair, annonce verifie.
"""

from __future__ import annotations

import io
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc.documents.commun import (occurrences_relues,  # noqa: E402
                                        propager_chaines)
from caviardoc.engine import Span  # noqa: E402

CONNUES = {"Zorglub-Quidam": "personne"}


@pytest.mark.parametrize("texte", [
    "Zorglub-\nQuidam",              # cesure de fin de ligne
    "ZorglubQuidam",                 # tiret absent
    "Zor­glub-Quidam",          # trait d'union conditionnel
    "Zorglub‑Quidam",           # trait d'union insecable
    "Zorg​lub-Quidam",          # espace de largeur nulle
    "Zorg‍lub-Quidam",          # joigneur de largeur nulle
    "Zorglub‐Quidam",           # tiret typographique
])
def test_propagation_replie_tirets_et_caracteres_invisibles(texte):
    phrase = f"Vu en consultation : {texte} ce jour."
    ajouts = propager_chaines(phrase, [], CONNUES)
    assert len(ajouts) == 1
    assert phrase[ajouts[0].start].lower() == "z"
    assert phrase[ajouts[0].end - 1] == "m"
    assert len(occurrences_relues(phrase, CONNUES)) == 1


@pytest.mark.parametrize("forme", ["NFC", "NFD"])
def test_propagation_replie_les_accents_decomposes(forme):
    nom = "Hélène Quidam"
    texte = unicodedata.normalize(forme, f"Vue ce jour : {nom}, stable.")
    for connues in ({unicodedata.normalize("NFC", nom): "personne"},
                    {unicodedata.normalize("NFD", nom): "personne"}):
        ajouts = propager_chaines(texte, [], connues)
        # Le nom entier est couvert, ses composants aussi.
        assert nom in {texte[a.start:a.end] for a in ajouts} or \
            unicodedata.normalize("NFD", nom) in {texte[a.start:a.end] for a in ajouts}


def test_apostrophes_typographiques():
    ajouts = propager_chaines("Chez M. D’Quidam ce jour.", [],
                              {"D'Quidam": "personne"})
    assert len(ajouts) == 1


def test_la_verification_utilise_le_meme_repli():
    from caviardoc.pipeline import replier
    assert replier("Zorglub-\nQuidam") == replier("ZORGLUB­QUIDAM")
    assert replier("é") == replier("é")
