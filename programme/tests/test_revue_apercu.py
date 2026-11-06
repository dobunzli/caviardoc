"""La relecture d'un PDF montre la page, et un cadre tracé dessus masque des mots."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import interface  # noqa: E402
from caviardoc.documents.ocr import MotOCR, PageOCR  # noqa: E402
from caviardoc.engine import Span  # noqa: E402
from caviardoc.revue import preparer  # noqa: E402

TEXTE = "Monsieur Dupont habite Echallens"


def _lecture() -> PageOCR:
    """Quatre mots alignes, chacun large de 100 px sur une page de 400 x 200."""
    mots, offsets, curseur = [], [], 0
    for i, mot in enumerate(TEXTE.split()):
        debut = TEXTE.index(mot, curseur)
        offsets.append((debut, debut + len(mot)))
        mots.append(MotOCR(mot, i * 100, 50, i * 100 + 90, 70, 90, 1, 1))
        curseur = debut + len(mot)
    return PageOCR(texte=TEXTE, mots=mots, offsets=offsets, largeur=400,
                   hauteur=200)


@pytest.fixture
def tache():
    from PIL import Image

    t = interface.Tache("t", "d", "doc.pdf")
    spans = [Span(9, 15, "PER", 0.6, "Dupont", "modele")]
    t.session = preparer("doc.pdf", [("page 1", TEXTE, spans)])
    t.textes = {"page 1": TEXTE}
    t.recevoir_apercu("page 1", Image.new("RGB", (400, 200), "white"), _lecture())
    return t


def test_la_vue_porte_la_page_et_les_boites(tache):
    vue = interface.revue_en_json(tache)
    partie = vue["parties"][0]
    assert partie["apercu"] == {"largeur": 400, "hauteur": 200}
    (passage,) = partie["passages"]
    assert passage["texte"] == "Dupont" and passage["origine"] == "modele"
    (x0, y0, x1, y1), = partie["boites"][passage["id"]]
    # Le deuxieme mot, dilate de 2 px, en fractions de la page.
    assert (x0, x1) == pytest.approx((98 / 400, 192 / 400))
    assert (y0, y1) == pytest.approx((48 / 200, 72 / 200))


def test_sans_apercu_la_vue_reste_celle_du_texte():
    t = interface.Tache("t", "d", "doc.docx")
    t.session = preparer("doc.docx", [("corps", TEXTE, [])])
    t.textes = {"corps": TEXTE}
    partie = interface.revue_en_json(t)["parties"][0]
    assert "apercu" not in partie and "boites" not in partie


def test_un_cadre_masque_les_mots_qu_il_entoure(tache):
    # Du deuxieme au troisieme mot : un seul passage.
    ajoutes = interface.ajouter_zone(tache, "page 1", 0.26, 0.2, 0.74, 0.4)
    assert ajoutes == 1
    nouveau = tache.session.ajoutees[0]
    assert nouveau.texte == "Dupont habite" and nouveau.origine == "humain"


def test_deux_mots_separes_font_deux_passages(tache):
    # Le cadre couvre toute la ligne mais on ne retient que les mots dont le
    # centre y tombe : les extremes seulement.
    interface.ajouter_zone(tache, "page 1", 0.0, 0.2, 0.12, 0.4)
    interface.ajouter_zone(tache, "page 1", 0.76, 0.2, 1.0, 0.4)
    assert [d.texte for d in tache.session.ajoutees] == ["Monsieur", "Echallens"]


def test_un_cadre_sans_mot_ne_masque_rien(tache):
    assert interface.ajouter_zone(tache, "page 1", 0.0, 0.8, 1.0, 1.0) == 0
    assert not tache.session.ajoutees


def test_une_page_inconnue_est_refusee(tache):
    with pytest.raises(KeyError):
        interface.ajouter_zone(tache, "page 9", 0, 0, 1, 1)


def test_l_apercu_est_une_image_reduite(tache):
    from PIL import Image
    import io

    apercu = tache.apercus["page 1"]
    image = Image.open(io.BytesIO(apercu.jpeg))
    assert image.format == "JPEG" and image.size[0] <= interface.APERCU_LARGEUR
    assert (apercu.largeur, apercu.hauteur) == (400, 200)


def test_une_zone_est_un_passage_a_part_dessine_sur_la_page(tache):
    from caviardoc.revue import Zone, decider
    tache.session.zones.append(Zone(
        identifiant="page 1:zone:0", partie="page 1", genre="entete",
        boite=[40.0, 20.0, 200.0, 100.0], texte="Monsieur"))
    partie = interface.revue_en_json(tache)["parties"][0]
    zone = next(p for p in partie["passages"] if p["id"] == "page 1:zone:0")
    assert zone["zone"] is True and zone["masque"] is True
    assert zone["etiquette"] == "zone : bloc d'en-tête"
    assert partie["boites"]["page 1:zone:0"] == [[0.1, 0.1, 0.5, 0.5]]
    # Les compteurs de doutes restent ceux du texte.
    assert partie["detections"] == 1 and partie["doutes"] == 1
    decider(tache.session, {"page 1:zone:0": {"etat": "rejetee"}})
    zone = next(p for p in interface.revue_en_json(tache)["parties"][0]["passages"]
                if p["id"] == "page 1:zone:0")
    assert zone["masque"] is False
