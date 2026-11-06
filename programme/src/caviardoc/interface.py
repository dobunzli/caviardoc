"""Interface d'accueil : deposer des documents, les caviarder, les recuperer.

Une page servie sur la boucle locale, ouverte dans le navigateur du poste.
Aucune bibliotheque graphique : le projet rendait deja du HTML pour la revue,
et un serveur local coute zero dependance, fonctionne identiquement sur macOS,
Windows et Linux, et n'ajoute rien a empaqueter.

Le depot des fichiers passe par un televersement vers 127.0.0.1 — un navigateur
ne peut pas lire un chemin local, et il n'y a pas lieu de lui en donner un.
Rien ne quitte la machine : le serveur n'ecoute que la boucle locale, exige un
jeton de session, et refuse les requetes dont l'en-tete `Host` n'est pas le
sien.

Les originaux televerses portent des donnees de patients. Ils vivent dans un
repertoire temporaire du repertoire de donnees local — jamais dans le depot —
et sont effaces a l'arret du serveur, quelle qu'en soit la cause.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import doctor, paths
from .interruption import Interrompu
from .tesseract import TesseractIndisponible
from .web_commun import (HOTE, SIGNE_VIVANT, PoigneeBase, ServeurLocal,
                         hotes_admis, noter_session, nouveau_jeton,
                         oublier_session)

if TYPE_CHECKING:
    # Pour l'annotation seulement : la couche document se charge a la demande.
    from .documents.zones import OptionsZones

#: Extensions acceptees au depot. Un format absent est refuse a l'entree
#: plutot que tente : mieux vaut un fichier non traite qu'un fichier qui
#: parait traite.
FORMATS_ENTREE = (".pdf", ".docx")

#: En-tete portant le nom du fichier televerse, encode pour l'URL.
ENTETE_NOM = "X-Caviardoc-Nom"

#: En-tete portant le dossier d'origine, quand on depose un dossier entier :
#: « Patients/Dupont » pour un fichier trouve dans ce sous-dossier. Il ne sert
#: qu'a reconstruire l'arborescence dans l'archive rendue, jamais a ecrire sur
#: le disque.
ENTETE_DOSSIER = "X-Caviardoc-Dossier"

#: Plafond par fichier. Un scan de plusieurs centaines de pages a 300 dpi reste
#: en dessous ; au-dela, c'est une erreur de manipulation, et la refuser vaut
#: mieux que de remplir la memoire du poste.
TAILLE_MAX = 300 * 1024 * 1024

_CHUNK = 1 << 20


@dataclass
class Depot:
    """Un fichier televerse, en attente de traitement."""

    identifiant: str
    nom: str
    chemin: Path
    taille: int
    #: Sous-dossier d'origine, assaini ; vide pour un fichier depose seul.
    dossier: str = ""


@dataclass
class Production:
    """Un fichier produit, telechargeable."""

    identifiant: str
    nom: str
    chemin: Path
    dossier: str = ""
    #: Page muette, donnee identifiante restante, image illisible, verification
    #: impossible… : a regarder avant de transmettre. L'archive les range a part.
    a_verifier: bool = False
    #: Le document a-t-il ete relu dans Caviardoc ? Sinon le serveur ne le livre
    #: que sur confirmation explicite, comme la page le demande a l'ecran.
    relu: bool = True


#: Dossier de l'archive qui rassemble les documents a regarder, en tete de
#: liste : le « 0 » le place avant tout nom de dossier ou de fichier usuel.
DOSSIER_A_VERIFIER = "0_A_Verifier"


#: Largeur des pages montrees a la relecture : assez pour lire, assez peu pour
#: rester legere.
APERCU_LARGEUR = 1100


@dataclass
class Apercu:
    """Une page de PDF telle qu'elle est, et la geometrie de ses mots."""

    jpeg: bytes
    #: Dimensions de l'image complete, celles des boites de `lecture`.
    largeur: int
    hauteur: int
    lecture: object


@dataclass
class Tache:
    """Un traitement en cours, suivi pas a pas par la page.

    Le traitement dure — chargement du moteur, OCR, detection, relecture,
    caviardage — et la relecture le suspend indefiniment, le temps que
    l'utilisateur decide. Une requete HTTP qui attendrait tout cela expirerait.
    La page lance donc la tache, puis interroge son etat.
    """

    identifiant: str
    depot: str
    nom: str
    etat: str = "en_attente"       # en_attente | en_cours | revue | fini | echec | interrompu
    etape: str = ""
    progression: float = 0.0
    resultat: dict | None = None
    erreur: str = ""
    journal: list[str] = field(default_factory=list)

    #: Relecture : la session en cours, les textes affiches, et le signal qui
    #: relache le crochet une fois les decisions prises.
    session: object = None
    textes: dict[str, str] = field(default_factory=dict)
    reprendre: threading.Event = field(default_factory=threading.Event)
    #: Demande d'interruption, posee par la page et lue entre deux etapes.
    interrompue: threading.Event = field(default_factory=threading.Event)
    #: Secondes passees a attendre l'utilisateur. Retranchees de la duree
    #: annoncee : sans cela, la page affiche le temps de reflexion du
    #: relecteur comme s'il etait celui du traitement.
    attente: float = 0.0
    #: Ce que la relecture a appris, tel que `exceptions.apprendre_de` le rend.
    lecons: dict | None = None
    #: Le document a-t-il ete montre a la relecture, et valide ? Faux sans
    #: relecture, et en relecture des doutes quand il n'en avait aucun : son
    #: texte n'a alors ete lu par personne, et la page demande de confirmer
    #: qu'il l'a ete avant de le livrer.
    relu: bool = False
    #: Pages d'un PDF montrees pendant la relecture, par intitule. En memoire
    #: seulement, et videes des la validation : ce sont des images de patients.
    apercus: dict[str, "Apercu"] = field(default_factory=dict)

    def recevoir_apercu(self, intitule: str, image, lecture) -> None:
        """Garde une page reduite pour la relecture, et de quoi situer les mots."""
        import io
        largeur, hauteur = image.size
        reduite = image.copy()
        reduite.thumbnail((APERCU_LARGEUR, APERCU_LARGEUR * 2))
        tampon = io.BytesIO()
        reduite.save(tampon, format="JPEG", quality=72)
        self.apercus[intitule] = Apercu(tampon.getvalue(), largeur, hauteur,
                                        lecture)

    def interrompre(self) -> None:
        """Demande l'arret, et reveille la relecture si elle attend."""
        self.interrompue.set()
        self.reprendre.set()

    def verifier_arret(self) -> None:
        if self.interrompue.is_set():
            raise Interrompu()

    def noter(self, etape: str, progression: float | None = None) -> None:
        self.etape = etape
        if progression is not None:
            self.progression = progression
        self.journal.append(etape)

    def vue(self) -> dict:
        """Etat destine a la page. Ne contient jamais de texte de patient."""
        return {"id": self.identifiant, "nom": self.nom, "etat": self.etat,
                "etape": self.etape, "progression": round(self.progression, 3),
                "resultat": self.resultat, "erreur": self.erreur,
                "lecons": self.lecons}


@dataclass
class Atelier:
    """Etat d'une session d'interface : ce qui est entre, ce qui est sorti.

    Volontairement en memoire et sans persistance : une session d'interface
    n'a pas a laisser de trace entre deux lancements, et le repertoire de
    travail est efface a l'arret.
    """

    racine: Path
    depots: dict[str, Depot] = field(default_factory=dict)
    productions: dict[str, Production] = field(default_factory=dict)
    taches: dict[str, Tache] = field(default_factory=dict)
    verrou: threading.Lock = field(default_factory=threading.Lock)
    #: Rejouer et retenir les corrections. Desactivable : une machine partagee
    #: ou un poste d'essai n'a pas a garder trace des decisions.
    apprend: bool = True
    #: Textes ajoutes a la main pendant cette session, par empreinte. En memoire
    #: vive et nulle part ailleurs : le fichier des corrections ne garde que des
    #: empreintes, mais l'utilisateur doit pouvoir lire ce qu'il vient d'ajouter.
    clairs: dict[str, str] = field(default_factory=dict)
    #: Lectures faites a l'avance, par depot : le document suivant est lu
    #: pendant que l'utilisateur relit le precedent. En memoire seulement.
    prelectures: dict = field(default_factory=dict)
    _modele: object = None
    _table: object = None
    _exceptions: object = None
    _lecteur: object = None

    def prelire(self, identifiant: str, dpi: int = 300) -> None:
        """Lance la lecture d'un PDF depose, en arriere-plan, une a la fois."""
        from concurrent.futures import ThreadPoolExecutor

        from .documents.caviardage import lire_pdf

        depot = self.depots.get(identifiant)
        if (depot is None or depot.chemin.suffix.lower() != ".pdf"
                or identifiant in self.prelectures):
            return
        with self.verrou:
            if self._lecteur is None:
                self._lecteur = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="prelecture")
        self.prelectures[identifiant] = (dpi, self._lecteur.submit(
            lire_pdf, depot.chemin, dpi))

    def oublier_prelecture(self, identifiant: str) -> None:
        """Abandonne la lecture d'avance d'un depot, et attend qu'elle lache
        le fichier : Windows refuse d'effacer un fichier encore ouvert."""
        attendue = self.prelectures.pop(identifiant, None)
        if attendue is not None and not attendue[1].cancel():
            try:
                attendue[1].result()
            except Exception:
                pass

    def prelecture(self, identifiant: str, dpi: int):
        """La lecture faite a l'avance pour ce depot, ou ``None``.

        Attend qu'elle se termine si elle est en cours. Un echec n'est pas une
        erreur : le document sera simplement lu comme d'habitude.
        """
        attendue = self.prelectures.pop(identifiant, None)
        if attendue is None or attendue[0] != dpi:
            return None
        try:
            return attendue[1].result()
        except Exception:
            return None

    def exceptions(self):
        """Repertoire des corrections retenues, relu a chaque document.

        Relu, et non garde en memoire : une correction faite sur le document
        precedent doit valoir pour le suivant, dans la meme session.
        """
        if not self.apprend:
            return None
        from .exceptions import Repertoire
        return Repertoire.charger()

    def entrees(self) -> Path:
        d = self.racine / "entrees"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def sorties(self) -> Path:
        d = self.racine / "sorties"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def modele(self):
        """Charge le moteur au premier besoin, et une seule fois.

        Le chargement prend une dizaine de secondes : le faire au demarrage du
        serveur retarderait l'affichage de la page pour un utilisateur qui n'a
        peut-etre encore rien a traiter.
        """
        with self.verrou:
            if self._modele is None:
                from .engine import load_model
                self._modele = load_model()
            return self._modele

    def table(self):
        """Table de pseudonymes partagee par toute la session.

        Deux documents du meme patient deposes ensemble doivent recevoir le
        meme pseudonyme, faute de quoi le lien entre eux se perd. Le revers
        est le cumul : une IA additionne ce que chaque document dit de
        `PATIENT_1`. La page le dit sous le choix du format, et « Nouvelle
        demande » remet la table a zero.
        """
        if self._table is None:
            from .pseudonymes import TablePseudonymes
            self._table = TablePseudonymes()
        return self._table


#: Caracteres qu'un nom de dossier ne peut pas porter sous Windows. Une archive
#: produite sur un Mac doit s'extraire partout.
_INTERDITS_DOSSIER = re.compile(r'[<>:"|?*]')


def assainir_dossier(brut: str) -> str:
    """Chemin relatif fourni par le navigateur, rendu inoffensif.

    Comme le nom de fichier, il vient du poste et peut etre hostile : aucun
    `..`, aucune racine, aucun caractere de controle. Il n'est de toute facon
    jamais joint a un repertoire du disque — il ne sert qu'a nommer les
    entrees de l'archive, ou un `..` suffirait a ecrire hors du dossier
    d'extraction.
    """
    parties = []
    for partie in re.split(r"[\\/]+", brut or ""):
        partie = "".join(c for c in partie if c.isprintable()).strip()
        partie = _INTERDITS_DOSSIER.sub("_", partie).strip(" .")
        if partie:
            parties.append(partie[:120])
    return "/".join(parties[:32])


_RESERVES_WINDOWS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)])


def assainir_nom(nom: str) -> str:
    """Nom de fichier sans separateur ni segment « .. ».

    `Path(nom).name` ne voit pas l'antislash sous macOS et Linux, ou il est un
    caractere de nom legitime ; le nom finit pourtant comme entree d'un ZIP que
    l'utilisateur extraira peut-etre sous Windows.
    """
    propre = re.split(r"[\\/]", nom)[-1]
    propre = re.sub(r'[<>:"|?*\x00-\x1f\x7f]', "_", propre).strip(" .")
    if propre and propre.split(".")[0].rstrip().upper() in _RESERVES_WINDOWS:
        propre = "_" + propre
    suffixe = Path(nom).suffix.lower()
    if not propre or propre.lower() == suffixe:
        propre = f"document{suffixe}"
    return propre


def controler_docx(chemin: Path) -> None:
    """Refuse un .docx dont la taille decompressee ou le nombre d'entrees sont
    demesures. Un fichier qui n'est pas un ZIP passe : il echouera a la lecture.
    """
    # Les plafonds vivent avec la lecture du .docx : une seule definition.
    from .documents.docx import ENTREES_MAX_DOCX, INFLATION_MAX_DOCX
    try:
        with zipfile.ZipFile(chemin) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, OSError):
        return
    if len(infos) > ENTREES_MAX_DOCX:
        raise ValueError(f"document trop complexe ({len(infos)} entrees, "
                         f"maximum {ENTREES_MAX_DOCX} entrees)")
    total = sum(i.file_size for i in infos)
    if total > INFLATION_MAX_DOCX:
        raise ValueError(f"document trop volumineux une fois decompresse "
                         f"({total // (1 << 20)} Mo, maximum "
                         f"{INFLATION_MAX_DOCX // (1 << 20)} Mo)")


def deposer(atelier: Atelier, nom: str, flux, taille: int,
            dossier: str = "") -> Depot:
    """Ecrit un televersement sur le disque, sous un nom que le client ne choisit pas.

    Le nom d'origine n'est conserve que pour l'affichage : il est fourni par le
    navigateur, donc par le document, donc potentiellement hostile. Le fichier
    sur le disque porte un identifiant tire au hasard et une extension prise
    dans une liste fermee.
    """
    suffixe = Path(nom).suffix.lower()
    if suffixe not in FORMATS_ENTREE:
        motif = ("le format .doc n'est pas pris en charge : enregistrez le "
                 "document en .docx" if suffixe == ".doc"
                 else f"format {suffixe or 'inconnu'} non pris en charge")
        raise ValueError(motif)
    if taille <= 0:
        raise ValueError("fichier vide")
    if taille > TAILLE_MAX:
        raise ValueError(f"fichier trop volumineux ({taille // (1 << 20)} Mo, "
                         f"maximum {TAILLE_MAX // (1 << 20)} Mo)")

    identifiant = secrets.token_hex(8)
    chemin = atelier.entrees() / f"{identifiant}{suffixe}"
    reste = taille
    with chemin.open("wb") as sortie:
        while reste > 0:
            bloc = flux.read(min(_CHUNK, reste))
            if not bloc:
                break
            sortie.write(bloc)
            reste -= len(bloc)
    if reste:
        chemin.unlink(missing_ok=True)
        raise ValueError("televersement interrompu")

    if suffixe == ".docx":
        try:
            controler_docx(chemin)
        except ValueError:
            chemin.unlink(missing_ok=True)
            raise

    depot = Depot(identifiant, assainir_nom(Path(nom).name), chemin, taille,
                  assainir_dossier(dossier))
    atelier.depots[identifiant] = depot
    return depot


def retirer(atelier: Atelier, identifiant: str) -> None:
    """Retire un depot de la liste, et efface l'original du disque."""
    depot = atelier.depots.get(identifiant)
    if depot is None:
        raise KeyError("depot inconnu")
    if any(t.depot == identifiant and t.etat in ("en_attente", "en_cours", "revue")
           for t in atelier.taches.values()):
        raise RuntimeError("document en cours de traitement")
    del atelier.depots[identifiant]
    atelier.oublier_prelecture(identifiant)
    depot.chemin.unlink(missing_ok=True)


def nouvelle_demande(atelier: Atelier) -> None:
    """Efface tout ce que la demande precedente a laisse, et repart a zero.

    Originaux, documents produits, archives, textes de relecture et table de
    pseudonymes : une nouvelle demande est souvent un autre patient, qui ne
    doit ni partager les pseudonymes du precedent ni en retrouver les fichiers.
    Refusee tant qu'un document est en cours de traitement.
    """
    if any(t.etat in ("en_attente", "en_cours", "revue")
           for t in atelier.taches.values()):
        raise RuntimeError("traitement en cours")
    for identifiant in list(atelier.prelectures):
        atelier.oublier_prelecture(identifiant)
    for depot in atelier.depots.values():
        depot.chemin.unlink(missing_ok=True)
    atelier.depots.clear()
    atelier.productions.clear()
    atelier.taches.clear()
    atelier.clairs.clear()
    atelier._table = None
    shutil.rmtree(atelier.sorties(), ignore_errors=True)
    shutil.rmtree(atelier.racine / "archives", ignore_errors=True)


def nom_final(atelier: Atelier, nom: str) -> str:
    """Nom de fichier anonymise avec les correspondances connues *maintenant*.

    Un fichier traite tot dans un lot a ete nomme avec ce que la table savait
    alors ; un document suivant peut reveler le nom que son titre portait. A la
    livraison, on repasse donc le nom dans la table complete : sans effet sur un
    nom deja anonymise, car les pseudonymes n'y sont pas des originaux.
    """
    from .noms import anonymiser_nom
    table = atelier.table()
    return anonymiser_nom(nom, table.correspondances,
                          personnes=table.formes_personnes)


def archiver(atelier: Atelier, identifiants, a_plat: bool = False) -> Production:
    """Rassemble des fichiers produits dans un .zip, arborescence comprise.

    Un dossier depose doit revenir comme il est parti : memes sous-dossiers,
    memes fichiers, caviardes. Les noms de dossiers portent souvent celui du
    patient — « Dupont Jean/ » — et sont anonymises comme les noms de
    fichiers, avec toutes les correspondances connues a la fin de la serie.
    """
    import zipfile

    from .noms import anonymiser_dossier

    choisis = [atelier.productions[i] for i in dict.fromkeys(identifiants)
               if i in atelier.productions]
    choisis = [p for p in choisis if p.chemin.is_file()]
    if not choisis:
        raise KeyError("rien a archiver")

    correspondances = atelier.table().correspondances
    personnes = atelier.table().formes_personnes

    def anonyme(dossier: str) -> str:
        return "/".join(anonymiser_dossier(p, correspondances,
                                           personnes=personnes)
                        for p in dossier.split("/") if p)

    racines = {p.dossier.split("/")[0] for p in choisis}
    racine = racines.pop() if len(racines) == 1 else ""
    nom = (f"{anonyme(racine)} - anonymisé.zip" if racine
           else "documents anonymisés.zip")

    chemin = atelier.racine / "archives" / f"{secrets.token_hex(8)}.zip"
    chemin.parent.mkdir(parents=True, exist_ok=True)
    pris: set[str] = set()
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as archive:
        for produit in choisis:
            if a_plat:
                voulu = (f"{DOSSIER_A_VERIFIER}_{nom_final(atelier, produit.nom)}"
                         if produit.a_verifier
                         else nom_final(atelier, produit.nom))
            else:
                voulu = "/".join(filter(None, (
                    DOSSIER_A_VERIFIER if produit.a_verifier else "",
                    anonyme(produit.dossier),
                    nom_final(atelier, produit.nom))))
            entree, rang = voulu, 2
            # Deux depots du meme nom dans le meme dossier — le meme fichier
            # depose deux fois, typiquement : aucun ne doit ecraser l'autre.
            while entree.lower() in pris:
                tige, ext = os.path.splitext(voulu)
                entree, rang = f"{tige} ({rang}){ext}", rang + 1
            pris.add(entree.lower())
            archive.write(produit.chemin, entree)

    jeton = secrets.token_hex(8)
    production = Production(jeton, nom, chemin)
    atelier.productions[jeton] = production
    return production


#: Ce que l'utilisateur peut demander de la relecture. Absent : aucune.
RELECTURE_TOUT = "tout"
RELECTURE_DOUTES = "doutes"


def mode_relecture(valeur) -> str | None:
    """Mode de relecture demande, ramene a `tout`, `doutes` ou rien.

    Le champ a d'abord ete un booleen ; il le reste accepte. Une valeur que
    l'on ne reconnait pas ne desactive pas la relecture : demander trop de
    relecture coute un clic, en demander trop peu laisse passer une erreur.
    """
    if valeur is True:
        return RELECTURE_TOUT
    if valeur in (None, False, "", "aucune"):
        return None
    return RELECTURE_DOUTES if valeur == RELECTURE_DOUTES else RELECTURE_TOUT


class _LeconsSures:
    """La session telle que la machine a le droit d'en apprendre.

    `exceptions.apprendre_de` ne lit que `rejetees` et `ajoutees`. Ici :
    - un nom de personne laisse lisible n'est jamais ecrit dans la liste
      « jamais » : ce serait un nom en clair, et il cesserait d'etre masque
      chez le patient suivant qui le porte ;
    - un masque manuel annule (origine humaine) n'est pas un « jamais » : c'est
      une annulation, pas un avis sur la chaine ;
    - un ajout est appris sans la ponctuation qui l'entoure, comme le
      rapprochement le lit (`exceptions._rogner`).
    """

    def __init__(self, session):
        from dataclasses import replace

        from .engine import est_personne
        from .exceptions import _rogner

        self.rejetees = [d for d in session.rejetees
                         if d.origine != "humain" and not est_personne(d.etiquette)]
        self.ajoutees = []
        for d in session.ajoutees:
            debut, fin = _rogner(d.texte, 0, len(d.texte))
            self.ajoutees.append(replace(d, texte=d.texte[debut:fin]))


def crochet_de_relecture(atelier: Atelier, tache: Tache,
                         seulement_doutes: bool = False):
    """Rend le crochet que les deux chaines appellent avant d'ecrire.

    Il suspend le traitement, publie les detections pour la page, et attend.
    Sans decision, rien ne se decouvre : `appliquer` ne retire que ce qui a
    ete explicitement rejete.

    Avec ``seulement_doutes``, un document sans aucun passage incertain n'est
    pas soumis : tout y reste masque, comme apres une validation sans decision.
    Un passage a juger compte ici comme un doute : lui aussi attend un regard,
    et sans ce regard il resterait lisible.
    """
    from .revue import appliquer, avec_zones, preparer

    def relire(parties, zones=None):
        tache.session = preparer(tache.nom, parties, zones)
        tache.textes = {intitule: texte for intitule, texte, _ in parties}
        if (seulement_doutes and not tache.session.doutes
                and not tache.session.a_juger):
            tache.noter("Aucun doute : document validé", 0.65)
            return avec_zones(tache.session, {
                intitule: appliquer(tache.session, intitule)
                for intitule in tache.textes})
        tache.etat = "revue"
        tache.noter("En attente de votre relecture", 0.55)

        depart = time.monotonic()
        tache.reprendre.wait()
        tache.attente += time.monotonic() - depart
        tache.verifier_arret()
        tache.relu = True

        tache.etat = "en_cours"
        tache.noter("Application de vos décisions", 0.65)

        # Ce que cette relecture apprend a la machine, pour les documents
        # suivants. Enregistre apres la validation, jamais avant : une session
        # abandonnee ne doit rien laisser derriere elle.
        if atelier.apprend:
            from .exceptions import apprendre_de
            apprise = _LeconsSures(tache.session)
            tache.lecons = apprendre_de(apprise)
            if apprise.ajoutees:
                from .exceptions import Repertoire
                repertoire = Repertoire.charger()   # le sel est celui du fichier
                for d in apprise.ajoutees:
                    atelier.clairs[repertoire.empreinte(d.texte)] = d.texte.strip()
            retenu = len(tache.lecons["retenu"])
            if retenu:
                tache.noter(f"{retenu} correction(s) retenue(s) pour la suite")

        return avec_zones(tache.session, {
            intitule: appliquer(tache.session, intitule)
            for intitule in tache.textes})

    return relire


def a_verifier(bilan: dict) -> bool:
    """Le document merite-t-il un regard avant d'etre transmis ?

    Meme regle que la pastille de la page (`gravite` : « a verifier » ou
    « a regarder ») : si l'un change, l'autre doit suivre. Une verification qui
    n'a pas pu se faire compte comme une alerte, jamais comme un succes.
    """
    return bool(bilan["fuites"] or not bilan["verifie"]
                or bilan["reserves"] or bilan["images_opaques"]
                or bilan["images_metadonnees"] or bilan["repli_complet"]
                or bilan.get("objets_retires") or bilan.get("sous_seuil"))


def zones_du_niveau(options: dict, niveau) -> OptionsZones:
    """Logos et en-tetes suivent le niveau, sauf si la requete les fixe.

    « Toutes les personnes » les retire : le logo et le bloc expediteur
    designent l'etablissement aussi surement qu'un nom. « Le patient
    seulement » les garde, puisqu'ils designent les soignants ; le nom du
    patient y est masque par la detection, qui lit en-tetes, pieds de page et
    images comme le reste du document.
    """
    from .documents.zones import OptionsZones
    from .roles import Niveau

    complet = niveau is Niveau.COMPLET
    return OptionsZones(
        logos=bool(options.get("logos", complet)),
        entetes=bool(options.get("entetes", complet)),
        bande_haute=float(options.get("bande_haute") or 0.0),
        premiere_page_seulement=not options.get("toutes_pages"))


