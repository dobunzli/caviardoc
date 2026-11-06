"""Logos peints dans l'image d'un scan.

Sur un scan, la page entiere est une seule image : le logo de l'hopital n'y est
qu'une tache de pixels, que `boites_logos` ne voit pas. Releve sur une lettre
d'un hopital, dont le logo restait intact alors que le nom du service, a cote, etait
masque.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

from caviardoc.documents.ocr import MotOCR, PageOCR  # noqa: E402
from caviardoc.documents.zones import (OptionsZones,  # noqa: E402
                                     boites_encre_non_lue,
                                     boites_logos_dessines,
                                     boites_supplementaires,
                                     boites_timbres)

LARGEUR, HAUTEUR = 1000, 1400
BLEU = (20, 90, 160)


def _feuille():
    return Image.new("RGB", (LARGEUR, HAUTEUR), (250, 248, 240))


def _lecture(*boites):
    """Une lecture dont les mots, hauts de 12 px, occupent ces boites."""
    p = PageOCR(largeur=LARGEUR, hauteur=HAUTEUR, dpi=300)
    pos = 0
    for i, (x0, y0, x1, y1) in enumerate(boites):
        p.mots.append(MotOCR(texte="mot", x0=x0, y0=y0, x1=x1, y1=y1,
                             confiance=90, bloc=i, ligne=0))
        p.offsets.append((pos, pos + 3))
        pos += 4
    p.texte = " ".join("mot" for _ in boites)
    return p


# Quelques mots du corps de la lettre, qui fixent la hauteur de mot a 12 px.
CORPS = [(100, 800 + 20 * i, 160, 812 + 20 * i) for i in range(9)]


def _dans(boite, attendue, marge=6):
    return all(abs(a - b) <= marge for a, b in zip(boite, attendue))


class TestLogoColore:
    def test_carre_colore_en_haut_trouve(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 60, 180, 140), fill=BLEU)
        boites = boites_logos_dessines(img, _lecture(*CORPS))
        assert len(boites) == 1
        assert _dans(boites[0], (100, 60, 181, 141))

    def test_cadre_colore_trouve(self):
        """Un label de certification n'est souvent qu'un cadre."""
        img = _feuille()
        ImageDraw.Draw(img).rectangle((600, 60, 760, 140), outline=(200, 60, 40),
                                      width=4)
        assert len(boites_logos_dessines(img, _lecture(*CORPS))) == 1

    def test_logo_en_pied_de_page_trouve(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 1300, 180, 1370), fill=BLEU)
        assert len(boites_logos_dessines(img, _lecture(*CORPS))) == 1

    def test_milieu_de_page_ignore(self):
        """Un schema ou une courbe dans le corps du compte rendu reste visible."""
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 650, 300, 750), fill=BLEU)
        assert boites_logos_dessines(img, _lecture(*CORPS)) == []

    def test_texte_en_couleur_ignore(self):
        """Des mots en couleur sont lus et masques un par un par la detection."""
        img = _feuille()
        ImageDraw.Draw(img).rectangle((400, 300, 600, 340), fill=BLEU)
        lecture = _lecture(*CORPS, (400, 300, 600, 312), (400, 314, 600, 326),
                           (400, 328, 600, 340))
        assert boites_logos_dessines(img, lecture) == []


class TestLogoSombre:
    def test_aplat_gris_trouve(self):
        """Le meme logo, scanne en niveaux de gris."""
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 60, 180, 140), fill=(60, 60, 60))
        assert len(boites_logos_dessines(img, _lecture(*CORPS))) == 1

    def test_ligne_de_texte_noir_ignoree(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 250, 400, 262), fill=(10, 10, 10))
        assert boites_logos_dessines(img, _lecture(*CORPS)) == []

    def test_ombre_du_bord_du_scan_ignoree(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((975, 0, 999, 450), fill=(30, 30, 30))
        assert boites_logos_dessines(img, _lecture(*CORPS)) == []

    def test_cadre_sombre_peu_rempli_ignore(self):
        """Un tableau ou un encadre noir n'est pas un aplat."""
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 60, 400, 300), outline=(0, 0, 0),
                                      width=3)
        assert boites_logos_dessines(img, _lecture(*CORPS)) == []


class _PageScannee:
    """Page PDF dont l'unique image couvre toute la feuille."""

    class rect:
        width, height = LARGEUR * 72 / 300, HAUTEUR * 72 / 300

    def get_images(self, full=True):
        return []


