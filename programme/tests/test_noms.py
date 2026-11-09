"""Anonymisation du nom de fichier : un fichier bien caviarde mais mal nomme fuit."""

import os
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.noms import (anonymiser_dossier, anonymiser_nom,  # noqa: E402
                         pour_detection)

CORR = {
    "Jean-Bidule Zorglub": "PERSONNE_1",
    "Zorglub": "PERSONNE_1",
    "J-C. Juvalier": "PERSONNE_2",
}


class TestPourDetection:
    def test_longueur_preservee(self):
        n = "5092_Jean-Bidule_Zorglub_Ordonnance.pdf"
        assert len(pour_detection(n)) == len(n)

    def test_separateurs_devenus_espaces(self):
        assert pour_detection("a_b-c.d") == "a b c d"


class TestAnonymisation:
    def test_separateurs_encadrants_preserves(self):
        """Le separateur qui precede le nom ne doit pas etre absorbe."""
        assert (anonymiser_nom("5092_Jean-Bidule_Zorglub_Ordonnance.pdf", CORR)
                == "5092_PERSONNE_1_Ordonnance.pdf")

    def test_extension_preservee(self):
        assert anonymiser_nom("Zorglub.pdf", CORR).endswith(".pdf")

    def test_casse_et_accents_ignores(self):
        assert anonymiser_nom("ZORGLUB-rapport.pdf", CORR) == "PERSONNE_1-rapport.pdf"

    def test_entite_la_plus_longue_prioritaire(self):
        """« Jean-Bidule Zorglub » doit primer sur « Zorglub » seul."""
        out = anonymiser_nom("dossier_Jean-Bidule_Zorglub_2024.pdf", CORR)
        assert out == "dossier_PERSONNE_1_2024.pdf"
        assert "Jean-Bidule" not in out

    def test_nom_sans_pii_inchange(self):
        assert anonymiser_nom("rapport_anonyme_2024.pdf", CORR) == "rapport_anonyme_2024.pdf"

    def test_plusieurs_entites(self):
        out = anonymiser_nom("Zorglub_vu_par_J-C._Juvalier.pdf", CORR)
        assert "Zorglub" not in out and "Juvalier" not in out
        assert "PERSONNE_1" in out and "PERSONNE_2" in out

    def test_correspondances_vides(self):
        assert anonymiser_nom("Zorglub.pdf", {}) == "Zorglub.pdf"


