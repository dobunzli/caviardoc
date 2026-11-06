"""Le maillon entre « mesurer » et « faire confiance ».

Une machine pouvait pointer vers un modele qui n'y avait jamais ete mesure, et
rien ne le signalait : le chiffre affiche par la documentation n'engageait
qu'elle. `--enregistrer` ferme la boucle — un modele ne devient celui de la
machine que s'il y a tenu ses seuils.

L'asymetrie est la meme que partout dans ce projet : un echec s'enregistre
comme un succes, parce que c'est le cas le plus utile a conserver. Ce que
l'echec n'autorise pas, c'est de retenir le modele.
"""

from __future__ import annotations

import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))
sys.path.insert(0, str(RACINE / "tools"))

import pytest  # noqa: E402

from caviardoc import doctor, models  # noqa: E402
from caviardoc.doctor import Level  # noqa: E402

TENUES = {"rappel_complet": 0.973, "rappel_patient": 0.950,
          "preservation_complet": 0.971, "preservation_patient": 0.971,
          "soignants_preserves": 1.0}
BASSES = {"rappel_complet": 0.622, "rappel_patient": 0.600,
          "preservation_complet": 0.971, "preservation_patient": 0.971,
          "soignants_preserves": 0.786}

AUTRE = next(k for k in models.REGISTRY if k != models.DEFAULT_MODEL)


@pytest.fixture
def donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    return tmp_path


class TestReleve:
    def test_aller_retour(self, donnees):
        assert models.derniere_mesure() is None
        models.noter_mesure(models.DEFAULT_MODEL, TENUES, tenu=True)

        mesure = models.derniere_mesure()
        assert mesure["modele"] == models.DEFAULT_MODEL
        assert mesure["tenu"] is True
        assert mesure["rappel_complet"] == 97.3
        assert mesure["date"]

    def test_un_echec_est_conserve(self, donnees):
        """C'est le cas le plus utile a garder : celui ou il ne faut rien
        confier a la machine."""
        models.noter_mesure(models.DEFAULT_MODEL, BASSES, tenu=False)
        mesure = models.derniere_mesure()
        assert mesure["tenu"] is False
        assert mesure["rappel_complet"] == 62.2

    def test_le_releve_neffacer_pas_le_modele_retenu(self, donnees):
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(models.DEFAULT_MODEL, TENUES, tenu=True)
        assert models.modele_actif() == models.DEFAULT_MODEL

    def test_une_mesure_sur_un_autre_modele_ne_compte_pas(self, donnees):
        """Mesurer int8 n'autorise rien a dire sur fp16."""
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(AUTRE, TENUES, tenu=True)

        assert models.derniere_mesure() is not None
        assert models.mesure_du_modele_actif() is None

    def test_un_etat_abime_ne_bloque_pas(self, donnees):
        from caviardoc import paths
        paths.ensure_dirs()
        paths.state_file().write_text("{pas du json", encoding="utf-8")
        assert models.derniere_mesure() is None


