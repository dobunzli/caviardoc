"""Une page scannee est lue deux fois, et l'OCR d'un autre ne fait pas foi.

Deux fuites sur des documents reels l'ont montre. Sur une lettre d'hopital,
Tesseract, dans son mode par defaut, fusionnait les deux colonnes de l'en-tete
et sautait l'adresse e-mail de l'expediteur et le nom du destinataire : ni
detectes, ni masques. Sur un compte rendu de neurologie, la couche invisible
posee par le scanner lisait « Nat.h:9.e.. » ; le nom de la secretaire
restait lisible en pied de page.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc.documents.commun import propager_chaines  # noqa: E402
from caviardoc.documents.ocr import (MotOCR, PageOCR, completer,  # noqa: E402
                                   couche_invisible)
from caviardoc.pipeline import occurrences_approchees  # noqa: E402


def _lecture(*mots):
    """PageOCR a partir de (texte, x0, y0, x1, y1), un bloc par mot."""
    p = PageOCR(largeur=1000, hauteur=1000)
    for i, (texte, x0, y0, x1, y1) in enumerate(mots):
        p.mots.append(MotOCR(texte, x0, y0, x1, y1, 90, bloc=i, ligne=1))
        debut = len(p.texte) + (2 if p.texte else 0)
        p.texte += ("\n\n" if p.texte else "") + texte
        p.offsets.append((debut, debut + len(texte)))
    return p


class TestCompleter:
    def test_mot_absent_ajoute(self):
        lecture = _lecture(("Madame", 600, 100, 700, 120))
        autre = _lecture(("Claire", 600, 140, 680, 160))
        assert completer(lecture, autre, 1000) == 1
        assert lecture.texte.endswith("Claire")
        debut, fin = lecture.offsets[-1]
        assert lecture.texte[debut:fin] == "Claire"
        assert lecture.mots[-1].bloc == 1000

    def test_meme_mot_au_meme_endroit_ignore(self):
        lecture = _lecture(("Amold-Vautier", 600, 100, 800, 120))
        autre = _lecture(("Arnold-Vautier", 602, 101, 801, 121))
        assert completer(lecture, autre, 1000) == 0

    def test_autre_mot_au_meme_endroit_ajoute(self):
        """La ou deux lignes ont ete fusionnees, la place est prise par un mot
        qui n'a rien a voir : la position seule ne dit pas que c'est lu."""
        lecture = _lecture(("Fa", 290, 630, 340, 700))
        autre = _lecture(("Tél.", 293, 636, 335, 663))
        assert completer(lecture, autre, 1000) == 1

    def test_ponctuation_seule_ignoree(self):
        lecture = _lecture(("Madame", 600, 100, 700, 120))
        autre = _lecture(("—", 100, 500, 120, 510))
        assert completer(lecture, autre, 1000) == 0

    def test_mots_d_une_meme_ligne_restent_ensemble(self):
        lecture = PageOCR(largeur=1000, hauteur=1000)
        autre = _lecture(("Claire", 600, 140, 680, 160),
                         ("Bessard", 690, 140, 780, 160))
        autre.mots[1] = MotOCR("Bessard", 690, 140, 780, 160, 90, bloc=0,
                               ligne=1)
        completer(lecture, autre, 1000)
        assert lecture.texte == "Claire Bessard"


class TestNomMalLu:
    """Sur un resultat de laboratoire scanne, le prenom du patient etait lu
    « JEAN-—BIDUIE » : ni le modele ni la propagation ne le reconnaissaient."""

    def _lus(self, texte, mot):
        return [texte[d:f] for d, f in occurrences_approchees(texte, mot)]

    def test_prenom_abime_retrouve(self):
        assert self._lus("Prénom _JEAN-—BIDUIE Né le", "Jean-Bidule") == [
            "JEAN-—BIDUIE"]

    def test_patronyme_abime_retrouve(self):
        assert self._lus("M. Zorglnb, vu ce jour", "ZORGLUB") == [
            "Zorglnb"]

    def test_autre_nom_ignore(self):
        assert self._lus("Dr Zorglolz", "Zorglub") == []

    def test_minuscule_ignoree(self):
        assert self._lus("une urine claires", "Claire") == []

    def test_nom_court_jamais_approche(self):
        assert self._lus("Paul Pauk", "Pau") == []

    def test_propagation_du_nom_complet(self):
        texte = "ZORGLUB Jean-Bidule\n\nNom ZORGLUB Prénom _JEAN-—BIDUIE"
        spans = propager_chaines(texte, [],
                                 {"ZORGLUB Jean-Bidule": "personne"})
        assert any(texte[s.start:s.end] == "JEAN-—BIDUIE" for s in spans)


pymupdf = pytest.importorskip("pymupdf")


def _pdf(render_mode: int):
    d = pymupdf.open()
    page = d.new_page()
    page.insert_text((72, 72), "Sophie Keller", render_mode=render_mode)
    return d


class TestCoucheInvisible:
    def test_couche_invisible_reconnue(self):
        assert couche_invisible(_pdf(3)[0])

    def test_couche_visible(self):
        assert not couche_invisible(_pdf(0)[0])

    def test_page_sans_texte(self):
        d = pymupdf.open()
        assert not couche_invisible(d.new_page())


pytest.importorskip("pytesseract")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents.caviardage import _lire_page  # noqa: E402

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")


def _police(taille):
    for nom in ("Arial.ttf", "DejaVuSans.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(nom, taille)
        except OSError:
            continue
    return ImageFont.load_default(size=taille)


@besoin_ocr
def test_scan_interrogeable_relu():
    """La couche invisible lit mal le nom ; la page est relue, et le nom y est."""
    image = Image.new("RGB", (1240, 1754), "white")
    dessin = ImageDraw.Draw(image)
    lignes = ["Consultation de neurologie du 9 decembre 2021",
              "Le patient presente un etat confusionnel aigu",
              "Genere le 13.12.2021 par Sophie Keller"]
    for i, ligne in enumerate(lignes):
        dessin.text((120, 200 + 80 * i), ligne, fill="black", font=_police(36))
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")

    d = pymupdf.open()
    page = d.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=tampon.getvalue())
    # Ce que le scanner a cru lire, pose sous l'image comme il le fait.
    lu_par_le_scanner = ["Consultation de neurologie du 9 decembre 2021",
                         "Le patient presente un etat confusionnel aigu",
                         "Genere le 13.12.2021 par Sop.h:9.e.. 'Kelle:"]
    for i, ligne in enumerate(lu_par_le_scanner):
        page.insert_text((58, 110 + 38 * i), ligne, fontsize=17,
                         render_mode=3)

    lecture, source = _lire_page(page, 150, "fra")
    assert source == "OCR + couche invisible"
    assert "Sophie" in lecture.texte
    assert "Keller" in lecture.texte
