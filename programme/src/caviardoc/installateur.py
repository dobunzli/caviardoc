"""Installation guidee, dans le navigateur, sur une machine neuve.

Ce module est le premier que rencontre un utilisateur qui n'ecrit pas de
commandes. Il doit donc fonctionner **avant** toute installation : rien ici
n'importe autre chose que la bibliotheque standard et les modules du projet
qui s'en contentent — `paths`, `doctor`, `models`, `web_commun`.

Chaque etape est une action nommee, avec son etat, sa progression et son
journal. Elles sont relancables une par une : un telechargement de 570 Mo
coupe par une perte de reseau ne doit pas obliger a tout recommencer, et
c'est le cas le plus frequent sur un poste de cabinet.

Plus rien n'est laisse a la charge de l'utilisateur. Python est pose par les
lanceurs avant que cette page existe ; les dependances, le modele et Tesseract
sont telecharges ici. L'etape « Python » ne fait donc plus que constater — mais
elle le constate quand meme, car on peut arriver ici par un interpreteur choisi
a la main.

Chaque etape annonce ce qu'elle va telecharger, d'ou et pour quel volume,
**avant** de le faire : l'utilisateur doit pouvoir refuser en connaissance de
cause, et ce qui est pose doit pouvoir etre repris (voir `desinstallation`).
"""

from __future__ import annotations

import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import doctor, materiel, models, paths, tesseract
from .web_commun import (HOTE, PoigneeBase, ServeurLocal, hotes_admis,
                         nouveau_jeton)


@dataclass
class Etape:
    """Une etape d'installation, relancable independamment des autres."""

    cle: str
    titre: str
    #: Ce que l'etape fait, et pourquoi elle compte. Affiche avant execution :
    #: l'utilisateur doit pouvoir refuser en connaissance de cause.
    explication: str
    #: True si l'etape agit ; False si elle ne fait que constater, l'action
    #: revenant a l'utilisateur (installer Tesseract).
    automatique: bool = True
    etat: str = "en_attente"     # en_attente | en_cours | ok | echec | manuelle
    message: str = ""
    remede: str = ""
    progression: float = 0.0
    journal: list[str] = field(default_factory=list)

    def noter(self, ligne: str) -> None:
        self.journal.append(f"{time.strftime('%H:%M:%S')}  {ligne}")

    def vue(self) -> dict:
        return {"cle": self.cle, "titre": self.titre,
                "explication": self.explication,
                "automatique": self.automatique, "etat": self.etat,
                "message": self.message, "remede": self.remede,
                "progression": round(self.progression, 3)}


@dataclass
class Installation:
    """L'ensemble des etapes, et le verrou qui empeche d'en lancer deux."""

    etapes: list[Etape]
    verrou: threading.Lock = field(default_factory=threading.Lock)
    en_cours: str = ""

    def par_cle(self, cle: str) -> Etape | None:
        return next((e for e in self.etapes if e.cle == cle), None)

    def vue(self) -> dict:
        faites = sum(1 for e in self.etapes if e.etat in ("ok", "manuelle"))
        return {"etapes": [e.vue() for e in self.etapes],
                "en_cours": self.en_cours,
                # Ce qui rend l'installation impossible sur cette machine : la
                # page grise alors les boutons et le dit.
                "bloquee": list(materiel.obstacles()),
                "faites": faites, "total": len(self.etapes),
                "pret": all(e.etat in ("ok", "manuelle") for e in self.etapes
                            if e.cle in ETAPES_INDISPENSABLES)}


#: Sans celles-ci, `caviardoc caviarder` ne demarre pas. Les autres degradent le
#: resultat sans l'empecher.
ETAPES_INDISPENSABLES = ("dependances", "modele")


# --------------------------------------------------------------------------- #
# Les etapes
# --------------------------------------------------------------------------- #

#: Ordre de grandeur de ce que `uv sync` verse dans son cache (archives
#: telechargees puis depaquetees). Sert seulement a graduer la barre, qui est
#: plafonnee : jamais on n'annonce une fin qui n'est pas la.
CACHE_ATTENDU = 2 * 1024 ** 3


def _taille_dossier(racine: Path) -> int:
    total = 0
    for dossier, _, fichiers in os.walk(racine):
        for nom in fichiers:
            try:
                total += os.lstat(os.path.join(dossier, nom)).st_size
            except OSError:
                pass
    return total


def _suivre_cache(uv: str, env: dict, etape: "Etape",
                  arret: threading.Event) -> None:
    """Fait avancer la barre d'une etape `uv sync` selon le cache de uv."""
    try:
        sortie = subprocess.run([uv, "cache", "dir"], env=env, **paths.TEXTE,
                                capture_output=True, timeout=20).stdout.strip()
        cache = Path(sortie)
        depart = _taille_dossier(cache) if cache.exists() else 0
    except (OSError, subprocess.SubprocessError):
        return
    etape.message = "Téléchargement des bibliothèques…"
    while not arret.wait(1.5):
        recu = max(0, _taille_dossier(cache) - depart)
        etape.progression = min(0.95, recu / CACHE_ATTENDU)
        if recu:
            etape.message = (f"Bibliothèques — {models.human(recu)} reçus "
                             f"(environ {models.human(CACHE_ATTENDU // 2)} au total)")

def _sonde(nom: str) -> doctor.Check | None:
    return next((ch for ch in doctor.run_all() if ch.name == nom), None)


def etape_python(etape: Etape) -> None:
    v = sys.version_info
    etape.noter(f"Python {v.major}.{v.minor}.{v.micro} — {sys.executable}")
    if v < (3, 11):
        etape.etat = "echec"
        etape.message = f"Python {v.major}.{v.minor} : il en faut 3.11 ou plus"
        # On y arrive par un interpreteur choisi a la main : les lanceurs,
        # eux, installent un Python convenable avant d'ouvrir cette page.
        etape.remede = ("Relancez l'installation par le fichier "
                        "« ▶ Ouvrir Caviardoc », en tête du dossier : "
                        "il installera un Python convenable.")
        return
    etape.etat = "ok"
    etape.message = f"Python {v.major}.{v.minor}.{v.micro}"
    etape.progression = 1.0


#: La version de uv que l'on pose, et son empreinte par plateforme. On ne passe
#: plus par le script « install.sh / install.ps1 » d'Astral, qui change sans
#: prevenir : on telecharge l'archive de cette version sur GitHub et on la
#: refuse si son SHA-256 differe. Les empreintes viennent des fichiers .sha256
#: publies par Astral avec la release 0.12.14 (github.com/astral-sh/uv). Les
#: memes valeurs sont dans tools/installer-uv.sh et tools/installer-uv.ps1 ; un
#: test les compare.
UV_VERSION = "0.12.14"

#: (sys.platform, architecture) -> (archive, SHA-256). Le choix est explicite :
#: une machine absente de cette table n'obtient pas de uv automatique.
UV_ASSETS = {
    ("darwin", "arm64"): (
        "uv-aarch64-apple-darwin.tar.gz",
        "dfed5683c5873d65eff8476eb93526059b81e5646a68305b5c94a26b29990894"),
    ("darwin", "x86_64"): (
        "uv-x86_64-apple-darwin.tar.gz",
        "e11a70264ceca38e929ecaeed953c323e517af7b960ebf9722d5918e8c7f0375"),
    ("win32", "x86_64"): (
        "uv-x86_64-pc-windows-msvc.zip",
        "02cecb254f37f3396da625680796d3990164a590fb5611d46f719ad83e7b9a89"),
    ("win32", "arm64"): (
        "uv-aarch64-pc-windows-msvc.zip",
        "46b930bdbb338099c7d9da711b751240eb97876eda6a553a2c28323967232041"),
    ("linux", "x86_64"): (
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "18ef5c3888ae59828cb13f38d57e9389b8173ecc719eff163bfafc74b38f5936"),
    ("linux", "arm64"): (
        "uv-aarch64-unknown-linux-gnu.tar.gz",
        "7fb91bd5d10529c60723eaec3caf44726f89280e5aeed78af8fc63fcad004c9b"),
}

