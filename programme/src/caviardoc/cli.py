"""Interface en ligne de commande.

Les sous-commandes `doctor`, `setup` et `info` ne dependent que de la
bibliotheque standard : elles fonctionnent sur une machine neuve, avant toute
installation. `selftest` charge le moteur et requiert les dependances.
"""

from __future__ import annotations

import argparse
import functools
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import desinstallation, doctor, materiel, models, paths, tesseract
from .doctor import Level
from .documents import codes_barres
from .documents.codes_barres import NOTE as NOTE_CODES_BARRES

# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    return sys.platform != "win32" or _sequences_windows()


@functools.lru_cache(maxsize=1)
def _sequences_windows() -> bool:
    """Active les sequences ANSI de la console Windows, si elle les connait.

    La console classique de Windows 10 — celle qu'ouvre un double-clic sur le
    `.bat` — ne les interprete que si le programme le demande : sans cela la
    fenetre affichait « ←[1mCaviardoc←[0m ». Echec : pas de couleur.
    """
    try:
        import ctypes
        noyau = ctypes.windll.kernel32
        sortie = noyau.GetStdHandle(-11)                # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not noyau.GetConsoleMode(sortie, ctypes.byref(mode)):
            return False
        vt = 0x0004                     # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(mode.value & vt or noyau.SetConsoleMode(sortie, mode.value | vt))
    except (AttributeError, OSError):
        return False


C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "cyan": "\033[36m",
}


