"""Une meme personne garde un seul pseudonyme dans tout le texte exporte.

Cas type d'une lettre scannee (noms inventes) : l'en-tete « Monsieur TARTEMPUENZ
Quidam ne le ... » etait masque d'un seul tenant, sous l'etiquette de la
date de naissance ou du numero AVS. Le nom entier n'etait donc jamais vu en
page 1, et « Cher Quidam », plus bas, devenait PERSONNE_1 alors que
« TARTEMPUENZ Quidam », en page 2, devenait PERSONNE_4. Le nom du fichier
produit portait les deux.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents import caviardage  # noqa: E402
from caviardoc.engine import Span  # noqa: E402
from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402

#: La chaine PDF relit le document produit : sans Tesseract, elle echoue.
besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")

EN_TETE = "Madame HUERLIBRUECK Truc, nee le 07.02.1969"
PERSONNES = ("Madame HUERLIBRUECK Truc", "HUERLIBRUECK Truc", "Chère Truc")


def _lettre(chemin):
    d = pymupdf.open()
    p1 = d.new_page()
    p1.insert_text((72, 80), EN_TETE, fontsize=11)
    p1.insert_text((72, 300), "Chère Truc,", fontsize=11)
    p1.insert_text((72, 320), "Le controle est rassurant.", fontsize=11)
    p2 = d.new_page()
    p2.insert_text((72, 80), "HUERLIBRUECK Truc", fontsize=11)
    p2.insert_text((72, 120), "Aucun traitement n'est necessaire.", fontsize=11)
    d.save(chemin)


def _detection(model, texte, **kw):
    """Detecteur factice : les personnes en brut, l'en-tete fusionne d'un bloc."""
    bruts, entites = [], []
    vus: set[tuple[int, int]] = set()
    for nom in PERSONNES:
        for m in re.finditer(re.escape(nom), texte):
            bruts.append(Span(m.start(), m.end(), "personne", 0.9, m.group()))
    for m in re.finditer(re.escape(EN_TETE), texte):
        # La date de naissance, notee par une regle, donne son etiquette au
        # bloc entier : le nom du patient y disparait.
        entites.append(Span(m.start(), m.end(), "date de naissance", 1.0,
                            m.group(), "regle"))
        vus.add((m.start(), m.end()))
    for sp in bruts:
        if not any(d <= sp.start and sp.end <= f for d, f in vus) \
                and sp.text != "Madame HUERLIBRUECK Truc":
            if not any(e.start <= sp.start and sp.end <= e.end for e in entites):
                entites.append(sp)
    return entites, bruts


@besoin_ocr
def test_une_personne_un_pseudonyme(tmp_path, monkeypatch):
    monkeypatch.setattr(caviardage, "analyse_detaillee", _detection)
    _lettre(tmp_path / "lettre.pdf")
    table = TablePseudonymes()
    caviardage.caviarder_pdf(None, tmp_path / "lettre.pdf", tmp_path / "out.pdf",
                             formats=frozenset({"texte"}), table=table)
    corr = table.correspondances
    assert corr["HUERLIBRUECK Truc"] == corr["Chère Truc"]
    texte = (tmp_path / "out.txt").read_text(encoding="utf-8")
    pseudos = set(re.findall(r"\[(PERSONNE_\d+|PATIENT_\d+)\]", texte))
    assert len(pseudos) == 1, texte