class TestRegressionsNomsMutiles:
    """Cas de noms mutiles, avec des noms inventes (Zorglub, Hürlibrück...)."""

    CORR = {
        "Jean-Bidule Zorglub": "PERSONNE_1",
        "Zorglub": "PERSONNE_1",
        "Jean-Bidule": "PERSONNE_1",
        "Ele": "PERSONNE_20",
        "Pre": "PERSONNE_18",
        "Rue": "PERSONNE_9",
        "adresse postale": "ADRESSE_2",
        "Le Brassus": "VILLE_2",
    }

    def test_pas_de_correspondance_en_plein_mot(self):
        """« Ele » ne doit pas etre reconnu dans « Electroneuromyographie »."""
        out = anonymiser_nom("Zorglub_Electroneuromyographie.pdf", self.CORR)
        assert out == "PERSONNE_1_Electroneuromyographie.pdf"

    def test_prefixe_court_ignore(self):
        """« Pre » ne doit pas mutiler « Prescription »."""
        out = anonymiser_nom("Zorglub_Prescription.pdf", self.CORR)
        assert "Prescription" in out

    def test_pas_de_correspondance_a_travers_un_tiret(self):
        """« Rue » ne doit pas etre reconnu dans « Rueyres »."""
        out = anonymiser_nom("Zorglub_CTR-Rueyres.pdf", self.CORR)
        assert out.endswith("CTR-Rueyres.pdf")

    def test_pseudonyme_non_reinjecte(self):
        """Un pseudonyme deja pose ne doit pas servir de cible a une autre entite.

        Un remplacement sequentiel produit « ADRPERSONNE_13E_POSTALE_2 » ;
        d'ou la passe unique sur le nom d'origine.
        """
        out = anonymiser_nom("Zorglub_adresse postale.pdf", self.CORR)
        assert out == "PERSONNE_1_ADRESSE_2.pdf"

    def test_pseudonymes_repetes_compactes(self):
        out = anonymiser_nom("Jean-Bidule_Zorglub_rapport.pdf", self.CORR)
        assert out == "PERSONNE_1_rapport.pdf"

    def test_entite_multi_mots_reconnue(self):
        out = anonymiser_nom("CTR-Le Brassus.pdf", self.CORR)
        assert out == "CTR-VILLE_2.pdf"

    def test_trema_transcrit_dans_le_texte(self):
        """Le scan ecrit « HUERLIBRUECK », le fichier « Hürlibrück ».

        Sans trema, les deux formes canoniques divergeaient (« hurlig » contre
        « huerlig ») et le patronyme du patient restait dans le nom produit.
        """
        corr = {"HUERLIBRUECK Truc": "PERSONNE_1", "HUERLIBRUECK": "PERSONNE_1"}
        out = anonymiser_nom("3175_Truc_Hürlibrück_Dermatologie.pdf", corr)
        assert out == "3175_Truc_PERSONNE_1_Dermatologie.pdf"

    def test_trema_transcrit_dans_le_fichier(self):
        corr = {"Hürlibrück": "PERSONNE_1"}
        assert anonymiser_nom("Huerlibrueck_rapport.pdf", corr) == "PERSONNE_1_rapport.pdf"

    def test_trema_simplement_retire(self):
        corr = {"Hürlibrück": "PERSONNE_1"}
        assert anonymiser_nom("HURLIBRUCK_rapport.pdf", corr) == "PERSONNE_1_rapport.pdf"

    def test_nom_de_fichier_decompose(self):
        """Le Mac livre souvent « u » suivi d'un trema combinant."""
        import unicodedata
        nom = unicodedata.normalize("NFD", "Hürlibrück_rapport.pdf")
        assert anonymiser_nom(nom, {"Hürlibrück": "PERSONNE_1"}) == "PERSONNE_1_rapport.pdf"


class TestDossier:
    def test_un_point_ne_protege_pas_la_fin_du_nom(self):
        """Pour un fichier, « .Zorglub » serait une extension, epargnee.
        Un dossier n'en a pas : tout le nom est examine."""
        assert anonymiser_nom("Dossier.Zorglub", CORR) == "Dossier.Zorglub"
        assert anonymiser_dossier("Dossier.Zorglub", CORR) == "Dossier.PERSONNE_1"

    def test_meme_regle_que_pour_les_fichiers(self):
        assert anonymiser_dossier("5092_Jean-Bidule_Zorglub", CORR) == "5092_PERSONNE_1"


