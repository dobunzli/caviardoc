"""Tesseract installe par nos soins, dans le dossier de l'application.

Jusqu'ici l'installateur constatait l'absence de Tesseract et donnait une
commande a taper : `brew install`, `winget install`, `apt install`. Trois
problemes, dans l'ordre de gravite.

D'abord, sur un poste de cabinet, ces commandes echouent : Homebrew n'est pas
la, `winget` demande une elevation de privileges que l'utilisateur n'a pas.
Ensuite, elles installent Tesseract *pour toute la machine*, ce qui deborde
largement de ce qu'un anonymiseur de documents a le droit de faire. Enfin,
aucune des trois n'installe le francais d'office, si bien que le cas nominal
etait un Tesseract present mais inutilisable pour nous.

On telecharge donc nous-memes, depuis conda-forge, dans le dossier de
l'application. Rien n'est ajoute au PATH, rien n'est modifie hors de ce
dossier, aucun mot de passe n'est demande, et `desinstaller()` rend la machine
a son etat anterieur.

Le telechargement passe par micromamba — un binaire statique unique, comme uv,
qui sait resoudre et poser des paquets conda dans un prefixe quelconque. Il est
epingle a une version et verifie par empreinte SHA-256 ; les paquets conda,
lui, les verifie par les empreintes de la repodata.

Ce qu'on accepte en echange, et qu'il vaut mieux ecrire que decouvrir : les
paquets conda peuvent executer des scripts a l'installation (pre/post-link), et
certains de ceux-ci le font. Ils s'executent avec les droits de l'utilisateur,
dans le prefixe, sans elevation — mais ils s'executent. On restreint donc la
surface : un seul canal (`--override-channels`), la configuration conda de la
machine ignoree (`--no-rc`, `--no-env`), et une version epinglee.

Ce module n'importe que la bibliotheque standard : l'installateur s'en sert
avant que la moindre dependance soit installee.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from . import paths
from .models import CHUNK, USER_AGENT, DownloadError, IntegrityError, human

ProgressCb = Callable[[str, int, int], None]

#: Version de micromamba, epinglee. Les empreintes ci-dessous sont celles des
#: fichiers publies sous cette etiquette : changer l'une sans l'autre fait
#: echouer l'installation, ce qui est le comportement voulu.
MICROMAMBA_VERSION = "2.9.0-0"

_MICROMAMBA_BASE = ("https://github.com/mamba-org/micromamba-releases/"
                    "releases/download")

#: sha256 des archives micromamba, par plateforme conda.
MICROMAMBA_SHA256 = {
    "osx-arm64": "500f5074feb8d02c4296ef9921c3650ed2874171805a9fbb8fbb53896433646b",
    "osx-64": "0426ecdc41636d369f57b8fe6acbf4385a69eca45b56d9ee7d3a840a9965d44f",
    "win-64": "97a336f4ab794bd96a6a4da5e6ed63e75a1d31830414a182419b23d3b36f3fe0",
    "linux-64": "8761c382127e6363bd9e0a2451aa3ef90d071a79133f736e2f759a3bf13040dd",
    "linux-aarch64": "e705ffeed90ce0659eb546e4b1e1028c9eaf0bc9cc854867b19ac5ce0ba5852f",
}

#: Version de Tesseract demandee a conda-forge. Epinglee : une version d'OCR
#: qui change sous les pieds change le texte reconnu, donc les entites
#: detectees, donc le rappel mesure.
TESSERACT_VERSION = "5.5.3"

CANAL = "conda-forge"

#: Les seules donnees de langue qu'on garde. conda-forge livre les 125 langues
#: de tessdata_fast (339 Mo) ; on en utilise une. `osd` sert a Tesseract
#: lui-meme pour l'orientation de la page, `eng` sert de repli.
LANGUES_GARDEES = ("fra", "eng", "osd")

#: Volumes mesures sur macOS arm64 le 7 septembre 2026 : 34 paquets conda
#: (200 Mo) plus micromamba (6 Mo), 129 Mo conserves apres elagage des langues.
#: Annonces avant telechargement, pour que le refus soit eclaire.
TELECHARGEMENT_ESTIME = 206 * 1024 * 1024
INSTALLE_ESTIME = 129 * 1024 * 1024


class InstallationImpossible(RuntimeError):
    """L'installation de Tesseract n'a pas abouti."""


class TesseractIndisponible(RuntimeError):
    """Tesseract manque ou ne lit pas : aucun PDF ne peut etre traite.

    Sans OCR, un texte present seulement dans les pixels (tampon, texte dessine)
    ne serait ni cherche ni verifie : le fichier produit passerait pour sur
    sans l'etre. On refuse donc le document plutot que de le livrer reserve.
    """


REMEDE = ("Lancez `caviardoc setup` pour l'installer (`caviardoc doctor` "
          "montre l'état), ou relancez l'installateur guidé "
          "(« ▶ Ouvrir Caviardoc », en tête du dossier), onglet Installation.")


def exiger(langue: str = "fra") -> None:
    """Leve `TesseractIndisponible` si l'OCR ne peut pas servir au PDF."""
    if binaire() is None:
        raise TesseractIndisponible(
            "Tesseract (OCR) est absent : Caviardoc refuse de traiter un PDF "
            "sans lui, car un texte présent seulement dans l'image ne serait "
            "pas cherché. Aucun fichier n'a été écrit. " + REMEDE)
    manquantes = [x for x in langue.split("+") if x not in langues()]
    if manquantes:
        raise TesseractIndisponible(
            f"Tesseract n'a pas la langue « {'+'.join(manquantes)} » : "
            "Caviardoc refuse de traiter un PDF sans elle. "
            "Aucun fichier n'a été écrit. " + REMEDE)


