"""Ce que la machine retient des corrections, et ce qu'elle refuse de retenir.

Le modele est fige : il n'apprend pas. Ce qui peut apprendre, c'est le poste —
en gardant les decisions de relecture et en les rejouant sur les documents
suivants. Deux listes, et **elles ne sont pas symetriques**, parce que se
tromper dans un sens coute infiniment plus cher que dans l'autre.

**« toujours masquer »** vient des masques poses a la main sur ce que l'outil
avait manque. C'est le sens sur : en cas d'erreur, un mot est masque pour
rien.

**« jamais masquer »** vient des masques retires. C'est le sens dangereux :
une erreur ici decouvre la chaine dans *tous* les documents a venir. Les deux
listes agissent des la premiere decision ; l'utilisateur peut retirer une
entree de la liste a tout moment, et certaines chaines ne sont jamais
apprises du tout.

## Ce qui n'est jamais appris

Rien de ce qu'une regle deterministe reconnait — numero AVS, IBAN, carte,
telephone, e-mail. Ces regles valident une somme de controle : elles ne se
trompent pratiquement pas, et ce qu'elles trouvent est par construction un
identifiant personnel. Retirer un tel masque est une decision qui vaut pour
un document, jamais pour tous les suivants.

Il se trouve que cette regle resout aussi une question de conservation : ce
qui reste apprenable en clair est du vocabulaire — « Macula », « Cher
confrere », le nom d'un etablissement — et non des identites.

## Pourquoi « toujours masquer » est stocke en empreintes

Cette liste-la contient, par nature, des noms de patients : ce sont
precisement les identites que l'outil avait manquees. Un fichier lisible
enumerant tous les patients passes par le cabinet serait un probleme plus
grave que celui qu'il resout. On ne conserve donc qu'une empreinte SHA-256
salee de chaque chaine, avec un sel propre a la machine. Le fichier ne se lit
pas ; il ne sert qu'a reconnaitre.

Le repertoire vit dans le repertoire de donnees local, jamais dans le depot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .detect import rules
from .engine import Origine, Span

#: En deca, une chaine est trop courte pour etre distinctive ; au-dela, c'est
#: une phrase et non une entite.
LONGUEUR_MIN = 3
LONGUEUR_MAX = 120

#: Nombre maximal de mots d'une entree « toujours masquer ». Au-dela, la
#: recherche par n-grammes couterait plus qu'elle ne rapporte.
MOTS_MAX = 6

VERSION = 1


class ApprentissageRefuse(ValueError):
    """La chaine ne doit pas etre apprise, et on sait pourquoi."""


@dataclass
class EntreeJamais:
    """Une chaine que l'utilisateur ne veut plus voir masquee."""

    chaine: str
    motifs: list[str] = field(default_factory=list)
    vues: int = 0
    derniere: str = ""


@dataclass
class EntreeToujours:
    """Une chaine a masquer, conservee sous forme d'empreinte seulement."""

    empreinte: str
    mots: int
    etiquette: str = "ajout manuel"


def _aujourdhui() -> str:
    return time.strftime("%Y-%m-%d")


def normaliser(chaine: str) -> str:
    """Forme comparable : espaces reduits, casse repliee.

    Volontairement sobre : pas de retrait d'accents. « Girard » et « Girárd »
    sont deux chaines differentes, et les confondre ferait masquer l'une pour
    l'autre sans que personne l'ait demande.
    """
    return " ".join(chaine.split()).casefold()


def _rogner(texte: str, debut: int, fin: int) -> tuple[int, int]:
    """Retire la ponctuation aux deux bouts d'un candidat.

    Un nom en fin de phrase est colle a sa virgule — « Fontannaz, » — et
    l'empreinte de ce fragment differe de celle du nom. Sans ce rognage, une
    chaine apprise est manquee des qu'une ponctuation la suit, c'est-a-dire
    presque toujours.
    """
    while debut < fin and not texte[debut].isalnum():
        debut += 1
    while fin > debut and not texte[fin - 1].isalnum():
        fin -= 1
    return debut, fin