_URL_UV = "https://github.com/astral-sh/uv/releases/download/"


def asset_uv(plateforme: str | None = None,
             machine: str | None = None) -> tuple[str, str] | None:
    """(archive, SHA-256) de uv pour cette machine, ou None si non fournie."""
    import platform
    plateforme = plateforme or sys.platform
    if plateforme.startswith("linux"):
        plateforme = "linux"
    architecture = (machine or platform.machine()).lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(
        architecture, architecture)
    return UV_ASSETS.get((plateforme, architecture))


def url_uv(archive: str) -> str:
    return f"{_URL_UV}{UV_VERSION}/{archive}"


def empreinte_conforme(octets: bytes, attendue: str) -> bool:
    import hashlib
    return hashlib.sha256(octets).hexdigest() == attendue.lower()


def _extraire_uv(octets: bytes, archive: str, destination: Path) -> None:
    """Pose `uv` (et `uvx`) depuis l'archive verifiee, par leur seul nom."""
    import io
    import tarfile
    import zipfile
    destination.mkdir(parents=True, exist_ok=True)
    noms = ("uv.exe", "uvx.exe") if archive.endswith(".zip") else ("uv", "uvx")
    if archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(octets)) as z:
            membres = {Path(n).name: n for n in z.namelist()}
            contenus = {n: z.read(membres[n]) for n in noms if n in membres}
    else:
        with tarfile.open(fileobj=io.BytesIO(octets), mode="r:gz") as t:
            contenus = {}
            for m in t.getmembers():
                if m.isfile() and Path(m.name).name in noms:
                    contenus[Path(m.name).name] = t.extractfile(m).read()
    if noms[0] not in contenus:
        raise ValueError("l'archive ne contient pas uv")
    for nom, contenu in contenus.items():
        cible = destination / nom
        cible.write_bytes(contenu)
        cible.chmod(0o755)


def _version_uv(uv: str) -> tuple[int, ...] | None:
    """La version d'un uv deja present, ou None si elle ne se lit pas."""
    import re
    try:
        sortie = subprocess.run([uv, "--version"], capture_output=True,
                                text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", sortie or "")
    return tuple(int(x) for x in m.groups()) if m else None


#: A la main, quand l'installation automatique echoue.
REMEDE_UV = {
    "darwin": "brew install uv",
    "win32": "winget install astral-sh.uv",
}.get(sys.platform, "https://docs.astral.sh/uv/getting-started/installation/")


def etape_uv(etape: Etape) -> None:
    """Pose uv s'il manque, comme le font les lanceurs.

    Un Python deja present sur la machine fait sauter l'installation de uv par
    les lanceurs, qui ne s'en servent que pour en obtenir un. L'etape suivante,
    elle, en a besoin : l'application renvoyait alors l'utilisateur taper une
    commande a la main, dans un terminal, pour une installation censee se
    faire en un double-clic.

    La version posee est epinglee (`UV_VERSION`) et son SHA-256 verifie avant
    toute extraction.
    """
    uv = paths.uv_exe()
    if uv:
        etape.noter(f"uv trouve : {uv}")
        # Un uv deja la est conserve (il peut etre celui de l'utilisateur),
        # mais un plus ancien que la version epinglee est signale : il peut ne
        # pas lire le uv.lock, et `uv sync --locked` echouera alors clairement.
        version = _version_uv(uv)
        epingle = tuple(int(x) for x in UV_VERSION.split("."))
        if version and version < epingle:
            etape.noter("attention : ce uv est plus ancien que la version "
                        f"{UV_VERSION} prévue ; mettez-le à jour si "
                        "l'installation des bibliothèques échoue")
        etape.etat = "ok"
        etape.message = "installé"
        etape.progression = 1.0
        return

    def echouer(message: str) -> None:
        etape.message = message
        etape.remede = (f"Installez-le à la main : {REMEDE_UV} — puis fermez "
                        "cette page, rouvrez « ▶ Ouvrir Caviardoc » et "
                        "relancez l'étape.")
        etape.etat = "echec"

    import urllib.request

    asset = asset_uv()
    if asset is None:
        echouer("uv n'est pas fourni automatiquement pour cet ordinateur")
        return
    archive, attendue = asset
    url = url_uv(archive)
    etape.noter(f"uv introuvable : téléchargement de {url}")
    etape.message = "Téléchargement de uv…"
    # Un User-Agent explicite, par prudence : certains serveurs refusent celui
    # par defaut de Python.
    requete = urllib.request.Request(url, headers={"User-Agent": "caviardoc"})
    try:
        with urllib.request.urlopen(requete, timeout=120) as reponse:
            octets = reponse.read()
    except (OSError, ValueError) as exc:
        etape.noter(f"échec du téléchargement : {exc}")
        echouer("Le téléchargement de uv a échoué : vérifiez la connexion")
        return
    etape.progression = 0.5

    if not empreinte_conforme(octets, attendue):
        etape.noter(f"SHA-256 attendu : {attendue}")
        echouer("uv refusé : l'empreinte SHA-256 du fichier téléchargé ne "
                f"correspond pas à celle attendue (version {UV_VERSION}). "
                "Rien n'a été installé")
        return

    etape.message = "Installation de uv…"
    try:
        _extraire_uv(octets, archive, Path.home() / ".local" / "bin")
    except Exception as exc:  # OSError, tarfile.TarError, zipfile.BadZipFile…
        etape.noter(f"échec de l'extraction : {exc}")
        echouer(f"L'installation de uv a échoué : {exc}")
        return
    uv = paths.uv_exe()
    if not uv:
        echouer("uv installé, mais introuvable")
        return

    # Retenu pour la desinstallation : ce uv est a nous, un autre ne l'est pas.
    from . import desinstallation
    desinstallation.noter_installe("uv")
    etape.noter(f"uv {UV_VERSION} installé : {uv}")
    etape.etat = "ok"
    etape.message = "installé"
    etape.progression = 1.0


#: Ce qu'il faut pouvoir importer pour caviarder : le moteur, et la couche
#: document qui ouvre les fichiers avant toute detection.
MODULES_REQUIS = ("onnxruntime", "gliner", "pymupdf", "docx", "PIL",
                  "pytesseract", "zxingcpp")


def etape_dependances(etape: Etape) -> None:
    """Installe le moteur et la couche document dans le venv local.

    Les deux extras, et non le seul moteur : `caviarder` ouvre des fichiers
    avant de detecter quoi que ce soit.
    """
    uv = paths.uv_exe()
    if not uv:
        etape.etat = "echec"
        etape.message = "uv est absent : installez-le d'abord"
        return

    venv = paths.venv_dir()
    etape.noter(f"environnement : {venv}")
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv))
    # Voir `maj.extras_sync` : `uv sync` elague, et cette etape faisait
    # disparaitre l'extra « dev » des machines qui l'avaient.
    from .maj import extras_sync
    cmd = [uv, "sync", "--locked", *extras_sync()]
    etape.noter("$ " + " ".join(cmd))

    processus = subprocess.Popen(cmd, cwd=paths.project_root(), env=env,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, **paths.TEXTE)
    # uv n'ecrit presque rien quand sa sortie n'est pas un terminal : il
    # reste muet pendant tout le telechargement. On mesure donc ce qu'il
    # verse dans son cache, ce qui donne a l'utilisateur un chiffre qui bouge.
    arret = threading.Event()
    suivi = threading.Thread(target=_suivre_cache,
                             args=(uv, env, etape, arret), daemon=True)
    suivi.start()
    for ligne in processus.stdout:
        ligne = ligne.rstrip()
        if ligne:
            etape.noter(ligne)
    code = processus.wait()
    arret.set()
    suivi.join(timeout=5)

    if code != 0:
        etape.etat = "echec"
        etape.message = f"uv sync --locked a échoué (code {code})"
        etape.remede = ("Les bibliothèques doivent correspondre exactement au "
                        "fichier uv.lock : si le journal parle d'un fichier "
                        "uv.lock à jour, mettez Caviardoc à jour ; sinon "
                        "vérifiez la connexion réseau et relancez cette "
                        "étape. Le détail est dans le journal.")
        return

    # Deux questions distinctes, qu'un seul « import » avec un delai d'une
    # minute confondait : le paquet est-il la, et se charge-t-il ? Sur une
    # machine occupee, le premier chargement de gliner depassait la minute,
    # et l'etape annoncait « introuvable » un paquet bel et bien installe.
    python = doctor._venv_python(venv)
    modules = MODULES_REQUIS
    manquants = [m for m in modules if not doctor._module_present(m, python)]
    if manquants:
        etape.etat = "echec"
        etape.message = "absentes après installation : " + ", ".join(manquants)
        if "zxingcpp" in manquants:
            etape.message += (" (zxingcpp : lecture des codes-barres, sans "
                              "laquelle Caviardoc refuse tout document)")
        etape.remede = ("Relancez cette étape. Si l'erreur persiste, le "
                        "détail est dans le journal.")
        return

    etape.noter("chargement d'essai — jusqu'à quelques minutes la première fois")
    reussi, cause = doctor.essai_import(modules, python)
    if not reussi:
        etape.noter(cause)
        etape.etat = "echec"
        etape.message = "installées, mais ne se chargent pas : " + cause
        etape.remede = ("Relancez cette étape : un premier chargement peut "
                        "échouer sur une machine très occupée. Si l'erreur "
                        "revient, le détail est dans le journal.")
        return

    etape.etat = "ok"
    etape.message = "installées"
    etape.progression = 1.0


