"""Ecriture manuscrite que l'OCR ne lit pas : masquee et signalee.

Releve sur un ECG scanne : nom et dates ecrits a la main au-dessus de la
grille, que l'OCR ne lisait qu'en bribes sans confiance ; ils restaient
lisibles dans un document presente comme verifie. Tout est synthetique ici :
boucles d'une ecriture cursive tracees au crayon, grille de papier millimetre,
trace, texte imprime.
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")
ImageFont = pytest.importorskip("PIL.ImageFont")

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents.manuscrit import (NOTE, NOTE_DOUTE,  # noqa: E402
                                           analyser, boites_manuscrit)
from caviardoc.documents.ocr import MotOCR, PageOCR  # noqa: E402

DPI = 300
MM = DPI / 25.4
LARGEUR, HAUTEUR = 2400, 1800

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")


def _feuille():
    return Image.new("RGB", (LARGEUR, HAUTEUR), (250, 250, 248))


def _lecture(*mots):
    """Une lecture faite de ces mots : (texte, boite, confiance)."""
    p = PageOCR(largeur=LARGEUR, hauteur=HAUTEUR, dpi=DPI)
    pos = 0
    for i, (texte, (x0, y0, x1, y1), confiance) in enumerate(mots):
        p.mots.append(MotOCR(texte=texte, x0=x0, y0=y0, x1=x1, y1=y1,
                             confiance=confiance, bloc=i, ligne=0))
        p.offsets.append((pos, pos + len(texte)))
        pos += len(texte) + 1
    p.texte = " ".join(m[0] for m in mots)
    return p


def cursive(page, x, y, longueur_mm=40, hauteur_mm=7, gris=150, epaisseur=4):
    """Des boucles d'ecriture liee (« eeee »), au crayon. Rend sa boite."""
    dessin = ImageDraw.Draw(page)
    rayon = hauteur_mm * MM / 2
    pas = rayon * 0.8
    points = []
    t = 0.0
    while pas * t / (2 * math.pi) < longueur_mm * MM - 2 * rayon:
        points.append((x + rayon + pas * t / (2 * math.pi) - rayon * math.sin(t),
                       y + rayon + rayon * math.cos(t)))
        t += 0.1
    dessin.line(points, fill=(gris, gris, gris), width=epaisseur, joint="curve")
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def grille(page, haut, bas, gauche=60, droite=LARGEUR - 60):
    """Papier millimetre d'ECG scanne : traits forts tous les 5 mm, et traits
    fins tous les mm, hachures comme le scanner les rend (3 mm sur 4)."""
    dessin = ImageDraw.Draw(page)
    fin, fort = (190, 190, 190), (165, 165, 165)
    pas = round(4 * MM)
    for i in range(int((droite - gauche) / MM) + 1):
        x = round(gauche + i * MM)
        if i % 5 == 0:
            dessin.line([(x, haut), (x, bas)], fill=fort, width=3)
        else:
            for y in range(haut, bas, pas):
                dessin.line([(x, y), (x, min(bas, y + 3 * pas // 4))],
                            fill=fin, width=2)
    for i in range(int((bas - haut) / MM) + 1):
        y = round(haut + i * MM)
        if i % 5 == 0:
            dessin.line([(gauche, y), (droite, y)], fill=fort, width=3)
        else:
            for x in range(gauche, droite, pas):
                dessin.line([(x, y), (min(droite, x + 3 * pas // 4), y)],
                            fill=fin, width=2)


def trace(page, y, gauche=60, droite=LARGEUR - 60):
    """Un trace d'ECG : ligne de base, onde P, QRS pointu, onde T ; coupe ca
    et la, comme un scan perd un bout de trait."""
    dessin = ImageDraw.Draw(page)
    morceau = []
    for x in range(gauche, droite):
        phase = (x - gauche) % int(22 * MM)
        v = 0.0
        v += 12 * math.exp(-((phase - 40) / 12) ** 2)           # P
        if 80 <= phase < 88:
            v -= (phase - 80) * 2                               # Q
        elif 88 <= phase < 96:
            v += (phase - 88) * 18 - 16                         # R
        elif 96 <= phase < 106:
            v += 128 - (phase - 96) * 15                        # descente
        v += 25 * math.exp(-((phase - 180) / 25) ** 2)          # T
        if (x - gauche) % int(17 * MM) < 12:                    # coupure
            if len(morceau) > 1:
                dessin.line(morceau, fill=(30, 30, 30), width=3)
            morceau = []
        else:
            morceau.append((x, y - v))
    if len(morceau) > 1:
        dessin.line(morceau, fill=(30, 30, 30), width=3)


def carre_d_etalonnage(page, x, y):
    ImageDraw.Draw(page).rectangle([x, y, x + 5 * MM, y + 10 * MM],
                                   fill=(10, 10, 10))


def contient(boite, rect, marge=0):
    return (boite[0] <= rect[0] + marge and boite[1] <= rect[1] + marge
            and boite[2] >= rect[2] - marge and boite[3] >= rect[3] - marge)


def test_ecriture_sur_page_blanche_masquee_avec_une_marge():
    page = _feuille()
    ecrit = cursive(page, 600, 300)
    boites = boites_manuscrit(page, _lecture(), DPI)
    assert len(boites) == 1
    assert contient(boites[0], ecrit)
    # Une marge, mais pas toute la page.
    assert boites[0][0] < ecrit[0] and boites[0][3] > ecrit[3]
    assert boites[0][2] - boites[0][0] < 60 * MM


def test_ecriture_meme_en_bribes_de_l_ocr_masquee():
    # L'OCR en tire des bribes sans confiance : elles n'expliquent rien.
    page = _feuille()
    x0, y0, x1, y1 = cursive(page, 600, 300)
    lecture = _lecture(("Jh~", (x0, y0 + 20, x0 + 60, y0 + 35), 12),
                       ("eeo", (x0 + 80, y0 + 30, x0 + 120, y0 + 40), 40))
    assert len(boites_manuscrit(page, lecture, DPI)) == 1


def test_grille_trace_et_carres_d_etalonnage_epargnes():
    page = _feuille()
    grille(page, 400, 1500)
    for y in (650, 1000, 1350):
        trace(page, y)
        for x in (100, 900, 1700):
            carre_d_etalonnage(page, x, y - 10 * MM)
    assert boites_manuscrit(page, _lecture(), DPI) == []


def test_ecriture_au_dessus_d_une_grille_masquee_seule():
    page = _feuille()
    grille(page, 400, 1500)
    trace(page, 700)
    carre_d_etalonnage(page, 100, 700 - 10 * MM)
    ecrit = cursive(page, 900, 250, longueur_mm=50)
    boites = boites_manuscrit(page, _lecture(), DPI)
    assert len(boites) == 1
    assert contient(boites[0], ecrit)
    assert boites[0][3] < 400                      # rien de la grille


def test_ecriture_sombre_sur_la_grille_masquee():
    # Au stylo, sur la grille meme : plus sombre que la grille, masquee ;
    # le trace, plus bas, n'est pas couvert.
    page = _feuille()
    grille(page, 400, 1500)
    trace(page, 1200)
    carre_d_etalonnage(page, 100, 1200 - 10 * MM)
    ecrit = cursive(page, 900, 600, longueur_mm=50, gris=60)
    boites = boites_manuscrit(page, _lecture(), DPI)
    assert any(contient(b, ecrit) for b in boites)
    assert all(b[3] < 1200 - 15 * MM for b in boites)


def test_ecriture_pale_sur_la_grille_non_masquee_mais_douteuse():
    # Au crayon, sur la grille : pas plus sombre qu'elle, et l'interieur de
    # la grille est ecarte. Elle n'est pas masquee, mais elle est rendue
    # comme douteuse : la page sera signalee.
    page = _feuille()
    grille(page, 400, 1500)
    ecrit = cursive(page, 900, 600, longueur_mm=50)
    masquees, douteuses = analyser(page, _lecture(), DPI)
    assert not any(contient(b, ecrit) for b in masquees)
    # L'exigence est le signalement de la page (verifiee a l'oeil) : la boite
    # n'est qu'un pointeur, elle n'a pas a couvrir toute l'ecriture, il suffit
    # qu'elle la recoupe.
    assert douteuses
    assert any(min(b[2], ecrit[2]) > max(b[0], ecrit[0])
               and min(b[3], ecrit[3]) > max(b[1], ecrit[1])
               for b in douteuses)


def test_trace_sans_grille_epargne():
    page = _feuille()
    trace(page, 900)
    assert boites_manuscrit(page, _lecture(), DPI) == []


def test_filets_cadres_et_aplats_epargnes():
    page = _feuille()
    d = ImageDraw.Draw(page)
    d.rectangle([200, 200, 2200, 1600], outline=(0, 0, 0), width=4)
    for y in range(300, 1600, 100):                 # un tableau
        d.line([(200, y), (2200, y)], fill=(0, 0, 0), width=3)
    d.line([(1200, 200), (1200, 1600)], fill=(0, 0, 0), width=3)
    d.rectangle([400, 1650, 400 + 12 * MM, 1650 + 10 * MM], fill=(0, 0, 0))
    assert boites_manuscrit(page, _lecture(), DPI) == []


def _texte(page, x, y, texte, taille):
    d = ImageDraw.Draw(page)
    fonte = ImageFont.load_default(size=taille)
    d.text((x, y), texte, fill=(0, 0, 0), font=fonte)
    return d.textbbox((x, y), texte, font=fonte)


def test_imprime_lu_epargne_meme_en_grand():
    page = _feuille()
    mots = []
    for i, mot in enumerate(("Rapport", "de", "consultation")):
        boite = _texte(page, 300 + i * 600, 300, mot, 80)    # titre, 7 mm
        mots.append((mot, boite, 92))
    for ligne in range(6):
        for i, mot in enumerate(("tension", "stable", "bilan", "normal")):
            boite = _texte(page, 300 + i * 400, 600 + ligne * 60, mot, 36)
            mots.append((mot, boite, 90))
    assert boites_manuscrit(page, _lecture(*mots), DPI) == []


def test_imprime_non_lu_de_taille_ordinaire_epargne():
    # Un paragraphe que l'OCR aurait manque n'a pas la hauteur d'une ecriture.
    page = _feuille()
    for ligne in range(8):
        _texte(page, 300, 600 + ligne * 60,
               "suivi habituel, controle dans trois mois", 36)
    assert boites_manuscrit(page, _lecture(), DPI) == []


def test_imprime_lu_de_facon_plausible_explique_l_encre():
    page = _feuille()
    boite = _texte(page, 300, 300, "Consultation", 80)
    assert boites_manuscrit(page, _lecture(("Consultatiom", boite, 55)),
                            DPI) == []


def test_note():
    assert NOTE == "écriture manuscrite masquée, à vérifier"


def _pdf_scanne(chemin, image):
    import pymupdf
    d = pymupdf.open()
    p = d.new_page(width=LARGEUR * 72 / DPI, height=HAUTEUR * 72 / DPI)
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    p.insert_image(p.rect, stream=tampon.getvalue())
    d.save(chemin)


def _rendu(chemin):
    import pymupdf
    with pymupdf.open(chemin) as d:
        return Image.open(io.BytesIO(
            d[0].get_pixmap(dpi=DPI).tobytes("png"))).convert("L")


@besoin_ocr
def test_pdf_scanne_ecriture_masquee_et_signalee(tmp_path):
    from caviardoc.documents.caviardage import caviarder_pdf
    page = _feuille()
    grille(page, 600, 1500)
    trace(page, 900)
    ecrit = cursive(page, 800, 300, longueur_mm=50)
    _pdf_scanne(tmp_path / "e.pdf", page)
    rapport = caviarder_pdf(None, tmp_path / "e.pdf", tmp_path / "o.pdf")
    rp = rapport.pages[0]
    assert rp.source.startswith("OCR")
    assert rp.manuscrit == 1
    sortie = _rendu(tmp_path / "o.pdf")
    zone = sortie.crop(tuple(int(v) for v in ecrit))
    assert max(zone.getdata()) < 40                    # entierement noire
    # Le trace, sous la grille, est intact.
    assert min(sortie.crop((60, 900 - 140, LARGEUR - 60, 920)).getdata()) < 80


@besoin_ocr
def test_pdf_scanne_sans_ecriture_rien_a_signaler(tmp_path):
    from caviardoc.documents.caviardage import caviarder_pdf
    page = _feuille()
    grille(page, 400, 1500)
    trace(page, 900)
    _pdf_scanne(tmp_path / "e.pdf", page)
    rapport = caviarder_pdf(None, tmp_path / "e.pdf", tmp_path / "o.pdf")
    assert rapport.pages[0].manuscrit == 0


@besoin_ocr
def test_pdf_scanne_ecriture_douteuse_signalee(tmp_path):
    from caviardoc.documents.caviardage import caviarder_pdf
    page = _feuille()
    grille(page, 400, 1500)
    cursive(page, 900, 600, longueur_mm=50)
    _pdf_scanne(tmp_path / "e.pdf", page)
    rapport = caviarder_pdf(None, tmp_path / "e.pdf", tmp_path / "o.pdf")
    rp = rapport.pages[0]
    assert rp.source.startswith("OCR")
    assert rp.manuscrit == 0 and rp.manuscrit_douteux >= 1


def test_a_verifier_et_pastille_de_la_page():
    from caviardoc import interface
    propre = {"fuites": [], "verifie": True, "reserves": [],
              "images_opaques": [], "images_metadonnees": [],
              "repli_complet": False, "manuscrit": []}
    assert interface.a_verifier(propre) is False
    assert interface.a_verifier(
        {**propre, "manuscrit": [{"page": 1, "nombre": 1}]}) is True
    # La pastille suit la meme regle, et l'encart dit ce qu'il y a a voir.
    gravite = interface._SCRIPT[interface._SCRIPT.index("function gravite"):]
    gravite = gravite[:gravite.index("\n}\n")]
    assert "b.manuscrit" in gravite
    assert "' : écriture manuscrite masquée, à vérifier'" in interface._SCRIPT
    # L'encre douteuse aussi : a verifier, jamais un succes.
    assert interface.a_verifier(
        {**propre, "manuscrit_douteux": [{"page": 2, "nombre": 3}]}) is True
    assert "b.manuscrit_douteux" in gravite
    assert ("'Écriture manuscrite possible non masquée (page ' + m.page"
            in interface._SCRIPT)
    assert "à vérifier à l'œil" in interface._SCRIPT


def test_note_de_doute():
    assert NOTE_DOUTE == "écriture manuscrite possible non masquée, à vérifier à l'œil"
