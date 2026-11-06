"""Les pages pivotees : le caviardage tombait a cote, en silence.

`page.get_text("words")` rend des coordonnees dans le repere brut du contenu ;
`page.get_pixmap()` dessine la page telle qu'elle s'affiche, `/Rotate` compris.
Sur une page pivotee les deux reperes divergent, et les rectangles noirs
atterrissaient ailleurs : le document sortait avec ses donnees intactes.

Ce n'est pas un cas rare. Un `/Rotate` apparait des qu'on assemble des envois
de plusieurs sources, ou qu'un scanner redresse une page de travers — soit
exactement la forme d'une demande de consilium.

Le pire n'etait pas la fuite mais son signalement : les pages non pivotees
etant correctes, la verification lisait le document, retrouvait les chaines
restees visibles ailleurs, et l'utilisateur voyait « 8 fuites » sans savoir
qu'une page entiere etait passee au travers.

Ces tests ne chargent pas le modele : ils mesurent la geometrie, qui est le
siege du defaut.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pymupdf = pytest.importorskip("pymupdf")
Image = pytest.importorskip("PIL.Image")

from caviardoc.documents.ocr import mots_depuis_couche_texte  # noqa: E402

DPI = 150
MOT = "14.04.2026"


def _document(rotation: int) -> "pymupdf.Document":
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    # Assez de texte pour que la couche soit jugee saine : c'est la condition
    # qui fait emprunter le chemin defectueux.
    for i in range(12):
        p.insert_text((60, 80 + i * 22),
                      f"Ligne {i} du courrier, rendez-vous du {MOT} a Echallens.",
                      fontsize=11)
    if rotation:
        p.set_rotation(rotation)
    return d


def _pixels(page, dpi: int = DPI):
    pix = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_les_boites_tombent_sur_le_texte(rotation):
    """Une boite doit recouvrir des pixels sombres : ceux du mot lui-meme.

    C'est l'assertion qui manquait. Elle vaut pour toutes les rotations, et
    c'est precisement ce que les trois quarts d'entre elles ne faisaient pas.
    """
    d = _document(rotation)
    page = d[0]
    lecture = mots_depuis_couche_texte(page, dpi=DPI)
    image = _pixels(page)

    boites = [m for m in lecture.mots if MOT in m.texte]
    assert boites, "le mot doit etre lu dans la couche texte"

    for mot in boites[:3]:
        x0, y0 = int(mot.x0), int(mot.y0)
        x1, y1 = int(mot.x1), int(mot.y1)
        assert 0 <= x0 < x1 <= image.width, f"boite hors image : {x0}-{x1}"
        assert 0 <= y0 < y1 <= image.height, f"boite hors image : {y0}-{y1}"
        morceau = image.crop((x0, y0, x1, y1))
        sombres = sum(1 for v in morceau.tobytes() if v < 128)
        assert sombres > 0, (f"rotation {rotation} : la boite ne couvre aucun "
                             "pixel de texte")


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_la_page_lue_a_les_dimensions_de_l_image(rotation):
    """Sinon les boites seraient bornees a un cadre qui n'existe pas."""
    d = _document(rotation)
    page = d[0]
    lecture = mots_depuis_couche_texte(page, dpi=DPI)
    image = _pixels(page)
    assert abs(lecture.largeur - image.width) <= 1
    assert abs(lecture.hauteur - image.height) <= 1


def test_le_controle_mord_sur_une_page_pivotee():
    """Sans la correction, la boite d'une page pivotee tombe sur du blanc.

    On refait ici le calcul fautif — coordonnees brutes, sans matrice de
    rotation — pour verifier que le test ci-dessus n'est pas vacant.
    """
    d = _document(90)
    page = d[0]
    image = _pixels(page)
    echelle = DPI / 72.0

    brut = [m for m in page.get_text("words") if MOT in m[4]][0]
    x0, y0, x1, y1 = (int(v * echelle) for v in brut[:4])
    morceau = image.crop((x0, y0, min(x1, image.width),
                          min(y1, image.height)))
    sombres = sum(1 for v in morceau.tobytes() if v < 128)
    assert sombres == 0, ("sans correction, la boite devrait tomber a cote — "
                          "si elle tombe juste, ce test ne prouve plus rien")


def test_une_page_droite_n_est_pas_touchee():
    """La matrice est l'identite sans rotation : rien ne doit bouger."""
    d = _document(0)
    page = d[0]
    lecture = mots_depuis_couche_texte(page, dpi=DPI)
    echelle = DPI / 72.0
    brut = [m for m in page.get_text("words") if MOT in m[4]][0]
    mot = [m for m in lecture.mots if MOT in m.texte][0]
    assert abs(mot.x0 - brut[0] * echelle) < 0.5
    assert abs(mot.y0 - brut[1] * echelle) < 0.5