class TestRenommer:
    def test_renomme_simplement(self, tmp_path):
        from caviardoc.noms import renommer
        a = tmp_path / "a.txt"
        a.write_text("x")
        renommer(a, tmp_path / "b.txt")
        assert not a.exists() and (tmp_path / "b.txt").read_text() == "x"

    def test_refus_passager_puis_reussite(self, tmp_path, monkeypatch):
        from caviardoc import noms
        monkeypatch.setattr(noms, "_PAUSES_RENOMMAGE", (0.0, 0.0))
        a = tmp_path / "a.txt"
        a.write_text("x")
        reel = Path.rename
        appels = []

        def capricieux(self, cible):
            appels.append(1)
            if len(appels) < 3:
                raise PermissionError(1, "Operation not permitted")
            return reel(self, cible)

        monkeypatch.setattr(Path, "rename", capricieux)
        journal = []
        noms.renommer(a, tmp_path / "b.txt", journal)
        assert (tmp_path / "b.txt").exists() and not a.exists()
        assert any("refuse" in l for l in journal)
        assert any("essai 3" in l for l in journal)

    def test_refus_durable_copie_puis_supprime(self, tmp_path, monkeypatch):
        from caviardoc import noms
        monkeypatch.setattr(noms, "_PAUSES_RENOMMAGE", (0.0,))
        a = tmp_path / "a.txt"
        a.write_text("x")

        def toujours(self, cible):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(Path, "rename", toujours)
        journal = []
        noms.renommer(a, tmp_path / "b.txt", journal)
        assert (tmp_path / "b.txt").read_text() == "x" and not a.exists()
        assert any("copie puis suppression" in l for l in journal)

    def test_echec_total_laisse_l_erreur_et_pas_de_copie(self, tmp_path, monkeypatch):
        import pytest
        from caviardoc import noms
        monkeypatch.setattr(noms, "_PAUSES_RENOMMAGE", ())
        a = tmp_path / "a.txt"
        a.write_text("x")
        monkeypatch.setattr(Path, "rename", lambda s, c: (_ for _ in ()).throw(
            PermissionError(1, "Operation not permitted")))
        monkeypatch.setattr(Path, "unlink",
                            lambda s, missing_ok=False: (_ for _ in ()).throw(
                                PermissionError(1, "Operation not permitted"))
                            if s.name == "a.txt" else os.remove(s))
        with pytest.raises(PermissionError):
            noms.renommer(a, tmp_path / "b.txt")

    def test_n_ecrase_jamais_un_fichier_existant(self, tmp_path):
        """Deux originaux distincts peuvent recevoir le meme nom anonymise.
        Sous macOS le second ecrasait le premier ; sous Windows il plantait."""
        from caviardoc.noms import renommer
        deja = tmp_path / "PATIENT_1.pdf"
        deja.write_text("premier")
        a = tmp_path / "Jean Dupont.pdf"
        a.write_text("second")
        final = renommer(a, deja)
        assert final == tmp_path / "PATIENT_1 (2).pdf"
        assert deja.read_text() == "premier" and final.read_text() == "second"
        assert not a.exists()

    def test_la_sortie_texte_compte_aussi(self, tmp_path):
        """« Lettre.pdf » et « Lettre.docx » du meme lot ecrivaient tous deux
        « Lettre.txt » : le second ecrasait le premier sans rien dire."""
        from caviardoc.noms import nom_libre
        (tmp_path / "Lettre.pdf").write_text("pdf")
        (tmp_path / "Lettre.txt").write_text("texte du pdf")
        voulu = tmp_path / "Lettre.docx"
        assert nom_libre(voulu) == voulu
        assert nom_libre(voulu, compagnons=(".txt",)) == tmp_path / "Lettre (2).docx"


class TestMotEtablissementSeul:
    def test_cabinet_seul_n_est_pas_substitue(self):
        corr = {"Cabinet": "ETABLISSEMENT_1"}
        assert anonymiser_nom("Court_Cabinet.pdf", corr) == "Court_Cabinet.pdf"
        assert anonymiser_nom("Court_CABINET.pdf", corr) == "Court_CABINET.pdf"
        assert anonymiser_nom("Court_Hôpital.pdf",
                              {"Hôpital": "ETABLISSEMENT_1"}) == "Court_Hôpital.pdf"

    def test_cabinet_avec_nom_reste_substitue(self):
        corr = {"Cabinet Dupont": "ETABLISSEMENT_1"}
        assert "Dupont" not in anonymiser_nom("Cabinet Dupont_Court.pdf", corr)

    def test_la_table_ne_garde_pas_le_mot_seul(self):
        from caviardoc.pseudonymes import TablePseudonymes
        table = TablePseudonymes()
        table.pseudonyme("Cabinet", "etablissement")
        table.pseudonyme("Cabinet Dupont", "etablissement")
        assert "Cabinet" not in table.correspondances
        assert "Cabinet Dupont" in table.correspondances


