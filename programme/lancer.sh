#!/usr/bin/env bash
# Caviardoc. Lance par le raccourci a double-clic, ou directement :
#     ./lancer.sh
#
# Un seul fichier pour tout : il trouve ou installe Python, puis ouvre la page.
# Celle-ci a deux onglets — l'application et son entretien — et s'ouvre sur
# celui qui sert : l'installation tant qu'il manque quelque chose, l'application
# des que tout est pret.
#
# Entre accolades : bash lit tout le bloc avant de l'executer. Une mise a jour
# remplace ce fichier pendant que la page est ouverte, et bash reprendrait
# sinon sa lecture, a la fermeture, au milieu d'un fichier qui a change.
{
  set -uo pipefail
  cd "$(dirname "$0")"

  echo "Caviardoc"
  echo

  # Les .pyc n'ont rien a faire dans le depot.
  export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${XDG_CACHE_HOME:-$HOME/.cache}/caviardoc/pycache}"

  attendre() { read -r -p "Appuyez sur Entree pour fermer. " _; }

  . tools/trouver-python.sh
  assurer_python || { attendre; exit 1; }

  PYTHONPATH=src "$PY" -m caviardoc console
  code=$?

  if [ $code -ne 0 ]; then
    echo
    echo "Caviardoc s'est arrete (code $code)."
    attendre
  fi
  exit $code
}