def apprenable(chaine: str) -> str:
    """Rend le motif du refus, ou une chaine vide si l'apprentissage est permis.

    Un motif plutot qu'un booleen : l'interface doit pouvoir dire pourquoi une
    correction n'a pas ete retenue, sinon l'utilisateur la refait indefiniment.
    """
    nu = chaine.strip()
    if len(nu) < LONGUEUR_MIN:
        return f"trop courte (moins de {LONGUEUR_MIN} caracteres)"
    if len(nu) > LONGUEUR_MAX:
        return f"trop longue (plus de {LONGUEUR_MAX} caracteres)"
    if len(nu.split()) > MOTS_MAX:
        return f"plus de {MOTS_MAX} mots"
    if rules.find_all(nu):
        return ("reconnue par une regle a somme de controle : c'est un "
                "identifiant personnel, et le decouvrir pour tous les "
                "documents a venir serait une erreur")
    return ""


@dataclass
class Repertoire:
    """Les deux listes, et le sel qui protege la seconde."""

    sel: str = ""
    jamais: list[EntreeJamais] = field(default_factory=list)
    toujours: list[EntreeToujours] = field(default_factory=list)
    #: Non vide quand le fichier existait mais n'a pas pu etre relu. Jamais
    #: ecrit : c'est un signal pour l'appelant, et l'ordre de sauvegarder
    #: l'original avant de l'ecraser.
    degat: str = ""

    # --- persistance ------------------------------------------------------
    @staticmethod
    def chemin() -> Path:
        return paths.data_dir() / "exceptions.json"

    @classmethod
    def charger(cls) -> "Repertoire":
        """Lit le repertoire. Un fichier illisible vaut un repertoire vide.

        Jamais une exception : une liste d'exceptions abimee ne doit pas
        empecher de caviarder un document. Mais le defaut sans elle n'est sur
        que pour « jamais » ; pour « toujours », il fait perdre des masques.
        Le degat est donc signale (``degat``), et ``enregistrer`` met l'original
        de cote avant de l'ecraser.
        """
        try:
            brut = json.loads(cls.chemin().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(sel=secrets.token_hex(16))
        except (OSError, ValueError) as exc:
            return cls(sel=secrets.token_hex(16),
                       degat=f"fichier illisible ({type(exc).__name__})")
        if not isinstance(brut, dict):
            return cls(sel=secrets.token_hex(16),
                       degat="contenu inattendu")

        try:
            return cls._depuis(brut)
        except (TypeError, ValueError, AttributeError) as exc:
            return cls(sel=secrets.token_hex(16),
                       degat=f"entree illisible ({type(exc).__name__})")

    @classmethod
    def _depuis(cls, brut: dict) -> "Repertoire":
        sel = brut.get("sel")
        return cls(
            sel=sel or secrets.token_hex(16),
            degat="" if sel else "sel absent : les empreintes sont orphelines",
            jamais=[EntreeJamais(**e) for e in brut.get("jamais", [])
                    if isinstance(e, dict) and "chaine" in e],
            # Les anciens fichiers portaient « vues » et « derniere » : on les
            # ignore a la lecture, ils disparaissent a la prochaine sauvegarde.
            toujours=[EntreeToujours(
                          empreinte=str(e["empreinte"]),
                          mots=int(e.get("mots", 1)),
                          etiquette=str(e.get("etiquette", "ajout manuel")))
                      for e in brut.get("toujours", [])
                      if isinstance(e, dict) and "empreinte" in e],
        )

    def enregistrer(self) -> Path:
        paths.ensure_dirs()
        charge = {
            "version": VERSION,
            "_lisez_moi": (
                "Liste d'exceptions locale a cette machine. « jamais » ne "
                "contient que du vocabulaire ; « toujours » ne contient que "
                "des empreintes, jamais de nom en clair. Ne pas versionner."),
            "sel": self.sel,
            "jamais": [e.__dict__ for e in self.jamais],
            "toujours": [e.__dict__ for e in self.toujours],
        }
        chemin = self.chemin()
        if self.degat and chemin.exists():
            # L'original abime reste recuperable : on ne l'ecrase pas en silence.
            horodatage = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(chemin, chemin.with_name(
                f"{chemin.name}.abime-{horodatage}"))
        self.degat = ""
        # Ecriture atomique : un arret en cours d'ecriture ne tronque pas la liste.
        # Mode 0600 fixe a la creation (POSIX), avant d'y ecrire : le sel et les
        # empreintes ne sont jamais lisibles par un autre compte, meme un instant.
        temporaire = chemin.with_name(chemin.name + ".tmp")
        donnees = json.dumps(charge, ensure_ascii=False, indent=2)
        try:
            temporaire.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as sortie:
            sortie.write(donnees)
        os.replace(temporaire, chemin)
        return chemin

    # --- empreintes -------------------------------------------------------
    def empreinte(self, chaine: str) -> str:
        """Empreinte salee d'une chaine normalisee.

        Salee, et le sel est propre a la machine : deux cabinets qui suivent
        le meme patient n'obtiennent pas la meme empreinte, et un fichier
        d'exceptions ne peut pas servir a tester si un nom donne y figure.
        """
        return hmac.new(self.sel.encode("utf-8"),
                        normaliser(chaine).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    # --- consultation -----------------------------------------------------
    def jamais_masquer(self, chaine: str) -> bool:
        cible = normaliser(chaine)
        return any(normaliser(e.chaine) == cible for e in self.jamais)

    #: Provenances sur lesquelles « jamais masquer » n'a pas autorite.
    #:
    #: Une regle porte une somme de controle validee. Un span humain porte une
    #: intention explicite — et quand les deux listes se contredisent sur la
    #: meme chaine, c'est le sens sur qui doit l'emporter : masquer de trop.
    INTOUCHABLES = frozenset({Origine.REGLE.value, Origine.HUMAIN.value})

    def filtrer(self, spans: list[Span]) -> list[Span]:
        """Retire les spans que l'utilisateur a dit de ne plus masquer."""
        return [s for s in spans
                if s.origine in self.INTOUCHABLES
                or not self.jamais_masquer(s.text)]

    def spans_toujours(self, texte: str) -> list[Span]:
        """Cherche dans ``texte`` les chaines apprises, par n-grammes de mots.

        On ne peut pas chercher les chaines elles-memes : elles ne sont pas
        conservees. On hache donc les candidats du document et on interroge
        l'index — l'operation inverse, pour le meme resultat.
        """
        if not self.toujours:
            return []
        index = {e.empreinte: e for e in self.toujours}
        longueurs = sorted({e.mots for e in self.toujours if 1 <= e.mots <= MOTS_MAX})
        mots = list(re.finditer(r"\S+", texte))

        trouves: list[Span] = []
        for n in longueurs:
            for i in range(len(mots) - n + 1):
                debut, fin = _rogner(texte, mots[i].start(),
                                     mots[i + n - 1].end())
                if debut >= fin:
                    continue
                entree = index.get(self.empreinte(texte[debut:fin]))
                if entree is not None:
                    trouves.append(Span(debut, fin, entree.etiquette, 1.0,
                                        texte[debut:fin],
                                        Origine.HUMAIN.value))
        return trouves

    # --- apprentissage ----------------------------------------------------
    def retenir_jamais(self, chaine: str, motif: str = "") -> tuple[bool, str]:
        """Enregistre un rejet. Actif des la premiere fois : rend (True, "")."""
        refus = apprenable(chaine)
        if refus:
            return False, refus

        cible = normaliser(chaine)
        entree = next((e for e in self.jamais
                       if normaliser(e.chaine) == cible), None)
        if entree is None:
            entree = EntreeJamais(chaine=chaine.strip())
            self.jamais.append(entree)
        entree.vues += 1
        entree.derniere = _aujourdhui()
        if motif and motif not in entree.motifs:
            entree.motifs.append(motif)

        return True, ""

    def retenir_toujours(self, chaine: str,
                         etiquette: str = "ajout manuel") -> tuple[bool, str]:
        """Enregistre un masque pose a la main. Actif immediatement."""
        refus = apprenable(chaine)
        if refus:
            return False, refus

        empreinte = self.empreinte(chaine)
        entree = next((e for e in self.toujours if e.empreinte == empreinte),
                      None)
        if entree is None:
            entree = EntreeToujours(empreinte=empreinte,
                                    mots=len(chaine.split()),
                                    etiquette=etiquette)
            self.toujours.append(entree)
        return True, ""

    def oublier(self, chaine: str) -> bool:
        """Retire une chaine des deux listes. Rend True si quelque chose a saute."""
        cible = normaliser(chaine)
        empreinte = self.empreinte(chaine)
        avant = len(self.jamais) + len(self.toujours)
        self.jamais = [e for e in self.jamais
                       if normaliser(e.chaine) != cible]
        self.toujours = [e for e in self.toujours if e.empreinte != empreinte]
        return len(self.jamais) + len(self.toujours) != avant

    def oublier_anonymes(self, clairs: dict[str, str] | None = None) -> int:
        """Retire les « toujours » dont la chaine n'est plus lisible ici.

        Ce sont celles qui ne figurent pas dans ``clairs`` : ajoutees lors
        d'une session precedente, il n'en reste que l'empreinte. Rend le
        nombre d'entrees retirees.
        """
        clairs = clairs or {}
        avant = len(self.toujours)
        self.toujours = [e for e in self.toujours if e.empreinte in clairs]
        return avant - len(self.toujours)

    def tout_oublier(self) -> int:
        """Vide les deux listes. Rend le nombre d'entrees retirees."""
        total = len(self.jamais) + len(self.toujours)
        self.jamais = []
        self.toujours = []
        return total

    def vue(self, clairs: dict[str, str] | None = None) -> dict:
        """Etat affichable. Ne rend jamais le contenu de « toujours » de lui-meme.

        Les empreintes sont illisibles, mais les compter et les dater suffit a
        rendre compte de ce que la machine a retenu. ``clairs`` associe une
        empreinte a la chaine que l'appelant connait encore — celle qu'on vient
        d'ajouter dans la session en cours, gardee en memoire vive seulement.
        Une entree qui y figure porte alors sa chaine ; les autres restent
        anonymes, et rien n'est lu sur le disque.
        """
        clairs = clairs or {}
        return {
            "jamais": [{"chaine": e.chaine, "vues": e.vues,
                        "derniere": e.derniere,
                        "motifs": e.motifs} for e in self.jamais],
            "toujours": [dict({"mots": e.mots, "etiquette": e.etiquette},
                              **({"chaine": clairs[e.empreinte]}
                                 if e.empreinte in clairs else {}))
                         for e in self.toujours],
        }


def apprendre_de(session, repertoire: Repertoire | None = None) -> dict:
    """Tire les lecons d'une session de relecture, et enregistre.

    Rend le compte rendu de ce qui a ete retenu et de ce qui a ete refuse :
    l'interface doit pouvoir le montrer, faute de quoi l'utilisateur refait
    indefiniment une correction que la machine ecarte en silence.
    """
    repertoire = repertoire or Repertoire.charger()
    retenu: list[dict] = []
    refuse: list[dict] = []

    for d in session.rejetees:
        # Une detection posee par une regle ne s'apprend pas : `apprenable`
        # le redira, mais le dire ici evite d'y penser deux fois.
        actif, explication = repertoire.retenir_jamais(d.texte, d.motif)
        (retenu if actif else refuse).append(
            {"chaine": d.texte, "action": "jamais", "explication": explication})

    for d in session.ajoutees:
        actif, explication = repertoire.retenir_toujours(d.texte, d.etiquette)
        # Rien de lisible dans le compte rendu d'un ajout : la chaine est une
        # identite, et elle n'est deja plus conservee en clair.
        (retenu if actif else refuse).append(
            {"chaine": "(masque ajoute)", "action": "toujours",
             "explication": explication})

    repertoire.enregistrer()
    return {"retenu": retenu, "refuse": refuse,
            "total": len(retenu) + len(refuse)}
