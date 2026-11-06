"""Un second lancement rouvre la page deja ouverte, plutot qu'une seconde."""

from __future__ import annotations

import threading
import urllib.parse

import pytest

from caviardoc import cli, installateur, interface, web_commun


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(installateur, "constater_en_fond", lambda inst: None)
    adresse: dict = {}
    prete, arret = threading.Event(), threading.Event()

    def pret(url):
        adresse["url"] = url
        prete.set()

    fil = threading.Thread(target=lambda: interface.servir(
        ouvrir=False, pret=pret, arret=arret, console=True), daemon=True)
    fil.start()
    assert prete.wait(5)
    yield adresse["url"], arret, fil
    arret.set()
    fil.join(timeout=10)


def test_la_page_ouverte_est_retrouvee(console):
    url, _, _ = console
    trouvee = web_commun.session_ouverte()
    assert trouvee is not None
    assert (urllib.parse.urlparse(trouvee).port
            == urllib.parse.urlparse(url).port)


def test_une_page_fermee_ne_l_est_plus(console):
    _, arret, fil = console
    arret.set()
    fil.join(timeout=10)
    assert web_commun.session_ouverte() is None


def test_une_note_perimee_ne_trompe_pas(tmp_path, monkeypatch):
    """Le port a pu etre repris par un autre programme, ou la page tuee."""
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    web_commun.noter_session(9, "jeton-perime")
    assert web_commun.session_ouverte() is None


def test_le_second_lancement_rouvre_la_page(console, monkeypatch):
    ouvertes = []
    monkeypatch.setattr("webbrowser.open", ouvertes.append)
    monkeypatch.setattr(cli, "_relais_optionnel", lambda argv: pytest.fail(
        "un second serveur a ete demarre"))
    assert cli.main(["console"]) == 0
    assert ouvertes and "jeton=" in ouvertes[0]


def test_vivant_exige_le_jeton(console):
    import urllib.error
    import urllib.request
    url, _, _ = console
    base = url.split("/?")[0]
    with pytest.raises(urllib.error.HTTPError) as refus:
        urllib.request.urlopen(base + "/vivant", timeout=5)
    assert refus.value.code == 403