class TestReclassementEtablissement:
    @staticmethod
    def _etiquette(texte):
        from caviardoc.engine import Span
        from caviardoc.pipeline import _merge
        (sp,) = _merge([Span(0, len(texte), "employeur", 0.9, texte)], texte)
        return sp.label

    def test_etablissements(self):
        assert self._etiquette("Hôpital de Pourtalès") == "etablissement"
        assert self._etiquette("Laboratoire Dr Risch") == "etablissement"
        assert self._etiquette("EMS Les Lilas") == "etablissement"

    def test_vrais_employeurs(self):
        assert self._etiquette("Nestlé SA") == "employeur"
        assert self._etiquette("Migros") == "employeur"

    def test_assureurs(self):
        for nom in ("Groupe Mutuel", "Assura", "CSS", "Helsana", "AXA",
                    "Zurich Assurances", "Vaudoise Assurances"):
            assert self._etiquette(nom) == "assureur", nom

    def test_zurich_seul_n_est_pas_un_assureur(self):
        assert self._etiquette("Zurich") == "employeur"

    def test_assureur_non_generique_dans_la_table(self):
        from caviardoc.pseudonymes import TablePseudonymes
        table = TablePseudonymes()
        assert table.pseudonyme("Groupe Mutuel", "assureur") == "ASSUREUR_1"
        assert "Groupe Mutuel" in table.correspondances


# --------------------------------------------------------------------------- #
# Noms d'un lot : personnes reliees, titres absorbes, mots proteges, patient,
# collisions. Noms inventes.
# --------------------------------------------------------------------------- #

def _table_du_lot():
    """La table d'un lot « Avis_pneumologie » : une pneumologue, un patient,
    un etablissement, un assureur, et une detection « employeur » faite des
    seuls mots « Service de pneumologie Dresse »."""
    from caviardoc.engine import Span
    from caviardoc.pseudonymes import TablePseudonymes
    t = TablePseudonymes()
    t.qualifier({"Dresse Véronique Negrel": ("medecin", "pneumologue"),
                 "Jean-Bidule Zorglub": ("patient", "")})
    t.enregistrer([Span(0, 1, "personne", 0.9, x) for x in (
        "Dresse Véronique Negrel", "Negrel", "Véronique",
        "Jean-Bidule Zorglub", "Zorglub")])
    for texte, etiquette in (("Service de pneumologie Dresse", "employeur"),
                             ("Hôpital de Pourtalès", "etablissement"),
                             ("Groupe Mutuel", "assureur")):
        t.pseudonyme(texte, etiquette)
    return t


#: Les douze noms du lot, et leur nom final une fois les collisions reglees.
DOUZE = {
    "Avis_pneumologique_ancien_dossier_Dresse_Negrel":
        "Avis_pneumologique_ancien_dossier_MEDECIN_1_PNEUMOLOGUE",
    "Pneumologie_Docteur_Veronique_Negrel": "Pneumologie_MEDECIN_1_PNEUMOLOGUE",
    "Pneumologie_Dr_Veronique_Negrel": "Pneumologie_MEDECIN_1_PNEUMOLOGUE_5",
    "pneumologie_Dresse_Negrel": "pneumologie_MEDECIN_1_PNEUMOLOGUE_6",
    "Pneumologie_Dresse_Veronique_Negrel": "Pneumologie_MEDECIN_1_PNEUMOLOGUE_7",
    "Pneumologie_Drsse_Veronique_Negrel": "Pneumologie_MEDECIN_1_PNEUMOLOGUE_8",
    "Pneumologie_Dresse_Veronique_Negrel_2": "Pneumologie_MEDECIN_1_PNEUMOLOGUE_2",
    "Pneumologie_Dresse_Veronique_Negrel_3": "Pneumologie_MEDECIN_1_PNEUMOLOGUE_3",
    "Pneumologie_Dresse_Veronique_Negrel_4": "Pneumologie_MEDECIN_1_PNEUMOLOGUE_4",
    "Cardiologie_Dr_Martin": "Cardiologie_MEDECIN_2_CARDIOLOGUE",
    "Court_Cabinet": "Court_Cabinet",
    "Rapport_Hopital_Pourtales": "Rapport_ETABLISSEMENT_1",
}