def traiter(atelier: Atelier, identifiant: str, options: dict,
            tache: Tache | None = None) -> dict:
    """Caviarde un depot et rend un compte rendu affichable.

    Rend un dictionnaire plutot qu'un objet : il part directement en JSON vers
    la page, et l'y adapter ailleurs n'apporterait rien.
    """
    from .documents.caviardage import caviarder_pdf
    from .documents.docx import caviarder_docx
    from .noms import anonymiser_nom, renommer
    from .pipeline import PolitiqueDates
    from .roles import Niveau

    depot = atelier.depots.get(identifiant)
    if depot is None:
        raise KeyError("depot inconnu")

    formats = frozenset({"pdf", "texte"} if options.get("format") == "les-deux"
                        else {options.get("format", "pdf")})
    niveau = Niveau(options.get("niveau", "complet"))
    zones = zones_du_niveau(options, niveau)
    dates = PolitiqueDates(options.get("dates", "masquer"))

    model = atelier.modele()
    table = atelier.table()
    # Un repertoire par depot : deux « rapport.pdf » venus de deux
    # sous-dossiers ne doivent pas s'ecraser l'un l'autre.
    provisoire = atelier.sorties() / depot.identifiant / depot.nom
    provisoire.parent.mkdir(parents=True, exist_ok=True)
    est_docx = depot.chemin.suffix.lower() == ".docx"

    mode = mode_relecture(options.get("revue"))
    relecture = (crochet_de_relecture(atelier, tache,
                                      seulement_doutes=mode == RELECTURE_DOUTES)
                 if tache is not None and mode else None)
    if tache is not None:
        tache.verifier_arret()
        tache.noter("Lecture et détection", 0.25)

    memoire = atelier.exceptions()
    if est_docx:
        rapport = caviarder_docx(model, depot.chemin, provisoire,
                                 verifier=True, zones=zones, niveau=niveau,
                                 formats=formats, table=table,
                                 revue=relecture, dates=dates,
                                 exceptions=memoire)
    else:
        dpi = int(options.get("dpi") or 300)
        rapport = caviarder_pdf(model, depot.chemin, provisoire,
                                dpi=dpi,
                                lu=atelier.prelecture(depot.identifiant, dpi),
                                verifier=True, zones=zones, niveau=niveau,
                                formats=formats, table=table,
                                revue=relecture, dates=dates,
                                apercu=(tache.recevoir_apercu
                                        if relecture is not None else None),
                                arret=tache.verifier_arret if tache else None,
                                exceptions=memoire)
    if tache is not None:
        tache.noter("Vérification du document produit", 0.85)

    unites = rapport.parties if est_docx else rapport.pages
    table.enregistrer(sp for unite in unites for sp in unite.entites)

    fuites = [f.vue() for f in
              list(rapport.fuites) + list(rapport.fuites_texte)]
    # La duree du rapport englobe l'attente de la relecture, qui est du temps
    # humain et non du temps de calcul : l'afficher telle quelle laisserait
    # croire que l'outil a mis une minute la ou il a mis trois secondes.
    duree = rapport.duree - (tache.attente if tache is not None else 0.0)
    bilan = {
        "nom": depot.nom,
        "dossier": depot.dossier,
        "entites": rapport.entites_total,
        "unites": len(unites),
        "duree": round(max(duree, 0.0), 1),
        # Le temps passe en relecture, que le chronometre du lot met a part.
        "attente": round(tache.attente if tache is not None else 0.0, 1),
        "reserves": [{"page": p.numero, "reserve": p.reserve}
                     for p in getattr(rapport, "pages", []) if p.reserve],
        "images_masquees": getattr(rapport, "images_masquees", 0),
        # Information seulement : les codes sont masques, rien a verifier.
        "codes_barres": (
            [{"lieu": f"Page {p.numero}", "nombre": p.codes_barres}
             for p in getattr(rapport, "pages", [])
             if getattr(p, "codes_barres", 0)]
            + [{"lieu": i.nom.rsplit("/", 1)[-1], "nombre": i.codes_barres}
               for i in getattr(rapport, "images_lues", [])
               if getattr(i, "codes_barres", 0)]),
        # Une image qu'aucun decodeur n'ouvre garde son contenu, quel qu'il
        # soit. C'est le seul reste de l'ancienne reserve sur les images, et il
        # doit se voir : le taire serait pire que tout.
        "images_opaques": [{"nom": i.nom, "raison": i.illisible}
                           for i in getattr(rapport, "images_lues", [])
                           if not i.lue],
        "verifie": rapport.verifie,
        # Ce que le PDF produit contient d'autre que des images de pages ;
        # liste vide = rien, absent = pas de PDF ou pas de controle.
        "structure_pdf": getattr(rapport, "structure_pdf", None),
        "erreur_verification": rapport.erreur_verification,
        "repli_complet": rapport.repli_complet,
        "noms_patient": sorted(rapport.noms_patient),
        # Ce que le document portait sans l'afficher. Un PDF n'en garde aucune :
        # il est reconstruit a partir de l'image de ses pages, et le dire evite
        # qu'on croie le PDF moins bien traite que le .docx.
        "metadonnees": (list(getattr(rapport, "metadonnees", [])) if est_docx
                        else ["toutes (PDF reconstruit à partir de l'image de "
                              "ses pages)"]),
        "images_metadonnees": list(getattr(rapport, "images_metadonnees", [])),
        # Objets incorpores (OLE, altChunk) retires faute de pouvoir les lire.
        "objets_retires": getattr(rapport, "objets", 0) if est_docx else 0,
        "fuites": fuites,
        # Montre a la relecture et valide. Sinon, la page demande de confirmer
        # que le document a ete relu avant de le copier ou de le telecharger.
        "relu": bool(tache is not None and tache.relu),
        # Ce que la verification relisait encore et qui a ete noirci dans la
        # foulee. Ce ne sont plus des fuites, mais l'utilisateur doit savoir
        # que la detection avait laisse passer ces chaines.
        "corrigees": [f.vue() for f in
                      list(getattr(rapport, "corrigees", []))
                      + list(getattr(rapport, "corrigees_texte", []))],
        # Drapeau de la mesure de cette machine : None si elle tient le seuil.
        "sous_seuil": (lambda a: a["court"] if a else None)(
            doctor.avertissement_mesure()),
        "produits": [],
    }
    douteux = a_verifier(bilan)

    # Le nom de fichier porte souvent le nom du patient : il est anonymise
    # comme le contenu, avec les correspondances connues a cet instant.
    correspondances = table.correspondances
    for chemin in (provisoire, provisoire.with_suffix(".txt")):
        if not chemin.exists():
            continue
        final = chemin.parent / anonymiser_nom(chemin.name, correspondances,
                                                    personnes=table.formes_personnes)
        if final != chemin:
            final = renommer(chemin, final,
                             tache.journal if tache is not None else None)
        jeton_fichier = secrets.token_hex(8)
        atelier.productions[jeton_fichier] = Production(
            jeton_fichier, final.name, final, depot.dossier,
            a_verifier=douteux,
            relu=bool(tache is not None and tache.relu))
        bilan["produits"].append({"id": jeton_fichier, "nom": final.name,
                                  "taille": final.stat().st_size})
    return bilan


def lancer(atelier: Atelier, identifiant: str, options: dict) -> Tache:
    """Demarre le traitement d'un depot dans un fil, et rend la tache."""
    depot = atelier.depots.get(identifiant)
    if depot is None:
        raise KeyError("depot inconnu")

    tache = Tache(identifiant=secrets.token_hex(8), depot=identifiant,
                  nom=depot.nom)
    atelier.taches[tache.identifiant] = tache

    def executer():
        import traceback
        try:
            tache.etat = "en_cours"
            tache.noter("Chargement du moteur de détection", 0.05)
            atelier.modele()
            tache.resultat = traiter(atelier, identifiant, options, tache)
            tache.etat = "fini"
            tache.noter("Terminé", 1.0)
        except Interrompu:
            # Rien de ce document ne doit rester : ni sortie partielle, ni page
            # montree pendant la relecture. L'etat ne change qu'ensuite : la
            # page, qui le lit, ne doit pas relancer avant le nettoyage.
            tache.apercus.clear()
            shutil.rmtree(atelier.sorties() / identifiant, ignore_errors=True)
            tache.noter("Interrompu")
            tache.etat = "interrompu"
        except Exception as exc:
            from .documents.caviardage import DocumentTropLong
            tache.etat = "echec"
            tache.erreur = (str(exc) if isinstance(
                exc, (TesseractIndisponible, DocumentTropLong))
                            else f"{type(exc).__name__}: {exc}")
            # La trace va au journal, pas a la page : elle peut contenir un
            # chemin de fichier, donc un nom de patient.
            tache.journal.append(traceback.format_exc())
            tache.noter("Échec")

    threading.Thread(target=executer, daemon=True).start()
    return tache


def revue_en_json(tache: Tache) -> dict:
    """Detections a relire, rendues en HTML pret a afficher.

    Le surlignage est celui de la page de revue : une seule implementation,
    donc un seul comportement — et l'echappement du HTML du document, qui
    protege d'un document malveillant, n'existe qu'a un endroit.
    """
    from .revue_web import surligner

    from .engine import Span

    parties = []
    for intitule, texte in tache.textes.items():
        dets = [d for d in tache.session.detections if d.partie == intitule]
        partie = {
            "partie": intitule, "detections": len(dets),
            "doutes": sum(d.doute for d in dets),
            "a_juger": sum(d.a_juger for d in dets),
            "html": surligner(texte, dets),
            # La liste des passages : de quoi les parcourir sans lire le texte.
            "passages": [{"id": d.identifiant, "texte": d.texte,
                          "origine": d.origine, "etiquette": d.etiquette,
                          "masque": d.masquee, "juger": d.a_juger}
                         for d in sorted(dets, key=lambda x: x.debut)],
        }
        apercu = tache.apercus.get(intitule)
        if apercu is not None:
            # Les boites sont celles que le caviardage noircirait, rendues en
            # fractions de la page : l'image est affichee a n'importe quelle
            # taille.
            boites = {}
            for d in dets:
                span = Span(d.debut, d.fin, d.etiquette, d.score, d.texte,
                            d.origine)
                boites[d.identifiant] = [
                    [round(max(0.0, x0) / apercu.largeur, 5),
                     round(max(0.0, y0) / apercu.hauteur, 5),
                     round(min(x1, apercu.largeur) / apercu.largeur, 5),
                     round(min(y1, apercu.hauteur) / apercu.hauteur, 5)]
                    for x0, y0, x1, y1 in apercu.lecture.boites_pour([span])]
            # Les zones (bloc d'en-tete, logo) sont des passages d'un genre a
            # part : un rectangle sur la page, sans texte a surligner. Elles
            # se cochent et se decochent comme les autres.
            for z in tache.session.zones:
                if z.partie != intitule:
                    continue
                x0, y0, x1, y1 = z.boite
                boites[z.identifiant] = [[
                    round(max(0.0, x0) / apercu.largeur, 5),
                    round(max(0.0, y0) / apercu.hauteur, 5),
                    round(min(x1, apercu.largeur) / apercu.largeur, 5),
                    round(min(y1, apercu.hauteur) / apercu.hauteur, 5)]]
                # En tete de liste : dessinees sous les mots, pour ne pas
                # les masquer au clic.
                partie["passages"].insert(0, {
                    "id": z.identifiant, "texte": z.texte or z.etiquette,
                    "origine": "zone", "etiquette": z.etiquette,
                    "masque": z.masquee, "juger": False, "zone": True})
            partie["apercu"] = {"largeur": apercu.largeur,
                                "hauteur": apercu.hauteur}
            partie["boites"] = boites
        parties.append(partie)
    return {"document": tache.nom, "parties": parties,
            "doutes": sum(p["doutes"] for p in parties),
            "a_juger": sum(p["a_juger"] for p in parties)}


def ajouter_zone(tache: Tache, partie: str, x0: float, y0: float,
                 x1: float, y1: float) -> int:
    """Masque les mots reconnus dans un cadre tracé sur la page.

    Le cadre est en fractions de la page. Un mot est retenu quand son centre y
    tombe ; les mots consecutifs forment un seul passage. Rend le nombre de
    passages ajoutes — zero, quand le cadre ne touche aucun mot reconnu.
    """
    from .revue import ajouter

    apercu = tache.apercus.get(partie)
    if apercu is None:
        raise KeyError("page inconnue")
    x0, x1 = sorted((x0 * apercu.largeur, x1 * apercu.largeur))
    y0, y1 = sorted((y0 * apercu.hauteur, y1 * apercu.hauteur))
    lecture = apercu.lecture
    choisis = [i for i, m in enumerate(lecture.mots)
               if x0 <= (m.x0 + m.x1) / 2 <= x1 and y0 <= (m.y0 + m.y1) / 2 <= y1]
    if not choisis:
        return 0
    series: list[list[int]] = [[choisis[0]]]
    for i in choisis[1:]:
        if i == series[-1][-1] + 1:
            series[-1].append(i)
        else:
            series.append([i])
    texte = tache.textes[partie]
    for serie in series:
        debut = lecture.offsets[serie[0]][0]
        fin = lecture.offsets[serie[-1]][1]
        if debut < fin:
            ajouter(tache.session, partie, texte, debut, fin)
    return len(series)


#: Prefixe des repertoires de travail, dans le repertoire de donnees local.
PREFIXE_ATELIER = "interface-"


#: Fichier verrouille par la session qui occupe un atelier, tant qu'elle vit.
MARQUE_VIVANT = ".vivant"

#: Refus du serveur quand un document non relu est demande sans confirmation.
MESSAGE_NON_RELU = (
    "Document non relu dans Caviardoc : confirmez que vous l'avez relu "
    "avant de le télécharger.").encode("utf-8")


