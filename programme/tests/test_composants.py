"""Un prenom ne doit pas survivre a cote d'un nom masque.

Deux cas de fuite, avec des noms inventes, qu'une relecture faite apres
l'anonymisation aurait revelees :

- un courriel adresse a « Paul-Machin Tartempion », puis commence par « Salut
  Paul-Machin, ». Le nom complet etait masque, le prenom seul non ;
- une signature « Valide electroniquement par Brontosaure Bidule », ou le
  patronyme etait masque par propagation, mais pas le prenom accole.

Les noms employes ici sont inventes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.documents.commun import propager_chaines  # noqa: E402
from caviardoc.engine import Origine, Span, apply_spans  # noqa: E402
from caviardoc.pipeline import (_merge, composants,  # noqa: E402
                              etendre_aux_voisins, propager,
                              propager_composants)


def _personne(texte: str, chaine: str) -> Span:
    i = texte.index(chaine)
    return Span(i, i + len(chaine), "personne", 0.99, chaine,
                Origine.MODELE.value)


def _masquer(texte: str, spans: list[Span]) -> str:
    return apply_spans(texte, _merge(spans, texte), render=lambda s: "[X]")


class TestComposants:
    def test_prenom_compose_et_patronyme(self):
        assert composants("Paul-Machin Tartempion") == ["Paul-Machin", "Tartempion"]

    def test_titres_et_fonctions_ecartes(self):
        assert composants("Dre Sylvie Girard") == ["Sylvie", "Girard"]
        assert composants("Monsieur Zorglub") == ["Zorglub"]

    def test_un_span_qui_n_est_pas_qu_un_nom_n_est_pas_decoupe(self):
        """Une union fusionnee porte un intitule, une date, du texte courant."""
        assert composants("Arrêt de travail Dupont") == []
        assert composants("Vaccination 12.03.2021") == []

    def test_un_nom_d_un_seul_mot_n_a_pas_de_composants(self):
        """Il est deja propage tel quel ; le redecouper ne sert a rien."""
        assert composants("Zorglub") == []


class TestPropagationDesComposants:
    COURRIEL = ("À: Paul-Machin Tartempion <ap@example.com>\n\n"
                "Salut Paul-Machin,\n\nJe suivais ce patient.")

    def test_le_prenom_seul_est_masque(self):
        nom = _personne(self.COURRIEL, "Paul-Machin Tartempion")
        spans = [nom] + propager_composants(self.COURRIEL, [nom])
        sortie = _masquer(self.COURRIEL, spans)
        assert "André" not in sortie
        assert "Salut [X]," in sortie

    def test_une_page_suivante_est_atteinte(self):
        """La propagation entre pages passe par les chaines connues."""
        page = "Salut Paul-Machin, voici le rapport de M. TARTEMPION."
        spans = propager_chaines(page, [], {"Paul-Machin Tartempion": "personne"})
        sortie = _masquer(page, spans)
        assert "André" not in sortie
        assert "TARTEMPION" not in sortie

    def test_un_composant_ne_ronge_pas_un_autre_mot(self):
        """« Marc » ne doit pas mordre dans « Marche »."""
        texte = "Marc Dubois.\nMarche difficile, Marcelin absent."
        nom = _personne(texte, "Marc Dubois")
        spans = [nom] + propager_composants(texte, [nom])
        sortie = _masquer(texte, spans)
        assert "Marche difficile, Marcelin absent." in sortie

    def test_un_prenom_nom_commun_reste_lisible_en_minuscules(self):
        """Le prenom « Claire » ne noircit pas une « urine claire »."""
        texte = "Claire Morel, infirmière.\nUrine claire. Claire a appelé."
        nom = _personne(texte, "Claire Morel")
        spans = [nom] + propager_composants(texte, [nom])
        sortie = _masquer(texte, spans)
        assert "Urine claire." in sortie
        assert "Claire" not in sortie


class TestExtensionAuxVoisins:
    SIGNATURE = ("Dresse Brontosaure\n\n"
                 "Validé électroniquement par Brontosaure Bidule "
                 "(Médecin chef de clinique), le 23.07.19")

    def _patronymes(self):
        texte = self.SIGNATURE
        premier = _personne(texte, "Brontosaure")
        return propager(texte, [premier])

    def test_le_prenom_accole_est_masque(self):
        texte = self.SIGNATURE
        connus = _merge(self._patronymes(), texte)
        spans = connus + propager(texte, etendre_aux_voisins(texte, connus))
        sortie = _masquer(texte, spans)
        assert "Bidule" not in sortie
        assert "par [X] (Médecin chef de clinique)" in sortie

    def test_une_detection_peu_sure_n_est_pas_decoupee(self):
        texte = "Investigation Directe\nInvestigation Directe du 3 mai"
        douteux = Span(0, 21, "personne", 0.26, "Investigation Directe")
        assert propager_composants(texte, [douteux]) == []

    def test_un_titre_ou_une_fonction_n_est_pas_absorbe(self):
        texte = "Validé par Docteur Brontosaure Médecin"
        nom = _personne(texte, "Brontosaure")
        spans = [nom] + etendre_aux_voisins(texte, [nom])
        assert _masquer(texte, spans) == "Validé par Docteur [X] Médecin"

    def test_une_formule_d_appel_ou_un_titre_en_tete_reste_lisible(self):
        """« Salut » et « Dresse » precedent un nom sans en faire partie."""
        for texte in ("Salut Paul-Machin,", "Dresse Brontosaure"):
            nom = texte.split()[1].rstrip(",")
            sp = _personne(texte, nom)
            spans = [sp] + etendre_aux_voisins(texte, [sp])
            assert _masquer(texte, spans).split()[0] == texte.split()[0]

    def test_rien_ne_franchit_un_saut_de_ligne_ni_une_ponctuation(self):
        texte = "Brontosaure\nBidule, Échallens"
        nom = _personne(texte, "Brontosaure")
        spans = [nom] + etendre_aux_voisins(texte, [nom])
        assert _masquer(texte, spans) == "[X]\nBidule, Échallens"

    def test_un_nom_complet_n_est_pas_etendu(self):
        """Il emporterait le service ou la ville qui le suit."""
        texte = "Jean Dupont Cardiologie"
        nom = _personne(texte, "Jean Dupont")
        spans = [nom] + etendre_aux_voisins(texte, [nom])
        assert _masquer(texte, spans) == "[X] Cardiologie"

    def test_le_mot_qui_precede_est_aussi_pris(self):
        texte = "signé Bidule Brontosaure."
        nom = _personne(texte, "Brontosaure")
        spans = [nom] + etendre_aux_voisins(texte, [nom])
        assert _masquer(texte, spans) == "signé [X]."


class TestFrontieresDeMot:
    """La propagation ne noircit plus l'interieur des mots."""

    def test_un_fragment_ne_ronge_pas_les_mots_qui_le_contiennent(self):
        """« cile », lu apres la coupure de « domi-cile », epargne « domicile »."""
        texte = "Retour a domi-\ncile prevu.\nSoins a domicile."
        i = texte.index("cile")
        fragment = Span(i, i + 4, "personne", 0.5, "cile")
        sortie = _masquer(texte, propager(texte, [fragment]))
        assert "Soins a domicile." in sortie

    def test_une_page_suivante_n_est_pas_rongee_non_plus(self):
        texte = "Le Sapiniere-Centre et le quartier de la Sapiniere 45."
        spans = propager_chaines(texte, [], {"Sapiniere 45": "rue"})
        assert _masquer(texte, spans) == ("Le Sapiniere-Centre et le quartier "
                                          "de la [X].")

    def test_les_lettres_espacees_restent_trouvables(self):
        texte = "C a b i n e t  d e  r a d i o l o g i e\nCabinet de radiologie"
        spans = propager_chaines(texte, [], {"Cabinet de radiologie": "employeur"})
        assert _masquer(texte, spans) == "[X]\n[X]"

    def test_un_chiffre_colle_ne_rompt_pas_la_frontiere(self):
        texte = "ne le12.03.1940 a Echallens"
        spans = propager_chaines(texte, [], {"12.03.1940": "date"})
        assert "1940" not in _masquer(texte, spans)
