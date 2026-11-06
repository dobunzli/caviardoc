"""Audit de securite, voie A : ce que la chaine PDF peut laisser lisible.

Un outil de confidentialite ne doit jamais annoncer « verifie » quand un
identifiant peut subsister. Chaque test reprend le plan de validation d'une
piste de l'audit, avec des donnees inventees.
"""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

pymupdf = pytest.importorskip("pymupdf")
pytest.importorskip("pytesseract")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from caviardoc import tesseract  # noqa: E402
from caviardoc.documents import caviardage, ocr  # noqa: E402
from caviardoc.documents.caviardage import (RapportPage,  # noqa: E402
                                          caviarder_pdf,
                                          etiquettes_a_masquer,
                                          verifier_octets)
from caviardoc.documents.commun import (propageable,  # noqa: E402
                                        propager_chaines)
from caviardoc.engine import Span  # noqa: E402

besoin_ocr = pytest.mark.skipif(
    tesseract.binaire() is None or "fra" not in (tesseract.langues() or []),
    reason="lire une image demande Tesseract et la langue francaise")

NOM = "Zorglub"
REMPLISSAGE = ("Compte rendu de consultation du service de medecine interne, "
               "tension stable, bilan sanguin sans particularite, poursuite "
               "du traitement habituel et controle dans trois mois.")


def _corps(page, y):
    """Assez de lignes courtes pour que la couche texte soit jugee saine."""
    page.insert_textbox(pymupdf.Rect(72, y, 520, y + 80), REMPLISSAGE,
                        fontsize=9)


def _detecte(mot, seulement_page=None):
    """Detecteur factice : une personne sur ``mot``, a la premiere page lue."""
    appels = {"n": 0}

    def _detection(model, texte, **kw):
        appels["n"] += 1
        i = texte.find(mot)
        if i == -1 or (seulement_page and appels["n"] != seulement_page):
            return [], []
        sp = Span(i, i + len(mot), "personne", 0.9, mot)
        return [sp], [sp]
    return _detection


# --- piste 1 : un nom d'un seul mot n'est ni propage ni verifie -------------

def test_propageable_mot_capitalise_de_personne():
    assert not propageable(NOM)                 # vocabulaire : inchange
    assert propageable(NOM, "personne")
    assert not propageable("autres", "personne")  # minuscules : reste du vocabulaire


def test_verification_cherche_le_nom_d_un_mot():
    page = RapportPage(numero=1, entites=[Span(0, 7, "personne", 0.9, NOM)])
    assert NOM in etiquettes_a_masquer([page])


@besoin_ocr
def test_nom_d_un_mot_propage_a_la_page_suivante(tmp_path, monkeypatch):
    d = pymupdf.open()
    for _ in range(2):
        p = d.new_page()
        p.insert_text((72, 80), f"Patient {NOM} presente ce jour.", fontsize=11)
        _corps(p, 120)
    d.save(tmp_path / "a.pdf")
    monkeypatch.setattr(caviardage, "analyse_detaillee",
                        _detecte(NOM, seulement_page=1))
    r = caviarder_pdf(None, tmp_path / "a.pdf", tmp_path / "o.pdf")
    masque = bool(r.pages[1].entites)
    assert masque or r.fuites, "page 2 en clair, annoncee verifiee"


# --- piste 6 : le controle du texte seul vaut celui du PDF ------------------

@besoin_ocr
def test_texte_seul_relit_une_occurrence_collee_et_en_capitales(tmp_path,
                                                                monkeypatch):
    nom = "Zorglub Quidam"
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 80), f"Patient {nom} presente ce jour.", fontsize=11)
    p.insert_text((72, 100), "ZORGLUBQUIDAMEST suivi au cabinet.", fontsize=11)
    _corps(p, 120)
    d.save(tmp_path / "t.pdf")
    monkeypatch.setattr(caviardage, "analyse_detaillee", _detecte(nom))
    r = caviarder_pdf(None, tmp_path / "t.pdf", tmp_path / "o.pdf",
                      formats=frozenset({"texte"}))
    propre = "quidam" not in "".join(r.texte.lower().split())
    assert propre or r.fuites_texte or not r.verifie


# --- piste 3 : une page illisible n'est pas une page blanche ----------------

