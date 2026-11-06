"""Telemetrie d'ONNX Runtime : coupee avant tout chargement de la bibliotheque.

Les versions officielles d'onnxruntime pour macOS et Linux embarquent le client
1DS de Microsoft. Il met des evenements d'utilisation en file dans
`~/Library/Application Support/Microsoft/DeveloperTools/.onnxruntime/` (macOS)
et les envoie a `https://mobile.events.data.microsoft.com/OneCollector/1.0`.
Son fil d'envoi survivait en outre a l'interpreteur et faisait abandonner le
processus en sortie, une fois sur trois environ (« recursive_mutex lock
failed », code 134).

Seule la variable `ORT_DISABLE_TELEMETRY=1`, posee avant que la bibliotheque
s'initialise, empeche la file, l'envoi et l'identifiant d'appareil (Privacy.md
du paquet). `disable_telemetry_events()` ne coupe que les evenements non
essentiels, apres un premier evenement deja emis : seul, il ne suffisait pas.

`couper` est appele a l'import du paquet (`caviardoc/__init__.py`) : la CLI,
l'interface et les outils de mesure passent tous par la avant d'importer le
moteur, et les processus enfants heritent de la variable.
"""

from __future__ import annotations

import os
import sys

VARIABLE = "ORT_DISABLE_TELEMETRY"


def couper() -> None:
    """Pose la variable, puis coupe aussi par l'API si la bibliotheque est deja
    chargee. Forcee a « 1 » : une valeur heritee ne peut pas la rallumer. Une
    erreur de l'API n'empeche jamais de travailler."""
    os.environ[VARIABLE] = "1"
    ort = sys.modules.get("onnxruntime")
    if ort is None:
        return
    try:
        ort.disable_telemetry_events()
    except Exception:
        pass


def coupee() -> bool:
    """La variable est-elle posee pour ce processus et ses enfants ?"""
    return os.environ.get(VARIABLE) == "1"