class TestAssemblage:
    def test_logo_du_scan_masque_avec_l_option(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 60, 180, 140), fill=BLEU)
        boites = boites_supplementaires(_PageScannee(), _lecture(*CORPS), [],
                                        OptionsZones(logos=True), 300,
                                        premiere=True, image=img)
        assert len(boites) == 1

    def test_rien_sans_l_option(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 60, 180, 140), fill=BLEU)
        assert boites_supplementaires(_PageScannee(), _lecture(*CORPS), [],
                                      OptionsZones(), 300, premiere=True,
                                      image=img) == []


class TestEncreNonLue:
    """Un logo trame en noir et blanc, ou un nom ecrit a la main en tete de
    formulaire : l'OCR n'en lit rien, la detection n'a rien a masquer."""

    def _gribouillis(self, img, x, y):
        d = ImageDraw.Draw(img)
        for i in range(6):
            d.line((x + 12 * i, y, x + 12 * i + 8, y + 30), fill=(20, 20, 20),
                   width=3)

    def test_ecriture_en_haut_trouvee(self):
        img = _feuille()
        self._gribouillis(img, 600, 60)
        boites = boites_encre_non_lue(img, _lecture(*CORPS))
        assert len(boites) == 1

    def test_logo_trame_trouve(self):
        img = _feuille()
        d = ImageDraw.Draw(img)
        for x in range(100, 180, 4):
            for y in range(60, 140, 4):
                d.point((x, y), fill=(0, 0, 0))
                d.point((x + 1, y), fill=(0, 0, 0))
        assert len(boites_encre_non_lue(img, _lecture(*CORPS))) == 1

    def test_texte_bien_lu_ignore(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 60, 160, 72), fill=(20, 20, 20))
        lecture = _lecture(*CORPS, (100, 60, 160, 72))
        assert boites_encre_non_lue(img, lecture) == []

    def test_coche_au_milieu_de_la_page_ignoree(self):
        """Plus bas que le haut de page, l'encre est un score ou un dessin."""
        img = _feuille()
        self._gribouillis(img, 600, 500)
        assert boites_encre_non_lue(img, _lecture(*CORPS)) == []

    def test_filet_ignore(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((100, 100, 900, 103), fill=(0, 0, 0))
        assert boites_encre_non_lue(img, _lecture(*CORPS)) == []

    def test_cadre_vide_ignore(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((600, 40, 900, 180), outline=(0, 0, 0),
                                      width=2)
        assert boites_encre_non_lue(img, _lecture(*CORPS)) == []


class TestTimbre:
    """Un timbre de medecin pale, pose de travers dans la case « Timbre et
    signature » au milieu d'un formulaire : l'OCR n'en lisait que l'adresse,
    et le nom du medecin restait lisible entre les aplats. Releve sur un
    formulaire de garantie de paiement scanne."""

    # Libelle « Timbre et signature », bien lu, au tiers de la page : hors des
    # bandes haute et basse de `boites_encre_non_lue`.
    LIBELLE = (300, 450, 360, 462)

    def _lecture(self, *mots):
        p = _lecture(*CORPS)
        for texte, (x0, y0, x1, y1), conf in mots:
            debut = len(p.texte) + 1
            p.mots.append(MotOCR(texte=texte, x0=x0, y0=y0, x1=x1, y1=y1,
                                 confiance=conf, bloc=99, ligne=0))
            p.offsets.append((debut, debut + len(texte)))
            p.texte += " " + texte
        return p

    def _ligne_de_timbre(self, img, x, y, gris=110):
        """Une ligne de lettres pales et penchees, que l'OCR ne lit pas."""
        d = ImageDraw.Draw(img)
        for i in range(10):
            d.line((x + 14 * i, y + 4 - i // 3, x + 14 * i + 8, y + 22 - i // 3),
                   fill=(gris, gris, gris + 30), width=3)

    def test_nom_du_timbre_masque(self):
        img = _feuille()
        self._ligne_de_timbre(img, 420, 445)
        lecture = self._lecture(("Timbre", self.LIBELLE, 95))
        assert boites_encre_non_lue(img, lecture) == []      # l'angle mort
        boites = boites_timbres(img, lecture)
        assert len(boites) == 1
        x0, y0, x1, y1 = boites[0]
        assert x0 <= 420 and y0 <= 442 and x1 >= 554 and y1 >= 467

    def test_lignes_du_timbre_masquees_d_un_tenant(self):
        """Entre deux lignes trouvees, la ligne trop pale pour compter comme
        encre est couverte elle aussi."""
        img = _feuille()
        self._ligne_de_timbre(img, 420, 445)
        self._ligne_de_timbre(img, 420, 467, gris=175)   # trop pale
        self._ligne_de_timbre(img, 420, 489)
        lecture = self._lecture(("Signature", self.LIBELLE, 95))
        boites = boites_timbres(img, lecture)
        assert len(boites) == 1
        x0, y0, x1, y1 = boites[0]
        assert y0 <= 442 and y1 >= 511 and x0 <= 420 and x1 >= 554

    def test_sans_libelle_rien(self):
        """Sans libelle, l'encre du milieu de page reste une coche ou un
        dessin, comme pour `boites_encre_non_lue`."""
        img = _feuille()
        self._ligne_de_timbre(img, 420, 445)
        assert boites_timbres(img, _lecture(*CORPS)) == []

    def test_libelle_mal_lu_ignore(self):
        img = _feuille()
        self._ligne_de_timbre(img, 420, 445)
        lecture = self._lecture(("Timbre", self.LIBELLE, 30))
        assert boites_timbres(img, lecture) == []

    def test_encre_loin_sous_la_case_epargnee(self):
        """Au-dela de la case (`HAUTEUR_TIMBRE`), l'encre est celle de la
        rubrique suivante."""
        img = _feuille()
        self._ligne_de_timbre(img, 420, 900)
        lecture = self._lecture(("Timbre", self.LIBELLE, 95))
        assert boites_timbres(img, lecture) == []

    def test_texte_bien_lu_de_la_case_epargne(self):
        img = _feuille()
        ImageDraw.Draw(img).rectangle((420, 480, 520, 492), fill=(20, 20, 20))
        lecture = self._lecture(("Timbre", self.LIBELLE, 95),
                                ("Tel", (420, 480, 520, 492), 92))
        assert boites_timbres(img, lecture) == []

    def test_masque_avec_l_option_logos_seulement(self):
        img = _feuille()
        self._ligne_de_timbre(img, 420, 445)
        lecture = self._lecture(("Timbre", self.LIBELLE, 95))
        avec = boites_supplementaires(_PageScannee(), lecture, [],
                                      OptionsZones(logos=True), 300,
                                      premiere=True, image=img)
        assert any(b[0] <= 420 and b[2] >= 554 and b[1] <= 445 <= b[3]
                   for b in avec)
        assert boites_supplementaires(_PageScannee(), lecture, [],
                                      OptionsZones(), 300, premiere=True,
                                      image=img) == []


class TestMotSymbole:
    """Un mot-symbole d'assureur en couleur, suivi de son symbole rond, en
    haut a droite d'un courrier scanne. L'OCR lisait le mot (92 %) et
    devinait le symbole (« (ii) », 25 %) : le symbole passait pour du texte,
    n'etait pas un logo, et le mot pose contre lui restait lisible."""

    MOT = (600, 60, 760, 100)
    SYMBOLE = (780, 50, 840, 110)

    def _page(self):
        img = _feuille()
        d = ImageDraw.Draw(img)
        d.rectangle(self.MOT, fill=BLEU)
        d.ellipse(self.SYMBOLE, outline=BLEU, width=8)
        d.rectangle((800, 65, 820, 95), fill=BLEU)
        return img

    def _lecture(self, confiance_symbole):
        p = _lecture(*CORPS)
        for texte, boite, conf in (("Acme", self.MOT, 92),
                                   ("(ii)", self.SYMBOLE, confiance_symbole)):
            debut = len(p.texte) + 1
            p.mots.append(MotOCR(texte, *boite, confiance=conf, bloc=500,
                                 ligne=0))
            p.offsets.append((debut, debut + len(texte)))
            p.texte += " " + texte
        return p

    def test_symbole_mal_lu_reste_un_logo(self):
        boites = boites_logos_dessines(self._page(), self._lecture(25))
        assert any(_dans(b, (780, 50, 841, 111)) for b in boites)

    def test_symbole_bien_lu_reste_du_texte(self):
        """Le seuil est celui de `boites_encre_non_lue` : un mot lu avec
        assurance explique l'encre qu'il couvre."""
        boites = boites_logos_dessines(self._page(), self._lecture(92))
        assert not any(b[0] >= 770 for b in boites)

    def test_mot_symbole_masque_avec_son_symbole(self):
        boites = boites_supplementaires(_PageScannee(), self._lecture(25), [],
                                        OptionsZones(logos=True, entetes=True),
                                        300, premiere=True, image=self._page())
        assert any(b[0] <= 600 and b[1] <= 60 and b[2] >= 760 and b[3] >= 100
                   for b in boites)
