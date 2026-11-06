"""Le PDF produit n'est fait que d'images de pages, et le controle le prouve."""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")
Image = pytest.importorskip("PIL.Image")

from caviardoc.documents.caviardage import serialiser_pdf  # noqa: E402
from caviardoc.documents.structure_pdf import controler_structure  # noqa: E402


def _document(pages: int = 2):
    tampon = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(tampon, "JPEG")
    d = pymupdf.open()
    for _ in range(pages):
        p = d.new_page(width=200, height=300)
        p.insert_image(p.rect, stream=tampon.getvalue())
    return d


def test_un_pdf_fait_d_images_n_a_rien_a_signaler():
    assert controler_structure(serialiser_pdf(_document())) == []


def test_le_catalogue_ne_porte_pas_d_info_et_les_metadonnees_sont_neutres():
    octets = serialiser_pdf(_document())
    d = pymupdf.open(stream=octets, filetype="pdf")
    catalogue = d.xref_object(d.pdf_catalog(), compressed=True)
    assert "Producer(MuPDF" not in catalogue.replace(" ", "")
    meta = d.metadata
    assert meta["producer"] == "Caviardoc" and meta["author"] == ""
    # Une date, celle du traitement, au jour pres : pas celle du document.
    assert meta["creationDate"].startswith("D:") and meta["creationDate"].endswith("000000Z")


def test_un_lien_est_signale():
    d = _document(1)
    d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(0, 0, 50, 50),
                      "uri": "https://exemple.invalide"})
    assert "lien" in controler_structure(d.tobytes())


def test_une_action_a_l_ouverture_est_signalee():
    d = _document(1)
    d.xref_set_key(d.pdf_catalog(), "OpenAction",
                   "<</S/JavaScript/JS(app.alert(1))>>")
    trouves = controler_structure(d.tobytes())
    assert "action à l'ouverture" in trouves and "script" in trouves


def test_du_texte_est_signale():
    d = _document(1)
    d[0].insert_text((20, 40), "Dupont")
    trouves = controler_structure(d.tobytes())
    assert "texte" in trouves and "police de texte" in trouves


def test_chaque_page_porte_une_mention_invisible_et_rien_d_autre():
    """Sans texte, un PDF de pages-images a la forme d'un PDF d'hameconnage, et
    VirusBarrier le signalait « TR/PDF.MalwareX ». La mention ne porte aucune
    donnee, ne change aucun pixel, et le controle ne la compte pas."""
    d = pymupdf.open(stream=serialiser_pdf(_document(2)), filetype="pdf")
    assert [p.get_text().strip() for p in d] == [
        "Document anonymisé par Caviardoc - page 1/2",
        "Document anonymisé par Caviardoc - page 2/2"]
    blanc = d[0].get_pixmap()
    assert min(blanc.samples) == 255             # invisible : la page reste blanche
    assert controler_structure(d.tobytes()) == []


def test_une_page_deja_produite_ne_recoit_pas_une_seconde_mention():
    """La correction recopie les pages qu'elle ne touche pas."""
    une_fois = pymupdf.open(stream=serialiser_pdf(_document(1)), filetype="pdf")
    deux_fois = pymupdf.open(stream=serialiser_pdf(une_fois), filetype="pdf")
    assert deux_fois[0].get_text().count("Caviardoc") == 1


def test_un_autre_texte_reste_signale_malgre_la_mention():
    d = pymupdf.open(stream=serialiser_pdf(_document(1)), filetype="pdf")
    d[0].insert_text((20, 40), "Dupont")
    trouves = controler_structure(d.tobytes())
    assert "texte" in trouves and "police de texte" in trouves
