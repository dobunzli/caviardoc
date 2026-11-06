"""Emplacements de fichiers, resolus a l'execution.

Regle fondamentale de ce projet : le depot ne contient que des sources. Le
modele, l'environnement virtuel et les caches sont des artefacts — telecharges
ou reconstruits — et vivent dans un repertoire de donnees local a la machine.

Trois raisons : ce sont des resultats et non des sources ; ils ne sont pas
portables, un venv macOS arm64 etant inutilisable sous Windows ; et ils pesent
plus d'un gigaoctet, qu'un depot git n'oublierait jamais.

Ce module n'utilise que la bibliotheque standard : il doit fonctionner sur une
machine neuve, avant toute installation de dependance.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "caviardoc"

#: Noms portes par le projet avant ses renommages, du plus recent au plus
#: ancien. Le repertoire de donnees en dependait : sur une machine qui a servi
#: a une ancienne version, le modele et les corrections retenues s'y trouvent
#: encore.
ANCIENS_APP_NAMES = ("doc-anonymizer", "pii-anonymizer")

#: Ce qu'on reprend d'un ancien repertoire de donnees en le deplacant : le
#: modele (plusieurs centaines de megaoctets), les corrections retenues, les
#: relectures en cours, la mise a jour qui attend son second temps
#: (`maj.FICHIER_ATTENTE`) et la trace de ce que le lanceur a installe
#: (`desinstallation.TRACE`). Le venv et Tesseract n'en sont pas : ils portent
#: des chemins absolus et, deplaces, ne fonctionneraient plus. Ils sont
#: reinstalles.
REPRIS = ("models", "exceptions.json", "revues", "maj-en-attente.json",
          "installe-par-nous.json")

#: Variable d'environnement permettant de forcer le repertoire de donnees.
ENV_DATA_DIR = "CAVIARDOC_DATA_DIR"

#: Pour lire la sortie d'un programme externe : ``subprocess.run(..., **TEXTE)``.
#:
#: uv, git et micromamba ecrivent en UTF-8. Sous Windows, `text=True` seul
#: decode avec la page de code ANSI (cp1252) : un chemin accentue —
#: « C:\Users\Hélène » — en sortait deforme, donc introuvable, et certains
#: octets UTF-8 levaient UnicodeDecodeError en pleine installation.
TEXTE = {"text": True, "encoding": "utf-8", "errors": "replace"}


def uv_exe() -> str | None:
    """Le uv a utiliser : celui du PATH, sinon celui de ``~/.local/bin``.

    L'installateur officiel de uv pose ses binaires dans ``~/.local/bin``. Le
    processus qui vient de les poser ne le voit pas dans son PATH — et sous
    Windows, un lanceur ouvert par double-clic ne le verra qu'apres une
    reconnexion. Ne regarder que le PATH faisait « disparaitre » un uv que
    l'on venait d'installer.
    """
    import shutil

    trouve = shutil.which("uv")
    if trouve:
        return trouve
    suffixe = ".exe" if sys.platform == "win32" else ""
    local = Path.home() / ".local" / "bin" / f"uv{suffixe}"
    return str(local) if local.is_file() else None


def project_root() -> Path:
    """Dossier du programme : `pyproject.toml`, code, tests et outils.

    C'est la que `uv sync` travaille. Il ne se confond pas avec la racine du
    depot, voir `repo_root`.
    """
    return Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """Racine du depot git, un cran au-dessus du dossier du programme.

    C'est le dossier que l'utilisateur ouvre : il n'y voit que les deux
    lanceurs, le README, la licence et `programme/`. Le reste y etait range a
    plat, et il fallait chercher le fichier a double-cliquer parmi une dizaine
    d'autres auxquels il ne devait pas toucher.

    C'est aussi celui qu'on supprime pour desinstaller, et celui ou git trouve
    son `.git`.
    """
    return project_root().parent


def data_dir() -> Path:
    """Repertoire de donnees local, propre a la machine. Hors du depot.

    Contient le modele telecharge, les caches et l'environnement virtuel.
    Surchargeable par la variable d'environnement ``CAVIARDOC_DATA_DIR``, utile
    pour placer les donnees sur un disque externe.
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))

    return (base / APP_NAME).resolve()


