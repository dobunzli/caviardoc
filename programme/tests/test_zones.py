"""Zones optionnelles : logos et blocs d'en-tete.

Ces traitements retirent des elements qui ne sont pas des donnees personnelles
au sens strict. Ils doivent donc rester inactifs par defaut, et selectifs quand
ils sont demandes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.documents.ocr import MotOCR, PageOCR  # noqa: E402
from caviardoc.documents.zones import (OptionsZones, boite_bande,  # noqa: E402
                                     boites_entetes, spans_des_zones)
from caviardoc.engine import Origine, Span  # noqa: E402
from caviardoc.pipeline import _merge  # noqa: E402


def _page(mots):
    """Construit une PageOCR minimale a partir de (texte, bloc, x, y).

    Les mots poses a la meme hauteur forment une ligne."""
    p = PageOCR(largeur=1000, hauteur=1000)
    pos = 0
    for texte, bloc, x, y in mots:
        p.mots.append(MotOCR(texte=texte, x0=x, y0=y, x1=x + 50, y1=y + 12,
                             confiance=100, bloc=bloc, ligne=y))
        p.offsets.append((pos, pos + len(texte)))
        pos += len(texte) + 1
    p.texte = " ".join(m[0] for m in mots)
    return p


class TestOptions:
    def test_inactives_par_defaut(self):
        assert not OptionsZones().actif

    def test_actives_des_qu_une_option_est_posee(self):
        assert OptionsZones(logos=True).actif
        assert OptionsZones(entetes=True).actif
        assert OptionsZones(bande_haute=0.2).actif


class TestBandeHaute:
    def test_fraction_nulle_ne_masque_rien(self):
        assert boite_bande(1000, 1000, 0.0) == []

    def test_fraction_donne_un_bandeau(self):
        (x0, y0, x1, y1), = boite_bande(1000, 800, 0.25)
        assert (x0, y0, x1, y1) == (0.0, 0.0, 1000.0, 200.0)

    def test_fraction_bornee_a_la_page(self):
        (_, _, _, y1), = boite_bande(100, 500, 3.0)
        assert y1 == 500


class TestEntetes:
    def test_bloc_haut_avec_marqueur_retenu(self):
        page = _page([("Case", 1, 50, 20), ("postale", 1, 100, 20)])
        assert len(boites_entetes(page, [], 1000)) == 1

    def test_bloc_bas_de_page_ignore(self):
        """Un marqueur d'adresse en pied de page n'est pas un en-tete."""
        page = _page([("Case", 1, 50, 900), ("postale", 1, 100, 900)])
        assert boites_entetes(page, [], 1000) == []

    def test_bloc_haut_sans_marqueur_ni_entite_ignore(self):
        page = _page([("Conclusion", 1, 50, 20), ("normale", 1, 150, 20)])
        assert boites_entetes(page, [], 1000) == []

    def test_bloc_touche_par_une_entite_retenu(self):
        """Meme sans marqueur, un bloc portant une entite detectee est un en-tete."""
        page = _page([("Fontannaz", 1, 50, 20), ("Yannick", 1, 150, 20)])
        spans = [Span(0, 9, "personne", 0.9, "Fontannaz")]
        assert len(boites_entetes(page, spans, 1000)) == 1

    def test_ligne_datee_d_un_tableau_ignoree(self):
        """Une liste de diagnostics met diagnostic et date dans le meme bloc.
        La date seule n'en fait pas un en-tete : le diagnostic reste lisible."""
        page = _page([("Balanite", 1, 50, 20), ("du", 1, 150, 20),
                      ("gland", 1, 200, 20), ("16.7.19", 1, 700, 20)])
        spans = [Span(18, 25, "date", 1.0, "16.7.19")]
        assert boites_entetes(page, spans, 1000) == []

    def test_service_regional_en_tete_de_bloc_retenu(self):
        page = _page([("Service", 1, 50, 20), ("régional", 1, 120, 20),
                      ("de", 1, 190, 20), ("transfusion", 1, 240, 20)])
        assert len(boites_entetes(page, [], 1000)) == 1

    def test_marqueur_devine_par_l_ocr_ignore(self):
        """L'ecriture manuscrite lue « Tel », avec une confiance de 24 %, ne
        fait pas d'un titre de formulaire un en-tete."""
        page = _page([("FERMEZ", 1, 50, 20), ("LES", 1, 150, 20),
                      ("YEUX", 1, 250, 20), ("Tel", 1, 400, 20)])
        page.mots[3] = MotOCR("Tel", 400, 20, 450, 32, 24, bloc=1, ligne=20)
        assert boites_entetes(page, [], 1000) == []

    def test_entite_devinee_par_l_ocr_ignoree(self):
        page = _page([("FERMEZ", 1, 50, 20), ("LES", 1, 150, 20),
                      ("YEUX", 1, 250, 20), ("Tel", 1, 400, 20),
                      ("1", 1, 460, 20)])
        page.mots[3] = MotOCR("Tel", 400, 20, 450, 32, 24, bloc=1, ligne=20)
        spans = [Span(16, 21, "numero de telephone", 0.33, "Tel 1")]
        assert boites_entetes(page, spans, 1000) == []

    def test_le_bloc_entier_est_couvert(self):
        """On masque tout le bloc, pas seulement les mots reconnus."""
        page = _page([("Dr", 1, 50, 20), ("inconnu", 1, 150, 20),
                      ("specialiste", 1, 250, 20)])
        (x0, y0, x1, y1), = boites_entetes(page, [], 1000)
        assert x0 == 50 and x1 == 300

    def test_page_sans_texte(self):
        assert boites_entetes(PageOCR(largeur=100, hauteur=100), [], 100) == []

    def test_nom_de_service_en_tete_de_bloc_retenu(self):
        """« Departement de medecine » n'a ni adresse ni entite : c'est son
        debut qui le designe comme nom d'etablissement."""
        page = _page([("Département", 1, 50, 20), ("de", 1, 120, 20),
                      ("médecine", 1, 170, 20)])
        assert len(boites_entetes(page, [], 1000)) == 1

    def test_service_au_milieu_d_une_phrase_ignore(self):
        page = _page([("Le", 1, 50, 20), ("service", 1, 120, 20),
                      ("de", 1, 170, 20), ("garde", 1, 220, 20)])
        assert boites_entetes(page, [], 1000) == []

    def test_rubrique_clinique_ignoree(self):
        """« Renseignements cliniques » n'est pas une Clinique : la rubrique et
        sa premiere ligne disparaissaient du texte exporte."""
        for texte in ("Renseignements cliniques", "Examen clinique",
                      "EXAMEN CLINIQUE", "Bilan de laboratoire",
                      "Vu au cabinet", "Un tel tableau", "Séjour à l'hôtel"):
            mots = [(m, 1, 50 + 60 * i, 20)
                    for i, m in enumerate(texte.split())]
            assert boites_entetes(_page(mots), [], 1000) == [], texte

    def test_nom_d_etablissement_retenu(self):
        for texte in ("Clinique du Verger", "CLINIQUE DES GLYCINES",
                      "Cabinet d'imagerie du Rivage", "Tél. 032 555 11 00",
                      "Tel 032 555 11 00", "Hôpital des Cèdres"):
            mots = [(m, 1, 50 + 60 * i, 20)
                    for i, m in enumerate(texte.split())]
            assert len(boites_entetes(_page(mots), [], 1000)) == 1, texte

    def test_la_ligne_d_examen_reste_lisible(self):
        """« Examen du … » suit souvent le nom du patient dans le meme bloc :
        le nom est masque, la ligne de l'examen ne l'est pas."""
        mots = [("Monsieur", 1, 50, 20), ("Yannick", 1, 120, 20)]
        mots += [(m, 1, 50 + 60 * i, 34)
                 for i, m in enumerate("Examen du 5 avril 2014".split())]
        page = _page(mots)
        (x0, y0, x1, y1), = boites_entetes(page, [], 1000)
        assert y1 < 34

    def test_ligne_d_examen_au_milieu_du_bloc(self):
        """Le bloc se coupe autour de la ligne gardee, sans la recouvrir."""
        mots = [("Monsieur", 1, 50, 20), ("Date", 1, 50, 34),
                ("d'examen", 1, 120, 34), ("Rue", 1, 50, 48)]
        page = _page(mots)
        rects = boites_entetes(page, [], 1000)
        assert len(rects) == 2
        assert all(r[3] <= 34 or r[1] >= 46 for r in rects)

    def test_ligne_d_examen_seule_ignoree(self):
        page = _page([("Examen", 1, 50, 20), ("du", 1, 120, 20)])
        spans = [Span(0, 6, "date", 0.9, "Examen")]
        assert boites_entetes(page, spans, 1000) == []

    def test_un_bloc_qui_porte_aussi_le_corps_ne_noircit_pas_le_corps(self):
        """Un PDF de traitement de texte range parfois la date, la reference et
        tout le corps de la lettre dans un seul bloc. Ce bloc commence dans le
        haut de la page, mais son rectangle descend jusqu'en bas du texte : le
        noircir effacait la lettre entiere, sortie texte comprise."""
        mots = [("Case", 1, 50, 300), ("postale", 1, 150, 300),
                ("(4034270/5)", 1, 50, 320)]
        # Le corps : deux paragraphes, bien en dessous, dans le meme bloc.
        mots += [("Monsieur", 1, 50, 420), ("et", 1, 150, 420),
                 ("Je", 1, 50, 600), ("vous", 1, 150, 600),
                 ("remercie", 1, 250, 600)]
        page = _page(mots)
        rects = boites_entetes(page, [], 1000)
        assert rects, "la date et la reference sont dans le haut de la page"
        assert max(r[3] for r in rects) < 400

    def test_deux_paragraphes_du_haut_restent_deux_zones(self):
        """Une adresse et la ligne de date, separees par une ligne blanche,
        ne forment pas une seule zone."""
        page = _page([("Case", 1, 50, 20), ("postale", 1, 120, 20),
                      ("Échallens,", 1, 50, 80), ("Tél.", 1, 120, 80)])
        assert len(boites_entetes(page, [], 1000)) == 2


