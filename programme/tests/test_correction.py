"""Ce que la verification relit encore, on le noircit.

La verification savait dire « Rue de la Sapiniere 45 — rue · page 1 » sans
rien en faire : le document sortait avec une alerte rouge, a reprendre a la
main. Elle a pourtant tout ce qu'il faut pour corriger : la chaine, la page, et
un OCR de la page produite qui situe chaque mot.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc.documents.commun import occurrences_relues  # noqa: E402

RUE = "Rue de la Sapiniere 45"


class TestOccurrencesRelues:
    """La correction cherche exactement comme la verification."""

    def test_sans_blancs_ni_casse(self):
        texte = "Domicile : RUE DE LA\nSAPINIERE 45, Echallens"
        (sp,) = occurrences_relues(texte, {RUE: "rue"})
        assert texte[sp.start:sp.end] == "RUE DE LA\nSAPINIERE 45"
        assert sp.label == "rue"

    def test_toutes_les_occurrences(self):
        texte = f"{RUE}. Puis encore {RUE}."
        assert len(occurrences_relues(texte, {RUE: "rue"})) == 2

    def test_sans_frontiere_de_mot(self):
        """La verification denonce « Dupont » dans « DupontJean » soude par
        l'OCR ; la correction doit l'y trouver aussi, sinon l'alerte revient
        a chaque passe."""
        assert occurrences_relues("M. DupontJean", {"Dupont": "personne"})


# --- Sur un vrai PDF : demande Tesseract ------------------------------------

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pytesseract")
pytest.importorskip("PIL")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents.caviardage import (corriger_fuites,  # noqa: E402
                                          pages_des_fuites)

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")


def _pdf_image(pages: list[list[str]]) -> bytes:
    """Un PDF fait d'images, comme ceux que produit le caviardage."""
    d = pymupdf.open()
    for lignes in pages:
        image = Image.new("RGB", (1240, 1754), "white")
        dessin = ImageDraw.Draw(image)
        fonte = ImageFont.load_default(size=40)
        for i, ligne in enumerate(lignes):
            dessin.text((80, 120 + i * 80), ligne, fill="black", font=fonte)
        tampon = io.BytesIO()
        image.save(tampon, format="JPEG", quality=90)
        page = d.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=tampon.getvalue())
    return d.tobytes()


@besoin_ocr
def test_la_chaine_relue_est_noircie_puis_ne_l_est_plus():
    octets = _pdf_image([["Compte rendu de consultation"],
                         ["Adresse du patient", RUE, "Suivi dans six mois."]])
    fuites = pages_des_fuites(octets, {RUE: "rue"}, dpi=150)
    assert fuites == {RUE: [2]}

    octets, restantes, corrigees = corriger_fuites(octets, fuites, dpi=150)

    assert restantes == {}
    assert corrigees == {RUE: [2]}
    assert pages_des_fuites(octets, {RUE: "rue"}, dpi=150) == {}
    # Le reste de la page survit : on noircit des mots, pas la page.
    relu = pymupdf.open(stream=octets, filetype="pdf")
    assert len(relu) == 2
    from caviardoc.documents.caviardage import _rendu
    from caviardoc.documents.ocr import ocr_image
    texte = ocr_image(_rendu(relu[1], 150), dpi=150).texte
    assert "Suivi" in texte


@besoin_ocr
def test_une_chaine_laissee_visible_a_la_relecture_n_est_pas_corrigee():
    """La correction repare un oubli de la machine, pas une decision humaine."""
    octets = _pdf_image([["Adresse du patient", RUE]])
    fuites = pages_des_fuites(octets, {RUE: "rue"}, dpi=150)

    _, restantes, corrigees = corriger_fuites(octets, fuites, dpi=150,
                                              epargnees={RUE})

    assert restantes == {RUE: [1]}
    assert corrigees == {}


def test_une_couche_texte_residuelle_reste_signalee():
    """Elle n'a pas de page ou noircir : elle doit rester une fuite."""
    cle = "couche texte residuelle : 12 caracteres"
    octets = _pdf_image([["Rien"]])
    _, restantes, corrigees = corriger_fuites(octets, {cle: []}, dpi=72)
    assert restantes == {cle: []}
    assert corrigees == {}


# --- La raison de chaque fuite qui subsiste -----------------------------------

from caviardoc.documents.caviardage import (RAISON_COUCHE_TEXTE,  # noqa: E402
                                          RAISON_LAISSEE, RAISON_SANS_ZONE)
from caviardoc.documents.fuites import depuis_chaines  # noqa: E402


@besoin_ocr
def test_la_raison_d_une_chaine_laissee_visible_est_celle_de_l_utilisateur():
    octets = _pdf_image([["Adresse du patient", RUE]])
    fuites = pages_des_fuites(octets, {RUE: "rue"}, dpi=150)
    raisons = {}
    corriger_fuites(octets, fuites, dpi=150, epargnees={RUE}, raisons=raisons)
    assert raisons == {RUE: RAISON_LAISSEE}


@besoin_ocr
def test_une_chaine_corrigee_n_a_plus_de_raison():
    octets = _pdf_image([["Adresse du patient", RUE]])
    fuites = pages_des_fuites(octets, {RUE: "rue"}, dpi=150)
    raisons = {}
    _, restantes, _ = corriger_fuites(octets, fuites, dpi=150, raisons=raisons)
    assert restantes == {} and raisons == {}


@besoin_ocr
def test_une_chaine_introuvable_sur_la_page_dit_qu_il_n_y_a_pas_de_zone():
    octets = _pdf_image([["Adresse du patient"]])
    raisons = {}
    _, restantes, _ = corriger_fuites(octets, {"Zxqvwk 4471": [1]}, dpi=150,
                                      raisons=raisons)
    assert restantes == {"Zxqvwk 4471": [1]}
    assert raisons == {"Zxqvwk 4471": RAISON_SANS_ZONE}


