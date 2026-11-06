"""Dates relatives : J0, J+4, J+373 au lieu des dates.

L'intervalle porte l'information clinique ; la date, elle, se cherche. Ces
tests verrouillent ce qui en fait une option sure : la date de naissance reste
masquee, une date qu'on ne sait pas situer aussi, et chaque document a son
propre J0.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.dates_relatives import (ETIQUETTE, TableDatesRelatives,  # noqa: E402
                                       echelle, libelle, lire_date)
from caviardoc.engine import Origine, Span, est_contextuel, est_date  # noqa: E402
from caviardoc.pipeline import PolitiqueDates, analyse, filtrer_dates  # noqa: E402
from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402

#: La chaine PDF relit le document produit : sans Tesseract, elle echoue.
besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")

AUJOURD_HUI = datetime.date(2026, 10, 16)
TEXTE = ("Patient ne le 11.10.1988. Opere le 12.03.2022, controle le "
         "16.03.2022, revu le 20.03.2023.")


def _date(texte_entier, mot, label=ETIQUETTE):
    i = texte_entier.index(mot)
    return Span(i, i + len(mot), label, 1.0, mot, Origine.REGLE.value)


class TestLecture:
    @pytest.mark.parametrize("texte,attendu", [
        ("12.03.2025", datetime.date(2025, 3, 12)),
        ("26.1.26", datetime.date(2026, 1, 26)),
        ("28.12.16", datetime.date(2016, 12, 28)),
        ("26.05.55", datetime.date(1955, 5, 26)),
        ("3-4-2020", datetime.date(2020, 4, 3)),
        ("13122021", datetime.date(2021, 12, 13)),
        ("5 avril 2014", datetime.date(2014, 4, 5)),
        ("1er mars 2014", datetime.date(2014, 3, 1)),
        ("12 janv. 2024", datetime.date(2024, 1, 12)),
        ("12 Février 2024", datetime.date(2024, 2, 12)),
    ])
    def test_jour_mois_annee(self, texte, attendu):
        assert lire_date(texte, AUJOURD_HUI) == attendu

    @pytest.mark.parametrize("texte", [
        "1er mars", "mars 2025", "31.02.2020", "45.13.2020", "Giro 2022", ""])
    def test_ce_qu_on_ne_sait_pas_situer(self, texte):
        """Un J+n approximatif ferait passer une estimation pour un
        intervalle."""
        assert lire_date(texte, AUJOURD_HUI) is None


class TestEchelle:
    def test_j0_est_la_plus_ancienne(self):
        spans = [_date(TEXTE, "16.03.2022"), _date(TEXTE, "20.03.2023"),
                 _date(TEXTE, "12.03.2022")]
        assert echelle(spans, AUJOURD_HUI) == {
            "12.03.2022": "J0", "16.03.2022": "J+4", "20.03.2023": "J+373"}

    def test_seules_les_dates_relatives_comptent(self):
        """La date de naissance ne fixe pas J0 : elle n'y figure pas."""
        spans = [_date(TEXTE, "11.10.1988", "date de naissance"),
                 _date(TEXTE, "16.03.2022")]
        assert echelle(spans, AUJOURD_HUI) == {"16.03.2022": "J0"}

    def test_libelle(self):
        assert [libelle(n) for n in (0, 4, 371)] == ["J0", "J+4", "J+371"]

    def test_l_etiquette_reste_une_date(self):
        assert est_date(ETIQUETTE) and est_contextuel(ETIQUETTE)


class TestFiltrage:
    def test_rien_n_est_demasque(self):
        spans = [_date(TEXTE, d, "date") for d in
                 ("11.10.1988", "12.03.2022", "16.03.2022", "20.03.2023")]
        gardes = filtrer_dates(TEXTE, spans, PolitiqueDates.RELATIVES)
        assert [s.text for s in gardes] == [s.text for s in spans]

    def test_la_date_de_naissance_reste_une_date_de_naissance(self):
        """Son ecart a J0 donnerait l'age au jour pres."""
        spans = [_date(TEXTE, d, "date") for d in ("11.10.1988", "12.03.2022")]
        gardes = filtrer_dates(TEXTE, spans, PolitiqueDates.RELATIVES)
        assert [s.label for s in gardes] == ["date de naissance", ETIQUETTE]

    def test_par_l_analyse_complete(self):
        spans = analyse(None, TEXTE, dates=PolitiqueDates.RELATIVES)
        par_texte = {s.text: s.label for s in spans}
        assert par_texte["11.10.1988"] == "date de naissance"
        assert par_texte["16.03.2022"] == ETIQUETTE


