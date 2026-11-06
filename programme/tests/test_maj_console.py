"""La mise a jour depuis la page : en fond, et en deux temps.

Le code d'abord, puis un redemarrage, puis les bibliotheques et la mesure dans
le nouveau processus. Sous Windows, une bibliotheque chargee ne se remplace
pas : `uv sync` echouait des que la mise a jour en changeait une.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from caviardoc import installateur, interface, maj, paths
from caviardoc.web_commun import ENTETE_JETON

COMPTE = {"applique": True, "de": "aaaaaaa", "vers": "bbbbbbb",
          "commits": 2, "rappel_avant": 95.0}


@pytest.fixture
def demarrer(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(installateur, "constater_en_fond", lambda inst: None)
    relances = []
    monkeypatch.setattr(
        interface.subprocess, "run",
        lambda cmd, **k: relances.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    # Sous Windows, la relance ne s'attend pas (voir `servir`) : Popen.
    monkeypatch.setattr(interface.subprocess, "Popen",
                        lambda cmd, **k: relances.append(cmd))
    fils = []

    def lancer():
        adresse: dict = {}
        prete, arret = threading.Event(), threading.Event()

        def pret(url):
            adresse["url"] = url
            prete.set()

        fil = threading.Thread(target=lambda: interface.servir(
            ouvrir=False, pret=pret, arret=arret, console=True), daemon=True)
        fil.start()
        fils.append((fil, arret))
        assert prete.wait(5)
        a = urllib.parse.urlparse(adresse["url"])
        return (f"{a.scheme}://{a.netloc}",
                urllib.parse.parse_qs(a.query)["jeton"][0], fil)

    yield lancer, relances
    for fil, arret in fils:
        arret.set()
        fil.join(timeout=10)


def _requete(base, jeton, chemin, charge=None):
    donnees = None if charge is None else json.dumps(charge).encode()
    req = urllib.request.Request(base + chemin, data=donnees,
                                 method="POST" if donnees else "GET",
                                 headers={ENTETE_JETON: jeton,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _attendre(condition, delai=10.0):
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_le_code_d_abord_puis_redemarrage(demarrer, monkeypatch):
    lancer, relances = demarrer
    monkeypatch.setattr(maj, "appliquer_code", lambda journal=None: dict(COMPTE))
    monkeypatch.setattr(maj, "appliquer_suite", lambda *a, **k: pytest.fail(
        "les bibliotheques ne se reinstallent pas dans l'ancien processus"))
    base, jeton, fil = lancer()

    assert _requete(base, jeton, "/maj", {}) == {"lance": True}
    fil.join(timeout=10)
    assert not fil.is_alive(), "le serveur ne s'est pas arrete pour redemarrer"
    assert any(cmd[2:5] == ["caviardoc", "console", "--port"] for cmd in relances)
    attente = json.loads((paths.data_dir() / maj.FICHIER_ATTENTE).read_text())
    assert attente["vers"] == "bbbbbbb"


def test_le_nouveau_processus_fait_le_second_temps(demarrer, monkeypatch):
    lancer, _ = demarrer
    maj.noter_attente(dict(COMPTE))
    libre = threading.Event()

    def suite(compte, journal=None, remesurer=True):
        journal("uv sync")
        libre.wait(10)
        return dict(compte, rappel_apres=96.0, regression=False)

    monkeypatch.setattr(maj, "appliquer_suite", suite)
    base, jeton, _ = lancer()
    assert not (paths.data_dir() / maj.FICHIER_ATTENTE).exists()

    vue = _requete(base, jeton, "/maj")["suivi"]
    assert vue["etat"] == "suite"
    # Pendant ce temps, aucun document ne part dans des bibliotheques a demi
    # installees.
    with pytest.raises(urllib.error.HTTPError) as refus:
        _requete(base, jeton, "/traiter", {"id": "x"})
    assert refus.value.code == 409

    libre.set()
    assert _attendre(lambda: _requete(base, jeton, "/maj").get("suivi", {})
                     .get("etat") == "fini" or False)


def test_deja_a_jour_ne_redemarre_pas(demarrer, monkeypatch):
    lancer, relances = demarrer
    monkeypatch.setattr(maj, "appliquer_code",
                        lambda journal=None: {"applique": False})
    base, jeton, fil = lancer()
    _requete(base, jeton, "/maj", {})
    assert _attendre(lambda: "dernière version" in json.dumps(
        _requete(base, jeton, "/maj"), ensure_ascii=False).lower())
    assert fil.is_alive()
    assert not any("console" in cmd for cmd in relances)
