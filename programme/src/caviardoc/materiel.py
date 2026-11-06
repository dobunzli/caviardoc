"""Ce que l'on sait de la machine : systeme, processeur, carte graphique.

Le meilleur modele n'est pas le meme partout. Constate sur un Windows reel : la
variante fp16 du modele met 922 s sur un seul document, contre 4 s sur un Mac,
parce qu'onnxruntime n'a pas de noyau fp16 pour MatMul sur un processeur x86.
Le choix du modele depend donc de ce qu'est la machine, et ce module la decrit.

Deux niveaux, pour ne pas payer ce qu'on n'utilise pas :

- ``architecture()`` : instantane, sans sous-processus. C'est tout ce qu'il faut
  pour choisir le modele, et ``models`` l'appelle a l'import.
- ``detecter()`` : processeur, fabricant et cartes graphiques, par des
  commandes du systeme. Environ une seconde ; pour l'affichage seulement.

La detection est un renseignement, jamais une condition : toute commande qui
echoue laisse un champ vide, et rien ne s'arrete.

**La carte graphique est detectee, pas utilisee.** L'employer demande un autre
paquet onnxruntime (CUDA pour NVIDIA, DirectML pour les autres), de plusieurs
gigaoctets, et une mesure du rappel sur ce chemin : tant que cela n'est pas fait,
un GPU ne change pas le modele retenu.
"""

from __future__ import annotations

import functools
import os
import platform
import subprocess
import sys
from dataclasses import dataclass

ARM64 = "arm64"
X86_64 = "x86_64"