def _verrouiller(flux) -> bool:
    """Pose un verrou exclusif, sans attendre. Rend False s'il est deja pris.

    Le systeme le leve quand le processus meurt, de quelque facon que ce soit :
    c'est ce qui distingue une session en cours d'une session abandonnee.
    """
    try:
        if os.name == "nt":
            import msvcrt
            flux.seek(0)
            msvcrt.locking(flux.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(flux.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def tenir_atelier(racine: Path):
    """Marque l'atelier comme occupe ; le flux rendu est a fermer en dernier."""
    flux = (racine / MARQUE_VIVANT).open("a+b")
    _verrouiller(flux)
    return flux


def _atelier_occupe(dossier: Path) -> bool:
    marque = dossier / MARQUE_VIVANT
    if not marque.is_file():
        return False
    try:
        with marque.open("a+b") as flux:
            return not _verrouiller(flux)
    except OSError:
        return True         # dans le doute, on ne detruit pas


def balayer_ateliers_abandonnes() -> list[Path]:
    """Efface les repertoires de travail laisses par une session precedente.

    L'effacement a l'arret ne couvre pas tout : une coupure de courant, un
    `kill`, un plantage de l'interpreteur laissent les originaux sur le
    disque — et ce sont des documents de patients. Balayer au demarrage donne
    le rattrapage qui manque.

    Un atelier dont la session vit encore est epargne : un second double-clic
    sur le lanceur — « rien ne se passe, je reclique » — effacait les documents
    que la premiere fenetre etait en train de traiter.
    """
    effaces = []
    for reste in sorted(paths.cache_dir().glob(f"{PREFIXE_ATELIER}*")):
        if reste.is_dir() and not _atelier_occupe(reste):
            shutil.rmtree(reste, ignore_errors=True)
            effaces.append(reste)
    return effaces


#: Pictogrammes, en traits simples. Dessines en CSS — un masque teinte par
#: `currentColor` — plutot qu'en caracteres : un emoji change d'allure d'un
#: systeme a l'autre, et ces pages s'affichent sur macOS comme sous Windows.
_ICONES = {
    "deposer": "<path d='M12 15V4'/><path d='m7 9 5-5 5 5'/>"
               "<path d='M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3'/>",
    "fichier": "<path d='M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 "
               "2-2V8z'/><path d='M14 3v5h5'/>",
    "dossier": "<path d='M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 "
               "2H5a2 2 0 0 1-2-2z'/>",
    "retirer": "<path d='M6 6l12 12M18 6 6 18'/>",
    "plus": "<path d='M12 5v14M5 12h14'/>",
    "ok": "<path d='m5 12.5 4.5 4.5L19 7.5'/>",
    "attention": "<path d='M12 4 2.8 19.5h18.4z'/><path d='M12 10v4.5'/>"
                 "<path d='M12 17.2v.3'/>",
    "info": "<circle cx='12' cy='12' r='9'/><path d='M12 11v5.5M12 7.8v.4'/>",
    "telecharger": "<path d='M12 4v11'/><path d='m7 10 5 5 5-5'/><path d='M5 20h14'/>",
    "copier": "<rect x='9' y='9' width='11' height='11' rx='2'/>"
              "<path d='M5 15V6a2 2 0 0 1 2-2h8'/>",
    "cadenas": "<rect x='5' y='11' width='14' height='9' rx='2'/>"
               "<path d='M8 11V8a4 4 0 0 1 8 0v3'/>",
    "bouclier": "<path d='M12 3 4.5 6v5.5c0 4.6 3.2 8.2 7.5 9.5 4.3-1.3 "
                "7.5-4.9 7.5-9.5V6z'/><path d='m9 12 2.2 2.2L15.5 10'/>",
}


def _css_icones() -> str:
    regles = []
    for nom, traits in _ICONES.items():
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' "
               "fill='none' stroke='black' stroke-width='2' "
               "stroke-linecap='round' stroke-linejoin='round'>"
               + traits + "</svg>")
        regles.append(f'.ico-{nom}{{--m:url("data:image/svg+xml,{quote(svg)}")}}')
    return "\n".join(regles) + "\n"


_STYLE = """
:root{--fond:#f3f5f7;--carte:#fff;--encre:#16191d;--gris:#5f6b78;
--gris-doux:#8b96a2;--trait:#e1e6ea;--trait-fort:#cdd4da;--accent:#0f5c63;
--accent-fonce:#0b474d;--accent-clair:#e6f1f1;--alerte:#a12d2d;
--alerte-clair:#fcf1f1;--alerte-trait:#ebc4c4;--attention:#8a5a11;
--attention-clair:#fff7e8;--attention-trait:#ecd8a8;--ok:#23704b;
--ok-clair:#e8f4ed;--rayon:10px;
--ombre:0 1px 2px rgba(16,24,32,.05),0 4px 14px rgba(16,24,32,.04)}
*{box-sizing:border-box}
[hidden]{display:none !important}
body{margin:0;background:var(--fond);color:var(--encre);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
.ico{display:inline-block;width:18px;height:18px;flex:none;
background-color:currentColor;-webkit-mask:var(--m) center/contain no-repeat;
mask:var(--m) center/contain no-repeat}

/* En-tete : le nom, le modele, et ce qui rassure — rien ne quitte le poste. */
header{background:var(--carte);padding:16px 24px 12px;display:flex;gap:14px;
align-items:center;flex-wrap:wrap}
header.seul{border-bottom:1px solid var(--trait);padding-bottom:16px}
.marque{display:flex;gap:12px;align-items:center;flex:1;min-width:14rem}
.logo{width:38px;height:38px;border-radius:10px;background:var(--accent);
color:#fff;display:grid;place-items:center;flex:none}
.logo .ico{width:22px;height:22px}
h1{font-size:1.1rem;margin:0;font-weight:650;letter-spacing:-.01em;
line-height:1.25}
.sous{color:var(--gris);font-size:.82rem}
.pastille-maj{font:inherit;font-size:.74rem;font-weight:600;margin-left:8px;
padding:1px 9px;border-radius:999px;border:0;cursor:pointer;color:#fff;
background:var(--alerte);vertical-align:1px}
.pastille-maj::before{content:"";display:inline-block;width:6px;height:6px;
border-radius:50%;background:#fff;margin-right:5px;vertical-align:1px}
.jauge-maj{height:6px;background:var(--trait);border-radius:999px;
overflow:hidden;margin:10px 0 6px;max-width:28rem}
.jauge-maj div{height:100%;background:var(--accent);transition:width .4s}
.jauge-maj.en-cours div{background:repeating-linear-gradient(-45deg,
var(--accent) 0 8px,color-mix(in srgb,var(--accent) 70%,#fff) 8px 16px);
background-size:23px 23px;animation:onde-maj 1s linear infinite}
@keyframes onde-maj{to{background-position:23px 0}}
.local{display:inline-flex;gap:6px;align-items:center;font-size:.8rem;
font-weight:600;color:var(--ok);background:var(--ok-clair);
border-radius:999px;padding:5px 12px}
.local .ico{width:14px;height:14px}
.quitter{margin-left:auto;font:inherit;font-size:.84rem;padding:6px 14px;
border-radius:8px;border:1px solid var(--trait);background:var(--carte);
color:var(--encre);cursor:pointer}
.fin{max-width:32rem;margin:15vh auto;padding:0 20px;text-align:center}
.quitter:hover{border-color:var(--accent);color:var(--accent)}
/* Onglets : une seule page, deux surfaces. L'utilisateur ouvrait jusqu'ici
   deux fichiers differents selon qu'il voulait installer ou travailler, sans
   qu'aucun des deux ne dise que l'autre existait. */
nav.onglets{display:flex;gap:4px;background:var(--carte);padding:0 24px;
border-bottom:1px solid var(--trait)}
nav.onglets button{font:inherit;font-size:.9rem;font-weight:600;border:0;
border-radius:0;background:none;color:var(--gris);padding:10px 14px;
cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
nav.onglets button:hover{color:var(--encre);background:none}
nav.onglets button.actif{color:var(--accent);border-bottom-color:var(--accent)}
nav.onglets .pastille-inst{display:inline-block;width:7px;height:7px;
border-radius:50%;background:var(--alerte);margin-left:6px;vertical-align:1px}
.panneau{display:none}
.panneau.actif{display:block}

main{max-width:58rem;margin:0 auto;padding:24px 24px 48px}
section{background:var(--carte);border:1px solid var(--trait);
border-radius:var(--rayon);padding:20px 22px;margin-bottom:18px;
box-shadow:var(--ombre)}
h2{font-size:.76rem;text-transform:uppercase;letter-spacing:.08em;
color:var(--gris);margin:0 0 14px;font-weight:650}
/* Les deux grandes sections de l'onglet Caviarder : plus lisibles qu'un
   intertitre ordinaire. */
h2.titre-section{font-size:.95rem;font-weight:750;color:var(--encre)}

button{font:inherit;font-size:.9rem;padding:8px 16px;
border:1px solid var(--trait-fort);background:var(--carte);color:var(--encre);
border-radius:8px;cursor:pointer;display:inline-flex;gap:7px;
align-items:center;justify-content:center;
transition:background .15s,border-color .15s}
button:hover:not(:disabled){border-color:var(--gris-doux);background:#f6f8f9}
button.primaire{background:var(--accent);color:#fff;border-color:var(--accent);
font-weight:600}
button.primaire:hover:not(:disabled){background:var(--accent-fonce);
border-color:var(--accent-fonce)}
button.grand{font-size:1rem;padding:11px 26px;border-radius:9px}
button.lien{border:0;background:none;padding:2px 4px;color:var(--accent);
font-size:.84rem}
button.lien:hover:not(:disabled){background:none;text-decoration:underline}
button.petit{padding:3px 10px;font-size:.8rem;border-radius:6px}
button:disabled{opacity:.45;cursor:default}
button:focus-visible,a:focus-visible,#zone:focus-visible{
outline:2px solid var(--accent);outline-offset:2px}

/* --- Depot ------------------------------------------------------------- */
#zone{border:2px dashed var(--trait-fort);border-radius:12px;
padding:30px 20px 26px;text-align:center;color:var(--gris);cursor:pointer;
background:#fafbfc;transition:border-color .15s,background .15s}
#zone:hover{border-color:var(--accent);background:#f6fafa}
#zone.survol{border-color:var(--accent);background:var(--accent-clair);
color:var(--encre)}
.zone-icone{width:54px;height:54px;border-radius:50%;margin:0 auto 12px;
background:var(--accent-clair);color:var(--accent);display:grid;
place-items:center}
.zone-icone .ico{width:26px;height:26px}
.zone-titre{font-size:1.03rem;font-weight:600;color:var(--encre);margin:0 0 3px}
.zone-sous{font-size:.86rem;margin:0 0 16px}
.zone-boutons{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.zone-boutons .ico{width:16px;height:16px;color:var(--accent)}

.bilan-liste{display:flex;gap:10px;align-items:center;
justify-content:space-between;margin:18px 0 8px;font-size:.86rem;
color:var(--gris)}
#liste{list-style:none;padding:0;margin:0;max-height:24rem;overflow:auto;
border:1px solid var(--trait);border-radius:9px}
#liste:empty{display:none}
#liste li{display:flex;gap:10px;align-items:center;padding:8px 12px;
border-top:1px solid var(--trait);font-size:.9rem}
#liste li:first-child{border-top:0}
#liste li .ico{width:16px;height:16px;color:var(--gris-doux)}
#liste li.dossier{background:#f7f9fa;font-weight:600;font-size:.86rem}
#liste li.dossier .ico{color:var(--attention)}
#liste li.dans-dossier{padding-left:36px}
.nom{flex:1;min-width:0;overflow-wrap:anywhere}
.etat{font-size:.8rem;color:var(--gris);white-space:nowrap}
.etat.ok{color:var(--ok)}
.etat.cours{color:var(--accent)}
.etat.attention{color:var(--attention)}
.etat.mal{color:var(--alerte);font-weight:600;white-space:normal}
button.retirer{border:0;background:none;padding:4px;border-radius:6px;
color:var(--gris-doux)}
button.retirer:hover:not(:disabled){background:var(--alerte-clair);
color:var(--alerte)}
#liste li button.retirer .ico{width:14px;height:14px;color:inherit}
#ignores{margin-top:8px}

/* --- Reglages ---------------------------------------------------------- */
/* Replies : on les consulte rarement. Replies, ils tiennent en une ligne qui
   dit leur etat — ce qui suffit la plupart du temps. */
details#reglages,details.repli{background:var(--carte);
border:1px solid var(--trait);border-radius:var(--rayon);margin:0 0 18px;
box-shadow:var(--ombre)}
details#reglages > summary,details.repli > summary{cursor:pointer;
padding:13px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;
list-style:none;border-radius:var(--rayon)}
details summary::-webkit-details-marker{display:none}
details > summary::before{content:"▸";color:var(--gris);flex:none;
transition:transform .15s;display:inline-block}
details[open] > summary::before{transform:rotate(90deg)}
details > summary:hover{background:#f7f9fa}
.cle{font-weight:650}
#resume{display:flex;gap:6px;flex-wrap:wrap;flex:1}
/* Ce qu'on obtient se distingue de ce qu'on masque : deux familles de
   reglages, deux teintes (le format en bleu), comme dans le panneau deplie.
   Chaque puce est une liste deroulante : on change sans deplier. */
.puce.sortie{background-color:#d6e8e9;color:var(--accent-fonce);font-weight:600}
.puce.format{background-color:#dce8fa;color:#1d4f91;font-weight:600}
.puce{font:inherit;font-size:.78rem;background-color:#edf1f3;color:var(--gris);
padding:2px 24px 2px 10px;border:0;border-radius:999px;cursor:pointer;
-webkit-appearance:none;appearance:none;background-repeat:no-repeat;
background-position:right 9px center;background-size:8px 5px;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 5'%3E%3Cpath d='M0 0l4 5 4-5z' fill='%23707b86'/%3E%3C/svg%3E")}
.puce:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.modifier::after{content:"Modifier";font-size:.84rem;font-weight:600;
color:var(--accent)}
details[open] .modifier::after{content:"Fermer"}
.groupes{display:grid;grid-template-columns:1fr;gap:16px;padding:4px 18px 18px}
@media(min-width:52rem){.groupes{grid-template-columns:1fr 1fr}}
fieldset.groupe{border:1px solid var(--trait);border-radius:9px;margin:0;
padding:4px 16px 6px;min-width:0}
fieldset.groupe legend{font-size:.74rem;text-transform:uppercase;
letter-spacing:.08em;color:var(--gris);font-weight:650;padding:0 6px;
margin-left:-6px}
.reglage{padding:12px 0;border-top:1px solid var(--trait)}
.reglage:first-of-type{border-top:0}
.intitule{font-size:.88rem;font-weight:600;margin-bottom:7px}
.segments{display:inline-flex;flex-wrap:wrap;gap:2px;background:#edf1f3;
border-radius:9px;padding:3px}
.segments label{position:relative;margin:0;cursor:pointer}
.segments input{position:absolute;opacity:0;width:1px;height:1px;margin:0}
.segments span{display:block;padding:5px 12px;border-radius:7px;
font-size:.86rem;color:var(--gris);transition:background .15s}
.segments label:hover span{color:var(--encre)}
.segments input:checked + span{background:var(--carte);color:var(--encre);
font-weight:600;box-shadow:0 1px 3px rgba(16,24,32,.14)}
.segments.format input:checked + span{background:#dce8fa;color:#1d4f91}
.segments input:focus-visible + span{outline:2px solid var(--accent);
outline-offset:1px}
.aide{font-size:.82rem;color:var(--gris);margin:7px 0 0;line-height:1.5}
.interrupteur{display:flex;gap:12px;align-items:flex-start;cursor:pointer;
margin:0}
.interrupteur input{-webkit-appearance:none;appearance:none;flex:none;
width:36px;height:21px;border-radius:999px;background:var(--trait-fort);
position:relative;margin:1px 0 0;cursor:pointer;transition:background .15s}
.interrupteur input::after{content:"";position:absolute;top:2px;left:2px;
width:17px;height:17px;border-radius:50%;background:#fff;
box-shadow:0 1px 2px rgba(0,0,0,.25);transition:left .15s}
.interrupteur input:checked{background:var(--accent)}
.interrupteur input:checked::after{left:17px}
.interrupteur input:focus-visible{outline:2px solid var(--accent);
outline-offset:2px}
.interrupteur.fixe{cursor:default}
.toujours{flex:none;width:36px;height:21px;border-radius:999px;margin-top:1px;
background:var(--ok-clair);color:var(--ok);display:grid;place-items:center}
.toujours .ico{width:13px;height:13px}
.interrupteur strong{display:block;font-size:.88rem;font-weight:600}
.interrupteur small{display:block;font-size:.82rem;color:var(--gris);
line-height:1.5;margin-top:2px}

button.telecharger{background:none;cursor:pointer;font-family:inherit}

/* --- Action et suivi --------------------------------------------------- */
.action{display:flex;gap:18px;align-items:center;flex-wrap:wrap;
margin:6px 0 18px}
.note{display:flex;gap:8px;align-items:flex-start;font-size:.83rem;
color:var(--gris);margin:0;flex:1;min-width:16rem}
.note .ico{width:16px;height:16px;margin-top:2px;color:var(--attention)}
.note.relire,.note.relire .ico{color:var(--alerte);font-weight:600}
#suivi{margin:0 0 18px}
.jauge{height:6px;background:var(--trait);border-radius:999px;
overflow:hidden;margin:0 0 7px}
.jauge div{height:100%;background:var(--accent);width:0;
transition:width .3s;border-radius:999px}
#progression{font-size:.86rem;color:var(--gris);overflow-wrap:anywhere}

/* --- Resultats --------------------------------------------------------- */
.tete-resultats{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
margin-bottom:4px}
.tete-resultats h2{margin:0}
#bilan{flex:1;font-size:.86rem;color:var(--gris)}
.option-archive{display:flex;gap:8px;align-items:flex-start;cursor:pointer;
font-size:.86rem;margin:2px 0 6px;padding:8px 12px;background:var(--fond);
border-radius:8px}
.option-archive[hidden]{display:none}
.option-archive input{margin:3px 0 0;accent-color:var(--accent)}
.option-archive small{display:block;color:var(--gris)}
.tourne{width:14px;height:14px;flex:none;border:2px solid currentColor;
border-right-color:transparent;border-radius:50%;
animation:tour .7s linear infinite}
@keyframes tour{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.tourne{animation-duration:2s}}
.resultat{padding:14px 0;border-top:1px solid var(--trait)}
.resultat:first-child{border-top:0}
.res-ligne{display:flex;gap:12px;align-items:flex-start}
.res-icone{width:28px;height:28px;border-radius:50%;display:grid;
place-items:center;flex:none}
.res-icone .ico{width:15px;height:15px}
.resultat.ok .res-icone{background:var(--ok-clair);color:var(--ok)}
.resultat.attention .res-icone{background:var(--attention-clair);
color:var(--attention)}
.resultat.alerte .res-icone,.resultat.echec .res-icone{
background:var(--alerte-clair);color:var(--alerte)}
.res-corps{flex:1;min-width:0}
.res-nom{font-weight:600;font-size:.94rem;overflow-wrap:anywhere}
.res-dossier{color:var(--gris);font-weight:400}
.detail{font-size:.83rem;color:var(--gris)}
.rassurance{margin-top:14px;padding:10px 14px;border:1px solid var(--trait);border-radius:8px;font-size:.88rem}
/* Sans `list-style:none`, le triangle natif s'ajoutait au « ▸ » commun. */
.rassurance summary{cursor:pointer;font-weight:600;list-style:none;
display:flex;gap:10px;align-items:center}
.rassurance p,.rassurance ul{margin:8px 0 0}
.res-liens{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
/* Non relu : une case a cocher avant de copier ou de telecharger. */
.confirmer-relu{display:flex;gap:8px;align-items:flex-start;margin-top:9px;
font-size:.86rem;cursor:pointer}
.confirmer-relu input{margin:3px 0 0;flex:none}
.confirmer-relu.a-cocher{color:var(--attention);font-weight:600}
.telecharger{display:inline-flex;gap:6px;align-items:center;padding:5px 12px;
border:1px solid var(--accent);border-radius:7px;color:var(--accent);
text-decoration:none;font-size:.84rem;font-weight:600}
.telecharger:hover{background:var(--accent-clair)}
.telecharger .ico{width:15px;height:15px}
/* Telecharger un document dont on vient de dire qu'il identifie encore
   quelqu'un ne doit pas etre le geste le plus facile de la page. */
.telecharger.risque{border-color:var(--alerte-trait);color:var(--alerte)}
.telecharger.risque:hover{background:var(--alerte-clair)}
.bandeau-mesure{background:var(--alerte);color:#fff;font-weight:600;
padding:12px 16px;border-radius:8px;margin:0 0 16px}
.fuite{background:var(--alerte-clair);border:1px solid var(--alerte-trait);
border-radius:8px;padding:10px 13px;margin-top:10px;font-size:.85rem;
color:var(--alerte)}
.fuite strong{display:block;margin-bottom:2px}
.fuite p{margin:0 0 6px;font-weight:400}
.fuite p:last-child{margin-bottom:0}
.fuite ul{margin:0;padding-left:1.1rem}
.fuite li{margin:2px 0}
/* Un element de contexte n'est pas une re-identification : meme forme, mais
   pas la meme couleur, sinon les deux se valent a l'oeil. */
.fuite.contexte{background:var(--attention-clair);
border-color:var(--attention-trait);color:var(--attention)}
/* Corrige : plus rien a craindre, mais un passage avait echappe. */
.fuite.corrige{background:var(--ok-clair);border-color:var(--trait);
color:var(--ok)}
.pourquoi{font-weight:400;opacity:.8}
.raison{font-weight:400;font-style:italic;opacity:.75;font-size:.92em}
.avis{background:var(--attention-clair);border:1px solid var(--attention-trait);
border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:.88rem}

/* --- Relecture --------------------------------------------------------- */
/* Elle prend toute la page : le reste attend sa decision. */
#relecture-hote{display:none}
body.en-relecture #relecture-hote{display:block}
body.en-relecture main:not(#relecture-hote),
body.en-relecture nav.onglets{display:none}
.relecture-tete{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;
margin-bottom:14px}
.relecture-tete h2{margin:0}
#titreRelecture{font-weight:600;font-size:1.02rem;overflow-wrap:anywhere}
#rangRelecture{color:var(--gris);font-size:.85rem}
/* Memes couleurs que la page de revue, meme signification. */
:root{--regle:#0f5c63;--modele:#8a5a11;--propage:#4a4a8f;--rejet:#9e2b2b;
--ajout:#256b4a;--juger:#8a3a78}
.partie{border:1px solid var(--trait);border-radius:8px;margin-bottom:14px;
overflow:hidden}
.partie h3{font-size:.74rem;text-transform:uppercase;letter-spacing:.08em;
color:var(--gris);margin:0;padding:9px 14px;background:#eef1f3;
border-bottom:1px solid var(--trait);font-weight:600}
.texte{padding:14px;white-space:pre-wrap;word-break:break-word;
font:13px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace}
mark{padding:1px 2px;border-radius:2px;cursor:pointer;border-bottom:2px solid}
mark.regle{background:#dcecec;border-color:var(--regle)}
mark.modele{background:#f7ebd8;border-color:var(--modele)}
mark.propage{background:#e6e6f2;border-color:var(--propage)}
mark.humain{background:#deeee4;border-color:var(--ajout)}
mark.rejetee{background:transparent;border-color:var(--rejet);
border-bottom-style:dashed;opacity:.55}
/* A juger : lisible par defaut, et pourtant visible — ni masque, ni oublie. */
mark.juger{background:#f4e3ef;border-color:var(--juger)}
mark.juger.rejetee{background:#fbf2f8;border-color:var(--juger);
border-bottom-style:dotted;opacity:1}
.legende{display:flex;gap:12px;font-size:.78rem;color:var(--gris);
flex-wrap:wrap;flex-basis:100%;margin:0}
.legende span::before{content:"";display:inline-block;width:10px;height:10px;
margin-right:6px;border-radius:2px;vertical-align:-1px}
.l-regle::before{background:#dcecec;border:1px solid var(--regle)}
.l-modele::before{background:#f7ebd8;border:1px solid var(--modele)}
.l-propage::before{background:#e6e6f2;border:1px solid var(--propage)}
.l-humain::before{background:#deeee4;border:1px solid var(--ajout)}
.l-juger::before{background:#f4e3ef;border:1px solid var(--juger)}
.l-bloc::before{background:#3b4652;border:1px solid #3b4652}
.outils-relecture{display:flex;gap:12px;align-items:center;flex-wrap:wrap;
margin:0 0 12px;padding:8px 0;font-size:.86rem;position:sticky;top:0;z-index:5;
background:var(--carte);border-bottom:1px solid var(--trait)}
.outils-relecture .case{display:flex;gap:7px;align-items:center;cursor:pointer;
font-weight:600}
#resumeDoutes{color:var(--gris)}
.note-solo{display:none;font-size:.82rem;color:var(--gris);margin:0 0 12px}
#relecture.solo-doutes .note-solo{display:block}
/* Seuls les doutes se distinguent : le reste reste masque, mais ne crie plus. */
#relecture.solo-doutes .partie.sans-doute{display:none}
#relecture.solo-doutes mark:not(.modele):not(.juger):not(.rejetee){
background:transparent;
border-bottom:1px dotted var(--gris-doux)}
mark.vise{outline:3px solid var(--modele);outline-offset:2px}
.lot-relecture{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
.lot-relecture[hidden]{display:none}
.lot-relecture button{font-size:.82rem;padding:6px 12px}
.barre-relecture{position:sticky;bottom:0;background:var(--carte);z-index:5;
border-top:1px solid var(--trait);margin:14px -22px -20px;padding:12px 22px;
display:flex;gap:12px;align-items:center;flex-wrap:wrap;
border-radius:0 0 var(--rayon) var(--rayon)}
.barre-relecture .primaire{margin-left:auto}
.lot-relecture:not([hidden]) + .primaire{margin-left:0}
.compte{color:var(--gris);font-size:.9rem;font-weight:600}

/* Aide : une ligne, qu'on referme une fois comprise. */
.aide-relecture{display:flex;gap:14px;align-items:center;margin-bottom:10px;
padding:9px 14px}
.aide-relecture[hidden]{display:none}
.aide-relecture > span{flex:1}
kbd{font:600 .78rem ui-monospace,Menlo,monospace;background:#fff;
border:1px solid var(--trait-fort);border-bottom-width:2px;border-radius:4px;
padding:0 5px}
.relecture-tete .lien{margin-left:auto}
.info-relecture{margin:0 0 10px;padding:7px 12px;border-radius:7px;
background:var(--attention-clair);color:var(--attention);font-size:.85rem}
.info-relecture[hidden]{display:none}

/* Choix de l'affichage : la page telle qu'elle est, ou son texte. */
.vues{display:inline-flex;gap:2px;background:#edf1f3;border-radius:9px;
padding:3px}
.vues[hidden]{display:none}
.vues button{border:0;background:none;padding:4px 14px;border-radius:7px;
font-size:.84rem;color:var(--gris);cursor:pointer}
.vues button[aria-pressed="true"]{background:var(--carte);color:var(--encre);
font-weight:600;box-shadow:0 1px 3px rgba(16,24,32,.14)}

/* Le document a gauche, la liste des passages a droite. */
.relecture-corps{display:grid;grid-template-columns:minmax(0,1fr) 19rem;
gap:18px;align-items:start}
@media(max-width:60rem){.relecture-corps{grid-template-columns:1fr}}
.colonne-liste{position:sticky;top:64px;border:1px solid var(--trait);
border-radius:8px;overflow:hidden;max-height:calc(100vh - 170px);
display:flex;flex-direction:column}
@media(max-width:60rem){.colonne-liste{position:static;max-height:none}}
.colonne-liste h3{font-size:.74rem;text-transform:uppercase;letter-spacing:.08em;
color:var(--gris);margin:0;padding:9px 14px;background:#eef1f3;
border-bottom:1px solid var(--trait);font-weight:600}
#passages{list-style:none;margin:0;padding:0;overflow:auto}
#passages li{display:flex;gap:9px;align-items:center;padding:6px 12px;
border-top:1px solid var(--trait);font-size:.86rem}
#passages li:first-child{border-top:0}
#passages li.groupe{background:#f7f9fa;font-size:.74rem;font-weight:650;
text-transform:uppercase;letter-spacing:.06em;color:var(--gris);
padding:5px 12px}
#passages li.passage{border-left:3px solid var(--trait)}
#passages li.passage.regle{border-left-color:var(--regle)}
#passages li.passage.modele{border-left-color:var(--modele)}
#passages li.passage.propage{border-left-color:var(--propage)}
#passages li.passage.humain{border-left-color:var(--ajout)}
#passages li.passage.juger{border-left-color:var(--juger)}
#passages li.passage.bloc{border-left-color:#3b4652}
#passages li.passage.juger.rejetee .passage-texte{color:var(--encre)}
#passages li.passage.juger.rejetee .etat-passage{background:#f4e3ef;
color:var(--juger)}
#passages li.passage.rejetee .passage-texte{color:var(--gris)}
.etat-passage{flex:none;font-size:.72rem;font-weight:600;padding:1px 7px;
border-radius:999px;background:var(--encre);color:#fff;white-space:nowrap}
#passages li.passage.rejetee .etat-passage{background:var(--ok-clair);
color:var(--ok)}
#passages input{flex:none;margin:0;cursor:pointer}
#passages .passage-texte{flex:1;min-width:0;border:0;background:none;
box-shadow:none;padding:2px 0;display:block;text-align:left;font:inherit;
color:var(--encre);cursor:pointer;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.passage-texte:hover{text-decoration:underline}
.passage-meta{flex:none;font-size:.74rem;color:var(--gris)}
#relecture.solo-doutes #passages li.passage:not(.modele):not(.juger):not(.bloc),
#relecture.solo-doutes #passages li.groupe.autres{display:none}
.vide-liste{padding:14px;color:var(--gris);font-size:.85rem}

/* La page, avec une boite sur chaque passage qui serait noirci. */
.page-image{margin:0 auto 18px;max-width:56rem}
.page-image h3{font-size:.74rem;text-transform:uppercase;letter-spacing:.08em;
color:var(--gris);margin:0 0 6px;font-weight:600}
.cadre-page{position:relative;border:1px solid var(--trait);border-radius:4px;
overflow:hidden;background:#fff;cursor:crosshair;user-select:none;
-webkit-user-select:none}
.cadre-page img{display:block;width:100%;height:100%;pointer-events:none}
.zone{position:absolute;background:rgba(20,24,28,.82);
border:2px solid var(--regle);border-radius:2px;cursor:pointer;
box-sizing:border-box}
.zone.modele{border-color:var(--modele);background:rgba(138,90,17,.78);
border-width:3px}
.zone.propage{border-color:var(--propage)}
.zone.humain{border-color:var(--ajout)}
.zone.juger{border-color:var(--juger);background:rgba(138,58,120,.78)}
.zone.bloc{border:2px solid #3b4652;background:rgba(20,24,28,.82)}
.zone.juger.rejetee{background:rgba(138,58,120,.1);border-style:dotted;
border-color:var(--juger)}
.zone:hover{background:rgba(20,24,28,.3)}
.zone.modele:hover{background:rgba(138,90,17,.3)}
.zone.rejetee{background:transparent;border-style:dashed;border-color:var(--rejet)}
.zone.vise{outline:3px solid var(--modele);outline-offset:3px}
#relecture.solo-doutes .page-image.sans-doute{display:none}
#relecture.solo-doutes .zone:not(.modele):not(.juger):not(.bloc):not(.rejetee){
background:rgba(20,24,28,.3);border-width:1px}
.cadre-trace{position:absolute;border:2px dashed var(--accent);
background:rgba(15,92,99,.15);pointer-events:none}
.legende-cadre{font-size:.78rem;color:var(--gris);margin:0 0 4px}

/* Masquer ce qu'on vient de choisir, la ou on regarde. */
.bulle{position:fixed;z-index:20;transform:translate(-50%,-100%);
padding:5px 12px;font-size:.84rem;font-weight:600;border-radius:7px;
background:var(--encre);color:#fff;border:0;box-shadow:0 3px 10px rgba(0,0,0,.3);
cursor:pointer}
.bulle[hidden]{display:none}

/* --- Memoire ----------------------------------------------------------- */
details.repli > *:not(summary){padding:0 18px}
details.repli > :last-child{padding-bottom:16px}
#resumeMemoire{color:var(--gris);font-size:.86rem}
.souvenir{display:flex;gap:10px;align-items:center;padding:8px 0;
border-top:1px solid var(--trait);font-size:.9rem}
.souvenir:first-child{border-top:0}
#memoire{margin-bottom:6px}
.aide ul{margin:6px 0 0;padding-left:1.1rem}
.aide li{margin:2px 0}

/* --- Pied de page : auteur, licence, absence de garantie ----------------- */
.pied{max-width:58rem;margin:0 auto;padding:4px 24px 28px;font-size:.78rem;
color:var(--gris-doux);text-align:center}
.pied a{color:inherit}

/* --- Modeles ----------------------------------------------------------- */
.modele{border:1px solid var(--trait);border-radius:9px;padding:12px 14px;
margin-bottom:10px}
.modele.retenu{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.modele-tete{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.modele-nom{font-weight:600}
.badge{font-size:.72rem;font-weight:650;padding:2px 9px;border-radius:999px;
white-space:nowrap}
.badge.ok{background:var(--ok-clair);color:var(--ok)}
.badge.attention{background:var(--attention-clair);color:var(--attention)}
.badge.mal{background:var(--alerte-clair);color:var(--alerte)}
.badge.service{background:var(--accent-clair);color:var(--accent)}
.modele-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
.machine{font-size:.84rem;color:var(--gris);margin:4px 0 10px}
.machine.mal{color:var(--alerte);background:var(--alerte-clair);font-weight:600;
padding:8px 11px;border-radius:6px}
.machine.attention{color:var(--attention);background:var(--attention-clair);
padding:8px 11px;border-radius:6px}
ul.a-supprimer{list-style:none;padding:0;margin:8px 0 12px}
ul.a-supprimer li{padding:6px 0;border-top:1px solid var(--trait)}
ul.a-supprimer li:first-child{border-top:0}
ul.a-supprimer .aide{margin:1px 0 0}
.intitule-dehors{font-size:.84rem;font-weight:600;margin:16px 0 4px}
ul.a-supprimer.dehors{margin-top:0}
ul.a-supprimer.dehors code{white-space:nowrap}
ul.a-supprimer .chemin{margin:2px 0 0;font:.8rem/1.5 ui-monospace,SFMono-Regular,
Menlo,monospace;color:var(--gris);word-break:break-all;user-select:all;
cursor:text}

/* --- Licences ------------------------------------------------------------ */
table.licences{width:100%;border-collapse:collapse;font-size:.83rem;
margin-top:8px}
table.licences th{text-align:left;font-weight:600;color:var(--gris);
font-size:.74rem;text-transform:uppercase;letter-spacing:.06em;
padding:6px 8px;border-bottom:1px solid var(--trait)}
table.licences td{padding:6px 8px;border-bottom:1px solid var(--trait);
vertical-align:top}
table.licences td:first-child{white-space:nowrap}
table.licences .version{color:var(--gris-doux)}
@media(max-width:40rem){table.licences td:nth-child(2),
table.licences th:nth-child(2){display:none}}

@media(max-width:40rem){
main{padding:16px 14px 40px}
header{padding:14px 16px 10px}
nav.onglets{padding:0 10px}
section{padding:16px}
.barre-relecture{margin:0 -16px -16px;padding:12px 16px}
}
""" + _css_icones()

_SCRIPT = """
const FORMATS = ['.pdf', '.docx'];
/* Les documents de la liste. `etat` suit l'envoi (file, envoi, pret,
   erreur) ; `traitement` suit l'anonymisation de la derniere serie. */
const attente = [];
/* Fichiers trouves dans un dossier mais d'un format non traite, par
   extension : on les compte au lieu d'en faire autant de lignes d'erreur. */
const ignores = {};
let enCours = false;
let interruption = false;
/* Taches du lot en cours de suivi : identifiant -> {f, rang, t}. Plusieurs a
   la fois quand les documents sont traites en parallele. */
const enCoursLot = new Map();
let lotTotal = 0;
const interruptionSignalee = new Set();
let derniers = [];
/* Chronometre du lot : instant du clic sur « Caviarder », en millisecondes. */
let debutLot = 0;
/* L'archive est en cours de preparation, ou vient d'etre lancee : le bouton
   ne doit pas etre repeint par le suivi du lot. */
let archiveOccupee = false;
/* Au moins un resultat a ete telecharge : « Nouvelle demande » ne demande
   alors plus de confirmation. */
let telecharge = false;

function entetes(extra){
  return Object.assign({'X-Caviardoc-Jeton': JETON}, extra || {});
}
/* Memes conventions que `models.human` : virgule decimale, unites
   francaises, et un gigaoctet plutot que « 1385,2 Mo ». */
function humain(n){
  const unites = [[1073741824, "Go"], [1048576, "Mo"], [1024, "ko"]];
  for (const [seuil, unite] of unites){
    if (n >= seuil){
      const v = (n / seuil).toFixed(1).replace(/[.]0$/, "");
      return v.replace(".", ",") + " " + unite;
    }
  }
  return n + " o";
}
function pluriel(n, un, plusieurs){
  return n + ' ' + (n > 1 ? plusieurs : un);
}
/* « 45 s », « 3 min 05 s », « 1 h 02 min » : une duree de lot se lit d'un
   coup d'oeil, sans « 185 s » a convertir de tete. */
function chrono(secondes){
  const s = Math.max(0, Math.round(secondes));
  if (s < 60) return s + ' s';
  const deux = n => String(n).padStart(2, '0');
  if (s < 3600) return Math.floor(s / 60) + ' min ' + deux(s % 60) + ' s';
  return Math.floor(s / 3600) + ' h ' + deux(Math.floor(s / 60) % 60) + ' min';
}
function extension(nom){
  const i = nom.lastIndexOf('.');
  return i > 0 ? nom.slice(i).toLowerCase() : '';
}
function el(balise, classe, texte){
  const e = document.createElement(balise);
  if (classe) e.className = classe;
  if (texte !== undefined) e.textContent = texte;
  return e;
}
function icone(nom){
  return el('span', 'ico ico-' + nom);
}
const pause = ms => new Promise(r => setTimeout(r, ms));

/* --- la liste --------------------------------------------------------- */
function libelleEtat(f){
  if (f.etat === 'erreur') return [f.erreur, 'mal'];
  if (f.etat === 'file') return ["en attente d'envoi", ''];
  if (f.etat === 'envoi') return ['envoi…', 'cours'];
  if (f.traitement === 'attente') return ['en attente', ''];
  if (f.traitement === 'cours') return ['en cours…', 'cours'];
  if (f.traitement === 'ok') return ['terminé', 'ok'];
  if (f.traitement === 'attention') return ['terminé · à regarder', 'attention'];
  if (f.traitement === 'alerte') return ['à vérifier', 'mal'];
  if (f.traitement === 'echec') return ['échec', 'mal'];
  return [humain(f.taille), ''];
}
function ligneDocument(f, imbrique){
  const li = el('li', imbrique ? 'dans-dossier' : '');
  const [texte, classe] = libelleEtat(f);
  const b = el('button', 'retirer');
  b.type = 'button';
  b.title = 'Retirer de la liste';
  b.setAttribute('aria-label', 'Retirer ' + f.nom);
  b.append(icone('retirer'));
  b.disabled = enCours || f.etat === 'envoi';
  b.addEventListener('click', () => retirer(f));
  li.append(icone('fichier'), el('span', 'nom', f.nom),
            el('span', 'etat ' + classe, texte), b);
  return li;
}
function dessiner(){
  const ul = document.getElementById('liste');
  ul.innerHTML = '';
  const groupes = new Map();
  for (const f of attente){
    if (!groupes.has(f.dossier)) groupes.set(f.dossier, []);
    groupes.get(f.dossier).push(f);
  }
  const cles = [...groupes.keys()].sort((a, b) =>
    a === '' ? -1 : b === '' ? 1 : a.localeCompare(b, 'fr'));
  for (const dossier of cles){
    const membres = groupes.get(dossier);
    if (dossier){
      const li = el('li', 'dossier');
      li.append(icone('dossier'), el('span', 'nom', dossier),
                el('span', 'etat', pluriel(membres.length, 'document', 'documents')));
      ul.append(li);
    }
    for (const f of membres) ul.append(ligneDocument(f, Boolean(dossier)));
  }

  const prets = attente.filter(f => f.etat === 'pret');
  const envois = attente.filter(f => f.etat === 'file' || f.etat === 'envoi').length;
  const dossiers = new Set(cles.filter(Boolean).map(d => d.split('/')[0])).size;
  let texte = pluriel(prets.length, 'document prêt', 'documents prêts');
  if (dossiers) texte += ' · ' + pluriel(dossiers, 'dossier', 'dossiers');
  const poids = prets.reduce((s, f) => s + f.taille, 0);
  if (poids) texte += ' · ' + humain(poids);
  if (envois) texte += ' · ' + envois + " en cours d'envoi";
  document.getElementById('bilanListe').hidden = attente.length === 0;
  document.getElementById('compteListe').textContent = texte;
  document.getElementById('vider').disabled = enCours;
  document.getElementById('nouvelleDemande').disabled = enCours || envois > 0;

  const ecartes = Object.entries(ignores);
  const p = document.getElementById('ignores');
  p.hidden = ecartes.length === 0;
  p.replaceChildren();
  if (ecartes.length){
    const total = ecartes.reduce((s, [, noms]) => s + noms.length, 0);
    const resume = pluriel(total, 'fichier ignoré', 'fichiers ignorés')
      + ' : seuls les PDF et les .docx sont traités. Ils ne seront ni '
      + 'caviardés ni inclus dans le résultat.'
      + (ignores['.doc'] ? " Enregistrez les .doc en .docx pour les traiter." : '');
    const liste = document.createElement('ul');
    liste.className = 'a-supprimer';
    for (const [, noms] of ecartes)
      for (const n of noms){
        const li = document.createElement('li');
        li.textContent = n;
        liste.append(li);
      }
    p.append(resume, liste);
  }

  const bouton = document.getElementById('lancer');
  bouton.disabled = enCours || envois > 0 || prets.length === 0;
  const stop = document.getElementById('interrompre');
  stop.hidden = !enCours;
  stop.disabled = interruption;
  stop.textContent = interruption ? 'Interruption…' : 'Interrompre';
  bouton.textContent = enCours ? 'Caviardage en cours…'
    : prets.length > 1 ? 'Caviarder les ' + prets.length + ' documents'
    : 'Caviarder';
}
let dessinPrevu = false;
function redessiner(){
  if (dessinPrevu) return;
  dessinPrevu = true;
  requestAnimationFrame(() => { dessinPrevu = false; dessiner(); });
}

/* --- le depot ---------------------------------------------------------- */
function ajouter(trouves){
  for (const {fichier, dossier} of trouves){
    const nom = fichier.name;
    // Fichiers caches et verrous de Word : jamais des documents.
    if (nom.startsWith('.') || nom.startsWith('~$')) continue;
    if (dossier && !FORMATS.includes(extension(nom))){
      const ext = extension(nom) || 'sans extension';
      (ignores[ext] = ignores[ext] || []).push(
        (dossier ? dossier + '/' : '') + nom);
      continue;
    }
    if (attente.some(f => f.nom === nom && f.dossier === dossier
                          && f.taille === fichier.size && f.etat !== 'erreur'))
      continue;
    attente.push({nom: nom, dossier: dossier, taille: fichier.size,
                  fichier: fichier, etat: 'file'});
  }
  dessiner();
  pomper();
}
/* Un envoi a la fois : le serveur n'en traite qu'un, et des centaines de
   requetes simultanees, pour un dossier entier, deborderaient sa file. */
let pompe = false;
async function pomper(){
  if (pompe) return;
  pompe = true;
  try {
    for (let f; (f = attente.find(x => x.etat === 'file'));) await televerser(f);
  } finally { pompe = false; }
}
async function televerser(f){
  f.etat = 'envoi';
  redessiner();
  try {
    const r = await fetch('/televerser', {method:'POST',
      headers: entetes({'X-Caviardoc-Nom': encodeURIComponent(f.nom),
                        'X-Caviardoc-Dossier': encodeURIComponent(f.dossier),
                        'Content-Type':'application/octet-stream'}),
      body: f.fichier});
    if (r.ok){ f.id = (await r.json()).id; f.etat = 'pret'; }
    else { f.erreur = await r.text(); f.etat = 'erreur'; }
  } catch (e){
    f.erreur = "envoi impossible"; f.etat = 'erreur';
  }
  f.fichier = null;
  // Retire de la liste pendant l'envoi : l'original ne doit pas rester.
  if (f.id && !attente.includes(f)) oublierDepot(f.id);
  redessiner();
}
function oublierDepot(id){
  return fetch('/retirer', {method:'POST',
    headers: entetes({'Content-Type':'application/json'}),
    body: JSON.stringify({id: id})}).catch(() => {});
}
function retirer(f){
  const i = attente.indexOf(f);
  if (i < 0) return;
  attente.splice(i, 1);
  dessiner();
  if (f.id) oublierDepot(f.id);
}
function reinitialiserEcran(){
  attente.splice(0);
  for (const k of Object.keys(ignores)) delete ignores[k];
  derniers = [];
  telecharge = false;
  document.getElementById('carteResultats').hidden = true;
  document.getElementById('suivi').hidden = true;
  document.getElementById('resultats').innerHTML = '';
  document.getElementById('bilan').textContent = '';
  dessiner();
}
function toutRetirer(){
  const tous = attente.splice(0);
  reinitialiserEcran();
  for (const f of tous) if (f.id) oublierDepot(f.id);
}
/* Repartir de zero : le serveur efface originaux, documents produits et
   correspondances de noms — un autre patient ne doit pas reprendre celles du
   precedent —, puis l'ecran revient a l'etat d'accueil. */
async function nouvelleDemande(){
  if (enCours) return;
  if (!telecharge && derniers.some(b => (b.produits || []).length)
      && !confirm("Les documents caviardés n'ont pas encore été téléchargés.\\n\\n"
                  + "Ils seront effacés. Continuer ?")) return;
  const bouton = document.getElementById('nouvelleDemande');
  bouton.disabled = true;
  try {
    const r = await fetch('/nouvelle-demande', {method:'POST',
      headers: entetes({'Content-Type':'application/json'}), body: '{}'});
    if (!r.ok) throw new Error(await r.text());
    reinitialiserEcran();
    window.scrollTo({top: 0, behavior: 'smooth'});
  } catch (e){
    alert("Impossible de tout effacer : " + (e.message || e));
  }
  bouton.disabled = false;
}

/* Un dossier glisse arrive comme une arborescence a parcourir : le
   navigateur ne livre ses fichiers qu'un niveau, et un lot, a la fois. */
function lireEntree(entree, parent, sortie){
  return new Promise(resolve => {
    if (entree.isFile){
      entree.file(f => { sortie.push({fichier: f, dossier: parent}); resolve(); },
                  () => resolve());
    } else if (entree.isDirectory){
      const chemin = parent ? parent + '/' + entree.name : entree.name;
      const lecteur = entree.createReader();
      const lot = () => lecteur.readEntries(async entrees => {
        if (!entrees.length) return resolve();
        for (const e of entrees) await lireEntree(e, chemin, sortie);
        lot();
      }, () => resolve());
      lot();
    } else resolve();
  });
}
async function deposerTransfert(transfert){
  // Les entrees se lisent pendant l'evenement : apres, la liste est videe.
  const entrees = [];
  const items = transfert.items || [];
  for (let i = 0; i < items.length; i++){
    const it = items[i];
    const entree = it.kind === 'file' && it.webkitGetAsEntry
      ? it.webkitGetAsEntry() : null;
    if (entree) entrees.push(entree);
  }
  if (!entrees.length){
    ajouter([...transfert.files].map(f => ({fichier: f, dossier: ''})));
    return;
  }
  const sous = document.getElementById('zoneSous');
  const avant = sous.textContent;
  sous.textContent = 'Lecture du dossier…';
  const trouves = [];
  for (const e of entrees) await lireEntree(e, '', trouves);
  sous.textContent = avant;
  ajouter(trouves);
}
function surfaceActive(){
  const p = document.getElementById('panneau-app');
  return (!p || p.classList.contains('actif'))
    && !document.body.classList.contains('en-relecture');
}

/* --- reglages ---------------------------------------------------------- */
function valeur(nom){
  const choisi = document.querySelector('input[name=' + nom + ']:checked');
  return choisi ? choisi.value : '';
}
/* Le repli n'est acceptable que s'il dit ce qu'il cache : sans cette ligne,
   on ne saurait plus avec quels reglages on s'apprete a anonymiser. */
const RESUME = {
  niveau: {complet: "toutes les personnes", patient: "patient seulement"},
  dates: {masquer: "toutes les dates", relatives: "dates en J+n",
          garder: "date de naissance", "garder-tout": "aucune date"},
  format: {pdf: "document caviardé", texte: "texte pseudonymisé",
           "les-deux": "document + texte"},
  relecture: {tout: "relecture complète", doutes: "relecture des doutes",
              aucune: "sans relecture"},
};
const CLASSE_PUCE = {niveau: 'puce', dates: 'puce', format: 'puce format',
                     relecture: 'puce sortie'};
/* Les listes sont construites une fois : les reconstruire a chaque choix
   detruirait celle qui a le focus. */
function batirResume(hote){
  const resume = hote.closest('summary');
  for (const nom of ['niveau', 'dates', 'format', 'relecture']) {
    const liste = el('select', CLASSE_PUCE[nom]);
    liste.dataset.reglage = nom;
    liste.setAttribute('aria-label',
      document.getElementById('lib-' + nom).textContent);
    for (const [cle, texte] of Object.entries(RESUME[nom])) {
      const o = el('option', '', texte);
      o.value = cle;
      liste.append(o);
    }
    liste.addEventListener('change', () => {
      const radio = document.querySelector(
        'input[name=' + nom + '][value="' + liste.value + '"]');
      if (!radio) return;
      radio.checked = true;
      radio.dispatchEvent(new Event('change', {bubbles: true}));
    });
    hote.append(liste);
  }
  /* Un clic ou Espace/Entree dans une liste ne doit pas replier le panneau. */
  resume.addEventListener('click', e => {
    if (e.target.closest('select')) e.preventDefault();
  });
  resume.addEventListener('keydown', e => {
    if (e.target.closest('select') && (e.key === ' ' || e.key === 'Enter'))
      e.stopPropagation();
  });
  resume.addEventListener('keyup', e => {
    if (e.target.closest('select') && (e.key === ' ' || e.key === 'Enter'))
      e.preventDefault();
  });
}
function majResume(){
  const hote = document.getElementById('resume');
  if (!hote.firstChild) batirResume(hote);
  for (const liste of hote.querySelectorAll('select'))
    liste.value = valeur(liste.dataset.reglage);
  document.getElementById('aide-niveau').textContent = AIDES.niveau[valeur('niveau')];
  document.getElementById('aide-dates').textContent = AIDES.dates[valeur('dates')];
  document.getElementById('aide-format').textContent = AIDES.format[valeur('format')];
  document.getElementById('aide-relecture').textContent =
    AIDES.relecture[valeur('relecture')];
}
/* L'explication suit le choix : elle decrit ce qui va se passer, pas les
   trois cas a la fois. */
const CUMUL = "Une personne garde le même pseudonyme dans tous les documents "
  + "de la demande : une IA suit le même patient d'un document à l'autre, "
  + "mais elle additionne aussi ce que chacun dit de lui. Deux documents "
  + "anodins peuvent ensemble le rendre reconnaissable. Pour des documents "
  + "qui n'ont pas à être rapprochés, faites une nouvelle demande.";
const AIDES = {
  niveau: {
    complet: "Les noms du patient, de ses proches et des soignants sont tous "
      + "masqués, de même que les images (logos, photos, signatures, "
      + "tampons) et les blocs d'adresse de l'expéditeur et du destinataire.",
    patient: "Les noms des médecins et des soignants restent lisibles, comme "
      + "les logos et les en-têtes ; le nom du patient y est masqué. "
      + "Si l'outil ne trouve pas qui est le patient dans un document, il y "
      + "masque toutes les personnes, par précaution.",
  },
  dates: {
    masquer: "Toutes les dates sont masquées, y compris celles des "
      + "consultations et des examens.",
    relatives: "Chaque date devient son écart en jours avec la plus ancienne "
      + "du document : J0, J+4, J+371. Les intervalles restent lisibles, "
      + "aucune date ne peut être cherchée. La date de naissance reste "
      + "masquée.",
    garder: "Les dates de consultation, d'examen ou d'hospitalisation restent "
      + "lisibles. Seule la date de naissance est masquée.",
    "garder-tout": "Aucune date n'est masquée, pas même la date de naissance "
      + "— qui suffit souvent, à elle seule, à retrouver une personne.",
  },
  /* Le cumul : un meme pseudonyme d'un document a l'autre aide l'IA a suivre
     le patient, et lui permet du meme coup d'additionner les details. */
  format: {
    pdf: "Un PDF est noirci au pixel, sans texte caché. Un .docx reste "
      + "modifiable : chaque donnée y devient un pseudonyme ([PATIENT_1]), "
      + "le même dans tous les documents de la demande. Une IA peut alors "
      + "additionner ce que chacun dit du patient.",
    texte: CUMUL,
    "les-deux": CUMUL,
  },
  relecture: {
    tout: "Pour chaque document, vous voyez ce qui a été trouvé : vous pouvez "
      + "retirer un masque posé à tort, ou masquer un passage oublié.",
    doutes: "Vous ne voyez que les passages dont l'outil n'est pas sûr (en "
      + "orange), et ceux qui peuvent aider à reconnaître la personne (en "
      + "violet). Un document sans aucun des deux est validé sans vous "
      + "déranger.",
    aucune: "Tout ce que l'outil trouve est masqué, sans que vous le voyiez. "
      + "Avant de copier ou de télécharger un document, vous confirmerez "
      + "l'avoir relu : ce qu'il raconte peut suffire à reconnaître la "
      + "personne.",
  },
};

/* --- le traitement ----------------------------------------------------- */
function avancer(fraction, texte){
  document.querySelector('#jauge div').style.width = (fraction * 100) + '%';
  document.getElementById('progression').textContent = texte;
}
function gravite(b){
  if (b.echec) return 'echec';
  const fuites = b.fuites || [];
  if (fuites.some(f => f.identifiante) || !b.verifie || b.sous_seuil) return 'alerte';
  if (fuites.length || (b.reserves || []).length
      || (b.images_opaques || []).length || (b.images_metadonnees || []).length
      || b.objets_retires || b.repli_complet) return 'attention';
  return 'ok';
}
async function lancer(){
  const aTraiter = attente.filter(f => f.etat === 'pret');
  if (!aTraiter.length) return;
  const options = {
    niveau: valeur('niveau'),
    dates: valeur('dates'),
    format: valeur('format'),
    revue: valeur('relecture'),
  };
  modeRestant = '';
  enCours = true;
  debutLot = performance.now();
  interruption = false;
  derniers = [];
  telecharge = false;
  for (const f of attente) f.traitement = aTraiter.includes(f) ? 'attente' : '';
  const sortie = document.getElementById('resultats');
  sortie.innerHTML = '';
  document.getElementById('carteResultats').hidden = false;
  document.getElementById('rassuranceAntivirus').hidden = !aTraiter.some(
    f => extension(f.nom) !== '.docx');
  document.getElementById('toutTelecharger').hidden = true;
  document.getElementById('bilan').textContent = '';
  document.getElementById('suivi').hidden = false;
  avancer(0, '');
  dessiner();

  lotTotal = aTraiter.length;
  enCoursLot.clear();
  /* Un document, du lancement au bilan. Rend faux si le lot doit s'arreter. */
  const traiterUn = async (f, rang) => {
    f.traitement = 'cours';
    dessiner();
    let bilan;
    try {
      const r = await fetch('/traiter', {method:'POST',
        headers: entetes({'Content-Type':'application/json'}),
        body: JSON.stringify(Object.assign({id: f.id}, options,
          modeRestant ? {revue: modeRestant} : {}))});
      if (!r.ok) throw new Error(await r.text());
      bilan = await suivre((await r.json()).id, f, rang, aTraiter.length);
    } catch (e){
      bilan = {nom: f.nom, dossier: f.dossier, echec: String(e.message || e)};
    }
    if (bilan.interrompu){ f.traitement = ''; return false; }
    f.traitement = gravite(bilan);
    derniers.push(bilan);
    sortie.append(rendre(bilan));
    majTout();
    dessiner();
    return true;
  };

  /* Avec relecture, un document a la fois : chaque relecture apprend a la
     machine des corrections qui valent pour le document suivant, et il faut
     les connaitre avant de le traiter. Le suivant est seulement lu a
     l'avance, pendant la relecture. Sans relecture — d'emblee, ou des que
     l'utilisateur valide la suite sans relecture —, plusieurs documents a la
     fois : le lot va environ deux fois plus vite. */
  let i = 0;
  while (i < aTraiter.length && !interruption){
    if ((modeRestant || options.revue) !== 'aucune'){
      if (i + 1 < aTraiter.length) prelire(aTraiter[i + 1]);
      if (!await traiterUn(aTraiter[i], i + 1)) break;
      i++;
      continue;
    }
    const reste = aTraiter.slice(i);
    let suivant = 0;
    const ouvrier = async () => {
      while (!interruption && suivant < reste.length){
        const k = suivant++;
        if (!await traiterUn(reste[k], i + k + 1)) return;
      }
    };
    await Promise.all(Array.from(
      {length: Math.max(1, Math.min(SIMULTANES, reste.length))}, ouvrier));
    i = aTraiter.length;
  }
  for (const f of aTraiter) if (f.traitement === 'attente' || f.traitement === 'cours') f.traitement = '';
  enCours = false;
  conclure(interruption && derniers.length < aTraiter.length
           ? aTraiter.length : 0);
  interruption = false;
  dessiner();
}
/* Interrompre : le document en cours est abandonne sans rien produire, ceux
   deja termines restent telechargeables, les autres reviennent a « pret ». */
async function interrompre(){
  if (!enCours || interruption) return;
  interruption = true;
  dessiner();
  await signalerInterruption();
}
async function signalerInterruption(){
  await Promise.all([...enCoursLot.keys()].map(async id => {
    if (interruptionSignalee.has(id)) return;
    interruptionSignalee.add(id);
    try {
      await fetch('/interrompre', {method:'POST',
        headers: entetes({'Content-Type':'application/json'}),
        body: JSON.stringify({tache: id})});
    } catch (e){ interruptionSignalee.delete(id); }
  }));
}
/* Lit le document suivant pendant la relecture du courant. Sans reponse
   attendue : un echec veut seulement dire qu'il sera lu le moment venu. */
function prelire(f){
  if (!f.id || extension(f.nom) !== '.pdf') return;
  fetch('/prelire', {method:'POST',
    headers: entetes({'Content-Type':'application/json'}),
    body: JSON.stringify({id: f.id})}).catch(() => {});
}
/* La jauge et la ligne de suivi, pour un document ou pour plusieurs. */
function majSuivi(){
  const actifs = [...enCoursLot.values()];
  const fait = derniers.length + actifs.reduce(
    (s, e) => s + (e.t ? e.t.progression : 0), 0);
  let texte;
  if (actifs.length === 1){
    const e = actifs[0];
    texte = e.f.nom + ' — ' + (lotTotal > 1 ? 'document ' + e.rang + ' sur '
      + lotTotal + ' · ' : '') + (e.t ? e.t.etape : '');
  } else {
    texte = derniers.length + ' sur ' + lotTotal + ' terminé'
      + (derniers.length > 1 ? 's' : '') + ' · en cours : '
      + actifs.map(e => e.f.nom).join(', ');
  }
  avancer(fait / Math.max(1, lotTotal), texte);
}
/* Ce que le lot a dure, de « Caviarder » a la fin. Le temps passe a relire
   est du temps humain : il est donne a part, pour que la duree affichee ne
   laisse pas croire que la machine l'a mis. */
function dureeLot(){
  const total = (performance.now() - debutLot) / 1000;
  const relecture = derniers.reduce((s, b) => s + (b.attente || 0), 0);
  return ' en ' + chrono(total)
    + (relecture >= 1 ? ' (dont ' + chrono(relecture) + ' de relecture)' : '');
}
function conclure(interrompuSur){
  const compte = k => derniers.filter(b => gravite(b) === k).length;
  const parts = [];
  if (compte('ok')) parts.push(compte('ok') + ' sans alerte');
  if (compte('attention')) parts.push(compte('attention') + ' à regarder');
  if (compte('alerte')) parts.push(compte('alerte') + ' à vérifier');
  if (compte('echec')) parts.push(pluriel(compte('echec'), 'échec', 'échecs'));
  const total = pluriel(derniers.length, 'document', 'documents');
  if (interrompuSur){
    document.getElementById('bilan').textContent =
      derniers.length ? total + ' : ' + parts.join(' · ') : '';
    avancer(derniers.length / interrompuSur, 'Interrompu — '
      + (derniers.length ? derniers.length + ' sur ' + interrompuSur
         + ' traité' + (derniers.length > 1 ? 's' : '') + dureeLot() + '.'
         : 'aucun document traité.'));
    majTout();
    return;
  }
  document.getElementById('bilan').textContent = total + ' : ' + parts.join(' · ');
  avancer(1, 'Terminé — ' + total + ' traité' + (derniers.length > 1 ? 's' : '')
    + dureeLot() + '.');
  majTout();
}
/* L'archive est proposee des qu'il y a deux fichiers a rassembler, sans
   attendre la fin du lot : sur une centaine de documents, personne ne veut
   les prendre un a un en attendant le dernier. Elle contient ce qui est deja
   produit, et le dit. */
function majTout(){
  const produits = derniers.reduce((s, b) => s + (b.produits || []).length, 0);
  const bouton = document.getElementById('toutTelecharger');
  bouton.hidden = produits < 2;
  if (!archiveOccupee)
    document.getElementById('libelleTout').textContent =
      'Tout télécharger (.zip) · ' + pluriel(produits, 'fichier', 'fichiers');
  bouton.classList.toggle('primaire', !enCours && !telecharge);
  // Une fois les fichiers pris, la suite naturelle est une nouvelle demande.
  const neuve = document.getElementById('nouvelleDemande');
  neuve.hidden = enCours || !derniers.length;
  neuve.classList.toggle('primaire', telecharge);
  document.getElementById('optionPlat').hidden = produits < 2
    || !derniers.some(b => b.dossier);
}
async function suivre(idTache, f, rang, total){
  const suivi = {f: f, rang: rang, t: null};
  enCoursLot.set(idTache, suivi);
  try {
    for (;;){
      if (interruption) await signalerInterruption();
      const r = await fetch('/tache?id=' + encodeURIComponent(idTache),
                            {headers: entetes()});
      if (!r.ok) throw new Error(await r.text());
      const t = await r.json();
      suivi.t = t;
      majSuivi();
      if (t.etat === 'interrompu') return {interrompu: true};
      if (t.etat === 'revue' && !interruption){
        await relire(idTache, f, rang, total); continue;
      }
      if (t.etat === 'fini') return t.resultat;
      if (t.etat === 'echec') return {nom: f.nom, dossier: f.dossier, echec: t.erreur};
      await pause(600);
    }
  } finally {
    enCoursLot.delete(idTache);
  }
}

/* --- relecture --------------------------------------------------------- */
let decisions = {};
let tacheRelue = null;
/* Ce que l'utilisateur a decide pour la suite du lot, en pleine relecture :
   « sans relecture » ou « seulement les doutes ». Vide : on suit le reglage. */
let modeRestant = '';
let rangDoute = -1;

/* Les passages de la page courante, tels que le serveur les a rendus. */
let passages = [];
let vueActive = 'page';
const ORIGINES = ['regle', 'modele', 'propage', 'humain'];
const NOM_ORIGINE = {regle: 'format reconnu', modele: 'doute',
                     propage: 'déjà trouvé', humain: 'ajouté',
                     juger: 'à juger', bloc: 'zone'};
const AIDE_JUGER = 'peut aider à reconnaître la personne';
function classeOrigine(o){
  return ORIGINES.includes(o) ? o : 'modele';
}
/* La couleur d'un passage : sa provenance, sauf pour un passage a juger, qui
   se distingue par ce qu'il est — un detail qui peut faire reconnaitre. */
function classeDe(q){
  if (q.zone) return 'bloc';
  return q.juger ? 'juger' : classeOrigine(q.origine);
}
function estRejete(q){
  const d = decisions[q.id];
  return d ? d.etat === 'rejetee' : !q.masque;
}
/* Un passage se decide en un seul endroit : le texte, la page et la liste
   montrent la meme chose, et se mettent a jour ensemble. */
/* Les gestes de la relecture, du plus ancien au plus recent, pour pouvoir les
   defaire un a un. Un ajout se defait en le laissant visible : le serveur le
   garde, mais il ne sera pas masque. */
let historique = [];
function majAnnuler(){
  const b = document.getElementById('annuler');
  b.disabled = historique.length === 0;
  b.textContent = historique.length
    ? 'Annuler (' + historique.length + ')' : 'Annuler';
}
function annuler(){
  const dernier = historique.pop();
  if (!dernier) return;
  if (dernier.ids) for (const id of dernier.ids) fixer(id, true, false);
  else fixer(dernier.id, dernier.avant, false);
  majAnnuler();
}
async function accueillirAjout(reponse){
  const connus = new Set(passages.map(q => q.id));
  peindre(await reponse.json());
  const ids = passages.filter(q => !connus.has(q.id)).map(q => q.id);
  if (ids.length){ historique.push({ids: ids}); majAnnuler(); }
}
function fixer(id, rejete, noter = true){
  if (noter){
    const q = passages.find(p => p.id === id);
    historique.push({id: id, avant: q ? estRejete(q) : !rejete});
    majAnnuler();
  }
  decisions[id] = {etat: rejete ? 'rejetee' : 'retenue', motif: ''};
  for (const e of document.querySelectorAll('#relecture [data-id]')){
    if (e.dataset.id !== id) continue;
    e.classList.toggle('rejetee', rejete);
    const c = e.querySelector('input[type=checkbox]');
    if (c) c.checked = !rejete;
    const etat = e.querySelector('.etat-passage');
    if (etat) etat.textContent = rejete ? 'reste lisible' : 'masqué';
  }
  majCompte();
  majDoutes();
}
function bascule(m){
  fixer(m.dataset.id, !m.classList.contains('rejetee'));
}
function majCompte(){
  const rejets = passages.filter(estRejete).length;
  document.getElementById('compte').textContent =
    pluriel(passages.length - rejets, 'passage masqué', 'passages masqués')
    + ' · ' + pluriel(rejets, 'laissé visible', 'laissés visibles');
}
function info(message){
  const p = document.getElementById('infoRelecture');
  p.textContent = message;
  p.hidden = !message;
  clearTimeout(info.delai);
  if (message) info.delai = setTimeout(() => info(''), 5000);
}
function blocPage(p){
  const bloc = el('div', 'page-image');
  bloc.dataset.partie = p.partie;
  const cadre = el('div', 'cadre-page');
  cadre.style.aspectRatio = p.apercu.largeur + ' / ' + p.apercu.hauteur;
  const img = el('img');
  img.alt = 'Aperçu de la ' + p.partie;
  img.draggable = false;
  img.src = '/revue/apercu?jeton=' + encodeURIComponent(JETON)
    + '&id=' + encodeURIComponent(tacheRelue)
    + '&partie=' + encodeURIComponent(p.partie);
  cadre.append(img);
  for (const q of p.passages){
    for (const [x0, y0, x1, y1] of (p.boites[q.id] || [])){
      const z = el('div', 'zone ' + classeDe(q));
      z.dataset.id = q.id;
      z.title = (q.zone ? q.etiquette + ' : ' : '') + q.texte.trim() + ' · '
        + (q.zone ? 'cliquez pour la laisser lisible'
          : q.juger ? AIDE_JUGER : NOM_ORIGINE[classeOrigine(q.origine)]);
      z.style.left = (x0 * 100) + '%';
      z.style.top = (y0 * 100) + '%';
      z.style.width = ((x1 - x0) * 100) + '%';
      z.style.height = ((y1 - y0) * 100) + '%';
      z.classList.toggle('rejetee', estRejete(q));
      z.addEventListener('click', () => bascule(z));
      cadre.append(z);
    }
  }
  brancherCadre(cadre, p.partie);
  bloc.append(el('h3', '', p.partie + ' — '
    + pluriel(p.detections, 'passage trouvé', 'passages trouvés')), cadre);
  return bloc;
}
/* Tracer un cadre sur la page masque les mots qu'il entoure : c'est le geste
   naturel pour un oubli, et il ne depend pas du texte lu par l'OCR. */
function brancherCadre(cadre, partie){
  cadre.addEventListener('pointerdown', e => {
    if (e.pointerType === 'touch' || e.button !== 0 || e.target.closest('.zone'))
      return;
    const r = cadre.getBoundingClientRect();
    const borne = (v, max) => Math.min(Math.max(v, 0), max);
    const x0 = borne(e.clientX - r.left, r.width);
    const y0 = borne(e.clientY - r.top, r.height);
    const trace = el('div', 'cadre-trace');
    cadre.append(trace);
    cadre.setPointerCapture(e.pointerId);
    e.preventDefault();
    const point = ev => [borne(ev.clientX - r.left, r.width),
                         borne(ev.clientY - r.top, r.height)];
    const dessiner = ev => {
      const [x, y] = point(ev);
      trace.style.left = Math.min(x0, x) + 'px';
      trace.style.top = Math.min(y0, y) + 'px';
      trace.style.width = Math.abs(x - x0) + 'px';
      trace.style.height = Math.abs(y - y0) + 'px';
    };
    const fin = async ev => {
      cadre.removeEventListener('pointermove', dessiner);
      cadre.removeEventListener('pointerup', fin);
      cadre.removeEventListener('pointercancel', fin);
      trace.remove();
      const [x, y] = point(ev);
      if (ev.type === 'pointercancel'
          || (Math.abs(x - x0) < 8 && Math.abs(y - y0) < 8)) return;
      const rep = await fetch('/revue/zone', {method:'POST',
        headers: entetes({'Content-Type':'application/json'}),
        body: JSON.stringify({tache: tacheRelue, partie: partie,
          x0: x0 / r.width, y0: y0 / r.height,
          x1: x / r.width, y1: y / r.height})});
      if (rep.ok) await accueillirAjout(rep);
      else info(await rep.text());
    };
    cadre.addEventListener('pointermove', dessiner);
    cadre.addEventListener('pointerup', fin);
    cadre.addEventListener('pointercancel', fin);
  });
}
/* Ce que devient un passage masque, selon le format choisi plus haut et le
   type du document : il n'est pas « noirci » dans un texte exporte. */
function effetDuMasque(nom){
  const docx = (nom || '').toLowerCase().endsWith('.docx');
  const pseudo = 'remplacé par un pseudonyme ([PATIENT_1], [DATE_1]…)';
  const document_ = docx ? pseudo + ' dans le .docx' : 'noirci dans le PDF';
  const format = valeur('format');
  if (format === 'texte') return pseudo + ' dans le texte';
  if (format === 'les-deux') return document_ + ', ' + (docx ? 'et dans le texte'
    : pseudo.replace('remplacé', 'et remplacé') + ' dans le texte');
  return document_;
}
function peindre(vue){
  const hote = document.getElementById('parties');
  const pages = document.getElementById('pages');
  hote.innerHTML = '';
  pages.innerHTML = '';
  passages = [];
  for (const p of vue.parties){
    const bloc = el('div', 'partie');
    const titre = el('h3', '', p.partie + ' — '
      + pluriel(p.detections, 'passage trouvé', 'passages trouvés'));
    const corps = el('div', 'texte');
    corps.dataset.partie = p.partie;
    corps.innerHTML = p.html;
    bloc.append(titre, corps);
    hote.append(bloc);
    for (const q of p.passages)
      passages.push(Object.assign({partie: p.partie}, q));
    if (p.apercu) pages.append(blocPage(p));
  }
  for (const m of hote.querySelectorAll('mark')){
    m.removeAttribute('onclick');
    m.addEventListener('click', () => bascule(m));
    // Dans les deux sens : un passage a juger arrive lisible, et un clic
    // l'a peut-etre masque avant que la page soit repeinte.
    if (decisions[m.dataset.id])
      m.classList.toggle('rejetee', decisions[m.dataset.id].etat === 'rejetee');
  }
  const avecPages = vue.parties.some(p => p.apercu);
  document.getElementById('vues').hidden = !avecPages;
  document.getElementById('aideCadre').hidden = !avecPages;
  choisirVue(avecPages ? vueActive : 'texte');
  dessinerListe(vue.parties.length > 1);
  for (const s of document.querySelectorAll('#legende [data-origine]'))
    s.hidden = !passages.some(q => classeDe(q) === s.dataset.origine);
  majCompte();
  majDoutes();
}
function choisirVue(nom){
  const pages = document.getElementById('pages');
  const texte = document.getElementById('parties');
  const possible = pages.childElementCount > 0;
  if (nom === 'page' && !possible) nom = 'texte';
  vueActive = nom;
  pages.hidden = nom !== 'page';
  texte.hidden = nom !== 'texte';
  for (const b of document.querySelectorAll('#vues button'))
    b.setAttribute('aria-pressed', String(b.dataset.vue === nom));
  info('');
}
/* La liste : les doutes d'abord, puis les passages a juger, puis le reste
   dans l'ordre du document. */
function dessinerListe(plusieursParties){
  const ul = document.getElementById('passages');
  ul.replaceChildren();
  const doutesListe = passages.filter(q => classeDe(q) === 'modele');
  const aJuger = passages.filter(q => classeDe(q) === 'juger');
  const autres = passages.filter(q => !['modele', 'juger'].includes(classeDe(q)));
  const groupes = [doutesListe, aJuger, autres].filter(l => l.length).length;
  document.getElementById('nbPassages').textContent = '· ' + passages.length;
  if (!passages.length){
    ul.append(el('li', 'vide-liste', "Rien n'a été trouvé. Sélectionnez un "
      + "texte pour en masquer."));
    return;
  }
  const groupe = (titre, liste, classe) => {
    if (!liste.length) return;
    if (groupes > 1)
      ul.append(el('li', 'groupe ' + classe, titre + ' · ' + liste.length));
    for (const q of liste) ul.append(ligneDePassage(q, plusieursParties));
  };
  groupe('À vérifier', doutesListe, 'doutes');
  groupe('À juger : ' + AIDE_JUGER, aJuger, 'juger');
  groupe('Autres passages', autres, 'autres');
}
function ligneDePassage(q, plusieursParties){
  const o = classeDe(q);
  const rejete = estRejete(q);
  const li = el('li', 'passage ' + o);
  li.dataset.id = q.id;
  li.classList.toggle('rejetee', rejete);
  const c = el('input');
  c.type = 'checkbox';
  c.checked = !rejete;
  c.title = q.juger
    ? 'Ce détail ' + AIDE_JUGER + '. Cochez pour le masquer dans le '
      + 'document final.'
    : 'Coché : masqué dans le document final. Décochez pour le '
      + 'laisser lisible.';
  c.setAttribute('aria-label', 'Masquer « ' + q.texte.trim() + ' »');
  c.addEventListener('change', () => fixer(q.id, !c.checked));
  const t = el('button', 'passage-texte', q.texte.trim() || '(vide)');
  t.type = 'button';
  t.title = 'Voir ce passage dans le document';
  t.addEventListener('click', () => viser(q.id));
  li.append(c, t, el('span', 'passage-meta',
    (q.zone ? q.etiquette : NOM_ORIGINE[o])
      + (plusieursParties ? ' · ' + q.partie : '')),
    el('span', 'etat-passage', rejete ? 'reste lisible' : 'masqué'));
  return li;
}
/* Amener le regard sur un passage : dans la page, ou dans le texte si la page
   n'a pas de boite pour lui. */
function viser(id){
  const cibles = () => Array.from(document.querySelectorAll(
      '#parties [data-id], #pages [data-id]'))
    .filter(e => e.dataset.id === id && e.offsetParent !== null);
  let trouvees = cibles();
  if (!trouvees.length && document.getElementById('vues').hidden === false){
    choisirVue(vueActive === 'page' ? 'texte' : 'page');
    trouvees = cibles();
  }
  if (!trouvees.length) return false;
  trouvees[0].scrollIntoView({block:'center', behavior:'smooth'});
  for (const e of trouvees) e.classList.add('vise');
  setTimeout(() => { for (const e of trouvees) e.classList.remove('vise'); }, 1400);
  return true;
}
/* L'orange est ce que le modele a estime sans rien pour le recouper : c'est
   la que l'outil peut se tromper, donc la que l'oeil sert le plus. */
function doutes(){
  return passages.filter(q => classeDe(q) === 'modele');
}
function majDoutes(){
  /* « Ne montrer que les doutes » garde aussi les passages a juger : sans un
     regard, ils resteraient lisibles. */
  for (const bloc of document.querySelectorAll('#parties .partie'))
    bloc.classList.toggle('sans-doute',
      !bloc.querySelector('mark.modele, mark.juger'));
  for (const bloc of document.querySelectorAll('#pages .page-image'))
    bloc.classList.toggle('sans-doute',
      !bloc.querySelector('.zone.modele, .zone.juger'));
  const liste = doutes();
  const n = liste.length;
  const restants = liste.filter(q => !estRejete(q)).length;
  const juger = passages.filter(q => q.juger).length;
  document.getElementById('resumeDoutes').textContent = (n
    ? pluriel(n, 'doute', 'doutes') : 'aucun doute')
    + (juger ? ' · ' + juger + ' à juger' : '');
  const suivant = document.getElementById('douteSuivant');
  suivant.disabled = !n;
  suivant.textContent = n && rangDoute >= 0
    ? 'Doute suivant (' + Math.min(rangDoute + 1, n) + '/' + n + ')'
    : 'Doute suivant';
  document.getElementById('relecture').classList.toggle('solo-doutes',
    document.getElementById('soloDoutes').checked);
  return restants;
}
function douteSuivant(){
  const liste = doutes();
  if (!liste.length) return;
  for (let essai = 0; essai < liste.length; essai++){
    rangDoute = (rangDoute + 1) % liste.length;
    if (viser(liste[rangDoute].id)) break;
  }
  majDoutes();
}
/* La selection courante dans le texte, rognee de ses blancs : un double clic
   emporte souvent l'espace qui suit le mot. */
function selectionTexte(){
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const noeud = sel.anchorNode && (sel.anchorNode.nodeType === 1
    ? sel.anchorNode : sel.anchorNode.parentElement);
  const bloc = noeud && noeud.closest('.texte');
  if (!bloc) return null;
  const plage = sel.getRangeAt(0);
  const brut = sel.toString();
  const tete = brut.length - brut.trimStart().length;
  const utile = brut.trim();
  if (!utile) return null;
  const avant = plage.cloneRange();
  avant.selectNodeContents(bloc);
  avant.setEnd(plage.startContainer, plage.startOffset);
  const debut = avant.toString().length + tete;
  return {bloc: bloc, debut: debut, fin: debut + utile.length, plage: plage};
}
async function masquerSelection(){
  const s = selectionTexte();
  document.getElementById('bulle').hidden = true;
  if (!s){ info("Sélectionnez d'abord le texte à masquer."); return; }
  const r = await fetch('/revue/ajout', {method:'POST',
    headers: entetes({'Content-Type':'application/json'}),
    body: JSON.stringify({tache: tacheRelue, partie: s.bloc.dataset.partie,
                          debut: s.debut, fin: s.fin})});
  window.getSelection().removeAllRanges();
  if (r.ok) await accueillirAjout(r);
  else info("Ajout refusé : " + await r.text());
}
/* La bulle « Masquer » suit la selection : le geste se fait ou l'on regarde. */
function majBulle(){
  const bulle = document.getElementById('bulle');
  const s = document.body.classList.contains('en-relecture')
    ? selectionTexte() : null;
  if (!s){ bulle.hidden = true; return; }
  const r = s.plage.getBoundingClientRect();
  bulle.style.left = Math.min(Math.max(r.left + r.width / 2, 40),
                              window.innerWidth - 40) + 'px';
  bulle.style.top = Math.max(r.top - 8, 44) + 'px';
  bulle.hidden = false;
}
/* Aide repliable : une fois comprise, elle ne reprend plus la place du texte. */
function aideMasquee(){
  try { return localStorage.getItem('caviardoc-aide-relecture') === 'masquee'; }
  catch (e) { return false; }
}
function montrerAide(visible){
  document.getElementById('aideRelecture').hidden = !visible;
  document.getElementById('montrerAide').hidden = visible;
  try {
    if (visible) localStorage.removeItem('caviardoc-aide-relecture');
    else localStorage.setItem('caviardoc-aide-relecture', 'masquee');
  } catch (e) { /* le navigateur refuse le stockage : l'aide reste visible */ }
}
function brancherRelecture(){
  document.getElementById('bulle').addEventListener('mousedown',
    e => e.preventDefault());           // garder la selection au clic
  document.getElementById('bulle').addEventListener('click', masquerSelection);
  document.getElementById('annuler').addEventListener('click', annuler);
  document.getElementById('fermerAide').addEventListener('click',
    () => montrerAide(false));
  document.getElementById('montrerAide').addEventListener('click',
    () => montrerAide(true));
  for (const b of document.querySelectorAll('#vues button'))
    b.addEventListener('click', () => choisirVue(b.dataset.vue));
  const zoneTexte = document.getElementById('parties');
  zoneTexte.addEventListener('mouseup', () => setTimeout(majBulle, 0));
  zoneTexte.addEventListener('keyup', majBulle);
  // Un double clic sur un mot le masque : le plus court des gestes.
  zoneTexte.addEventListener('dblclick', e => {
    if (e.target.closest('mark')) return;
    setTimeout(masquerSelection, 0);
  });
  document.addEventListener('selectionchange', () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) document.getElementById('bulle').hidden = true;
  });
  window.addEventListener('scroll', () => {
    document.getElementById('bulle').hidden = true;
  }, {passive: true});
  document.addEventListener('keydown', e => {
    if (!document.body.classList.contains('en-relecture')) return;
    if (e.target.closest && e.target.closest('textarea, select')) return;
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'z' || e.key === 'Z')){
      e.preventDefault();
      annuler();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest && e.target.closest('input, textarea, select')) return;
    if (e.key === 'm' || e.key === 'M'){
      e.preventDefault();
      masquerSelection();
    } else if (e.key === 'd' || e.key === 'D'){
      e.preventDefault();
      douteSuivant();
    }
  });
}
function relire(idTache, f, rang, total){
  tacheRelue = idTache;
  decisions = {};
  historique = [];
  rangDoute = -1;
  return new Promise(async resolve => {
    const r = await fetch('/revue?id=' + encodeURIComponent(idTache),
                          {headers: entetes()});
    const mode = modeRestant || valeur('relecture');
    document.getElementById('soloDoutes').checked = mode === 'doutes';
    peindre(await r.json());
    document.getElementById('aideRelecture').hidden = aideMasquee();
    document.getElementById('montrerAide').hidden = !aideMasquee();
    document.getElementById('titreRelecture').textContent =
      (f.dossier ? f.dossier + '/' : '') + f.nom;
    document.getElementById('effetMasque').textContent = effetDuMasque(f.nom);
    document.getElementById('rangRelecture').textContent =
      total > 1 ? 'document ' + rang + ' sur ' + total : '';
    document.body.classList.add('en-relecture');
    window.scrollTo(0, 0);

    const valider = document.getElementById('valider');
    const lot = document.getElementById('lotRelecture');
    const confiance = document.getElementById('validerConfiance');
    const seulDoutes = document.getElementById('validerDoutes');
    const reste = total - rang;
    /* Valider ce document, et decider d'un coup du sort des suivants. Les
       decisions deja prises ici valent : « tout valider » ne les efface pas. */
    const terminer = async (suite) => {
      for (const b of [valider, confiance, seulDoutes]) b.disabled = true;
      if (suite) modeRestant = suite;
      await fetch('/revue/valider', {method:'POST',
        headers: entetes({'Content-Type':'application/json'}),
        body: JSON.stringify({tache: idTache, decisions: decisions})});
      document.body.classList.remove('en-relecture');
      document.getElementById('suivi').scrollIntoView({block:'center'});
      chargerMemoire();
      resolve();
    };
    document.getElementById('interrompreRelecture').onclick = async () => {
      if (!confirm("Interrompre l'anonymisation ? Ce document ne sera pas "
                   + "produit, ni ceux qui suivent.")) return;
      for (const b of [valider, confiance, seulDoutes]) b.disabled = true;
      await interrompre();
      document.body.classList.remove('en-relecture');
      document.getElementById('suivi').scrollIntoView({block:'center'});
      resolve();
    };
    valider.disabled = false;
    valider.onclick = () => terminer('');
    valider.textContent = reste > 0
      ? 'Valider et passer au document suivant (' + (rang + 1) + '/' + total + ')'
      : 'Valider et produire';
    /* Avec un seul document derriere, le bouton principal suffit : les
       choix « pour la suite » ne portent que sur plusieurs documents. */
    lot.hidden = reste < 2;
    if (reste > 1){
      const mots = reste + ' documents suivants';
      confiance.disabled = false;
      confiance.textContent = 'Valider, sans relire les ' + mots;
      confiance.onclick = () => terminer('aucune');
      seulDoutes.hidden = mode === 'doutes';
      seulDoutes.disabled = false;
      seulDoutes.textContent = 'Valider, ne relire que les doutes des ' + mots;
      seulDoutes.onclick = () => terminer('doutes');
    }
  });
}

/* --- resultats --------------------------------------------------------- */
const NATURE = {'.pdf': 'PDF', '.docx': 'Word', '.txt': 'Texte'};
function encart(classe, titre, explication){
  const bloc = el('div', classe);
  bloc.append(el('strong', '', titre));
  if (explication) bloc.append(el('p', '', explication));
  return bloc;
}
function rendre(b){
  const niveau = gravite(b);
  const bloc = el('div', 'resultat ' + niveau);
  const ligne = el('div', 'res-ligne');
  const pastille = el('span', 'res-icone');
  pastille.append(icone(niveau === 'ok' ? 'ok'
                        : niveau === 'echec' ? 'retirer' : 'attention'));
  const corps = el('div', 'res-corps');
  const nom = el('div', 'res-nom');
  if (b.dossier) nom.append(el('span', 'res-dossier', b.dossier + '/'));
  nom.append(document.createTextNode(b.nom));
  // Le nom du fichier porte souvent celui du patient : il est anonymisé
  // comme le contenu, et on le dit avant le téléchargement.
  const sansExt = s => s.replace(/[.][^.]*$/, '');
  const renomme = (b.produits || []).find(p => sansExt(p.nom) !== sansExt(b.nom));
  if (renomme)
    nom.append(el('span', 'res-dossier', ' (téléchargé sous le nom anonymisé « '
      + sansExt(renomme.nom) + ' »)'));
  corps.append(nom);
  ligne.append(pastille, corps);
  bloc.append(ligne);
  if (b.echec){
    corps.append(encart('fuite', 'Échec', b.echec));
    return bloc;
  }

  const unite = extension(b.nom) === '.docx' ? ['partie', 'parties'] : ['page', 'pages'];
  const morceaux = [pluriel(b.entites, 'élément masqué', 'éléments masqués'),
                    pluriel(b.unites, unite[0], unite[1]),
                    String(b.duree).replace('.', ',') + ' s'];
  if (b.images_masquees)
    morceaux.push(pluriel(b.images_masquees, 'image recouverte', 'images recouvertes'));
  morceaux.push(b.verifie ? 'résultat vérifié' : 'NON VÉRIFIÉ');
  corps.append(el('div', 'detail', morceaux.join(' · ')));
  for (const cb of (b.codes_barres || []))
    corps.append(el('div', 'detail', cb.lieu + ' : code-barres masqué ('
                    + cb.nombre + ').'));
  if ((b.metadonnees || []).length)
    corps.append(el('div', 'detail', 'Métadonnées retirées : '
                    + b.metadonnees.join(', ') + '.'));
  if (Array.isArray(b.structure_pdf)){
    corps.append(b.structure_pdf.length
      ? el('div', 'detail', 'Le PDF contient aussi : ' + b.structure_pdf.join(', ') + '.')
      : el('div', 'detail', 'Structure du PDF contrôlée : seulement des images de pages, '
           + 'aucun script, lien ni fichier joint.'));
  }

  if (!b.verifie){
    corps.append(encart('fuite', "Le document produit n'a pas pu être vérifié",
      (b.erreur_verification ? b.erreur_verification + '. ' : '')
      + "Contrôlez-le vous-même avant de le transmettre."));
  }
  if (b.sous_seuil){
    corps.append(encart('fuite', "À vérifier : " + b.sous_seuil,
      "La détection de cette machine n'a pas atteint le seuil mesuré : relisez "
      + "ce document avant de le transmettre."));
  }
  if (b.repli_complet){
    corps.append(encart('fuite contexte', "Patient non repéré",
      "Toutes les personnes ont été masquées par précaution, soignants compris."));
  }
  for (const r of (b.reserves || []))
    corps.append(encart('fuite contexte',
      'Page ' + r.page + " : rien n'y a été détecté", r.reserve));
  if (b.objets_retires)
    corps.append(encart('fuite contexte', b.objets_retires
      + ' objet(s) incorporé(s) retiré(s)',
      "Ces contenus (classeur, PDF, document collé) ne peuvent être ni lus ni "
      + "vérifiés : ils ont été retirés du document produit."));
  for (const nom of (b.images_metadonnees || []))
    corps.append(encart('fuite contexte', 'Métadonnées conservées : ' + nom,
      "Ce format d'image ne permet pas de les retirer sans la modifier. "
      + "Avec « Toutes les personnes », l'image serait retirée."));
  for (const im of (b.images_opaques || []))
    corps.append(encart('fuite contexte', 'Image illisible : ' + im.nom,
      "Son contenu n'a pu être ni masqué ni vérifié (" + im.raison
      + "). Avec « Toutes les personnes », elle serait recouverte."));

  const fuites = b.fuites || [];
  const graves = fuites.filter(f => f.identifiante);
  const contexte = fuites.filter(f => !f.identifiante);
  if (graves.length){
    corps.append(blocFuite(graves, false,
      pluriel(graves.length, 'donnée identifiante encore lisible',
              'données identifiantes encore lisibles'),
      "Ce document peut permettre de reconnaître la personne. Ne le diffusez pas en l'état."));
  }
  if (contexte.length){
    corps.append(blocFuite(contexte, true,
      pluriel(contexte.length, 'élément de contexte encore lisible',
              'éléments de contexte encore lisibles'),
      "Seuls, ils ne désignent personne ; recoupés, ils peuvent y aider."));
  }
  const corrigees = b.corrigees || [];
  if (corrigees.length){
    corps.append(blocFuite(corrigees, false,
      pluriel(corrigees.length, 'passage resté lisible, masqué automatiquement',
              'passages restés lisibles, masqués automatiquement'),
      "La vérification du document produit les relisait encore : ils ont été "
      + "noircis là où elle les lisait, puis vérifiés à nouveau.", 'corrige'));
  }

  const liens = el('div', 'res-liens');
  const relu = confirmationDeRelecture(b);
  if (relu.bloc && (b.produits || []).length) corps.append(relu.bloc);
  for (const p of (b.produits || [])){
    const lien = el('a', 'telecharger' + (graves.length ? ' risque' : ''));
    lien.dataset.base = '/telecharger?jeton=' + encodeURIComponent(JETON)
              + '&id=' + encodeURIComponent(p.id);
    lien.href = lien.dataset.base;
    lien.download = p.nom;
    lien.title = p.nom;
    // Avant tout autre gestionnaire : un document non relu ne part pas.
    lien.addEventListener('click', e => {
      if (!relu.accorde()) e.preventDefault();
      else lien.href = lien.dataset.base + relu.suffixe();
    });
    lien.addEventListener('click', e => {
      if (e.defaultPrevented) return;
      telecharge = true;
      majTout();
    });
    lien.append(icone('telecharger'), document.createTextNode(
      (NATURE[extension(p.nom)] || 'Fichier') + ' · ' + humain(p.taille)));
    if (graves.length){
      // On ne bloque pas — c'est son document, et il peut avoir de bonnes
      // raisons. On demande seulement que le geste soit voulu.
      lien.onclick = (e) => {
        if (e.defaultPrevented) return;
        if (!confirm("Ce document contient encore " + graves.length
            + " donnée(s) permettant de reconnaître la personne.\\n\\n"
            + "Télécharger quand même ?")) e.preventDefault();
      };
    }
    liens.append(lien);
    if (extension(p.nom) === '.txt')
      liens.append(boutonCopier(lien, graves, relu));
  }
  if (liens.childElementCount) corps.append(liens);
  return bloc;
}
/* Un document que personne n'a relu ne se livre pas d'un clic : sans
   relecture, ou en relecture des doutes quand il n'en avait aucun, son texte
   n'a ete vu par personne. Or masquer les noms ne le rend pas anonyme : ce
   qu'il raconte peut suffire a reconnaitre la personne. Une case a cocher,
   une fois par document, avant de le copier ou de le telecharger. */
function confirmationDeRelecture(b){
  if (b.relu || b.echec)
    return {bloc: null, accorde: () => true, suffixe: () => ''};
  const bloc = el('label', 'confirmer-relu');
  const c = el('input');
  c.type = 'checkbox';
  bloc.append(c, el('span', '', "J'ai relu ce document : ce qu'il raconte ne "
    + "permet pas de reconnaître la personne."));
  c.checked = Boolean(b.confirme);
  c.addEventListener('change', () => {
    b.confirme = c.checked;
    bloc.classList.remove('a-cocher');
  });
  return {bloc: bloc, suffixe: () => c.checked ? '&confirme=1' : '', accorde: () => {
    if (c.checked) return true;
    bloc.classList.add('a-cocher');
    c.focus();
    return false;
  }};
}
/* Certains outils n'acceptent pas de fichier : le texte s'y colle. Meme
   avertissement qu'au telechargement quand il identifie encore quelqu'un. */
function boutonCopier(lien, graves, relu){
  const bouton = el('button', 'telecharger' + (graves.length ? ' risque' : ''));
  bouton.type = 'button';
  const libelle = document.createTextNode('Copier le texte');
  bouton.append(icone('copier'), libelle);
  bouton.addEventListener('click', async () => {
    if (!relu.accorde()) return;
    if (graves.length && !confirm("Ce texte contient encore " + graves.length
        + " donnée(s) permettant de reconnaître la personne.\\n\\n"
        + "Copier quand même ?")) return;
    try {
      const reponse = await fetch(lien.href);
      if (!reponse.ok) throw new Error(reponse.status);
      await navigator.clipboard.writeText(await reponse.text());
      telecharge = true;
      majTout();
      libelle.textContent = 'Texte copié';
    } catch (e) {
      libelle.textContent = 'Copie impossible : téléchargez le texte';
    }
    setTimeout(() => { libelle.textContent = 'Copier le texte'; }, 2500);
  });
  return bouton;
}
/* Une fuite se lit : quelle chaine, pourquoi elle etait masquee, et ou elle
   subsiste. Sans l'etiquette on ne distingue pas un nom de patient d'un faux
   positif du detecteur ; sans la page on ne peut pas aller verifier. */
function blocFuite(fuites, contexte, titre, explication, classe){
  const bloc = encart('fuite' + (contexte ? ' contexte' : '')
                      + (classe ? ' ' + classe : ''), titre, explication);
  const ul = el('ul');
  for (const f of fuites){
    const li = el('li', '', f.texte);
    const details = [];
    if (f.etiquette) details.push(f.etiquette);
    if (f.pages && f.pages.length) details.push('page ' + f.pages.join(', '));
    if (details.length) li.append(el('span', 'pourquoi', ' — ' + details.join(' · ')));
    if (f.raison) li.append(el('div', 'raison', f.raison));
    ul.append(li);
  }
  bloc.append(ul);
  return bloc;
}
/* Le zip se construit cote serveur, puis le navigateur le recoit : sur une
   centaine de documents, cela prend de longues secondes pendant lesquelles
   rien ne bouge. Le bouton dit donc ou l'on en est. */
function etatArchive(etat){
  const bouton = document.getElementById('toutTelecharger');
  const ico = document.getElementById('icoTout');
  const occupe = etat !== 'repos';
  archiveOccupee = occupe;
  bouton.disabled = occupe;
  bouton.setAttribute('aria-busy', etat === 'prepare' ? 'true' : 'false');
  document.getElementById('aPlat').disabled = occupe;
  ico.className = etat === 'prepare' ? 'tourne'
    : etat === 'lance' ? 'ico ico-ok' : 'ico ico-telecharger';
  if (etat === 'prepare')
    document.getElementById('libelleTout').textContent = "Préparation de l'archive…";
  else if (etat === 'lance')
    document.getElementById('libelleTout').textContent =
      'Téléchargement lancé — voir vos téléchargements';
  else majTout();
}
async function toutTelecharger(){
  if (archiveOccupee) return;
  const ids = derniers.flatMap(b => (b.produits || []).map(p => p.id));
  if (!ids.length) return;
  const risques = derniers.filter(b => (b.fuites || []).some(f => f.identifiante)).length;
  if (risques && !confirm(pluriel(risques, 'document contient', 'documents contiennent')
      + " encore des données permettant de reconnaître la personne.\\n\\n"
      + "Télécharger l'archive quand même ?")) return;
  const aConfirmer = derniers.filter(b => !b.relu && !b.confirme
                                    && (b.produits || []).length);
  const nonRelus = aConfirmer.length;
  if (nonRelus && !confirm(pluriel(nonRelus, "document n'a", "documents n'ont")
      + " pas été relu" + (nonRelus > 1 ? "s" : "") + " dans Caviardoc.\\n\\n"
      + "Confirmez que vous les avez relus : ce qu'un document raconte peut "
      + "suffire à reconnaître la personne.")) return;
  for (const b of aConfirmer) b.confirme = true;
  etatArchive('prepare');
  // Laisse le navigateur peindre l'indicateur avant le travail qui suit.
  await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
  let lance = false;
  try {
    const r = await fetch('/archive', {method:'POST',
      headers: entetes({'Content-Type':'application/json'}),
      body: JSON.stringify({ids: ids, confirme: true,
                            a_plat: !document.getElementById('optionPlat').hidden
                                    && document.getElementById('aPlat').checked})});
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    const a = el('a');
    a.href = '/telecharger?jeton=' + encodeURIComponent(JETON)
           + '&id=' + encodeURIComponent(d.id);
    a.download = d.nom;
    document.body.append(a);
    a.click();
    a.remove();
    lance = true;
    telecharge = true;
  } catch (e){
    alert("Archive impossible : " + (e.message || e));
  }
  if (lance){
    etatArchive('lance');
    await pause(3000);
  }
  etatArchive('repos');
}

/* --- ce que la machine a retenu -------------------------------------- */
async function chargerMemoire(){
  const r = await fetch('/memoire', {headers: entetes()});
  const m = await r.json();
  const hote = document.getElementById('memoire');
  hote.innerHTML = '';

  // Replie, la section doit dire s'il y a quelque chose dedans.
  const total = m.jamais.length + m.toujours.length;
  document.getElementById('resumeMemoire').textContent =
    total ? pluriel(total, 'correction', 'corrections') : "rien pour l'instant";

  if (!total){
    hote.append(el('p', 'aide', "Rien pour l'instant : vos corrections de "
      + "relecture apparaîtront ici."));
    return;
  }
  for (const e of m.jamais){
    const li = el('div', 'souvenir');
    const b = el('button', 'petit', 'Retirer de la liste');
    b.type = 'button';
    b.onclick = () => oublier(e.chaine);
    li.append(el('span', 'nom', "Laisser visible « " + e.chaine + " »"),
              el('span', 'etat ok', 'appliqué'), b);
    hote.append(li);
  }
  /* Un texte ajoute dans cette session se lit ; ceux des sessions
     precedentes n'existent que sous forme d'empreinte, et se comptent. */
  for (const e of m.toujours.filter(e => e.chaine)){
    const li = el('div', 'souvenir');
    const b = el('button', 'petit', 'Retirer de la liste');
    b.type = 'button';
    b.onclick = () => oublier(e.chaine);
    li.append(el('span', 'nom', "Toujours masquer « " + e.chaine + " »"),
              el('span', 'etat ok', 'appliqué'), b);
    hote.append(li);
  }
  const anonymes = m.toujours.filter(e => !e.chaine).length;
  if (anonymes){
    const li = el('div', 'souvenir');
    const b = el('button', 'petit', 'Oublier '
                 + (anonymes > 1 ? 'ces ' + anonymes + ' textes' : 'ce texte'));
    b.type = 'button';
    b.onclick = () => oublier({anonymes: true});
    li.append(el('span', 'nom', pluriel(anonymes, 'texte masqué',
                 'textes masqués') + ' lors de sessions précédentes : '
                 + (anonymes > 1 ? 'ils ne sont pas affichés' : "il n'est pas affiché")
                 + ', car Caviardoc ne garde jamais en clair ce que vous '
                 + 'masquez. ' + (anonymes > 1 ? 'Ils restent appliqués' : 'Il reste appliqué')
                 + ' aux nouveaux documents.'),
              b);
    hote.append(li);
  }
  const tout = el('button', 'petit', 'Supprimer les corrections');
  tout.type = 'button';
  tout.onclick = () => {
    if (confirm('Supprimer toutes les corrections retenues par cette '
        + 'machine (' + total + ') ? Les textes masqués lors de sessions '
        + 'précédentes seront oubliés eux aussi. Cette action est '
        + 'irréversible.')) oublier({tout: true});
  };
  hote.append(tout);
}
async function oublier(cible){
  await fetch('/oublier', {method:'POST',
    headers: entetes({'Content-Type':'application/json'}),
    body: JSON.stringify(typeof cible === 'string' ? {chaine: cible} : cible)});
  chargerMemoire();
}

async function quitter(){
  if (!confirm("Fermer Caviardoc ?\\n\\nVos fichiers d'origine ne sont "
      + "pas touchés. Seules les copies de travail sont effacées, y compris "
      + "les documents caviardés que vous n'avez pas encore téléchargés : "
      + "téléchargez-les avant de quitter.")) return;
  try {
    await fetch('/quitter', {method:'POST', headers: entetes()});
  } catch (e) { /* le serveur s'arrete : la reponse peut ne pas arriver */ }
  const fin = document.createElement('main');
  fin.className = 'fin';
  const titre = document.createElement('h1');
  titre.textContent = 'Caviardoc est fermé';
  const texte = document.createElement('p');
  texte.textContent = 'Les copies de travail ont été effacées. Vous pouvez '
    + 'fermer cet onglet.';
  fin.append(titre, texte);
  document.body.replaceChildren(fin);
  window.close();
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('quitter').onclick = quitter;
  const zone = document.getElementById('zone');
  const choix = document.getElementById('choix');
  const choixDossier = document.getElementById('choixDossier');
  zone.addEventListener('click', e => {
    if (!e.target.closest('button, input')) choix.click();
  });
  zone.addEventListener('keydown', e => {
    if (e.target === zone && (e.key === 'Enter' || e.key === ' ')){
      e.preventDefault(); choix.click();
    }
  });
  document.getElementById('choisirFichiers').addEventListener('click', () => choix.click());
  document.getElementById('choisirDossier').addEventListener('click', () => choixDossier.click());
  choix.addEventListener('change', () => {
    ajouter([...choix.files].map(f => ({fichier: f, dossier: ''})));
    choix.value = '';
  });
  choixDossier.addEventListener('change', () => {
    ajouter([...choixDossier.files].map(f => {
      const morceaux = (f.webkitRelativePath || f.name).split('/');
      morceaux.pop();
      return {fichier: f, dossier: morceaux.join('/')};
    }));
    choixDossier.value = '';
  });

  // Toute la page accepte le depot. Ailleurs que sur la zone, le navigateur
  // ouvrirait le fichier a la place de l'interface — et la session avec.
  let finSurvol = null;
  document.addEventListener('dragover', e => {
    e.preventDefault();
    if (!surfaceActive()){ e.dataTransfer.dropEffect = 'none'; return; }
    e.dataTransfer.dropEffect = 'copy';
    zone.classList.add('survol');
    clearTimeout(finSurvol);
    finSurvol = setTimeout(() => zone.classList.remove('survol'), 150);
  });
  document.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('survol');
    if (surfaceActive()) deposerTransfert(e.dataTransfer);
  });

  document.getElementById('lancer').addEventListener('click', lancer);
  document.getElementById('interrompre').addEventListener('click', interrompre);
  document.getElementById('vider').addEventListener('click', toutRetirer);
  document.getElementById('toutTelecharger').addEventListener('click', toutTelecharger);
  document.getElementById('nouvelleDemande').addEventListener('click', nouvelleDemande);
  document.getElementById('douteSuivant').addEventListener('click', douteSuivant);
  document.getElementById('soloDoutes').addEventListener('change', majDoutes);
  brancherRelecture();
  for (const champ of document.querySelectorAll('#reglages input'))
    champ.addEventListener('change', majResume);
  majResume();
  dessiner();
  chargerMemoire();
});
"""


#: Le script des onglets, et des deux sections que la console ajoute a
#: l'installateur : mise a jour et desinstallation.
_SCRIPT_ONGLETS = """
function choisirOnglet(nom){
  for (const b of document.querySelectorAll('nav.onglets button'))
    b.classList.toggle('actif', b.dataset.cible === nom);
  for (const p of document.querySelectorAll('.panneau'))
    p.classList.toggle('actif', p.id === 'panneau-' + nom);
  try { history.replaceState(null, '', '#' + nom); } catch (e) {}
}

/* --- mise a jour ------------------------------------------------------ */
async function chargerMaj(){
  const hote = document.getElementById('maj');
  try {
    const r = await fetch('/maj', {headers: entetes()});
    const e = await r.json();
    if (e.suivi){
      // Le second temps tourne apres le redemarrage : on le suit jusqu'au bout.
      peindreSuiviMaj(e.suivi);
      peindreVersion(e);
      if (['code', 'relance', 'suite'].includes(e.suivi.etat))
        setTimeout(chargerMaj, 2000);
      else rafraichirVersion();
      return;
    }
    hote.innerHTML = '';
    if (e.erreur){
      hote.append(phrase('aide', e.erreur));
      return;
    }
    peindreVersion(e);
    if (e.empeche){
      hote.append(phrase('aide', e.empeche));
      return;
    }
    if (!e.disponible){
      hote.append(phrase('aide', "Vous avez la dernière version"
        + (e.locale.date ? ', du ' + dateLisible(e.locale.date) : '') + '.'));
      return;
    }
    hote.append(phrase('', "Une nouvelle version est disponible."));
    if (e.changements && e.changements.length){
      const ul = document.createElement('ul');
      ul.className = 'aide';
      // « 1a2b3c4  Message » : seul le message parle a l'utilisateur.
      for (const c of e.changements.slice(0, 8)){
        const li = document.createElement('li');
        li.textContent = c.replace(/^[0-9a-f]+[ ]+/, '');
        ul.append(li);
      }
      hote.append(ul);
    }
    const b = document.createElement('button');
    b.className = 'primaire';
    b.textContent = "Mettre à jour";
    b.onclick = () => appliquerMaj(b);
    hote.append(b);
  } catch (err) {
    hote.innerHTML = '';
    hote.append(phrase('aide', "Vérification impossible : " + err));
  }
}
/* La version installee, a cote de l'auteur ; et, s'il y en a une plus
   recente, une pastille qui la telecharge et l'installe depuis la section
   Mise a jour, ou se voit l'avancement. */
const ETAPES_MAJ = {code: [1, "Téléchargement"], relance: [2, "Redémarrage"],
                    suite: [3, "Bibliothèques et nouvelle mesure"]};
function peindreVersion(e){
  const hote = document.getElementById('version');
  if (!hote) return;
  const enCours = e.suivi && ETAPES_MAJ[e.suivi.etat];
  if (!enCours && !e.locale) return;
  hote.innerHTML = '';
  if (e.locale)
    hote.append(' · version ' + (e.locale.date ? 'du ' + dateLisible(e.locale.date)
                                               : e.locale.reference));
  if (!enCours && !e.disponible) return;
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'pastille-maj';
  b.textContent = enCours ? "Mise à jour en cours…" : "Mettre à jour";
  b.title = enCours ? "Voir l'avancement"
                    : "Télécharge et installe la nouvelle version";
  b.onclick = () => {
    choisirOnglet('inst');
    document.getElementById('maj').scrollIntoView({block: 'center'});
    const lancer = document.querySelector('#maj button.primaire');
    if (!enCours && lancer && !lancer.disabled) appliquerMaj(lancer);
  };
  hote.append(b);
}
/* Le suivi ne dit pas la version : une fois la mise a jour finie, on la
   redemande, sans toucher au compte rendu affiche dans la section. */
async function rafraichirVersion(){
  try {
    peindreVersion(await (await fetch('/maj', {headers: entetes()})).json());
  } catch (err) {}
}
function peindreSuiviMaj(s){
  const hote = document.getElementById('maj');
  hote.innerHTML = '';
  const enCours = ['code', 'relance', 'suite'].includes(s.etat);
  hote.append(phrase(s.etat === 'echec' ? 'fuite' : '', s.message));
  if (enCours || s.etat === 'fini'){
    // Le serveur ne chiffre pas l'avancement : la jauge avance par etape,
    // et ondule tant que l'etape tourne.
    const [n, nom] = ETAPES_MAJ[s.etat] || [3, "Terminé"];
    const j = document.createElement('div');
    j.className = 'jauge-maj' + (enCours ? ' en-cours' : '');
    const barre = document.createElement('div');
    barre.style.width = (enCours ? (n - 1) / 3 * 100 + 100 / 6 : 100) + '%';
    j.append(barre);
    hote.append(j, phrase('aide', enCours ? 'Étape ' + n + ' sur 3 : ' + nom
                                          : nom));
  }
  if (enCours){
    hote.append(phrase('aide', "Laissez cette page ouverte."));
    for (const l of s.dernieres || []) hote.append(phrase('aide', l));
    return;
  }
  const c = s.compte || {};
  if (s.etat === 'fini' && c.applique){
    hote.append(phrase('aide', 'Version ' + c.de + ' → ' + c.vers + '.'));
    if (c.rappel_apres != null)
      hote.append(phrase('aide', 'Nouvelle mesure : '
        + c.rappel_apres.toFixed(1).replace('.', ',') + ' % des données de '
        + 'test repérées.'));
  }
  if (c.regression)
    hote.append(phrase('fuite', "Attention : sur cette machine, le modèle "
      + "retrouve désormais moins de données qu'avant la mise à jour ("
      + c.rappel_avant.toFixed(1) + ' % → ' + c.rappel_apres.toFixed(1) + ' %).'));
}
async function appliquerMaj(bouton){
  bouton.disabled = true;
  bouton.textContent = "Mise à jour…";
  const hote = document.getElementById('maj');
  try {
    const r = await fetch('/maj', {method:'POST', headers: entetes()});
    if (!r.ok) throw new Error(await r.text());
  } catch (err) {
    // Sans cela le bouton restait grise sur « Mise a jour… », sans rien dire.
    hote.innerHTML = '';
    hote.append(phrase('fuite', "La mise à jour n'a pas démarré : "
      + (err.message || err)));
    return;
  }
  // Sans attendre le premier releve : la jauge doit paraitre tout de suite.
  const depart = {etat: 'code', message: "Téléchargement de la nouvelle version…"};
  peindreSuiviMaj(depart);
  peindreVersion({suivi: depart});
  // Le serveur redemarre entre le code et les bibliotheques. On le suit :
  // une requete qui echoue, ou un etat que seul le nouveau processus connait,
  // disent qu'il est revenu — la page se recharge alors, avec le nouveau code.
  // On ne recharge qu'une fois le serveur revenu : recharger pendant la
  // coupure laisserait le navigateur sur une page d'erreur.
  let coupe = false, annonce = false;
  for (let i = 0; i < 2400; i++){
    await new Promise(r => setTimeout(r, 1500));
    let e;
    try {
      e = await (await fetch('/maj', {headers: entetes()})).json();
    } catch (err) {
      coupe = true;
      continue;
    }
    const etat = e.suivi ? e.suivi.etat : '';
    if (coupe || etat === 'suite' || (annonce && etat !== 'relance')){
      location.reload();
      return;
    }
    if (etat === 'relance') annonce = true;
    if (e.suivi){ peindreSuiviMaj(e.suivi); peindreVersion(e); }
    if (!['code', 'relance'].includes(etat)){ rafraichirVersion(); return; }
  }
}

/* --- desinstallation -------------------------------------------------- */
async function chargerDesinstallation(){
  const hote = document.getElementById('desinstallation');
  const r = await fetch('/desinstallation', {headers: entetes()});
  const d = await r.json();
  hote.innerHTML = '';
  if (!d.elements.length){
    hote.append(phrase('aide', "Rien à supprimer sur cet ordinateur."));
  } else {
    const total = d.elements.reduce((s, e) => s + e.octets, 0);
    const b = document.createElement('button');
    b.textContent = "Tout supprimer";
    b.onclick = () => supprimer(b);
    hote.append(b);
    hote.append(phrase('aide', "Libère " + humain(total) + ". Retire tout ce "
      + "que l'installation a ajouté sur cet ordinateur. Vos documents ne "
      + "sont jamais touchés."));
  }
  if (d.deja_la && d.deja_la.length){
    const p = phrase('aide', "Déjà présents sur cet ordinateur avant Caviardoc"
      + ", donc laissés en place : "
      + d.deja_la.map(x => x.nom).join(', ') + ". Ils ne font pas partie "
      + "de ce qui a été installé, et d'autres programmes s'en servent "
      + "peut-être.");
    p.title = d.deja_la.map(x => x.nom + ' : ' + x.chemin).join('\\n');
    hote.append(p);
  }
  if (d.dehors.length){
    hote.append(phrase('intitule-dehors', 'À faire vous-même, si vous le souhaitez :'));
    // Le chemin en clair, et selectionnable d'un clic : c'est lui qu'on colle
    // dans le Finder (Aller au dossier) ou l'Explorateur.
    const ul = document.createElement('ul');
    ul.className = 'a-supprimer dehors';
    for (const x of d.dehors){
      const li = document.createElement('li');
      const chemin = phrase('chemin', x.chemin);
      chemin.title = 'Cliquez pour sélectionner, puis copiez';
      const p = phrase('aide', x.pourquoi + (x.commande ? ' ' : ''));
      if (x.commande){
        const code = document.createElement('code');
        code.textContent = x.commande;
        p.append(code);
      }
      li.append(phrase('', x.titre + ' — ' + humain(x.octets)), chemin, p);
      ul.append(li);
    }
    hote.append(ul);
  }
}
async function supprimer(bouton){
  if (!confirm("Tout supprimer ? Le modèle, les bibliothèques et les "
      + "corrections retenues seront effacés ; il faudra tout réinstaller "
      + "pour utiliser à nouveau Caviardoc.\\n\\nVos documents et le "
      + "dossier du programme ne sont pas touchés.")) return;
  bouton.disabled = true;
  const hote = document.getElementById('desinstallation');
  const depot = (await (await fetch('/desinstallation',
    {headers: entetes()})).json()).depot || '';
  const fin = (echecs, ferme, dossier) => {
    hote.innerHTML = '';
    const reste = dossier ? "le dossier « " + dossier + " »"
                          : "le dossier caviardoc";
    hote.append(phrase(echecs.length ? 'fuite' : '',
      echecs.length ? echecs.join(' ; ')
        : ferme ? "La suppression se termine en arrière-plan. Fermez cette "
                  + "page, puis mettez " + reste + " à la corbeille."
                : "Tout est désinstallé, sauf " + reste + " lui-même, que "
                  + "nous vous laissons mettre à la corbeille."));
  };
  // La jauge : pleine largeur de la ligne, un element apres l'autre ; sans
  // pourcentage (bande animee) tant qu'on ne sait pas.
  hote.innerHTML = '';
  const jauge = document.createElement('div');
  jauge.className = 'jauge-maj en-cours';
  const barre = document.createElement('div');
  barre.style.width = '100%';
  jauge.append(barre);
  const ligne = phrase('aide', "Suppression en cours…");
  hote.append(jauge, ligne);
  const r = await fetch('/desinstallation', {method:'POST', headers: entetes()});
  // 409 : une suppression tourne deja (double clic, autre onglet) — on la suit.
  const d = r.status === 409 ? {lance: true} : await r.json();
  if (!d.lance) return fin(d.echecs || [], !!d.ferme, d.depot || depot);
  let ratees = 0;
  for (;;){
    await new Promise(ok => setTimeout(ok, 500));
    let v;
    try { v = await (await fetch('/desinstallation/suivi',
      {headers: entetes()})).json(); ratees = 0; }
    catch (e) {
      // Le serveur ne repond plus : on n'attend pas indefiniment.
      if (++ratees >= 20) return fin([], true, depot);
      continue;
    }
    if (v.etat === 'fini') return fin(v.echecs || [], false, depot);
    if (v.total){
      jauge.classList.remove('en-cours');
      barre.style.width = (v.fait / v.total * 100) + '%';
      ligne.textContent = "Suppression en cours… " + (v.courant || '')
        + " (" + v.fait + " / " + v.total + ")";
    }
  }
}

/* --- modele de detection ----------------------------------------------- */
const AVIS = {
  conseille: ['Conseillé', 'ok'],
  utilisable: ['Utilisable', 'ok'],
  juste: ['Mémoire juste', 'attention'],
  deconseille: ['Déconseillé', 'mal'],
};
async function chargerModeles(){
  const hote = document.getElementById('modeles');
  if (!hote) return;
  let v;
  try {
    const r = await fetch('/modeles', {headers: entetes()});
    v = await r.json();
  } catch (e) { return; }
  hote.innerHTML = '';
  const m = v.machine;
  const mat = m.materiel || {};
  hote.append(phrase('machine', 'Cet ordinateur : '
    + (mat.libelle ? mat.libelle + '. ' : '')
    + (m.ram ? humain(m.ram) + ' de mémoire' : 'mémoire inconnue')
    + ', ' + humain(m.disque_libre) + ' libres sur le disque.'
    + ((mat.gpus || []).length
       ? ' La carte graphique n\\u2019est pas utilisée pour l\\u2019instant : '
         + 'le modèle conseillé est choisi pour le processeur.' : '')));
  // Un ordinateur trop juste est dit avant tout telechargement.
  const comp = m.compatibilite || {};
  if (comp.niveau && comp.niveau !== 'ok'){
    const alerte = phrase('machine ' + (comp.niveau === 'incompatible' ? 'mal' : 'attention'),
      (comp.bloquant
        ? 'Caviardoc ne peut pas s\\u2019installer sur cet ordinateur. '
        : comp.niveau === 'incompatible'
          ? 'Cet ordinateur n\\u2019est pas assez puissant pour Caviardoc. '
          : 'Cet ordinateur est juste. ') + (comp.raisons || []).join(' '));
    alerte.setAttribute('role', 'alert');
    hote.append(alerte);
  }
  const prop = v.proposition;
  const reporte = prop && sessionStorage.getItem('modele-plus-tard') === prop.cle;
  if (prop && !reporte) hote.append(avisNouveauModele(prop));
  // La pastille de l'onglet signale la proposition, sans l'imposer ;
  // l'installation incomplete (data-incomplet) est tenue par peindre().
  const pastille = document.getElementById('alerte-inst');
  pastille.dataset.propose = prop && !reporte ? '1' : '';
  pastille.className = pastille.dataset.incomplet || pastille.dataset.propose
    ? 'pastille-inst' : '';
  for (const d of v.modeles) hote.append(carteModele(d));
  // Installation impossible : aucun bouton de telechargement ne doit rester.
  if (comp.bloquant) for (const b of hote.querySelectorAll('button')) b.disabled = true;
}
function avisNouveauModele(p){
  const bloc = document.createElement('div');
  bloc.className = 'machine attention';
  bloc.setAttribute('role', 'status');
  const pourcent = x => x.toFixed(1).replace('.', ',') + ' %';
  bloc.append(phrase('', 'Un modèle de détection plus performant est disponible : '
    + p.libelle + ' (' + humain(p.taille)
    + (p.rappel_reference != null
       ? ', repère ' + pourcent(p.rappel_reference) + ' des données de test' : '')
    + '). ' + (p.nouveautes || '')));
  const actions = document.createElement('div');
  actions.className = 'modele-actions';
  const bouton = (texte, f) => {
    const b = document.createElement('button');
    b.className = 'petit';
    b.textContent = texte;
    b.onclick = () => f(b);
    actions.append(b);
  };
  bouton('Installer', b => agirModele('installer', {cle: p.cle, libelle: p.libelle,
    avis: 'utilisable'}, b));
  bouton('Plus tard', () => {
    sessionStorage.setItem('modele-plus-tard', p.cle);
    chargerModeles();
  });
  bouton('Ne plus proposer', async b => {
    b.disabled = true;
    await fetch('/modeles/refuser', {method:'POST',
      headers: entetes({'Content-Type':'application/json'}),
      body: JSON.stringify({cle: p.cle})});
    chargerModeles();
  });
  bloc.append(actions);
  return bloc;
}
function carteModele(d){
  const carte = document.createElement('div');
  carte.className = 'modele' + (d.actif ? ' retenu' : '');
  const tete = document.createElement('div');
  tete.className = 'modele-tete';
  tete.append(phrase('modele-nom', d.libelle));
  const [texte, ton] = AVIS[d.avis];
  tete.append(phrase('badge ' + ton, texte));
  if (d.actif) tete.append(phrase('badge service',
    d.installe ? 'En service' : 'Choisi, pas installé'));
  carte.append(tete);

  carte.append(phrase('aide', 'Taille : ' + humain(d.taille)
    + ' · Mémoire : ' + humain(d.memoire) + ' · Licence : ' + d.licence));
  const pourcent = x => x.toFixed(1).replace('.', ',') + ' %';
  carte.append(phrase('aide', d.rappel_machine != null
    ? 'Repère ' + pourcent(d.rappel_machine)
      + ' des données de test sur cet ordinateur.'
    : d.rappel_reference != null
      ? 'Repère ' + pourcent(d.rappel_reference) + ' des données de test.'
      : 'Pas encore mesuré.'));
  if (d.raison) carte.append(phrase('aide', d.raison));
  if (d.remplace_par) carte.append(phrase('aide', 'Remplacé par '
    + d.remplace_par + ' : vous pouvez le supprimer pour libérer de la place.'));

  const actions = document.createElement('div');
  actions.className = 'modele-actions';
  if (d.sur_disque){
    const b = document.createElement('button');
    b.className = 'petit';
    b.textContent = 'Supprimer (libère ' + humain(d.sur_disque) + ')';
    b.onclick = () => supprimerModele(d, b);
    actions.append(b);
  }
  if (!d.installe){
    const b = document.createElement('button');
    b.className = 'petit';
    b.textContent = (d.sur_disque ? 'Terminer l’installation' : 'Installer')
      + ' (' + humain(d.taille) + ')';
    b.onclick = () => agirModele('installer', d, b);
    actions.append(b);
  }
  if (d.installe && !d.actif){
    const b = document.createElement('button');
    b.className = 'petit';
    b.textContent = 'Utiliser ce modèle';
    b.onclick = () => agirModele('utiliser', d, b);
    actions.append(b);
  }
  if (actions.childElementCount) carte.append(actions);
  return carte;
}
async function agirModele(action, d, bouton){
  let confirme = false;
  if (d.avis === 'deconseille' && action !== 'supprimer'){
    if (!confirm(d.libelle + ' est déconseillé.\\n\\n' + d.raison
        + '\\n\\nUn document anonymisé avec ce modèle peut laisser passer '
        + 'des données personnelles. Continuer quand même ?')) return;
    confirme = true;
  }
  bouton.disabled = true;
  const r = await fetch('/modeles/' + action, {method:'POST',
    headers: entetes({'Content-Type':'application/json'}),
    body: JSON.stringify({cle: d.cle, confirme: confirme})});
  if (!r.ok) alert(await r.text());
  chargerModeles();
}
async function supprimerModele(d, bouton){
  const avertissement = d.actif
    ? "C'est le modèle en service : Caviardoc ne pourra plus rien "
      + "caviarder avant de l'avoir réinstallé (" + humain(d.taille)
      + " à télécharger).\\n\\nSupprimer quand même ?"
    : 'Supprimer ' + d.libelle + ' et libérer ' + humain(d.sur_disque) + ' ?';
  if (!confirm(avertissement)) return;
  agirModele('supprimer', d, bouton);
}

function dateLisible(iso){
  const [a, m, j] = iso.split('-');
  return j && m && a ? j + '.' + m + '.' + a : iso;
}
function phrase(classe, texte){
  const d = document.createElement('div');
  if (classe) d.className = classe;
  d.textContent = texte;
  return d;
}

document.addEventListener('DOMContentLoaded', () => {
  for (const b of document.querySelectorAll('nav.onglets button'))
    b.addEventListener('click', () => choisirOnglet(b.dataset.cible));
  const demande = (location.hash || '').replace('#', '');
  choisirOnglet(demande === 'inst' || demande === 'app' ? demande
                                                        : ONGLET_INITIAL);
  if (ONGLET_INITIAL === 'inst'){
    const pastille = document.getElementById('alerte-inst');
    pastille.dataset.incomplet = '1';
    pastille.className = 'pastille-inst';
  }
  chargerMaj();
  chargerDesinstallation();
  chargerModeles();
  // Une installation ou une suppression lancee ailleurs — par « Tout
  // installer », typiquement — doit se voir ici sans recharger la page.
  setInterval(() => {
    if (!document.hidden && document.getElementById('panneau-inst')
          .classList.contains('actif')) chargerModeles();
  }, 4000);
});
"""


#: L'en-tete des deux pages, signe de l'auteur. Le modele n'y figure plus :
#: il a sa section dans l'onglet Installation, avec sa taille et sa mesure.
_ENTETE = """
<header{classe}>
  <div class="marque">
    <span class="logo"><span class="ico ico-bouclier"></span></span>
    <div><h1>Caviardoc</h1>
    <div class="sous">par {auteur}<span id="version"></span></div></div>
  </div>
  <span class="local"><span class="ico ico-cadenas"></span>Tout reste sur cet
  ordinateur</span>
  <button type="button" class="quitter" id="quitter"
          title="Ferme Caviardoc et efface les fichiers déposés">Quitter</button>
</header>
"""


#: Le corps de l'onglet « Caviarder » : deposer, regler, lancer, recuperer.
#: Sorti de la fonction pour que la page seule et la console le partagent.
_CORPS_APP = """
<section>
  <h2 class="titre-section">Documents</h2>
  <div id="zone" tabindex="0" role="button"
       aria-label="Ajouter des documents ou un dossier">
    <div class="zone-icone"><span class="ico ico-deposer"></span></div>
    <p class="zone-titre">Glissez ici vos documents, ou un dossier entier</p>
    <p class="zone-sous" id="zoneSous">PDF et Word (.docx) · les sous-dossiers
    sont parcourus</p>
    <div class="zone-boutons">
      <button type="button" id="choisirFichiers"><span
      class="ico ico-fichier"></span>Choisir des fichiers</button>
      <button type="button" id="choisirDossier"><span
      class="ico ico-dossier"></span>Choisir un dossier</button>
    </div>
    <input type="file" id="choix" multiple accept=".pdf,.docx" hidden>
    <input type="file" id="choixDossier" webkitdirectory multiple hidden>
  </div>
  <div class="bilan-liste" id="bilanListe" hidden>
    <span id="compteListe"></span>
    <button type="button" class="lien" id="vider">Tout retirer</button>
  </div>
  <ul id="liste"></ul>
  <p class="aide" id="ignores" hidden></p>
</section>

<details id="reglages">
  <summary><span class="cle">Réglages</span><span id="resume"></span><span
  class="modifier"></span></summary>
  <div class="groupes">
    <fieldset class="groupe">
      <legend>Ce qui est masqué</legend>
      <div class="reglage">
        <div class="intitule" id="lib-niveau">Noms de personnes</div>
        <div class="segments" role="radiogroup" aria-labelledby="lib-niveau">
          <label><input type="radio" name="niveau" value="complet"
          checked><span>Toutes les personnes</span></label>
          <label><input type="radio" name="niveau" value="patient"><span>Le
          patient seulement</span></label>
        </div>
        <p class="aide" id="aide-niveau"></p>
      </div>
      <div class="reglage">
        <div class="intitule" id="lib-dates">Dates</div>
        <div class="segments" role="radiogroup" aria-labelledby="lib-dates">
          <label><input type="radio" name="dates" value="masquer"
          checked><span>Toutes</span></label>
          <label><input type="radio" name="dates" value="relatives"><span>En
          jours (J+n)</span></label>
          <label><input type="radio" name="dates" value="garder"><span>La date
          de naissance seulement</span></label>
          <label><input type="radio" name="dates"
          value="garder-tout"><span>Aucune</span></label>
        </div>
        <p class="aide" id="aide-dates"></p>
      </div>
      <div class="reglage">
        <div class="interrupteur fixe"><span class="toujours"
        title="Toujours appliqué"><span class="ico ico-ok"></span></span><span>
        <strong>Métadonnées · toujours retirées</strong>
        <small>Ce qu'un document porte sans l'afficher.</small>
        </span></div>
      </div>
    </fieldset>
    <fieldset class="groupe">
      <legend>Ce que vous obtenez</legend>
      <div class="reglage">
        <div class="intitule" id="lib-format">Format</div>
        <div class="segments format" role="radiogroup" aria-labelledby="lib-format">
          <label><input type="radio" name="format" value="pdf"
          checked><span>Document caviardé</span></label>
          <label><input type="radio" name="format" value="texte"><span>Texte
          pseudonymisé</span></label>
          <label><input type="radio" name="format"
          value="les-deux"><span>Les deux</span></label>
        </div>
        <p class="aide" id="aide-format"></p>
      </div>
      <div class="reglage">
        <div class="intitule" id="lib-relecture">Relecture</div>
        <div class="segments" role="radiogroup" aria-labelledby="lib-relecture">
          <label><input type="radio" name="relecture" value="tout"
          checked><span>Tout relire</span></label>
          <label><input type="radio" name="relecture"
          value="doutes"><span>Seulement les doutes</span></label>
          <label><input type="radio" name="relecture"
          value="aucune"><span>Sans relecture</span></label>
        </div>
        <p class="aide" id="aide-relecture"></p>
      </div>
    </fieldset>
  </div>
</details>

<div class="action">
  <button class="primaire grand" id="lancer" disabled>Caviarder</button>
  <button class="grand" id="interrompre" hidden>Interrompre</button>
  <p class="note relire"><span class="ico ico-info"></span><span>Relisez chaque
  document avant de le transmettre : la détection n'est jamais parfaite, et ce
  qui reste lisible (diagnostic, profession, événement) peut suffire à
  reconnaître quelqu'un.</span></p>
</div>
<div id="suivi" hidden>
  <div class="jauge" id="jauge"><div></div></div>
  <div id="progression"></div>
</div>

<section id="carteResultats" hidden>
  <div class="tete-resultats">
    <h2 class="titre-section">Résultats</h2>
    <span id="bilan"></span>
    <button type="button" id="toutTelecharger" hidden><span
    class="ico ico-telecharger" id="icoTout"></span><span
    id="libelleTout" aria-live="polite">Tout télécharger (.zip)</span></button>
    <button type="button" id="nouvelleDemande" hidden
    title="Efface les documents déposés et les résultats, pour repartir de zéro"><span
    class="ico ico-plus"></span>Nouvelle demande</button>
  </div>
  <label class="option-archive" id="optionPlat" hidden><input type="checkbox"
  id="aPlat"><span>Tout dans un seul dossier, sans sous-dossiers<small>Utile
  si votre assistant n'accepte pas qu'on lui dépose une arborescence.</small></span></label>
  <div id="resultats"></div>
  <details class="rassurance" id="rassuranceAntivirus" hidden>
    <summary>Votre antivirus signale un fichier ?</summary>
    <p>C'est une fausse alerte connue. Pour effacer le nom, le texte et les
    métadonnées, Caviardoc reconstruit chaque PDF à partir de l'image de
    ses pages : le fichier ne contient plus ni texte, ni police, ni script, ni
    lien, ni pièce jointe. Cette forme ressemble, de l'extérieur, à celle de
    certains PDF malveillants, et quelques antivirus s'y trompent
    (« TR/PDF.MalwareX », « Heur.PDF »).</p>
    <p>La ligne « Structure du PDF contrôlée » sous chaque document le
    vérifie sur le fichier produit. Aucun document ne quitte votre
    ordinateur.</p>
    <ul>
      <li>Si l'antivirus a mis le fichier en quarantaine, restaurez-le depuis
      son journal, ou retraitez le document.</li>
      <li>Pour ne plus être alerté, excluez de l'analyse le dossier de
      téléchargement, ou déclarez le faux positif à l'éditeur de l'antivirus.</li>
    </ul>
  </details>
</section>

<details class="repli" id="memoire-repli">
  <summary><span class="cle">Ce que cette machine a retenu</span><span
  id="resumeMemoire"></span></summary>
  <div id="memoire"></div>
  <div class="aide">Vos corrections de relecture valent aussi pour les
  documents suivants, sur cet ordinateur seulement.
  <ul>
    <li><strong>Masquer</strong> ou <strong>Laisser visible</strong> un texte
    s'applique tout de suite, aux documents suivants.</li>
    <li><strong>Retirer de la liste</strong> annule cette correction : la
    machine ne s'en souvient plus.</li>
    <li>Les numéros (AVS, IBAN, téléphone, e-mail) restent toujours masqués,
    et les textes que vous masquez ne sont jamais écrits en clair sur le
    disque : ils ne se lisent ici que pendant la session où vous les avez
    ajoutés.</li>
  </ul></div>
</details>
"""

#: La relecture, hors des onglets : elle prend toute la page quand elle
#: s'ouvre, et n'appartient a aucun des deux.
_CORPS_RELECTURE = """
<main id="relecture-hote">
<section id="relecture">
  <div class="relecture-tete">
    <h2>Relecture</h2>
    <span id="titreRelecture"></span>
    <span id="rangRelecture"></span>
    <button type="button" class="lien" id="montrerAide" hidden>Aide</button>
  </div>
  <div class="avis aide-relecture" id="aideRelecture">
    <span><strong>Coché, surligné ou encadré en plein : masqué</strong> —
    <span id="effetMasque">noirci ou remplacé par un pseudonyme</span> dans le
    document final.<br>
    <strong>Décoché, en pointillé : laissé tel quel</strong>, lisible dans le
    document final.<br>
    Regardez surtout les <strong>doutes</strong>, en orange : ce sont les
    passages dont l'outil n'est pas sûr. En violet, <strong>à juger</strong> :
    une profession, un sport ou une activité, une nationalité ou une origine,
    un événement, un âge. Rien de cela ne nomme la personne, mais chacun peut
    aider à la reconnaître ; ils restent lisibles tant que vous ne les masquez
    pas. Pour <strong>laisser un passage
    visible</strong>, cliquez dessus ou décochez-le. Pour <strong>masquer un
    oubli</strong>, sélectionnez le texte<span id="aideCadre"> ou tracez un
    cadre sur la page</span>.<br>
    Raccourcis : <kbd>M</kbd> masquer la sélection · <kbd>D</kbd> doute
    suivant · <kbd>Ctrl</kbd>+<kbd>Z</kbd> annuler.</span>
    <button type="button" class="petit" id="fermerAide">Compris</button>
  </div>
  <div class="outils-relecture">
    <div class="vues segments" id="vues" role="group" aria-label="Affichage"
         hidden>
      <button type="button" data-vue="page" aria-pressed="true">Page</button>
      <button type="button" data-vue="texte" aria-pressed="false">Texte</button>
    </div>
    <label class="case"><input type="checkbox" id="soloDoutes"><span>Ne montrer
    que les doutes</span></label>
    <span id="resumeDoutes"></span>
    <button type="button" class="petit" id="douteSuivant">Doute suivant</button>
    <button type="button" class="petit" id="annuler" disabled
            title="Défait le dernier geste (Ctrl/Cmd+Z)">Annuler</button>
    <div class="legende" id="legende">
      <span class="l-regle" data-origine="regle"
            title="Numéro, date ou adresse reconnus par leur forme">numéro ou
            format reconnu</span>
      <span class="l-modele" data-origine="modele"
            title="Trouvé par le modèle seul, sans rien pour le confirmer">doute
            : le modèle n'est pas sûr</span>
      <span class="l-propage" data-origine="propage"
            title="Un texte déjà trouvé ailleurs dans le document">déjà trouvé
            ailleurs</span>
      <span class="l-humain" data-origine="humain"
            title="Passage que vous avez ajouté">ajouté par vous</span>
      <span class="l-bloc" data-origine="bloc"
            title="Bloc d'en-tête, logo ou tampon noirci d'un seul tenant : décochez pour le laisser lisible">zone entière</span>
      <span class="l-juger" data-origine="juger"
            title="Profession, sport ou activité, nationalité ou origine, événement, âge — laissé lisible tant que vous ne le masquez pas">à
            juger : peut aider à reconnaître la personne</span>
    </div>
  </div>
  <p class="note-solo">Les autres passages restent masqués : ils sont
  seulement moins en évidence, parce qu'une règle ou un nom déjà trouvé ailleurs
  les confirme.</p>
  <p class="info-relecture" id="infoRelecture" role="status" hidden></p>
  <div class="relecture-corps">
    <div class="colonne-document">
      <div id="pages"></div>
      <div id="parties"></div>
    </div>
    <aside class="colonne-liste" aria-label="Passages trouvés">
      <h3>Passages <span id="nbPassages"></span></h3>
      <ul id="passages"></ul>
    </aside>
  </div>
  <button type="button" id="bulle" class="bulle" hidden>Masquer</button>
  <div class="barre-relecture">
    <span class="compte" id="compte"></span>
    <div class="lot-relecture" id="lotRelecture" hidden>
      <button type="button" id="validerConfiance">Valider la suite</button>
      <button type="button" id="validerDoutes">Valider la suite</button>
    </div>
    <button id="interrompreRelecture"
            title="Abandonne ce document et ceux qui suivent">Interrompre</button>
    <button class="primaire" id="valider">Valider et produire</button>
  </div>
</section>
</main>
"""


#: Ce que l'onglet « Installation » ajoute a ce que l'installateur fournit
#: deja : reprendre l'installation, et la mettre a jour. Les deux existaient en
#: ligne de commande seulement, ce qui revenait a ne pas les proposer.
_CORPS_MODELES = """
<section>
  <h2>Modèle de détection</h2>
  <div id="modeles">Chargement…</div>
  <p class="aide">Le modèle repère les noms et les adresses ; les numéros
  (AVS, IBAN, téléphone…) sont repérés par des règles. Un modèle n'est proposé
  ici que s'il repère au moins 90 % des données de test.</p>
</section>
"""

#: En tete de l'onglet : c'est la que mene la pastille de l'en-tete.
_CORPS_MAJ = """
<section>
  <h2>Mise à jour</h2>
  <div id="maj">Vérification…</div>
</section>
"""

_CORPS_ENTRETIEN = """
<section>
  <h2>Désinstallation</h2>
  <div id="desinstallation">Calcul…</div>
</section>
"""


def _entete(seul: bool = False) -> str:
    from .licences import AUTEUR
    return (_ENTETE.replace("{classe}", ' class="seul"' if seul else "")
            .replace("{auteur}", html.escape(AUTEUR)))


def _lien_licence(jeton: str) -> str:
    return f"/licence?jeton={html.escape(jeton)}"


def _corps_licences(jeton: str) -> str:
    """Les mentions legales completes, et la licence de chaque composant."""
    from . import licences

    lignes = []
    for c in licences.COMPOSANTS:
        version = licences.version(c.paquet)
        lignes.append(
            f'<tr><td><a href="{html.escape(c.lien)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(c.nom)}</a>'
            + (f' <span class="version">{html.escape(version)}</span>'
               if version else "")
            + f'</td><td>{html.escape(c.role)}</td>'
            f'<td>{html.escape(c.licence)}</td></tr>')
    return f"""
<section id="licences">
  <h2>Licences</h2>
  <p>Caviardoc © {licences.ANNEE} {html.escape(licences.AUTEUR)}.
  Logiciel libre : vous pouvez l'utiliser, le modifier et le redistribuer
  selon la <a href="{_lien_licence(jeton)}" target="_blank"
  rel="noopener">licence GNU AGPL</a> (version 3 ou ultérieure). Il est fourni
  <strong>sans aucune garantie</strong>. Code source :
  <a href="{licences.DEPOT_SOURCE}" target="_blank"
  rel="noopener noreferrer">{licences.DEPOT_SOURCE.removeprefix("https://")}</a>.</p>
  <p class="aide">Il utilise les composants suivants, chacun sous sa propre
  licence.</p>
  <table class="licences"><thead><tr><th>Composant</th><th>Rôle</th>
  <th>Licence</th></tr></thead><tbody>
  {"".join(lignes)}
  </tbody></table>
</section>
"""


def vue_modeles(ram: int | None) -> dict:
    """Les modeles du registre, ce qu'ils pesent, et lequel convient ici.

    Est conseille le meilleur modele utilisable que la machine porte : un
    rappel de reference eleve, et assez de memoire pour le charger avec deux
    gigaoctets de marge pour le systeme et le navigateur.
    """
    from . import models

    marge = 2 * 1024 ** 3
    actif = models.modele_actif()
    utilisables = [s for s in models.REGISTRY.values() if not s.deconseille]
    portes = [s for s in utilisables if ram is None or ram >= s.memoire + marge]
    # A rappel egal (fp16 et fp32 rendent le meme), le defaut de cette machine.
    meilleur = max(portes or utilisables,
                   key=lambda s: (s.rappel_reference or 0,
                                  s.key == models.DEFAULT_MODEL),
                   default=None)

    installes = {s.key for s in models.REGISTRY.values()
                 if models.check(s).complete}
    propose = models.modele_propose(models.REGISTRY, actif, installes,
                                    models.modeles_refuses(), ram, marge)
    en_service = models.REGISTRY[actif]

    vue = []
    for spec in models.REGISTRY.values():
        mesure = models.mesure_du_modele(spec.key) or {}
        rappels = [v for c, v in mesure.items()
                   if c.startswith("rappel_") and isinstance(v, (int, float))]
        if spec.deconseille:
            avis, raison = "deconseille", spec.deconseille
        elif ram is not None and ram < spec.memoire + marge:
            avis = "juste"
            raison = (f"Cet ordinateur a {models.human(ram)} de mémoire : "
                      f"cela fonctionnera, mais il restera peu de place pour "
                      f"les autres programmes.")
        else:
            avis = "conseille" if spec is meilleur else "utilisable"
            raison = ""
        vue.append({
            "cle": spec.key,
            "libelle": spec.description,
            "taille": spec.total_size,
            "sur_disque": models.taille_installee(spec),
            "installe": models.check(spec).complete,
            "memoire": spec.memoire,
            "licence": spec.license,
            "actif": spec.key == actif,
            "avis": avis,
            "raison": raison,
            "rappel_machine": min(rappels) if rappels else None,
            "rappel_reference": spec.rappel_reference,
            # Le modele en service fait celui-ci inutile : on peut le retirer.
            "remplace_par": (en_service.description
                             if spec.key in en_service.remplace
                             and spec.key in installes else ""),
        })
    # Celui qui sert d'abord, puis du plus recommandable au moins.
    ordre = {"conseille": 0, "utilisable": 1, "juste": 2, "deconseille": 3}
    vue.sort(key=lambda m: (not m["actif"], ordre[m["avis"]]))
    from . import doctor, materiel
    disque = models.free_space(paths.data_dir())
    # Le modele conseille pour cette machine : c'est lui qu'il faudrait loger.
    retenu = models.REGISTRY[models.DEFAULT_MODEL]
    verdict = materiel.evaluer(
        ram, disque, ram_minimale=doctor.MIN_RAM_GB * 1024 ** 3,
        disque_necessaire=retenu.total_size + marge)
    return {"machine": {"ram": ram,
                        "disque_libre": disque,
                        "materiel": materiel.detecter().vue(),
                        "compatibilite": verdict.vue()},
            "modeles": vue,
            "proposition": None if propose is None else {
                "cle": propose.key,
                "libelle": propose.description,
                "taille": propose.total_size,
                "rappel_reference": propose.rappel_reference,
                "nouveautes": propose.nouveautes,
            }}


def onglet_initial() -> str:
    """Ou l'utilisateur atterrit : la ou il a quelque chose a faire.

    Rien d'installe, c'est l'onglet Installation ; tout est pret, c'est
    l'application. On ne demande pas a quelqu'un qui vient anonymiser un
    document de choisir d'abord entre deux onglets dont un seul lui sert.
    """
    from . import doctor, models
    if doctor.interprete_du_moteur() is None:
        return "inst"
    if not models.check(models.REGISTRY[models.modele_actif()]).complete:
        return "inst"
    return "app"


def _bandeau_mesure() -> str:
    """Bandeau rouge en tete de l'onglet application si la mesure n'est pas tenue.

    Avertit sans bloquer : le traitement reste possible, chaque document porte
    en plus son propre drapeau.
    """
    alerte = doctor.avertissement_mesure()
    if not alerte:
        return ""
    return ('<div class="bandeau-mesure" id="bandeau-mesure" role="alert">'
            + html.escape(alerte["texte"]) + "</div>\n")


def page_console(jeton: str, modele: str, initial: str = "") -> str:
    """La page unique : deux onglets, un seul fichier a double-cliquer."""
    from . import installateur
    from .licences import pied_de_page

    initial = initial or onglet_initial()
    journal_inst = f"/journal-installation?jeton={html.escape(jeton)}"
    onglet_inst = (installateur._ACTIONS.replace("JOURNAL", journal_inst)
                   + "\n<main>\n" + _CORPS_MAJ
                   + installateur._ETAPES + _CORPS_MODELES
                   + _CORPS_ENTRETIEN + _corps_licences(jeton)
                   + "\n</main>\n")

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Caviardoc</title>
<style>{_STYLE}
{installateur.styles_confines("#panneau-inst")}
/* La barre d'onglets fait deja passer d'un cote a l'autre :
   deux chemins pour le meme geste, c'est un de trop. */
#panneau-inst #ouvrir{{display:none}}</style></head><body>
{_entete()}
<nav class="onglets">
  <button data-cible="app">Caviarder</button>
  <button data-cible="inst">Installation<span id="alerte-inst"></span></button>
</nav>

<div class="panneau" id="panneau-app">
<main>
{_bandeau_mesure()}{_CORPS_APP}
</main>
</div>

<div class="panneau" id="panneau-inst">
{onglet_inst}
</div>

{_CORPS_RELECTURE}
{pied_de_page(_lien_licence(jeton))}
<script>const JETON = {json.dumps(jeton)};
const ONGLET_INITIAL = {json.dumps(initial)};
const SIMULTANES = {_simultanes()};
{_SCRIPT}
{_SCRIPT_ONGLETS}
/* Le script de l'installateur vit dans sa propre portee : il definit
   `entetes` et `lancer`, comme celui-ci, et la derniere definition gagnerait
   silencieusement. */
(function(){{
{installateur._SCRIPT}
}})();
</script></body></html>"""


def _simultanes() -> int:
    """Documents traites a la fois sans relecture, pour cette machine."""
    from .concurrence import documents_simultanes
    try:
        return documents_simultanes()
    except Exception:
        return 1   # un document a la fois : plus lent, jamais faux


def page_html(jeton: str, modele: str) -> str:
    """Page d'accueil seule, sans l'onglet d'installation.

    Conservee pour `caviardoc interface`, qui sert la surface de travail seule —
    et pour les tests, qui n'ont pas a demarrer une installation.
    """
    from .licences import pied_de_page
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Caviardoc</title>
<style>{_STYLE}</style></head><body>
{_entete(seul=True)}
<main>
{_bandeau_mesure()}{_CORPS_APP}
</main>
{_CORPS_RELECTURE}
{pied_de_page(_lien_licence(jeton))}
<script>const JETON = {json.dumps(jeton)};
const ONGLET_INITIAL = "app";
const SIMULTANES = {_simultanes()};
{_SCRIPT}
</script></body></html>"""


def _doit_redemarrer() -> bool:
    """Le moteur est installe, mais pas pour l'interpreteur qui nous execute."""
    interprete = doctor.interprete_du_moteur()
    return (interprete is not None
            and interprete.resolve() != Path(sys.executable).resolve())


def servir(port: int = 0, ouvrir: bool = True, pret=None,
           arret: threading.Event | None = None, console: bool = False) -> int:
    """Sert l'interface jusqu'a l'arret, puis efface le repertoire de travail.

    ``pret`` est appele avec l'adresse des que le serveur ecoute : le port etant
    choisi par le systeme, c'est le seul moyen pour un appelant — un test, par
    exemple — de savoir ou frapper.

    ``console`` ajoute l'onglet d'installation, d'entretien et de mise a jour.
    C'est la forme que lancent les raccourcis a double-clic : un seul fichier,
    et l'utilisateur atterrit la ou il a quelque chose a faire.
    """
    from . import installateur, models
    from .documents import ocr

    paths.ensure_dirs()
    balayer_ateliers_abandonnes()
    racine = paths.cache_dir() / f"{PREFIXE_ATELIER}{secrets.token_hex(6)}"
    racine.mkdir(parents=True)
    vivant = tenir_atelier(racine)
    atelier = Atelier(racine=racine)
    # Les copies de pages de l'OCR vont dans l'atelier : effacees a l'arret, et
    # balayees au demarrage suivant si le processus meurt en cours de route.
    temporaires_avant = ocr.confiner_temporaires(racine / "tmp")
    # Une relance dans le venv reprend le jeton de la session : l'adresse que
    # le navigateur connait reste valable, la page n'a qu'a se recharger.
    jeton_session = os.environ.pop("CAVIARDOC_JETON", "") or nouveau_jeton()
    relance = {"voulue": False, "maj": False}
    fini = arret or threading.Event()
    installation = installateur.etapes_par_defaut() if console else None
    if installation is not None:
        installateur.constater_en_fond(installation)

    # La mise a jour, en deux temps separes par un redemarrage (voir
    # `maj.appliquer_code`). Elle tourne dans un fil : uv sync puis la mesure
    # prennent plusieurs minutes, qu'une requete ne doit pas retenir.
    suivi_maj = {"etat": "", "message": "", "journal": [], "compte": None}
    # La suppression complete, suivie par la page : etat "", "cours" ou "fini".
    suivi_desinst = {"etat": "", "fait": 0, "total": 0, "courant": "",
                     "echecs": []}

    def journal_maj(ligne: str) -> None:
        suivi_maj["journal"] = (suivi_maj["journal"] + [ligne])[-400:]

    def maj_en_cours() -> bool:
        return suivi_maj["etat"] in ("code", "relance", "suite")

    def maj_premier_temps() -> None:
        from . import maj as module_maj
        try:
            compte = module_maj.appliquer_code(journal_maj)
        except Exception as exc:
            suivi_maj.update(etat="echec", message=(
                str(exc) if isinstance(exc, module_maj.MajImpossible)
                else f"{type(exc).__name__}: {exc}"))
            return
        if not compte["applique"]:
            suivi_maj.update(etat="fini", compte=compte,
                             message="Vous avez déjà la dernière version.")
            return
        module_maj.noter_attente(compte)
        suivi_maj.update(etat="relance",
                         message="Redémarrage avec la nouvelle version…")
        relance["voulue"] = relance["maj"] = True
        fini.set()

    def maj_second_temps(compte: dict) -> None:
        from . import maj as module_maj
        suivi_maj.update(
            etat="suite", compte=compte,
            message=("Nouvelle version installée. Mise à jour des "
                     "bibliothèques, puis nouvelle mesure : plusieurs "
                     "minutes."))
        try:
            compte = module_maj.appliquer_suite(compte, journal_maj)
        except Exception as exc:
            suivi_maj.update(etat="echec", message=(
                str(exc) if isinstance(exc, module_maj.MajImpossible)
                else f"{type(exc).__name__}: {exc}"))
            return
        suivi_maj.update(etat="fini", compte=compte,
                         message="Mise à jour terminée.")
        # Les coches de l'onglet Installation disaient l'avant.
        if installation is not None:
            for cle in ("dependances", "modele", "mesure"):
                etape = installation.par_cle(cle)
                if etape is not None and etape.etat != "en_cours":
                    etape.etat = "en_attente"
            installateur.constater(installation)

    if console:
        from .maj import reprendre_attente
        attente = reprendre_attente()
        if attente is not None:
            threading.Thread(target=maj_second_temps, args=(attente,),
                             daemon=True).start()
    from .doctor import total_ram_bytes
    #: Lue une fois : elle ne change pas pendant la session.
    ram = total_ram_bytes() if console else None

    def recaler_etape_modele(message: str) -> None:
        """L'etape « Modele » de la liste dit ce qui est vrai maintenant."""
        etape = installation.par_cle("modele")
        if etape is not None and etape.etat != "en_cours":
            spec = models.REGISTRY[models.modele_actif()]
            if models.check(spec).complete:
                etape.etat, etape.message = "ok", f"installé ({spec.key})"
            else:
                etape.etat, etape.message = "en_attente", message
            etape.remede = ""
            etape.noter(message)

    class Poignee(PoigneeBase):
        jeton = jeton_session

        def _json(self, code: int, charge) -> None:
            self.repondre(code, json.dumps(charge).encode("utf-8"),
                          "application/json; charset=utf-8")

        def do_GET(self):
            if not self.hote_admis():
                return self.refus(b"hote refuse")
            adresse = urlparse(self.path)
            # Dans l'URL pour l'ouverture de la page — c'est le seul moyen de
            # transmettre le jeton au navigateur — et en en-tete pour les
            # requetes que la page emet ensuite, ou il ne traine ni dans
            # l'historique ni dans un journal.
            # Preuve de possession du jeton, avant que le client ne l'envoie :
            # elle ne revele rien du jeton et n'en exige aucun.
            if adresse.path == "/preuve":
                return self.repondre_preuve(
                    parse_qs(adresse.query).get("defi", [""])[0])
            if not (self.jeton_admis(self.jeton_de_l_entete())
                    or self.jeton_admis(
                        parse_qs(adresse.query).get("jeton", [""])[0])):
                return self.refus()

            if adresse.path == "/vivant":
                return self.repondre(200, SIGNE_VIVANT)

            if adresse.path in ("/", "/index.html"):
                page = (page_console(self.jeton, models.modele_actif())
                        if console
                        else page_html(self.jeton, models.modele_actif()))
                return self.repondre(200, page.encode("utf-8"),
                                     "text/html; charset=utf-8")

            if console and adresse.path == "/etapes":
                return self._json(200, dict(installation.vue(),
                                            redemarrage=_doit_redemarrer()))

            if console and adresse.path == "/journal-installation":
                lignes = []
                for e in installation.etapes:
                    lignes.append(f"===== {e.titre} · {e.etat} =====")
                    lignes.extend(e.journal or ["(rien)"])
                    if e.message:
                        lignes.append(f">>> {e.message}")
                    lignes.append("")
                return self.repondre(200, "\n".join(lignes).encode("utf-8"))

            if console and adresse.path == "/maj":
                from . import maj as module_maj
                if suivi_maj["etat"]:
                    vue = {k: v for k, v in suivi_maj.items() if k != "journal"}
                    vue["dernieres"] = suivi_maj["journal"][-3:]
                    if not maj_en_cours():
                        # Le resultat se montre une fois ; ensuite la page
                        # verifie de nouveau, comme a l'ordinaire.
                        suivi_maj.update(etat="", message="", journal=[],
                                         compte=None)
                    return self._json(200, {"suivi": vue})
                try:
                    return self._json(200, module_maj.verifier().vue())
                except module_maj.MajImpossible as exc:
                    return self._json(200, {"erreur": str(exc)})
                except Exception as exc:
                    return self._json(200, {"erreur": f"{type(exc).__name__}: {exc}"})

            if adresse.path == "/licence":
                from .licences import texte_licence
                return self.repondre(200, texte_licence())

            if console and adresse.path == "/modeles":
                return self._json(200, vue_modeles(ram))

            if console and adresse.path == "/desinstallation":
                from . import desinstallation
                return self._json(200, {
                    "elements": [e.vue() for e in desinstallation.inventaire()
                                 if e.par_defaut],
                    "dehors": [x.vue() for x in
                               desinstallation.laisses_a_l_utilisateur()],
                    "deja_la": [{"nom": n, "chemin": str(c)} for n, c in
                                desinstallation.deja_presents()],
                    "depot": str(desinstallation.depot()),
                })

            if console and adresse.path == "/desinstallation/suivi":
                return self._json(200, dict(suivi_desinst))

            if adresse.path == "/tache":
                voulu = parse_qs(adresse.query).get("id", [""])[0]
                tache = atelier.taches.get(voulu)
                if tache is None:
                    return self.repondre(404, b"tache inconnue")
                return self._json(200, tache.vue())

            if adresse.path == "/revue":
                voulu = parse_qs(adresse.query).get("id", [""])[0]
                tache = atelier.taches.get(voulu)
                if tache is None or tache.session is None:
                    return self.repondre(404, b"relecture inconnue")
                return self._json(200, revue_en_json(tache))

            if adresse.path == "/revue/apercu":
                requete = parse_qs(adresse.query)
                tache = atelier.taches.get(requete.get("id", [""])[0])
                apercu = (tache.apercus.get(requete.get("partie", [""])[0])
                          if tache is not None else None)
                if apercu is None:
                    return self.repondre(404, b"page inconnue")
                return self.repondre(200, apercu.jpeg, "image/jpeg")

            if adresse.path == "/memoire":
                from .exceptions import Repertoire
                return self._json(200, Repertoire.charger().vue(atelier.clairs))

            if adresse.path == "/journal":
                # Le journal peut porter une trace d'exception, donc un chemin
                # de fichier : il ne sort que sur demande explicite.
                lignes = []
                for t in atelier.taches.values():
                    lignes.append(f"--- {t.nom} · {t.etat} ---")
                    lignes.extend(t.journal)
                    if t.erreur:
                        lignes.append(f"ERREUR : {t.erreur}")
                corps = ("\n".join(lignes) or "aucune tache").encode("utf-8")
                return self.repondre(200, corps)

            if adresse.path == "/telecharger":
                # L'identifiant sert de clef dans une table tenue par le
                # serveur : aucun chemin fourni par le client n'est joint a un
                # repertoire, et rien hors de cette table n'est atteignable.
                voulu = parse_qs(adresse.query).get("id", [""])[0]
                produit = atelier.productions.get(voulu)
                if produit is None or not produit.chemin.is_file():
                    return self.repondre(404, b"introuvable")
                if not (produit.relu or parse_qs(adresse.query).get(
                        "confirme", [""])[0] == "1"):
                    return self.repondre(409, MESSAGE_NON_RELU)
                return self.envoyer_fichier(produit.chemin,
                                            nom_final(atelier, produit.nom))

            return self.repondre(404, b"introuvable")

        def do_POST(self):
            if not self.hote_admis():
                return self.refus(b"hote refuse")
            if not self.jeton_admis(self.jeton_de_l_entete()):
                return self.refus()
            chemin = urlparse(self.path).path

            if console and chemin == "/lancer":
                taille = int(self.headers.get("Content-Length") or 0)
                try:
                    charge = json.loads(self.rfile.read(taille) or b"{}")
                except json.JSONDecodeError:
                    return self.repondre(400, b"json invalide")
                if installation.en_cours:
                    return self.repondre(409, b"une etape est deja en cours")
                try:
                    etape = installateur.executer(installation,
                                                  charge.get("cle", ""))
                except KeyError:
                    return self.repondre(404, b"etape inconnue")
                return self._json(200, etape.vue())

            if console and chemin.startswith("/modeles/"):
                taille = int(self.headers.get("Content-Length") or 0)
                try:
                    charge = json.loads(self.rfile.read(taille) or b"{}")
                except json.JSONDecodeError:
                    return self.repondre(400, b"json invalide")
                spec = models.REGISTRY.get(charge.get("cle", ""))
                if spec is None:
                    return self.repondre(404, "modèle inconnu".encode())
                if installation.en_cours == "constat":
                    # Le constat du demarrage dure une fraction de seconde :
                    # on l'attend plutot que de refuser.
                    with installation.verrou:
                        pass
                if installation.en_cours:
                    return self.repondre(
                        409, "une étape d'installation est en cours".encode())
                action = chemin.removeprefix("/modeles/")

                if action == "installer":
                    from . import materiel
                    impossibles = materiel.obstacles()
                    if impossibles:
                        return self.repondre(409, " ".join(impossibles).encode())

                if action == "refuser":
                    models.refuser_proposition(spec.key)
                    return self._json(200, {"refuse": spec.key})

                if action == "supprimer":
                    liberes = models.supprimer(spec)
                    if spec.key == models.modele_actif():
                        # Le moteur en memoire ne doit pas survivre a ses
                        # fichiers : le prochain document le rechargera, ou
                        # dira qu'il manque.
                        atelier._modele = None
                        recaler_etape_modele("supprimé — cliquez sur "
                                             "« Lancer » pour le réinstaller")
                    return self._json(200, {"liberes": liberes})

                # Un modèle déconseillé reste possible, mais jamais par
                # inadvertance : l'interface doit l'avoir fait confirmer.
                if spec.deconseille and not charge.get("confirme"):
                    return self.repondre(409, ("Modèle déconseillé : "
                                               + spec.deconseille).encode())
                if action == "installer":
                    models.definir_modele_actif(spec.key)
                    atelier._modele = None
                    installateur.executer(installation, "modele")
                    return self._json(200, {"lance": True})
                if action == "utiliser":
                    if not models.check(spec).complete:
                        return self.repondre(409, "modèle non installé".encode())
                    models.definir_modele_actif(spec.key)
                    atelier._modele = None
                    recaler_etape_modele("en service")
                    return self._json(200, {"actif": spec.key})
                return self.repondre(404, b"introuvable")

            if console and chemin == "/maj":
                if maj_en_cours():
                    return self.repondre(409, "Mise à jour déjà en cours.".encode())
                if installation.en_cours:
                    return self.repondre(409, (
                        "Une étape d'installation est en cours : attendez "
                        "qu'elle se termine.").encode())
                if any(t.etat in ("en_attente", "en_cours", "revue")
                       for t in atelier.taches.values()):
                    # Le redemarrage qui suit couperait le traitement.
                    return self.repondre(409, (
                        "Un document est en cours de traitement : attendez "
                        "qu'il soit terminé.").encode())
                suivi_maj.update(etat="code", journal=[], compte=None,
                                 message="Téléchargement de la nouvelle version…")
                threading.Thread(target=maj_premier_temps, daemon=True).start()
                return self._json(200, {"lance": True})

            if console and chemin == "/desinstallation":
                from . import desinstallation
                retenus = [e for e in desinstallation.inventaire()
                           if e.par_defaut]
                bloquants = [e for e in retenus
                             if desinstallation.s_execute_depuis(e)]
                if not bloquants:
                    if suivi_desinst["etat"] == "cours":
                        return self.repondre(409, b"suppression en cours")

                    def avancer(fait, total, titre):
                        suivi_desinst.update(fait=fait, total=total, courant=titre)

                    def supprimer_en_fil():
                        try:
                            echecs = desinstallation.supprimer(
                                retenus, progression=avancer)
                        except Exception as exc:
                            echecs = [f"{type(exc).__name__}: {exc}"]
                        suivi_desinst.update(etat="fini", echecs=echecs, courant="")

                    suivi_desinst.update(etat="cours", fait=0, total=len(retenus),
                                         courant="", echecs=[])
                    threading.Thread(target=supprimer_en_fil, daemon=True).start()
                    return self._json(200, {"lance": True})

                # La console tourne sous l'interpreteur du venv, qu'elle doit
                # justement effacer : elle ne peut pas se supprimer elle-meme.
                # On confie donc le travail a un interpreteur exterieur, et on
                # s'arrete aussitot pour lui liberer les fichiers.
                exterieur = desinstallation.interprete_exterieur()
                if exterieur is None:
                    return self._json(200, {"echecs": [
                        "Aucun interpreteur exterieur au dossier a supprimer. "
                        "Utilisez le lanceur de desinstallation."]})
                depart = {"start_new_session": True} if os.name != "nt" else {
                    "creationflags": 0x00000008}    # DETACHED_PROCESS
                subprocess.Popen(
                    [str(exterieur), "-m", "caviardoc", "desinstaller",
                     "--oui", "--patienter"],
                    cwd=paths.project_root(),
                    env=dict(os.environ, PYTHONPATH="src"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    **depart)
                fini.set()
                return self._json(200, {"echecs": [], "ferme": True,
                                        "depot": str(desinstallation.depot())})

            if chemin == "/televerser":
                nom = unquote(self.headers.get(ENTETE_NOM) or "")
                dossier = unquote(self.headers.get(ENTETE_DOSSIER) or "")
                taille = int(self.headers.get("Content-Length") or 0)
                if taille > TAILLE_MAX:
                    return self.repondre(413, b"fichier trop volumineux")
                try:
                    depot = deposer(atelier, nom, self.rfile, taille, dossier)
                except ValueError as exc:
                    return self.repondre(400, str(exc).encode("utf-8"))
                return self._json(200, {"id": depot.identifiant,
                                        "nom": depot.nom,
                                        "dossier": depot.dossier,
                                        "taille": depot.taille})

            taille = int(self.headers.get("Content-Length") or 0)
            try:
                charge = json.loads(self.rfile.read(taille) or b"{}")
            except json.JSONDecodeError:
                return self.repondre(400, b"json invalide")

            if chemin == "/traiter":
                if maj_en_cours():
                    return self.repondre(409, (
                        "Mise à jour en cours : attendez qu'elle se termine "
                        "(onglet Installation).").encode())
                try:
                    tache = lancer(atelier, charge.get("id", ""), charge)
                except KeyError:
                    return self.repondre(404, b"depot inconnu")
                return self._json(200, tache.vue())

            if chemin == "/prelire":
                atelier.prelire(charge.get("id", ""),
                                int(charge.get("dpi") or 300))
                return self._json(200, {"ok": True})

            if chemin == "/interrompre":
                tache = atelier.taches.get(charge.get("tache", ""))
                if tache is None:
                    return self.repondre(404, b"tache inconnue")
                tache.interrompre()
                return self._json(200, tache.vue())

            if chemin in ("/revue/ajout", "/revue/zone", "/revue/valider"):
                from .revue import ajouter, decider

                tache = atelier.taches.get(charge.get("tache", ""))
                if tache is None or tache.session is None:
                    return self.repondre(404, b"relecture inconnue")
                if tache.etat != "revue":
                    return self.repondre(409, b"la relecture n'est plus ouverte")

                if chemin == "/revue/ajout":
                    partie = charge.get("partie", "")
                    try:
                        ajouter(tache.session, partie, tache.textes[partie],
                                int(charge["debut"]), int(charge["fin"]))
                    except (KeyError, ValueError) as exc:
                        return self.repondre(400, str(exc).encode("utf-8"))
                    return self._json(200, revue_en_json(tache))

                if chemin == "/revue/zone":
                    try:
                        ajoutes = ajouter_zone(
                            tache, charge.get("partie", ""),
                            *(float(charge[k]) for k in ("x0", "y0", "x1", "y1")))
                    except (KeyError, ValueError, TypeError) as exc:
                        return self.repondre(400, str(exc).encode("utf-8"))
                    if not ajoutes:
                        return self.repondre(
                            422, "Aucun mot reconnu dans ce cadre.".encode("utf-8"))
                    return self._json(200, revue_en_json(tache))

                decider(tache.session, charge.get("decisions") or {})
                tache.apercus.clear()
                tache.reprendre.set()
                return self._json(200, tache.vue())

            if chemin == "/retirer":
                try:
                    retirer(atelier, charge.get("id", ""))
                except KeyError:
                    return self.repondre(404, b"depot inconnu")
                except RuntimeError as exc:
                    return self.repondre(409, str(exc).encode("utf-8"))
                return self._json(200, {"retire": True})

            if chemin == "/nouvelle-demande":
                try:
                    nouvelle_demande(atelier)
                except RuntimeError as exc:
                    return self.repondre(409, str(exc).encode("utf-8"))
                return self._json(200, {"efface": True})

            if chemin == "/archive":
                if not charge.get("confirme") and any(
                        not atelier.productions[i].relu
                        for i in charge.get("ids") or []
                        if isinstance(i, str) and i in atelier.productions):
                    return self.repondre(409, MESSAGE_NON_RELU)
                try:
                    archive = archiver(atelier, charge.get("ids") or [],
                                       a_plat=bool(charge.get("a_plat")))
                except KeyError:
                    return self.repondre(404, b"rien a archiver")
                return self._json(200, {"id": archive.identifiant,
                                        "nom": archive.nom,
                                        "taille": archive.chemin.stat().st_size})

            if chemin == "/oublier":
                from .exceptions import Repertoire
                repertoire = Repertoire.charger()
                if charge.get("tout") is True:
                    enleve = bool(repertoire.tout_oublier())
                    atelier.clairs.clear()
                elif charge.get("anonymes") is True:
                    enleve = bool(repertoire.oublier_anonymes(atelier.clairs))
                else:
                    enleve = repertoire.oublier(charge.get("chaine", ""))
                repertoire.enregistrer()
                return self._json(200, dict(repertoire.vue(atelier.clairs),
                                            enleve=enleve))

            if chemin == "/quitter":
                self.repondre(200, b"ok")
                fini.set()
                return

            if console and chemin == "/redemarrer":
                # Le moteur vient d'etre installe dans le venv, mais ce
                # processus tourne sous le Python d'amorcage, qui ne peut pas
                # le charger : il passe la main a l'interpreteur du venv.
                if not _doit_redemarrer() or installation.en_cours:
                    return self.repondre(409, b"rien a relancer")
                relance["voulue"] = True
                self.repondre(200, b"ok")
                fini.set()
                return

            return self.repondre(404, b"introuvable")

    serveur = ServeurLocal((HOTE, port), Poignee)
    Poignee.hotes = hotes_admis(serveur.server_port)
    adresse = f"http://{HOTE}:{serveur.server_port}/?jeton={jeton_session}"
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()
    if console:
        noter_session(serveur.server_port, jeton_session)

    # `caviardoc interface` sera souvent arrete par un gestionnaire de service ou
    # une fermeture de terminal, qui envoient SIGTERM et non Ctrl-C. Sans ce
    # relais, l'interpreteur s'arrete sans executer le `finally` ci-dessous et
    # les originaux restent sur le disque.
    ancien_term = ancien_hup = None
    try:
        ancien_term = signal.signal(signal.SIGTERM,
                                    lambda *_: fini.set())
        # Fermer le terminal envoie SIGHUP (absent sous Windows).
        if hasattr(signal, "SIGHUP"):
            ancien_hup = signal.signal(signal.SIGHUP,
                                       lambda *_: fini.set())
    except ValueError:
        pass          # hors du fil principal : un test, typiquement

    try:
        # Dans le `try` : si l'ouverture du navigateur echoue, la note de
        # session et les originaux sont tout de meme nettoyes.
        if pret is not None:
            pret(adresse)
        else:
            print(f"  Interface ouverte sur {adresse}")
            print("  Ctrl-C pour arreter. Les fichiers deposes seront "
                  "effaces.")
        if ouvrir:
            webbrowser.open(adresse)
        fini.wait()
    except KeyboardInterrupt:
        print()
    finally:
        if ancien_term is not None:
            signal.signal(signal.SIGTERM, ancien_term)
        if ancien_hup is not None:
            signal.signal(signal.SIGHUP, ancien_hup)
        serveur.shutdown()
        if console:
            oublier_session(jeton_session)
        # Les originaux portent des donnees de patients : ils ne survivent pas
        # a la session, quelle que soit la facon dont elle se termine. Le
        # verrou d'abord : Windows n'efface pas un fichier ouvert.
        vivant.close()
        ocr.restaurer_temporaires(temporaires_avant)
        shutil.rmtree(racine, ignore_errors=True)
        if pret is None:
            print("  Fichiers de travail effaces.")
    if relance["voulue"]:
        serveur.server_close()
        interprete = doctor.interprete_du_moteur()
        if interprete is None and relance["maj"]:
            interprete = Path(sys.executable)
        if interprete is not None:
            print("  Mise a jour appliquee : redemarrage avec la nouvelle "
                  "version." if relance["maj"] else
                  "  Moteur installe : redemarrage avec l'environnement "
                  "de l'application.")
            env = dict(os.environ, CAVIARDOC_JETON=jeton_session)
            commande = [str(interprete), "-m", "caviardoc", "console",
                        "--port", str(serveur.server_port), "--sans-navigateur"]
            if relance["maj"] and os.name == "nt":
                # Sous Windows, ce processus ne doit pas attendre le suivant :
                # vivant, il garderait chargees les bibliotheques que le second
                # temps de la mise a jour doit remplacer. Le nouveau reste
                # attache a la meme console, qui demeure ouverte.
                subprocess.Popen(commande, env=env)
                return 0
            return subprocess.run(commande, env=env).returncode
    return 0