def _gribouillis(taille=(1200, 1600), graine=7):
    """Traits de stylo denses, sans une lettre lisible."""
    hasard = random.Random(graine)
    image = Image.new("RGB", taille, "white")
    dessin = ImageDraw.Draw(image)
    for _ in range(260):
        x, y = hasard.randint(50, taille[0] - 50), hasard.randint(50, taille[1] - 50)
        dessin.line([x, y, x + hasard.randint(-90, 90), y + hasard.randint(-90, 90)],
                    fill="black", width=5)
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    return tampon.getvalue()


@besoin_ocr
def test_scan_illisible_n_est_pas_annonce_sans_reserve(tmp_path):
    d = pymupdf.open()
    p = d.new_page()
    p.insert_image(p.rect, stream=_gribouillis())
    d.save(tmp_path / "s.pdf")
    r = caviarder_pdf(None, tmp_path / "s.pdf", tmp_path / "o.pdf")
    assert r.pages[0].reserve or not r.verifie


@besoin_ocr
def test_page_illisible_a_cote_d_une_page_detectee(tmp_path, monkeypatch):
    d = pymupdf.open()
    p1 = d.new_page()
    p1.insert_text((72, 80), f"Patient {NOM} Quidam presente ce jour.", fontsize=11)
    _corps(p1, 120)
    p2 = d.new_page()
    p2.insert_image(p2.rect, stream=_gribouillis())
    d.save(tmp_path / "s.pdf")
    monkeypatch.setattr(caviardage, "analyse_detaillee",
                        _detecte(f"{NOM} Quidam"))
    r = caviarder_pdf(None, tmp_path / "s.pdf", tmp_path / "o.pdf")
    assert r.pages[1].reserve


def test_page_blanche_reste_sans_reserve(tmp_path):
    d = pymupdf.open()
    d.new_page()
    d.save(tmp_path / "b.pdf")
    if tesseract.binaire() is None:
        pytest.skip("Tesseract absent")
    r = caviarder_pdf(None, tmp_path / "b.pdf", tmp_path / "o.pdf")
    assert not r.pages[0].reserve


# --- piste 4 : la couche texte n'est pas toute la page ----------------------

def test_couche_aux_caracteres_de_remplacement_est_relue_par_ocr(monkeypatch):
    """Une police sans table Unicode donne des jetons de longueur normale."""
    illisible = " ".join(["�����"] * 30)
    lu = ocr.PageOCR(texte=illisible)
    appels = []
    monkeypatch.setattr(caviardage, "mots_depuis_couche_texte",
                        lambda page, dpi: lu)
    monkeypatch.setattr(caviardage, "_rendu", lambda page, dpi: object())
    monkeypatch.setattr(caviardage, "ocr_page",
                        lambda *a, **k: appels.append(1) or ocr.PageOCR())
    _, source = caviardage._lire_page(object(), 300, "fra")
    assert appels and source.startswith("OCR")


@besoin_ocr
def test_petite_image_sur_page_a_couche_texte(tmp_path, monkeypatch):
    image = Image.new("RGB", (900, 60), "white")
    ImageDraw.Draw(image).text((5, 5), f"Assure {NOM} Quidam",
                               fill="black", font=ImageFont.load_default(size=44))
    tampon = io.BytesIO()
    image.save(tampon, format="PNG")
    d = pymupdf.open()
    p = d.new_page()
    _corps(p, 80)
    p.insert_image(pymupdf.Rect(72, 300, 222, 310), stream=tampon.getvalue())
    d.save(tmp_path / "v.pdf")
    monkeypatch.setattr(caviardage, "analyse_detaillee", _detecte(NOM))
    r = caviarder_pdf(None, tmp_path / "v.pdf", tmp_path / "o.pdf")
    sortie = (tmp_path / "o.pdf").read_bytes()
    lu = verifier_octets(sortie, {NOM})
    assert r.pages[0].entites and NOM in r.pages[0].texte_lu
    assert not lu or r.pages[0].reserve or not r.verifie


