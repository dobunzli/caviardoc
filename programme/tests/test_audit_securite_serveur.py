"""Audit de securite : liste « toujours », preuve du serveur, durcissement.

Donnees synthetiques, boucle locale uniquement, repertoire de donnees isole.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from caviardoc import exceptions, interface, web_commun
from caviardoc.interface import Atelier, Production
from caviardoc.web_commun import ENTETE_JETON, PoigneeBase, ServeurLocal


@pytest.fixture(autouse=True)
def donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))


# --- A. liste « toujours » ---------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="modes POSIX")
def test_le_fichier_d_exceptions_est_en_0600():
    r = exceptions.Repertoire.charger()
    r.retenir_toujours("Zorglub Machin")
    chemin = r.enregistrer()
    assert stat.S_IMODE(chemin.stat().st_mode) == 0o600
    # Une seconde ecriture, avec un .tmp laisse par un arret : meme mode.
    chemin.with_name(chemin.name + ".tmp").write_text("x")
    r.enregistrer()
    assert stat.S_IMODE(chemin.stat().st_mode) == 0o600


def test_les_anciens_champs_se_chargent_puis_disparaissent():
    r = exceptions.Repertoire.charger()
    r.retenir_toujours("Zorglub Machin")
    chemin = r.enregistrer()
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    brut["toujours"][0].update(vues=7, derniere="2025-01-02")
    chemin.write_text(json.dumps(brut), encoding="utf-8")

    r2 = exceptions.Repertoire.charger()
    assert len(r2.toujours) == 1
    assert r2.jamais_masquer("x") is False
    assert "vues" not in r2.vue()["toujours"][0]
    r2.enregistrer()
    nouveau = json.loads(chemin.read_text(encoding="utf-8"))
    assert set(nouveau["toujours"][0]) == {"empreinte", "mots", "etiquette"}


# --- C1. jeton_admis ---------------------------------------------------------
def test_jeton_admis_ne_leve_jamais():
    class P(PoigneeBase):
        jeton = "secret-é"

    p = P.__new__(P)
    assert p.jeton_admis("secret-é") is True
    assert p.jeton_admis("secret-e") is False
    assert p.jeton_admis("jetón-ünicode") is False
    assert p.jeton_admis("\ud800") is False        # substitut isole
    assert p.jeton_admis(None) is False            # type: ignore[arg-type]


# --- C2. delai et plafond de fils --------------------------------------------
def test_delai_et_plafond_sont_configures():
    assert PoigneeBase.timeout == 30
    assert ServeurLocal.fils_max == 16
    assert ServeurLocal.delai_connexion == 30.0


def test_les_connexions_en_trop_sont_refusees_poliment():
    arret = threading.Event()

    class Lente(PoigneeBase):
        def do_GET(self):
            arret.wait(5)
            self.repondre(200, b"ok")

    class Petit(ServeurLocal):
        fils_max = 2

    serveur = Petit(("127.0.0.1", 0), Lente)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    port = serveur.server_port
    ouvertes = []
    try:
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            ouvertes.append(s)
        # Laisser les deux fils prendre leur place.
        for _ in range(100):
            if serveur._places._value == 0:
                break
            threading.Event().wait(0.02)
        extra = socket.create_connection(("127.0.0.1", port), timeout=3)
        assert extra.recv(100).startswith(b"HTTP/1.0 503")
        extra.close()
        arret.set()
        for s in ouvertes:
            assert b"200" in s.recv(100)
        for _ in range(100):
            if serveur._places._value == 2:
                break
            threading.Event().wait(0.02)
        assert serveur._places._value == 2        # places rendues
    finally:
        arret.set()
        for s in ouvertes:
            s.close()
        serveur.shutdown()
        serveur.server_close()


# --- B. preuve du serveur ----------------------------------------------------
def _ecrire_session(port, jeton):
    web_commun.noter_session(port, jeton)


def _faux_serveur(reponse_preuve, vus):
    """Faux serveur ; ``reponse_preuve`` recoit le chemin demande."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            vus.append((self.path, self.headers.get(ENTETE_JETON)))
            corps = (reponse_preuve(self.path) if self.path.startswith("/preuve")
                     else web_commun.SIGNE_VIVANT)
            self.send_response(200)
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.mark.parametrize("reponse", [
    lambda chemin: web_commun.SIGNE_VIVANT,             # l'ancienne constante
    lambda chemin: b"0" * 64,                           # mauvaise preuve
    lambda chemin: b"",
])
def test_un_faux_serveur_ne_recoit_jamais_le_jeton(reponse):
    vus: list = []
    faux = _faux_serveur(reponse, vus)
    try:
        _ecrire_session(faux.server_port, "jeton-secret")
        assert web_commun.session_ouverte() is None
    finally:
        faux.shutdown()
        faux.server_close()
    assert vus, "le defi aurait du etre envoye"
    assert all(jeton is None for _, jeton in vus)
    assert all("jeton-secret" not in chemin for chemin, _ in vus)


