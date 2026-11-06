"""Liste d'exceptions : ce qui s'apprend, ce qui ne s'apprend jamais.

Les deux listes ne sont pas symetriques, et ces tests le verrouillent.
Retenir a tort un « toujours masquer » masque un mot de trop. Retenir a tort
un « jamais masquer » decouvre une chaine dans tous les documents suivants,
en silence — c'est la seule erreur de ce module qui puisse blesser quelqu'un.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import exceptions  # noqa: E402
from caviardoc.engine import Origine, Span  # noqa: E402
from caviardoc.exceptions import (Repertoire,  # noqa: E402
                                apprenable, apprendre_de, normaliser)

AVS = "756.1234.5678.97"
IBAN = "CH93 0076 2011 6238 5295 7"


@pytest.fixture
def donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def repertoire(donnees):
    return Repertoire.charger()


class TestCeQuiNeSApprendJamais:
    """Une regle a somme de controle designe un identifiant personnel."""

    @pytest.mark.parametrize("chaine", [AVS, IBAN, "032 555 88 66",
                                        "jm.dubois@example.com"])
    def test_les_identifiants_a_somme_de_controle_sont_refuses(self, chaine):
        motif = apprenable(chaine)
        assert motif, f"{chaine} ne doit pas etre apprenable"
        assert "somme de controle" in motif

    def test_le_refus_sapplique_aussi_a_jamais_masquer(self, repertoire):
        actif, explication = repertoire.retenir_jamais(AVS, "c'est le mien")
        assert actif is False
        assert "identifiant personnel" in explication
        assert repertoire.jamais == []

    def test_une_chaine_trop_courte_est_refusee(self):
        assert "trop courte" in apprenable("Dr")

    def test_une_phrase_entiere_est_refusee(self):
        phrase = "le patient a ete recu ce matin par le medecin de garde"
        assert apprenable(phrase)

    def test_le_vocabulaire_lui_sapprend(self):
        for chaine in ("Macula", "Cher confrere", "Cabinet du Belvedere"):
            assert apprenable(chaine) == "", chaine


class TestJamaisMasquer:
    def test_actif_des_la_premiere_fois(self, repertoire):
        actif, explication = repertoire.retenir_jamais("Macula", "terme medical")
        assert actif is True
        assert explication == ""
        assert repertoire.jamais_masquer("Macula") is True

    def test_retirer_de_la_liste_le_desactive(self, repertoire):
        repertoire.retenir_jamais("Macula")
        assert repertoire.oublier("Macula") is True
        assert repertoire.jamais_masquer("Macula") is False

    def test_la_casse_et_les_espaces_ne_comptent_pas(self, repertoire):
        repertoire.retenir_jamais("Cher confrere")
        repertoire.retenir_jamais("cher   CONFRERE")
        assert len(repertoire.jamais) == 1, "une seule entree, pas deux"
        assert repertoire.jamais_masquer("Cher Confrere") is True

    def test_les_motifs_sont_conserves(self, repertoire):
        repertoire.retenir_jamais("Macula", "terme anatomique")
        repertoire.retenir_jamais("Macula", "toujours un faux positif")
        assert repertoire.jamais[0].motifs == ["terme anatomique",
                                               "toujours un faux positif"]


class TestFiltre:
    def test_un_span_du_modele_est_retire(self, repertoire):
        repertoire.retenir_jamais("Macula")
        spans = [Span(0, 6, "personne", 0.7, "Macula", Origine.MODELE.value)]
        assert repertoire.filtrer(spans) == []

    def test_un_span_de_regle_traverse_le_filtre(self, repertoire):
        """Rien dans cette liste n'a autorite sur une somme de controle."""
        # On force l'entree, en contournant `retenir_jamais` qui la refuserait.
        repertoire.jamais.append(
            exceptions.EntreeJamais(chaine=AVS, vues=99))
        spans = [Span(0, len(AVS), "numero AVS", 1.0, AVS,
                      Origine.REGLE.value)]
        assert repertoire.filtrer(spans) == spans


