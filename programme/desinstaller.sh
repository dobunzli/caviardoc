#!/usr/bin/env bash
# Desinstallation. Lance par les lanceurs a double-clic, ou directement :
#     ./desinstaller.sh
#
# Ne cherche pas a installer quoi que ce soit : desinstaller avec un Python
# qu'on viendrait de telecharger pour l'occasion serait absurde.
set -uo pipefail
cd "$(dirname "$0")"

echo "Caviardoc — desinstallation"
echo

attendre() { read -r -p "Appuyez sur Entree pour fermer. " _; }

. tools/trouver-python.sh
PY="$(trouver_python || true)"

if [ -z "$PY" ]; then
  # Sans Python on ne peut ni calculer les chemins ni mesurer les tailles. On
  # donne alors les emplacements connus, et l'utilisateur tranche.
  echo "Aucun Python n'a ete trouve : la desinstallation guidee n'est pas"
  echo "possible. Les dossiers a supprimer sont :"
  echo
  if [ "$(uname)" = "Darwin" ]; then
    echo "  ~/Library/Application Support/caviardoc"
  else
    echo "  \${XDG_DATA_HOME:-~/.local/share}/caviardoc"
  fi
  echo "  ~/.cache/caviardoc"
  echo
  echo "Puis le dossier qui contient les lanceurs : $(cd .. && pwd)"
  echo
  attendre
  exit 1
fi

PYTHONPATH=src "$PY" -m caviardoc desinstaller "$@"
code=$?

echo
attendre
exit $code