class TestDoctor:
    def test_jamais_mesuree(self, donnees):
        ch = doctor.check_mesure()
        assert ch.level is Level.WARN
        assert "jamais faite" in ch.detail
        assert "--enregistrer" in ch.fix

    def test_mesure_tenue(self, donnees):
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(models.DEFAULT_MODEL, TENUES, tenu=True)

        ch = doctor.check_mesure()
        assert ch.level is Level.OK
        assert "95.0 %" in ch.detail, "le pire rappel, pas le meilleur"

    def test_mesure_sous_le_seuil_est_bloquante(self, donnees):
        """Un rappel insuffisant n'est pas un avertissement : c'est un arret."""
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(models.DEFAULT_MODEL, BASSES, tenu=False)

        ch = doctor.check_mesure()
        assert ch.level is Level.FAIL
        assert "60.0 %" in ch.detail
        assert "Ne confiez aucun document" in ch.fix

    def test_mesure_portant_sur_un_autre_modele(self, donnees):
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(AUTRE, TENUES, tenu=True)

        ch = doctor.check_mesure()
        assert ch.level is Level.WARN
        # Le message nomme les deux modeles : « faite sur X, pas sur Y ».
        # Savoir sur quoi la mesure porte vaut mieux que savoir qu'elle ne
        # porte pas sur le bon.
        assert AUTRE in ch.detail
        assert models.DEFAULT_MODEL in ch.detail

    def test_inspecter_un_autre_modele_que_celui_retenu(self, donnees):
        """`doctor --modele` ne doit pas afficher la mesure du modele retenu.

        Une mesure faite sur fp16 n'apprend rien sur int8. L'afficher au vert
        en inspectant int8 serait exactement le chiffre rassurant que ce
        controle existe pour eviter.
        """
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(models.DEFAULT_MODEL, TENUES, tenu=True)

        retenu = doctor.check_mesure(models.DEFAULT_MODEL)
        assert retenu.level is Level.OK

        autre = doctor.check_mesure(AUTRE)
        assert autre.level is Level.WARN
        assert AUTRE in autre.detail

    def test_mesure_du_modele_accepte_une_cle(self, donnees):
        models.definir_modele_actif(models.DEFAULT_MODEL)
        models.noter_mesure(models.DEFAULT_MODEL, TENUES, tenu=True)

        assert models.mesure_du_modele(models.DEFAULT_MODEL) is not None
        assert models.mesure_du_modele(AUTRE) is None
        assert models.mesure_du_modele() == models.mesure_du_modele_actif()

    def test_le_controle_figure_dans_le_diagnostic(self, donnees):
        assert "Mesure du rappel" in [c.name for c in doctor.run_all()]


class TestEnregistrer:
    """La decision elle-meme, sans relancer une mesure de vingt secondes."""

    def _enregistrer(self, mesures, sous, modele=None):
        import mesurer
        mesurer._modele_mesure = modele
        try:
            return mesurer._enregistrer(mesures, sous)
        finally:
            mesurer._modele_mesure = None

    def test_une_mesure_tenue_retient_le_modele(self, donnees, capsys):
        models.definir_modele_actif(AUTRE)

        code = self._enregistrer(TENUES, [], modele=models.DEFAULT_MODEL)

        assert code == 0
        assert models.modele_actif() == models.DEFAULT_MODEL
        assert models.derniere_mesure()["tenu"] is True
        assert "tous les seuils tenus" in capsys.readouterr().out

    def test_une_mesure_basse_ne_retient_rien(self, donnees, capsys):
        models.definir_modele_actif(models.DEFAULT_MODEL)

        code = self._enregistrer(BASSES, ["rappel_complet", "rappel_patient"],
                                 modele=AUTRE)

        assert code == 1
        assert models.modele_actif() == models.DEFAULT_MODEL, \
            "un modele qui echoue ne doit pas devenir celui de la machine"
        sortie = capsys.readouterr().out
        assert "n'est PAS retenu" in sortie
        assert AUTRE in sortie

    def test_une_mesure_basse_est_tout_de_meme_enregistree(self, donnees):
        self._enregistrer(BASSES, ["rappel_complet"], modele=AUTRE)
        mesure = models.derniere_mesure()
        assert mesure["modele"] == AUTRE
        assert mesure["tenu"] is False

    def test_le_modele_retenu_qui_echoue_est_signale_fortement(self, donnees,
                                                               capsys):
        """Le cas le plus grave : ce qui tourne ici ne tient plus ses seuils."""
        models.definir_modele_actif(models.DEFAULT_MODEL)

        self._enregistrer(BASSES, ["rappel_complet"],
                          modele=models.DEFAULT_MODEL)

        sortie = capsys.readouterr().out
        assert "ne confiez aucun document" in sortie.lower()

    def test_sans_drapeau_modele_cest_le_modele_actif_qui_est_mesure(
            self, donnees):
        models.definir_modele_actif(AUTRE)
        self._enregistrer(TENUES, [])
        assert models.derniere_mesure()["modele"] == AUTRE


def test_le_drapeau_dit_ce_quil_fait():
    """L'aide doit annoncer la condition, pas seulement l'effet."""
    source = (RACINE / "tools" / "mesurer.py").read_text(encoding="utf-8")
    assert '"--enregistrer"' in source
    assert "seulement s'il tient les seuils" in source
