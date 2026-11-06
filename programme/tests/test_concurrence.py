"""Plusieurs documents a la fois : ce qui se partage, et ce qui ne se partage pas."""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import concurrence  # noqa: E402
from caviardoc.engine import Span  # noqa: E402
from caviardoc.interface import Atelier, deposer  # noqa: E402
from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402


def _dans_un_autre_fil(fonction) -> bool:
    """La fonction a-t-elle pu s'executer dans un autre fil, sous une seconde ?"""
    fait = threading.Event()
    threading.Thread(target=lambda: (fonction(), fait.set()), daemon=True).start()
    return fait.wait(1.0)


def _prendre_pdf():
    with concurrence.pdf():
        pass


class TestVerrouPdf:
    def test_exclusif_entre_deux_fils(self):
        with concurrence.pdf():
            assert not _dans_un_autre_fil(_prendre_pdf)
        assert _dans_un_autre_fil(_prendre_pdf)

    def test_reentrant_pour_le_meme_fil(self):
        with concurrence.pdf():
            with concurrence.pdf():
                pass
            # Toujours tenu apres la sortie du niveau interieur.
            assert not _dans_un_autre_fil(_prendre_pdf)
        assert _dans_un_autre_fil(_prendre_pdf)

    def test_relache_pendant_l_ocr_puis_repris(self):
        with concurrence.pdf():
            with concurrence.pdf():
                with concurrence.hors_pdf():
                    assert _dans_un_autre_fil(_prendre_pdf)
                assert not _dans_un_autre_fil(_prendre_pdf)
            assert not _dans_un_autre_fil(_prendre_pdf)
        assert _dans_un_autre_fil(_prendre_pdf)

    def test_hors_pdf_sans_verrou_ne_fait_rien(self):
        with concurrence.hors_pdf():
            pass
        assert _dans_un_autre_fil(_prendre_pdf)


@pytest.mark.parametrize("coeurs, memoire, attendu", [
    (1, 16 * 1024 ** 3, 1),
    (2, 16 * 1024 ** 3, 1),
    (4, 8 * 1024 ** 3, 2),
    (14, 8 * 1024 ** 3, 2),
    (6, 16 * 1024 ** 3, 3),
    (14, 24 * 1024 ** 3, 4),
    (14, 0, 4),
])
def test_documents_simultanes(coeurs, memoire, attendu):
    assert concurrence.documents_simultanes(coeurs, memoire) == attendu


def test_la_table_partagee_ne_donne_jamais_deux_fois_le_meme_numero():
    """Deux documents traites en meme temps enregistrent des noms nouveaux."""
    table = TablePseudonymes()
    noms = [f"Patient{chr(65 + i)}{chr(65 + j)}" for i in range(20)
            for j in range(10)]
    fils = [threading.Thread(target=table.enregistrer, args=(
        [Span(0, len(n), "personne", 1.0, n) for n in noms[k::4]],))
        for k in range(4)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    pseudos = [table.pseudonyme(n, "personne") for n in noms]
    assert len(set(pseudos)) == len(noms)


def _pdf_texte() -> bytes:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for _ in range(2):
        # Assez de mots pour que la couche texte soit lue sans Tesseract.
        page = doc.new_page()
        page.insert_text((72, 72), "Le controle annuel est sans "
                         "particularite, AVS 756.1234.5678.97, tension normale.")
        page.insert_text((72, 90), "Le traitement est poursuivi sans "
                         "changement et un nouveau controle est prevu.")
    return doc.tobytes()


class TestPrelecture:
    def test_la_lecture_faite_a_l_avance_donne_le_meme_document(self, tmp_path):
        from caviardoc.documents import caviardage
        source = tmp_path / "a.pdf"
        source.write_bytes(_pdf_texte())
        sans = caviardage.caviarder_pdf(None, source, tmp_path / "s1.pdf")
        avec = caviardage.caviarder_pdf(None, source, tmp_path / "s2.pdf",
                                        lu=caviardage.lire_pdf(source))
        assert ([p.entites for p in avec.pages]
                == [p.entites for p in sans.pages])
        assert not avec.fuites
        # La verification relit le PDF produit par OCR : sans Tesseract —
        # c'est le cas de l'integration continue —, elle ne peut pas conclure.
        from caviardoc import tesseract
        if tesseract.binaire() is not None:
            assert avec.verifie

    def test_l_atelier_lit_a_l_avance_puis_rend_la_lecture_une_fois(self,
                                                                     tmp_path):
        atelier = Atelier(racine=tmp_path)
        octets = _pdf_texte()
        depot = deposer(atelier, "a.pdf", io.BytesIO(octets), len(octets))
        atelier.prelire(depot.identifiant)
        lu = atelier.prelecture(depot.identifiant, 300)
        assert lu is not None and len(lu) == 2
        assert atelier.prelecture(depot.identifiant, 300) is None

    def test_une_autre_resolution_relit_le_document(self, tmp_path):
        atelier = Atelier(racine=tmp_path)
        octets = _pdf_texte()
        depot = deposer(atelier, "a.pdf", io.BytesIO(octets), len(octets))
        atelier.prelire(depot.identifiant, 300)
        assert atelier.prelecture(depot.identifiant, 200) is None

    def test_un_docx_n_est_pas_lu_a_l_avance(self, tmp_path):
        atelier = Atelier(racine=tmp_path)
        depot = deposer(atelier, "a.docx", io.BytesIO(b"PK\x03\x04" + b"0" * 40),
                        44)
        atelier.prelire(depot.identifiant)
        assert atelier.prelectures == {}

    def test_retirer_un_depot_oublie_sa_lecture(self, tmp_path):
        from caviardoc.interface import retirer
        atelier = Atelier(racine=tmp_path)
        octets = _pdf_texte()
        depot = deposer(atelier, "a.pdf", io.BytesIO(octets), len(octets))
        atelier.prelire(depot.identifiant)
        retirer(atelier, depot.identifiant)
        assert atelier.prelectures == {}


def test_le_texte_seul_est_verifie_par_le_controle_du_texte(tmp_path):
    """Pas de PDF produit, donc rien a relire par OCR : le controle du texte
    exporte tient lieu de verification. Il s'affichait « NON VERIFIE »."""
    from caviardoc.documents import caviardage
    source = tmp_path / "a.pdf"
    source.write_bytes(_pdf_texte())
    rapport = caviardage.caviarder_pdf(None, source, tmp_path / "s.pdf",
                                       formats=frozenset({"texte"}))
    assert rapport.verifie and not rapport.erreur_verification
    assert (tmp_path / "s.txt").is_file() and not (tmp_path / "s.pdf").exists()
    sans = caviardage.caviarder_pdf(None, source, tmp_path / "t.pdf",
                                    formats=frozenset({"texte"}), verifier=False)
    assert not sans.verifie


def test_affichage_mupdf_coupe_et_messages_collectes(caplog):
    import logging
    import pymupdf
    with concurrence.pdf():
        pass
    assert pymupdf.TOOLS.mupdf_display_errors() is False
    assert pymupdf.TOOLS.mupdf_display_warnings() is False
    pymupdf.TOOLS.mupdf_warnings(reset=True)
    try:
        pymupdf.open(stream=b"%PDF-1.4\n garbage", filetype="pdf").load_page(0)
    except Exception:
        pass
    with caplog.at_level(logging.DEBUG, logger="caviardoc.concurrence"):
        comptes = concurrence.journaliser_messages_mupdf()
    assert pymupdf.TOOLS.mupdf_warnings() == ""
    assert all("MuPDF (x" in r.message for r in caplog.records)
    assert len(caplog.records) == len(comptes)