def plateforme_conda() -> str:
    """Nom conda de cette plateforme, ou leve si elle n'est pas connue."""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return "osx-arm64" if arm else "osx-64"
    if sys.platform == "win32":
        return "win-64"
    if sys.platform.startswith("linux"):
        return "linux-aarch64" if arm else "linux-64"
    raise InstallationImpossible(
        f"plateforme non prise en charge : {sys.platform}/{machine}")


# --- Emplacements ----------------------------------------------------------

def prefixe() -> Path:
    """Le dossier ou vit notre Tesseract. Hors du depot, hors du systeme."""
    return paths.data_dir() / "tesseract"


def _racine_micromamba() -> Path:
    """Prefixe racine de micromamba : son cache de paquets vit dedans.

    Impose explicitement, sinon micromamba ecrit dans ~/.local/share/mamba —
    hors de notre dossier, donc hors de ce que `desinstaller()` peut reprendre.
    """
    return paths.cache_dir() / "mamba"


def _chemins_binaire(base: Path) -> tuple[Path, ...]:
    """Emplacements possibles de l'executable sous un prefixe conda.

    Les paquets Windows de conda-forge posent leurs binaires sous
    ``Library/bin`` ; les autres plateformes sous ``bin``.
    """
    return (base / "bin" / "tesseract",
            base / "Library" / "bin" / "tesseract.exe",
            base / "bin" / "tesseract.exe")


def _chemins_tessdata(base: Path) -> tuple[Path, ...]:
    return (base / "share" / "tessdata",
            base / "Library" / "share" / "tessdata")


def binaire_local() -> Path | None:
    """L'executable que nous avons installe, ou None."""
    for chemin in _chemins_binaire(prefixe()):
        if chemin.is_file():
            return chemin
    return None


def binaire() -> Path | None:
    """L'executable a utiliser : le notre d'abord, celui du systeme ensuite.

    Le notre passe devant parce qu'on connait ses langues : un Tesseract
    systeme peut etre la sans le francais, cas ou l'OCR rend un texte anglais
    accentue au hasard.
    """
    local = binaire_local()
    if local:
        return local
    trouve = shutil.which("tesseract")
    return Path(trouve) if trouve else None


