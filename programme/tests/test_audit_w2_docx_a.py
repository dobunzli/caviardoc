"""Audit vague 2, voie D : texte exporte, revisions, glossaire, metadonnees ZIP."""

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from caviardoc.documents.docx import caviarder_docx, lire_partie  # noqa: E402
from caviardoc.engine import Span, apply_spans  # noqa: E402
from test_docx import DOCUMENT, _fichier, _lire, _p, _t  # noqa: E402


def _span(debut, fin, texte, etiquette="nom"):
    return Span(start=debut, end=fin, label=etiquette, score=1.0, text=texte)


def test_apply_spans_imbriques_ne_laisse_pas_de_queue():
    """Un masque manuel qui chevauche une detection ne laisse rien en clair."""
    texte = "Vu Jean-Marc Dubois hier"
    interne = _span(3, 7, "Jean")
    externe = _span(3, 19, "Jean-Marc Dubois")
    sortie = apply_spans(texte, [interne, externe],
                         render=lambda s: "[X_LONG_PSEUDONYME]"
                         if s is interne else "[Y]")
    assert "Dubois" not in sortie and "Marc" not in sortie
    assert sortie.startswith("Vu [") and sortie.endswith(" hier")


def test_apply_spans_chevauchement_partiel():
    texte = "abc DEFGH ijk"
    sortie = apply_spans(texte, [_span(4, 7, "DEF"), _span(6, 9, "FGH")],
                         render=lambda s: "<<<<<<<<<<>")
    assert not set("DEFGH") & set(sortie)


def test_revisions_collees_ne_forment_pas_une_date():
    """Une date supprimee suivie d'une date inseree ne doit pas se recoller."""
    xml = DOCUMENT.format(corps=_p(
        "<w:del><w:r><w:delText>12.03.1974</w:delText></w:r></w:del>"
        "<w:ins><w:r><w:t>13.03.1974</w:t></w:r></w:ins>"))
    texte, _ = lire_partie(xml)
    assert "12.03.197413.03.1974" not in texte


def test_revisions_collees_les_deux_dates_disparaissent(tmp_path):
    corps = _p("<w:del><w:r><w:delText>12.03.1974</w:delText></w:r></w:del>"
               "<w:ins><w:r><w:t>13.03.1974</w:t></w:r></w:ins>")
    source = _fichier(tmp_path / "r.docx", corps)
    sortie = tmp_path / "out.docx"
    caviarder_docx(None, source, sortie)
    produit = _lire(sortie)
    assert "12.03.1974" not in produit
    assert "13.03.1974" not in produit


def test_glossaire_et_corps_sont_proposes_separement(tmp_path):
    extras = {"word/glossary/document.xml": DOCUMENT.format(
        corps=_p(_t("Texte du glossaire")))}
    source = _fichier(tmp_path / "r.docx", _p(_t("Texte du corps")), extras)
    offerts = []

    def revue(parties):
        offerts.extend(parties)
        return {i: s for i, t, s in parties}

    caviarder_docx(None, source, tmp_path / "out.docx", revue=revue)
    textes = sorted(t for _, t, _ in offerts)
    assert textes == ["Texte du corps", "Texte du glossaire"]
    assert len({i for i, _, _ in offerts}) == 2


def test_zip_sortie_sans_date_ni_commentaire_ni_extra(tmp_path):
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for nom, contenu in (
                ("[Content_Types].xml", "<Types/>"),
                ("word/document.xml", DOCUMENT.format(corps=_p(_t("Rien"))))):
            info = zipfile.ZipInfo(nom, date_time=(2019, 7, 4, 13, 37, 42))
            info.comment = b"auteur-distinctif"
            info.extra = b"UT\x05\x00\x01\x00\x00\x00\x00"
            z.writestr(info, contenu)
    source = tmp_path / "r.docx"
    source.write_bytes(tampon.getvalue())
    sortie = tmp_path / "out.docx"
    caviarder_docx(None, source, sortie)
    with zipfile.ZipFile(sortie) as z:
        for info in z.infolist():
            assert info.date_time[0] != 2019, info.filename
            assert info.comment == b"", info.filename
            assert info.extra == b"", info.filename