class TestToujoursMasquer:
    def test_actif_des_la_premiere_fois(self, repertoire):
        """Le sens sur : au pire un mot masque pour rien."""
        actif, _ = repertoire.retenir_toujours("Yannick Fontannaz")
        assert actif is True

    def test_la_chaine_nest_jamais_conservee_en_clair(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        chemin = repertoire.enregistrer()
        brut = chemin.read_text(encoding="utf-8")

        assert "Yannick" not in brut
        assert "Fontannaz" not in brut
        assert "fontannaz" not in brut.lower()

    def test_la_vue_ne_divulgue_rien_non_plus(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        vue = json.dumps(repertoire.vue(), ensure_ascii=False)
        assert "Fontannaz" not in vue
        assert repertoire.vue()["toujours"][0]["mots"] == 2

    def test_le_sel_differe_dune_machine_a_lautre(self, tmp_path,
                                                  monkeypatch):
        """Un fichier d'exceptions ne doit pas servir a tester un nom."""
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "a"))
        a = Repertoire.charger()
        monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "b"))
        b = Repertoire.charger()

        assert a.sel != b.sel
        assert a.empreinte("Yannick Fontannaz") != b.empreinte("Yannick Fontannaz")

    def test_la_chaine_apprise_est_retrouvee_dans_un_texte(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        texte = "Le patient Yannick Fontannaz, ne le 23.04.1931, va bien."

        spans = repertoire.spans_toujours(texte)

        assert len(spans) == 1
        assert spans[0].text == "Yannick Fontannaz"
        assert spans[0].origine == Origine.HUMAIN.value
        assert texte[spans[0].start:spans[0].end] == "Yannick Fontannaz"

    def test_toutes_les_occurrences_sont_trouvees(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        texte = "Yannick Fontannaz ... revu ce jour, Yannick Fontannaz sort."
        assert len(repertoire.spans_toujours(texte)) == 2

    def test_la_casse_ne_fait_pas_manquer(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        spans = repertoire.spans_toujours("Monsieur YANNICK FONTANNAZ, 97 ans")
        assert len(spans) == 1
        assert spans[0].text == "YANNICK FONTANNAZ"

    def test_un_repertoire_vide_ne_trouve_rien(self, repertoire):
        assert repertoire.spans_toujours("un texte quelconque") == []


class TestPersistance:
    def test_aller_retour(self, donnees):
        r = Repertoire.charger()
        r.retenir_jamais("Macula")
        r.retenir_jamais("Macula")
        r.retenir_toujours("Yannick Fontannaz")
        r.enregistrer()

        relu = Repertoire.charger()
        assert relu.jamais_masquer("Macula") is True
        assert len(relu.spans_toujours("vu par Yannick Fontannaz hier")) == 1

    def test_un_fichier_abime_vaut_un_repertoire_vide(self, donnees):
        """Une liste abimee ne doit pas empecher de caviarder un document."""
        from caviardoc import paths
        paths.ensure_dirs()
        Repertoire.chemin().write_text("{pas du json", encoding="utf-8")

        r = Repertoire.charger()
        assert r.jamais == [] and r.toujours == []
        assert r.sel, "un sel neuf est tire"

    def test_le_fichier_porte_un_avertissement(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        brut = repertoire.enregistrer().read_text(encoding="utf-8")
        assert "Ne pas versionner" in brut

    def test_le_repertoire_vit_hors_du_depot(self, donnees):
        from caviardoc.paths import is_inside_synced_folder
        assert not is_inside_synced_folder(Repertoire.chemin())

    def test_oublier_retire_des_deux_listes(self, repertoire):
        repertoire.retenir_jamais("Macula")
        repertoire.retenir_toujours("Yannick Fontannaz")

        assert repertoire.oublier("Macula") is True
        assert repertoire.oublier("Yannick Fontannaz") is True
        assert repertoire.oublier("jamais vu") is False
        assert repertoire.jamais == [] and repertoire.toujours == []

    def test_oublier_anonymes_garde_ce_qui_se_lit_encore(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        repertoire.retenir_toujours("Autre Nom")
        clairs = {repertoire.empreinte("Autre Nom"): "Autre Nom"}

        assert repertoire.oublier_anonymes(clairs) == 1
        assert [e.empreinte for e in repertoire.toujours] == list(clairs)
        assert repertoire.oublier_anonymes(clairs) == 0

    def test_tout_oublier_vide_les_deux_listes(self, repertoire):
        repertoire.retenir_jamais("Macula")
        repertoire.retenir_toujours("Yannick Fontannaz")

        assert repertoire.tout_oublier() == 2
        assert repertoire.jamais == [] and repertoire.toujours == []
        assert repertoire.tout_oublier() == 0


class TestApprendreDUneSession:
    def _session(self):
        from caviardoc.revue import ajouter, decider, preparer
        texte = "Macula normale. Patient Yannick Fontannaz, AVS " + AVS
        spans = [Span(0, 6, "personne", 0.7, "Macula", Origine.MODELE.value),
                 Span(texte.index(AVS), texte.index(AVS) + len(AVS),
                      "numero AVS", 1.0, AVS, Origine.REGLE.value)]
        session = preparer("essai.docx", [("corps", texte, spans)])
        decider(session, {"corps:0": {"etat": "rejetee", "motif": "anatomie"},
                          "corps:1": {"etat": "rejetee", "motif": "le mien"}})
        ajouter(session, "corps", texte, texte.index("Yannick"),
                texte.index("Yannick") + len("Yannick Fontannaz"))
        return session

    def test_une_session_alimente_les_deux_listes(self, donnees):
        compte = apprendre_de(self._session())

        relu = Repertoire.charger()
        assert len(relu.jamais) == 1, "l'AVS ne doit pas y figurer"
        assert relu.jamais[0].chaine == "Macula"
        assert len(relu.toujours) == 1
        assert compte["total"] == 3

    def test_lavs_rejete_est_signale_comme_non_retenu(self, donnees):
        compte = apprendre_de(self._session())
        refus = [r for r in compte["refuse"] if r["chaine"] == AVS]
        assert refus, "l'utilisateur doit savoir pourquoi ce rejet n'est pas garde"
        assert "identifiant personnel" in refus[0]["explication"]

    def test_le_compte_rendu_ne_divulgue_pas_les_ajouts(self, donnees):
        """Un ajout est une identite : elle n'a pas a reapparaitre en clair."""
        compte = apprendre_de(self._session())
        rendu = json.dumps(compte, ensure_ascii=False)
        assert "Fontannaz" not in rendu

    def test_une_session_suffit_pour_le_jamais(self, donnees):
        apprendre_de(self._session())
        assert Repertoire.charger().jamais_masquer("Macula") is True


class TestDansLePipeline:
    def test_le_filtre_sapplique_a_lanalyse(self, donnees):
        from caviardoc.pipeline import analyse

        r = Repertoire.charger()
        r.retenir_jamais("Macula")
        r.enregistrer()

        texte = f"Macula normale, AVS {AVS}."
        sans = analyse(None, texte)
        avec = analyse(None, texte, exceptions=Repertoire.charger())

        # Sans modele, seule la regle AVS detecte : le filtre ne doit pas y
        # toucher, et le resultat est identique.
        assert [s.text for s in sans] == [AVS]
        assert [s.text for s in avec] == [AVS]

    def test_les_ajouts_appris_sont_masques_a_lanalyse(self, donnees):
        """Sans modele : la seule detection possible vient de la liste."""
        from caviardoc.pipeline import analyse

        r = Repertoire.charger()
        r.retenir_toujours("Yannick Fontannaz")
        r.enregistrer()

        texte = "Le patient Yannick Fontannaz va bien."
        assert analyse(None, texte) == []
        trouves = analyse(None, texte, exceptions=Repertoire.charger())
        assert [s.text for s in trouves] == ["Yannick Fontannaz"]

    def test_toujours_lemporte_sur_jamais(self, donnees):
        """Le sens sur gagne quand les deux listes se contredisent."""
        from caviardoc.pipeline import analyse

        r = Repertoire.charger()
        r.retenir_toujours("Yannick Fontannaz")
        r.retenir_jamais("Yannick Fontannaz")
        r.enregistrer()

        trouves = analyse(None, "Vu Yannick Fontannaz ce jour.",
                          exceptions=Repertoire.charger())
        assert [s.text for s in trouves] == ["Yannick Fontannaz"]


def test_normaliser_ne_touche_pas_aux_accents():
    """Confondre « Girard » et « Girard » accentue masquerait l'un pour l'autre."""
    assert normaliser("Girárd") != normaliser("Girard")
    assert normaliser("  Cher   Confrere  ") == "cher confrere"


class TestVueEnClairEnMemoire:
    """Le mot ajoute se lit pendant la session ; le disque n'en garde que l'empreinte."""

    def test_une_chaine_connue_en_memoire_est_rendue(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        repertoire.retenir_toujours("Autre Patient")
        clairs = {repertoire.empreinte("Yannick Fontannaz"): "Yannick Fontannaz"}
        entrees = repertoire.vue(clairs)["toujours"]
        assert [e.get("chaine") for e in entrees] == ["Yannick Fontannaz", None]

    def test_sans_memoire_rien_ne_fuit(self, repertoire):
        repertoire.retenir_toujours("Yannick Fontannaz")
        assert "chaine" not in repertoire.vue()["toujours"][0]
        assert "Fontannaz" not in repertoire.enregistrer().read_text(encoding="utf-8")
