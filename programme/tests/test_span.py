"""Contrats du type Span : vocabulaire, provenance, coherence du texte.

`Span` est traverse par cinq communautes du projet — detection, roles, dates,
caviardage, orchestration. Ce n'est pas un god object : il porte cinq champs et
aucune responsabilite metier. Mais sa centralite fait que ses faiblesses de
typage se propagent partout, d'ou ces tests.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import pipeline  # noqa: E402
from caviardoc.engine import (AUTORITE, Categorie, Origine, Span,  # noqa: E402
                            est_date, est_identifiant_personnel, est_naissance,
                            est_organisation, est_personne,
                            est_quasi_identifiant)


def _span(label="personne", score=0.9, origine=Origine.MODELE.value,
          start=0, end=9, text="Zorglub"):
    return Span(start, end, label, score, text, origine)


class TestVocabulaire:
    """Les etiquettes ne doivent plus etre testees par comparaison de chaines."""

    def test_les_predicats_couvrent_le_vocabulaire_du_modele(self):
        """Chaque categorie doit etre reconnue par au moins un predicat.

        Sans cela une etiquette pourrait traverser tout le systeme sans jamais
        etre classee — et donc sans jamais etre masquee au niveau « patient ».
        """
        predicats = (est_personne, est_date, est_naissance,
                     est_identifiant_personnel, est_organisation,
                     est_quasi_identifiant)
        orphelines = [c.value for c in Categorie
                      if not any(p(c.value) for p in predicats)]
        # Les etiquettes de localisation et de contact ne relevent d'aucun
        # predicat de role : elles sont masquees par appartenance au bloc
        # patient, pas par leur nature. On fige la liste pour qu'un ajout
        # silencieux au vocabulaire soit visible ici.
        assert set(orphelines) == {
            "adresse postale", "rue", "ville", "code postal et localite",
            "numero de telephone", "email", "IBAN", "numero de carte",
            "numero de dossier", "identifiant de dossier",
        }

    def test_naissance_est_aussi_une_date(self):
        assert est_date(Categorie.DATE_NAISSANCE.value)
        assert est_naissance(Categorie.DATE_NAISSANCE.value)

    def test_date_simple_n_est_pas_une_naissance(self):
        assert est_date(Categorie.DATE.value)
        assert not est_naissance(Categorie.DATE.value)

    def test_identifiants_personnels(self):
        for c in (Categorie.AVS, Categorie.PATIENT, Categorie.ASSURE):
            assert est_identifiant_personnel(c.value), c
        assert not est_identifiant_personnel(Categorie.DOSSIER.value)

    def test_predicats_insensibles_a_la_casse(self):
        assert est_personne("PERSONNE")
        assert est_identifiant_personnel("NUMERO AVS")

    def test_methodes_de_span_delegent_aux_predicats(self):
        assert _span(label="personne").est_personne()
        assert _span(label="date de naissance").est_naissance()
        assert _span(label="employeur").est_organisation()


class TestOrigine:
    """La provenance ne doit plus etre deduite d'une valeur de score."""

    def test_ordre_d_autorite(self):
        assert (AUTORITE[Origine.REGLE.value]
                > AUTORITE[Origine.PROPAGE.value]
                > AUTORITE[Origine.MODELE.value])

    def test_origine_par_defaut_est_le_modele(self):
        assert Span(0, 1, "personne", 0.9, "a").origine == Origine.MODELE.value

    def test_la_regle_impose_son_etiquette_malgre_un_score_egal(self):
        """Une somme de controle l'emporte sur une estimation du modele.

        Auparavant l'arbitrage se jouait sur `score`, ou la valeur 1,0 servait
        de sentinelle pour « regle ». La propagation attribuant elle aussi 1,0,
        l'egalite etait tranchee par l'ordre d'iteration — donc par hasard.
        """
        texte = "AVS 756.1234.5678.97"
        spans = [
            Span(4, 20, "personne", 1.0, texte[4:20], Origine.MODELE.value),
            Span(4, 20, "numero AVS", 1.0, texte[4:20], Origine.REGLE.value),
        ]
        fusionnes = pipeline._merge(spans, texte)
        assert len(fusionnes) == 1
        assert fusionnes[0].label == "numero AVS"
        assert fusionnes[0].origine == Origine.REGLE.value

    def test_la_propagation_prime_sur_le_modele(self):
        texte = "Zorglub ici"
        spans = [
            Span(0, 9, "ville", 0.8, "Zorglub", Origine.MODELE.value),
            Span(0, 9, "personne", 0.8, "Zorglub", Origine.PROPAGE.value),
        ]
        assert pipeline._merge(spans, texte)[0].label == "personne"