class TestNomsDuLot:
    @pytest.mark.parametrize("nom, attendu", [
        ("Avis_pneumologique_ancien_dossier_Dresse_Negrel.pdf",
         "Avis_pneumologique_ancien_dossier_MEDECIN_1_PNEUMOLOGUE.pdf"),
        ("Pneumologie_Docteur_Veronique_Negrel.pdf",
         "Pneumologie_MEDECIN_1_PNEUMOLOGUE.pdf"),
        ("Pneumologie_Dr_Veronique_Negrel.pdf",
         "Pneumologie_MEDECIN_1_PNEUMOLOGUE.pdf"),
        ("pneumologie_Dresse_Negrel.pdf", "pneumologie_MEDECIN_1_PNEUMOLOGUE.pdf"),
        ("Pneumologie_Dresse_Veronique_Negrel.pdf",
         "Pneumologie_MEDECIN_1_PNEUMOLOGUE.pdf"),
        ("Pneumologie_Drsse_Veronique_Negrel.pdf",
         "Pneumologie_MEDECIN_1_PNEUMOLOGUE.pdf"),
        ("Pneumologie_Dresse_Veronique_Negrel_3.pdf",
         "Pneumologie_MEDECIN_1_PNEUMOLOGUE_3.pdf"),
        ("Court_Cabinet.pdf", "Court_Cabinet.pdf"),
        ("Rapport_Hopital_Pourtales.pdf", "Rapport_ETABLISSEMENT_1.pdf"),
        ("Decompte_Groupe_Mutuel.pdf", "Decompte_ASSUREUR_1.pdf"),
        ("Lettre_Zorglub_Jean-Bidule.pdf", "Lettre_PATIENT.pdf"),
        # La detection « employeur » sans nom propre ne remplace rien.
        ("Lettre_Service_de_pneumologie_Dresse.pdf",
         "Lettre_Service_de_pneumologie_Dresse.pdf"),
    ])
    def test_un_nom(self, nom, attendu):
        assert anonymiser_nom(nom, table=_table_du_lot()) == attendu

    def test_les_douze_noms_et_leurs_collisions(self, tmp_path):
        import re
        from caviardoc.noms import AnonymiseurNoms, planifier_renommages
        documents = []
        for nom in DOUZE:
            (tmp_path / f"{nom}.pdf").write_text(nom)
            documents.append((tmp_path / f"{nom}.pdf", f"{nom}.pdf"))
        plan = planifier_renommages(
            documents, AnonymiseurNoms(table=_table_du_lot()))
        finaux = {s.stem: f.stem for s, f in plan}
        sortie = {n: finaux.get(n, n) for n in DOUZE}
        assert len({v.casefold() for v in sortie.values()}) == 12
        assert not any("(" in f.name for _, f in plan)
        # Le premier (ordre des originaux) a revendiquer un rang le garde.
        premiers: dict[tuple[str, str], str] = {}
        for nom in sorted(DOUZE, key=lambda n: (n.casefold(), n)):
            m = re.match(r"(.*)_([234])$", nom)
            if m:
                famille = DOUZE[nom].rsplit("_", 1)[0]
                premiers.setdefault((famille.casefold(), m.group(2)), nom)
        assert premiers
        for (_f, rang), nom in premiers.items():
            assert sortie[nom].endswith("_" + rang)
        # Un original sans rang obtient le nom nu.
        assert sortie["Pneumologie_Docteur_Veronique_Negrel"] \
            == "Pneumologie_MEDECIN_1_PNEUMOLOGUE"
        assert sortie["Court_Cabinet"] == "Court_Cabinet"

    def test_medecin_connu_du_seul_nom_rejoint_la_table(self):
        """« Dr Martin » n'est nomme par aucun document : il recoit un
        pseudonyme de medecin, que la table retient pour la suite."""
        t = _table_du_lot()
        assert (anonymiser_nom("Cardiologie_Dr_Martin.pdf", table=t)
                == "Cardiologie_MEDECIN_2_CARDIOLOGUE.pdf")
        assert t.pseudonyme("Dr Martin", "personne") == "MEDECIN_2_CARDIOLOGUE"
        assert (anonymiser_nom("Martin_ordonnance.pdf", table=t)
                == "MEDECIN_2_CARDIOLOGUE_ordonnance.pdf")

    def test_medecin_sans_table_numerote_apres_les_autres(self):
        corr = {"Negrel": "MEDECIN_1_PNEUMOLOGUE"}
        assert (anonymiser_nom("Cardiologie_Dr_Martin.pdf", corr,
                               personnes={"Negrel"})
                == "Cardiologie_MEDECIN_2_CARDIOLOGUE.pdf")

    def test_specialite_lue_dans_le_dossier(self):
        from caviardoc.noms import AnonymiseurNoms
        a = AnonymiseurNoms({}, contexte=("Avis_pneumologie",))
        assert a.nom("Lettre_Pr_Tartempion.pdf") == "Lettre_MEDECIN_1_PNEUMOLOGUE.pdf"

    @pytest.mark.parametrize("titre", [
        "Dr", "DR", "dr.", "Dre", "Dresse", "Drsse", "Doctoresse", "Docteur",
        "Pr", "Prof", "Professeur"])
    def test_tout_titre_est_absorbe(self, titre):
        out = anonymiser_nom(f"Pneumologie_{titre}_Negrel.pdf",
                             table=_table_du_lot())
        assert out == "Pneumologie_MEDECIN_1_PNEUMOLOGUE.pdf"

    def test_employeur_mal_etiquete_n_avale_pas_le_medecin(self):
        """Le cas reel : une detection « employeur » contenant la specialite
        et le nom faisait « EMPLOYEUR_17_PERSONNE_3 »."""
        t = _table_du_lot()
        t.pseudonyme("Pneumologie Dresse Negrel", "employeur")
        assert (anonymiser_nom("Avis_pneumologique_ancien_dossier_Dresse_Negrel.pdf",
                               table=t)
                == "Avis_pneumologique_ancien_dossier_MEDECIN_1_PNEUMOLOGUE.pdf")

    def test_mot_protege_jamais_remplace(self):
        corr = {"Avis Pneumologie Rapport": "EMPLOYEUR_1",
                "Pneumologie": "PERSONNE_2"}
        nom = "Avis_Pneumologie_Rapport.pdf"
        assert anonymiser_nom(nom, corr, personnes={"Pneumologie"}) == nom

    def test_pseudonymes_deja_poses_inchanges(self):
        t = _table_du_lot()
        for nom in ("Pneumologie_MEDECIN_1_PNEUMOLOGUE_2.pdf",
                    "PATIENT_Lettre.pdf", "Rapport_ETABLISSEMENT_1.pdf"):
            assert anonymiser_nom(nom, table=t) == nom

    def test_nom_colle_en_camelcase(self):
        assert (anonymiser_nom("AvisVeroniqueNegrel.pdf", table=_table_du_lot())
                == "Avis_MEDECIN_1_PNEUMOLOGUE.pdf")

    def test_homonyme_titre_du_patient_est_un_autre_medecin(self):
        """Seul le patient porte « Zorglub » ; « Dr_Zorglub » est un medecin
        homonyme, pas le patient."""
        t = _table_du_lot()
        out = anonymiser_nom("Cardiologie_Dr_Zorglub.pdf", table=t)
        assert out == "Cardiologie_MEDECIN_2_CARDIOLOGUE.pdf"
        assert anonymiser_nom("Zorglub_Lettre.pdf", table=t) == "PATIENT_Lettre.pdf"