def _motif_echec(exc: Exception) -> str:
    """Le motif affiche pour un document en echec : le message seul quand il
    est deja ecrit pour l'utilisateur, le type de l'erreur sinon."""
    from .documents.caviardage import DocumentTropLong
    if isinstance(exc, (tesseract.TesseractIndisponible, DocumentTropLong)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _propre(texte: str) -> str:
    """Remplace les caracteres de controle (ESC, CR, ...) par « ? ».

    Une chaine tiree d'un document ne doit pas piloter le terminal : effacer
    ou reecrire les avertissements deja affiches. Seuls la fin de ligne et la
    tabulation restent.
    """
    return "".join("?" if (ord(ch) < 32 and ch not in "\n\t") or
                   0x7F <= ord(ch) < 0xA0 else ch for ch in str(texte))


def c(text: str, *styles: str) -> str:
    if not _color_enabled():
        return text
    return "".join(C[s] for s in styles) + text + C["reset"]


MARK = {Level.OK: ("v", "green"), Level.WARN: ("!", "yellow"), Level.FAIL: ("x", "red")}


def print_checks(checks: list[doctor.Check]) -> None:
    width = max(len(ch.name) for ch in checks)
    for ch in checks:
        sym, col = MARK[ch.level]
        print(f"  {c(sym, col, 'bold')}  {ch.name.ljust(width)}  {ch.detail}")
        if ch.fix and ch.level is not Level.OK:
            print(f"     {' ' * width}  {c('-> ' + ch.fix, 'dim')}")


def confirm(question: str, default: bool = True, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return default
    suffix = "[O/n]" if default else "[o/N]"
    try:
        answer = input(f"{question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("o", "oui", "y", "yes")


# --------------------------------------------------------------------------- #
# Commandes
# --------------------------------------------------------------------------- #

def cmd_info(args: argparse.Namespace) -> int:
    cle = models.modele_actif()
    spec = models.REGISTRY[cle]
    print(c("Emplacements", "bold"))
    print(f"  Depot (git)             {paths.repo_root()}")
    print(f"  Donnees (locales)       {paths.data_dir()}")
    print(f"  Modeles                 {paths.models_dir()}")
    print(f"  Environnement virtuel   {paths.venv_dir()}")
    print()
    origine = ("defaut du registre" if cle == models.DEFAULT_MODEL
               else "retenu sur cette machine")
    print(c(f"Modele ({origine})", "bold"))
    print(f"  {spec.key}  ({models.human(spec.total_size)}, {spec.license})")
    print(f"  {spec.repo} @ {spec.revision}")
    print(c(f"  {spec.notes}", "dim"))
    print()
    print(c("Rien de ce qui figure sous « Donnees » n'est versionne ni transporte "
            "entre vos machines.", "dim"))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    spec = models.REGISTRY[getattr(args, "modele", None) or models.modele_actif()]
    print(c("Diagnostic de l'environnement", "bold"))
    if getattr(args, "modele", None):
        print(c(f"  (modele inspecte : {spec.key} — le reglage de la machine "
                "n'est pas modifie)", "dim"))
    if args.deep:
        # La taille vient du registre : elle a longtemps ete ecrite en dur a
        # « 350 Mo », qui etait celle d'int8, et n'a pas suivi le passage a fp16.
        print(c(f"  (verification des empreintes : lecture de "
                f"{models.human(spec.total_size)}, patientez)", "dim"))
    print()
    checks = doctor.run_all(deep=args.deep, spec=spec)
    print_checks(checks)
    print()

    fails = [ch for ch in checks if ch.level is Level.FAIL]
    warns = [ch for ch in checks if ch.level is Level.WARN]
    fixable = [ch for ch in fails if ch.auto_fixable]

    if not fails and not warns:
        print(c("Tout est en place.", "green", "bold"))
        return 0
    if not fails:
        print(c(f"{len(warns)} avertissement(s), rien de bloquant.", "yellow"))
        return 0

    print(c(f"{len(fails)} probleme(s) bloquant(s).", "red", "bold"))
    if fixable:
        print(f"Lancez {c('caviardoc setup', 'cyan')} pour en corriger "
              f"{len(fixable)} automatiquement.")
    return 1


def _uv_sync(assume_yes: bool) -> bool:
    """Installe les dependances du moteur dans le venv, hors du depot."""
    uv = paths.uv_exe()
    venv = paths.venv_dir()
    if not uv:
        print(c("  uv est absent — installation des dependances impossible.", "yellow"))
        print("  Installez uv, puis relancez. Sinon, manuellement :")
        print(f"    python3 -m venv {venv}")
        print(f"    {venv}/bin/pip install -e '.[engine]'")
        return False

    print(f"  Environnement : {venv}")
    if not confirm("  Installer les dependances du moteur et des documents "
                   "(~1 Go) ?", True, assume_yes):
        print(c("  Ignore.", "dim"))
        return False

    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv))
    # Les deux extras, et non le seul moteur : `caviarder` ouvre des fichiers
    # avant de detecter quoi que ce soit. N'installer que `engine` laissait une
    # machine ou tout le diagnostic passait au vert et ou le premier document
    # echouait sur un ImportError.
    # `extras_sync` plutot que la liste en dur : `uv sync` desinstalle ce qui
    # n'est pas demande, et `setup` faisait donc disparaitre l'extra « dev »
    # d'une machine qui l'avait. Le meme defaut avait ete corrige dans `maj` ;
    # il restait ici et dans l'installateur.
    from .maj import extras_sync
    cmd = [uv, "sync", "--locked", *extras_sync()]
    print(c(f"  $ UV_PROJECT_ENVIRONMENT={venv} {' '.join(cmd[:3])} ...", "dim"))
    res = subprocess.run(cmd, cwd=paths.project_root(), env=env)
    return res.returncode == 0


def cmd_setup(args: argparse.Namespace) -> int:
    # Sans `--modele`, on installe celui que cette machine a deja retenu :
    # relancer `setup` ne doit jamais la ramener en arriere sur le defaut.
    demande = args.modele
    spec = models.REGISTRY[demande or models.modele_actif()]
    print(c("Installation", "bold"))
    print()

    # Avant tout telechargement : un Mac Intel ou un macOS trop ancien n'a pas
    # les bibliotheques, et l'echec arriverait apres plus d'un gigaoctet.
    impossibles = materiel.obstacles()
    if impossibles:
        print(c("Cet ordinateur ne peut pas faire tourner Caviardoc :",
                "red", "bold"))
        for raison in impossibles:
            print(f"  - {raison}")
        print("\nRien n'a été installé.")
        return 1

    checks = doctor.run_all(spec=spec)
    print_checks(checks)
    print()

    blocking = [ch for ch in checks
                if ch.level is Level.FAIL and not ch.auto_fixable]
    if blocking:
        print(c("Ces points doivent etre regles a la main avant de continuer :", "red", "bold"))
        for ch in blocking:
            print(f"  - {ch.name} : {ch.fix}")
        return 1

    paths.ensure_dirs()

    failures: list[str] = []

    # --- dependances -------------------------------------------------------
    # Une seule synchronisation couvre les deux jeux : il suffit que l'un des
    # deux manque pour la declencher.
    deps = [ch for ch in checks
            if ch.name.startswith("Dependances") and ch.level is Level.FAIL]
    if deps:
        print(c("Dependances Python", "bold"))
        if not _uv_sync(args.yes):
            failures.append("dependances Python non installees")
        print()

    # --- modele ------------------------------------------------------------
    status = models.check(spec)
    if status.complete:
        print(c("Modele deja present.", "green"))
    else:
        print(c("Modele de detection", "bold"))
        print(f"  {spec.description}")
        print(f"  Source  : {spec.repo} @ {spec.revision} ({spec.license})")
        print(f"  Taille  : {models.human(status.missing_bytes)} a telecharger")
        print(f"  Vers    : {spec.local_dir()}")
        print(c(f"  {spec.notes}", "dim"))
        print()
        if not confirm("  Telecharger maintenant ?", True, args.yes):
            print(c("  Ignore. Relancez `caviardoc setup` plus tard.", "dim"))
            failures.append("modele non telecharge")
        else:
            print()
            try:
                models.download(spec, on_progress=models.cli_progress)
                print(c("\n  Modele installe et verifie.", "green"))
            except models.IntegrityError as exc:
                print(c(f"\nEchec de verification : {exc}", "red", "bold"))
                print("Le fichier telecharge ne correspond pas a l'empreinte "
                      "epinglee. Rien n'a ete installe.")
                failures.append("empreinte du modele incorrecte")
            except models.DownloadError as exc:
                print(c(f"\nEchec reseau : {exc}", "red", "bold"))
                failures.append("telechargement du modele echoue")

    print()
    if failures:
        print(c("Installation incomplete.", "red", "bold"))
        for f in failures:
            print(f"  - {f}")
        print(f"Corrigez la cause, puis relancez "
              f"{c(f'caviardoc setup --modele {spec.key}', 'cyan')}.")
        return 1

    # Le modele n'est retenu qu'une fois installe et verifie : une machine ne
    # doit jamais pointer vers un modele qu'elle n'a pas.
    if demande and models.modele_actif() != spec.key:
        models.definir_modele_actif(spec.key)
        print(c(f"Modele retenu pour cette machine : {spec.key}", "green"))
        print(c("  Toutes les commandes l'utilisent desormais. Mesurez-le avant "
                "de lui confier un document :", "dim"))
        print(c("  python programme/tools/mesurer.py --details", "cyan"))
        print()

    print(c("Termine.", "green", "bold"))
    print(f"Verifiez avec {c('caviardoc doctor', 'cyan')}, "
          f"puis {c('caviardoc selftest', 'cyan')}.")
    return 0


def _fuites_en_clair(fuites) -> str:
    """« Kunzli (personne, p. 3) » plutot que « Kunzli ».

    L'etiquette dit pourquoi la chaine etait masquee, donc si la fuite est une
    re-identification ou un faux positif du detecteur. La page dit ou aller
    voir. Sans elles, on annonce un danger sans donner de quoi l'instruire.
    """
    morceaux = []
    for f in sorted(fuites):
        details = [d for d in (f.etiquette,
                               "p. " + ", ".join(str(n) for n in f.pages)
                               if f.pages else "", f.raison) if d]
        morceaux.append(f"{f.texte}" + (f" ({'; '.join(details)})" if details else ""))
    return _propre(", ".join(morceaux))


def cmd_desinstaller(args: argparse.Namespace) -> int:
    """Reprend ce que l'installation a pose, apres l'avoir montre et chiffre.

    Rien n'est supprime avant que l'utilisateur ait vu la liste, les tailles et
    le total. Ce qui appartient a un autre outil — le dossier global de uv — ou
    a l'utilisateur — le depot — est montre mais jamais touche : on donne la
    commande, il decide.
    """
    elements = desinstallation.inventaire()
    dehors = desinstallation.laisses_a_l_utilisateur()

    retenus = [e for e in elements if e.par_defaut or args.tout]
    ecartes = [e for e in elements if e not in retenus]

    print(c("Desinstallation", "bold"))
    print()

    if retenus:
        print("Sera supprime :")
        for e in retenus:
            print(f"  - {e.titre} — {c(models.human(e.octets), 'bold')}")
            print(f"      {e.chemin}")
            print(c(f"      {e.pourquoi}", "dim"))
        total = sum(e.octets for e in retenus)
        print()
        print(f"  Total : {c(models.human(total), 'bold')}")
        print()
    else:
        total = 0
        print(c("Rien a supprimer : aucune trace d'installation.", "green"))
        print()

    deja = desinstallation.deja_presents()
    if deja:
        print("Deja presents avant Caviardoc, donc laisses en place :")
        for nom, chemin in deja:
            print(f"  - {nom}  {c(str(chemin), 'dim')}")
        print()

    if ecartes:
        print("Trouve, mais laisse en place :")
        for e in ecartes:
            print(f"  - {e.titre} — {models.human(e.octets)}")
            print(f"      {e.chemin}")
            print(c(f"      {e.pourquoi}", "dim"))
        print(c(f"  Ajoutez {c('--tout', 'cyan')} pour les reprendre aussi.", "dim"))
        print()

    # Ni le dossier global de uv ni le depot ne sont supprimes ici : le premier
    # est partage avec le reste de la machine, le second appartient a
    # l'utilisateur. On montre, on chiffre, on donne la commande.
    print("Hors de notre perimetre — a vous de voir :")
    for d in dehors:
        print(f"  - {d.titre} — {models.human(d.octets)}")
        print(f"      {d.chemin}")
        print(c(f"      {d.pourquoi}", "dim"))
        if d.commande:
            print(f"      {c(d.commande, 'cyan')}")
    print()

    bloquants = [e for e in retenus if desinstallation.s_execute_depuis(e)]
    if bloquants:
        print(c("Impossible depuis cet interpreteur.", "red", "bold"))
        for e in bloquants:
            print(f"  Python s'execute depuis {c(sys.prefix, 'cyan')}, "
                  f"a l'interieur de « {e.titre} ».")
        print("  Relancez par le lanceur de desinstallation "
              "(programme/desinstaller.sh), qui utilise un interpreteur exterieur.")
        return 1

    if not retenus:
        return 0

    if not confirm(c("Supprimer definitivement ?", "bold"), False, args.oui):
        print(c("Rien n'a ete supprime.", "dim"))
        return 1

    print()
    echecs = desinstallation.supprimer(
        retenus, essais=20 if args.patienter else 1)
    if echecs:
        print(c("Desinstallation incomplete.", "red", "bold"))
        for e in echecs:
            print(f"  - {_propre(e)}")
        return 1

    print(c(f"Supprime : {models.human(total)}.", "green", "bold"))
    print(f"Il ne reste que le depot : {desinstallation.depot()}")
    return 0


#: Extensions traitees, et la chaine qui s'en charge. Un format absent est
#: signale et ignore plutot que tente : mieux vaut un fichier non traite qu'un
#: fichier qui parait traite.
FORMATS_ENTREE = (".pdf", ".docx")


def _afficher_docx(rapport) -> None:
    """Detail d'un .docx : une ligne par partie de l'archive, puis les reserves."""
    for partie in rapport.parties:
        print(f"  {_propre(partie.nom)}: {partie.caracteres} caracteres, "
              f"{len(partie.entites)} entites, {partie.remplacements} "
              f"remplacements ({partie.duree:.1f}s)")

    if rapport.proprietes:
        print(f"  {rapport.proprietes} propriete(s) de document neutralisee(s)")
    if rapport.metadonnees:
        print(f"  metadonnees retirees : {_propre(', '.join(rapport.metadonnees))}")
    for nom in rapport.images_metadonnees:
        print(c(f"  {_propre(nom)} : metadonnees conservees, format non pris en charge "
                "— sans --sans-logos, l'image serait retiree", "yellow"))

    # Les images sont desormais lues et caviardees comme le reste. On detaille
    # celles ou il s'est passe quelque chose, et l'on compte les autres : savoir
    # qu'une image a ete regardee et trouvee vide fait partie du compte rendu.
    muettes = 0
    for image in rapport.images_lues:
        if not image.lue:
            print(c(f"  {_propre(image.nom)} : illisible ({_propre(image.illisible)}) — son "
                    "contenu n'est ni masque ni verifie ; sans "
                    "--sans-logos, elle serait retiree",
                    "yellow"))
        elif image.boites:
            print(f"  {_propre(image.nom)} : {image.mots_reconnus} mots lus, "
                  f"{len(image.entites)} entites, {image.boites} boites "
                  f"({image.duree:.1f}s)")
            if getattr(image, "codes_barres", 0):
                print(c(f"       {NOTE_CODES_BARRES} ({image.codes_barres})",
                        "dim"))
        else:
            muettes += 1
    if muettes:
        print(f"  {muettes} image(s) lue(s), rien a y masquer")

    if rapport.objets:
        # Un classeur, un PDF colle ou un altChunk ne se lit pas : ils sont
        # retires du document produit, et il faut le dire.
        print(c(f"  {rapport.objets} objet(s) incorpore(s) retire(s) : leur "
                "contenu n'etait ni lu ni verifiable — document a verifier",
                "yellow"))

    for option in rapport.zones_ignorees:
        print(c(_propre(f"  --{option} est sans effet sur un .docx"), "dim"))


def duree_lisible(secondes: float) -> str:
    """« 45 s », « 3 min 05 s », « 1 h 02 min » — comme la page."""
    s = max(0, round(secondes))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s // 60 % 60:02d} min"


def cmd_caviarder(args: argparse.Namespace) -> int:
    """Caviarde des documents, et anonymise leur nom de fichier.

    Deux chaines coexistent, choisies sur l'extension : un PDF passe par
    l'image et ressort sans couche texte, un .docx est reecrit en place,
    ses pseudonymes remplacant les donnees dans le fichier lui-meme.
    """
    from .documents.caviardage import caviarder_pdf
    from .documents.docx import caviarder_docx
    from .documents import ocr
    from .documents.qualite import diagnostiquer_pdf
    from .engine import EngineUnavailable, load_model
    from .documents.zones import OptionsZones
    from .pipeline import PolitiqueDates
    from .pseudonymes import TablePseudonymes
    from .exceptions import Repertoire
    from .revue import appliquer, avec_zones, preparer
    from .revue_web import servir
    from .roles import Niveau
    from .noms import anonymiser_nom, nom_libre, renommer

    zones = OptionsZones(
        logos=args.logos,
        entetes=args.entetes,
        bande_haute=args.bande_haute,
        premiere_page_seulement=not args.toutes_pages)

    # Tesseract manquant : le message est dit une fois, avant tout fichier, et
    # rien n'est cree.
    if any(Path(f).suffix.lower() in FORMATS_ENTREE for f in args.fichiers):
        try:
            tesseract.exiger()
            codes_barres.exiger()
        except tesseract.TesseractIndisponible as exc:
            print(c(_propre(str(exc)), "red", "bold"))
            return 1

    dest = Path(args.sortie)
    dest.mkdir(parents=True, exist_ok=True)

    cle = args.modele or models.modele_actif()
    try:
        model = load_model(cle)
    except EngineUnavailable as exc:
        print(c(_propre(f"Moteur indisponible : {exc}"), "red"))
        return 1
    # Le modele conditionne le rappel, donc la protection reelle du document.
    # Il figure dans la trace au meme titre que le niveau de caviardage.
    print(c(f"Modele : {cle}", "dim"))
    # Avertissement, jamais un refus : dit une fois, avant le lot.
    alerte_mesure = doctor.avertissement_mesure()
    if alerte_mesure:
        print(c(_propre("ATTENTION : " + alerte_mesure["texte"]), "red", "bold"))

    formats = frozenset({"pdf", "texte"} if args.format == "les-deux"
                        else {args.format})
    table = TablePseudonymes()
    # Les corrections retenues dans la console valent aussi ici : sans elles,
    # un nom appris resterait en clair dans la sortie du CLI. Relues a chaque
    # document, comme la console, pour que le lot profite de ce qui precede.
    if Repertoire.charger().degat:
        print(c("Liste de corrections retenues illisible : l'original est mis "
                "de cote, les masques appris sont perdus.", "yellow", "bold"))
    # Le chronometre part apres le chargement du modele : c'est le temps
    # d'anonymisation qu'on veut lire, pas celui du demarrage.
    debut = time.monotonic()
    traites = 0

    def crochet_revue(nom_document: str):
        """Rend le crochet que les deux chaines appellent avant d'ecrire.

        Il recoit les parties detectees, ouvre la page de relecture, bloque
        jusqu'a la validation, et rend les spans retenus par partie.
        """
        def relire(parties, zones=None):
            session = preparer(nom_document, parties, zones)
            session.enregistrer()
            textes = {intitule: texte for intitule, texte, _ in parties}
            session = servir(session, textes)
            retenus = avec_zones(session, {
                intitule: appliquer(session, intitule) for intitule in textes})
            # Appliquee : la session (detections, contextes) n'a plus d'objet.
            session.supprimer()
            return retenus
        return relire

    total_fuites = 0
    a_renommer: list[Path] = []
    echecs: list[tuple[str, str]] = []

    # Les copies temporaires de l'OCR vont dans un dossier a nous, efface a la
    # fin meme sur une interruption : un processus tue les laisserait ailleurs.
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    travail = tempfile.mkdtemp(prefix="cli-", dir=paths.cache_dir())
    temporaires_avant = ocr.confiner_temporaires(travail)

    try:
        for chemin in (Path(f) for f in args.fichiers):
            if not chemin.is_file():
                print(c(_propre(f"introuvable : {chemin}"), "yellow"))
                continue

            suffixe = chemin.suffix.lower()
            if suffixe not in FORMATS_ENTREE:
                motif = ("le format .doc n'est pas pris en charge : enregistrez le "
                         "document en .docx" if suffixe == ".doc"
                         else f"format {suffixe or 'inconnu'} non pris en charge")
                print(c(_propre(f"\n{chemin.name}"), "bold"))
                print(c(_propre(f"  ignore : {motif}"), "yellow"))
                continue

            est_docx = suffixe == ".docx"
            relecture = crochet_revue(chemin.name) if args.revue else None
            print(c(_propre(f"\n{chemin.name}"), "bold"))
            if not est_docx:
                try:
                    print(c(_propre(f"  {diagnostiquer_pdf(chemin).explication}"), "dim"))
                except Exception as exc:
                    # Un PDF vide ou chiffre ne doit pas interrompre le lot.
                    traites += 1
                    echecs.append((chemin.name, _motif_echec(exc)))
                    print(c(_propre(f"  ECHEC : {_motif_echec(exc)}"), "red", "bold"))
                    continue

            # Jamais par-dessus un fichier existant : un document du meme nom deja
            # traite dans ce lot, ou l'original lui-meme si `-o` designe son dossier.
            # La sortie texte compte aussi : « x.pdf » et « x.docx » ecrivent tous
            # deux « x.txt ».
            provisoire = nom_libre(dest / chemin.name, compagnons=(".txt",))
            traites += 1
            # Inscrit avant le traitement : une interruption en plein document
            # laisse un fichier partiel, qui ne doit pas garder le nom d'origine.
            a_renommer.append(provisoire)
            try:
                if est_docx:
                    rapport = caviarder_docx(model, chemin, provisoire,
                                             verifier=not args.sans_verification,
                                             zones=zones,
                                             niveau=Niveau(args.niveau),
                                             formats=formats, table=table,
                                             revue=relecture,
                                             dates=PolitiqueDates(args.dates),
                                             exceptions=Repertoire.charger())
                else:
                    rapport = caviarder_pdf(model, chemin, provisoire, dpi=args.dpi,
                                            verifier=not args.sans_verification,
                                            zones=zones,
                                            revue=relecture,
                                            niveau=Niveau(args.niveau),
                                            formats=formats, table=table,
                                            dates=PolitiqueDates(args.dates),
                                            exceptions=Repertoire.charger())
            except Exception as exc:
                # Un document en echec ne doit pas interrompre le lot.
                echecs.append((chemin.name, _motif_echec(exc)))
                print(c(_propre(f"  ECHEC : {_motif_echec(exc)}"), "red", "bold"))
                continue

            # Pseudonymes coherents, pour le contenu comme pour le nom de fichier.
            unites = rapport.parties if est_docx else rapport.pages
            table.enregistrer(sp for unite in unites for sp in unite.entites)

            # Le renommage attend la fin du lot : un document dont la couche texte
            # est corrompue ne livre aucun nom exploitable, alors que le document
            # suivant, lui, nomme le meme patient. Renommer au fil de l'eau
            # laisserait le premier fichier porter l'identite en clair.

            if rapport.noms_patient:
                print(f"  patient reconnu : {c(_propre(', '.join(sorted(rapport.noms_patient))), 'cyan')}")
            elif rapport.repli_complet:
                print(c("  patient NON identifie — niveau complet applique par "
                        "securite sur ce document", "yellow", "bold"))

            if est_docx:
                _afficher_docx(rapport)
            else:
                for page in rapport.pages:
                    zone = f", dont {page.zones} zone(s)" if page.zones else ""
                    print(f"  p.{page.numero}: {page.mots_reconnus} mots "
                          f"({page.source}), {len(page.entites)} entites, "
                          f"{page.boites} boites{zone} ({page.duree:.1f}s)")
                    if page.reserve:
                        print(c(_propre(f"       ! {page.reserve}"), "yellow"))
                    if getattr(page, "codes_barres", 0):
                        print(c(f"       {NOTE_CODES_BARRES} "
                                f"({page.codes_barres})", "dim"))

            corrigees = (list(getattr(rapport, "corrigees", []))
                         + list(getattr(rapport, "corrigees_texte", [])))
            if corrigees:
                print(c(f"  corrige automatiquement : {_fuites_en_clair(corrigees)}",
                        "yellow"))
            if rapport.fuites_texte:
                total_fuites += len(rapport.fuites_texte)
                print(c(f"  FUITES (texte) : {_fuites_en_clair(rapport.fuites_texte)}",
                        "red", "bold"))
            if rapport.fuites:
                total_fuites += len(rapport.fuites)
                print(c(f"  FUITES : {_fuites_en_clair(rapport.fuites)}",
                        "red", "bold"))
            elif rapport.erreur_verification:
                print(c(_propre(f"  NON VERIFIE : {rapport.erreur_verification}"), "yellow", "bold"))
            elif rapport.verifie and not rapport.fuites_texte:
                print(c("  verifie : aucune donnee residuelle", "green"))
    finally:
        ocr.restaurer_temporaires(temporaires_avant)
        shutil.rmtree(travail, ignore_errors=True)
        # Meme sur une interruption ou une erreur imprevue : les sorties deja
        # ecrites ne gardent pas le nom du document d'origine.
        print()
        correspondances = table.correspondances
        for provisoire in a_renommer:
            for chemin in (provisoire, provisoire.with_suffix(".txt")):
                if not chemin.exists():
                    continue
                try:
                    nom_final = anonymiser_nom(chemin.name, correspondances,
                                             personnes=table.formes_personnes)
                    if nom_final != chemin.name:
                        final = renommer(chemin, dest / nom_final)
                        print(f"  {_propre(chemin.name)}\n    -> "
                              f"{c(_propre(final.name), 'cyan')}")
                except OSError as exc:
                    print(c(_propre(f"  renommage impossible : {chemin.name} "
                                f"({exc.strerror or exc})"), "red", "bold"))

    print()
    duree = duree_lisible(time.monotonic() - debut)
    print(c(f"{traites} document(s) traite(s) en {duree}"
            + (", relecture comprise" if args.revue else "") + ".", "bold"))
    if echecs:
        print(c(f"{len(echecs)} document(s) en echec :", "red", "bold"))
        for nom, motif in echecs:
            print(_propre(f"  - {nom} : {motif}"))
    if total_fuites:
        print(c(f"{total_fuites} fuite(s) detectee(s).", "red", "bold"))
    if echecs or total_fuites:
        return 1
    print(c("Termine.", "green", "bold"))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Charge le moteur et anonymise une phrase de reference."""
    from .engine import selftest
    return selftest(verbose=True)


def cmd_maj(args: argparse.Namespace) -> int:
    """Verifie, et n'applique que si on le lui demande."""
    from .maj import MajImpossible, appliquer, verifier

    try:
        etat = verifier()
    except MajImpossible as exc:
        print(c(_propre(str(exc)), "yellow"))
        return 1

    print(c("Mise a jour", "bold"))
    print(f"  Installee   {etat.locale.reference}  {etat.locale.date}  "
          f"{etat.locale.resume}")
    if etat.distante:
        print(f"  Disponible  {etat.distante.reference}  {etat.distante.date}  "
              f"{etat.distante.resume}")
    print()

    if etat.empeche:
        print(c(_propre(etat.empeche), "yellow"))
        return 1
    if not etat.disponible:
        print(c("Deja a jour.", "green"))
        return 0

    print(c(f"{etat.retard} nouveau(x) commit(s) :", "bold"))
    for ligne in etat.changements:
        print(f"  {_propre(ligne)}")
    print()

    if not args.appliquer:
        print("Rien n'a ete change. Pour appliquer :")
        print(f"  {c('caviardoc maj --appliquer', 'cyan')}")
        return 0

    print(c("La mise a jour remplace le code, les dependances et "
            "possiblement le modele.", "yellow"))
    print(c("Le rappel mesure sur cette machine ne vaudra plus : il sera "
            "remesure ensuite.", "yellow"))
    if not confirm("  Appliquer maintenant ?", True, args.yes):
        print(c("  Annule.", "dim"))
        return 0
    print()

    try:
        compte = appliquer(journal=lambda l: print(f"  {_propre(l)}"),
                           remesurer=not args.sans_mesure)
    except Exception as exc:
        print(c(_propre(f"\nEchec : {exc}"), "red", "bold"))
        return 1

    print()
    print(c(f"Mise a jour de {compte['de']} vers {compte['vers']}.",
            "green", "bold"))
    avant, apres = compte.get("rappel_avant"), compte.get("rappel_apres")
    if apres is not None:
        ancien = f" (etait {avant:.1f} %)" if avant is not None else ""
        print(f"  Rappel mesure : {apres:.1f} %{ancien}")
    if compte.get("regression"):
        print(c("  ATTENTION : le rappel a baisse. Ne confiez pas de document "
                "a cette machine avant d'avoir compris pourquoi.",
                "red", "bold"))
        return 1
    return 0


def cmd_installer(args: argparse.Namespace) -> int:
    """Ouvre l'installation guidee dans le navigateur.

    Ne verifie rien avant : c'est precisement ce que la page est faite pour
    montrer. Elle doit s'ouvrir sur une machine ou il manque tout.
    """
    from .installateur import servir

    print(c("Caviardoc — installation guidee", "bold"))
    print()
    servir(port=args.port, ouvrir=not args.sans_navigateur)
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    """La page unique : l'application et son entretien, dans deux onglets.

    Ne refuse rien au demarrage, contrairement a `interface` : c'est justement
    sur une machine incomplete qu'elle doit s'ouvrir, sur l'onglet
    d'installation. Refuser reviendrait a renvoyer l'utilisateur vers un second
    fichier a double-cliquer — ce qu'elle existe pour supprimer.
    """
    from .interface import servir

    print(c("Caviardoc", "bold"))
    print()
    return servir(port=args.port, ouvrir=not args.sans_navigateur,
                  console=True)


def cmd_interface(args: argparse.Namespace) -> int:
    """Ouvre l'interface locale dans le navigateur du poste."""
    from .interface import servir

    # Une mesure sous le seuil n'empeche pas de demarrer : la page l'affiche
    # en bandeau rouge et marque chaque document.
    bloquants = [ch for ch in doctor.run_all()
                 if ch.level is Level.FAIL and ch.name != "Mesure du rappel"]
    if bloquants:
        print(c("L'interface ne peut pas demarrer :", "red", "bold"))
        for ch in bloquants:
            print(f"  - {ch.name} : {ch.detail}")
        print(f"Lancez {c('caviardoc doctor', 'cyan')} pour le detail.")
        return 1

    print(c("Caviardoc — interface locale", "bold"))
    print()
    servir(port=args.port, ouvrir=not args.sans_navigateur)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="caviardoc",
        description="Anonymiseur de documents, local et multiplateforme.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="verifier l'environnement")
    d.add_argument("--deep", action="store_true",
                   help="recalculer les empreintes du modele (lit "
                        f"{models.human(models.REGISTRY[models.modele_actif()].total_size)})")
    # Inspecter un autre modele que celui retenu, sans changer le reglage de la
    # machine. Utile avant de basculer — savoir si un modele est deja present et
    # intact evite un telechargement de plusieurs centaines de megaoctets — et
    # pour diagnostiquer un modele qu'on ne veut pas retenir.
    d.add_argument("--modele", default=None, choices=sorted(models.REGISTRY),
                   help="inspecter ce modele plutot que celui retenu "
                        f"(retenu : {models.modele_actif()})")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="installer ce qui manque")
    s.add_argument("--yes", "-y", action="store_true", help="ne rien demander")
    # `--model` etait l'orthographe d'origine, seule option anglaise d'une
    # interface francaise. Conservee en alias : elle figure dans la
    # documentation deja diffusee.
    s.add_argument("--modele", "--model", dest="modele", default=None,
                   choices=sorted(models.REGISTRY),
                   help="modele a installer, et a retenir pour cette machine "
                        f"(defaut : {models.modele_actif()})")
    s.set_defaults(func=cmd_setup)

    i = sub.add_parser("info", help="afficher les emplacements")
    i.set_defaults(func=cmd_info)

    t = sub.add_parser("selftest", help="verifier le moteur de bout en bout")
    t.set_defaults(func=cmd_selftest, besoin_moteur=True)

    m = sub.add_parser("maj", help="verifier et appliquer une mise a jour")
    m.add_argument("--appliquer", action="store_true",
                   help="appliquer la mise a jour, apres confirmation")
    m.add_argument("--yes", "-y", action="store_true", help="ne rien demander")
    m.add_argument("--sans-mesure", action="store_true",
                   help="ne pas remesurer apres coup (deconseille : le rappel "
                        "annonce ne vaut que pour une version donnee)")
    m.set_defaults(func=cmd_maj)

    n = sub.add_parser("installer",
                       help="installation guidee dans le navigateur")
    n.add_argument("--port", type=int, default=0,
                   help="port d'ecoute (defaut : choisi par le systeme)")
    n.add_argument("--sans-navigateur", action="store_true",
                   help="ne pas ouvrir le navigateur, afficher l'adresse")
    n.set_defaults(func=cmd_installer)

    v = sub.add_parser("console",
                       help="l'application et son entretien, en une page")
    v.add_argument("--port", type=int, default=0,
                   help="port d'ecoute (0 = choisi par le systeme)")
    v.add_argument("--sans-navigateur", action="store_true",
                   help="ne pas ouvrir le navigateur")
    v.set_defaults(func=cmd_console, moteur_si_possible=True)

    g = sub.add_parser("interface",
                       help="ouvrir l'interface locale dans le navigateur")
    g.add_argument("--port", type=int, default=0,
                   help="port d'ecoute (defaut : choisi par le systeme)")
    g.add_argument("--sans-navigateur", action="store_true",
                   help="ne pas ouvrir le navigateur, afficher l'adresse")
    g.set_defaults(func=cmd_interface, besoin_moteur=True)

    x = sub.add_parser("desinstaller",
                       help="reprendre ce que l'installation a pose")
    x.add_argument("--tout", action="store_true",
                   help="inclure ce que nous n'avons pas installe nous-memes "
                        "(uv et ses Pythons)")
    x.add_argument("--oui", action="store_true",
                   help="ne pas demander confirmation")
    x.add_argument("--patienter", action="store_true",
                   help="reessayer si un fichier est encore ouvert (utilise "
                        "par la console, qui se supprime elle-meme)")
    x.set_defaults(func=cmd_desinstaller)

    k = sub.add_parser("caviarder",
                       help="caviarder des PDF et des .docx")
    k.add_argument("fichiers", nargs="+")
    k.add_argument("--sortie", "-o", required=True, help="repertoire de sortie")
    k.add_argument("--modele", default=None, choices=sorted(models.REGISTRY),
                   help="modele de detection, pour cette execution seulement. "
                        "Par defaut celui retenu sur cette machine "
                        f"({models.modele_actif()}), fixe par `caviardoc setup "
                        "--modele`.")
    k.add_argument("--dpi", type=int, default=300,
                   help="resolution de rendu de la chaine image (PDF)")
    k.add_argument("--sans-verification", action="store_true",
                   help="ne pas relire le document produit (deconseille)")
    # Logos et en-tetes sont masques par defaut, comme dans l'interface : un
    # tampon ou un bloc expediteur designe l'etablissement, et les laisser
    # passer doit etre un choix, pas l'oubli d'une option. `--logos` et
    # `--entetes` restent acceptes pour les scripts qui les passaient.
    k.add_argument("--logos", dest="logos", action="store_true", default=True,
                   help=argparse.SUPPRESS)
    k.add_argument("--sans-logos", dest="logos", action="store_false",
                   help="laisser les images integrees (logos, tampons, "
                        "signatures). Par defaut elles sont masquees ; sur un "
                        ".docx, retirees")
    k.add_argument("--entetes", dest="entetes", action="store_true",
                   default=True, help=argparse.SUPPRESS)
    k.add_argument("--sans-entetes", dest="entetes", action="store_false",
                   help="laisser les blocs expediteur et destinataire du haut "
                        "de page. Par defaut ils sont masques ; sur un .docx, "
                        "les en-tetes et pieds de page sont vides")
    k.add_argument("--bande-haute", type=float, default=0.0, metavar="FRACTION",
                   help="noircir cette fraction haute de la page, sans condition "
                        "(ex. 0.2) — recours pour les scans ou le logo fait "
                        "partie de l'image")
    k.add_argument("--toutes-pages", action="store_true",
                   help="masquer les blocs du haut de page et appliquer "
                        "--bande-haute sur toutes les pages, pas seulement "
                        "la premiere")
    k.add_argument("--revue", action="store_true",
                   help="relire les detections dans le navigateur avant "
                        "d'ecrire. La page est servie sur la boucle locale et "
                        "affiche le document en clair : elle ne quitte jamais "
                        "la machine. Sans action de votre part, tout reste "
                        "masque.")
    k.add_argument("--format", default="pdf",
                   choices=["pdf", "texte", "les-deux"],
                   help="pdf : document caviarde — au pixel pour un PDF, par "
                        "pseudonymes pour un .docx (defaut) ; texte : "
                        "texte anonymise a "
                        "pseudonymes stables, format recommande pour alimenter "
                        "un modele de langage ; les-deux.")
    k.add_argument("--niveau", default="complet", choices=["complet", "patient"],
                   help="complet : masquer toutes les donnees detectees "
                        "(defaut) ; patient : ne masquer que ce qui identifie "
                        "le patient, en conservant les soignants, les "
                        "etablissements et leurs coordonnees. En cas de doute "
                        "sur une personne, elle reste masquee.")
    k.add_argument("--dates", default="masquer",
                   choices=["masquer", "relatives", "garder", "garder-tout"],
                   help="masquer : toutes les dates (defaut) ; relatives : "
                        "remplacer chacune par son ecart en jours avec la plus "
                        "ancienne du document (J0, J+4), la date de naissance "
                        "restant masquee ; garder : les conserver sauf la date "
                        "de naissance ; garder-tout : les conserver toutes. La "
                        "date de naissance, combinee au code postal et au "
                        "sexe, identifie une personne dans la plupart des "
                        "cas ; une date d'evenement se recherche. "
                        "« relatives » garde les intervalles sans l'une ni "
                        "l'autre.")
    k.set_defaults(func=cmd_caviarder, besoin_moteur=True)

    return p


