"""Controle de l'environnement : ce qui est present, ce qui manque, comment le reparer.

Concu pour tourner sur une machine neuve, avec un Python nu et aucune
dependance installee. N'importe que la bibliotheque standard.
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import __version__, models, paths, tesseract

MIN_PYTHON = (3, 11)
#: En deca de ce seuil, la couche generative facultative reste desactivee.
COMFORTABLE_RAM_GB = 12

#: Plancher reel, en deca duquel la session ONNX ne tient pas.
#:
#: Ce plancher etait fixe a 8 Go, ce qui confondait deux choses : la cible de
#: conception — tenir dans 8 Go — et le minimum vital. Une machine de 7 Go se
#: voyait refuser l'installation alors qu'elle fait tourner le moteur sans
#: peine. Un objectif n'est pas une condition d'entree.
MIN_RAM_GB = 4

#: Entre ce seuil et le plancher, le modele tient mais sans marge, et on le
#: dit. Il occupe environ 1,8 Go ; il n'existe pas de variante plus legere
#: assez fiable (les quantifications entieres ont ete retirees).
ETROIT_RAM_GB = 8


class Level(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Check:
    name: str
    level: Level
    detail: str
    fix: str = ""
    #: True si `caviardoc setup` sait reparer ce point tout seul.
    auto_fixable: bool = False


# --------------------------------------------------------------------------- #
# Sondes systeme
# --------------------------------------------------------------------------- #

def total_ram_bytes() -> int | None:
    """Memoire vive totale, ou None si indeterminable."""
    try:
        if sys.platform == "win32":
            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return int(stat.ullTotalPhys)

        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, **paths.TEXTE, timeout=5)
            if out.returncode == 0:
                return int(out.stdout.strip())

        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        return None


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _can_import(module: str, python: Path | None = None) -> bool:
    if python is None:
        try:
            __import__(module)
            return True
        except Exception:
            return False
    if not python.is_file():
        return False
    try:
        out = subprocess.run([str(python), "-c", f"import {module}"],
                             capture_output=True, timeout=60)
        return out.returncode == 0
    except Exception:
        return False


def essai_import(modules, python: Path,
                 delai: int = 900) -> tuple[bool, str]:
    """Charge vraiment ``modules`` dans ``python`` ; rend (reussi, cause).

    A la difference de `_can_import`, l'echec dit pourquoi — derniere ligne
    de l'erreur, ou delai depasse — au lieu d'un simple « non ». Et le delai
    est long : le premier import de gliner tire torch et transformers, et
    au premier lancement, ou sur une machine chargee, il depasse la minute.
    Une minute suffisait a faire annoncer « introuvable » un paquet installe.
    """
    if not python.is_file():
        return False, f"interpréteur absent : {python}"
    try:
        res = subprocess.run([str(python), "-c", "import " + ", ".join(modules)],
                             capture_output=True, **paths.TEXTE, timeout=delai)
    except subprocess.TimeoutExpired:
        return False, f"chargement interrompu après {delai // 60} min"
    except OSError as exc:
        return False, str(exc)
    if res.returncode == 0:
        return True, ""
    lignes = [l for l in res.stderr.splitlines() if l.strip()]
    return False, (lignes[-1].strip() if lignes else f"code {res.returncode}")


#: Module dont la presence decide qu'un interpreteur peut charger le moteur.
#: Le plus lourd de la chaine, et le dernier a s'installer : s'il est la, le
#: reste l'est.
MODULE_MOTEUR = "gliner"


def _module_present(module: str, python: Path | None = None) -> bool:
    """Le module est-il importable, sans l'importer ?

    `_can_import` execute vraiment l'import. Pour gliner cela charge torch :
    plusieurs secondes, a chaque demarrage de la ligne de commande. Ici on veut
    seulement savoir ou il se trouve, donc on regarde sans ouvrir.
    """
    if python is None:
        return importlib.util.find_spec(module) is not None
    if not python.is_file():
        return False
    code = ("import importlib.util, sys; "
            f"sys.exit(0 if importlib.util.find_spec({module!r}) else 1)")
    try:
        return subprocess.run([str(python), "-c", code],
                              capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def interprete_du_moteur() -> Path | None:
    """L'interpreteur capable de charger le moteur, ou None s'il n'y en a pas.

    Constater que les dependances sont installees ne suffit pas : encore
    faut-il les executer depuis l'interpreteur qui les a. Elles vivent dans le
    venv, alors que les lanceurs demarrent `caviardoc` avec le Python d'amorcage
    — celui qui n'a que la bibliotheque standard, et c'est son role.

    Sans cette distinction, l'installation se declare complete, le diagnostic
    est vert, et le premier document soumis echoue sur « dependances du moteur
    absentes » : le message est exact, mais il designe le mauvais coupable.
    """
    if _module_present(MODULE_MOTEUR):
        return Path(sys.executable)
    candidat = _venv_python(paths.venv_dir())
    if _module_present(MODULE_MOTEUR, candidat):
        return candidat
    return None


# --------------------------------------------------------------------------- #
# Controles
# --------------------------------------------------------------------------- #

def check_python() -> Check:
    v = sys.version_info
    cur = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        return Check("Python", Level.FAIL, f"{cur} — il en faut au moins "
                     f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
                     fix="Relancer par le lanceur de la plateforme : il installe un "
                         "Python convenable (ou python.org, Homebrew, winget).")
    return Check("Python", Level.OK, f"{cur} ({platform.python_implementation()})")


def check_version() -> Check:
    return Check("Caviardoc", Level.OK, __version__)


def check_platform() -> Check:
    return Check("Plateforme", Level.OK,
                 f"{platform.system()} {platform.release()} · {platform.machine()}")


def check_ram() -> Check:
    ram = total_ram_bytes()
    if ram is None:
        return Check("Memoire vive", Level.WARN, "indeterminable")
    gb = ram / (1024 ** 3)
    if gb < MIN_RAM_GB:
        return Check("Memoire vive", Level.FAIL, f"{gb:.0f} Go — insuffisant",
                     fix=f"Le modele occupe environ 1,8 Go ; il en faut au "
                         f"moins {MIN_RAM_GB} au total.")
    if gb < ETROIT_RAM_GB:
        # Il n'y a pas de repli plus leger : les quantifications entieres n'ont
        # jamais atteint 90 % de rappel sur une machine mesuree et ont ete
        # retirees. Sur une petite machine, le choix n'est pas entre deux
        # modeles, il est entre ce modele et rien.
        return Check("Memoire vive", Level.WARN, f"{gb:.0f} Go — juste",
                     fix="Le modele occupe environ 1,8 Go : le traitement "
                         "tiendra mais laissera peu de marge. Il n'existe pas "
                         "de variante plus legere assez fiable.")
    if gb < COMFORTABLE_RAM_GB:
        return Check("Memoire vive", Level.WARN, f"{gb:.0f} Go — profil contraint",
                     fix="La couche generative facultative restera desactivee. "
                         "Le moteur de detection fonctionne normalement.")
    return Check("Memoire vive", Level.OK, f"{gb:.0f} Go — profil confortable")


def check_data_dir() -> Check:
    d = paths.data_dir()
    if paths.is_inside_synced_folder(d):
        return Check("Repertoire de donnees", Level.FAIL,
                     f"{d} se trouve dans le depot",
                     fix=f"Definir {paths.ENV_DATA_DIR} vers un chemin hors du depot.")
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".ecriture"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("Repertoire de donnees", Level.FAIL, f"{d} : {exc}",
                     fix="Verifier les droits d'ecriture.")
    return Check("Repertoire de donnees", Level.OK, str(d))


def check_migration() -> list[Check]:
    """Signale chaque repertoire de donnees laisse par un ancien nom du projet."""
    controles = []
    for ancien in paths.anciens_data_dirs():
        taille = sum(f.stat().st_size for f in ancien.rglob("*") if f.is_file())
        controles.append(Check(
            "Ancien repertoire", Level.WARN,
            f"{ancien} ({models.human(taille)})",
            fix="Laisse par une version precedente. Le modele et les "
                "corrections en sont repris au demarrage ; le reste n'est plus "
                "utilise. Pour le supprimer : onglet Installation, section "
                "Desinstallation."))
    return controles


def check_disk(spec: models.ModelSpec) -> Check:
    free = models.free_space(paths.data_dir())
    needed = spec.total_size * 2  # marge pour les fichiers .part
    if free < needed:
        return Check("Espace disque", Level.FAIL,
                     f"{models.human(free)} libres, il en faut {models.human(needed)}",
                     fix="Liberer de l'espace sur le volume systeme.")
    return Check("Espace disque", Level.OK, f"{models.human(free)} libres")


def check_uv() -> Check:
    exe = paths.uv_exe()
    if not exe:
        return Check("uv", Level.WARN, "absent",
                     fix="`caviardoc setup` l'installe, ou l'installateur guide "
                         "(« ▶ Ouvrir Caviardoc », onglet Installation).")
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True, **paths.TEXTE,
                             timeout=10).stdout.strip()
    except Exception:
        ver = "version inconnue"
    return Check("uv", Level.OK, ver)


#: Ce qu'il faut faire quand Tesseract manque. Ce n'est plus une commande a
#: taper : l'installateur le pose lui-meme, dans le dossier de l'application.
_REMEDE_TESSERACT = ("Obligatoire pour tout PDF (sans lui, Caviardoc le refuse). "
                     "`caviardoc setup` l'installe, ou l'installateur guide "
                     "(« ▶ Ouvrir Caviardoc », en tete du dossier), "
                     "onglet Installation.")


def check_tesseract() -> Check:
    exe = tesseract.binaire()
    if exe is None:
        return Check("Tesseract (OCR)", Level.FAIL,
                     "absent : aucun PDF ne sera traite",
                     fix=_REMEDE_TESSERACT, auto_fixable=True)

    # D'ou vient-il ? La distinction compte pour le remede : le notre, on sait
    # le reparer ; celui du systeme, on n'y touche pas.
    notre = tesseract.binaire_local() is not None
    origine = "installe par nous" if notre else f"du systeme ({exe})"

    langs = tesseract.langues()
    if "fra" not in langs:
        # Bloquant : Caviardoc refuse un PDF sans le francais (tesseract.exiger).
        return Check("Tesseract (OCR)", Level.FAIL,
                     f"present ({origine}), mais sans le francais "
                     f"({len(langs)} langues) : aucun PDF ne sera traite",
                     fix="`caviardoc setup` installe une copie avec le "
                         "francais, dans le dossier de l'application, "
                         "sans toucher a celle-ci.",
                     auto_fixable=True)
    return Check("Tesseract (OCR)", Level.OK,
                 f"present, francais disponible — {origine}")


def _check_deps(intitule: str, needed: tuple[str, ...]) -> Check:
    """Presence d'un jeu de modules, dans l'interpreteur courant ou le venv."""
    here = [m for m in needed if _can_import(m)]
    if len(here) == len(needed):
        return Check(intitule, Level.OK, "installees (interpreteur courant)")

    vpy = _venv_python(paths.venv_dir())
    if vpy.is_file():
        there = [m for m in needed if _can_import(m, vpy)]
        if len(there) == len(needed):
            return Check(intitule, Level.OK, f"installees dans {paths.venv_dir()}")
        missing = [m for m in needed if m not in there]
        return Check(intitule, Level.FAIL,
                     f"manquantes dans le venv : {', '.join(missing)}",
                     fix="caviardoc setup", auto_fixable=True)

    missing = [m for m in needed if m not in here]
    return Check(intitule, Level.FAIL, f"manquantes : {', '.join(missing)}",
                 fix="caviardoc setup", auto_fixable=True)


def check_engine_deps() -> Check:
    """Dependances de la detection : sans elles, aucune entite n'est trouvee."""
    return _check_deps("Dependances du moteur", ("onnxruntime", "gliner"))


