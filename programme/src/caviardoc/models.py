"""Registre des modeles, telechargement verifie et controle d'integrite.

Le modele n'est jamais versionne dans git ni depose dans le dossier
depot : il pese plusieurs centaines de megaoctets — 573 Mo pour le defaut
actuel — et un depot git n'oublie jamais ce qu'on y met une fois. Il est telecharge au premier lancement dans le repertoire de donnees
local, et chaque fichier est verifie par son empreinte SHA-256.

Ce module n'utilise que la bibliotheque standard.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import materiel, paths

USER_AGENT = "caviardoc/0.1 (+https://github.com/)"
CHUNK = 1 << 20  # 1 Mio


@dataclass(frozen=True)
class RemoteFile:
    """Un fichier distant, epingle par empreinte et par taille."""

    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ModelSpec:
    """Un modele telechargeable depuis Hugging Face."""

    key: str
    repo: str
    revision: str
    license: str
    description: str
    files: tuple[RemoteFile, ...]
    notes: str = ""
    #: Memoire vive occupee une fois charge, en octets. Ce qui decide si une
    #: machine le porte, bien plus que la place sur le disque.
    memoire: int = 0
    #: Pourquoi ne pas l'utiliser, ou "" s'il est utilisable. Un modele
    #: deconseille reste au registre — il faut pouvoir le mesurer — mais
    #: l'interface ne propose pas de l'installer.
    deconseille: str = ""
    #: Rappel mesure sur le corpus de reference, en pour cent, pour qu'on sache
    #: avant de le telecharger ce qu'on peut en attendre.
    rappel_reference: float | None = None
    #: Cles des modeles que celui-ci surpasse. Quand l'un d'eux est en service,
    #: la console propose celui-ci (voir `modele_propose`), sans rien telecharger
    #: sans l'accord de l'utilisateur.
    remplace: tuple[str, ...] = ()
    #: Une ou deux phrases sur ce qui est meilleur, montrees avec la proposition.
    nouveautes: str = ""

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def url_for(self, filename: str) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.revision}/{filename}"

    def local_dir(self) -> Path:
        return paths.models_dir() / self.key


# Empreintes calculees le 2 septembre 2026 a partir de la revision `main`.
# Le modele multilingue repose sur l'encodeur mdeberta-v3-base : c'est le seul
# encodeur GLiNER qui traite correctement le francais. Les variantes plus
# legeres (44 Mo, 79 Mo) sont anglophones et ne conviennent pas.
#
# Les quantifications entieres (int8, uint8, q4...) sont ecartees : leurs noyaux
# choisissent leur implementation selon le processeur, et le rappel avec eux.
# int8 a ete mesure a 62,2 % sur deux machines arm64 et 51,4 % sur x86-64, sans
# jamais atteindre le seuil de 90 %. Il a ete retire du registre le 2026-10-04 :
# un modele propose doit tenir le seuil.

#: Ce que tous les modeles partagent : le tokenizer et les configurations.
_FICHIERS_COMMUNS = (
    RemoteFile("tokenizer.json",
               "914bd3c8fb7b525af9e23b60d0ec7b1248ddb2b99014efd9c02ebeb022f8cab7", 16331948),
    RemoteFile("spm.model",
               "13c8d666d62a7bc4ac8f040aab68e942c861f93303156cc28f5c7e885d86d6e3", 4305025),
    RemoteFile("gliner_config.json",
               "69e141f7fe1864e0d81ab0e542c68387d588db62393bcd93b471d54dcf0f5c16", 732),
    RemoteFile("tokenizer_config.json",
               "78f866883daf7ee2bc400200a155cdbe9116ed0a6ed597ff573ea2c9862a89a6", 1806),
    RemoteFile("special_tokens_map.json",
               "9463f61e1b109a8eb4688b829260d7c6b1e6dff04c98ff7269bb89e2b92369b9", 286),
    RemoteFile("added_tokens.json",
               "030e747c4ca7992a3ac794c6fda9919352c88ae722e85178217cd083b450078d", 86),
    RemoteFile("config.json",
               "8aece71b73ca0fbd6dd121ad755deb736e7757d053ced523c2e4959ff446d3f5", 28),
)

#: Le modele en demi-precision. Les noyaux flottants ne choisissent pas leur
#: implementation selon le processeur comme le font les noyaux entiers : 97,3 %
#: de rappel sur arm64 comme sur x86-64. Mais onnxruntime n'a pas de noyau fp16
#: pour MatMul sur CPU x86 : voir `modele_pour`.
GLINER_MULTI_PII_FP16 = ModelSpec(
    key="gliner-multi-pii-fp16",
    repo="onnx-community/gliner_multi_pii-v1",
    revision="main",
    license="Apache-2.0",
    description="GLiNER multilingue PII — demi-précision (fp16)",
    memoire=int(1.8 * 1024 ** 3),
    rappel_reference=97.3,
    notes=(
        "Le format par defaut sur arm64 (Mac, Windows ARM), ou le fp16 est "
        "natif. Environ 1,8 Go en memoire, 97,3 % de rappel. Tres lent sur un "
        "processeur x86 : 922 s pour un seul document sur un Windows reel."
    ),
    files=(
        RemoteFile("onnx/model_fp16.onnx",
                   "e756837e13d16ef832f6cd9fca473c2c3dd1eb232cab13e6443d958d608e797b", 579717643),
    ) + _FICHIERS_COMMUNS,
)

#: Le modele en pleine precision : le fichier d'origine, dont fp16 derive. Plus
#: lourd (1,16 Go), mais ses noyaux existent sur tous les processeurs.
GLINER_MULTI_PII_FP32 = ModelSpec(
    key="gliner-multi-pii-fp32",
    repo="onnx-community/gliner_multi_pii-v1",
    revision="main",
    license="Apache-2.0",
    description="GLiNER multilingue PII — pleine précision (fp32)",
    # Pic mesure sur arm64 : 1,7 Go, contre 2,5 Go pour fp16, qui repasse par
    # des conversions.
    memoire=int(1.7 * 1024 ** 3),
    rappel_reference=97.3,
    notes=(
        "Pleine precision. Environ 1,16 Go sur le disque, mais moins de memoire "
        "que fp16 a l'usage. Meme rappel que fp16 au dixieme pres (97,3 % / "
        "95,0 % sur arm64), et des noyaux rapides sur tous les processeurs, y "
        "compris x86 ou la variante fp16 est tres lente."
    ),
    files=(
        RemoteFile("onnx/model.onnx",
                   "7704865e414f24591da6aee08716a25677855bc8a84af81396d90e40df1e68d2", 1157129714),
    ) + _FICHIERS_COMMUNS,
)

REGISTRY: dict[str, ModelSpec] = {
    m.key: m for m in (GLINER_MULTI_PII_FP16, GLINER_MULTI_PII_FP32)
}

#: Le defaut depend de l'architecture. Sur arm64, fp16 est natif et rapide. Sur
#: x86 (donc la plupart des Windows), onnxruntime n'a pas de noyau fp16 pour
#: MatMul sur CPU : constate sur un Windows reel, 922 s pour un seul document,
#: contre 4 s sur un Mac. fp32 y rend exactement les memes chiffres (97,3 % /
#: 95,0 %, verifie sur arm64), plus vite et avec moins de memoire.
#:
#: Sur une petite machine, le vrai choix est « ce modele ou renoncer » : un
#: anonymiseur qui laisse passer une identite sur deux ne rend pas le service
#: qu'on lui demande. La carte graphique n'entre pas dans ce choix : voir
#: `materiel`.
def modele_pour(architecture: str) -> str:
    """Cle du meilleur modele pour une architecture de processeur."""
    return (GLINER_MULTI_PII_FP16 if architecture == materiel.ARM64
            else GLINER_MULTI_PII_FP32).key


DEFAULT_MODEL = modele_pour(materiel.architecture())

#: Nom du fichier ONNX a charger, par modele.
FICHIER_ONNX = {
    GLINER_MULTI_PII_FP16.key: "onnx/model_fp16.onnx",
    GLINER_MULTI_PII_FP32.key: "onnx/model.onnx",
}


# --------------------------------------------------------------------------- #
# Modele retenu sur cette machine
# --------------------------------------------------------------------------- #
#
# Un modele peut rendre des resultats dependants du processeur : int8, retire,
# rendait 97 % de rappel sur une machine et 62 % sur une autre. Le bon modele
# est donc une propriete de la machine, etablie par
# `tools/mesurer.py`, et non un choix du depot — qui est partage par toutes.
#
# Il est enregistre dans le repertoire de donnees local, a cote du modele
# lui-meme, et toutes les commandes s'y referent : ce que `mesurer` mesure est
# ce que `caviarder` utilisera. Un drapeau seul ne suffirait pas : l'oublier
# rendrait le document moins protege, sans rien signaler.

#: Cle de l'etat d'installation ou est note le modele retenu.
CLE_ETAT_MODELE = "modele"


def _etat() -> dict:
    """Etat d'installation de cette machine. Un etat illisible vaut vide."""
    try:
        charge = json.loads(paths.state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return charge if isinstance(charge, dict) else {}


def modele_actif() -> str:
    """Cle du modele retenu sur cette machine, sinon celui du registre.

    Un etat ecrit par une version anterieure peut nommer un modele qui n'existe
    plus : on retombe alors sur le defaut plutot que d'echouer au chargement.
    """
    cle = _etat().get(CLE_ETAT_MODELE)
    return cle if cle in REGISTRY else DEFAULT_MODEL


def definir_modele_actif(cle: str) -> None:
    """Note ``cle`` comme le modele a utiliser sur cette machine."""
    if cle not in REGISTRY:
        raise KeyError(cle)
    _ecrire_etat({CLE_ETAT_MODELE: cle})


def _ecrire_etat(champs: dict) -> None:
    etat = _etat()
    etat.update(champs)
    paths.ensure_dirs()
    paths.state_file().write_text(json.dumps(etat, indent=2, ensure_ascii=False)
                                  + "\n", encoding="utf-8")


#: Cle de l'etat ou sont notes les modeles dont l'utilisateur ne veut plus
#: qu'on lui parle.
CLE_ETAT_REFUSES = "modeles_refuses"


def modeles_refuses() -> frozenset[str]:
    """Cles des modeles que l'utilisateur a decline de se voir proposer."""
    refuses = _etat().get(CLE_ETAT_REFUSES)
    if not isinstance(refuses, list):
        return frozenset()
    return frozenset(c for c in refuses if isinstance(c, str))


def refuser_proposition(cle: str) -> None:
    """Note que ``cle`` ne doit plus etre propose sur cette machine."""
    if cle not in REGISTRY:
        raise KeyError(cle)
    _ecrire_etat({CLE_ETAT_REFUSES: sorted(modeles_refuses() | {cle})})


def modele_propose(registre: dict[str, ModelSpec], actif: str,
                   installes: set[str] | frozenset[str],
                   refuses: set[str] | frozenset[str], ram: int | None,
                   marge: int = 2 * 1024 ** 3) -> ModelSpec | None:
    """Le modele a proposer a la place de ``actif``, ou None.

    Un candidat declare surpasser le modele en service (``remplace``), n'est
    pas deconseille, tient en memoire avec ``marge`` de reste, n'est pas deja
    installe et n'a pas ete decline. Plusieurs candidats : le meilleur rappel.
    """
    candidats = [
        s for s in registre.values()
        if actif in s.remplace and not s.deconseille
        and s.key not in installes and s.key not in refuses
        and (ram is None or ram >= s.memoire + marge)
    ]
    return max(candidats, key=lambda s: s.rappel_reference or 0, default=None)


#: Cle de la derniere mesure faite sur cette machine.
#:
#: Le modele retenu et la mesure vivent cote a cote, et c'est voulu : le
#: premier ne veut rien dire sans la seconde. Une machine peut pointer vers un
#: modele qui n'y a jamais ete mesure — c'etait le cas jusqu'ici, et rien ne le
#: signalait.
CLE_ETAT_MESURE = "mesure"


def derniere_mesure() -> dict | None:
    """Derniere mesure enregistree sur cette machine, ou None."""
    mesure = _etat().get(CLE_ETAT_MESURE)
    return mesure if isinstance(mesure, dict) else None


def noter_mesure(cle: str, mesures: dict, tenu: bool,
                 corpus: str | None = None) -> dict:
    """Enregistre une mesure, qu'elle tienne les seuils ou non.

    Un echec s'enregistre comme un succes : c'est meme le cas le plus utile a
    conserver, puisque c'est celui ou l'utilisateur ne doit rien confier a la
    machine. Ce que l'echec n'autorise pas, c'est de retenir le modele.

    ``corpus`` : l'empreinte du corpus mesure. Sans elle, deux mesures ne se
    comparent pas : un chiffre n'a de sens que sur le meme corpus.
    """
    import time

    releve = {
        "modele": cle,
        "date": time.strftime("%Y-%m-%d"),
        "tenu": bool(tenu),
        **{nom: round(valeur * 100, 1) for nom, valeur in mesures.items()},
    }
    if corpus:
        releve["corpus"] = corpus
    _ecrire_etat({CLE_ETAT_MESURE: releve})
    return releve


def mesure_du_modele(cle: str | None = None) -> dict | None:
    """Mesure de cette machine, si elle porte bien sur le modele ``cle``.

    Une mesure faite sur un autre modele ne dit rien de celui qui sera
    utilise : mieux vaut rendre None que d'afficher un chiffre rassurant qui
    ne decrit pas ce qui tourne.

    Sans argument, le modele retenu par la machine.
    """
    mesure = derniere_mesure()
    if mesure and mesure.get("modele") == (cle or modele_actif()):
        return mesure
    return None


def mesure_du_modele_actif() -> dict | None:
    """Mesure portant sur le modele retenu. Conserve pour les appelants."""
    return mesure_du_modele()


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

@dataclass
class FileStatus:
    file: RemoteFile
    path: Path
    present: bool
    size_ok: bool = False
    hash_ok: bool | None = None  # None = non verifie (verification rapide)

    @property
    def ok(self) -> bool:
        return self.present and self.size_ok and self.hash_ok is not False


@dataclass
class ModelStatus:
    spec: ModelSpec
    files: list[FileStatus] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(f.ok for f in self.files)

    @property
    def missing(self) -> list[FileStatus]:
        return [f for f in self.files if not f.ok]

    @property
    def missing_bytes(self) -> int:
        return sum(f.file.size for f in self.missing)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def check(spec: ModelSpec, deep: bool = False) -> ModelStatus:
    """Controle la presence du modele.

    Par defaut ne verifie que la presence et la taille — instantane. Avec
    ``deep=True``, recalcule l'empreinte SHA-256 de chaque fichier, ce qui lit
    l'integralite du modele depuis le disque : ``spec.total_size``, soit
    plusieurs centaines de megaoctets.
    """
    status = ModelStatus(spec=spec)
    root = spec.local_dir()
    for rf in spec.files:
        p = root / rf.name
        if not p.is_file():
            status.files.append(FileStatus(rf, p, present=False))
            continue
        size_ok = p.stat().st_size == rf.size
        hash_ok = None
        if deep and size_ok:
            hash_ok = sha256_of(p) == rf.sha256
        status.files.append(FileStatus(rf, p, present=True, size_ok=size_ok, hash_ok=hash_ok))
    return status


# --------------------------------------------------------------------------- #
# Telechargement
# --------------------------------------------------------------------------- #

ProgressCb = Callable[[str, int, int], None]


def _download_one(spec: ModelSpec, rf: RemoteFile, on_progress: ProgressCb | None) -> None:
    dest = spec.local_dir() / rf.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    # Reprise : un .part laisse par une coupure est complete, pas recommence.
    # Le fichier ONNX pese plusieurs centaines de megaoctets, et un reseau de
    # cabinet qui decroche a 90 % faisait tout retelecharger. L'empreinte
    # couvre le fichier entier : une reprise sur un debut corrompu est rejetee
    # a la fin, comme n'importe quel telechargement altere.
    h = hashlib.sha256()
    read = 0
    if tmp.is_file() and 0 < tmp.stat().st_size < rf.size:
        with tmp.open("rb") as fh:
            for block in iter(lambda: fh.read(CHUNK), b""):
                h.update(block)
                read += len(block)
    else:
        tmp.unlink(missing_ok=True)

    headers = {"User-Agent": USER_AGENT}
    if read:
        headers["Range"] = f"bytes={read}-"
    req = urllib.request.Request(spec.url_for(rf.name), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        if read and resp.status != 206:
            # Le serveur ignore la plage demandee et renvoie tout : on repart.
            h, read = hashlib.sha256(), 0
        total = read + int(resp.headers.get("Content-Length") or rf.size - read)
        with tmp.open("ab" if read else "wb") as out:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                out.write(block)
                h.update(block)
                read += len(block)
                if on_progress:
                    on_progress(rf.name, read, total)

    digest = h.hexdigest()
    if digest != rf.sha256:
        tmp.unlink(missing_ok=True)
        raise IntegrityError(
            f"empreinte incorrecte pour {rf.name}\n"
            f"  attendue : {rf.sha256}\n"
            f"  obtenue  : {digest}"
        )
    tmp.replace(dest)


class IntegrityError(RuntimeError):
    """Un fichier telecharge ne correspond pas a son empreinte epinglee."""


class DownloadError(RuntimeError):
    """Le telechargement a echoue pour une raison reseau."""


def download(spec: ModelSpec, on_progress: ProgressCb | None = None,
             only_missing: bool = True) -> None:
    """Telecharge le modele, en verifiant chaque fichier."""
    paths.ensure_dirs()
    spec.local_dir().mkdir(parents=True, exist_ok=True)

    status = check(spec)
    todo = [f.file for f in status.missing] if only_missing else list(spec.files)

    for rf in todo:
        try:
            _download_one(spec, rf, on_progress)
        except (OSError, http.client.HTTPException) as exc:
            # Pas seulement URLError : une coupure en cours de fichier leve
            # TimeoutError, ConnectionResetError ou IncompleteRead, qui
            # remontaient tels quels au lieu d'un « relancez » comprehensible.
            raise DownloadError(f"telechargement de {rf.name} impossible : "
                                f"{getattr(exc, 'reason', exc)}") from exc


def taille_installee(spec: ModelSpec) -> int:
    """Octets occupes sur le disque par ce modele, fichiers partiels compris."""
    dossier = spec.local_dir()
    if not dossier.is_dir():
        return 0
    return sum(f.stat().st_size for f in dossier.rglob("*") if f.is_file())


def supprimer(spec: ModelSpec) -> int:
    """Efface les fichiers du modele, et rend les octets liberes.

    Le dossier est celui du registre, jamais un chemin venu d'ailleurs : la
    cle est verifiee par l'appelant contre `REGISTRY`.
    """
    liberes = taille_installee(spec)
    dossier = spec.local_dir()
    if dossier.is_dir():
        shutil.rmtree(dossier)
    return liberes


def free_space(path: Path) -> int:
    """Espace disque disponible, en octets, pour le volume contenant ``path``."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def human(n: float) -> str:
    """Taille lisible, en francais : virgule decimale, et pas de « ,0 » inutile.

    Le point decimal anglais detonnait a cote des « 97,3 % » du reste de
    l'interface, et « 206,0 Mo » annonce une precision qu'on n'a pas.
    """
    for unit in ("o", "Ko", "Mo", "Go"):
        if abs(n) < 1024 or unit == "Go":
            if unit == "o":
                return f"{n:.0f} {unit}"
            texte = f"{n:.1f}".rstrip("0").rstrip(".")
            return f"{texte.replace('.', ',')} {unit}"
        n /= 1024
    return f"{n:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " Go"


def cli_progress(name: str, read: int, total: int) -> None:
    """Barre de progression simple, sur la sortie d'erreur."""
    if not sys.stderr.isatty():
        return
    pct = read / total if total else 0
    width = 28
    filled = int(width * pct)
    bar = "#" * filled + "." * (width - filled)
    short = name.rsplit("/", 1)[-1]
    sys.stderr.write(f"\r  {short:<24s} [{bar}] {pct*100:5.1f} %  {human(read):>9s}")
    sys.stderr.flush()
    if read >= total:
        sys.stderr.write("\n")
