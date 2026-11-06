"""Audit vague 3, voie I : mise a jour interrompue, mesure echouee, garde.

Aucun reseau, aucun git reel : archives fabriquees, `_github` simule,
repertoire de donnees dans tmp.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from caviardoc import maj, paths  # noqa: E402
from caviardoc.maj import MajImpossible  # noqa: E402
from test_maj import (ANCIEN, NOUVEAU, _github_simule, _version,  # noqa: E402,F401
                      _zip, copie_zip)

MARQUEUR = "programme/src/caviardoc/version_archive.py"


def _nouvelle_archive():
    # Le marqueur en tete : l'ordre le plus defavorable.
    return _zip({
        MARQUEUR: _version(NOUVEAU),
        "programme/pyproject.toml": "[project] # nouveau",
        "programme/src/caviardoc/__init__.py": "",
        "programme/src/caviardoc/ajoute.py": "nouveau module",
        "programme/src/caviardoc/zeta.py": "dernier",
    })


def test_archive_echec_en_route_garde_la_version_ancienne(copie_zip, monkeypatch):
    monkeypatch.setattr(maj, "_github", _github_simule(
        NOUVEAU, archive=_nouvelle_archive()))
    vrai = maj.os.replace

    def panne(src, dst):
        if Path(dst).name == "zeta.py":
            raise PermissionError("tenu")
        return vrai(src, dst)

    monkeypatch.setattr(maj.os, "replace", panne)
    monkeypatch.setattr(maj.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        maj.appliquer_code()
    # Le marqueur n'a pas bouge : la mise a jour peut etre relancee.
    assert maj.commit_archive()[0] == ANCIEN


@pytest.fixture
def donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))
    monkeypatch.setattr(maj.paths, "uv_exe", lambda: None)
    return tmp_path


def _faux_models(monkeypatch):
    from caviardoc import models
    monkeypatch.setattr(models, "check",
                        lambda spec: SimpleNamespace(complete=True))


def test_second_temps_echoue_puis_reprise(copie_zip, monkeypatch):
    """Une mesure en panne ne laisse pas la machine « a jour »."""
    monkeypatch.setattr(maj.paths, "uv_exe", lambda: None)
    _faux_models(monkeypatch)
    github = _github_simule(NOUVEAU, archive=_nouvelle_archive())
    monkeypatch.setattr(maj, "_github", github)

    compte = maj.appliquer_code()
    maj.noter_attente(compte)
    assert maj.reprendre_attente() == compte      # consomme, comme avant

    monkeypatch.setattr(maj, "mesurer", lambda journal=None: None)
    with pytest.raises(MajImpossible, match="mesure"):
        maj.appliquer_suite(compte, remesurer=True)

    # Le code est a jour, mais une nouvelle demande reprend au lieu de
    # repondre « deja a jour ».
    monkeypatch.setattr(maj, "verifier", lambda *a, **k: SimpleNamespace(
        empeche=None, disponible=False,
        locale=SimpleNamespace(reference=NOUVEAU[:7])))
    reprise = maj.appliquer_code()
    assert reprise["applique"] is True

    monkeypatch.setattr(maj, "mesurer", lambda journal=None: 96.0)
    fini = maj.appliquer_suite(reprise, remesurer=True)
    assert fini["rappel_apres"] == 96.0
    assert maj.appliquer_code()["applique"] is False   # acheve : plus rien


def test_mesure_plantee_nest_pas_un_succes(donnees, monkeypatch):
    _faux_models(monkeypatch)
    racine = donnees / "p"
    (racine / "tools").mkdir(parents=True)
    (racine / "tools" / "mesurer.py").write_text("")
    py = donnees / "py"
    py.write_text("")
    monkeypatch.setattr(maj.paths, "project_root", lambda: racine)
    from caviardoc import doctor
    monkeypatch.setattr(doctor, "_venv_python", lambda d: py)
    monkeypatch.setattr(maj.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="", stderr="Traceback: boom"))
    lignes = []
    assert maj.mesurer(journal=lignes.append) is None
    assert any("boom" in l for l in lignes)
    with pytest.raises(MajImpossible, match="mesure"):
        maj.appliquer_suite({"applique": True, "rappel_avant": 95.0},
                            remesurer=True)


def test_la_mesure_guidee_enregistre_son_resultat():
    import inspect
    from caviardoc import installateur
    assert "--enregistrer" in inspect.getsource(installateur)


@pytest.mark.skipif(sys.platform == "win32", reason="pas de SIGHUP sous Windows")
def test_servir_gere_sighup(monkeypatch, tmp_path):
    """SIGHUP arrete `servir` par le meme chemin que SIGTERM : arret du
    serveur et effacement des fichiers de travail."""
    import signal
    import threading
    from caviardoc import interface

    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))
    evenements = []

    class FauxServeur:
        server_port = 1

        def __init__(self, *a, **k):
            pass

        def serve_forever(self):
            pass

        def shutdown(self):
            evenements.append("shutdown")

        def server_close(self):
            pass

    gestionnaires = {}

    def faux_signal(numero, gestionnaire):
        gestionnaires[numero] = gestionnaire
        if numero == signal.SIGHUP and callable(gestionnaire):
            # Le signal arrive pendant l'attente, depuis un autre fil.
            threading.Timer(0.05, gestionnaire, args=(numero, None)).start()
        return signal.SIG_DFL

    monkeypatch.setattr(interface, "ServeurLocal", FauxServeur)
    monkeypatch.setattr(interface.signal, "signal", faux_signal)
    racines = []
    monkeypatch.setattr(interface.shutil, "rmtree",
                        lambda chemin, *a, **k: racines.append(chemin))

    def attendre():
        interface.servir(ouvrir=False, pret=lambda adresse: None)

    fil = threading.Thread(target=attendre, daemon=True)
    fil.start()
    fil.join(10)

    assert not fil.is_alive(), "SIGHUP n'a pas arrete servir"
    assert signal.SIGHUP in gestionnaires
    assert evenements == ["shutdown"]
    assert racines, "les fichiers de travail n'ont pas ete effaces"
