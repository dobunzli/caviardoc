"""Anonymisation du nom de fichier : un fichier bien caviarde mais mal nomme fuit."""

import os
import sys
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
