"""Les images d'un PDF a couche texte sont lues, elles aussi.

Le cas qui l'a montre : une page scannee a laquelle le logiciel d'archivage
avait ajoute deux lignes de texte (« importe le… »). Ces deux lignes suffisaient
a juger la couche texte saine ; la detection ne lisait qu'elles, et le scan —
nom, date de naissance, adresse — sortait en clair, annonce « verifie », puisque
la verification ne cherche que ce qui a ete detecte. Une etiquette patient
collee en image passait de meme des que le masquage des images etait coupe.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pytesseract")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents import ocr  # noqa: E402
from caviardoc.documents.caviardage import _lire_page, caviarder_pdf  # noqa: E402
from caviardoc.documents.zones import OptionsZones  # noqa: E402
from test_docx import AVS  # noqa: E402

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")

#: Deux lignes de vrai texte : assez pour que la couche soit jugee saine.
ENTETE = ("Document importe le 12.06.2024 a 14:32 - archive electronique - "
          "page 1 sur 1 - copie conforme a l'original numerise")
PIED = ("Ce document a ete numerise et indexe automatiquement par le systeme "
        "d'archivage du service de cardiologie")


def _image(lignes, taille):
    image = Image.new("RGB", taille, "white")
    dessin = ImageDraw.Draw(image)
    fonte = ImageFont.load_default(size=40)
    for i, ligne in enumerate(lignes):
        dessin.text((30, 30 + i * 60), ligne, fill="black", font=fonte)
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return tampon.getvalue()


def _scan_avec_entete(chemin, rotation=0):
    d = pymupdf.open()
    page = d.new_page()
    page.insert_image(pymupdf.Rect(0, 40, 595, 790), stream=_image(
        ["Service de cardiologie", f"Assure {AVS}",
         "Echocardiographie dans les normes."], (1190, 1500)))
    page.insert_text((40, 25), ENTETE, fontsize=8)
    page.insert_text((40, 820), PIED, fontsize=8)
    page.set_rotation(rotation)
    d.save(chemin)


def _relu(chemin):
    produit = pymupdf.open(chemin)
    pix = produit[0].get_pixmap(dpi=200)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    return "".join(ocr.ocr_image(image).texte.split())


def test_les_petites_images_ne_sont_pas_lues():
    """Puces et filets : rien a y lire, et autant d'OCR epargnes."""
    d = pymupdf.open()
    page = d.new_page()
    page.insert_image(pymupdf.Rect(72, 72, 80, 80), stream=_image([], (16, 16)))
    page.insert_image(pymupdf.Rect(72, 200, 332, 245),
                      stream=_image(["Etiquette"], (520, 90)))
    zones = ocr.zones_images(page, dpi=72)
    assert len(zones) == 1
    assert zones[0] == pytest.approx((72, 200, 332, 245), abs=1)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_les_zones_suivent_la_rotation_de_la_page(rotation):
    """Meme repere que les mots : sans rotation appliquee, l'OCR lirait une
    zone vide a cote de l'image."""
    d = pymupdf.open()
    page = d.new_page()
    noir = io.BytesIO()
    Image.new("RGB", (100, 50), "black").save(noir, "PNG")
    page.insert_image(pymupdf.Rect(100, 600, 300, 700), stream=noir.getvalue())
    page.set_rotation(rotation)
    (x0, y0, x1, y1), = ocr.zones_images(page, dpi=72)
    rendu = Image.open(io.BytesIO(page.get_pixmap(dpi=72).tobytes("png"))).convert("L")
    assert rendu.getpixel((int((x0 + x1) / 2), int((y0 + y1) / 2))) < 50


@besoin_ocr
def test_un_scan_sous_un_entete_numerique_est_lu(tmp_path):
    _scan_avec_entete(tmp_path / "mixte.pdf")
    page = pymupdf.open(tmp_path / "mixte.pdf")[0]
    lecture, source = _lire_page(page, 300, "fra")
    assert source == "couche texte + images"
    assert AVS in lecture.texte
    assert ENTETE.split()[0] in lecture.texte    # la couche texte reste lue


@besoin_ocr
@pytest.mark.parametrize("rotation", [0, 90])
def test_le_scan_est_caviarde_et_le_reste_lisible(tmp_path, rotation):
    _scan_avec_entete(tmp_path / "mixte.pdf", rotation)
    rapport = caviarder_pdf(None, tmp_path / "mixte.pdf", tmp_path / "out.pdf")
    assert AVS in {sp.text for sp in rapport.pages[0].entites}
    relu = _relu(tmp_path / "out.pdf")
    assert "".join(AVS.split(".")) not in relu.replace(".", "")
    assert "Echocardiographie" in relu
    assert rapport.verifie and not rapport.fuites


@besoin_ocr
def test_une_etiquette_en_image_est_lue_meme_sans_masquage_des_images(tmp_path):
    d = pymupdf.open()
    page = d.new_page()
    page.insert_text((72, 72), "Rapport de consultation", fontsize=14)
    page.insert_text((72, 100), "Le patient se presente pour un controle annuel "
                     "sans particularite.", fontsize=11)
    page.insert_text((72, 118), "La tension arterielle est normale et le "
                     "traitement est poursuivi.", fontsize=11)
    page.insert_image(pymupdf.Rect(72, 220, 432, 265),
                      stream=_image([f"AVS {AVS}"], (720, 90)))
    d.save(tmp_path / "etiquette.pdf")
    rapport = caviarder_pdf(None, tmp_path / "etiquette.pdf", tmp_path / "out.pdf",
                            zones=OptionsZones(logos=False))
    assert rapport.pages[0].source == "couche texte + images"
    assert "".join(AVS.split(".")) not in _relu(tmp_path / "out.pdf").replace(".", "")


@besoin_ocr
def test_un_scan_interrogeable_n_est_pas_lu_deux_fois(tmp_path):
    """Un scan « interrogeable » porte deja sa reconnaissance en couche
    invisible : la doubler dupliquerait chaque mot."""
    d = pymupdf.open()
    page = d.new_page()
    lignes = ["Service de cardiologie", "Echocardiographie dans les normes."]
    lignes += [PIED[:60], PIED[60:]] * 3
    octets = _image(lignes, (1190, 1684))
    page.insert_image(page.rect, stream=octets)
    # La couche invisible, posee comme le fait un logiciel d'OCR : chaque mot
    # reconnu, a l'endroit de son glyphe.
    echelle = page.rect.width / 1190
    for mot in ocr.ocr_image(Image.open(io.BytesIO(octets))).mots:
        hauteur = (mot.y1 - mot.y0) * echelle
        page.insert_text((mot.x0 * echelle, mot.y1 * echelle), mot.texte,
                         fontsize=hauteur, render_mode=3)
    d.save(tmp_path / "interrogeable.pdf")
    page = pymupdf.open(tmp_path / "interrogeable.pdf")[0]
    lecture, _ = _lire_page(page, 300, "fra")
    assert lecture.texte.count("Echocardiographie") == 1
