"""Audit vague 3, voie H : detection (generiques, dates, patient), logos
en page pivotee, lecture des images, format des metadonnees."""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw  # noqa: E402

from caviardoc.engine import Span  # noqa: E402
from caviardoc.pipeline import (PolitiqueDates, filtrer_dates,  # noqa: E402
                                filtrer_generiques)


def _span(texte, aiguille, label, origine="modele"):
    d = texte.index(aiguille)
    return Span(d, d + len(aiguille), label, 0.9, aiguille, origine)


# --- 1. un nom fait de vocabulaire, apres une civilite -------------------- #

def test_civilite_et_nom_de_famille_homonyme_du_vocabulaire_conserves():
    t = "Nous recevons Monsieur Page ce matin."
    sp = _span(t, "Monsieur Page", "personne")
    assert filtrer_generiques([sp]) == [sp]


def test_civilite_seule_toujours_ecartee():
    t = "Nous recevons Monsieur ce matin."
    assert filtrer_generiques([_span(t, "Monsieur", "personne")]) == []


def test_vocabulaire_sans_majuscule_toujours_ecarte():
    t = "le patient est sorti"
    assert filtrer_generiques([_span(t, "le patient", "personne")]) == []


# --- 2. date de naissance du modele perdue a la fusion -------------------- #

def test_naissance_du_modele_ne_devient_pas_date_relative():
    t = "Dossier du 12.03.1950 ouvert."
    regle = _span(t, "12.03.1950", "date", "regle")
    modele = _span(t, "12.03.1950", "date de naissance")
    gardes = filtrer_dates(t, [regle, modele], PolitiqueDates.RELATIVES)
    assert gardes
    assert all(g.label == "date de naissance" for g in gardes)


# --- 3. patient designe par un signal non valide -------------------------- #

def test_naissance_par_etiquette_seule_n_exclut_pas_le_patient_mentionne():
    from caviardoc.roles import noms_du_patient
    t = "Imprime par Sophie Rey 12.03.2024\n\nPatient : Louis Aubry, consulte."
    spans = [_span(t, "Sophie Rey", "personne"),
             _span(t, "12.03.2024", "date de naissance"),
             _span(t, "Louis Aubry", "personne")]
    assert "Louis Aubry" in noms_du_patient(t, spans)


# --- 4. logos d'une page pivotee ------------------------------------------ #

def test_boites_logos_suivent_la_rotation_de_la_page():
    pymupdf = pytest.importorskip("pymupdf")
    from caviardoc.documents.zones import boites_logos
    tampon = io.BytesIO()
    Image.new("RGB", (40, 20), "red").save(tampon, "PNG")
    d = pymupdf.open()
    page = d.new_page(width=300, height=400)
    page.insert_image(pymupdf.Rect(20, 30, 120, 80), stream=tampon.getvalue())
    page.set_rotation(90)
    (x0, y0, x1, y1), = boites_logos(page, 72)
    # Image de 100 x 50 : sur la page pivotee de 90 degres, 50 x 100.
    assert abs((x1 - x0) - 50) < 1 and abs((y1 - y0) - 100) < 1
    assert 0 <= x0 and x1 <= page.rect.width and y1 <= page.rect.height


# --- 5. fusion OCR : le mot juste ecarte par la position ------------------ #

def test_ocr_image_ne_jette_pas_un_mot_different_sur_un_mot_de_la_couche(
        monkeypatch):
    from caviardoc.documents import ocr
    from caviardoc.documents.ocr import MotOCR, PageOCR
    couche = PageOCR(largeur=1000, hauteur=1000)
    couche.mots.append(MotOCR("Brxchbxum", 100, 100, 300, 130, 40, bloc=0,
                              ligne=1))
    couche.texte = "Brxchbxum"
    couche.offsets.append((0, 9))
    lu = PageOCR(largeur=400, hauteur=100)
    lu.mots.append(MotOCR("Brechbaum", 0, 0, 200, 30, 90, bloc=0, ligne=1))
    lu.texte = "Brechbaum"
    lu.offsets.append((0, 9))
    monkeypatch.setattr(ocr, "ocr_image", lambda *a, **k: lu)
    rendu = Image.new("RGB", (1000, 1000), "white")
    ajoutes = ocr.completer_par_les_images(
        couche, rendu, [(100, 100, 500, 200)])
    assert ajoutes == 1
    assert "Brechbaum" in couche.texte


# --- 6. images lues comme rendues ----------------------------------------- #

def _octets(image, format="PNG", **kw):
    tampon = io.BytesIO()
    image.save(tampon, format, **kw)
    return tampon.getvalue()


def test_image_la_transparente_lue_sur_fond_blanc():
    from caviardoc.documents.images import _ouvrir
    img = Image.new("LA", (40, 20), (0, 0))
    ImageDraw.Draw(img).rectangle((5, 5, 30, 15), fill=(0, 255))
    lu = _ouvrir(_octets(img)).convert("RGB")
    assert lu.getpixel((1, 1)) == (255, 255, 255)
    assert lu.getpixel((10, 10)) == (0, 0, 0)


def test_image_palette_transparente_lue_sur_fond_blanc():
    from caviardoc.documents.images import _ouvrir
    img = Image.new("P", (40, 20), 0)
    img.putpalette([0, 0, 0, 255, 0, 0] + [0] * 762)
    ImageDraw.Draw(img).rectangle((5, 5, 30, 15), fill=1)
    lu = _ouvrir(_octets(img, transparency=0)).convert("RGB")
    assert lu.getpixel((1, 1)) == (255, 255, 255)
    assert lu.getpixel((10, 10)) == (255, 0, 0)


def test_image_orientation_exif_appliquee():
    from caviardoc.documents.images import _ouvrir
    img = Image.new("RGB", (40, 20), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # a tourner de 90 degres pour l'affichage
    lu = _ouvrir(_octets(img, "JPEG", exif=exif.tobytes()))
    assert lu.size == (20, 40)


# --- 7. metadonnees : liste blanche de formats ---------------------------- #

def test_porte_des_metadonnees_refuse_un_format_hors_liste(monkeypatch):
    from caviardoc.documents import metadonnees
    img = Image.new("RGB", (8, 8), "white")
    octets = _octets(img, "PPM")
    appels = []
    reel = Image.open

    def espion(*a, **k):
        appels.append(k.get("formats"))
        return reel(*a, **k)
    monkeypatch.setattr(Image, "open", espion)
    metadonnees.porte_des_metadonnees(octets)
    assert appels and all(f for f in appels)
