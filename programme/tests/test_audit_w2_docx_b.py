"""Audit vague 2, voie D : image lue autrement que rendue (images.py)."""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from caviardoc.documents.images import _ouvrir  # noqa: E402


def test_image_rgba_lue_comme_rendue():
    img = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle((5, 5, 30, 15), fill=(0, 0, 0, 255))
    tampon = io.BytesIO()
    img.save(tampon, "PNG")
    lu = _ouvrir(tampon.getvalue()).convert("RGB")
    # Rendu sur fond blanc : le pixel transparent doit etre blanc.
    assert lu.getpixel((1, 1)) == (255, 255, 255)


# --- NOEUD_TEXTE lineaire, equivalent a l'ancienne expression -------------- #

def test_noeud_texte_equivalent_a_l_ancienne_expression():
    import random
    import re

    from caviardoc.documents.docx import NOEUD_TEXTE
    ancienne = re.compile(
        r"<(w:t|w:delText|w:instrText|a:t|m:t)((?:\s[^>]*?)?)(/>|>(.*?)</\1>)",
        re.DOTALL)
    briques = ["<w:t>", "<w:t/>", "<w:t a='1'>", "<w:t a='1'/>", "</w:t>",
               "<w:delText>", "</w:delText>", "<a:t>", "</a:t>", "<w:tx>",
               "<w:t\n>", "abc", "\n", "<w:p>", "</w:p>", "<w:t >", "<m:t/>"]
    hasard = random.Random(7)
    for _ in range(3000):
        xml = "".join(hasard.choice(briques) for _ in range(hasard.randint(1, 14)))
        attendu = [(m.start(), m.end(), m.group(1), m.group(2), m.group(4))
                   for m in ancienne.finditer(xml)]
        obtenu = [(m.start(), m.end(), m.group(1), m.group(2), m.group(4))
                  for m in NOEUD_TEXTE.finditer(xml)]
        assert obtenu == attendu, xml


# --- Lecture bornee des archives ------------------------------------------- #

def test_lecture_archive_bornee_aux_octets_reels(monkeypatch, tmp_path):
    import zipfile

    from caviardoc.documents import docx
    monkeypatch.setattr(docx, "INFLATION_MAX_DOCX", 1000)
    chemin = tmp_path / "r.docx"
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"a" * 5000)
    with pytest.raises(ValueError, match="volumineux"):
        docx._lire_archive(chemin)


def test_lecture_archive_bornee_en_nombre_d_entrees(monkeypatch, tmp_path):
    import zipfile

    from caviardoc.documents import docx
    monkeypatch.setattr(docx, "ENTREES_MAX_DOCX", 2)
    chemin = tmp_path / "r.docx"
    with zipfile.ZipFile(chemin, "w") as z:
        for i in range(3):
            z.writestr(f"x{i}.xml", b"a")
    with pytest.raises(ValueError, match="complexe"):
        docx._lire_archive(chemin)


def test_relecture_bornee(monkeypatch):
    import io
    import zipfile

    from caviardoc.documents import docx
    monkeypatch.setattr(docx, "INFLATION_MAX_DOCX", 1000)
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"a" * 5000)
    with pytest.raises(ValueError):
        docx._relire_archive(tampon.getvalue(), {"x": "y"}, "fra")


# --- onnxruntime : telemetrie coupee --------------------------------------- #

def test_telemetrie_onnxruntime_coupee(monkeypatch):
    import types

    from caviardoc import engine
    appels = []
    faux = types.ModuleType("onnxruntime")
    faux.set_default_logger_severity = lambda n: appels.append(("log", n))
    faux.disable_telemetry_events = lambda: appels.append(("telemetrie",))
    monkeypatch.setitem(sys.modules, "onnxruntime", faux)
    engine._faire_taire_avertissements()
    assert ("telemetrie",) in appels


def test_telemetrie_absente_ne_casse_rien(monkeypatch):
    import types

    from caviardoc import engine
    faux = types.ModuleType("onnxruntime")
    faux.set_default_logger_severity = lambda n: None
    monkeypatch.setitem(sys.modules, "onnxruntime", faux)
    engine._faire_taire_avertissements()