@besoin_ocr
def test_texte_vectoriel_est_lu_masque_et_verifie(tmp_path, monkeypatch):
    """Glyphes dessines en rectangles : aucune couche texte, seulement des traces."""
    masque = ImageFont.load_default(size=40).getmask(NOM)
    largeur, hauteur = masque.size
    d = pymupdf.open()
    p = d.new_page()
    _corps(p, 80)
    pas = 1.0
    for y in range(hauteur):
        for x in range(largeur):
            if masque.getpixel((x, y)) > 128:
                p.draw_rect(pymupdf.Rect(72 + x * pas, 300 + y * pas,
                                         72 + (x + 1) * pas, 300 + (y + 1) * pas),
                            fill=(0, 0, 0), color=None)
    d.save(tmp_path / "v.pdf")
    assert NOM not in d[0].get_text()
    monkeypatch.setattr(caviardage, "analyse_detaillee", _detecte(NOM))
    r = caviarder_pdf(None, tmp_path / "v.pdf", tmp_path / "o.pdf")
    assert NOM in r.pages[0].texte_lu
    assert r.pages[0].entites
    assert not verifier_octets((tmp_path / "o.pdf").read_bytes(), {NOM})


def test_sans_tesseract_le_pdf_est_refuse(tmp_path, monkeypatch):
    d = pymupdf.open()
    _corps(d.new_page(), 80)
    d.save(tmp_path / "s.pdf")
    monkeypatch.setattr(caviardage.tesseract, "binaire", lambda: None)
    with pytest.raises(tesseract.TesseractIndisponible) as exc:
        caviarder_pdf(None, tmp_path / "s.pdf", tmp_path / "o.pdf")
    assert "caviardoc setup" in str(exc.value)
    assert not (tmp_path / "o.pdf").exists()
    with pytest.raises(tesseract.TesseractIndisponible):
        caviardage.lire_pdf(tmp_path / "s.pdf")


# --- piste 5 : codes et dessins vectoriels ----------------------------------

@besoin_ocr
def test_code_vectoriel_dans_le_corps(tmp_path):
    """Un QR dessine en rectangles vectoriels est masque, et verifie masque."""
    zxingcpp = pytest.importorskip("zxingcpp")
    d = pymupdf.open()
    p = d.new_page()
    _corps(p, 80)
    modules = Image.fromarray(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode("DOSSIER-0012345", zxingcpp.QRCode),
        scale=1, add_quiet_zones=True)).convert("L")
    for j in range(modules.height):
        for i in range(modules.width):
            if modules.getpixel((i, j)) < 128:
                p.draw_rect(pymupdf.Rect(200 + i * 4, 400 + j * 4,
                                         204 + i * 4, 404 + j * 4),
                            fill=(0, 0, 0), color=None)
    d.save(tmp_path / "q.pdf")

    def decodables(chemin):
        with pymupdf.open(chemin) as doc:
            img = Image.open(io.BytesIO(
                doc[0].get_pixmap(dpi=300).tobytes("png"))).convert("RGB")
        return zxingcpp.read_barcodes(img, return_errors=True)

    assert decodables(tmp_path / "q.pdf")
    r = caviarder_pdf(None, tmp_path / "q.pdf", tmp_path / "o.pdf")
    assert r.pages[0].codes_barres >= 1
    assert r.verifie and not r.fuites
    assert not decodables(tmp_path / "o.pdf")


def test_ocr_en_echec_fait_echouer_le_document(tmp_path, monkeypatch):
    d = pymupdf.open()
    p = d.new_page()
    _corps(p, 80)
    p.insert_text((72, 300), f"Assure {NOM} Quidam", fontsize=11)
    d.save(tmp_path / "s.pdf")
    monkeypatch.setattr(caviardage.tesseract, "binaire", lambda: "/x")
    monkeypatch.setattr(caviardage.tesseract, "langues", lambda: ["fra"])
    monkeypatch.setattr(caviardage, "analyse_detaillee", _detecte(NOM))

    def _echec(*a, **k):
        raise RuntimeError("tesseract plante")
    monkeypatch.setattr(ocr, "ocr_page", _echec)
    monkeypatch.setattr(ocr, "ocr_image", _echec)
    monkeypatch.setattr(caviardage, "completer_par_la_page", _echec)
    with pytest.raises(tesseract.TesseractIndisponible):
        caviarder_pdf(None, tmp_path / "s.pdf", tmp_path / "o.pdf")
    assert not (tmp_path / "o.pdf").exists()