def anciens_data_dirs() -> list[Path]:
    """Tous les repertoires de donnees d'anciens noms qui existent encore.

    Vide quand il n'y a rien a migrer — le cas normal. Un ancien repertoire
    migre reste present (le venv y demeure) : le premier ne doit pas cacher
    les suivants.
    """
    if os.environ.get(ENV_DATA_DIR):
        return []
    return [ancien for nom in ANCIENS_APP_NAMES
            if (ancien := data_dir().with_name(nom)).is_dir()]


def ancien_data_dir() -> Path | None:
    """Premier repertoire de donnees d'un ancien nom, ou None."""
    anciens = anciens_data_dirs()
    return anciens[0] if anciens else None


def reprendre_ancien() -> list[str]:
    """Deplace ici ce qu'un ancien nom avait laisse dans son repertoire.

    Appele a chaque demarrage, avant que quoi que ce soit ne cree le nouveau
    repertoire ; ne fait rien dans le cas normal. Un element deja present sous
    le nouveau nom n'est jamais ecrase, sauf un dossier vide. Un deplacement
    refuse — un fichier encore ouvert, sous Windows — sera retente au
    demarrage suivant ; le diagnostic signale l'ancien repertoire en attendant.

    Rend les noms des elements repris.
    """
    repris = []
    # Dans l'ordre des noms : le premier a fournir un element l'emporte, rien
    # n'est jamais ecrase.
    for ancien in anciens_data_dirs():
        for nom in REPRIS:
            source, cible = ancien / nom, data_dir() / nom
            try:
                if not source.exists():
                    continue
                if cible.is_dir() and not any(cible.iterdir()):
                    cible.rmdir()
                if cible.exists():
                    continue
                cible.parent.mkdir(parents=True, exist_ok=True)
                source.rename(cible)
            except OSError:
                continue
            repris.append(nom)
    return repris


def models_dir() -> Path:
    """Racine des modeles telecharges."""
    return data_dir() / "models"


def venv_dir() -> Path:
    """Environnement virtuel, hors du depot.

    Il contient des binaires compiles pour une plateforme et des chemins
    absolus. Le versionner rendrait le depot inutilisable d'une machine a
    l'autre, en plus de l'alourdir de plusieurs gigaoctets.
    """
    return data_dir() / "venv"


def cache_dir() -> Path:
    """Caches divers (telechargements partiels, resultats intermediaires)."""
    return data_dir() / "cache"


def state_file() -> Path:
    """Etat d'installation propre a cette machine."""
    return data_dir() / "state.json"


def is_inside_synced_folder(path: Path) -> bool:
    """Indique si ``path`` se trouve sous la racine du depot."""
    try:
        path.resolve().relative_to(repo_root())
    except (ValueError, OSError):
        return False
    return True


def taille_reelle(chemin: Path) -> int:
    """Octets reellement occupes par ``chemin``, fichier ou dossier.

    Les liens symboliques sont ignores et les liens durs comptes une fois.
    Les deux comptent ici : conda pose beaucoup des uns et des autres, et les
    suivre naivement triple le chiffre qu'on annonce a l'utilisateur.
    """
    if not chemin.exists():
        return 0
    if chemin.is_file() and not chemin.is_symlink():
        return chemin.stat().st_size

    vus: set[int] = set()
    total = 0
    for fichier in chemin.rglob("*"):
        try:
            if fichier.is_symlink() or not fichier.is_file():
                continue
            etat = fichier.stat()
        except OSError:
            continue
        # Un numero de fichier nul ne dit rien (FAT, exFAT, certains partages
        # reseau sous Windows) : le compter comme un lien dur ne garderait que
        # le premier fichier du dossier.
        if etat.st_ino:
            if etat.st_ino in vus:
                continue
            vus.add(etat.st_ino)
        total += etat.st_size
    return total


def ensure_dirs() -> None:
    """Cree les repertoires de donnees s'ils n'existent pas."""
    for d in (data_dir(), models_dir(), cache_dir()):
        d.mkdir(parents=True, exist_ok=True)