class TestCoherenceDuTexte:
    """`text` doit toujours correspondre a `[start:end]` du document source."""

    def test_merge_exige_le_texte_source(self):
        """Le parametre ne doit pas avoir de valeur par defaut.

        Un defaut vide rouvrait un chemin ou `text` etait repris du span
        arbitre plutot que decoupe dans la source : apres fusion par union, il
        ne correspondait alors plus aux offsets, et la verification finale
        cherchait une chaine qui n'existait pas.
        """
        params = inspect.signature(pipeline._merge).parameters
        assert params["texte"].default is inspect.Parameter.empty

    def test_le_texte_fusionne_est_decoupe_dans_la_source(self):
        """L'union de deux spans qui se chevauchent porte le texte de l'union.

        C'est le cas reel qui a motive la correction : sur un en-tete de
        laboratoire, deux passes detectaient des empans differents et se
        chevauchant. Le span fusionne heritait alors du texte de l'un des deux,
        qui ne correspondait plus a ses propres offsets.
        """
        texte = "Monsieur Zorglub Jean-Bidule, ne le 23.04.1931"
        spans = [
            Span(9, 18, "personne", 0.9, "Zorglub"),
            Span(9, 28, "personne", 0.8, "Zorglub Jean-Bidule"),
        ]
        fusionne = pipeline._merge(spans, texte)[0]
        assert fusionne.text == texte[fusionne.start:fusionne.end]
        assert fusionne.text == "Zorglub Jean-Bidule"

    def test_spans_disjoints_ne_sont_pas_fusionnes(self):
        """Deux entites separees par une espace restent deux entites."""
        texte = "Monsieur Zorglub Jean-Bidule, ne le 23.04.1931"
        spans = [
            Span(9, 18, "personne", 0.9, "Zorglub"),
            Span(19, 28, "personne", 0.9, "Jean-Bidule"),
        ]
        assert len(pipeline._merge(spans, texte)) == 2

    def test_merge_sans_texte_leve_une_erreur(self):
        with pytest.raises(TypeError):
            pipeline._merge([_span()])


class TestLignesVides:
    """Un span qui franchit une ligne vide reunit deux blocs du document."""

    def test_coupe_a_la_ligne_vide(self):
        """Cas du rapport type : le modele lisait l'adresse du destinataire et
        le lieu de la lettre comme une seule adresse postale."""
        texte = "Case postale\n1510 Moudon\n\nGrandvaux, le 5 avril 2014"
        debut, fin = 13, texte.index(",")
        sp = Span(debut, fin, "code postal et localite", 1.0,
                  texte[debut:fin], Origine.REGLE.value)
        coupes = pipeline.couper_aux_lignes_vides(texte, [sp])
        assert [s.text for s in coupes] == ["1510 Moudon", "Grandvaux"]
        assert all(s.text == texte[s.start:s.end] for s in coupes)
        assert all(s.label == sp.label and s.origine == sp.origine
                   for s in coupes)

    def test_un_saut_de_ligne_simple_ne_coupe_pas(self):
        """Un nom renvoye a la ligne reste un seul nom."""
        texte = "Dr Pierre\nMercier"
        sp = Span(3, len(texte), "personne", 0.9, texte[3:])
        assert pipeline.couper_aux_lignes_vides(texte, [sp]) == [sp]

    def test_ligne_faite_d_espaces(self):
        texte = "Moudon\n   \nGrandvaux"
        sp = Span(0, len(texte), "ville", 0.9, texte)
        coupes = pipeline.couper_aux_lignes_vides(texte, [sp])
        assert [s.text for s in coupes] == ["Moudon", "Grandvaux"]
