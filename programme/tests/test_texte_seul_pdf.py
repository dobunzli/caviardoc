"""Export « texte » seul d'un PDF : ni nom ni AVS, et verdict juste."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pytesseract")

from test_audit_pdf_a import _corps, besoin_ocr  # noqa: E402

from caviardoc.documents import caviardage  # noqa: E402
from caviardoc.documents.caviardage import caviarder_pdf  # noqa: E402
from caviardoc.engine import Span  # noqa: E402

NOM = "Zorglub Quidam"
AVS = "756.1234.5678.97"


def _detecteur(original):
    def detection(model, texte, **kw):
        bruts, spans = original(model, texte, **kw)
        i = texte.find(NOM)
        if i != -1:
            sp = Span(i, i + len(NOM), "personne", 0.9, NOM)
            bruts, spans = list(bruts) + [sp], list(spans) + [sp]
        return bruts, spans
    return detection


@besoin_ocr
def test_texte_seul_sans_nom_ni_avs(tmp_path, monkeypatch):
    d = pymupdf.open()
    for _ in range(2):
        p = d.new_page()
        p.insert_text((72, 80), f"Patient {NOM}, AVS {AVS}.", fontsize=11)
        _corps(p, 120)
    d.save(tmp_path / "t.pdf")
    monkeypatch.setattr(caviardage, "analyse_detaillee",
                        _detecteur(caviardage.analyse_detaillee))
    r = caviarder_pdf(None, tmp_path / "t.pdf", tmp_path / "o.pdf",
                      formats=frozenset({"texte"}))
    compact = "".join(r.texte.lower().split())
    assert AVS not in r.texte and "5678.97" not in r.texte
    assert "zorglub" not in compact and "quidam" not in compact
    assert r.verifie and not r.fuites_texte
    assert not (tmp_path / "o.pdf").exists()


@besoin_ocr
def test_texte_seul_signale_une_fuite_non_corrigeable(tmp_path, monkeypatch):
    """Si le texte exporte garde un identifiant, le rapport doit l'annoncer."""
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 80), f"Patient {NOM}, AVS {AVS}.", fontsize=11)
    _corps(p, 120)
    d.save(tmp_path / "t.pdf")
    monkeypatch.setattr(caviardage, "analyse_detaillee",
                        _detecteur(caviardage.analyse_detaillee))
    monkeypatch.setattr(caviardage, "_assembler_texte",
                        lambda pages, table: "Patient Zorglub Quidam")
    r = caviarder_pdf(None, tmp_path / "t.pdf", tmp_path / "o.pdf",
                      formats=frozenset({"texte"}))
    assert r.fuites_texte, "texte en clair annonce propre"