def _rouvrir_session(args: argparse.Namespace) -> bool:
    """Une page est deja ouverte : on la remet sous les yeux.

    Un second double-clic sur le lanceur ouvrait une seconde page, servie par
    un second processus, a cote de la premiere et de son lot en cours.
    """
    from .web_commun import session_ouverte

    adresse = session_ouverte()
    if adresse is None:
        return False
    print("Caviardoc est deja ouvert : la page est rouverte dans le "
          "navigateur.")
    if not args.sans_navigateur:
        import webbrowser
        webbrowser.open(adresse)
    return True


def _relais_optionnel(argv: list[str]) -> int | None:
    """Comme le relais, mais sans exiger que le moteur existe.

    La console doit pouvoir s'ouvrir sur une machine ou rien n'est installe :
    c'est le cas qu'elle sert. Quand un interpreteur complet existe, on le
    prefere ; sinon on continue avec celui qu'on a, et l'onglet d'installation
    fait le reste.
    """
    interprete = doctor.interprete_du_moteur()
    if interprete is None:
        return None
    if interprete.resolve() == Path(sys.executable).resolve():
        return None
    return subprocess.run([str(interprete), "-m", "caviardoc", *argv]).returncode


def _assurer_interprete_du_moteur(argv: list[str]) -> int | None:
    """Passe la main a l'interpreteur qui a le moteur, si ce n'est pas le notre.

    Les lanceurs demarrent `caviardoc` avec le Python d'amorcage : celui qui n'a
    que la bibliotheque standard. C'est voulu — c'est ce qui permet d'ouvrir
    l'installateur sur une machine ou il manque tout. Mais les commandes qui
    chargent le modele ont besoin du venv, et sans ce relais elles echouaient
    sur « dependances du moteur absentes » alors que les dependances etaient
    bel et bien installees, ailleurs.

    Rend None quand il n'y a rien a faire, sinon le code de sortie.
    """
    interprete = doctor.interprete_du_moteur()
    if interprete is None:
        print(c("Les dependances du moteur sont introuvables.", "red", "bold"))
        print(f"  Lancez {c('caviardoc setup', 'cyan')}, ou l'installateur "
              "guide de votre plateforme.")
        return 1

    if interprete.resolve() == Path(sys.executable).resolve():
        return None

    # subprocess plutot que os.execv : sous Windows, remplacer le processus
    # rend la main au terminal avant que l'enfant ait fini.
    return subprocess.run([str(interprete), "-m", "caviardoc", *argv]).returncode