def _ligne(texte: str, bloc: int, y: int):
    return [(mot, bloc, 50 + 60 * i, y) for i, mot in enumerate(texte.split())]


class TestCorpsDeLettre:
    """Sur une lettre courte, le corps commence dans le haut de la page."""

    CORPS = ("J'ai vu en consultation votre patient, Monsieur Jean Dupont, pour",
             "un contrôle de son diabète. Le traitement par metformine est",
             "poursuivi sans changement. Sa fille l'accompagnait aujourd'hui.")

    def test_un_paragraphe_redige_n_est_pas_un_en_tete(self):
        """Il porte « Monsieur » et le nom du patient, et disparaissait en
        entier : « [PERSONNE_1] » a la place de toute la consultation."""
        mots = [m for i, l in enumerate(self.CORPS) for m in _ligne(l, 1, 200 + 14 * i)]
        page = _page(mots)
        nom = page.texte.index("Jean")
        spans = [Span(nom, nom + 11, "personne", 0.9, "Jean Dupont")]
        assert boites_entetes(page, spans, 1000) == []

    def test_le_bloc_destinataire_reste_masque(self):
        bloc = ("Monsieur Jean Dupont", "Rue des Lilas 4", "1040 Échallens")
        mots = [m for i, l in enumerate(bloc) for m in _ligne(l, 1, 100 + 14 * i)]
        assert boites_entetes(_page(mots), [], 1000)

    def test_une_ligne_concerne_reste_masquee(self):
        """Une seule ligne longue, mais faite de noms propres et de chiffres."""
        ligne = ("Concerne : M. Jean Dupont, né le 12.03.1961, rue des Lilas 4, "
                 "1040 Échallens")
        assert boites_entetes(_page(_ligne(ligne, 1, 100)), [], 1000)


