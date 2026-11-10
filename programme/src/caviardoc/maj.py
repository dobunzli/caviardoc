"""Mise a jour : verifier, appliquer, remesurer, relancer.

Le depot est le canal de distribution. Une mise a jour amene donc trois choses
d'un coup, et il faut les traiter dans cet ordre :

1. **le code**, par `git` — ou, pour une copie telechargee en ZIP depuis
   GitHub, par l'archive du commit publie (voir « Sans git ») ;
2. **les dependances**, par `uv sync` depuis le `uv.lock` de la nouvelle
   version — c'est ce fichier qui les epingle, pas nous ;
3. **le modele**, par `caviardoc setup` : une nouvelle version peut epingler
   d'autres empreintes SHA-256, et le telechargement les verifie.

Puis une quatrieme, qui n'est pas une formalite : **remesurer**.

Le rappel annonce sur une machine vaut pour une version du code, du modele et
des bibliotheques. Les trois viennent de changer. Un outil qui se met a jour
en silence laisse l'utilisateur se fier a un chiffre qui ne decrit plus rien
— et ce chiffre sert a decider s'il confie un dossier patient a la machine.
La mise a jour compare donc avant et apres, et alerte si le rappel a baisse.

C'est aussi pourquoi rien ne s'applique sans confirmation explicite. Le depot
est public : le canal de mise a jour est un chemin d'execution de code vers
des postes qui detiennent des dossiers medicaux. La verification est
automatique ; l'application ne l'est pas.

Ce module n'utilise que la bibliotheque standard.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

#: Branche suivie quand aucun tag n'existe encore.
BRANCHE = "main"

class MajImpossible(RuntimeError):
    """La mise a jour ne peut pas se faire, et on sait pourquoi."""


@dataclass
class Revision:
    """Une version du depot, telle que git la nomme."""

    reference: str
    date: str = ""
    resume: str = ""


@dataclass
class Etat:
    """Ce que la verification a trouve."""

    locale: Revision
    distante: Revision | None = None
    retard: int = 0
    propre: bool = True
    empeche: str = ""
    changements: list[str] = field(default_factory=list)
    #: Copie sans git : le commit complet a telecharger. Celui-la et pas « la
    #: branche » : ce qu'on installe est ce qu'on a annonce, meme si un commit
    #: est publie entre la verification et l'application.
    cible: str = ""

    @property
    def disponible(self) -> bool:
        return self.retard > 0 and not self.empeche

    def vue(self) -> dict:
        return {
            "locale": self.locale.__dict__,
            "distante": self.distante.__dict__ if self.distante else None,
            "retard": self.retard,
            "propre": self.propre,
            "empeche": self.empeche,
            "disponible": self.disponible,
            "changements": self.changements,
        }


#: Ou chercher git quand celui du PATH ne demarre pas.
#:
#: Sur macOS, `/usr/bin/git` n'est qu'un relais vers les outils de
#: developpement. Quand Xcode est installe mais que sa licence n'a jamais ete
#: acceptee, ce relais refuse toute commande (code 69) — alors que le git des
#: Command Line Tools, a cote, fonctionne. La mise a jour prenait ce refus pour
#: un dossier hors depot, et conseillait de tout retelecharger a la main.
GIT_DE_SECOURS = (
    "/Library/Developer/CommandLineTools/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
)


@functools.lru_cache(maxsize=1)
def executable_git() -> str:
    """Un git qui demarre vraiment, ou `MajImpossible` qui dit pourquoi."""
    candidats = dict.fromkeys(c for c in (shutil.which("git"), *GIT_DE_SECOURS)
                              if c and Path(c).is_file())
    refus = ""
    for candidat in candidats:
        try:
            essai = subprocess.run([candidat, "--version"], capture_output=True,
                                   **paths.TEXTE, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            refus = refus or str(exc)
            continue
        if essai.returncode == 0:
            return candidat
        refus = refus or (essai.stderr or essai.stdout).strip()

    if not candidats:
        raise MajImpossible(
            "git est absent. La mise à jour automatique en a besoin : elle "
            "récupère le code depuis le dépôt. Installez git, ou "
            "retéléchargez le projet à la main.")
    if "license" in refus.lower():
        raise MajImpossible(
            "git ne démarre pas : la licence d'Xcode n'a pas encore été "
            "acceptée sur ce Mac. Ouvrez le Terminal, lancez "
            "« sudo xcodebuild -license accept », puis revenez ici.")
    raise MajImpossible(f"git ne démarre pas : {refus or 'cause inconnue'}")


def _git(*arguments: str, racine: Path | None = None,
         delai: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([executable_git(), *arguments],
                          cwd=str(racine or paths.repo_root()),
                          capture_output=True, **paths.TEXTE, timeout=delai)


def _revision(reference: str) -> Revision:
    res = _git("log", "-1", "--format=%h|%ad|%s", "--date=short", reference)
    if res.returncode != 0:
        return Revision(reference=reference)
    court, date, resume = (res.stdout.strip().split("|", 2) + ["", ""])[:3]
    return Revision(reference=court, date=date, resume=resume)


def est_un_depot() -> bool:
    """Le dossier du projet est-il une copie du depot git ?

    On regarde le dossier, sans demander a git : un git qui refuse de demarrer
    ne fait pas d'une copie du depot un dossier ordinaire, et les confondre
    donnait un conseil faux. `.git` peut aussi etre un fichier (worktree).
    """
    return (paths.repo_root() / ".git").exists()


def verifier(reseau: bool = True) -> Etat:
    """Compare la version installee a celle du depot distant.

    Ne modifie rien : `git fetch` ne touche pas a l'arbre de travail.
    """
    if not est_un_depot():
        return _verifier_archive(reseau)
    # Un git qui ne demarre pas leve ici, avec sa vraie raison.
    executable_git()

    etat = Etat(locale=_revision("HEAD"))

    # Les fichiers non suivis (un `.claudeignore` perso) n'empechent rien :
    # seul un fichier suivi et modifie serait ecrase. Si un nouveau commit
    # devait ecraser un fichier non suivi, `merge --ff-only` refuse (voir
    # `appliquer_code`).
    sale = _git("status", "--porcelain", "--untracked-files=no")
    etat.propre = not sale.stdout.strip()
    if not etat.propre:
        etat.empeche = (
            "Des fichiers du programme ont été modifiés sur cet ordinateur, "
            "et une mise à jour les écraserait. Enregistrez-les ou annulez-les "
            "d'abord.")

    if reseau:
        rapatrie = _git("fetch", "--quiet", "--tags", "origin")
        if rapatrie.returncode != 0:
            etat.empeche = etat.empeche or (
                "Vérification impossible : le serveur des mises à jour ne "
                "répond pas. Vérifiez la connexion à internet.")
            return etat

    distant = f"origin/{BRANCHE}"
    if _git("rev-parse", "--verify", "--quiet", distant).returncode != 0:
        etat.empeche = etat.empeche or f"{distant} est introuvable."
        return etat

    etat.distante = _revision(distant)
    compte = _git("rev-list", "--count", f"HEAD..{distant}")
    etat.retard = int(compte.stdout.strip() or 0)
    if etat.retard:
        journal = _git("log", "--format=%h  %s", f"HEAD..{distant}")
        etat.changements = [l for l in journal.stdout.splitlines() if l.strip()]
    return etat


# --------------------------------------------------------------------------- #
# Le rappel mesure, avant et apres
# --------------------------------------------------------------------------- #

def _dev_installe() -> bool:
    """L'extra « dev » est-il present dans le venv de cette machine ?

    On se contente de chercher pytest : c'est ce que l'extra apporte, et le
    seul marqueur observable sans interroger uv.
    """
    venv = paths.venv_dir()
    motifs = ("lib/python*/site-packages/pytest",
              "Lib/site-packages/pytest")          # Windows
    return any(any(venv.glob(m)) for m in motifs)


def extras_sync() -> list[str]:
    """Extras a passer a `uv sync` lors d'une mise a jour.

    Toujours le moteur et la couche document — sans eux l'outil ne fonctionne
    pas. L'extra « dev » seulement s'il etait deja installe : on preserve ce
    que la machine avait, sans l'imposer a celles qui ne testent pas.
    """
    extras = ["--extra", "engine", "--extra", "documents"]
    if _dev_installe():
        extras += ["--extra", "dev"]
    return extras


def dernier_rappel() -> float | None:
    """Le plus bas rappel de la derniere mesure, ou None si jamais mesuree.

    Le plus bas, et non la moyenne : il decrit le pire cas, et c'est le pire
    cas qui compte quand on protege une identite.
    """
    from . import models

    mesure = models.derniere_mesure()
    if not mesure:
        return None
    rappels = [v for c, v in mesure.items()
               if c.startswith("rappel_") and isinstance(v, (int, float))]
    return min(rappels) if rappels else None


def noter_rappel(valeur: float) -> None:
    """Enregistre un rappel isole, quand la mesure detaillee n'est pas la.

    Le relevé complet est ecrit par `tools/mesurer.py --enregistrer` : un seul
    ecrivain, une seule forme dans `state.json`. Cette fonction n'existe que
    pour les cas ou l'on ne dispose que du chiffre.
    """
    from . import models

    models.noter_mesure(models.modele_actif(),
                        {"rappel_complet": float(valeur) / 100},
                        tenu=float(valeur) >= 90.0)


NIVEAUX_MESURE = (("rappel_complet", "complet"), ("rappel_patient", "patient"))

#: Baisse tolerable entre deux mesures (points de pourcentage).
TOLERANCE_RAPPEL = 0.5


def empreinte_corpus(racine: Path | None = None) -> str | None:
    """sha256 du corpus de mesure, ou None s'il n'est pas sur cette machine."""
    import hashlib
    fichier = ((racine or paths.project_root())
               / "tests" / "corpus" / "corpus.json")
    try:
        return hashlib.sha256(fichier.read_bytes()).hexdigest()
    except OSError:
        return None


def comparer_mesures(avant: dict | None, apres: dict | None) -> dict:
    """Compare deux releves, niveau par niveau.

    Rend ``{"comparable": bool, "baisses": [...]}``. Deux releves ne se
    comparent que s'ils sont complets (les deux rappels), pris sur le meme
    corpus (empreinte) et avec le meme modele : sinon l'ecart ne dit rien du
    code, et l'annoncer serait une fausse alerte.
    """
    def complet(m) -> bool:
        return (isinstance(m, dict) and all(
            isinstance(m.get(cle), (int, float)) for cle, _ in NIVEAUX_MESURE))

    if not (complet(avant) and complet(apres) and avant.get("corpus")
            and avant.get("corpus") == apres.get("corpus")
            and avant.get("modele") == apres.get("modele")):
        return {"comparable": False, "baisses": []}
    baisses = [{"niveau": nom, "avant": avant[cle], "apres": apres[cle]}
               for cle, nom in NIVEAUX_MESURE
               if apres[cle] < avant[cle] - TOLERANCE_RAPPEL]
    return {"comparable": True, "baisses": baisses}


def version_du_code(racine: Path | None = None) -> str:
    """``__version__`` lue dans le fichier, pas dans le module deja charge :
    apres une mise a jour, ce dernier decrirait l'ancienne version."""
    try:
        texte = ((racine or paths.project_root()) / "src" / "caviardoc"
                 / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', texte, re.M)
    return m.group(1) if m else ""


def version_de_la_cible(reference: str) -> str:
    """Numero de version de la cible : le tag qui pointe dessus, sinon la
    version inscrite dans le code. Chaine vide si rien ne la donne ; ne leve
    jamais, la mise a jour ne doit pas en dependre."""
    try:
        if reference and est_un_depot():
            res = _git("describe", "--tags", "--exact-match", reference,
                       delai=30)
            tag = res.stdout.strip().removeprefix("v")
            if res.returncode == 0 and _numeros(tag) is not None:
                return tag
    except Exception:
        pass
    try:
        return version_du_code()
    except Exception:
        return ""


_LIGNE_MESURE = re.compile(r"(rappel_\w+)\s+([\d.]+)\s*%")

# Vrai quand la derniere mesure n'a pas pu avoir lieu faute d'outil (ou n'a
# rien rendu a lire) : une installation sans outil de mesure n'a rien a
# remesurer. Distinct d'une mesure lancee qui plante, qui reste une erreur.
_mesure_sans_outil = False


def mesurer(journal=None) -> float | None:
    """Relance la mesure et rend le plus bas des rappels.

    Le plus bas, et non la moyenne : c'est celui qui decrit le pire cas, et
    c'est le pire cas qui compte quand on protege une identite.
    """
    global _mesure_sans_outil
    from . import doctor

    _mesure_sans_outil = False
    outil = paths.project_root() / "tools" / "mesurer.py"
    python = doctor._venv_python(paths.venv_dir())
    if not outil.is_file() or not python.is_file():
        _mesure_sans_outil = True
        return None

    # `--enregistrer` : la mesure d'apres mise a jour est celle qui doit faire
    # foi. Elle ecrit le releve complet, et ne retient le modele que s'il tient
    # encore ses seuils — une mise a jour qui degrade le rappel ne doit pas
    # laisser la machine pointer vers un modele qu'elle vient de disqualifier.
    res = subprocess.run([str(python), str(outil), "--enregistrer"],
                         cwd=str(paths.project_root()),
                         capture_output=True, **paths.TEXTE, timeout=1800)
    if journal is not None:
        for ligne in (res.stdout or "").splitlines():
            journal(ligne)
        if res.returncode != 0:
            for ligne in (res.stderr or "").splitlines():
                journal(ligne)
    if res.returncode != 0:
        # Une mesure qui a plante n'est pas une mesure : ne rien en tirer.
        return None
    rappels = [float(v) for _, v in _LIGNE_MESURE.findall(res.stdout or "")]
    return min(rappels) if rappels else None


# --------------------------------------------------------------------------- #
# Appliquer
# --------------------------------------------------------------------------- #

def appliquer(journal=None, remesurer: bool = True) -> dict:
    """Applique la mise a jour, puis remesure. Rend un compte rendu.

    N'est jamais appelee toute seule : c'est une action de l'utilisateur.
    Les deux temps d'affilee, dans ce processus : c'est la forme de la ligne
    de commande. La page, elle, redemarre entre les deux (`appliquer_code`).
    """
    compte = appliquer_code(journal)
    if not compte["applique"]:
        return compte
    return appliquer_suite(compte, journal, remesurer)


def _noteur(journal):
    def noter(ligne: str) -> None:
        if journal is not None:
            journal(f"{time.strftime('%H:%M:%S')}  {ligne}")
    return noter


def appliquer_code(journal=None) -> dict:
    """Premier temps : le code seul. Rend le compte rendu a completer.

    La page redemarre ensuite avant `appliquer_suite`. Sous Windows, une
    bibliotheque chargee par le processus — onnxruntime, torch, PyMuPDF des
    qu'un document a ete traite — ne peut pas etre remplacee : `uv sync`
    echouait sur « acces refuse » des que la mise a jour en changeait une.
    Un processus neuf ne les a pas encore chargees.
    """
    noter = _noteur(journal)

    # Un second temps inacheve (echec de `uv sync`, de la mesure, processus
    # quitte) : le code est deja a jour, mais la machine ne l'est pas. Sans
    # cette reprise, `verifier` ne compare que les versions du code et
    # repondait « deja a jour ».
    reste = _suite_inachevee()
    if reste is not None:
        noter("second temps d'une mise a jour precedente a reprendre")
        return reste

    etat = verifier()
    if etat.empeche:
        raise MajImpossible(etat.empeche)
    if not etat.disponible:
        return {"applique": False, "motif": "deja a jour",
                "revision": etat.locale.reference}

    avant = dernier_rappel()
    from . import models
    mesure_avant = models.derniere_mesure()
    version_de = version_du_code()
    depart = etat.locale.reference
    noter(f"version installee : {depart}")
    noter(f"version disponible : {etat.distante.reference} "
          f"({etat.retard} commit(s))")

    if est_un_depot():
        # `--ff-only` : jamais de fusion automatique. Si l'historique a
        # diverge, on s'arrete plutot que de fabriquer un etat que personne
        # n'a teste.
        noter("$ git merge --ff-only origin/" + BRANCHE)
        fusion = _git("merge", "--ff-only", f"origin/{BRANCHE}")
        for ligne in (fusion.stdout + fusion.stderr).splitlines():
            noter(ligne)
        if fusion.returncode != 0:
            sortie = fusion.stdout + fusion.stderr
            if "untracked working tree files" in sortie:
                raise MajImpossible(
                    "Mise à jour impossible : un fichier que vous avez "
                    "ajouté dans le dossier du programme porte le même nom "
                    "qu'un fichier de la nouvelle version. Déplacez-le ou "
                    "renommez-le, puis relancez. Rien n'a été changé.")
            raise MajImpossible(
                "Mise à jour impossible : cette copie a divergé de la version "
                "publiée. Rien n'a été changé.")
    else:
        _appliquer_archive(etat.cible, noter)

    compte = {
        "applique": True,
        "de": depart,
        "vers": etat.distante.reference,
        "commits": etat.retard,
        "rappel_avant": avant,
        "mesure_avant": mesure_avant,
        "version_de": version_de,
        "version_vers": version_de_la_cible(
            etat.cible or etat.distante.reference),
    }
    # Durable des maintenant : `appliquer_suite` l'efface en cas de succes.
    _noter_suite(compte)
    return compte


def appliquer_suite(compte: dict, journal=None,
                    remesurer: bool = True) -> dict:
    """Second temps : dependances, modele, remesure. Complete ``compte``."""
    noter = _noteur(journal)

    # --- 2. les dependances ------------------------------------------------
    uv = paths.uv_exe()
    if uv:
        # `uv sync` elague : ce qui n'est pas demande est desinstalle. Sans
        # cette precaution, une mise a jour retirait pytest — installe par
        # l'extra « dev » — et la suite de tests devenait injouable apres
        # chaque `maj`. Sur un projet dont la discipline est de mesurer apres
        # chaque changement, retirer en silence de quoi verifier est le
        # contraire de ce qu'on veut.
        #
        # On ne l'impose pas pour autant : l'extra n'est ajoute que s'il etait
        # deja la. Une machine qui ne teste pas garde un venv leger.
        extras = extras_sync()
        noter("$ uv sync --locked " + " ".join(extras))
        env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(paths.venv_dir()))
        sync = subprocess.run(
            [uv, "sync", "--locked", *extras],
            cwd=str(paths.project_root()), env=env,
            capture_output=True, **paths.TEXTE, timeout=1800)
        for ligne in (sync.stdout + sync.stderr).splitlines():
            noter(ligne)
        if sync.returncode != 0:
            raise MajImpossible(
                "Le code est à jour, mais les dépendances n'ont pas pu être "
                "installées : elles doivent correspondre exactement au fichier "
                "uv.lock, et ce n'est pas le cas ou le réseau a échoué. "
                "Relancez la mise à jour ; si l'erreur persiste, le détail "
                "est dans le journal.")
    else:
        noter("uv absent : dependances non mises a jour")

    # --- 3. le modele ------------------------------------------------------
    # Une nouvelle version peut epingler d'autres empreintes : `setup` les
    # verifie, et refuse un fichier qui ne correspond pas.
    from . import models
    spec = models.REGISTRY[models.modele_actif()]
    controle = models.check(spec)
    if not controle.complete:
        noter(f"modele {spec.key} : {models.human(controle.missing_bytes)} "
              "a telecharger")
        models.download(spec)
        noter("modele installe et verifie")
    else:
        noter(f"modele {spec.key} : inchange")

    # `mesurer` enregistre lui-meme le releve complet : ne rien reecrire ici,
    # ce serait remplacer une mesure detaillee par un chiffre isole.
    global _mesure_sans_outil
    _mesure_sans_outil = False
    apres = mesurer(journal=noter) if remesurer else None
    if remesurer and apres is None and _mesure_sans_outil:
        noter("aucun outil de mesure sur cette installation : mesure ignoree")
    elif remesurer and apres is None:
        # Ne pas annoncer « terminee » : l'alerte de baisse de rappel est la
        # seule qui existe, et elle ne peut pas se declencher sans mesure.
        # Le second temps reste a faire (marqueur conserve).
        raise MajImpossible(
            "Le code et les dépendances sont à jour, mais la mesure du "
            "rappel a échoué : le chiffre affiché n'est plus valable. "
            "Relancez la mise à jour, ou mesurez depuis l'onglet Installation "
            "avant de confier un document à cette machine.")

    compte = dict(compte, rappel_apres=apres, regression=False,
                  baisses=[], mesure_comparable=None)
    if apres is not None:
        from . import models
        verdict = comparer_mesures(compte.get("mesure_avant"),
                                   models.derniere_mesure())
        compte.update(mesure_comparable=verdict["comparable"],
                      baisses=verdict["baisses"],
                      regression=bool(verdict["baisses"]))
        if not verdict["comparable"]:
            noter("mesure precedente non comparable : "
                  "nouvelle reference enregistree")
        for b in verdict["baisses"]:
            noter(f"ALERTE : le rappel {b['niveau']} passe de "
                  f"{b['avant']:.1f} % a {b['apres']:.1f} %")
    # Le second temps est acheve : plus rien a reprendre.
    (paths.data_dir() / FICHIER_SUITE).unlink(missing_ok=True)
    return compte