def _sorties_tolerantes() -> None:
    """Un caractere que la console ne sait pas ecrire ne doit pas tout arreter.

    Constate sur Windows : `caviardoc setup` plantait avec une
    `UnicodeEncodeError` en affichant « ▶ » (le nom du lanceur), parce que la
    console — ou le tube — est en cp1252 ou cp850, qui ne l'ont pas. Une
    erreur d'affichage rendait le code de sortie 1 et arretait l'installation.
    Le caractere est remplace par « ? » ; les accents, eux, passent.
    """
    for flux in (sys.stdout, sys.stderr):
        try:
            flux.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass        # flux redirige vers un objet sans reconfigure


def main(argv: list[str] | None = None) -> int:
    _sorties_tolerantes()
    repris = paths.reprendre_ancien()
    if repris:
        print("Repris de l'ancien dossier de donnees : " + ", ".join(repris),
              file=sys.stderr)
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    # Avant tout import du moteur : la variable doit preceder le chargement
    # d'ONNX Runtime.
    from . import web_commun
    web_commun.desactiver_telemetrie()
    try:
        if getattr(args, "besoin_moteur", False):
            code = _assurer_interprete_du_moteur(argv)
            if code is not None:
                return code
        elif getattr(args, "moteur_si_possible", False):
            if args.func is cmd_console and _rouvrir_session(args):
                return 0
            code = _relais_optionnel(argv)
            if code is not None:
                return code
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
