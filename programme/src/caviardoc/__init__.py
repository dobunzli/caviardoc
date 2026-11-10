"""Caviardoc : caviardage de documents, local et multiplateforme."""

from . import telemetrie as _telemetrie

__version__ = "0.2.1"

# Avant tout import du moteur, par quelque point d'entree que ce soit : voir
# `telemetrie`.
_telemetrie.couper()
