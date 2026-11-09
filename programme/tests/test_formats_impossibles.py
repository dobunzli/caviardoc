"""Detections du modele qui ne peuvent pas avoir le format de leur etiquette.

Sur un electrocardiogramme scanne, des bouts de legende d'un a trois
caracteres sortaient en « numero AVS ». Les numeros ci-dessous sont inventes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.engine import Origine, Span  # noqa: E402
from caviardoc.pipeline import analyse, filtrer_formats_impossibles  # noqa: E402

TEXTE = ("25 mm/s 0.30-25 Hz F50 AT-1 1.1\n"
         "No AVS 756 1234 5678 97, tel. 021 555 01 23\n"
         "IBAN CH93 0076 2011 6238 5295 7, ecrire a jean.exemple@hopital.ch")


def _span(sous, label, origine=Origine.MODELE.value, n=0):
    i = -1
    for _ in range(n + 1):
        i = TEXTE.index(sous, i + 1)
    return Span(i, i + len(sous), label, 0.2, sous, origine)


@pytest.mark.parametrize("sous, label", [
    ("1.1", "numero AVS"),
    ("25", "numero AVS"),
    ("F50", "numero de carte"),
    ("AT-1", "IBAN"),
    ("Hz", "email"),
    ("0.30-25", "numero de telephone"),
])
def test_fragment_trop_court_pour_le_format_ecarte(sous, label):
    assert filtrer_formats_impossibles(TEXTE, [_span(sous, label)]) == []


@pytest.mark.parametrize("sous, label", [
    ("756 1234 5678 97", "numero AVS"),
    ("021 555 01 23", "numero de telephone"),
    ("CH93 0076 2011 6238 5295 7", "IBAN"),
    ("jean.exemple@hopital.ch", "email"),
])
def test_numero_complet_garde(sous, label):
    sp = _span(sous, label)
    assert filtrer_formats_impossibles(TEXTE, [sp]) == [sp]


def test_morceau_d_un_numero_en_groupes_garde():
    # Le modele n'a pris qu'un groupe : c'est le numero entier qui compte.
    sp = _span("1234", "numero AVS")
    assert filtrer_formats_impossibles(TEXTE, [sp]) == [sp]


def test_regle_jamais_ecartee():
    sp = _span("1.1", "numero AVS", origine=Origine.REGLE.value)
    assert filtrer_formats_impossibles(TEXTE, [sp]) == [sp]


def test_autres_etiquettes_non_touchees():
    sp = _span("Hz", "personne")
    assert filtrer_formats_impossibles(TEXTE, [sp]) == [sp]


class FauxModele:
    def predict_entities(self, texte, labels, threshold=0.0):
        i = texte.find("1.1")
        return [{"start": i, "end": i + 3, "label": "numero AVS",
                 "score": 0.19, "text": "1.1"}] if "numero AVS" in labels else []


def test_analyse_ne_masque_plus_le_fragment():
    spans = analyse(FauxModele(), TEXTE, normalise_case=False)
    assert not any(TEXTE[sp.start:sp.end] == "1.1" for sp in spans)
