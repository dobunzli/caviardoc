"""La verification apres ecriture compare avec le meme repli que la correction."""
from __future__ import annotations

import io
import unicodedata
import zipfile
from types import SimpleNamespace

import pytest

from caviardoc.documents import docx, images

NOM = "Zorglub Étienne"

VARIANTES = {
    "nfd": unicodedata.normalize("NFD", "ZORGLUB étienne"),
    "soft": "Zor­glub Étienne",
    "zero_width": "Zor​glub⁠ Étienne",
    "tiret_insecable": "Zorglub‑Étienne",
}


def _archive(corps: str) -> bytes:
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
           f'{corps}</w:t></w:r></w:p></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


@pytest.mark.parametrize("variante", VARIANTES.values(), ids=VARIANTES)
def test_fuite_xml_repliee_signalee(variante):
    xml, _ = docx._relire_archive(_archive(f"Bonjour {variante}."), {NOM}, "fra")
    assert NOM in xml


def test_xml_propre_non_signale():
    xml, _ = docx._relire_archive(_archive("Bonjour [PERSONNE_1]."), {NOM}, "fra")
    assert not xml


@pytest.mark.parametrize("variante", VARIANTES.values(), ids=VARIANTES)
def test_fuite_texte_assemble_repliee(variante):
    from caviardoc.documents import commun
    assert commun.chaines_lisibles(f"Bonjour {variante}.", {NOM}) == {NOM}


@pytest.mark.parametrize("variante", VARIANTES.values(), ids=VARIANTES)
def test_fuite_image_repliee(variante, monkeypatch):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buf, "PNG")
    monkeypatch.setattr(images, "ocr_image",
                        lambda *a, **k: SimpleNamespace(texte=f"Bonjour {variante}"))
    fuites = images.fuites_dans_images({"word/media/a.png": buf.getvalue()},
                                       {NOM}, "fra")
    assert fuites == {NOM: ["word/media/a.png"]}