def check_documents_deps() -> Check:
    """Dependances de la couche document : sans elles, aucun fichier n'est lu.

    Ce controle manquait, et son absence rendait le diagnostic trompeur : une
    machine ou seul l'extra `engine` etait installe affichait un `doctor` tout
    vert et un `selftest` reussi, alors que `caviardoc caviarder` s'arretait sur
    un ImportError des le premier fichier. Le moteur sait detecter ; encore
    faut-il quelque chose pour ouvrir le document et y dessiner.
    """
    return _check_deps("Dependances documents",
                       ("pymupdf", "docx", "PIL", "pytesseract"))


def check_codes_barres() -> Check:
    """Lecture des codes-barres : sans zxing-cpp, tout document est refuse.

    Un code-barres ou un QR d'etiquette encode l'identite du patient : le
    laisser lisible annulerait le caviardage. Caviardoc le cherche dans chaque
    page et chaque image, et refuse le document s'il ne sait pas le faire.
    """
    if _can_import("zxingcpp"):
        return Check("Codes-barres (zxing-cpp)", Level.OK,
                     "installe (interpreteur courant)")
    vpy = _venv_python(paths.venv_dir())
    if vpy.is_file() and _can_import("zxingcpp", vpy):
        return Check("Codes-barres (zxing-cpp)", Level.OK,
                     f"installe dans {paths.venv_dir()}")
    return Check("Codes-barres (zxing-cpp)", Level.FAIL,
                 "absent : Caviardoc refuse de traiter un document sans lui",
                 fix="caviardoc setup", auto_fixable=True)


