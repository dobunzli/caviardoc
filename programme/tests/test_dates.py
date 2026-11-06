"""Politique de dates : une date de naissance n'est pas une date comme les autres.

Une date de prelevement ou d'edition ne dit presque rien d'une personne. Une
date de naissance, combinee au code postal et au sexe, l'identifie dans la
grande majorite des cas. Les deux ne peuvent donc pas suivre la meme regle.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.engine import Span  # noqa: E402
from caviardoc.pipeline import (PolitiqueDates, est_date_de_naissance,  # noqa: E402
                              filtrer_dates)


def _span(texte, aiguille, label="date"):
    d = texte.index(aiguille)
    return Span(d, d + len(aiguille), label, 1.0, aiguille)


class TestReconnaissanceNaissance:
    def test_marqueur_ne_le(self):
        t = "Monsieur Fontannaz, ne le 23.04.1931, Grandvaux"
        assert est_date_de_naissance(t, _span(t, "23.04.1931"))

    def test_marqueur_accentue(self):
        t = "né(e) le 23.04.1931"
        assert est_date_de_naissance(t, _span(t, "23.04.1931"))

    def test_libelle_explicite(self):
        t = "Date de naissance : 23.04.1931"
        assert est_date_de_naissance(t, _span(t, "23.04.1931"))

    def test_etiquette_du_modele_suffit(self):
        t = "23.04.1931 sans contexte"
        assert est_date_de_naissance(
            t, _span(t, "23.04.1931", "date de naissance"))

    def test_date_ordinaire_non_reconnue(self):
        t = "Prelevement du 21.10.2019 a 08:45"
        assert not est_date_de_naissance(t, _span(t, "21.10.2019"))

    def test_marqueur_trop_loin_ignore(self):
        t = "ne le" + " " * 60 + "21.10.2019"
        assert not est_date_de_naissance(t, _span(t, "21.10.2019"))


class TestFiltrage:
    TEXTE = "Fontannaz ne le 23.04.1931, prelevement du 21.10.2019"

    def _spans(self):
        return [_span(self.TEXTE, "Fontannaz", "personne"),
                _span(self.TEXTE, "23.04.1931"),
                _span(self.TEXTE, "21.10.2019")]

    def test_masquer_conserve_tout(self):
        assert len(filtrer_dates(self.TEXTE, self._spans(),
                                 PolitiqueDates.MASQUER)) == 3

    def test_garder_retient_la_naissance(self):
        gardes = filtrer_dates(self.TEXTE, self._spans(), PolitiqueDates.GARDER)
        textes = [g.text for g in gardes]
        assert "23.04.1931" in textes, "la date de naissance doit rester masquee"
        assert "21.10.2019" not in textes
        assert "Fontannaz" in textes

    def test_garder_tout_libere_les_dates(self):
        gardes = filtrer_dates(self.TEXTE, self._spans(),
                               PolitiqueDates.GARDER_TOUT)
        assert [g.text for g in gardes] == ["Fontannaz"]

    def test_les_autres_entites_ne_sont_jamais_touchees(self):
        for pol in PolitiqueDates:
            gardes = filtrer_dates(self.TEXTE, self._spans(), pol)
            assert any(g.label == "personne" for g in gardes)


class TestFormeDeDate:
    """Cas types d'un tableau d'antecedents (donnees inventees) : le reglage des dates
    ne doit pas dependre de l'etiquette que le modele a choisie."""

    TABLEAU = ("Densitometrie osseuse: osteoporose lombaire.\n11.2.16\n"
               "Rx genou et US: bursite de la patte d'oie\n28.12.16\n"
               "Avis ORL: examen normal\n15.9.11\n")

    def test_regle_annee_a_deux_chiffres(self):
        from caviardoc.detect import rules
        dates = [m.text for m in rules.find_all(self.TABLEAU)
                 if m.label == "date"]
        assert dates == ["11.2.16", "28.12.16", "15.9.11"]

    def test_pas_de_date_dans_un_numero_plus_long(self):
        from caviardoc.detect import rules
        for t in ("192.168.1.1", "1.2.16.3", "31.2.20"):
            assert not [m for m in rules.find_all(t) if m.label == "date"], t

    def test_date_etiquetee_avs_liberee(self):
        sp = _span(self.TABLEAU, "11.2.16", "numero AVS")
        assert filtrer_dates(self.TABLEAU, [sp],
                             PolitiqueDates.GARDER_TOUT) == []

    def test_fragment_de_date_libere(self):
        d = self.TABLEAU.index("28.12.16") + 6
        sp = Span(d, d + 2, "numero AVS", 0.5, "16")
        assert filtrer_dates(self.TABLEAU, [sp],
                             PolitiqueDates.GARDER_TOUT) == []

    def test_nom_colle_a_une_date_garde_le_nom(self):
        t = "Vu Dupont 12.3.55 ce jour"
        sp = _span(t, "Dupont 12.3.55", "personne")
        gardes = filtrer_dates(t, [sp], PolitiqueDates.GARDER_TOUT)
        assert [g.text for g in gardes] == ["Dupont"]

    def test_date_au_milieu_reste_masquee(self):
        t = "Dupont 12.3.55 Jean"
        sp = _span(t, t, "personne")
        assert filtrer_dates(t, [sp], PolitiqueDates.GARDER_TOUT) == [sp]

    def test_masquer_ne_touche_a_rien(self):
        sp = _span(self.TABLEAU, "11.2.16", "numero AVS")
        assert filtrer_dates(self.TABLEAU, [sp],
                             PolitiqueDates.MASQUER) == [sp]


