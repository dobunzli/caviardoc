"""Le texte produit ne garde pas ce que le PDF noircit.

Le cas qui l'a montre : un compte rendu dont le logo — « Reseau Hospitalier
Lacustre » — et le nom du service, compose a cote, etaient absents du PDF
mais bien lisibles dans le fichier ``.txt``. Les zones (logos, blocs d'en-tete)
ne noircissaient que des pixels ; le texte, assemble a part, ne les connaissait
pas. Et le nom du service, sans adresse ni entite reconnue, n'etait meme pas
pris pour un en-tete.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pytesseract")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents.caviardage import caviarder_pdf  # noqa: E402
from caviardoc.documents.zones import OptionsZones  # noqa: E402

pytestmark = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire un logo demande Tesseract et la langue francaise")

CORPS = ["Echocardiographie dans les normes.",
         "Le ventricule gauche n'est pas dilate et sa fonction est conservee.",
         "Aucun epanchement pericardique n'est visible sur les coupes."]


def _logo():
    image = Image.new("RGB", (400, 530), "white")
    dessin = ImageDraw.Draw(image)
    fonte = ImageFont.load_default(size=44)
    for i, ligne in enumerate(["Reseau", "Hospitalier", "Lacustre"]):
        dessin.text((20, 60 + i * 90), ligne, fill="black", font=fonte)
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return tampon.getvalue()


def _compte_rendu(chemin):
    d = pymupdf.open()
    page = d.new_page()
    page.insert_image(pymupdf.Rect(40, 30, 113, 127), stream=_logo())
    page.insert_text((120, 75), "Departement de medecine", fontsize=9)
    page.insert_text((120, 88), "Service de cardiologie", fontsize=9)
    for i, ligne in enumerate(CORPS):
        page.insert_text((72, 300 + i * 18), ligne, fontsize=11)
    d.save(chemin)


@pytest.mark.parametrize("formats", [frozenset({"pdf", "texte"}),
                                     frozenset({"texte"})])
def test_le_texte_perd_le_logo_et_le_nom_du_service(tmp_path, formats):
    _compte_rendu(tmp_path / "cr.pdf")
    caviarder_pdf(None, tmp_path / "cr.pdf", tmp_path / "out.pdf",
                  zones=OptionsZones(logos=True, entetes=True), formats=formats)
    texte = (tmp_path / "out.txt").read_text(encoding="utf-8").lower()
    for reste in ("hospitalier", "lacustre", "departement", "cardiologie"):
        assert reste not in texte, f"« {reste} » subsiste dans le texte"
    assert "echocardiographie dans les normes" in texte


def test_sans_les_options_le_texte_reste_intact(tmp_path):
    """Les zones sont a la demande : sans elles, rien n'est retire."""
    _compte_rendu(tmp_path / "cr.pdf")
    caviarder_pdf(None, tmp_path / "cr.pdf", tmp_path / "out.pdf",
                  zones=OptionsZones(), formats=frozenset({"texte"}))
    texte = (tmp_path / "out.txt").read_text(encoding="utf-8").lower()
    assert "departement de medecine" in texte