def etape_modele(etape: Etape) -> None:
    """Telecharge le modele et verifie chaque fichier par son empreinte.

    C'est l'etape la plus longue et la plus fragile : plusieurs centaines de
    megaoctets sur un reseau de cabinet. Elle est donc relancable, et reprend
    ce qui manque plutot que tout retelecharger.
    """
    spec = models.REGISTRY[models.modele_actif()]
    etat = models.check(spec)
    if etat.complete:
        etape.noter(f"{spec.key} deja present et conforme")
        etape.etat = "ok"
        etape.message = f"installé ({spec.key})"
        etape.progression = 1.0
        return

    total = etat.missing_bytes
    etape.noter(f"{spec.key} : {models.human(total)} a telecharger depuis "
                f"{spec.repo}")
    faits = {"octets": 0}

    def avance(nom, lus, taille):
        # `lus` repart de zero a chaque fichier : on cumule nous-memes.
        faits["octets"] = max(faits["octets"], lus)
        etape.progression = min(0.99, faits["octets"] / total) if total else 0.0
        etape.message = (f"{nom.rsplit('/', 1)[-1]} — "
                         f"{models.human(faits['octets'])} sur "
                         f"{models.human(total)}")

    try:
        models.download(spec, on_progress=avance)
    except models.IntegrityError as exc:
        etape.noter(f"empreinte incorrecte : {exc}")
        etape.etat = "echec"
        etape.message = "le fichier téléchargé ne correspond pas à son empreinte"
        etape.remede = ("Rien n'a été installé. Relancez : un téléchargement "
                        "corrompu est le cas le plus fréquent. Si l'échec se "
                        "répète, le dépôt distant a peut-être changé.")
        return
    except models.DownloadError as exc:
        etape.noter(f"echec reseau : {exc}")
        etape.etat = "echec"
        etape.message = "téléchargement interrompu"
        etape.remede = ("Vérifiez la connexion, puis relancez cette étape : "
                        "elle reprend ce qui manque, sans tout recommencer.")
        return

    etape.etat = "ok"
    etape.message = f"téléchargé et contrôlé ({spec.key})"
    etape.progression = 1.0


def etape_tesseract(etape: Etape) -> None:
    """Installe Tesseract dans le dossier de l'application, s'il manque.

    Cette etape agissait autrefois par la parole : elle constatait l'absence et
    affichait `brew install`. Sur un poste de cabinet, cette commande echoue
    faute d'Homebrew ou faute de droits, et l'installation s'arretait la.

    On telecharge donc, comme pour le modele : rien n'est ajoute au PATH, rien
    n'est touche hors du dossier de l'application, et aucun mot de passe n'est
    demande. Un Tesseract systeme deja pourvu du francais est laisse en place.
    """
    exe = tesseract.binaire()
    deja = _constat_tesseract()
    if deja:
        etape.noter(f"{tesseract.version()} — {exe}")
        etape.etat, etape.message = deja
        etape.progression = 1.0
        return

    if exe is not None:
        etape.noter(f"Tesseract present ({exe}) mais sans le francais : "
                    "on installe notre propre copie a cote, sans y toucher.")

    etape.progression = 0.02
    lues = {"n": 0}

    def avance(nom, lus, total):
        etape.message = f"{nom} — {models.human(lus)}"
        # micromamba pese peu : cette part de la barre reste petite, la suite
        # est mesuree en lignes de journal.
        etape.progression = 0.02 + (0.08 * lus / total if total else 0)

    def journal(ligne):
        etape.noter(ligne)
        lues["n"] += 1
        # micromamba n'annonce pas de total : la progression est indicative, et
        # plafonnee pour ne jamais annoncer une fin qui n'est pas la.
        etape.progression = min(0.95, 0.10 + lues["n"] / 90)

    try:
        tesseract.installer(on_progress=avance, on_ligne=journal)
    except models.IntegrityError as exc:
        etape.noter(str(exc))
        etape.etat = "echec"
        etape.message = "le fichier téléchargé ne correspond pas à son empreinte"
        etape.remede = ("Rien n'a été installé. Relancez : un téléchargement "
                        "corrompu est le cas le plus fréquent.")
        return
    except models.DownloadError as exc:
        etape.noter(str(exc))
        etape.etat = "echec"
        etape.message = "téléchargement interrompu"
        etape.remede = ("Vérifiez la connexion, puis relancez cette étape.")
        return
    except tesseract.InstallationImpossible as exc:
        etape.noter(str(exc))
        etape.etat = "echec"
        etape.message = str(exc).splitlines()[0]
        etape.remede = ("Les documents scannés ne seront pas lisibles ; le "
                        "reste fonctionne. Vous pouvez relancer cette étape "
                        "plus tard.")
        return

    etape.noter(f"{tesseract.version()} — {tesseract.prefixe()}")
    etape.etat = "ok"
    etape.message = "installé, avec le français"
    etape.progression = 1.0