def test_la_couche_texte_residuelle_dit_pourquoi():
    cle = "couche texte residuelle : 12 caracteres"
    raisons = {}
    corriger_fuites(_pdf_image([["Rien"]]), {cle: []}, dpi=72, raisons=raisons)
    assert raisons == {cle: RAISON_COUCHE_TEXTE}


def test_la_raison_arrive_a_l_interface():
    (f,) = depuis_chaines({"x 1"}, {"x 1": "numero de dossier"}, {"x 1": [2]},
                          {"x 1": "parce que"})
    assert f.vue()["raison"] == "parce que"
    assert depuis_chaines({"x 1"})[0].vue()["raison"] == ""


# --- Un .docx : remplacer dans le XML, noircir dans les images ---------------

from xml.etree import ElementTree  # noqa: E402

from caviardoc.documents.docx import (RapportDocx, _corriger_fuites,  # noqa: E402
                                    _produire_texte, lire_partie)
from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402

DOCUMENT = ('<?xml version="1.0" encoding="UTF-8"?>\n<w:document xmlns:w="'
            'http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>{}</w:body></w:document>')


def _corps(*noeuds: str) -> bytes:
    runs = "".join(f"<w:r><w:t xml:space=\"preserve\">{n}</w:t></w:r>"
                   for n in noeuds)
    return DOCUMENT.format(f"<w:p>{runs}</w:p>").encode("utf-8")


def test_le_xml_est_reecrit_meme_reparti_ou_en_capitales():
    """Word decoupe les mots, et un attribut peut porter la chaine en
    capitales : la verification les lit tous deux, la correction aussi."""
    contenu = {
        "word/document.xml": _corps("Domicile : Rue de la Sapi", "niere 45."),
        "customXml/item1.xml": b'<x adresse="RUE DE LA SAPINIERE 45"/>',
    }

    corrigees, xml, images = _corriger_fuites(
        contenu, set(), {RUE}, {}, {RUE: "rue"}, TablePseudonymes(), "fra")

    assert corrigees == {RUE}
    assert xml == set() and images == {}
    for nom, octets in contenu.items():
        assert "sapiniere" not in octets.decode().lower()
        ElementTree.fromstring(octets)          # le XML reste valide
    texte, _ = lire_partie(contenu["word/document.xml"].decode())
    assert texte == "Domicile : [RUE_1]."


def test_une_partie_retiree_n_est_pas_retouchee():
    """Elle ne sera pas dans l'archive produite : rien a y corriger."""
    contenu = {"word/document.xml": _corps(RUE),
               "docProps/custom.xml": f"<p>{RUE}</p>".encode()}

    _corriger_fuites(contenu, {"docProps/custom.xml"}, {RUE}, {},
                     {RUE: "rue"}, TablePseudonymes(), "fra")

    assert contenu["docProps/custom.xml"] == f"<p>{RUE}</p>".encode()


def test_le_texte_produit_est_corrige_aussi():
    nom = "word/document.xml"
    lectures = {nom: lire_partie(_corps("Adresse : ", RUE).decode())}
    rapport = RapportDocx(source=Path("a.docx"), sortie=Path("b.docx"))

    _produire_texte(lectures, [nom], {nom: []}, {RUE: "rue"},
                    TablePseudonymes(), rapport)

    assert RUE not in rapport.texte
    assert [f.texte for f in rapport.corrigees_texte] == [RUE]
    assert rapport.fuites_texte == []


def test_un_intitule_de_l_export_n_est_pas_une_fuite_du_texte():
    """« page 2 », masque en tete d'une page de suite, ne se lit pas dans
    « --- page 2 --- » : cet intitule vient de l'export, pas du document."""
    from caviardoc.documents.commun import assembler_texte, sans_intitules
    texte = assembler_texte([("a", []), ("b", [])], TablePseudonymes())
    assert "page 2" in texte
    assert "page 2" not in sans_intitules(texte)
    assert sans_intitules("--- page 1 ---\n\nbonjour page 2\n").strip() == "bonjour page 2"


def test_le_texte_docx_ignore_ses_propres_intitules():
    corps, pied = "word/document.xml", "word/footer1.xml"
    lectures = {corps: lire_partie(_corps("Rien a voir").decode()),
                pied: lire_partie(_corps("Autre").decode())}
    rapport = RapportDocx(source=Path("a.docx"), sortie=Path("b.docx"))

    _produire_texte(lectures, [corps, pied], {corps: [], pied: []},
                    {"pied de page 1": "numero de dossier"},
                    TablePseudonymes(), rapport)

    assert "--- pied de page 1 ---" in rapport.texte
    assert rapport.fuites_texte == [] and rapport.corrigees_texte == []


@besoin_ocr
def test_une_image_incorporee_est_noircie_la_ou_on_la_relit():
    image = io.BytesIO()
    fond = Image.new("RGB", (1100, 230), "white")
    dessin = ImageDraw.Draw(fond)
    fonte = ImageFont.load_default(size=44)
    dessin.text((30, 25), "Adresse du patient", fill="black", font=fonte)
    dessin.text((30, 110), RUE, fill="black", font=fonte)
    fond.save(image, format="PNG")
    nom = "word/media/image1.png"
    contenu = {nom: image.getvalue()}

    corrigees, xml, images = _corriger_fuites(
        contenu, set(), set(), {RUE: [nom]}, {RUE: "rue"},
        TablePseudonymes(), "fra")

    assert corrigees == {RUE}
    assert images == {}
    relue = Image.open(io.BytesIO(contenu[nom]))
    assert relue.format == "PNG"                 # format conserve