class TestPatientDuDossier:
    @staticmethod
    def _table(*noms):
        from caviardoc.pseudonymes import TablePseudonymes
        t = TablePseudonymes()
        for nom, fois in noms:
            for _ in range(fois):
                t.pseudonyme(nom, "personne")
        return t

    def test_role_patient_des_documents(self):
        t = _table_du_lot()
        assert anonymiser_dossier("Zorglub Jean-Bidule", table=t) == "PATIENT"

    def test_personne_nommee_par_le_dossier(self):
        t = self._table(("Paul Tartempion", 1), ("Jean-Bidule Zorglub", 1))
        assert (anonymiser_nom("Lettre_Zorglub.pdf", table=t,
                               contexte=("Zorglub Jean-Bidule",))
                == "Lettre_PATIENT.pdf")
        assert (anonymiser_nom("Lettre_Tartempion.pdf", table=t,
                               contexte=("Zorglub Jean-Bidule",))
                == "Lettre_PERSONNE_1.pdf")

    def test_la_plus_citee_seulement_pour_un_dossier(self):
        t = self._table(("Paul Tartempion", 1), ("Jean-Bidule Zorglub", 3))
        assert (anonymiser_nom("Lettre_Zorglub.pdf", table=t, contexte=("Lot",))
                == "Lettre_PATIENT.pdf")
        # Sans dossier commun, rien ne dit qui est le patient du lot.
        assert anonymiser_nom("Lettre_Zorglub.pdf", table=t) == "Lettre_PERSONNE_2.pdf"

    def test_egalite_pas_de_patient(self):
        t = self._table(("Paul Tartempion", 2), ("Jean-Bidule Zorglub", 2))
        assert (anonymiser_nom("Lettre_Zorglub.pdf", table=t, contexte=("Lot",))
                == "Lettre_PERSONNE_2.pdf")

    def test_un_medecin_n_est_jamais_le_patient(self):
        from caviardoc.pseudonymes import TablePseudonymes
        t = TablePseudonymes()
        t.qualifier({"Dr Paul Tartempion": ("medecin", "")})
        for _ in range(3):
            t.pseudonyme("Dr Paul Tartempion", "personne")
        assert (anonymiser_nom("Lettre_Tartempion.pdf", table=t, contexte=("Lot",))
                == "Lettre_MEDECIN_1.pdf")