def etape_mesure(etape: Etape) -> None:
    """Mesure ce que vaut le modele sur CETTE machine.

    Ce n'est pas une verification de forme. Le modele quantifie rend des
    resultats dependants du processeur : sur le corpus de regression, une
    machine rend 97 % de rappel et une autre 62 %. Tant que ce defaut n'est
    pas resolu, le seul chiffre auquel se fier est celui obtenu ici.
    """
    outil = paths.project_root() / "tools" / "mesurer.py"
    if not outil.is_file():
        etape.etat = "manuelle"
        etape.message = "outil de mesure introuvable"
        return

    python = doctor._venv_python(paths.venv_dir())
    if not python.is_file():
        etape.etat = "echec"
        etape.message = "installez d'abord les dépendances"
        return

    etape.noter(f"$ {python} -u {outil} --enregistrer")
    etape.progression = 0.1
    # `-u` : sans cela, la sortie d'un processus relie a un tube est tamponnee
    # et n'arrive qu'a la fin. Sur un ordinateur lent la mesure dure des
    # minutes, pendant lesquelles la page n'affichait rien — impossible de dire
    # si elle avancait ou etait bloquee. `stdin` ferme : le processus n'a aucune
    # raison d'attendre une saisie.
    processus = subprocess.Popen(
        [str(python), "-u", str(outil), "--enregistrer"],
        cwd=paths.project_root(),
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")

    # Le temps ecoule, pour qu'une mesure lente se distingue d'une mesure
    # bloquee : le chiffre avance tant que le processus vit.
    depart = time.time()
    fini = threading.Event()

    def compter():
        while not fini.wait(2):
            ecoule = int(time.time() - depart)
            etape.message = (f"Mesure en cours depuis {ecoule // 60} min "
                             f"{ecoule % 60:02d} s — plusieurs minutes sont "
                             "normales sur un ordinateur sans carte graphique")

    compteur = threading.Thread(target=compter, daemon=True)
    compteur.start()
    rappels: dict[str, float] = {}
    dernieres: list[str] = []
    try:
        for ligne in processus.stdout:
            ligne = ligne.rstrip()
            if not ligne:
                continue
            etape.noter(ligne)
            dernieres = (dernieres + [ligne])[-6:]
            etape.progression = min(0.95, etape.progression + 0.02)
            for nom in ("rappel_complet", "rappel_patient"):
                if nom in ligne:
                    try:
                        rappels[nom] = float(ligne.split("%")[0].split()[-1])
                    except (ValueError, IndexError):
                        pass
        processus.wait()
    finally:
        # Le compteur s'arrete avant que le resultat soit ecrit : sans cela il
        # pouvait ecraser « reussie » par un temps ecoule.
        fini.set()
        compteur.join(timeout=5)
    etape.noter(f"mesure terminée en {int(time.time() - depart)} s "
                f"(code {processus.returncode})")

    if not rappels:
        etape.message = ("la mesure n'a rien rendu"
                         + (f" (le processus s'est arrêté, code "
                            f"{processus.returncode})"
                            if processus.returncode else ""))
        # La fin de sa sortie : c'est presque toujours la cause (un module qui
        # ne se charge pas, une bibliotheque systeme absente). Sans cela, il
        # fallait ouvrir le journal pour la lire.
        fin = "\n".join(dernieres)
        etape.remede = ("Dernières lignes du processus :\n" + fin + "\n\n"
                        if fin else "") + "Consultez le journal, puis relancez."
        etape.etat = "echec"
        return

    pire = min(rappels.values())
    etape.progression = 1.0
    if pire < 90:
        etape.etat = "manuelle"
        etape.message = (f"insuffisante : {_pourcent(pire)} des données de "
                         "test repérées, 90 % requis")
        # Deux situations sans rien de commun : un modele autre que celui
        # conseille pour cette machine (on peut basculer), ou le modele
        # conseille lui-meme (une machine que personne n'a vue).
        if models.modele_actif() != models.DEFAULT_MODEL:
            etape.remede = (
                "Ce n'est pas le modèle conseillé pour cet ordinateur. "
                "Basculez dessus :\n"
                f"    caviardoc setup --modele {models.DEFAULT_MODEL}\n"
                "puis relancez cette mesure. Ne confiez pas de document à "
                "l'outil avant que ce chiffre dépasse 90 %.")
        else:
            etape.remede = (
                "Vous êtes déjà sur le modèle conseillé pour cet ordinateur, "
                "qui rendait le même chiffre sur toutes les machines mesurées "
                "jusqu'ici. Un "
                "résultat bas ici est une découverte, pas un réglage à "
                "corriger : signalez-le, avec le journal détaillé et le "
                "modèle de votre processeur.\n"
                "N'utilisez pas l'outil sur des documents réels tant que ce "
                "chiffre n'a pas dépassé 90 %.")
    else:
        etape.etat = "ok"
        etape.message = f"réussie : {_pourcent(pire)} des données de test repérées"


def etapes_par_defaut() -> Installation:
    """Les etapes, avec une explication d'une ligne.

    Une ligne, et pas davantage : cette page se lit debout, pendant que ca
    telecharge. Le detail qui compte vraiment est dans le journal.
    """
    return Installation(etapes=[
        Etape("python", "Python",
              "Le langage dans lequel l'outil est écrit. Le lanceur "
              "l'installe s'il manque ; ici, on vérifie seulement sa version.",
              automatique=False),
        Etape("uv", "uv",
              "Le programme qui installe les bibliothèques. S'il manque, il "
              "est téléchargé depuis github.com/astral-sh/uv (SHA-256 "
              "vérifié), sans droits administrateur.",
              automatique=True),
        Etape("dependances", "Bibliothèques",
              "Les composants qui lisent les documents et repèrent les "
              "données. Environ 1 Go, rangé à part : le reste de l'ordinateur "
              "n'est pas modifié.",
              automatique=True),
        Etape("modele", "Modèle de détection",
              "Le modèle qui repère les noms et les adresses ("
              f"{models.human(models.REGISTRY[models.modele_actif()].total_size)}"
              "). Téléchargé une seule fois et contrôlé ; ensuite, tout se "
              "passe sur cet ordinateur.",
              automatique=True),
        Etape("tesseract", "Lecture des documents scannés",
              "Tesseract, pour lire les PDF scannés ("
              f"{models.human(tesseract.TELECHARGEMENT_ESTIME)}). S'installe "
              "sans mot de passe administrateur.",
              automatique=True),
        Etape("mesure", "Mesure sur cette machine",
              "Vérifie sur des documents de test que la détection fonctionne "
              "bien sur cet ordinateur : il faut repérer au moins 90 % des "
              "données.",
              automatique=True),
    ])


# --------------------------------------------------------------------------- #
# Le constat au demarrage
# --------------------------------------------------------------------------- #

def _pourcent(valeur: float) -> str:
    """« 95,0 % » : la virgule decimale de l'interface, pas le point du code."""
    return f"{valeur:.1f}".replace(".", ",") + " %"


def _constat_python():
    v = sys.version_info
    return ("ok", f"Python {v.major}.{v.minor}.{v.micro}") if v >= (3, 11) else None


def _constat_uv():
    return ("ok", "installé") if paths.uv_exe() else None


def _constat_dependances():
    # Presence seulement, sans import : charger gliner prend des secondes, et
    # ce constat retarderait l'affichage de chaque lancement.
    python = doctor._venv_python(paths.venv_dir())
    if all(doctor._module_present(m, python) for m in MODULES_REQUIS):
        return "ok", "installées"
    return None


def _constat_modele():
    spec = models.REGISTRY[models.modele_actif()]
    return ("ok", f"installé ({spec.key})") if models.check(spec).complete else None


def _constat_tesseract():
    exe = tesseract.binaire()
    if exe is None or "fra" not in tesseract.langues():
        return None
    return "ok", "installé, avec le français"


def _constat_mesure():
    """La derniere mesure, si elle porte sur le modele en service.

    Une mesure d'un autre modele ne dit rien de celui-ci : l'etape reste alors
    a faire, plutot que d'afficher un chiffre qui ne le concerne pas.
    """
    from .maj import dernier_rappel

    mesure = models.derniere_mesure() or {}
    if mesure.get("modele") != models.modele_actif():
        return None
    rappel = dernier_rappel()
    if rappel is None:
        return None
    annee, mois, jour = (mesure.get("date", "").split("-") + ["", "", ""])[:3]
    quand = f", le {jour}.{mois}.{annee}" if annee and mois and jour else ""
    if rappel < 90:
        return "manuelle", (f"insuffisante : {_pourcent(rappel)} des données de "
                            f"test repérées, 90 % requis{quand}")
    return "ok", f"réussie : {_pourcent(rappel)} des données de test repérées{quand}"


CONSTATS = {
    "python": _constat_python,
    "uv": _constat_uv,
    "dependances": _constat_dependances,
    "modele": _constat_modele,
    "tesseract": _constat_tesseract,
    "mesure": _constat_mesure,
}


def constater(installation: Installation) -> None:
    """Coche ce qui est deja en place, sans rien installer ni telecharger.

    Les etapes ne vivent qu'en memoire : a chaque lancement elles repartaient
    toutes « en attente », et une machine entierement installee montrait six
    cercles vides — avec, pour seul moyen de les cocher, un « Tout installer »
    qui relancait `uv sync` et plusieurs minutes de mesure pour apprendre ce
    qu'on savait deja.

    Chaque constat est leger : un fichier, un module, une mesure enregistree.
    Une etape dont rien ne prouve qu'elle est faite reste en attente, jamais
    en echec : elle n'a pas ete tentee.
    """
    with installation.verrou:
        # Les boutons restent grises le temps du constat : une etape lancee
        # pendant ce temps verrait son etat ecrase par lui.
        installation.en_cours = "constat"
        try:
            for etape in installation.etapes:
                if etape.etat != "en_attente" or etape.cle not in CONSTATS:
                    continue
                try:
                    trouve = CONSTATS[etape.cle]()
                except Exception as exc:
                    etape.noter(f"constat impossible : {type(exc).__name__}: {exc}")
                    continue
                if trouve:
                    etape.etat, etape.message = trouve
                    etape.progression = 1.0
                    etape.noter(f"deja en place au demarrage : {etape.message}")
                    if etape.etat == "manuelle":
                        etape.remede = ("Relancez cette étape pour le détail "
                                        "et la marche à suivre.")
        finally:
            installation.en_cours = ""


def constater_en_fond(installation: Installation) -> None:
    """`constater` dans un fil : la page s'affiche sans l'attendre, et le
    rafraichissement periodique montre les coches des qu'elles sont posees."""
    threading.Thread(target=constater, args=(installation,), daemon=True).start()


ACTIONS = {
    "python": etape_python,
    "uv": etape_uv,
    "dependances": etape_dependances,
    "modele": etape_modele,
    "tesseract": etape_tesseract,
    "mesure": etape_mesure,
}


def executer(installation: Installation, cle: str) -> Etape:
    """Lance une etape dans un fil, et rend l'etape aussitot."""
    etape = installation.par_cle(cle)
    if etape is None:
        raise KeyError(cle)
    action = ACTIONS.get(cle)
    if action is None:
        raise KeyError(cle)

    # Sur une machine ou les bibliotheques n'existent pas (Mac Intel, macOS
    # trop ancien), rien ne se lance : l'etape echouerait apres un long
    # telechargement. Cote serveur, et non seulement par un bouton grise.
    impossibles = materiel.obstacles()
    if impossibles:
        etape.noter("installation impossible : " + " ".join(impossibles))
        etape.message = impossibles[0]
        etape.remede = ("Cet ordinateur ne peut pas faire tourner Caviardoc"
                        ". Rien n'a été installé.")
        etape.etat = "echec"
        return etape

    def travailler():
        with installation.verrou:
            installation.en_cours = cle
            etape.etat = "en_cours"
            etape.message = ""
            etape.remede = ""
            etape.progression = 0.0
            etape.noter(f"--- {etape.titre} ---")
            try:
                action(etape)
            except Exception as exc:
                import traceback
                # Le journal et le message d'abord, l'etat en dernier : la page
                # interroge en boucle, et publier « echec » avant d'avoir ecrit
                # la cause lui laisse afficher un echec sans explication.
                etape.noter(traceback.format_exc())
                etape.message = f"{type(exc).__name__}: {exc}"
                etape.etat = "echec"
            finally:
                installation.en_cours = ""

    threading.Thread(target=travailler, daemon=True).start()
    return etape


# --------------------------------------------------------------------------- #
# La page
# --------------------------------------------------------------------------- #

_STYLE = """
:root{--fond:#f4f6f7;--carte:#fff;--encre:#15181c;--gris:#5d6672;
--trait:#dfe4e9;--accent:#0f5c63;--accent-clair:#e7f0f0;--alerte:#9e2b2b;
--ok:#256b4a;--attente:#8a5a11}
*{box-sizing:border-box}
body{margin:0;background:var(--fond);color:var(--encre);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}

header{padding:18px 22px 14px}
h1{font-size:1.2rem;margin:0 0 3px;font-weight:600;letter-spacing:-.01em}
.sous{color:var(--gris);font-size:.86rem}

/* La barre d'action tient en haut : le bouton principal se trouvait sous la
   ligne de flottaison, il fallait faire defiler pour commencer. */
.actions{position:sticky;top:0;z-index:5;background:var(--carte);
border-top:1px solid var(--trait);border-bottom:1px solid var(--trait)}
.actions-int{max-width:58rem;margin:0 auto;padding:11px 22px;display:flex;
gap:12px;align-items:center;flex-wrap:wrap}
.patience{max-width:58rem;margin:0 auto;padding:0 22px 10px;
font-size:.86rem;color:var(--gris)}
.patience[hidden]{display:none}
.jauge-globale{flex:1;min-width:90px;height:4px;background:var(--trait);
border-radius:2px;overflow:hidden}
.jauge-globale div{height:100%;width:0;background:var(--accent);
transition:width .4s}
.actions .sous{white-space:nowrap}

main{max-width:58rem;margin:0 auto;padding:20px 22px 40px;display:grid;
grid-template-columns:1fr;gap:18px;align-items:start}
@media(min-width:62rem){main{grid-template-columns:1.1fr .9fr}}
section{background:var(--carte);border:1px solid var(--trait);border-radius:6px;
padding:16px 18px}
h2{font-size:.74rem;text-transform:uppercase;letter-spacing:.09em;
color:var(--gris);margin:0 0 12px;font-weight:600}
h3{font-size:.92rem;margin:16px 0 5px;font-weight:600}
h3:first-of-type{margin-top:0}
p{margin:0 0 9px}

.etape{border-top:1px solid var(--trait);padding:11px 0}
.etape:first-of-type{border-top:0;padding-top:0}
.ligne{display:flex;gap:9px;align-items:baseline}
.pastille{width:1.2rem;text-align:center;font-weight:700;flex:none}
.pastille.ok{color:var(--ok)}
.pastille.echec{color:var(--alerte)}
.pastille.manuelle{color:var(--attente)}
.pastille.en_cours{color:var(--accent)}
.titre{font-weight:600;flex:1}
.explication{color:var(--gris);font-size:.85rem;margin:2px 0 0 2.1rem}
.message{font-size:.85rem;margin:5px 0 0 2.1rem}
.message.echec{color:var(--alerte);font-weight:600}
.remede{margin:7px 0 0 2.1rem;background:#fff8e6;border:1px solid #e6d2a0;
border-radius:4px;padding:8px 11px;font-size:.83rem;white-space:pre-wrap}
.jauge{height:4px;background:var(--trait);border-radius:2px;overflow:hidden;
margin:7px 0 0 2.1rem}
.jauge div{height:100%;background:var(--accent);width:0;transition:width .3s}

button{font:inherit;padding:7px 15px;border:1px solid var(--trait);
background:var(--carte);border-radius:5px;cursor:pointer;color:inherit}
button:hover:not(:disabled){border-color:var(--gris)}
button.primaire{background:var(--accent);color:#fff;border-color:var(--accent);
font-weight:600}
button.primaire:hover:not(:disabled){background:#0c4b51}
button.petit{padding:3px 9px;font-size:.81rem}
button:disabled{opacity:.4;cursor:default}
a{color:var(--accent)}

.doc{font-size:.88rem;line-height:1.5}
.doc ol{padding-left:1.15rem;margin:0 0 10px}
.doc li{margin-bottom:5px}
.doc dl{margin:0 0 10px}
.doc dt{font-weight:600;margin-top:7px}
.doc dd{margin:1px 0 0;color:var(--gris)}
.franc{background:var(--accent-clair);border-left:3px solid var(--accent);
padding:9px 12px;margin:10px 0 0;font-size:.85rem}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace;
background:#eef1f3;padding:1px 4px;border-radius:3px}
.pied{max-width:58rem;margin:0 auto;padding:0 22px 28px;font-size:.78rem;
color:var(--gris);text-align:center}
.pied a{color:inherit}
"""


_SCRIPT = """
const PASTILLES = {en_attente:"\\u25cb", en_cours:"\\u25d0", ok:"\\u2713",
                   echec:"\\u2717", manuelle:"!"};
function entetes(extra){
  return Object.assign({'X-Caviardoc-Jeton': JETON}, extra || {});
}
let relance = false;
// Le redemarrage n'a pas abouti : le message reste affiche jusqu'au prochain essai.
let abandon = false;
let texteAttente = '';
async function rafraichir(){
  const r = await fetch('/etapes', {headers: entetes()});
  const vue = await r.json();
  peindre(vue);
  if (vue.redemarrage && !vue.en_cours && !relance) redemarrer();
  return vue;
}
// Le moteur est installe, mais ce processus ne peut pas le charger : il
// repart sous l'interpreteur de l'application, puis la page se recharge.
async function redemarrer(){
  relance = true;
  abandon = false;
  const p = document.getElementById('patience');
  if (!texteAttente) texteAttente = p.textContent;
  p.textContent = 'Finalisation, un instant…';
  p.hidden = false;
  await fetch('/redemarrer', {method:'POST', headers: entetes()}).catch(() => {});
  // Dix minutes : le premier lancement de l'interpreteur du moteur (torch,
  // analyse de l'antivirus) peut etre tres long, surtout sous Windows.
  for (let i = 0; i < 600; i++){
    await new Promise(r => setTimeout(r, 1000));
    try {
      const r = await fetch('/etapes', {headers: entetes()});
      if (r.ok && !(await r.json()).redemarrage){ location.reload(); return; }
    } catch (e) { /* le serveur redemarre */ }
  }
  // Le serveur n'est pas revenu : on n'attend plus, et la reprise n'a plus lieu.
  abandon = true;
  p.textContent = "Le redémarrage prend trop de temps. Cliquez de nouveau sur " +
    "« Tout installer » pour poursuivre.";
  p.hidden = false;
  document.getElementById('tout').disabled = false;
  relance = false;
  enMarche = false;
  retenirTout(false);
}
function peindre(vue){
  const hote = document.getElementById('etapes');
  hote.innerHTML = '';
  for (const e of vue.etapes){
    const bloc = document.createElement('div');
    bloc.className = 'etape';

    const ligne = document.createElement('div');
    ligne.className = 'ligne';
    const past = document.createElement('span');
    past.className = 'pastille ' + e.etat;
    past.textContent = PASTILLES[e.etat] || '\\u25cb';
    const titre = document.createElement('span');
    titre.className = 'titre'; titre.textContent = e.titre;
    ligne.append(past, titre);

    if (e.etat !== 'en_cours'){
      const b = document.createElement('button');
      b.className = 'petit';
      b.textContent = e.etat === 'en_attente' ? 'Lancer' : 'Relancer';
      b.disabled = !!vue.en_cours;
      b.onclick = () => lancer(e.cle);
      ligne.append(b);
    }
    bloc.append(ligne);

    const exp = document.createElement('div');
    exp.className = 'explication'; exp.textContent = e.explication;
    bloc.append(exp);

    if (e.etat === 'en_cours'){
      const j = document.createElement('div');
      j.className = 'jauge';
      const d = document.createElement('div');
      d.style.width = (e.progression*100) + '%';
      j.append(d); bloc.append(j);
    }
    if (e.message){
      const m = document.createElement('div');
      m.className = 'message' + (e.etat === 'echec' ? ' echec' : '');
      m.textContent = e.message;
      bloc.append(m);
    }
    if (e.remede){
      const r = document.createElement('div');
      r.className = 'remede'; r.textContent = e.remede;
      bloc.append(r);
    }
    hote.append(bloc);
  }
  document.getElementById('avancement').textContent =
    `${vue.faites} / ${vue.total}`;
  document.getElementById('globale').style.width =
    (vue.total ? (vue.faites / vue.total) * 100 : 0) + '%';
  // Installation impossible sur cet ordinateur : on le dit, et on grise tout.
  const bloquee = (vue.bloquee || []).length > 0;
  let avis = document.getElementById('bloquee');
  if (bloquee){
    if (!avis){
      avis = document.createElement('div');
      avis.id = 'bloquee'; avis.className = 'remede'; avis.setAttribute('role', 'alert');
      hote.parentNode.insertBefore(avis, hote);
    }
    avis.textContent = 'Installation impossible sur cet ordinateur. ' + vue.bloquee.join(' ');
    for (const b of hote.querySelectorAll('button')) b.disabled = true;
  } else if (avis){
    avis.remove();
  }
  document.getElementById('tout').disabled = bloquee || !!vue.en_cours;
  document.getElementById('patience').hidden = !(vue.en_cours || enMarche || relance || abandon);
  // Pastille rouge de l'onglet : elle s'eteint des que l'installation est
  // complete, sauf si un nouveau modele est propose (data-propose, pose par
  // la console) — chacun ecrit son drapeau, la pastille lit les deux.
  const alerte = document.getElementById('alerte-inst');
  if (alerte){
    alerte.dataset.incomplet = vue.pret ? '' : '1';
    alerte.className = alerte.dataset.incomplet || alerte.dataset.propose
      ? 'pastille-inst' : '';
  }
  const ouvrir = document.getElementById('ouvrir');
  ouvrir.disabled = !vue.pret;
  ouvrir.title = vue.pret ? '' :
    "Les bibliothèques et le modèle doivent être installés d'abord.";
}
async function lancer(cle){
  await fetch('/lancer', {method:'POST',
    headers: entetes({'Content-Type':'application/json'}),
    body: JSON.stringify({cle: cle})});
  await suivre();
}
async function suivre(){
  for (;;){
    const vue = await rafraichir();
    if (!vue.en_cours) return vue;
    await new Promise(r => setTimeout(r, 500));
  }
}
let enMarche = false;
/* « Tout installer » doit survivre au redemarrage qui suit les bibliotheques :
   le serveur repart sous l'interpreteur de l'application et la page se
   recharge, ce qui efface cette boucle. Sans memoire, l'installation s'arretait
   la — modele, Tesseract et mesure restaient « a lancer ». L'intention est donc
   gardee pour la session de l'onglet, et reprise au rechargement. */
const MEMOIRE_TOUT = 'caviardoc-tout-installer';
function retenirTout(oui){
  try {
    if (oui) sessionStorage.setItem(MEMOIRE_TOUT, '1');
    else sessionStorage.removeItem(MEMOIRE_TOUT);
  } catch (e) { /* stockage indisponible : on perd seulement la reprise */ }
}
function toutEstEnCours(){
  try { return sessionStorage.getItem(MEMOIRE_TOUT) === '1'; }
  catch (e) { return false; }
}
async function tout(){
  if (enMarche) return;
  enMarche = true;
  retenirTout(true);
  if (abandon){
    abandon = false;
    document.getElementById('patience').textContent = texteAttente;
  }
  document.getElementById('tout').disabled = true;
  const vue = await rafraichir();
  for (const e of vue.etapes){
    if (e.etat === 'ok') continue;
    await lancer(e.cle);
    // La page va se recharger : ne pas lancer la suite sur un serveur qui
    // redemarre. La memoire reste posee, le rechargement reprendra.
    if (relance) return;
  }
  retenirTout(false);
  enMarche = false;
  const fin = await rafraichir();
  // Le bandeau « jamais mesurée » et la pastille de l'onglet sont posés au
  // chargement : une fois tout installé, la page se recharge (sur l'onglet
  // Installation, l'adresse le retient) pour les recalculer.
  if (fin.faites === fin.total && document.getElementById('bandeau-mesure'))
    location.reload();
}
async function ouvrirInterface(){
  const r = await fetch('/interface', {method:'POST', headers: entetes()});
  const d = await r.json();
  if (d.url) window.location = d.url;
  else alert("Impossible d'ouvrir l'interface : " + (d.erreur || '?'));
}
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('tout').onclick = tout;
  document.getElementById('ouvrir').onclick = ouvrirInterface;
  if (toutEstEnCours()) tout();
  else rafraichir();
  setInterval(() => { if (!document.hidden) rafraichir(); }, 3000);
});
"""

#: L'en-tete de la page autonome. La console a le sien.
_ENTETE = """
<header>
  <h1>Installation de Caviardoc</h1>
  <div class="sous">par D. Bünzli · Tout s'installe sur cette machine. Aucun document, aucune
  donnée de patient ne sortira d'ici.</div>
</header>
"""

#: La barre d'action, et la liste des etapes : les deux morceaux que la console
#: reprend tels quels. Les separer evite d'en tenir deux versions, qui
#: divergeraient au premier changement.
_ACTIONS = """
<div class="actions">
  <div class="actions-int">
    <button class="primaire" id="tout">Tout installer</button>
    <button id="ouvrir" disabled>Ouvrir Caviardoc</button>
    <div class="jauge-globale"><div id="globale"></div></div>
    <span class="sous" id="avancement"></span>
    <a href="JOURNAL" target="_blank">Journal détaillé</a>
  </div>
  <div class="patience" id="patience" hidden>Veuillez patienter, installation
  en cours. Cela peut prendre quelques minutes en fonction de votre connexion
  internet.</div>
</div>
"""

_ETAPES = """
<section>
  <h2>Étapes</h2>
  <div id="etapes"></div>
</section>
"""

#: Le texte explicatif, montre seulement par la page autonome : dans la console
#: il ferait doublon avec ce que dit deja l'onglet Caviardoc.
_DOC = """
<section class="doc">
  <h2>Pendant que ça installe</h2>

  <h3>Ce que fait Caviardoc</h3>
  <p>Il retire d'un document ce qui identifie une personne — nom, date de
  naissance, adresse, AVS, téléphone, IBAN — et rend un document encore
  lisible.</p>

  <h3>Comment</h3>
  <ol>
    <li><strong>Lecture.</strong> Un .docx directement. Un PDF par sa couche
    texte, ou par reconnaissance optique si c'est un scan.</li>
    <li><strong>Détection.</strong> Des règles pour ce qui a une forme fixe
    (AVS, IBAN, dates), un modèle local pour le reste (noms, adresses). Aucun
    modèle génératif.</li>
    <li><strong>Propagation.</strong> Un nom trouvé une fois est cherché
    partout : le manquer une seule fois suffirait à identifier la personne.</li>
    <li><strong>Relecture.</strong> Vous voyez et corrigez chaque détection
    avant qu'un pixel soit noirci.</li>
    <li><strong>Caviardage.</strong> Le PDF est réécrit en image, sans couche
    texte : rien à copier sous les rectangles. Le .docx reçoit des
    pseudonymes.</li>
    <li><strong>Vérification.</strong> Le document produit est relu, et
    l'outil signale ce qui aurait survécu.</li>
  </ol>

  <h3>Qui est masqué : vous choisissez</h3>
  <dl>
    <dt>Complet <span class="sous">— par défaut</span></dt>
    <dd>Toutes les personnes détectées sont masquées, soignants compris.</dd>
    <dt>Patient</dt>
    <dd>Seul le patient est masqué ; soignants et établissements restent
    lisibles. Si le patient n'est pas identifié avec certitude, l'outil
    revient au niveau complet plutôt que de laisser passer quelque chose.</dd>
  </dl>

  <h3>Ce qu'il faut savoir</h3>
  <div class="franc">Masquer les noms ne rend pas un document anonyme. Le
  contenu médical reste lisible, et un détail rare peut suffire à retrouver la
  personne, notamment pour une IA qui cherche sur internet. Décider si un
  document peut sortir du cabinet reste votre responsabilité.</div>
  <div class="franc">Aucun modèle ne détecte 100 % des données personnelles.
  Cet outil assiste le caviardage, il ne le garantit pas : une relecture
  humaine reste nécessaire.</div>
  <div class="franc">Le modèle est figé. Il n'apprend pas de vos corrections,
  et resoumettre un document ne changera rien.</div>
  <div class="franc">Le modèle est quantifié, et la quantification s'est déjà
  révélée dépendante du processeur : un format abandonné pour cette raison
  rendait 97 % de rappel sur une machine, 62 % sur une autre. L'étape de
  mesure vérifie la vôtre plutôt que de la supposer.</div>
</section>
"""

_CORPS = _ENTETE + _ACTIONS + "\n<main>\n" + _ETAPES + _DOC + "\n</main>\n"


def styles_confines(portee: str) -> str:
    """Les regles propres aux etapes, reecrites sous une portee donnee.

    La console affiche ces etapes dans un onglet, a cote de l'interface de
    travail. Or les deux feuilles definissent `.jauge` et `button.petit` avec
    des valeurs differentes : concatenees telles quelles, la derniere gagnerait
    et casserait la barre de progression de l'autre onglet.

    On ne recopie pas pour autant : les regles sont extraites d'ici meme, donc
    un changement de style suit dans les deux endroits.
    """
    debut = _STYLE.index(".etape{border-top")
    fin = _STYLE.index("button{font:inherit;padding:7px 15px")
    regles = _STYLE[debut:fin]
    # Egalement la barre d'action, qui n'existe que sur cette page.
    d2 = _STYLE.index(".actions{position:sticky")
    f2 = _STYLE.index("main{max-width:58rem")
    regles = _STYLE[d2:f2] + regles

    sorties = []
    for regle in regles.split("}"):
        if "{" not in regle:
            continue
        selecteurs, declarations = regle.split("{", 1)
        prefixes = ", ".join(f"{portee} {sel.strip()}"
                             for sel in selecteurs.split(",") if sel.strip())
        sorties.append(f"{prefixes}{{{declarations}}}")
    return "\n".join(sorties)


def page_html(jeton: str) -> str:
    """Page d'installation. Fonction pure : aucun acces reseau ni disque."""
    from .licences import pied_de_page
    corps = (_CORPS.replace("JOURNAL", f"/journal?jeton={html.escape(jeton)}")
             + pied_de_page(f"/licence?jeton={html.escape(jeton)}"))
    debut = ('<!doctype html><html lang="fr"><' + 'head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<title>Installation — Caviardoc</title>'
             f'<style>{_STYLE}</style></' + 'head><body>')
    fin = f'<script>const JETON = {json.dumps(jeton)};\n{_SCRIPT}</script></body></html>'
    return debut + corps + fin


# --------------------------------------------------------------------------- #
# Le serveur
# --------------------------------------------------------------------------- #

def demarrer_interface(adresse: dict) -> tuple[str, subprocess.Popen | None]:
    """Demarre l'interface principale, dans l'interpreteur qui a le moteur.

    L'installateur tourne sur le Python d'amorcage : celui que le lanceur a
    trouve ou installe, et qui n'a que la bibliotheque standard. C'est ce qui
    lui permet de s'ouvrir sur une machine ou il manque tout — mais cela veut
    dire qu'il ne peut pas charger le modele lui-meme.

    Il servait pourtant l'interface dans son propre processus. Tout s'affichait
    comme installe, puis le premier document echouait sur « dependances du
    moteur absentes » : exact, et trompeur, puisqu'elles etaient installees
    dans le venv, a cote.

    Rend (message d'erreur, processus). Le message est vide en cas de succes ;
    le processus est None quand l'interface tourne dans un fil, ici meme.
    """
    interprete = doctor.interprete_du_moteur()
    if interprete is None:
        return ("les dependances du moteur sont introuvables — "
                "relancez l'etape « Bibliotheques »"), None

    if interprete.resolve() == Path(sys.executable).resolve():
        # On tourne deja dans le venv : un fil suffit, et l'arret est commun.
        from .interface import servir as servir_interface
        prete = threading.Event()

        def note(url):
            adresse["url"] = url
            prete.set()

        threading.Thread(target=lambda: servir_interface(ouvrir=False,
                                                         pret=note),
                         daemon=True).start()
        return ("" if prete.wait(20) else "demarrage trop long"), None

    # `-u` n'est pas un detail : la sortie d'un enfant branche sur un tuyau
    # est tamponnee, et l'adresse resterait dans le tampon jusqu'a l'arret du
    # processus. On attendrait donc une ligne deja ecrite, jusqu'a expiration.
    processus = subprocess.Popen(
        [str(interprete), "-u", "-m", "caviardoc", "interface",
         "--sans-navigateur"],
        cwd=paths.project_root(), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, **paths.TEXTE)

    trouvee = threading.Event()
    marque = "Interface ouverte sur "

    def lire():
        for ligne in processus.stdout:
            if marque in ligne:
                adresse["url"] = ligne.split(marque, 1)[1].strip()
                trouvee.set()

    threading.Thread(target=lire, daemon=True).start()
    if not trouvee.wait(60):
        processus.terminate()
        return "l'interface n'a pas demarre a temps", None
    return "", processus


def servir(port: int = 0, ouvrir: bool = True, pret=None,
           arret: threading.Event | None = None,
           installation: Installation | None = None) -> None:
    """Sert la page d'installation jusqu'a l'arret.

    Memes regles d'acces que les autres surfaces du projet : boucle locale,
    jeton de session, en-tete `Host` verifie. Cette page-ci n'affiche pas de
    donnees de patient, mais elle lance des commandes sur la machine — raison
    suffisante pour ne pas la laisser ouverte a tout processus local.
    """
    if installation is None:
        installation = etapes_par_defaut()
        constater_en_fond(installation)
    jeton_session = nouveau_jeton()
    fini = arret or threading.Event()
    adresse_interface = {"url": ""}
    #: L'interface demarree a cote, s'il a fallu changer d'interpreteur. On la
    #: suit pour ne pas laisser un serveur orphelin derriere nous.
    enfants: list[subprocess.Popen] = []

    class Poignee(PoigneeBase):
        jeton = jeton_session

        def _json(self, code: int, charge) -> None:
            self.repondre(code, json.dumps(charge).encode("utf-8"),
                          "application/json; charset=utf-8")

        def _autorise(self, adresse) -> bool:
            return (self.jeton_admis(self.jeton_de_l_entete())
                    or self.jeton_admis(
                        parse_qs(adresse.query).get("jeton", [""])[0]))

        def do_GET(self):
            if not self.hote_admis():
                return self.refus(b"hote refuse")
            adresse = urlparse(self.path)
            if not self._autorise(adresse):
                return self.refus()

            if adresse.path in ("/", "/index.html"):
                return self.repondre(200, page_html(self.jeton).encode("utf-8"),
                                     "text/html; charset=utf-8")

            if adresse.path == "/etapes":
                return self._json(200, installation.vue())

            if adresse.path == "/licence":
                from .licences import texte_licence
                return self.repondre(200, texte_licence())

            if adresse.path == "/journal":
                lignes = []
                for e in installation.etapes:
                    lignes.append(f"===== {e.titre} · {e.etat} =====")
                    lignes.extend(e.journal or ["(rien)"])
                    if e.message:
                        lignes.append(f">>> {e.message}")
                    lignes.append("")
                corps = "\n".join(lignes).encode("utf-8")
                return self.repondre(200, corps)

            return self.repondre(404, b"introuvable")

        def do_POST(self):
            if not self.hote_admis():
                return self.refus(b"hote refuse")
            if not self.jeton_admis(self.jeton_de_l_entete()):
                return self.refus()
            chemin = urlparse(self.path).path

            if chemin == "/lancer":
                taille = int(self.headers.get("Content-Length") or 0)
                try:
                    charge = json.loads(self.rfile.read(taille) or b"{}")
                except json.JSONDecodeError:
                    return self.repondre(400, b"json invalide")
                if installation.en_cours:
                    return self.repondre(409, b"une etape est deja en cours")
                try:
                    etape = executer(installation, charge.get("cle", ""))
                except KeyError:
                    return self.repondre(404, b"etape inconnue")
                return self._json(200, etape.vue())

            if chemin == "/interface":
                # L'interface principale se sert elle-meme, sur son propre
                # port et avec son propre jeton : l'installateur ne fait que
                # la demarrer et rendre son adresse.
                if not adresse_interface["url"]:
                    erreur, proc = demarrer_interface(adresse_interface)
                    if erreur:
                        return self._json(500, {"erreur": erreur})
                    if proc is not None:
                        enfants.append(proc)
                return self._json(200, {"url": adresse_interface["url"]})

            if chemin == "/quitter":
                self.repondre(200, b"ok")
                fini.set()
                return

            return self.repondre(404, b"introuvable")

    serveur = ServeurLocal((HOTE, port), Poignee)
    Poignee.hotes = hotes_admis(serveur.server_port)
    adresse = f"http://{HOTE}:{serveur.server_port}/?jeton={jeton_session}"
    threading.Thread(target=serveur.serve_forever, daemon=True).start()

    if pret is not None:
        pret(adresse)
    else:
        print(f"  Installation ouverte sur {adresse}")
        print("  Ctrl-C pour arreter.")
    if ouvrir:
        webbrowser.open(adresse)

    ancien_term = None
    try:
        ancien_term = signal.signal(signal.SIGTERM, lambda *_: fini.set())
    except (ValueError, AttributeError):
        pass          # hors du fil principal : un test, typiquement

    try:
        fini.wait()
    except KeyboardInterrupt:
        print()
    finally:
        if ancien_term is not None:
            signal.signal(signal.SIGTERM, ancien_term)
        serveur.shutdown()
        # L'interface demarree a cote ne doit pas survivre a l'installateur :
        # elle ecoute sur un port et tient un repertoire de travail.
        for enfant in enfants:
            enfant.terminate()
            try:
                enfant.wait(timeout=5)
            except subprocess.TimeoutExpired:
                enfant.kill()