class TestVoisinDeLogo:
    """Le nom du service se compose a cote du logo de l'hopital."""

    #: Logo de 70 x 100, en pixels a 300 dpi : l'ecart tolere est de 166 px.
    LOGO = (40.0, 30.0, 110.0, 130.0)

    def _bloc(self, x, y):
        return _page([("Divers", 1, x, y), ("mots", 1, x + 60, y)])

    def test_bloc_contre_le_logo_retenu(self):
        page = self._bloc(115, 70)
        assert len(boites_entetes(page, [], 1000, logos=[self.LOGO])) == 1

    def test_sans_logo_le_meme_bloc_est_ignore(self):
        assert boites_entetes(self._bloc(115, 70), [], 1000) == []

    def test_bloc_trop_loin_du_logo_ignore(self):
        page = self._bloc(400, 70)
        assert boites_entetes(page, [], 1000, logos=[self.LOGO]) == []

    def test_titre_sous_le_logo_ignore(self):
        """Le milieu du bloc compte : un titre place sous le logo n'en fait pas
        partie, meme s'il s'etend jusqu'a lui."""
        page = self._bloc(60, 140)
        assert boites_entetes(page, [], 1000, logos=[self.LOGO]) == []


class TestTexteDesZones:
    """Ce que le PDF noircit, le texte ne doit pas le garder."""

    def test_un_logo_donne_un_seul_span(self):
        page = _page([("Réseau", 1, 10, 10), ("Hospitalier", 1, 10, 30),
                      ("Lacustre", 1, 10, 50), ("Corps", 2, 300, 300)])
        spans = spans_des_zones(page, [(0, 0, 100, 100)])
        assert [s.text for s in spans] == ["Réseau Hospitalier Lacustre"]
        assert spans[0].label == "employeur"
        assert spans[0].origine == Origine.ZONE.value

    def test_un_mot_a_peine_effleure_reste_lisible(self):
        """Une zone qui ne couvre qu'un bord du mot ne le masque pas."""
        page = _page([("Dedans", 1, 10, 10), ("Bord", 1, 90, 10)])
        spans = spans_des_zones(page, [(0, 0, 100, 100)])
        assert [s.text for s in spans] == ["Dedans"]

    def test_deux_zones_donnent_deux_spans(self):
        page = _page([("Haut", 1, 10, 10), ("Milieu", 2, 10, 500),
                      ("Bas", 3, 10, 900)])
        spans = spans_des_zones(page, [(0, 0, 100, 50), (0, 880, 100, 950)])
        assert [s.text for s in spans] == ["Haut", "Bas"]

    def test_une_detection_garde_son_etiquette(self):
        """La zone n'est qu'un defaut : une personne reconnue reste une personne."""
        page = _page([("Dr", 1, 10, 10), ("Huber", 1, 70, 10),
                      ("Cardiologie", 1, 130, 10)])
        reconnu = Span(0, 8, "personne", 0.3, "Dr Huber")
        zone = spans_des_zones(page, [(0, 0, 200, 50)])
        fusion = _merge([reconnu] + zone, page.texte)
        assert len(fusion) == 1
        assert fusion[0].label == "personne"
        assert fusion[0].text == page.texte

    def test_aucune_zone_aucun_span(self):
        assert spans_des_zones(_page([("Mot", 1, 10, 10)]), []) == []
