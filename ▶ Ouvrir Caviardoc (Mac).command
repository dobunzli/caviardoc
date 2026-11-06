#!/usr/bin/env bash
# Double-cliquez ce fichier. C'est le seul dont vous ayez besoin.
#
# Il installe ce qui manque, puis ouvre Caviardoc dans votre navigateur.
# Au premier lancement, macOS peut demander confirmation : le fichier vient
# d'internet. Si le double-clic ne fait rien, ouvrez le Terminal dans ce
# dossier et lancez : chmod +x *.command
#
# Le « ▶ » en tete du nom n'est pas decoratif : le Finder classe les symboles
# avant les lettres, et ce fichier apparait ainsi en premier dans le dossier.
#
# Tout le reste vit dans `programme/`, pour que ce dossier-ci ne montre que
# les deux lanceurs.
#
# Entre accolades : bash lit tout le bloc avant de l'executer, et une mise a
# jour peut remplacer ce fichier pendant que la page est ouverte.
{
  cd "$(dirname "$0")/programme"
  # Par bash, et non `./lancer.sh` : un ZIP decompresse par un autre outil que
  # celui du Finder perd le bit executable, et le `chmod +x *.command` que donne
  # le README ne le rend qu'a ce fichier-ci.
  /bin/bash ./lancer.sh
  code=$?

  # Apres « Quitter » dans la page (code 0), on ferme aussi la fenetre du
  # Terminal. Si macOS refuse ou si la commande echoue, la fenetre reste ouverte :
  # c'est sans consequence. En cas d'erreur, on la laisse pour qu'on puisse la lire.
  # Celle-ci seulement, reconnue a son terminal : toutes celles qui portaient
  # le nom du lanceur fermaient aussi une autre session encore ouverte.
  if [ $code -eq 0 ]; then
    ici="$(tty 2>/dev/null)"
    ( sleep 1
      osascript -e 'on run {ici}' -e 'tell application "Terminal"' \
        -e 'repeat with w in windows' \
        -e 'if tty of selected tab of w is ici then' -e 'close w' -e 'exit repeat' \
        -e 'end if' -e 'end repeat' -e 'end tell' -e 'end run' "$ici" \
        >/dev/null 2>&1 ) >/dev/null 2>&1 &
    disown 2>/dev/null
  fi
  exit $code
}
