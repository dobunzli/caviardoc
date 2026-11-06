"""Boites des mots de la couche texte : jamais plus hautes que l'encre plausible.

Cas d'un courrier reel : sa police embarquee declarait 1,49 et 0,43 corps
d'ascendante et de descendante. Chaque mot recevait une boite de deux
interlignes, et le nom d'un medecin, masque a bon droit, noircissait le bas
de la ligne du dessus. Les textes ci-dessous sont inventes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")

from caviardoc.documents import ocr  # noqa: E402
from caviardoc.documents.ocr import mots_depuis_couche_texte  # noqa: E402

TAILLE = 11.0
ASC, DESC = 1.49, -0.43       # metriques gonflees de la police embarquee
INTERLIGNE = 14.2


class _PageGonflee:
    """Page dont la couche porte des metriques gonflees, comme `pymupdf` les rend."""

    rotation_matrix = pymupdf.Identity
    rect = pymupdf.Rect(0, 0, 595, 842)

    def __init__(self, lignes, direction=(1, 0), asc=ASC, desc=DESC):
        self._mots, blocs = [], []
        for n, (x, base, mots) in enumerate(lignes):
            y0, y1 = base - asc * TAILLE, base - desc * TAILLE
            spans = []
            for i, mot in enumerate(mots):
                x0, x1 = x + 60 * i, x + 60 * i + 50
                self._mots.append((x0, y0, x1, y1, mot, n, 0, i))
                spans.append({"size": TAILLE, "origin": (x0, base),
                              "bbox": (x0, y0, x1, y1), "text": mot})
            blocs.append({"type": 0, "lines": [{"dir": direction,
                                                "spans": spans}]})
        self._dict = {"blocks": blocs}

    def get_text(self, genre):
        return self._mots if genre == "words" else self._dict


def test_une_boite_gonflee_est_ramenee_autour_de_la_ligne_de_base():
    base = 100.0
    lecture = mots_depuis_couche_texte(
        _PageGonflee([(72, base, ["Docteure", "Zorglub"])]), dpi=72)
    for mot in lecture.mots:
        assert mot.y0 == pytest.approx(base - ocr.COUCHE_ASCENDANTE_MAX * TAILLE)
        assert mot.y1 == pytest.approx(base + ocr.COUCHE_DESCENDANTE_MAX * TAILLE)


def test_le_nom_ne_mord_plus_sur_la_ligne_du_dessus():
    dessus, dessous = 100.0, 100.0 + INTERLIGNE
    lecture = mots_depuis_couche_texte(_PageGonflee([
        (72, dessus, ["traitement", "suivi"]),
        (72, dessous, ["Docteure", "Zorglub"])]), dpi=72)
    haut = [m for m in lecture.mots if m.texte in ("traitement", "suivi")]
    nom = [m for m in lecture.mots if m.texte in ("Docteure", "Zorglub")]
    # Avant, le nom commencait au-dessus de la ligne de base du dessus. Il
    # doit maintenant commencer sous le corps de ses lettres, au niveau des
    # descendantes (0,21 corps pour une Arial). Les capitales du dessous
    # montent a 0,75 corps : la ligne du dessus doit s'arreter avant.
    assert all(m.y0 > dessus + 0.2 * TAILLE for m in nom)
    assert all(m.y1 < dessous - 0.75 * TAILLE for m in haut)


def test_un_texte_vertical_garde_sa_boite():
    page = _PageGonflee([(72, 100.0, ["Zorglub"])], direction=(0, -1))
    (mot,) = mots_depuis_couche_texte(page, dpi=72).mots
    assert (mot.y0, mot.y1) == pytest.approx((100.0 - ASC * TAILLE,
                                              100.0 - DESC * TAILLE))


def test_une_boite_sobre_n_est_jamais_agrandie():
    page = _PageGonflee([(72, 100.0, ["Zorglub"])], asc=0.7, desc=-0.1)
    (mot,) = mots_depuis_couche_texte(page, dpi=72).mots
    assert (mot.y0, mot.y1) == pytest.approx((100.0 - 0.7 * TAILLE,
                                              100.0 + 0.1 * TAILLE))


def test_les_boites_d_une_police_ordinaire_couvrent_toute_l_encre():
    """Capitales accentuees et descendantes restent sous les boites."""
    from PIL import Image, ImageDraw

    d = pymupdf.open()
    p = d.new_page()
    for i, ligne in enumerate(["Égypte Ångström jg (q)", "Ÿves Ôtez gypj",
                               "ÉÈÊ ÀÂ ÇÎ qpgj"]):
        p.insert_text((72, 100 + 14 * i), ligne, fontsize=11)
    pix = p.get_pixmap(dpi=300)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    image = image.convert("L")
    dessin = ImageDraw.Draw(image)
    for mot in mots_depuis_couche_texte(p, dpi=300).mots:
        dessin.rectangle((mot.x0 - 2, mot.y0 - 2, mot.x1 + 2, mot.y1 + 2),
                         fill=255)
    assert image.getextrema()[0] >= 128, "de l'encre depasse des boites"


# --- un mot de l'image n'est « deja lu » que si sa boite connue le couvre ---

def _page_lue(*mots):
    from caviardoc.documents.ocr import MotOCR, PageOCR
    p = PageOCR(largeur=3000, hauteur=3000)
    for i, (texte, x0, y0, x1, y1) in enumerate(mots):
        p.mots.append(MotOCR(texte, x0, y0, x1, y1, 90, bloc=i, ligne=1))
        debut = len(p.texte) + (2 if p.texte else 0)
        p.texte += ("\n\n" if p.texte else "") + texte
        p.offsets.append((debut, debut + len(texte)))
    return p


def _completer(couche, lue):
    from PIL import Image
    return ocr.completer_par_les_images(
        couche, Image.new("L", (3000, 3000), 255), [(0, 0, 3000, 3000)],
        lecteur=lambda *a, **k: lue)


def test_un_nom_d_image_decale_de_sa_couche_est_garde_en_double():
    """Signature en image : le nom se voit au-dessus de sa couche texte."""
    couche = _page_lue(("Zorglub", 1301, 2850, 1520, 2913))
    lue = _page_lue(("Zorglub", 1301, 2832, 1515, 2869))
    assert _completer(couche, lue) == 1
    assert (couche.mots[-1].y0, couche.mots[-1].y1) == (2832, 2869)


def test_un_nom_couvert_par_sa_couche_n_est_pas_double():
    couche = _page_lue(("Zorglub", 1301, 2830, 1520, 2913))
    lue = _page_lue(("Zorglub", 1303, 2845, 1515, 2900))
    assert _completer(couche, lue) == 0
