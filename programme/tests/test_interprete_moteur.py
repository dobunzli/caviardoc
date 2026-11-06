"""L'interpreteur qui execute le moteur n'est pas celui qui lance la commande.

Les lanceurs demarrent `caviardoc` avec le Python d'amorcage : celui qu'ils ont
trouve ou installe, et qui n'a que la bibliotheque standard. C'est voulu — c'est
ce qui permet d'ouvrir l'installateur sur une machine ou il manque tout. Les
dependances, elles, vivent dans le venv.

Le defaut qui a motive ces tests : l'installateur servait l'interface dans son
propre processus. Tout s'affichait comme installe, le diagnostic etait vert, et
le premier document echouait sur « dependances du moteur absentes ». Le message
etait exact et designait le mauvais coupable : les dependances etaient
installees, dans un interpreteur que personne n'utilisait.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import cli, doctor, installateur, paths  # noqa: E402


# --- Qui peut charger le moteur -------------------------------------------

def test_l_interpreteur_courant_gagne_s_il_a_le_moteur(monkeypatch):
    monkeypatch.setattr(doctor, "_module_present",
                        lambda module, python=None: python is None)
    assert doctor.interprete_du_moteur() == Path(sys.executable)


def test_a_defaut_on_prend_celui_du_venv(monkeypatch, tmp_path):
    faux_venv = tmp_path / "venv"
    monkeypatch.setattr(paths, "venv_dir", lambda: faux_venv)
    monkeypatch.setattr(doctor, "_module_present",
                        lambda module, python=None: python is not None)

    trouve = doctor.interprete_du_moteur()
    assert trouve == doctor._venv_python(faux_venv)
    assert trouve != Path(sys.executable)


def test_sans_moteur_nulle_part_on_le_dit(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "venv_dir", lambda: tmp_path / "venv")
    monkeypatch.setattr(doctor, "_module_present",
                        lambda module, python=None: False)
    assert doctor.interprete_du_moteur() is None


def test_la_presence_se_verifie_sans_importer(monkeypatch):
    """Importer gliner charge torch : plusieurs secondes a chaque commande."""
    assert doctor._module_present("json")
    assert not doctor._module_present("module_qui_n_existe_pas_du_tout")


# --- Les commandes qui en ont besoin le declarent -------------------------

@pytest.mark.parametrize("commande", ["interface", "caviarder", "selftest"])
def test_les_commandes_du_moteur_sont_marquees(commande):
    extra = ["fichier.pdf", "-o", "sortie"] if commande == "caviarder" else []
    args = cli.build_parser().parse_args([commande] + extra)
    assert getattr(args, "besoin_moteur", False), commande


@pytest.mark.parametrize("commande", ["doctor", "info", "installer", "setup"])
def test_les_autres_ne_le_sont_pas(commande):
    """Elles doivent tourner sur un Python nu : c'est leur raison d'etre."""
    args = cli.build_parser().parse_args([commande])
    assert not getattr(args, "besoin_moteur", False), commande


# --- Le relais -------------------------------------------------------------

def test_aucun_relais_quand_on_est_deja_au_bon_endroit(monkeypatch):
    monkeypatch.setattr(doctor, "interprete_du_moteur",
                        lambda: Path(sys.executable))
    assert cli._assurer_interprete_du_moteur(["selftest"]) is None


def test_le_relais_rejoue_la_commande_dans_l_autre_interpreteur(monkeypatch):
    autre = Path("/un/autre/python")
    monkeypatch.setattr(doctor, "interprete_du_moteur", lambda: autre)
    vus = {}

    def faux_run(cmd, *a, **k):
        vus["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 3)

    monkeypatch.setattr(cli.subprocess, "run", faux_run)
    assert cli._assurer_interprete_du_moteur(["caviarder", "a.pdf"]) == 3
    assert vus["cmd"] == [str(autre), "-m", "caviardoc", "caviarder", "a.pdf"]


def test_sans_interpreteur_on_renvoie_a_l_installation(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "interprete_du_moteur", lambda: None)
    assert cli._assurer_interprete_du_moteur(["selftest"]) == 1
    assert "caviardoc setup" in capsys.readouterr().out


# --- L'installateur --------------------------------------------------------

def test_l_installateur_demarre_l_interface_dans_le_bon_interpreteur(monkeypatch):
    """Le defaut d'origine : il la servait dans son propre processus."""
    autre = Path("/un/autre/python")
    monkeypatch.setattr(doctor, "interprete_du_moteur", lambda: autre)
    vus = {}

    class FauxProcessus:
        def __init__(self, cmd, **k):
            vus["cmd"] = cmd
            self.stdout = iter(
                ["Caviardoc\n",
                 "  Interface ouverte sur http://127.0.0.1:5555/?jeton=x\n"])

        def terminate(self): pass

    monkeypatch.setattr(installateur.subprocess, "Popen", FauxProcessus)

    adresse = {"url": ""}
    erreur, proc = installateur.demarrer_interface(adresse)
    assert erreur == ""
    assert adresse["url"] == "http://127.0.0.1:5555/?jeton=x"
    assert proc is not None, "le processus doit etre suivi, pour etre arrete"
    assert vus["cmd"][0] == str(autre)
    assert "-u" in vus["cmd"], "sans -u, l'adresse reste dans le tampon"


def test_l_installateur_refuse_proprement_sans_moteur(monkeypatch):
    monkeypatch.setattr(doctor, "interprete_du_moteur", lambda: None)
    erreur, proc = installateur.demarrer_interface({"url": ""})
    assert "moteur" in erreur
    assert proc is None