class TestRolesDesNoms:
    """Le titre lu dans un nom de fichier qualifie la personne avant le texte :
    le texte et le nom du fichier portent le meme pseudonyme."""

    def test_roles_lus_dans_les_noms(self):
        from caviardoc.noms import roles_des_noms
        roles = roles_des_noms(["Avis_pneumologique_ancien_dossier_Dresse_Negrel.pdf",
                                "Pneumologie_Drsse_Veronique_Negrel_2.pdf",
                                "Lettre_Dr_Martin.pdf", "Court_Cabinet.pdf"],
                               ("Avis_cardiologie",))
        assert roles == {"Negrel": ("medecin", "pneumologue"),
                         "Veronique Negrel": ("medecin", "pneumologue"),
                         "Martin": ("medecin", "cardiologue")}

    def test_le_texte_suit_le_nom_du_fichier(self):
        from caviardoc.noms import roles_des_noms
        from caviardoc.pseudonymes import TablePseudonymes
        t = TablePseudonymes()
        t.qualifier(roles_des_noms(["Avis_pneumologique_Dresse_Negrel.pdf"]),
                    faible=True)
        # Le document nomme « Mme V. Negrel » sans la qualifier.
        assert t.pseudonyme("Mme V. Negrel", "personne") == "MEDECIN_1_PNEUMOLOGUE"
        assert (anonymiser_nom("Avis_pneumologique_Dresse_Negrel.pdf", table=t)
                == "Avis_pneumologique_MEDECIN_1_PNEUMOLOGUE.pdf")
        # Un autre porteur du patronyme n'est pas concerne.
        assert t.pseudonyme("Paul Negrel", "personne") == "PERSONNE_1"

    def test_le_role_des_documents_prime(self):
        from caviardoc.pseudonymes import TablePseudonymes
        t = TablePseudonymes()
        t.qualifier({"Negrel": ("medecin", "pneumologue"),
                     "Martin": ("medecin", "cardiologue")}, faible=True)
        t.qualifier({"Dresse Negrel": ("medecin", ""),
                     "Jean Martin": ("patient", "")})
        # Medecin sans specialite dans le document : celle du nom s'ajoute.
        assert t.pseudonyme("Dresse Negrel", "personne") == "MEDECIN_1_PNEUMOLOGUE"
        assert t.pseudonyme("Jean Martin", "personne") == "PATIENT_1"


