"""Reprendre tout ce que l'installation a pose, et rien d'autre.

Un outil qui s'installe en telechargeant Python, un environnement virtuel, un
modele de 570 Mo et un moteur d'OCR doit savoir defaire tout cela. Sans quoi il
laisse, sur une machine qu'on croit propre, plus d'un gigaoctet dans des
dossiers que personne ne pense a regarder.

Deux principes, et ils tirent en sens contraire :

- **Complet.** Ce qui a ete pose doit pouvoir etre repris, y compris ce qui vit
  hors du dossier de l'application — uv et le Python qu'il a installe.
- **Rien d'autre.** uv est peut-etre anterieur a nous, et sert peut-etre a
  autre chose. Le supprimer parce qu'il se trouve la serait un degat, pas un
  nettoyage.

D'ou la trace : quand un lanceur installe uv, il le note. La desinstallation
distingue alors ce qu'elle *sait* avoir pose de ce qu'elle ne fait que trouver,
et ne propose par defaut que le premier.

Une limite, apprise en la franchissant : avoir installe uv ne fait pas de nous
le proprietaire de tout ce que uv range ensuite dans ``~/.local/share/uv``. Ce
dossier contient son cache et les interpreteurs Python qu'il gere, dont des
versions posees pour d'autres projets, parfois anterieures a nous. Un essai a
supprime le dossier entier au motif que la trace disait « uv est a nous » : il
a emporte un Python installe trois mois plus tot, sans rapport avec ce projet.

La regle qui en decoule : **on ne supprime jamais le dossier global d'un autre
outil.** Ce qui appartient a uv est montre, chiffre, et accompagne de la
commande que uv fournit pour s'en occuper lui-meme — c'est lui qui sait ce
qu'il peut retirer sans casser autre chose. Meme chose pour le depot, qui est
le dossier que l'utilisateur a cree et depuis lequel le lanceur s'execute.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths

#: Fichier ou les lanceurs notent ce qu'ils ont installe hors du dossier de
#: l'application. Il vit dedans, donc il disparait avec lui.
TRACE = "installe-par-nous.json"


def _fichier_trace() -> Path:
    return paths.data_dir() / TRACE


def noter_installe(quoi: str, valeur=True) -> None:
    """Retient que nous avons installe ``quoi``. Appele par les lanceurs.

    Ecrit sans bruit : un lanceur qui echoue a poser cette trace ne doit pas
    faire echouer l'installation. La consequence d'une trace manquante est
    seulement qu'on proposera moins, jamais qu'on supprimera plus.
    """
    try:
        paths.data_dir().mkdir(parents=True, exist_ok=True)
        trace = pose_par_nous()
        trace[quoi] = valeur
        _fichier_trace().write_text(json.dumps(trace, indent=2) + "\n")
    except OSError:
        pass


def pose_par_nous() -> dict:
    try:
        return json.loads(_fichier_trace().read_text())
    except (OSError, ValueError):
        return {}


@dataclass
class Element:
    """Une chose a supprimer, avec de quoi decider en connaissance de cause."""

    cle: str
    titre: str
    chemin: Path
    octets: int
    #: Ce que c'est, et ce qu'on perd en le supprimant.
    pourquoi: str
    #: True si nous l'avons pose. Les autres sont montres mais pas coches.
    notre_fait: bool = True
    #: Quand l'element ne se supprime pas en effacant ``chemin`` mais par la
    #: commande de l'outil qui l'a pose (``uv python uninstall``).
    commande: tuple[str, ...] = ()

    @property
    def par_defaut(self) -> bool:
        return self.notre_fait

    def vue(self) -> dict:
        return {"cle": self.cle, "titre": self.titre, "chemin": str(self.chemin),
                "octets": self.octets, "pourquoi": self.pourquoi,
                "par_defaut": self.par_defaut}


def _cache_pycache() -> Path:
    """Ou les lanceurs envoient les .pyc, pour ne pas salir le depot."""
    force = os.environ.get("PYTHONPYCACHEPREFIX")
    if force:
        return Path(force)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / paths.APP_NAME / "pycache"


def _uv_binaires() -> list[Path]:
    """uv tel que l'installateur officiel le pose, dans le dossier personnel.

    Un uv venu d'Homebrew ou de winget est ailleurs : on ne le voit pas, et
    c'est voulu — il n'est pas a nous.
    """
    base = Path.home() / ".local" / "bin"
    suffixe = ".exe" if sys.platform == "win32" else ""
    return [base / f"uv{suffixe}", base / f"uvx{suffixe}"]


def _uv_donnees() -> Path:
    """Cache de uv et Pythons qu'il gere — dont celui que le lanceur a pose."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "uv"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "uv"