def tessdata() -> Path | None:
    """Le dossier de donnees a imposer, ou None si on utilise celui du systeme."""
    if binaire_local() is None:
        return None
    for chemin in _chemins_tessdata(prefixe()):
        if chemin.is_dir():
            return chemin
    return None


def environnement(base: dict | None = None) -> dict:
    """L'environnement a passer a un appel Tesseract.

    TESSDATA_PREFIX n'est impose que pour notre propre installation : sur un
    Tesseract systeme, l'ecraser casserait une configuration qui marche.
    """
    env = dict(os.environ if base is None else base)
    donnees = tessdata()
    if donnees is not None:
        env["TESSDATA_PREFIX"] = str(donnees)
    return env


def installe() -> bool:
    return binaire_local() is not None


def version() -> str | None:
    exe = binaire()
    if exe is None:
        return None
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True,
                             **paths.TEXTE, timeout=10, env=environnement())
    except Exception:
        return None
    premiere = (out.stdout or out.stderr).splitlines()
    return premiere[0].strip() if premiere else None


def langues() -> list[str]:
    """Les langues disponibles pour l'executable retenu."""
    exe = binaire()
    if exe is None:
        return []
    try:
        out = subprocess.run([str(exe), "--list-langs"], capture_output=True,
                             **paths.TEXTE, timeout=10, env=environnement())
    except Exception:
        return []
    lignes = (out.stdout or out.stderr).splitlines()
    return [ln.strip() for ln in lignes[1:] if ln.strip()]


# --- Installation ----------------------------------------------------------