class TestCollisions:
    @staticmethod
    def _plan(tmp_path, *noms, txt=()):
        from caviardoc.noms import AnonymiseurNoms, planifier_renommages
        documents = []
        for provisoire, origine in noms:
            (tmp_path / provisoire).write_text(provisoire)
            documents.append((tmp_path / provisoire, origine))
        for nom in txt:
            (tmp_path / nom).write_text(nom)
        return {s.name: f.name for s, f in planifier_renommages(
            documents, AnonymiseurNoms({"Negrel": "MEDECIN_1"},
                                       personnes={"Negrel"}))}

    def test_fichier_existant_jamais_vise(self, tmp_path):
        (tmp_path / "Avis_MEDECIN_1.pdf").write_text("ancien")
        (tmp_path / "Avis_MEDECIN_1_2.pdf").write_text("ancien")
        assert self._plan(tmp_path, ("Avis_Negrel.pdf", "Avis_Negrel.pdf")) == {
            "Avis_Negrel.pdf": "Avis_MEDECIN_1_3.pdf"}

    def test_le_texte_suit_son_document(self, tmp_path):
        plan = self._plan(tmp_path, ("Avis_Dr_Negrel.pdf", "Avis_Dr_Negrel.pdf"),
                          ("Avis_Negrel.pdf", "Avis_Negrel.pdf"),
                          txt=("Avis_Negrel.txt",))
        assert plan == {"Avis_Dr_Negrel.pdf": "Avis_MEDECIN_1.pdf",
                        "Avis_Negrel.pdf": "Avis_MEDECIN_1_2.pdf",
                        "Avis_Negrel.txt": "Avis_MEDECIN_1_2.txt"}

    def test_nom_provisoire_entre_parentheses_oublie(self, tmp_path):
        """« Lettre (2).docx » n'est qu'un nom provisoire : le nom final part
        de l'original, et la collision se numerote « _2 »."""
        plan = self._plan(tmp_path, ("Lettre.pdf", "Lettre.pdf"),
                          ("Lettre (2).docx", "Lettre.docx"),
                          txt=("Lettre.txt", "Lettre (2).txt"))
        assert plan == {"Lettre (2).docx": "Lettre_2.docx",
                        "Lettre (2).txt": "Lettre_2.txt"}

    def test_la_casse_ne_distingue_pas(self, tmp_path):
        plan = self._plan(tmp_path, ("avis_Negrel.pdf", "avis_Negrel.pdf"),
                          ("Avis_Negrel_x.pdf", "Avis_Negrel.pdf"))
        assert plan == {"Avis_Negrel_x.pdf": "Avis_MEDECIN_1.pdf",
                        "avis_Negrel.pdf": "avis_MEDECIN_1_2.pdf"}


class TestPersonneContreProtection:
    """Un mot protege (Court, Long, Garde, Page, Famille) qui est le nom d'une
    personne de la table est remplace : l'identite l'emporte."""

    @staticmethod
    def _table():
        from caviardoc.engine import Span
        from caviardoc.pseudonymes import TablePseudonymes
        t = TablePseudonymes()
        t.enregistrer([Span(0, 1, "personne", 0.9, x) for x in (
            "Jean Court", "Marie Long", "Paul Garde", "Anne Page",
            "Luc Famille")])
        return t

    @pytest.mark.parametrize("nom", [
        "Rapport_Jean_Court", "Marie_Long_bilan", "Paul_Garde",
        "Anne_Page_CR", "Luc_Famille"])
    def test_le_nom_protege_d_une_personne_est_remplace(self, nom):
        sortie = anonymiser_nom(nom + ".pdf", table=self._table())
        for mot in ("Court", "Long", "Garde", "Page", "Famille"):
            assert mot not in sortie, sortie
        assert "PERSONNE_" in sortie

    def test_le_mot_protege_sans_personne_reste(self):
        t = _table_du_lot()
        assert anonymiser_nom("Court_Cabinet.pdf", table=t) == "Court_Cabinet.pdf"
        assert (anonymiser_nom("Rapport_Court_bilan.pdf", table=t)
                == "Rapport_Court_bilan.pdf")

    def test_specialite_reste_claire(self):
        assert "Pneumologie" in anonymiser_nom(
            "Pneumologie_Dr_Negrel.pdf", table=_table_du_lot())
