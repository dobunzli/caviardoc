"""Codes-barres et QR codes : masques sans condition, et verifies apres coup.

Un code d'etiquette encode l'identite du patient. Les codes sont fabriques ici
avec zxing-cpp, sur des textes inventes, puis colles dans des images et des
pages PDF (en image, et dessines en rectangles vectoriels).
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

zxingcpp = pytest.importorskip("zxingcpp")
from PIL import Image, ImageChops  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents import codes_barres  # noqa: E402

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")

TEXTE = "DOSSIER-0012345"
FORMATS = [("qr", zxingcpp.QRCode), ("code128", zxingcpp.Code128)]


def fabriquer(format_, texte=TEXTE, echelle=4):
    """Le code, en niveaux de gris, zones de silence comprises."""
    code = zxingcpp.create_barcode(texte, format_)
    return Image.fromarray(zxingcpp.write_barcode_to_image(
        code, scale=echelle, add_hrt=True, add_quiet_zones=True)).convert("L")


def sur_page(code, taille=(1200, 1600), position=(300, 500)):
    page = Image.new("RGB", taille, "white")
    page.paste(code.convert("RGB"), position)
    return page


def lisibles(image):
    return [b.text for b in zxingcpp.read_barcodes(image, return_errors=True)]


@pytest.mark.parametrize("nom,format_", FORMATS)
def test_code_decodable_avant_et_plus_apres(nom, format_):
    page = sur_page(fabriquer(format_))
    assert TEXTE in lisibles(page)
    posees = codes_barres.masquer(page)
    assert len(posees) == 1
    assert lisibles(page) == []
    assert codes_barres.compter(page) == 0


def test_chiffres_sous_un_code_lineaire_sont_couverts():
    code = fabriquer(zxingcpp.Code128)
    page = sur_page(code)
    x0, y0, x1, y1 = codes_barres.masquer(page)[0]
    # Le bas de l'etiquette (chiffres imprimes) tient dans le rectangle.
    assert y1 >= 500 + code.height - 1
    assert x0 <= 300 and x1 >= 300 + code.width - 1


def test_page_sans_code_inchangee():
    page = Image.new("RGB", (800, 600), "white")
    ImageDrawer = __import__("PIL.ImageDraw", fromlist=["ImageDraw"])
    ImageDrawer.Draw(page).rectangle([100, 100, 400, 300], outline="black")
    avant = page.copy()
    assert codes_barres.masquer(page) == []
    assert ImageChops.difference(avant, page).getbbox() is None


def test_plusieurs_codes_et_code_inverse():
    page = Image.new("RGB", (1400, 900), "white")
    page.paste(fabriquer(zxingcpp.QRCode, "QR-UN").convert("RGB"), (50, 50))
    page.paste(fabriquer(zxingcpp.Code128, "BARRES-DEUX").convert("RGB"),
               (500, 400))
    page.paste(Image.eval(fabriquer(zxingcpp.QRCode, "QR-NEGATIF"),
                          lambda v: 255 - v).convert("RGB"), (1000, 50))
    assert len(lisibles(page)) == 3
    assert len(codes_barres.masquer(page)) >= 3
    assert lisibles(page) == []


def test_code_tourne_masque():
    page = sur_page(fabriquer(zxingcpp.Code128).rotate(90, expand=True))
    assert lisibles(page)
    codes_barres.masquer(page)
    assert lisibles(page) == []


def datamatrix_elargi(colonnes, echelle=8):
    """Un DataMatrix dans lequel on a insere ``colonnes`` colonnes en trop.

    Une colonne ou deux : la dimension reste plausible et seule la somme de
    controle echoue, comme pour un code abime. Trois et plus : la dimension
    lue n'existe pas, et zxing rend un candidat en erreur de format, sans
    aucun contenu — ce que produisent aussi des lignes de texte ordinaires.
    """
    m = Image.fromarray(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode(TEXTE, zxingcpp.DataMatrix),
        scale=1, add_quiet_zones=False)).convert("L")
    largeur, hauteur = m.size
    milieu = largeur // 2
    code = Image.new("L", (largeur + colonnes, hauteur), 255)
    code.paste(m.crop((0, 0, milieu, hauteur)), (0, 0))
    for i in range(colonnes):     # colonnes 1, 2, 1, 2... du code d'origine
        source = 1 + i % 2
        code.paste(m.crop((source, 0, source + 1, hauteur)), (milieu + i, 0))
    code.paste(m.crop((milieu, 0, largeur, hauteur)), (milieu + colonnes, 0))
    code = code.resize((code.width * echelle, hauteur * echelle), Image.NEAREST)
    return sur_page(code)


def erreurs(image):
    return {str(b.error.type) for b in zxingcpp.read_barcodes(
        image, try_rotate=True, try_downscale=True, try_invert=True,
        return_errors=True) if b.error}


def test_candidat_sans_structure_de_code_n_est_pas_noirci():
    # Sur une lettre, un tel candidat couvrait tout un paragraphe et la
    # moitie d'un tableau : un aplat de la largeur de la page.
    page = datamatrix_elargi(3)
    assert erreurs(page) == {str(zxingcpp.ErrorType.Format)}
    avant = page.copy()
    assert codes_barres.masquer(page) == []
    assert ImageChops.difference(avant, page).getbbox() is None
    assert codes_barres.compter(page) == 0


def test_code_a_somme_de_controle_fausse_reste_masque():
    page = datamatrix_elargi(1)
    assert erreurs(page) == {str(zxingcpp.ErrorType.Checksum)}
    posees = codes_barres.masquer(page)
    assert len(posees) == 1
    x0, y0, x1, y1 = posees[0]
    assert x0 <= 300 and y0 <= 500
    assert erreurs(page) == set()


def test_sans_zxing_le_document_est_refuse(monkeypatch):
    monkeypatch.setitem(sys.modules, "zxingcpp", None)
    with pytest.raises(codes_barres.CodesBarresIndisponibles) as exc:
        codes_barres.exiger()
    assert "zxing-cpp" in str(exc.value) and "refuse" in str(exc.value)
    assert isinstance(exc.value, tesseract.TesseractIndisponible)


# --- image seule et image de .docx -----------------------------------------

def _png(image):
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return tampon.getvalue()


@besoin_ocr
@pytest.mark.parametrize("nom,format_", FORMATS)
def test_image_avec_code(nom, format_):
    from caviardoc.documents.images import caviarder_image, fuites_dans_images
    octets = _png(sur_page(fabriquer(format_)))
    produit, rapport = caviarder_image(None, "word/media/image1.png", octets)
    assert rapport.codes_barres == 1 and rapport.boites >= 1
    assert TEXTE in lisibles(Image.open(io.BytesIO(octets)).convert("RGB"))
    assert lisibles(Image.open(io.BytesIO(produit)).convert("RGB")) == []
    assert fuites_dans_images({"a.png": produit}, set()) == {}
    # La verification denonce un code qui subsiste.
    assert fuites_dans_images({"a.png": octets}, set()) == {
        codes_barres.CODE_ENCORE_LISIBLE: ["a.png"]}


@besoin_ocr
def test_image_sans_code_inchangee():
    from caviardoc.documents.images import caviarder_image
    octets = _png(Image.new("RGB", (400, 300), "white"))
    produit, rapport = caviarder_image(None, "x.png", octets)
    assert produit == octets
    assert rapport.codes_barres == 0 and rapport.boites == 0


@besoin_ocr
def test_docx_avec_code_dans_une_image(tmp_path):
    from caviardoc.documents.docx import caviarder_docx
    chemin = tmp_path / "e.docx"
    import docx
    d = docx.Document()
    d.add_paragraph("Demande de consilium pour un avis rhumatologique "
                    "concernant des douleurs lombaires persistantes evoluant "
                    "depuis plusieurs mois malgre un traitement antalgique "
                    "bien conduit. Etiquette collee ci-dessous pour le suivi "
                    "du dossier de consultation ambulatoire du service.")
    png = tmp_path / "c.png"
    png.write_bytes(_png(sur_page(fabriquer(zxingcpp.QRCode), (700, 500),
                                  (100, 100))))
    d.add_picture(str(png))
    d.save(chemin)
    rapport = caviarder_docx(None, chemin, tmp_path / "o.docx")
    assert [i.codes_barres for i in rapport.images_lues] == [1]
    assert rapport.verifie and not rapport.fuites
    with zipfile.ZipFile(tmp_path / "o.docx") as z:
        medias = [n for n in z.namelist() if n.startswith("word/media/")]
        assert medias
        for n in medias:
            assert lisibles(Image.open(io.BytesIO(z.read(n))).convert("RGB")) == []


# --- PDF ---------------------------------------------------------------------

def _page_pdf(d, code, vectoriel):
    import pymupdf
    p = d.new_page()
    p.insert_textbox(pymupdf.Rect(72, 72, 520, 160),
                     "Compte rendu de consultation du service de medecine "
                     "interne, tension stable, bilan sanguin sans "
                     "particularite, poursuite du traitement habituel et "
                     "controle dans trois mois.", fontsize=9)
    if vectoriel:
        # Un rectangle par module : aucun pixel dans le fichier.
        modules = Image.fromarray(zxingcpp.write_barcode_to_image(
            zxingcpp.create_barcode(TEXTE, zxingcpp.QRCode), scale=1,
            add_quiet_zones=True)).convert("L")
        taille = 4
        for y in range(modules.height):
            for x in range(modules.width):
                if modules.getpixel((x, y)) < 128:
                    p.draw_rect(pymupdf.Rect(200 + x * taille, 300 + y * taille,
                                             204 + x * taille, 304 + y * taille),
                                fill=(0, 0, 0), color=None)
    else:
        tampon = io.BytesIO()
        code.convert("RGB").save(tampon, format="PNG")
        p.insert_image(pymupdf.Rect(200, 300, 200 + code.width * 0.75,
                                    300 + code.height * 0.75),
                       stream=tampon.getvalue())
    return p


def _pages_rendues(chemin):
    import pymupdf
    from PIL import Image as I
    with pymupdf.open(chemin) as d:
        return [I.open(io.BytesIO(p.get_pixmap(dpi=300).tobytes("png"))).convert("RGB")
                for p in d]


@besoin_ocr
@pytest.mark.parametrize("vectoriel", [False, True])
def test_pdf_avec_code(tmp_path, vectoriel):
    import pymupdf
    from caviardoc.documents.caviardage import caviarder_pdf
    d = pymupdf.open()
    _page_pdf(d, fabriquer(zxingcpp.QRCode, echelle=6), vectoriel)
    d.save(tmp_path / "e.pdf")
    avant = _pages_rendues(tmp_path / "e.pdf")[0]
    assert TEXTE in lisibles(avant)
    rapport = caviarder_pdf(None, tmp_path / "e.pdf", tmp_path / "o.pdf")
    assert rapport.pages[0].codes_barres >= 1
    assert rapport.verifie and not rapport.fuites
    assert not rapport.pages[0].reserve        # information, pas a verifier
    assert lisibles(_pages_rendues(tmp_path / "o.pdf")[0]) == []


@besoin_ocr
def test_pdf_code_encore_lisible_est_une_fuite(tmp_path, monkeypatch):
    import pymupdf
    from caviardoc.documents import caviardage
    d = pymupdf.open()
    _page_pdf(d, fabriquer(zxingcpp.QRCode, echelle=6), False)
    d.save(tmp_path / "e.pdf")
    monkeypatch.setattr(codes_barres, "masquer", lambda image: [])
    rapport = caviardage.caviarder_pdf(None, tmp_path / "e.pdf",
                                       tmp_path / "o.pdf")
    assert rapport.fuites and "code-barres encore lisible" in rapport.fuites[0].texte
    assert not rapport.succes


@besoin_ocr
def test_pdf_sans_code_aucune_note(tmp_path):
    import pymupdf
    from caviardoc.documents.caviardage import caviarder_pdf
    d = pymupdf.open()
    p = d.new_page()
    p.insert_textbox(pymupdf.Rect(72, 72, 520, 160),
                     "Compte rendu de consultation du service de medecine "
                     "interne, tension stable, bilan sanguin sans "
                     "particularite, poursuite du traitement habituel et "
                     "controle dans trois mois.", fontsize=9)
    d.save(tmp_path / "e.pdf")
    rapport = caviarder_pdf(None, tmp_path / "e.pdf", tmp_path / "o.pdf")
    assert rapport.pages[0].codes_barres == 0
    assert rapport.verifie and not rapport.fuites


def test_note_et_licence_et_controle():
    from caviardoc import doctor, licences
    assert codes_barres.NOTE == "code-barres masqué"
    assert any(c.paquet == "zxing-cpp" and c.licence == "Apache-2.0"
               for c in licences.COMPOSANTS)
    assert doctor.check_codes_barres().level is doctor.Level.OK


def test_installation_exige_zxing():
    from caviardoc import installateur
    assert "zxingcpp" in installateur.MODULES_REQUIS
