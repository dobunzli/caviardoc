"""Formats ecrits courants des identifiants suisses (audit : lacunes de la couche 1)."""

import pytest

from caviardoc.detect.rules import find_all

NB, NN = " ", " "
AVS = "756.1234.5678.97"
IBAN = "CH93 0076 2011 6238 5295 7"
#: 20 chiffres, cle de Luhn valide (numero factice).
CARTE20 = "80756123456789012347"


@pytest.mark.parametrize("texte, label", [
    ("Tel +41 (0)21 555 12 34 merci", "numero de telephone"),
    ("Tel 021/555 12 34 merci", "numero de telephone"),
    ("Tel 021 / 555 12 34 merci", "numero de telephone"),
    (f"Tel +41{NB}21{NB}555{NB}12{NB}34 merci", "numero de telephone"),
    (f"Tel 021{NN}555{NN}12{NN}34 merci", "numero de telephone"),
    ("Tel +41 21 555 12 34 merci", "numero de telephone"),
    (f"IBAN {IBAN.replace(' ', NB)} ok", "IBAN"),
    (f"IBAN {IBAN.replace(' ', NN)} ok", "IBAN"),
    (f"IBAN {IBAN} ok", "IBAN"),
    ("AVS 756-1234-5678-97 ok", "numero AVS"),
    (f"AVS 756{NB}1234{NB}5678{NB}97 ok", "numero AVS"),
    (f"AVS 756{NN}1234{NN}5678{NN}97 ok", "numero AVS"),
    (f"AVS {AVS} ok", "numero AVS"),
])
def test_format_detecte(texte, label):
    assert label in {m.label for m in find_all(texte)}


def test_carte_20_chiffres():
    c = CARTE20
    assert len(c) == 20
    for sep in ("", " ", "-", NB, NN):
        txt = f"Carte {sep.join(c[i:i+4] for i in range(0, 20, 4))} fin"
        assert "numero de carte" in {m.label for m in find_all(txt)}, sep


def test_carte_20_chiffres_luhn_invalide_ignoree():
    c = CARTE20
    faux = c[:-1] + str((int(c[-1]) + 1) % 10)
    assert "numero de carte" not in {m.label for m in find_all(f"Carte {faux} fin")}


@pytest.mark.parametrize("texte", [
    "Le 12.03.2024 nous avons paye 1 234.50 francs.",
    "Rendez-vous a 14 h 30 pour 3 patients.",
    "Facture 2024/0012 du 5 mai.",
    "Total 021 555 12 pieces.",
    "AVS 756-1234-5678-98 invalide",
    f"IBAN CH94{NB}0076{NB}2011{NB}6238{NB}5295{NB}7 invalide",
    "Code 12345678901234567890123 sans sens",
])
def test_negatifs(texte):
    labels = {m.label for m in find_all(texte)}
    assert not labels & {"numero de telephone", "IBAN", "numero AVS", "numero de carte"}