def check_model(spec: models.ModelSpec, deep: bool = False) -> Check:
    st = models.check(spec, deep=deep)
    if st.complete:
        detail = f"{spec.key} — present ({models.human(spec.total_size)})"
        if deep:
            detail += ", empreintes verifiees"
        return Check("Modele de detection", Level.OK, detail)
    n = len(st.missing)
    return Check("Modele de detection", Level.FAIL,
                 f"{spec.key} — {n} fichier(s) manquant(s) ou corrompu(s), "
                 f"{models.human(st.missing_bytes)} a telecharger",
                 fix=f"caviardoc setup --modele {spec.key}", auto_fixable=True)


def _source_installee() -> Path | None:
    """Dossier depuis lequel le paquet a ete installe dans le venv.

    Renseigne par PEP 610 : `direct_url.json` porte l'URL du dossier source
    d'une installation editable. Rend None si le paquet n'est pas installe de
    cette facon — auquel cas il n'y a rien a comparer.
    """
    import json
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    # Deux emplacements, dans cet ordre : le venv de la machine, puis celui de
    # l'interpreteur courant. Ne regarder que le premier laissait le controle
    # muet des qu'on lancait `caviardoc` autrement — depuis `uv run`, ou depuis
    # un venv place dans le depot. Or c'est precisement le code en train de
    # s'executer qu'il faut verifier.
    venvs = [paths.venv_dir(), Path(sys.prefix)]
    # L'ancien nom du paquet, tant qu'un venv d'avant le renommage tourne.
    motifs = tuple(f"{site}/{paquet}-*.dist-info/direct_url.json"
                   for site in ("lib/python*/site-packages", "Lib/site-packages")
                   for paquet in ("caviardoc", "doc_anonymizer"))
    for direct in (d for v in venvs for m in motifs for d in v.glob(m)):
        try:
            infos = json.loads(direct.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = infos.get("url", "")
        if not url.startswith("file:"):
            continue
        # Le chemin est encode : « PDF%20anonymizer ». Le decoder avant de
        # comparer, sans quoi tout chemin contenant une espace parait different.
        return Path(url2pathname(unquote(urlparse(url).path)))
    return None


def check_source_installee() -> Check | None:
    """Le venv execute-t-il le code de ce depot, ou celui d'un autre dossier ?

    L'installation editable retient le chemin du depot au moment du `uv sync`.
    Deplacer ou renommer le dossier ne le met pas a jour : le venv continue de
    pointer ailleurs, et la commande `caviardoc` execute alors un code different
    de celui qu'on lit et modifie. Rien ne le signalait.

    Constate en exercant `maj` : le venv de cette machine pointait encore vers
    « Projets/PDF anonymizer », nom porte par le projet sur une autre machine
    et inexistant ici, alors que le diagnostic affichait tout au vert.

    Rend None quand il n'y a rien a verifier : paquet non installe en editable,
    ou venv absent.
    """
    source = _source_installee()
    if source is None:
        return None

    depot = paths.project_root()
    try:
        identiques = source.resolve() == depot.resolve()
    except OSError:
        identiques = False
    if identiques:
        return Check("Code execute", Level.OK, str(depot))

    manquant = not source.exists()
    detail = (f"le venv pointe vers {source}"
              + (" — ce dossier n'existe pas" if manquant else ""))
    return Check(
        "Code execute", Level.FAIL, detail,
        fix=("La commande `caviardoc` executerait un autre code que celui de ce "
             "depot. Reinstallez-le ici :\n       caviardoc setup"),
        auto_fixable=True)


def check_mesure(cle: str | None = None) -> Check:
    """Cette machine a-t-elle ete mesuree, et avec quel resultat ?

    Le controle manquait, et son absence etait la faille la plus discrete du
    diagnostic : tout pouvait passer au vert sur une machine ou le modele
    n'avait jamais ete confronte au corpus. Le rappel annonce par la
    documentation n'engage que les machines ou il a ete constate.

    ``cle`` designe le modele examine, qui n'est pas toujours celui retenu :
    `doctor --modele` en inspecte un autre. Une mesure faite sur fp16
    n'apprend rien sur fp32, et l'afficher au vert en inspectant fp32 serait
    exactement le chiffre rassurant que ce controle existe pour eviter.
    """
    cle = cle or models.modele_actif()
    mesure = models.mesure_du_modele(cle)
    if mesure is None:
        precedente = models.derniere_mesure()
        detail = ("jamais faite sur cette machine" if precedente is None
                  else f"faite sur {precedente.get('modele')}, "
                       f"pas sur {cle}")
        return Check("Mesure du rappel", Level.WARN, detail,
                     fix="Le rappel depend de la machine. Etablissez le votre :"
                         "\n       python programme/tools/mesurer.py --enregistrer")

    rappels = [v for c, v in mesure.items() if c.startswith("rappel_")]
    pire = min(rappels) if rappels else 0.0
    quand = mesure.get("date", "?")
    if not mesure.get("tenu"):
        return Check("Mesure du rappel", Level.FAIL,
                     f"{pire:.1f} % le {quand} — sous le seuil",
                     fix="Ne confiez aucun document a cette machine avant "
                         "d'avoir compris pourquoi.")
    return Check("Mesure du rappel", Level.OK, f"{pire:.1f} % le {quand}")


def avertissement_mesure() -> dict | None:
    """Avertissement a afficher quand la mesure de cette machine n'est pas tenue.

    None si la mesure du modele retenu tient les seuils. Sinon un dict avec
    ``court`` (drapeau joint a chaque document), ``texte`` (message complet) et
    ``rappel`` (pire rappel mesure en %, ou None si jamais mesure). Ne bloque
    rien : c'est un avertissement, pas un refus.
    """
    mesure = models.mesure_du_modele()
    if mesure is not None and mesure.get("tenu"):
        return None
    court = "machine sous le seuil de détection mesuré"
    consigne = "relancer la mesure dans l'onglet Installation"
    if mesure is None:
        return {"court": "machine non mesurée : " + court, "rappel": None,
                "texte": ("Cette machine n'a jamais été mesurée : le rappel de "
                          "détection est inconnu. Pour la mesurer, " + consigne
                          + " (ou : python programme/tools/mesurer.py "
                          "--enregistrer).")}
    rappels = [v for c, v in mesure.items() if c.startswith("rappel_")]
    pire = min(rappels) if rappels else 0.0
    return {"court": court, "rappel": pire,
            "texte": (f"Cette machine détecte {pire:.1f} % des données de test "
                      f"(mesure du {mesure.get('date', '?')}), sous le seuil "
                      f"de 90 %. Des données personnelles peuvent subsister : "
                      f"vérifiez chaque document. Pour refaire la mesure, "
                      + consigne + ".")}


def run_all(deep: bool = False, spec: models.ModelSpec | None = None) -> list[Check]:
    spec = spec or models.REGISTRY[models.modele_actif()]
    controles = [
        check_version(),
        check_platform(),
        check_python(),
        check_ram(),
        check_data_dir(),
        check_disk(spec),
        check_uv(),
        check_tesseract(),
        check_engine_deps(),
        check_documents_deps(),
        check_codes_barres(),
        check_model(spec, deep=deep),
        check_mesure(spec.key),
    ]
    source = check_source_installee()
    if source is not None:
        controles.insert(-1, source)
    controles.extend(check_migration())
    return controles