def _env_sans_couleurs() -> dict:
    """L'environnement pour lire la sortie de uv : sans codes ANSI.

    Avec FORCE_COLOR, uv colore meme ce qu'il ecrit dans un tube, et
    ``--color never`` n'y change rien (uv 0.4) ; seul NO_COLOR l'emporte.
    """
    return {**os.environ, "NO_COLOR": "1"}


def dossier_python_uv(uv: str, version: str) -> Path | None:
    """Le dossier ou uv a installe ce Python, ou None s'il n'en gere pas.

    Seulement un Python que uv *gere* (``--managed-python``) : un Python
    quelconque du systeme ne sera jamais rendu ici. Et jamais un
    environnement virtuel (``--system``) : lance depuis le dossier du
    programme, uv rendait son ``.venv``, et notre Python restait en place.
    """
    try:
        sortie = subprocess.run(
            [uv, "python", "find", "--managed-python", "--system", version],
            capture_output=True, env=_env_sans_couleurs(), **paths.TEXTE,
            timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not sortie:
        return None
    for parent in Path(sortie.splitlines()[0]).parents:
        if parent.parent.name == "python" and parent.name.startswith("cpython"):
            return parent
    return None


def dossier_cache_uv(uv: str) -> Path | None:
    """Le cache de uv, tel que uv le dit.

    Sans couleurs, voir `_env_sans_couleurs` : un chemin colore n'existe pas, et
    le cache disparaissait sans un mot.
    """
    try:
        sortie = subprocess.run([uv, "cache", "dir"], capture_output=True,
                                env=_env_sans_couleurs(), **paths.TEXTE,
                                timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(sortie) if sortie else None


def inventaire() -> list[Element]:
    """Ce que l'on peut supprimer sans deborder : nos dossiers, nos binaires.

    Volontairement court. Tout ce qui appartient a un autre outil passe par
    `laisses_a_l_utilisateur()`, qui montre sans toucher.
    """
    trace = pose_par_nous()
    elements: list[Element] = []

    def ajouter(cle, titre, chemin, pourquoi, notre_fait=True, commande=()):
        if chemin and chemin.exists():
            elements.append(Element(cle, titre, chemin,
                                    paths.taille_reelle(chemin), pourquoi,
                                    notre_fait, commande))

    ajouter("donnees", "Données de l'application", paths.data_dir(),
            "Le modèle, les bibliothèques, Tesseract et les corrections "
            "retenues.")

    for rang, ancien in enumerate(paths.anciens_data_dirs()):
        ajouter("ancien" if rang == 0 else f"ancien-{rang + 1}",
                "Données d'une ancienne version", ancien,
                "Laissées par la version précédente du programme ; "
                "plus utilisées.")

    ajouter("pycache", "Fichiers temporaires de Python", _cache_pycache(),
            "Recréés automatiquement au besoin.")

    # Le Python que le lanceur a installe, quand la trace dit que c'est lui —
    # jamais un Python deja present. On ne supprime pas le dossier de uv : on
    # demande a uv de retirer cette version-la, et elle seule. Place avant le
    # binaire uv, dont la commande a besoin.
    version = trace.get("python")
    uv = paths.uv_exe()
    if version and uv:
        dossier = dossier_python_uv(uv, str(version))
        ajouter("python", f"Python {version}", dossier,
                "Installé par le lanceur. Retiré par uv lui-même, qui ne "
                "touche à aucun autre Python.",
                commande=(uv, "python", "uninstall", str(version)))

    # Le cache de uv, mais seulement quand uv est a nous : il ne contient alors
    # que ce que nous lui avons fait telecharger. Vide par uv lui-meme, jamais
    # en effacant son dossier — qui contient aussi ses Pythons.
    if trace.get("uv") and uv:
        ajouter("cache-uv", "Téléchargements de uv", dossier_cache_uv(uv),
                "Les archives que uv a téléchargées pour installer les "
                "bibliothèques. Vidé par uv lui-même.",
                commande=(uv, "cache", "clean"))

    # Le binaire uv, et lui seul : ni son cache, ni ses Pythons, qui ne nous
    # appartiennent pas meme quand c'est nous qui avons pose le binaire.
    notre_uv = bool(trace.get("uv"))
    for binaire in _uv_binaires():
        ajouter("uv", f"Programme {binaire.name}", binaire,
                "Installé par le lanceur pour préparer Python."
                if notre_uv else
                "Il était déjà là avant l'installation : laissé en place.",
                notre_fait=notre_uv)

    return elements


@dataclass
class ALaMain:
    """Ce qu'on montre sans y toucher, avec de quoi s'en occuper soi-meme."""

    titre: str
    chemin: Path
    octets: int
    pourquoi: str
    #: La commande que l'utilisateur peut lancer, s'il en existe une.
    commande: str = ""

    def vue(self) -> dict:
        return {"titre": self.titre, "chemin": str(self.chemin),
                "octets": self.octets, "pourquoi": self.pourquoi,
                "commande": self.commande}


def laisses_a_l_utilisateur() -> list[ALaMain]:
    """Ce qui existe, qu'on a peut-etre fait grossir, et qu'on ne supprime pas.

    Le cache de uv, ses Pythons et le depot. Dans tous les cas la suppression
    deborderait de notre perimetre : les deux premiers sont partages avec tout
    ce qui utilise uv sur cette machine, le dernier est le dossier de
    l'utilisateur.

    Le cache et les Pythons sont deux dossiers distincts (``uv cache dir`` et
    ``~/.local/share/uv`` hors Windows) : ``uv cache clean`` ne vide que le
    premier. Les confondre annoncait la taille de l'un a cote de la commande
    de l'autre.
    """
    dehors: list[ALaMain] = []

    # Quand uv est a nous, `inventaire` vide deja son cache.
    uv = paths.uv_exe()
    cache = (dossier_cache_uv(uv)
             if uv and not pose_par_nous().get("uv") else None)
    if cache and cache.exists():
        dehors.append(ALaMain(
            "Cache de uv", cache, paths.taille_reelle(cache),
            "Les archives que uv a téléchargées, pour Caviardoc et d'autres "
            "programmes ; retéléchargées au besoin. Pour le vider vous-même :",
            "uv cache clean"))

    donnees_uv = _uv_donnees()
    if donnees_uv.exists():
        # Le Python que le lanceur a pose est repris par `inventaire`, par la
        # commande de uv : il n'est plus a retirer a la main.
        dehors.append(ALaMain(
            "Pythons de uv", donnees_uv,
            paths.taille_reelle(donnees_uv),
            "Les versions de Python que uv a installées, chacune dans son "
            "dossier ; d'autres programmes s'en servent peut-être. Pour en "
            "retirer une vous-même :",
            "uv python uninstall <version>"))

    racine = depot()
    dehors.append(ALaMain(
        "Le dossier du programme", racine, paths.taille_reelle(racine),
        "Celui d'où vous lancez Caviardoc. Supprimez-le vous-même, "
        "en dernier."))

    return dehors


def deja_presents() -> list[tuple[str, Path]]:
    """Ce dont l'outil se sert mais qui etait la avant lui, et qu'il laisse.

    Python, uv, Tesseract : quand l'un d'eux est a l'utilisateur, la
    desinstallation n'y touche pas, et il faut le dire — sans quoi on les voit
    toujours « installes » apres une desinstallation et l'on croit qu'elle a
    echoue.
    """
    trouves: list[tuple[str, Path]] = []
    trace = pose_par_nous()

    python = Path(getattr(sys, "_base_executable", sys.executable))
    try:
        chez_nous = python.resolve().is_relative_to(paths.data_dir().resolve())
    except (OSError, ValueError):
        chez_nous = False
    if not trace.get("python") and not chez_nous:
        v = sys.version_info
        trouves.append((f"Python {v.major}.{v.minor}.{v.micro}", python))

    uv = shutil.which("uv")
    if uv and not trace.get("uv"):
        trouves.append(("uv", Path(uv)))

    from . import tesseract
    systeme = tesseract.binaire()
    if systeme and tesseract.binaire_local() is None:
        trouves.append(("Tesseract", systeme))

    return trouves


def depot() -> Path:
    """Le depot lui-meme. Jamais supprime ici — on en donne le chemin."""
    return paths.repo_root()


def s_execute_depuis(element: Element) -> bool:
    """L'interpreteur courant vit-il dans ce qu'on s'apprete a supprimer ?

    Sous Windows, un fichier ouvert ne se supprime pas : la desinstallation
    echouerait a moitie. Ailleurs elle passerait, mais il vaut mieux le meme
    comportement partout qu'une surprise sur une seule plateforme.
    """
    try:
        Path(sys.prefix).resolve().relative_to(element.chemin.resolve())
    except (ValueError, OSError):
        return False
    return True


def interprete_exterieur() -> Path | None:
    """Un Python qui ne vit pas dans ce qu'on s'apprete a supprimer.

    La console tourne sous l'interpreteur du venv — celui du dossier de
    donnees. Elle ne peut donc pas se supprimer elle-meme : sous Windows un
    fichier ouvert ne s'efface pas, et ailleurs on scierait la branche en
    pleine operation.

    On cherche donc un interpreteur d'amorcage, celui-la meme que les lanceurs
    utilisent. Il n'a besoin d'aucune dependance : `desinstaller` n'efface que
    des dossiers.
    """
    donnees = paths.data_dir().resolve()

    def dehors(chemin: Path | None) -> bool:
        if chemin is None or not chemin.is_file():
            return False
        try:
            Path(sys.prefix if chemin == Path(sys.executable)
                 else chemin).resolve().relative_to(donnees)
        except (ValueError, OSError):
            return True
        return False

    candidats = [Path(sys.executable)]
    # Le Python dont le venv est issu : sur une machine ou le lanceur a
    # installe Python, il n'est pas dans le PATH, et c'etait le seul candidat.
    base = getattr(sys, "_base_executable", None)
    if base:
        candidats.append(Path(base))
    for nom in ("python3", "python"):
        trouve = shutil.which(nom)
        if trouve:
            candidats.append(Path(trouve))
    for candidat in candidats:
        if dehors(candidat):
            return candidat
    return None


def supprimer(elements: list[Element], essais: int = 1,
              pause: float = 1.0, progression=None) -> list[str]:
    """Supprime les elements donnes. Rend la liste des echecs, en clair.

    ``essais`` sert quand la suppression est lancee par la console : celle-ci
    tourne sous l'interpreteur du venv et vient de demander son propre arret.
    Sous Windows, un fichier encore ouvert ne s'efface pas — il faut laisser au
    processus le temps de rendre la main.

    ``progression(fait, total, titre)``, si donnee, est appelee avant chaque
    element (``titre`` est celui qu'on va traiter), puis une fois a la fin.
    """
    echecs: list[str] = []

    # Sous Windows, le Python qui execute cette desinstallation ne peut pas
    # effacer son propre dossier. Son retrait — et celui du binaire uv, dont la
    # commande a besoin — est alors confie a un processus detache, qui attend
    # que celui-ci se termine.
    python_en_service = next((e for e in elements if e.commande
                              and _interpreteur_dans(e.chemin)), None)
    differer = sys.platform == "win32" and python_en_service is not None
    binaires_uv = [e.chemin for e in elements if e.cle == "uv"] if differer else []

    for fait, element in enumerate(elements):
        if progression:
            progression(fait, len(elements), element.titre)
        if differer and (element is python_en_service or element.cle == "uv"):
            continue
        if element.commande:
            erreur = _lancer_commande(element)
            if erreur:
                echecs.append(f"{element.titre} ({element.chemin}) : {erreur}")
            continue
        dernier = ""
        for tentative in range(max(1, essais)):
            try:
                if element.chemin.is_dir() and not element.chemin.is_symlink():
                    shutil.rmtree(element.chemin)
                else:
                    element.chemin.unlink(missing_ok=True)
                dernier = ""
                break
            except OSError as exc:
                dernier = exc.strerror or str(exc)
                if tentative + 1 < essais:
                    time.sleep(pause)
        if dernier:
            echecs.append(f"{element.titre} ({element.chemin}) : {dernier}")

    if differer:
        erreur = _differer_retrait_python(python_en_service, binaires_uv)
        if erreur:
            echecs.append(f"{python_en_service.titre} : {erreur}")
    if progression:
        progression(len(elements), len(elements), "")
    return echecs


def _interpreteur_dans(dossier: Path) -> bool:
    """L'interpreteur de base du processus vit-il dans ``dossier`` ?

    ``sys.base_prefix`` plutot que ``sys.prefix`` : dans un venv, le second est
    le venv, alors que les fichiers que Windows garde ouverts sont ceux du
    Python dont le venv est issu.
    """
    try:
        Path(sys.base_prefix).resolve().relative_to(dossier.resolve())
    except (ValueError, OSError):
        return False
    return True


def _lancer_commande(element: Element) -> str:
    """Execute la commande de l'outil qui a pose l'element. Rend l'erreur, ou ''."""
    try:
        fini = subprocess.run(list(element.commande), capture_output=True,
                              **paths.TEXTE, timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if fini.returncode != 0:
        return (fini.stderr or fini.stdout).strip() or f"code {fini.returncode}"
    return ""


def _differer_retrait_python(element: Element, binaires: list[Path]) -> str:
    """Windows : retire Python, puis uv, une fois ce processus termine."""
    uv = element.commande[0]
    etapes = [" ".join(f'"{a}"' for a in element.commande)]
    etapes += [f'del /f /q "{b}"' for b in binaires]
    # ping sert de minuterie : `timeout` exige une console, que ce processus
    # detache n'a pas.
    ligne = "ping -n 8 127.0.0.1 >nul & " + " & ".join(etapes)
    try:
        subprocess.Popen(f'cmd /c "{ligne}"',
                         creationflags=0x00000008 | 0x08000000,  # DETACHED | NO_WINDOW
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    except OSError as exc:
        return (f"impossible de programmer le retrait ({exc}). "
                f"A la main : \"{uv}\" python uninstall {element.commande[-1]}")
    return ""