def architecture() -> str:
    """``arm64``, ``x86_64`` ou le nom brut du systeme.

    Sous Windows, un Python x64 qui tourne en emulation sur une machine ARM
    annonce ``AMD64`` : ``PROCESSOR_ARCHITEW6432`` donne la vraie machine.
    """
    brut = platform.machine()
    if sys.platform == "win32":
        brut = (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE") or brut)
    brut = brut.lower()
    if brut in ("arm64", "aarch64"):
        return ARM64
    if brut in ("amd64", "x86_64", "x64"):
        return X86_64
    return brut


def systeme() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


@dataclass(frozen=True)
class Materiel:
    systeme: str
    architecture: str
    #: apple | intel | amd | qualcomm | "" quand on ne sait pas.
    fabricant: str = ""
    processeur: str = ""
    gpus: tuple[str, ...] = ()

    @property
    def libelle(self) -> str:
        """Une ligne lisible : « Windows · x86-64 · Intel Core i7 · NVIDIA … »."""
        nom_systeme = {"macos": "macOS", "windows": "Windows"}.get(
            self.systeme, "Linux")
        nom_arch = {ARM64: "ARM64", X86_64: "x86-64"}.get(
            self.architecture, self.architecture)
        morceaux = [nom_systeme, nom_arch]
        if self.processeur:
            morceaux.append(self.processeur)
        morceaux += list(self.gpus)
        return " · ".join(morceaux)

    def vue(self) -> dict:
        return {"systeme": self.systeme, "architecture": self.architecture,
                "fabricant": self.fabricant, "processeur": self.processeur,
                "gpus": list(self.gpus), "libelle": self.libelle}


def _executer(commande: list[str], delai: float = 15.0) -> str:
    """La sortie d'une commande, ou '' : une detection ne doit rien casser."""
    try:
        fini = subprocess.run(commande, capture_output=True, text=True,
                              timeout=delai, errors="replace",
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return fini.stdout if fini.returncode == 0 else ""


def _fabricant(texte: str) -> str:
    bas = texte.lower()
    for marque, nom in (("apple", "apple"), ("genuineintel", "intel"),
                        ("intel", "intel"), ("authenticamd", "amd"),
                        ("amd", "amd"), ("qualcomm", "qualcomm"),
                        ("snapdragon", "qualcomm")):
        if marque in bas:
            return nom
    return ""


def _powershell(commande: str) -> list[str]:
    """Les lignes non vides rendues par une commande PowerShell."""
    sortie = _executer(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", commande])
    return [ligne.strip() for ligne in sortie.splitlines() if ligne.strip()]


def _processeur_et_gpus_windows() -> tuple[str, tuple[str, ...]]:
    noms = _powershell("(Get-CimInstance Win32_Processor).Name")
    gpus = _powershell("(Get-CimInstance Win32_VideoController).Name")
    processeur = noms[0] if noms else os.environ.get("PROCESSOR_IDENTIFIER", "")
    return processeur, tuple(gpus)


def _processeur_et_gpus_macos() -> tuple[str, tuple[str, ...]]:
    processeur = _executer(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
    gpus = tuple(
        ligne.split(":", 1)[1].strip()
        for ligne in _executer(["system_profiler", "SPDisplaysDataType"]).splitlines()
        if "Chipset Model" in ligne)
    return processeur, gpus


def _processeur_et_gpus_linux() -> tuple[str, tuple[str, ...]]:
    processeur = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for ligne in fh:
                if ligne.lower().startswith("model name"):
                    processeur = ligne.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    gpus = tuple(
        ligne.split(":", 1)[1].split("(UUID")[0].strip()
        for ligne in _executer(["nvidia-smi", "-L"]).splitlines() if ":" in ligne)
    return processeur, gpus


#: Version minimale de macOS : les paquets onnxruntime et torch epingles par
#: `uv.lock` ne sont publies que pour `macosx_14_0_arm64`.
MACOS_MINIMAL = 14

#: Valeur par defaut d'un parametre qui veut dire « lis la machine » : ``None``
#: serait ambigu, puisqu'il signifie aussi « version de macOS inconnue ».
_LIRE = object()


def version_macos() -> int | None:
    """Version majeure de macOS (14, 15...), ou None hors macOS ou si illisible."""
    if sys.platform != "darwin":
        return None
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return None


def obstacles(systeme_=_LIRE, architecture_=_LIRE,
              version_macos_=_LIRE) -> tuple[str, ...]:
    """Ce qui rend l'installation **impossible**, et non seulement lente.

    Lu dans `uv.lock` : sous macOS, onnxruntime et torch n'ont de paquet que pour
    arm64, et macOS 14 ou plus ; sous Windows, torch n'en a que pour x86-64. Sur un Mac Intel, ou un Mac plus ancien, l'etape
    « Bibliotheques » echouerait apres un long telechargement, avec un message
    d'erreur de uv peu parlant. Mieux vaut le dire avant de commencer — et ne
    pas commencer.

    Rend les raisons, ou un tuple vide. Une version inconnue ne condamne pas.
    """
    sys_ = systeme() if systeme_ is _LIRE else systeme_
    arch = architecture() if architecture_ is _LIRE else architecture_
    version = version_macos() if version_macos_ is _LIRE else version_macos_
    raisons: list[str] = []

    if arch not in (ARM64, X86_64):
        raisons.append(
            f"Le processeur ({arch}) n'est pas pris en charge : il faut un "
            "processeur 64 bits Intel, AMD ou ARM.")
    if sys_ == "windows" and arch == ARM64:
        # torch n'est pas publie pour Windows ARM64 dans `uv.lock` : l'etape
        # « Bibliotheques » echouait apres plusieurs centaines de megaoctets.
        # Un Python x64 emule n'y change rien — la machine reste ARM, et c'est
        # elle que `architecture()` decrit.
        raisons.append(
            "Ce PC Windows a un processeur ARM (Snapdragon, par exemple). Une "
            "des bibliothèques de détection (torch) n'est pas publiée pour ces "
            "processeurs : Caviardoc ne peut pas s'y installer. Il "
            "fonctionne sur les PC à processeur Intel ou AMD.")
    if sys_ == "macos":
        if arch == X86_64:
            raisons.append(
                "Ce Mac a un processeur Intel. Les bibliothèques de détection "
                "(onnxruntime, torch) ne sont plus publiées pour les Mac Intel : "
                "Caviardoc ne peut pas s'y installer. Il fonctionne sur les "
                "Mac Apple Silicon (M1 et suivants).")
        if version is not None and version < MACOS_MINIMAL:
            raisons.append(
                f"macOS {version} est trop ancien : les bibliothèques de "
                f"détection demandent macOS {MACOS_MINIMAL} ou plus récent. "
                "Mettez macOS à jour, puis relancez.")
    return tuple(raisons)


@dataclass(frozen=True)
class Compatibilite:
    """Cette machine peut-elle faire tourner l'outil ?"""

    #: ok | juste | incompatible
    niveau: str
    raisons: tuple[str, ...] = ()
    #: True quand l'installation est impossible, pas seulement deconseillee :
    #: la memoire ou le disque insuffisants sont dits, mais ne bloquent pas.
    bloquant: bool = False

    def vue(self) -> dict:
        return {"niveau": self.niveau, "raisons": list(self.raisons),
                "bloquant": self.bloquant}


def evaluer(ram: int | None, disque_libre: int | None, *, ram_minimale: int,
            disque_necessaire: int, architecture_=_LIRE, systeme_=_LIRE,
            version_macos_=_LIRE, coeurs: int | None = None) -> Compatibilite:
    """Dit si l'ordinateur est assez puissant, et pourquoi sinon.

    Est **incompatible** ce qui empeche le modele de se charger ou de tenir :
    un processeur d'une architecture qu'aucun paquet ne couvre, trop peu de
    memoire, pas assez de place pour le telecharger. Est **juste** ce qui
    marchera mais lentement : un seul coeur. On le dit avant l'installation,
    pas apres un telechargement de plus d'un gigaoctet.

    Une valeur inconnue (``None``) ne condamne pas : on ne refuse pas une
    machine sur ce qu'on n'a pas su lire.
    """
    absolus = obstacles(systeme_, architecture_, version_macos_)
    incompatibles: list[str] = list(absolus)
    justes: list[str] = []

    if ram is not None and ram < ram_minimale:
        incompatibles.append(
            f"Pas assez de mémoire : {ram / 1024 ** 3:.1f} Go, il en faut au "
            f"moins {ram_minimale / 1024 ** 3:.0f} Go.")
    if disque_libre is not None and disque_libre < disque_necessaire:
        incompatibles.append(
            f"Pas assez de place sur le disque : {disque_libre / 1024 ** 3:.1f} "
            f"Go libres, il en faut {disque_necessaire / 1024 ** 3:.1f} Go.")

    coeurs = coeurs if coeurs is not None else os.cpu_count()
    if coeurs is not None and coeurs < 2:
        justes.append("Un seul coeur de processeur : l'analyse sera très lente.")

    if incompatibles:
        return Compatibilite("incompatible", tuple(incompatibles + justes),
                             bloquant=bool(absolus))
    if justes:
        return Compatibilite("juste", tuple(justes))
    return Compatibilite("ok")


@functools.cache
def detecter() -> Materiel:
    """La machine, aussi precisement qu'on peut la lire. Mise en cache."""
    sys_ = systeme()
    lecture = {"windows": _processeur_et_gpus_windows,
               "macos": _processeur_et_gpus_macos}.get(
                   sys_, _processeur_et_gpus_linux)
    try:
        processeur, gpus = lecture()
    except Exception:       # une detection ne doit jamais faire echouer
        processeur, gpus = "", ()
    # Sur une puce Apple, le « chipset graphique » est la puce elle-meme : le
    # dire deux fois n'apprend rien.
    gpus = tuple(g for g in gpus if g != processeur)
    return Materiel(systeme=sys_, architecture=architecture(),
                    fabricant=_fabricant(processeur),
                    processeur=processeur, gpus=gpus)
