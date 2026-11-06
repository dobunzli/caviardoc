#!/usr/bin/env bash
# Premiere installation sur une machine macOS ou Linux, depuis un terminal.
# N'exige rien : si Python manque, il est propose a l'installation.
set -uo pipefail
cd "$(dirname "$0")"
echo "Caviardoc — installation"
echo
# Les .pyc ne doivent pas atterrir dans le dossier synchronise.
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$HOME/.cache/caviardoc/pycache}"

. tools/trouver-python.sh
assurer_python || exit 1

PYTHONPATH=src "$PY" -m caviardoc doctor || true
echo
PYTHONPATH=src "$PY" -m caviardoc setup "$@"
