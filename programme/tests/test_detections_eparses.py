"""Detections du modele sur des mots a peine lus par l'OCR.

Sur un electrocardiogramme scanne, l'OCR lit des bouts du trace avec une
confiance de 0 a 35 %, et le modele les prenait pour des noms : des aplats noirs
au milieu des courbes. Une detection du modele qui ne repose que sur de tels
mots n'est plus masquee ; une regle, ou un nom dont un mot est bien lu, si.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.documents.caviardage import analyser_page  # noqa: E402
from caviardoc.documents.ocr import CONFIANCE_EPARS, MotOCR, PageOCR  # noqa: E402


class FauxModele:
    """Rend, pour chaque etiquette demandee, les chaines qu'on lui a confiees."""

    def __init__(self, reponses: dict[str, list[str]]):
        self.reponses = reponses

    def predict_entities(self, texte, labels, threshold=0.0):
        trouves = []
        for label in labels:
            for chaine in self.reponses.get(label, []):
                i = texte.find(chaine)
                if i >= 0:
                    trouves.append({"start": i, "end": i + len(chaine),
                                    "label": label, "score": 0.9,
                                    "text": chaine})
        return trouves


def _lecture(*mots):
    """Une ligne de mots (texte, confiance), de gauche a droite."""
    p = PageOCR(largeur=2000, hauteur=1000, dpi=300)
    pos, x = 0, 100
    for texte, confiance in mots:
        p.mots.append(MotOCR(texte=texte, x0=x, y0=100, x1=x + 30 * len(texte),
                             y1=140, confiance=confiance, bloc=0, ligne=0))
        p.offsets.append((pos, pos + len(texte)))
        pos += len(texte) + 1
        x += 30 * len(texte) + 30
    p.texte = " ".join(m[0] for m in mots)
    return p


def _masques(lecture, modele):
    page = pymupdf.open()
    page.new_page()
    _, rapport = analyser_page(modele, page[0], lu=(lecture, "OCR"))
    return {lecture.texte[sp.start:sp.end] for sp in rapport.entites}


def test_detection_sur_mots_a_peine_lus_ecartee():
    bas = CONFIANCE_EPARS - 1
    lecture = _lecture(("Zorglub", bas), ("Qwfpgj", 0), ("ECG", 95))
    masques = _masques(lecture, FauxModele(
        {"personne": ["Zorglub", "Qwfpgj"]}))
    assert not any("Zorglub" in m or "Qwfpgj" in m for m in masques)


def test_detection_avec_un_mot_bien_lu_gardee():
    lecture = _lecture(("Machin", 10), ("Truc", CONFIANCE_EPARS),
                       ("Bidule", 90), ("ECG", 95))
    masques = _masques(lecture, FauxModele(
        {"personne": ["Machin Truc", "Bidule"]}))
    assert any("Machin" in m and "Truc" in m for m in masques)
    assert any("Bidule" in m for m in masques)


def test_regle_sur_mot_a_peine_lu_gardee():
    lecture = _lecture(("ecrire", 95), ("dupont@exemple.ch", 5))
    masques = _masques(lecture, FauxModele({}))
    assert any("dupont@exemple.ch" in m for m in masques)
