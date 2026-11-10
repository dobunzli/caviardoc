"""Reglages communs aux tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import exceptions, interface, paths, revue  # noqa: E402

_DATA_DIR_REEL = paths.data_dir


def _isole() -> bool:
    """Le test a-t-il ecarte le repertoire de donnees de l'utilisateur ?"""
    return bool(os.environ.get("CAVIARDOC_DATA_DIR")
                or paths.data_dir is not _DATA_DIR_REEL)


@pytest.fixture(autouse=True)
def _donnees_dans_un_repertoire_de_test(monkeypatch, tmp_path_factory):
    """Aucun test n'ecrit dans le repertoire de donnees de l'utilisateur.

    Un test qui notait une mesure (97 %) sans isoler le repertoire ecrasait le
    `state.json` reel : la mise a jour suivante comparait alors la mesure a
    une reference factice. Les tests qui veulent un autre repertoire le
    redefinissent eux-memes par `CAVIARDOC_DATA_DIR`.
    """
    monkeypatch.setenv("CAVIARDOC_DATA_DIR",
                       str(tmp_path_factory.mktemp("donnees_auto")))


@pytest.fixture(autouse=True)
def _ne_pas_toucher_aux_donnees_reelles(monkeypatch, tmp_path_factory):
    """Un test qui n'isole pas le repertoire de donnees ne doit ni y ecrire, ni
    en deplacer quoi que ce soit.

    Les corrections retenues et les relectures d'un test s'ajoutaient a celles
    de l'utilisateur. Depuis le renommage, un `exceptions.json` de test sous le
    nouveau nom bloquerait en outre la reprise des vraies corrections, et
    `cli.main`, qui reprend l'ancien repertoire au demarrage, deplacerait son
    modele.
    """
    a_part = tmp_path_factory.mktemp("donnees")
    chemin, sessions = exceptions.Repertoire.chemin, revue.dossier_sessions
    reprendre = paths.reprendre_ancien

    monkeypatch.setattr(exceptions.Repertoire, "chemin", staticmethod(
        lambda: chemin() if _isole() else a_part / "exceptions.json"))
    monkeypatch.setattr(revue, "dossier_sessions",
                        lambda: sessions() if _isole() else a_part / "revues")
    monkeypatch.setattr(paths, "reprendre_ancien",
                        lambda: reprendre() if _isole() else [])


@pytest.fixture(autouse=True)
def _ne_pas_balayer_les_ateliers_reels(monkeypatch):
    """Les tests qui lancent le serveur ne doivent pas toucher aux vraies sessions.

    `servir` efface au demarrage tous les ateliers `interface-*` du repertoire
    de donnees. Sans repertoire de donnees isole, c'est celui de l'utilisateur :
    lancer la suite pendant qu'il relit un document supprimait ses fichiers
    deposes, et son traitement echouait sur un fichier disparu. Le balayage
    n'a lieu que si le test a isole `CAVIARDOC_DATA_DIR`.
    """
    reel = interface.balayer_ateliers_abandonnes

    def balayer():
        return reel() if os.environ.get("CAVIARDOC_DATA_DIR") else []

    monkeypatch.setattr(interface, "balayer_ateliers_abandonnes", balayer)


@pytest.fixture(autouse=True)
def _ne_pas_noter_la_session_reelle(monkeypatch):
    """Une console de test ne doit pas se faire passer pour la page ouverte
    par l'utilisateur : le lancement suivant rouvrirait le test, disparu."""
    noter, oublier = interface.noter_session, interface.oublier_session

    def seulement_isole(fonction):
        return lambda *a: (fonction(*a) if os.environ.get("CAVIARDOC_DATA_DIR")
                           else None)

    monkeypatch.setattr(interface, "noter_session", seulement_isole(noter))
    monkeypatch.setattr(interface, "oublier_session", seulement_isole(oublier))
