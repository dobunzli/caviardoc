"""Vocabulaire neutre : ne masquer que ce qui identifie.

Mesure sur corpus medical reel : a politique alignee sur le caviardage humain
de reference, le filtre fait passer la precision de 31 % a 35 % en retirant
19 % des faux positifs isoles, pour un point de rappel. Le compromis est
volontairement prudent — la liste ne contient que des termes effectivement
releves comme faux positifs, jamais des termes ajoutes par anticipation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.detect.generique import est_generique  # noqa: E402
from caviardoc.engine import Origine, Span  # noqa: E402
from caviardoc.pipeline import filtrer_generiques  # noqa: E402


class TestVocabulaireNeutre:
    def test_terme_de_metier_seul(self):
        assert est_generique("patient")
        assert est_generique("médecin")

    def test_qualificatif_de_resultat(self):
        assert est_generique("Non validé")
        assert est_generique("anormal")

    def test_un_seul_mot_significatif_suffit_a_conserver(self):
        """« Monsieur » est generique, « Monsieur Zorglub » ne l'est pas."""
        assert est_generique("Monsieur")
        assert not est_generique("Monsieur Zorglub")

    def test_accents_et_casse_ignores(self):
        assert est_generique("PATIENTE")
        assert est_generique("perimetre")
        assert est_generique("périmètre")

    def test_texte_vide_ou_ponctuation_conserve(self):
        """Sans mot, la liste n'a pas d'avis : on ne retire rien."""
        assert not est_generique("")
        assert not est_generique("---")


class TestChiffresJamaisEcartes:
    """Une liste de mots n'a aucune competence sur des chiffres."""

    def test_age_conserve_malgre_un_mot_generique(self):
        """« 93 ans » n'a pour mot que « ans », mais l'age identifie."""
        assert not est_generique("93 ans")

    def test_numero_conserve(self):
        assert not est_generique("032 555 88 66")
        assert not est_generique("23.04.1931")

    def test_libelle_avec_numero_conserve(self):
        assert not est_generique("page 12")


class TestFiltrage:
    def test_les_regles_ne_sont_jamais_filtrees(self):
        """Une somme de controle valide ne se discute pas.

        Si une regle deterministe reconnait un identifiant dans un texte qui
        ressemble a du vocabulaire courant, c'est la regle qui a raison.
        """
        sp = Span(0, 7, "numero de dossier", 1.0, "patient", Origine.REGLE.value)
        assert filtrer_generiques([sp]) == [sp]

    def test_une_detection_du_modele_est_filtrable(self):
        sp = Span(0, 7, "personne", 0.4, "patient", Origine.MODELE.value)
        assert filtrer_generiques([sp]) == []

    def test_une_propagation_est_filtrable(self):
        sp = Span(0, 7, "personne", 1.0, "patient", Origine.PROPAGE.value)
        assert filtrer_generiques([sp]) == []

    def test_une_vraie_entite_survit(self):
        sp = Span(0, 9, "personne", 0.6, "Zorglub", Origine.MODELE.value)
        assert filtrer_generiques([sp]) == [sp]

    def test_filtrage_partiel_dun_lot(self):
        lot = [
            Span(0, 7, "personne", 0.4, "patient"),
            Span(8, 17, "personne", 0.6, "Zorglub"),
            Span(18, 28, "date", 1.0, "23.04.1931", Origine.REGLE.value),
        ]
        gardes = [s.text for s in filtrer_generiques(lot)]
        assert gardes == ["Zorglub", "23.04.1931"]


def test_je_n_est_personne():
    """Le modele prend « Je » en tete de phrase pour une personne."""
    assert est_generique("Je")


def test_les_formules_d_appel_ne_sont_pas_des_noms():
    """« Chere Consœur, » devenait « Chere PERSONNE_2 » : la ligature
    echappait a « consoeur », et « Cher », « Collegue » n'etaient pas listes."""
    from caviardoc.detect.generique import est_generique
    for appel in ("Chère Consœur", "Cher Confrère", "Chère Collègue",
                  "Cher Monsieur", "Chère Doctoresse", "Chers collègues"):
        assert est_generique(appel), appel
    assert not est_generique("Chère Madame Dupont")