#: Entre les deux temps, la page redemarre : ce fichier porte le compte rendu
#: du premier au processus qui fera le second.
FICHIER_ATTENTE = "maj-en-attente.json"


#: Le second temps n'est acheve que lorsque `appliquer_suite` va a son terme :
#: ce fichier, lui, survit au redemarrage et a un echec.
FICHIER_SUITE = "maj-suite-a-faire.json"


def _noter_suite(compte: dict) -> None:
    try:
        paths.ensure_dirs()
        (paths.data_dir() / FICHIER_SUITE).write_text(
            json.dumps(compte), encoding="utf-8")
    except OSError:
        pass          # le marqueur aide la reprise, il ne doit pas l'empecher


def _suite_inachevee() -> dict | None:
    try:
        compte = json.loads(
            (paths.data_dir() / FICHIER_SUITE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return compte if isinstance(compte, dict) and compte.get("applique") else None


def noter_attente(compte: dict) -> None:
    """Retient qu'une mise a jour attend son second temps."""
    paths.ensure_dirs()
    (paths.data_dir() / FICHIER_ATTENTE).write_text(
        json.dumps(compte), encoding="utf-8")


def reprendre_attente() -> dict | None:
    """Le compte rendu d'un premier temps, une seule fois ; None s'il n'y en a pas."""
    fichier = paths.data_dir() / FICHIER_ATTENTE
    try:
        compte = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        fichier.unlink(missing_ok=True)
    return compte if isinstance(compte, dict) and compte.get("applique") else None


# --------------------------------------------------------------------------- #
# Sans git : l'archive publiee par GitHub
# --------------------------------------------------------------------------- #
#
# Le README fait telecharger le projet par « Download ZIP » : une telle copie
# n'a pas de `.git`, et la mise a jour lui etait refusee — c'est-a-dire a
# presque toutes les installations, et a toutes celles sous Windows.
#
# GitHub produit ce ZIP avec `git archive`, qui remplit `version_archive.py`
# (attribut `export-subst` dans `.gitattributes`) : la copie sait de quel
# commit elle vient. La verification compare ce commit a la branche publiee
# par l'API de GitHub ; l'application telecharge l'archive de ce commit
# precis et remplace les fichiers. Les deux passent par HTTPS, comme le
# `git fetch` de l'autre canal : la confiance est la meme.

#: Le depot publie, « proprietaire/nom ».
DEPOT_GITHUB = "dobunzli/caviardoc"

#: Au-dela, une archive n'est pas celle du projet (quelques megaoctets).
TAILLE_MAX_ARCHIVE = 100 * 1024 * 1024

#: Dossiers entierement a nous : un fichier qui n'est plus dans la nouvelle
#: version y est supprime. Ailleurs on ne fait que remplacer — la racine est
#: le dossier de l'utilisateur, qui a pu y poser autre chose.
DOSSIERS_REMPLACES = ("programme/src", "programme/tests", "programme/tools",
                      "programme/docs")


def commit_archive(racine: Path | None = None) -> tuple[str, str] | None:
    """(commit, date) inscrits par `git archive`, ou None hors d'une archive.

    Lu dans le fichier et non importe : apres une mise a jour, le module deja
    charge decrirait l'ancienne version.
    """
    fichier = (racine or paths.project_root()) / "src" / "caviardoc" / "version_archive.py"
    try:
        texte = fichier.read_text(encoding="utf-8")
    except OSError:
        return None
    commit = re.search(r'COMMIT = "([0-9a-f]{40})"', texte)
    date = re.search(r'DATE = "(\d{4}-\d{2}-\d{2})"', texte)
    if commit is None:
        return None      # copie git, ou copie faite a la main : non renseigne
    return commit.group(1), date.group(1) if date else ""


def _github(chemin: str, delai: int = 30, introuvable: str = "") -> bytes:
    """Le corps d'une reponse de GitHub ; `MajImpossible` qui dit pourquoi.

    `introuvable` : le message d'un 404, quand l'appelant sait ce qui manque.
    """
    import urllib.error
    import urllib.request

    url = (chemin if chemin.startswith("https://")
           else f"https://api.github.com/repos/{DEPOT_GITHUB}/{chemin}")
    requete = urllib.request.Request(url, headers={
        "User-Agent": "caviardoc", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(requete, timeout=delai) as reponse:
            corps = reponse.read(TAILLE_MAX_ARCHIVE + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MajImpossible(introuvable or (
                "Vérification impossible : le dépôt des mises à jour est "
                "introuvable sur GitHub (privé ou renommé).")) from exc
        if exc.code in (403, 429):
            raise MajImpossible(
                "GitHub limite le nombre de vérifications : réessayez dans "
                "une heure.") from exc
        raise MajImpossible(f"GitHub a répondu {exc.code}.") from exc
    except (OSError, ValueError) as exc:
        raise MajImpossible(
            "Vérification impossible : le serveur des mises à jour ne "
            "répond pas. Vérifiez la connexion à internet.") from exc
    if len(corps) > TAILLE_MAX_ARCHIVE:
        raise MajImpossible("Réponse inattendue du serveur des mises à jour.")
    return corps


#: Les seules adresses de notes de version transmises a l'interface.
PREFIXE_NOTES = f"https://github.com/{DEPOT_GITHUB}/releases/"


def _numeros(version: str) -> tuple[int, ...] | None:
    """« v1.2.3 » -> (1, 2, 3) ; None si ce n'est pas une version numerique."""
    version = version.strip().removeprefix("v")
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(n) for n in version.split("."))


def notes_installee() -> dict:
    """Les notes de la version installee : adresse construite, sans reseau."""
    from . import __version__
    return {"version": __version__,
            "url": f"{PREFIXE_NOTES}tag/v{__version__}"}


def notes_nouvelle() -> dict | None:
    """{version, url} de la derniere release si elle est plus recente.

    Ne leve jamais : sans reponse exploitable, il n'y a simplement pas de
    lien, et la verification de mise a jour n'en est pas touchee.
    """
    from . import __version__
    try:
        release = json.loads(_github("releases/latest", delai=10))
        version = str(release.get("tag_name", "")).strip().removeprefix("v")
        url = str(release.get("html_url", ""))
        nouvelle, locale = _numeros(version), _numeros(__version__)
        if (nouvelle is None or locale is None or nouvelle <= locale
                or not url.startswith(PREFIXE_NOTES)):
            return None
        return {"version": version, "url": url}
    except Exception:
        return None


def vue_avec_notes(etat: Etat) -> dict:
    """`etat.vue()` et les liens vers les notes de version."""
    vue = etat.vue()
    vue["notes_installee"] = notes_installee()
    if etat.disponible:
        notes = notes_nouvelle()
        if notes:
            vue["notes"] = notes
    return vue


def _verifier_archive(reseau: bool) -> Etat:
    locale = commit_archive()
    if locale is None:
        raise MajImpossible(
            "Ce programme n'a été installé ni avec git, ni depuis l'archive "
            "ZIP de GitHub : sa version est inconnue, et la mise à jour "
            "automatique impossible. Téléchargez la nouvelle version à la "
            "main.")
    commit, date = locale
    etat = Etat(locale=Revision(reference=commit[:7], date=date))
    if not reseau:
        return etat

    try:
        # La tete de la branche d'abord, puis la comparaison avec elle : la
        # liste des commits d'une comparaison est tronquee au-dela de 250, et
        # son dernier element n'est alors plus la version publiee.
        tete = json.loads(_github(f"commits/{BRANCHE}"))   # 404 : depot
        cible = str(tete.get("sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", cible):
            raise ValueError(cible)
        comparaison = json.loads(_github(
            f"compare/{commit}...{cible}",
            introuvable="Vérification impossible : cette version ne figure "
                        "pas parmi les versions publiées (copie non publiée ?). "
                        "Téléchargez la nouvelle version à la main."))
    except MajImpossible as exc:
        etat.empeche = str(exc)
        return etat
    except ValueError:
        etat.empeche = "Réponse illisible du serveur des mises à jour."
        return etat

    if comparaison.get("status") not in ("ahead", "identical"):
        etat.empeche = ("Cette copie ne correspond pas à la version publiée : "
                        "la mise à jour automatique ne peut rien garantir. "
                        "Téléchargez la nouvelle version à la main.")
        return etat

    etat.retard = int(comparaison.get("ahead_by") or 0)
    if not etat.retard:
        etat.distante = etat.locale
        return etat

    def resume(c: dict) -> str:
        lignes = ((c.get("commit") or {}).get("message") or "").splitlines()
        return lignes[0] if lignes else ""

    quand = ((tete.get("commit") or {}).get("committer") or {}).get("date", "")
    etat.cible = cible
    etat.distante = Revision(reference=cible[:7], date=quand[:10],
                             resume=resume(tete))
    # Le plus recent d'abord, comme `git log`.
    etat.changements = [f"{c.get('sha', '')[:7]}  {resume(c)}"
                        for c in reversed(comparaison.get("commits") or [])]
    return etat


def _membres_archive(archive) -> dict:
    """Chemins relatifs a la racine du depot, valides un par un.

    Le ZIP de GitHub range tout sous un dossier « depot-commit/ ». Aucun
    chemin ne doit en sortir, ni remonter : on ecrit dans le dossier de
    l'utilisateur.
    """
    membres: dict = {}
    noms = [m for m in archive.infolist() if not m.is_dir()]
    if not noms:
        raise MajImpossible("Archive de mise à jour vide.")
    prefixe = noms[0].filename.split("/", 1)[0] + "/"
    for membre in noms:
        nom = membre.filename
        relatif = nom[len(prefixe):] if nom.startswith(prefixe) else ""
        morceaux = relatif.split("/")
        if (not relatif or relatif.startswith("/") or "\\" in relatif
                or any(m in ("", ".", "..") for m in morceaux)
                or ":" in morceaux[0]):
            raise MajImpossible(f"Archive de mise à jour suspecte : {nom!r}.")
        membres[relatif] = membre
    for indispensable in ("programme/pyproject.toml",
                          "programme/src/caviardoc/__init__.py",
                          "programme/src/caviardoc/version_archive.py"):
        if indispensable not in membres:
            raise MajImpossible("L'archive téléchargée n'est pas celle de "
                                "Caviardoc. Rien n'a été changé.")
    return membres


def _remplacer(provisoire: Path, cible: Path) -> None:
    """`os.replace`, en reessayant : sous Windows, un antivirus ou l'indexation
    tient souvent un instant le fichier qu'on veut remplacer."""
    for pause in (0.2, 0.5, 1.0, 2.0, 0.0):
        try:
            os.replace(provisoire, cible)
            return
        except PermissionError:
            if not pause:
                provisoire.unlink(missing_ok=True)
                raise
            time.sleep(pause)


def _appliquer_archive(commit: str, noter) -> None:
    """Remplace les fichiers du programme par ceux de l'archive de ``commit``."""
    import io
    import zipfile

    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise MajImpossible("Version cible inconnue : relancez la vérification.")
    url = f"https://codeload.github.com/{DEPOT_GITHUB}/zip/{commit}"
    noter(f"telechargement de {url}")
    corps = _github(url, delai=120)
    try:
        archive = zipfile.ZipFile(io.BytesIO(corps))
    except zipfile.BadZipFile as exc:
        raise MajImpossible("Archive de mise à jour illisible. Rien n'a été "
                            "changé.") from exc
    with archive:
        membres = _membres_archive(archive)
        # Tout est lu avant que rien ne soit ecrit : une archive tronquee
        # echoue ici, pas a mi-chemin du remplacement.
        contenus = {rel: archive.read(m) for rel, m in membres.items()}

    racine = paths.repo_root()
    ecrits = 0
    # `version_archive.py` definit la version installee : il est ecrit en
    # dernier. Si l'ecriture echoue en route (fichier tenu sous Windows,
    # disque plein, processus tue), la copie continue de se dire ancienne et
    # la mise a jour peut etre relancee ; ecrit au milieu, il la ferait
    # passer pour a jour sur un arbre mele.
    marqueur = "programme/src/caviardoc/version_archive.py"
    ordre = sorted(contenus, key=lambda rel: rel == marqueur)
    for relatif in ordre:
        donnees = contenus[relatif]
        cible = racine / relatif
        cible.parent.mkdir(parents=True, exist_ok=True)
        provisoire = cible.with_name(cible.name + ".maj")
        provisoire.write_bytes(donnees)
        mode = (membres[relatif].external_attr >> 16) & 0o777
        if mode & 0o111 and os.name != "nt":
            provisoire.chmod(0o755)
        _remplacer(provisoire, cible)
        ecrits += 1
    noter(f"{ecrits} fichiers remplaces")

    retires = 0
    for dossier in DOSSIERS_REMPLACES:
        base = racine / dossier
        if not base.is_dir():
            continue
        for fichier in base.rglob("*"):
            relatif = fichier.relative_to(racine).as_posix()
            if (fichier.is_file() and relatif not in contenus
                    and "__pycache__" not in fichier.parts):
                # Un reste qu'on ne peut pas retirer ne gene pas la nouvelle
                # version : on le dit, on n'interrompt rien.
                try:
                    fichier.unlink()
                    retires += 1
                except OSError as exc:
                    noter(f"{relatif} : non retire ({exc.strerror or exc})")
    if retires:
        noter(f"{retires} fichiers retires (absents de la nouvelle version)")


def relancer(argv: list[str] | None = None) -> None:
    """Remplace le processus courant par une version fraiche du meme.

    `execv` plutot qu'un sous-processus : le code Python deja charge en
    memoire est celui d'avant la mise a jour, et continuer a l'executer
    reviendrait a faire tourner l'ancienne version en croyant la nouvelle.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable,
             [sys.executable, "-m", "caviardoc", *(argv or ["interface"])])
