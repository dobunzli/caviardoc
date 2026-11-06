"""La telemetrie d'ONNX Runtime est coupee avant toute session du moteur.

Sans reseau ni vrai modele : `gliner` et `onnxruntime` sont remplaces par des
modules factices qui notent, au moment ou ils sont importes ou construisent
une session, si la coupure etait deja en place.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import subprocess
import sys
import types
from pathlib import Path

from caviardoc import engine, models, telemetrie

SRC = Path(__file__).resolve().parents[1] / "src"


class _Factices(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Fournit `gliner` et `onnxruntime` factices, en notant l'etat de la
    variable a l'import de chacun."""

    def __init__(self, journal: list):
        self.journal = journal

    def find_spec(self, nom, chemin=None, cible=None):
        if nom in ("gliner", "onnxruntime"):
            return importlib.machinery.ModuleSpec(nom, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        journal = self.journal
        journal.append(("import " + module.__name__,
                        os.environ.get(telemetrie.VARIABLE)))
        if module.__name__ == "onnxruntime":
            module.set_default_logger_severity = lambda niveau: None
            module.disable_telemetry_events = (
                lambda: journal.append(("api", None)))

            class InferenceSession:
                def __init__(self, *args, **kwargs):
                    journal.append(("session",
                                    os.environ.get(telemetrie.VARIABLE)))
            module.InferenceSession = InferenceSession
        else:
            class GLiNER:
                @staticmethod
                def from_pretrained(*args, **kwargs):
                    import onnxruntime
                    return onnxruntime.InferenceSession("modele.onnx")
            module.GLiNER = GLiNER


def test_aucune_session_avant_coupure(monkeypatch):
    journal: list = []
    for nom in ("gliner", "onnxruntime"):
        monkeypatch.delitem(sys.modules, nom, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Factices(journal), *sys.meta_path])
    # Comme si un appelant avait retire la variable apres l'import du paquet.
    monkeypatch.delenv(telemetrie.VARIABLE, raising=False)
    monkeypatch.setattr(models, "check", lambda spec, deep=False:
                        types.SimpleNamespace(complete=True, missing_bytes=0))

    engine.load_model(next(iter(models.REGISTRY)))

    assert ("session", "1") in journal, journal
    avant_session = journal[:journal.index(("session", "1"))]
    assert ("import gliner", "1") in avant_session, journal
    assert ("import onnxruntime", "1") in avant_session, journal
    assert ("api", None) in avant_session, journal
    assert all(etat == "1" for evenement, etat in journal
               if evenement != "api"), journal


def test_import_du_paquet_coupe_avant_le_moteur():
    """Tout point d'entree importe `caviardoc` d'abord : la variable y est
    forcee, meme heritee a « 0 », sans charger onnxruntime pour autant."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env[telemetrie.VARIABLE] = "0"
    env["PYTHONPATH"] = str(SRC)
    code = ("import os, sys, caviardoc\n"
            "print(os.environ.get('ORT_DISABLE_TELEMETRY'),"
            " 'onnxruntime' in sys.modules)")
    sortie = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True, timeout=60)
    assert sortie.returncode == 0, sortie.stderr[-2000:]
    assert sortie.stdout.split() == ["1", "False"]


def test_couper_appelle_l_api_si_deja_chargee(monkeypatch):
    appels = []
    faux = types.ModuleType("onnxruntime")
    faux.disable_telemetry_events = lambda: appels.append(1)
    monkeypatch.setitem(sys.modules, "onnxruntime", faux)
    monkeypatch.setenv(telemetrie.VARIABLE, "0")
    telemetrie.couper()
    assert appels == [1]
    assert telemetrie.coupee()