def test_la_bonne_preuve_permet_d_envoyer_le_jeton():
    vus: list = []
    port_faux = 0

    def juste(chemin):
        defi = urllib.parse.parse_qs(
            urllib.parse.urlparse(chemin).query)["defi"][0]
        return web_commun.preuve_du_jeton("jeton-secret", defi, port_faux).encode()

    faux = _faux_serveur(juste, vus)
    port_faux = faux.server_port
    try:
        _ecrire_session(faux.server_port, "jeton-secret")
        adresse = web_commun.session_ouverte()
    finally:
        faux.shutdown()
        faux.server_close()
    assert adresse == f"http://127.0.0.1:{faux.server_port}/?jeton=jeton-secret"
    assert vus[0][1] is None                      # defi sans jeton
    assert vus[-1] == ("/vivant", "jeton-secret")  # jeton apres la preuve


# --- C3. telechargement sans confirmation -----------------------------------------------------
@pytest.fixture
def console(monkeypatch, tmp_path):
    ateliers: list = []
    vrai = interface.Atelier

    def fabrique(*a, **k):
        ateliers.append(vrai(*a, **k))
        return ateliers[-1]

    monkeypatch.setattr(interface, "Atelier", fabrique)
    adresse: dict = {}
    prete, arret = threading.Event(), threading.Event()

    def pret(url):
        adresse["url"] = url
        prete.set()

    fil = threading.Thread(target=lambda: interface.servir(
        ouvrir=False, pret=pret, arret=arret), daemon=True)
    fil.start()
    assert prete.wait(5)
    u = urllib.parse.urlparse(adresse["url"])
    base = f"{u.scheme}://{u.netloc}"
    jeton = urllib.parse.parse_qs(u.query)["jeton"][0]
    atelier = ateliers[0]
    for ident in ("lu", "nonlu"):
        f = tmp_path / f"{ident}.txt"
        f.write_text("contenu synthetique", encoding="utf-8")
        atelier.productions[ident] = Production(ident, f.name, f)
    yield base, jeton
    arret.set()
    fil.join(timeout=10)


def _get(base, jeton, chemin):
    req = urllib.request.Request(base + chemin, headers={ENTETE_JETON: jeton})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _archive(base, jeton, charge):
    req = urllib.request.Request(
        base + "/archive", data=json.dumps(charge).encode(), method="POST",
        headers={ENTETE_JETON: jeton, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_la_preuve_est_servie_sans_jeton_et_correcte(console):
    base, jeton = console
    port = urllib.parse.urlparse(base).port
    with urllib.request.urlopen(base + "/preuve?defi=abc", timeout=5) as r:
        assert r.read().decode() == web_commun.preuve_du_jeton(jeton, "abc", port)


def test_telecharger_ne_demande_aucune_confirmation(console):
    base, jeton = console
    assert _get(base, jeton, "/telecharger?id=nonlu")[0] == 200
    assert _get(base, jeton, "/telecharger?id=lu")[0] == 200


def test_archive_ne_demande_aucune_confirmation(console):
    base, jeton = console
    assert _archive(base, jeton, {"ids": ["lu", "nonlu"]})[0] == 200


def test_une_preuve_relayee_depuis_une_autre_console_echoue(console, capsys):
    """Un intrus sur le port B relaie le defi vers la vraie console (port A)."""
    base_a, jeton = console
    vus: list = []

    def relais(chemin):
        with urllib.request.urlopen(base_a + chemin, timeout=5) as r:
            return r.read()

    faux = _faux_serveur(relais, vus)
    try:
        _ecrire_session(faux.server_port, jeton)
        assert web_commun.session_ouverte() is None
    finally:
        faux.shutdown()
        faux.server_close()
    assert vus and all(j is None for _, j in vus)
    assert "ne s'est pas authentifiée" in capsys.readouterr().err


def test_un_port_ferme_reste_silencieux(capsys):
    _ecrire_session(9, "jeton-perime")
    assert web_commun.session_ouverte() is None
    assert capsys.readouterr().err == ""
