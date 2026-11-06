"""Au demarrage, l'installateur coche ce qui est deja en place.

Les etapes ne vivent qu'en memoire : sans constat, une machine entierement
installee montrait a chaque lancement six cercles vides, et le seul moyen de
les cocher etait de tout relancer.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import installateur, models  # noqa: E402


@pytest.fixture
def constats(monkeypatch):
    """Chaque constat renvoie ce que le test lui fait renvoyer."""
    reponses = {cle: None for cle in installateur.CONSTATS}
    for cle in reponses:
        monkeypatch.setitem(installateur.CONSTATS, cle,
                            lambda cle=cle: reponses[cle])
    return reponses


def _etats(installation):
    return {e.cle: e.etat for e in installation.etapes}


def test_ce_qui_est_en_place_est_coche(constats):
    for cle in constats:
        constats[cle] = ("ok", f"{cle} en place")
    installation = installateur.etapes_par_defaut()
    installateur.constater(installation)
    assert set(_etats(installation).values()) == {"ok"}
    vue = installation.vue()
    assert vue["faites"] == vue["total"] and vue["pret"]
    assert installation.par_cle("modele").message == "modele en place"


def test_ce_qui_manque_reste_a_faire_et_non_en_echec(constats):
    """Une etape jamais tentee n'a pas echoue."""
    constats["python"] = ("ok", "Python 3.11")
    installation = installateur.etapes_par_defaut()
    installateur.constater(installation)
    etats = _etats(installation)
    assert etats["python"] == "ok"
    assert all(etats[c] == "en_attente" for c in etats if c != "python")


def test_un_constat_qui_plante_ne_bloque_pas_les_autres(constats, monkeypatch):
    def casse():
        raise OSError("disque illisible")

    monkeypatch.setitem(installateur.CONSTATS, "tesseract", casse)
    constats["modele"] = ("ok", "present")
    installation = installateur.etapes_par_defaut()
    installateur.constater(installation)
    assert installation.par_cle("tesseract").etat == "en_attente"
    assert "disque illisible" in "\n".join(installation.par_cle("tesseract").journal)
    assert installation.par_cle("modele").etat == "ok"
    assert installation.en_cours == ""


def test_une_etape_deja_jouee_n_est_pas_ecrasee(constats):
    constats["dependances"] = ("ok", "constat")
    installation = installateur.etapes_par_defaut()
    etape = installation.par_cle("dependances")
    etape.etat, etape.message = "echec", "uv sync a échoué"
    installateur.constater(installation)
    assert etape.etat == "echec" and etape.message == "uv sync a échoué"


class TestMesure:
    def _mesure(self, monkeypatch, releve):
        monkeypatch.setattr(models, "derniere_mesure", lambda: releve)
        monkeypatch.setattr(models, "modele_actif", lambda: "fp16")

    def test_une_mesure_suffisante_est_cochee(self, monkeypatch):
        self._mesure(monkeypatch, {"modele": "fp16", "date": "2026-09-06",
                                   "rappel_complet": 97.3, "rappel_patient": 95.0})
        etat, message = installateur._constat_mesure()
        assert etat == "ok"
        assert "95,0 %" in message and "06.09.2026" in message

    def test_une_mesure_insuffisante_le_dit(self, monkeypatch):
        self._mesure(monkeypatch, {"modele": "fp16", "rappel_complet": 62.0})
        etat, message = installateur._constat_mesure()
        assert etat == "manuelle" and "insuffisant" in message

    def test_la_mesure_d_un_autre_modele_ne_compte_pas(self, monkeypatch):
        self._mesure(monkeypatch, {"modele": "int8", "rappel_complet": 97.0})
        assert installateur._constat_mesure() is None

    def test_sans_mesure_rien_n_est_coche(self, monkeypatch):
        self._mesure(monkeypatch, None)
        assert installateur._constat_mesure() is None


def test_les_modules_absents_ne_sont_pas_constates(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    assert installateur._constat_dependances() is None