def _telecharger_verifie(url: str, dest: Path, sha256: str,
                         nom: str, on_progress: ProgressCb | None) -> None:
    """Telecharge dans un fichier temporaire, verifie, puis met en place.

    Rien n'apparait a l'emplacement final avant que l'empreinte corresponde :
    une coupure de reseau ne laisse pas un fichier tronque qu'on prendrait
    ensuite pour valide.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    requete = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    empreinte = hashlib.sha256()
    lus = 0
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse, tmp.open("wb") as sortie:
            total = int(reponse.headers.get("Content-Length") or 0)
            while True:
                bloc = reponse.read(CHUNK)
                if not bloc:
                    break
                sortie.write(bloc)
                empreinte.update(bloc)
                lus += len(bloc)
                if on_progress:
                    on_progress(nom, lus, total)
    except (OSError, http.client.HTTPException) as exc:
        # URLError, mais aussi une coupure en cours de route (TimeoutError,
        # ConnectionResetError, IncompleteRead) : toutes se relancent.
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"telechargement de {nom} impossible : "
                            f"{getattr(exc, 'reason', exc)}") from exc

    obtenue = empreinte.hexdigest()
    if obtenue != sha256:
        tmp.unlink(missing_ok=True)
        raise IntegrityError(f"empreinte incorrecte pour {nom}\n"
                             f"  attendue : {sha256}\n"
                             f"  obtenue  : {obtenue}")
    tmp.replace(dest)


def chemin_micromamba() -> Path:
    exe = "micromamba.exe" if sys.platform == "win32" else "micromamba"
    return _racine_micromamba() / "bin" / exe


def installer_micromamba(on_progress: ProgressCb | None = None) -> Path:
    """Pose micromamba dans notre cache, ou rend celui qui y est deja."""
    deja = chemin_micromamba()
    if deja.is_file():
        return deja

    plateforme = plateforme_conda()
    sha = MICROMAMBA_SHA256.get(plateforme)
    if sha is None:
        raise InstallationImpossible(
            f"micromamba n'est pas publie pour {plateforme}")

    url = (f"{_MICROMAMBA_BASE}/{MICROMAMBA_VERSION}/"
           f"micromamba-{plateforme}.tar.bz2")

    with tempfile.TemporaryDirectory() as travail:
        archive = Path(travail) / "micromamba.tar.bz2"
        _telecharger_verifie(url, archive, sha, "micromamba", on_progress)

        # L'archive contient bin/micromamba et des metadonnees ; on n'extrait
        # que l'executable, et on refuse tout chemin qui sortirait du dossier.
        deja.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:bz2") as tar:
            membre = next((m for m in tar.getmembers()
                           if m.isfile()
                           and Path(m.name).name in ("micromamba", "micromamba.exe")
                           and ".." not in Path(m.name).parts), None)
            if membre is None:
                raise InstallationImpossible(
                    "archive micromamba inattendue : executable introuvable")
            source = tar.extractfile(membre)
            if source is None:
                raise InstallationImpossible("archive micromamba illisible")
            with deja.open("wb") as sortie:
                shutil.copyfileobj(source, sortie)

    deja.chmod(0o755)
    return deja


def _elaguer_langues() -> int:
    """Supprime les donnees de langue qu'on n'utilise pas. Rend les octets liberes."""
    libere = 0
    for dossier in _chemins_tessdata(prefixe()):
        if not dossier.is_dir():
            continue
        for fichier in dossier.rglob("*.traineddata"):
            if fichier.stem in LANGUES_GARDEES:
                continue
            libere += fichier.stat().st_size
            fichier.unlink()
    return libere


def installer(on_progress: ProgressCb | None = None,
              on_ligne: Callable[[str], None] | None = None) -> Path:
    """Telecharge et installe Tesseract dans le dossier de l'application.

    Relancable : si le prefixe existe deja et fonctionne, on ne fait rien. En
    cas d'echec, le prefixe partiel est efface — un Tesseract a moitie pose est
    pire qu'un Tesseract absent, parce que le diagnostic le croirait present.
    """
    if installe():
        return binaire_local()

    paths.ensure_dirs()
    mamba = installer_micromamba(on_progress)

    cible = prefixe()
    racine = _racine_micromamba()
    commande = [
        str(mamba), "create", "--yes",
        "--root-prefix", str(racine),
        "--prefix", str(cible),
        "--channel", CANAL,
        "--override-channels",   # rien d'autre que conda-forge
        "--no-rc",               # ignore la configuration conda de la machine
        "--no-env",              # ... et ses variables d'environnement
        f"tesseract={TESSERACT_VERSION}",
    ]
    if on_ligne:
        on_ligne("$ " + " ".join(commande))

    processus = subprocess.Popen(commande, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, **paths.TEXTE)
    for ligne in processus.stdout:
        ligne = ligne.rstrip()
        if ligne and on_ligne:
            on_ligne(ligne)
    code = processus.wait()

    if code != 0:
        shutil.rmtree(cible, ignore_errors=True)
        raise InstallationImpossible(
            f"micromamba a echoue (code {code}). Le detail est dans le journal.")

    if binaire_local() is None:
        shutil.rmtree(cible, ignore_errors=True)
        raise InstallationImpossible(
            "installation terminee, mais l'executable reste introuvable")

    libere = _elaguer_langues()
    if on_ligne and libere:
        on_ligne(f"langues inutilisees supprimees : {human(libere)} liberes")

    # Le cache de paquets a fait son office. Les fichiers du prefixe y sont
    # lies en dur : le supprimer ne retire qu'un lien, pas les donnees.
    poubelle = racine / "pkgs"
    if poubelle.is_dir():
        shutil.rmtree(poubelle, ignore_errors=True)
        if on_ligne:
            on_ligne("cache de telechargement vide")

    if "fra" not in langues():
        raise InstallationImpossible(
            "Tesseract est installe, mais sans le francais. "
            "Signalez-le : c'est une anomalie du paquet conda-forge.")

    return binaire_local()


def desinstaller() -> None:
    """Rend la machine a son etat anterieur. Rien ne subsiste ailleurs."""
    shutil.rmtree(prefixe(), ignore_errors=True)
    shutil.rmtree(_racine_micromamba(), ignore_errors=True)


def taille_installee() -> int:
    """Octets reellement occupes par notre prefixe."""
    return paths.taille_reelle(prefixe())
