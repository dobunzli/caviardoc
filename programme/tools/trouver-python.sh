# Trouve un Python utilisable, ou l'installe. A sourcer, pas a executer :
#     . tools/trouver-python.sh
#     assurer_python || exit 1        # renseigne $PY
#
# Partage par lancer.sh (double-clic), bootstrap.sh (terminal) et
# desinstaller.sh. Deux copies du meme code finissaient par diverger.

# Le plancher qu'on accepte, et la version qu'on installe quand il n'y a rien.
# Les deux different : on se contente d'un 3.11 deja present, mais on n'en
# installe pas un aussi vieux.
VERSION_MINIMALE="${VERSION_MINIMALE:-3.11}"
VERSION_INSTALLEE="${VERSION_INSTALLEE:-3.14}"

# On verifie la version, et non la seule presence : un Python 3.9 passait le
# test puis echouait plus loin, sur un message incomprehensible.
python_convient() {
  [ -n "${1:-}" ] || return 1
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
    >/dev/null 2>&1
}

# Sur un Mac sans outils de developpement, /usr/bin/python3 n'est qu'un relais :
# l'executer ouvre une fenetre proposant d'installer Xcode, a chaque lancement,
# pour un Python 3.9 qu'on refuserait de toute facon.
relais_xcode() {
  [ "$(uname)" = "Darwin" ] && [ "$1" = "/usr/bin/python3" ] \
    && ! /usr/bin/xcode-select -p >/dev/null 2>&1
}

# Ecrit le chemin sur stdout, ou rend 1. N'installe rien, ne dit rien.
trouver_python() {
  for candidat in "${PYTHON:-}" python3 python; do
    [ -n "$candidat" ] || continue
    chemin="$(command -v "$candidat" 2>/dev/null)" || continue
    relais_xcode "$chemin" && continue
    if python_convient "$chemin"; then echo "$chemin"; return 0; fi
  done
  return 1
}

# Le Python de l'environnement de l'application, une fois installee. C'est lui
# que la page utilise de toute facon ; le prendre d'emblee evite, quand Python
# a ete pose par uv hors du PATH, d'annoncer « aucun Python » a chaque
# lancement. Meme emplacement que `paths.venv_dir()`.
python_de_l_application() {
  if [ -n "${CAVIARDOC_DATA_DIR:-}" ]; then
    donnees="$CAVIARDOC_DATA_DIR"
  elif [ "$(uname)" = "Darwin" ]; then
    donnees="$HOME/Library/Application Support/caviardoc"
  else
    donnees="${XDG_DATA_HOME:-$HOME/.local/share}/caviardoc"
  fi
  python_convient "$donnees/venv/bin/python" || return 1
  echo "$donnees/venv/bin/python"
}

# Un Python que uv a deja pose. Ne telecharge rien, ne dit rien.
# --system partout : sans lui, uv rend le .venv du dossier courant ou celui de
# VIRTUAL_ENV, que l'on prendrait pour un Python a lui.
python_de_uv() {
  uv_local="$(trouver_uv || true)"
  [ -n "$uv_local" ] || return 1
  chemin="$("$uv_local" python find --system "$VERSION_INSTALLEE" 2>/dev/null || true)"
  python_convient "$chemin" || return 1
  echo "$chemin"
}

trouver_uv() {
  command -v uv 2>/dev/null && return 0
  [ -x "$HOME/.local/bin/uv" ] && { echo "$HOME/.local/bin/uv"; return 0; }
  return 1
}

# uv telecharge un Python autonome, sans droits d'administrateur et sans
# toucher au systeme. C'est ce qui evite d'envoyer l'utilisateur telecharger un
# installateur a la main — le seul point ou l'on abandonnait.
installer_python_avec_uv() {
  uv_local="$(trouver_uv || true)"

  if [ -z "$uv_local" ]; then
    UV_POSE_PAR_NOUS=1
    # Telecharger et executer un script distant ne se fait pas en silence dans
    # un outil de confidentialite : on dit quoi, d'ou, et on demande.
    echo "Caviardoc peut installer Python pour vous, avec uv."
    echo "  uv est un outil libre d'Astral. Il sera telecharge depuis :"
    echo "      https://github.com/astral-sh/uv/releases (version 0.12.14, empreinte SHA-256 verifiee)"
    echo "  Il s'installe dans votre dossier personnel, sans mot de passe"
    echo "  administrateur et sans modifier le systeme."
    echo
    printf "Installer uv puis Python ? [O/n] "
    read -r reponse
    case "${reponse:-o}" in
      [nN]*) return 2 ;;
    esac
    echo
    echo "Telechargement de uv..."
    sh "$(dirname "${BASH_SOURCE[0]:-$0}")/installer-uv.sh" || return 3
    uv_local="$(trouver_uv || true)"
    [ -n "$uv_local" ] || return 3
  fi

  echo
  echo "Installation de Python $VERSION_INSTALLEE..."
  # Un Python deja connu de uv n'est pas a nous : on ne le notera pas.
  if ! "$uv_local" python find --system "$VERSION_INSTALLEE" >/dev/null 2>&1; then
    PYTHON_POSE_PAR_NOUS=1
  fi
  "$uv_local" python install "$VERSION_INSTALLEE" || return 4

  PY="$("$uv_local" python find --system "$VERSION_INSTALLEE" 2>/dev/null || true)"
  python_convient "$PY" || return 5
  echo
  echo "Python $("$PY" -c 'import platform; print(platform.python_version())') installe."
  return 0
}

# Note ce que nous avons pose hors du dossier de l'application, pour que la
# desinstallation sache le distinguer de ce qui etait deja la. Sans bruit : une
# trace manquante fait proposer moins, jamais supprimer plus.
noter_ce_qu_on_a_pose() {
  [ -n "${UV_POSE_PAR_NOUS:-}" ] || [ -n "${PYTHON_POSE_PAR_NOUS:-}" ] || return 0
  # Chacun separement : un uv deja la n'empeche pas que Python soit de nous.
  UV_POSE_PAR_NOUS="${UV_POSE_PAR_NOUS:-}" \
  PYTHON_POSE_PAR_NOUS="${PYTHON_POSE_PAR_NOUS:-}" \
  PYTHONPATH=src "$PY" -c "
import os
from caviardoc import desinstallation as d
if os.environ.get('UV_POSE_PAR_NOUS'):
    d.noter_installe('uv')
if os.environ.get('PYTHON_POSE_PAR_NOUS'):
    d.noter_installe('python', '$VERSION_INSTALLEE')
" >/dev/null 2>&1 || true
}

# Renseigne $PY. Rend 0 si un interpreteur est pret, sinon explique et rend 1.
assurer_python() {
  PY="$(python_de_l_application || trouver_python || python_de_uv || true)"
  [ -n "$PY" ] && return 0

  echo "Aucun Python $VERSION_MINIMALE ou superieur n'a ete trouve."
  echo
  installer_python_avec_uv
  case $? in
    0) noter_ce_qu_on_a_pose; return 0 ;;
    2) echo
       echo "Installez alors Python $VERSION_MINIMALE ou superieur depuis"
       echo "https://www.python.org/downloads/ puis relancez ce fichier." ;;
    3) echo
       echo "Le telechargement a echoue. Verifiez votre connexion, ou"
       echo "installez Python depuis https://www.python.org/downloads/" ;;
    4) echo
       echo "L'installation de Python a echoue." ;;
    5) echo
       echo "Python a ete installe, mais reste introuvable. Fermez cette"
       echo "fenetre, rouvrez-la, et relancez ce fichier." ;;
  esac
  echo
  return 1
}
