"""Liens vers les notes de version (releases GitHub) dans l'etat de mise a jour."""
import json

import pytest

from caviardoc import __version__, maj

URL = f"https://github.com/{maj.DEPOT_GITHUB}/releases/tag/v9.9.9"


def _release(monkeypatch, tag="v9.9.9", url=URL, erreur=None):
    def github(chemin, delai=30, introuvable=""):
        assert chemin == "releases/latest"
        if erreur:
            raise erreur
        return json.dumps({"tag_name": tag, "html_url": url}).encode()
    monkeypatch.setattr(maj, "_github", github)


def _etat(disponible=True):
    return maj.Etat(locale=maj.Revision(reference="abc1234"),
                    retard=3 if disponible else 0)


def test_lien_present_si_release_plus_recente(monkeypatch):
    _release(monkeypatch)
    vue = maj.vue_avec_notes(_etat())
    assert vue["notes"] == {"version": "9.9.9", "url": URL}


def test_comparaison_numerique_et_non_textuelle(monkeypatch):
    _release(monkeypatch, tag="v0.10.0",
             url=f"{maj.PREFIXE_NOTES}tag/v0.10.0")
    monkeypatch.setattr("caviardoc.__version__", "0.9.0")
    assert maj.vue_avec_notes(_etat())["notes"]["version"] == "0.10.0"


@pytest.mark.parametrize("tag", [f"v{__version__}", "v0.0.1", "nightly", ""])
def test_pas_de_lien_si_meme_ou_ancienne_version(monkeypatch, tag):
    _release(monkeypatch, tag=tag, url=f"{maj.PREFIXE_NOTES}tag/{tag}")
    assert "notes" not in maj.vue_avec_notes(_etat())


@pytest.mark.parametrize("url", [
    "http://github.com/dobunzli/caviardoc/releases/tag/v9.9.9",
    "https://evil.example/https://github.com/dobunzli/caviardoc/releases/",
    "https://github.com/autre/depot/releases/tag/v9.9.9",
    "javascript:alert(1)", ""])
def test_pas_de_lien_si_url_hors_du_depot(monkeypatch, url):
    _release(monkeypatch, url=url)
    assert "notes" not in maj.vue_avec_notes(_etat())


@pytest.mark.parametrize("erreur", [maj.MajImpossible("x"), OSError("x"),
                                    ValueError("x")])
def test_erreur_de_l_api_ne_casse_rien(monkeypatch, erreur):
    _release(monkeypatch, erreur=erreur)
    vue = maj.vue_avec_notes(_etat())
    assert "notes" not in vue and vue["disponible"]


def test_reponse_illisible(monkeypatch):
    monkeypatch.setattr(maj, "_github", lambda *a, **k: b"pas du json")
    assert "notes" not in maj.vue_avec_notes(_etat())


def test_pas_de_requete_sans_mise_a_jour(monkeypatch):
    def github(*a, **k):
        raise AssertionError("pas de requete")
    monkeypatch.setattr(maj, "_github", github)
    assert "notes" not in maj.vue_avec_notes(_etat(disponible=False))


def test_notes_de_la_version_installee_sans_reseau(monkeypatch):
    monkeypatch.setattr(maj, "_github", lambda *a, **k: 1 / 0)
    n = maj.vue_avec_notes(_etat(disponible=False))["notes_installee"]
    assert n == {"version": __version__,
                 "url": f"{maj.PREFIXE_NOTES}tag/v{__version__}"}