class TestTable:
    def test_rend_le_libelle_et_delegue_le_reste(self):
        commune = TablePseudonymes()
        table = TableDatesRelatives(commune, {"16.03.2022": "J+4"})
        assert table.rendre(_date(TEXTE, "16.03.2022")) == "[J+4]"
        assert table.pseudonyme("Dupont", "personne") == \
            commune.pseudonyme("Dupont", "personne")

    def test_une_date_relative_qu_on_ne_sait_pas_situer_reste_masquee(self):
        table = TableDatesRelatives(TablePseudonymes(), {})
        assert table.pseudonyme("1er mars", ETIQUETTE) == "DATE_1"

    def test_chaque_document_a_son_j0(self):
        """Deux documents d'un lot partagent la table, pas leur origine : leurs
        dates ne se recoupent pas."""
        commune = TablePseudonymes()
        a = TableDatesRelatives(commune, {"12.03.2022": "J0"})
        b = TableDatesRelatives(commune, {"12.03.2022": "J+30"})
        assert a.pseudonyme("12.03.2022", ETIQUETTE) == "J0"
        assert b.pseudonyme("12.03.2022", ETIQUETTE) == "J+30"


class TestChaines:
    def test_docx(self, tmp_path):
        from caviardoc.documents.docx import caviarder_docx
        from test_docx import _docx, _lire, _p, _t
        source = tmp_path / "cr.docx"
        source.write_bytes(_docx(_p(_t(TEXTE))))
        sortie = tmp_path / "out" / "cr.docx"
        sortie.parent.mkdir()
        rapport = caviarder_docx(None, source, sortie,
                                 formats=frozenset({"docx", "texte"}),
                                 dates=PolitiqueDates.RELATIVES)
        for produit in (_lire(sortie), rapport.texte):
            assert "[J0]" in produit and "[J+4]" in produit
            assert "[J+373]" in produit
            assert "2022" not in produit and "1988" not in produit
        assert rapport.dates_relatives == 3
        assert rapport.verifie and not rapport.fuites

    @besoin_ocr
    def test_pdf(self, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")
        from caviardoc.documents import caviardage
        d = pymupdf.open()
        d.new_page().insert_text((72, 80), TEXTE, fontsize=9)
        d.save(tmp_path / "cr.pdf")
        rapport = caviardage.caviarder_pdf(
            None, tmp_path / "cr.pdf", tmp_path / "out.pdf", verifier=False,
            formats=frozenset({"pdf", "texte"}),
            dates=PolitiqueDates.RELATIVES)
        assert rapport.dates_relatives == 3
        texte = (tmp_path / "out.txt").read_text(encoding="utf-8")
        assert "[J+4]" in texte and "16.03.2022" not in texte

    def test_le_libelle_est_ecrit_dans_le_rectangle(self):
        from PIL import Image
        from caviardoc.documents.caviardage import _inscrire, _noircir
        image = Image.new("RGB", (300, 60), (255, 255, 255))
        _noircir(image, [(10, 10, 200, 50)])
        _inscrire(image, [((10, 10, 200, 50), "J+371")])
        # `getcolors` plutot que `getdata`, deprecie a partir de Pillow 12.
        dedans = image.crop((10, 10, 200, 50)).getcolors(190 * 40)
        assert any(p == (255, 255, 255) or sum(p) > 600 for _, p in dedans)

    def test_l_interface_et_la_ligne_de_commande_le_proposent(self):
        from caviardoc import interface
        from caviardoc.cli import build_parser
        page = interface.page_console("jeton", "gliner-multi-pii-fp16", "app")
        assert 'name="dates" value="relatives"' in page
        args = build_parser().parse_args(
            ["caviarder", "-o", "sortie", "--dates", "relatives", "x.pdf"])
        assert PolitiqueDates(args.dates) is PolitiqueDates.RELATIVES