class TestNaissanceSeulement:
    """« La date de naissance seulement » : le modele pose « date de
    naissance » sur des dates ordinaires, il ne doit plus suffire seul."""

    def test_antecedent_etiquete_naissance_libere(self):
        t = "Avis ORL: examen normal\n15.9.11\n"
        sp = _span(t, "15.9.11", "date de naissance")
        assert filtrer_dates(t, [sp], PolitiqueDates.GARDER) == []

    def test_date_apres_le_nom_du_patient_reste_masquee(self):
        t = "Dupont Jean 26.05.1955\nTel. :"
        spans = [_span(t, "Dupont Jean", "personne"),
                 _span(t, "26.05.1955", "date")]
        gardes = filtrer_dates(t, spans, PolitiqueDates.GARDER)
        assert "26.05.1955" in [g.text for g in gardes]

    def test_etiquette_naissance_annee_complete_reste_masquee(self):
        t = "Anamnese\n26.05.1955\n"
        sp = _span(t, "26.05.1955", "date de naissance")
        assert filtrer_dates(t, [sp], PolitiqueDates.GARDER) == [sp]

    def test_horodatage_libere(self):
        t = "Powered by axenita 30.09.2026, 10:12"
        sp = _span(t, "30.09.2026", "date de naissance")
        assert filtrer_dates(t, [sp], PolitiqueDates.GARDER) == []

    def test_marqueur_toujours_decisif(self):
        t = "nee le 15.9.11"
        sp = _span(t, "15.9.11", "numero AVS")
        gardes = filtrer_dates(t, [sp], PolitiqueDates.GARDER)
        assert [g.text for g in gardes] == ["15.9.11"]

    def test_date_apres_un_etablissement_liberee(self):
        t = "Cabinet Medical Dupont Martin,\n30.09.2026, DB"
        spans = [_span(t, "Cabinet Medical Dupont Martin", "personne"),
                 _span(t, "30.09.2026", "date de naissance")]
        gardes = filtrer_dates(t, spans, PolitiqueDates.GARDER)
        assert "30.09.2026" not in [g.text for g in gardes]


class TestDateEcriteDeNaissance:
    def test_la_naissance_en_lettres_reste_masquee(self):
        t = "Paul Martin, né le 23 avril 1931. Vu le 5 avril 2014."
        spans = [_span(t, "23 avril 1931"), _span(t, "5 avril 2014")]
        gardes = filtrer_dates(t, spans, PolitiqueDates.GARDER)
        assert [s.text for s in gardes] == ["23 avril 1931"]
