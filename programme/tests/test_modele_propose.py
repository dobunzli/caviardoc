"""Proposer un modele plus performant, sans rien telecharger sans accord.

Le catalogue declare qu'un modele en surpasse d'autres (`remplace`). La decision
de le proposer est une fonction pure ; le refus de l'utilisateur, lui, survit
dans l'etat de la machine.
"""

from __future__ import annotations

import dataclasses

import pytest

from caviardoc import interface, models

GO = 1024 ** 3
ANCIEN = models.GLINER_MULTI_PII_FP16
NOUVEAU = dataclasses.replace(
    models.GLINER_MULTI_PII_FP32, key="nouveau", memoire=2 * GO,
    rappel_reference=99.0, remplace=(ANCIEN.key,), nouveautes="Mieux.")
REGISTRE = {ANCIEN.key: ANCIEN, NOUVEAU.key: NOUVEAU}


def propose(actif=ANCIEN.key, installes=(), refuses=(), ram=16 * GO,
            registre=REGISTRE):
    return models.modele_propose(registre, actif, set(installes), set(refuses),
                                 ram)


def test_propose_quand_le_modele_en_service_est_remplace():
    assert propose() is NOUVEAU


def test_rien_si_le_modele_en_service_n_est_pas_concerne():
    assert propose(actif=NOUVEAU.key) is None
    assert propose(actif="autre") is None


def test_rien_si_deja_installe_ou_refuse():
    assert propose(installes=[NOUVEAU.key]) is None
    assert propose(refuses=[NOUVEAU.key]) is None


def test_rien_si_deconseille():
    rejete = dataclasses.replace(NOUVEAU, deconseille="rappel insuffisant")
    assert propose(registre={ANCIEN.key: ANCIEN, rejete.key: rejete}) is None


def test_rien_si_la_memoire_ne_suffit_pas():
    assert propose(ram=3 * GO) is None
    assert propose(ram=4 * GO) is NOUVEAU
    assert propose(ram=None) is NOUVEAU


def test_le_meilleur_rappel_l_emporte():
    meilleur = dataclasses.replace(NOUVEAU, key="meilleur", rappel_reference=99.5)
    registre = {**REGISTRE, meilleur.key: meilleur}
    assert propose(registre=registre) is meilleur


def test_le_registre_actuel_ne_propose_rien():
    assert propose(registre=models.REGISTRY, actif=models.DEFAULT_MODEL) is None


@pytest.fixture
def donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    return tmp_path


def test_le_refus_persiste_sans_ecraser_le_reste(donnees):
    autre = models.GLINER_MULTI_PII_FP32.key
    models.definir_modele_actif(autre)
    assert models.modeles_refuses() == frozenset()
    models.refuser_proposition(autre)
    models.refuser_proposition(autre)
    assert models.modeles_refuses() == {autre}
    assert models.modele_actif() == autre


def test_refuser_un_modele_inconnu_echoue(donnees):
    with pytest.raises(KeyError):
        models.refuser_proposition("inconnu")


def test_etat_abime_vaut_aucun_refus(donnees):
    (donnees / "state.json").write_text('{"modeles_refuses": 3}', encoding="utf-8")
    assert models.modeles_refuses() == frozenset()


def test_la_vue_expose_la_proposition(donnees, monkeypatch):
    monkeypatch.setattr(models, "REGISTRY", REGISTRE)
    monkeypatch.setattr(models, "DEFAULT_MODEL", ANCIEN.key)
    monkeypatch.setattr(models, "check", lambda s: type("C", (), {"complete": s is ANCIEN})())
    models.definir_modele_actif(ANCIEN.key)
    vue = interface.vue_modeles(16 * GO)
    assert vue["proposition"]["cle"] == NOUVEAU.key
    assert vue["proposition"]["nouveautes"] == "Mieux."
    models.refuser_proposition(NOUVEAU.key)
    assert interface.vue_modeles(16 * GO)["proposition"] is None
