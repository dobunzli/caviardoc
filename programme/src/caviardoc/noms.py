"""Anonymisation du nom de fichier lui-meme.

Un document parfaitement caviarde dont le fichier s'appelle
``5092_Jean-Bidule_Zorglub_Ordonnance.pdf`` n'est pas anonymise. Le nom
apparait dans les listings, les pieces jointes, les journaux de serveur et les
sauvegardes — souvent plus visible que le contenu.

Trois exigences propres aux noms de fichiers :

1. Les separateurs. « Jean-Noel_Zorglub » ne correspond a aucune chaine du
   document, qui contient « Jean-Bidule Zorglub ». Le nom est donc lu mot a
   mot, separateurs et casse ignores (voir `AnonymiseurNoms`).

2. La coherence. Le nom de fichier doit recevoir le meme pseudonyme que le
   contenu, sans quoi le lien entre les deux est rompu : on part de la table
   de pseudonymes du lot. Seul le patient du dossier y devient « PATIENT ».

3. La lisibilite. « Avis_pneumologique_Dresse_Negrel » doit rester un avis
   pneumologique : specialites, types de documents et services restent en
   clair, seul le nom (titre compris) est remplace.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .engine import Categorie, est_personne
from .roles import PROFESSIONS_SOIGNANTES, SPECIALITES

#: Separateurs usuels des noms de fichiers, remplaces par une espace pour la
#: detection. Tous font un seul caractere : la longueur est preservee.
SEPARATEURS = "_-."


def pour_detection(nom: str) -> str:
    """Copie du nom ou les separateurs deviennent des espaces, meme longueur."""
    return "".join(" " if c in SEPARATEURS else c for c in nom)


def _cle(s: str) -> str:
    """Forme canonique servant a rapprocher deux ecritures d'une meme entite."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


#: Mots qui designent un genre d'etablissement sans en nommer aucun (sans
#: accents, en minuscules).
MOTS_ETABLISSEMENT = frozenset({
    "cabinet", "clinique", "policlinique", "hopital", "hopitaux", "centre",
    "laboratoire", "labo", "institut", "service", "pharmacie", "groupe",
    "permanence", "ems"})

#: Mots de `MOTS_ETABLISSEMENT` qui ne suffisent pas a faire d'un nom un
#: etablissement : « Groupe Mutuel » est un assureur, donc un employeur.
_AMBIGUS = frozenset({"groupe"})


def _mots_sans_accents(forme: str) -> list[str]:
    sans = "".join(c for c in unicodedata.normalize("NFKD", forme)
                   if not unicodedata.combining(c))
    return [m.lower() for m in re.findall(r"[^\W\d_]+", sans)]


def est_mot_etablissement(forme: str) -> bool:
    """Vrai si la forme n'est faite que de mots d'etablissement generiques.

    « Cabinet » n'identifie personne : le substituer dans un nom de fichier
    ou un texte masquerait un mot courant. « Cabinet Dupont » reste a masquer.
    """
    mots = _mots_sans_accents(forme)
    return bool(mots) and all(m in MOTS_ETABLISSEMENT for m in mots)


def contient_mot_etablissement(texte: str) -> bool:
    """Vrai si le texte nomme un etablissement : « Hopital de Pourtales »."""
    return any(m in MOTS_ETABLISSEMENT and m not in _AMBIGUS
               for m in _mots_sans_accents(texte))


#: Voyelles inflechies, transcrites comme on les ecrit sans clavier allemand.
_TREMAS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue",
                         "Ä": "AE", "Ö": "OE", "Ü": "UE"})


def _cle_transcrite(s: str) -> str:
    """Comme `_cle`, mais « ü » vaut « ue » : « Tartempünz » -> « tartempuenz ».

    Un meme nom s'ecrit « Tartempünz » dans le nom du fichier et « TARTEMPUENZ » dans
    un en-tete en capitales, ou encore « Tartempunz ». `_cle` seule ramene le
    premier a « tartempunz » et le second a « tartempuenz » : le nom du patient
    restait alors dans le nom du fichier produit.
    """
    return _cle(unicodedata.normalize("NFC", s).translate(_TREMAS))


def formes_canoniques(s: str) -> list[str]:
    """Les formes canoniques de ``s`` : trema retire, puis trema transcrit.

    Deux ecritures designent la meme entite des qu'elles partagent l'une de
    ces formes : « Tartempünz » rejoint « Tartempunz » par la premiere, « TARTEMPUENZ »
    par la seconde.
    """
    return [c for c in dict.fromkeys((_cle(s), _cle_transcrite(s))) if c]


#: Longueur minimale de la forme canonique d'une entite pour etre cherchee dans
#: un nom de fichier. En deca, les collisions sont massives : « Ele », « Pre »
#: ou « Rue » se retrouvent au milieu de « Electroneuromyographie »,
#: « Prescription » ou « Ruz », et mutilent le nom au lieu de l'anonymiser.
LONGUEUR_MIN = 5

#: Seuil des noms deja trouves comme personnes dans le document lui-meme :
#: « Wu » dans « rapport_Wu.pdf » est le patient, pas un mot courant. Les
#: frontieres de mot restent exigees, donc « wu » dans « Wuhan » est epargne.
LONGUEUR_MIN_PERSONNE = 2


#: Civilites et titres qui precedent frequemment un nom dans un document
#: medical, mais jamais dans un nom de fichier.
TITRES = {
    "m", "mr", "mme", "mlle", "monsieur", "madame", "mademoiselle",
    "dr", "dre", "dresse", "drsse", "docteur", "doctoresse", "pr", "prof",
    "professeur", "med", "méd",
}

#: Formules d'appel, qui precedent un prenom sans en faire partie : « Salut
#: Paul-Machin, » ne doit pas devenir un nom de deux mots, ni « Cher
#: Dominique » un certain « Cher ». Sans accents.
SALUTATIONS = {"salut", "bonjour", "bonsoir", "coucou", "hello", "cher",
               "chere", "merci"}


def _en_tete_de_nom(mot: str) -> bool:
    """Titre ou formule d'appel : un mot qui precede un nom sans en etre."""
    return mot.rstrip(".").lower() in TITRES or _cle(mot) in SALUTATIONS


def sans_titres(texte: str) -> list[str]:
    """Les mots d'un nom, civilites et titres de tete retires.

    « M. Jean-Bidule Zorglub » -> ["Jean-Bidule", "Zorglub"].
    """
    mots = [m for m in re.split(r"[\s,]+", texte.strip()) if m]
    i = 0
    while i < len(mots) and _en_tete_de_nom(mots[i]):
        i += 1
    return mots[i:] if i < len(mots) else []


def composants_du_nom(texte: str) -> list[str]:
    """Prenoms et patronymes d'un nom de personne, sans titre ni initiale."""
    noyaux = (m.strip(".,;:()").strip() for m in sans_titres(texte))
    return [n for n in noyaux
            if len(n) >= 3 and not _en_tete_de_nom(n)]


def variantes(texte: str, etiquette: str = "") -> list[str]:
    """Formes sous lesquelles une entite peut apparaitre dans un nom de fichier.

    Une detection rend « M. Jean-Bidule Zorglub », alors que le fichier
    s'appelle « Jean-Bidule_Zorglub_Ordonnance.pdf ». Sans cette normalisation,
    le rapprochement echoue et le nom de fichier continue de nommer le patient.

    Pour une personne, chaque composant du nom est egalement enregistre : les
    fichiers ne reprennent souvent que le patronyme, ou l'inversent.
    """
    formes: list[str] = []
    brut = texte.strip()
    if brut:
        formes.append(brut)

    mots = [m for m in re.split(r"[\s,]+", brut) if m]
    sans_titre = sans_titres(brut)
    if sans_titre and len(sans_titre) < len(mots):
        formes.append(" ".join(sans_titre))

    if est_personne(etiquette):
        formes += composants_du_nom(brut)

    # Dedoublonner en conservant l'ordre, du plus long au plus court.
    vues: set[str] = set()
    uniques: list[str] = []
    for f in sorted(formes, key=len, reverse=True):
        cle = _cle(f)
        if cle and cle not in vues:
            vues.add(cle)
            uniques.append(f)
    return uniques


# --------------------------------------------------------------------------- #
# Noms de fichiers et de dossiers
#
# Un nom de fichier n'est pas une phrase : « Pneumologie_Dresse_Veronique_
# Negrel_2 » aligne une specialite, un titre, un prenom, un patronyme et un
# rang. La premiere version y cherchait toutes les cles de la table, toutes
# categories confondues, la plus longue d'abord : une detection « employeur »
# qui contenait « Pneumologie » avalait la specialite et le nom du medecin, un
# titre restait ou partait selon la cle trouvee, et la meme medecin devenait
# PERSONNE_3 dans un nom, PERSONNE_60 dans l'autre.
#
# Le nom est donc decoupe en mots, chacun classe : pseudonyme deja pose, titre,
# mot protege (specialite, type de document, service generique), mot outil,
# nombre, rang final, ou candidat. Ne sont remplacees que des suites de mots
# qui nomment une personne connue du lot, titre compris, ou une cle
# d'organisation dont la partie propre figure en entier.
# --------------------------------------------------------------------------- #

#: Titres qui, accoles a un nom dans un nom de fichier, sont absorbes avec lui
#: et font de la personne un professionnel de sante. Sans accents.
TITRES_PROFESSIONNELS = frozenset({
    "dr", "dre", "dresse", "drsse", "docteur", "docteure", "doctoresse",
    "pr", "prof", "professeur", "professeure", "med"})

#: Civilites : absorbees avec le nom qu'elles precedent, sans rien dire du role.
CIVILITES = frozenset({"m", "mr", "mme", "mlle", "monsieur", "madame",
                       "mademoiselle"})

#: Mots outils et particules : jamais un nom a eux seuls.
MOTS_OUTILS = frozenset({
    "de", "du", "des", "d", "la", "le", "les", "l", "et", "en", "a", "au",
    "aux", "pour", "par", "sur", "avec", "sans", "vu", "chez", "un", "une",
    "von", "van", "der", "den", "di", "da", "dos", "del"})

#: Abreviations usuelles d'une specialite dans un nom de fichier.
_ABREVIATIONS_SPECIALITE = {
    "pneumo": "pneumologue", "cardio": "cardiologue", "neuro": "neurologue",
    "gastro": "gastroenterologue", "rhumato": "rhumatologue",
    "dermato": "dermatologue", "onco": "oncologue", "uro": "urologue",
    "nephro": "nephrologue", "gyneco": "gynecologue",
    "endocrino": "endocrinologue", "hemato": "hematologue",
    "ophtalmo": "ophtalmologue", "ophta": "ophtalmologue",
    "radio": "radiologue", "chir": "chirurgien", "angio": "angiologue",
    "allergo": "allergologue", "infectio": "infectiologue",
    "psychiatrique": "psychiatre", "pediatrique": "pediatre",
    "geriatrique": "geriatre", "chirurgical": "chirurgien",
    "chirurgicale": "chirurgien", "orthopedique": "orthopediste",
}


def _specialites_des_noms() -> dict[str, str]:
    """Forme canonique d'une specialite, nom ou adjectif -> specialite.

    `roles.SPECIALITES` ecarte les adjectifs : dans un texte, « bilan
    cardiologique » decrit un examen. Dans un nom de fichier, « Avis
    pneumologique Dresse X » dit bien la specialite de la signataire.
    """
    table: dict[str, str] = {}
    for forme, specialite in SPECIALITES.items():
        cle = _cle(forme)
        table.setdefault(cle, specialite)
        if cle.endswith("logie"):
            table.setdefault(cle[:-5] + "logique", specialite)
    for forme, specialite in _ABREVIATIONS_SPECIALITE.items():
        table.setdefault(forme, specialite)
    return table


SPECIALITES_DES_NOMS = _specialites_des_noms()

#: Types de documents et mots de classement : toujours gardes en clair.
MOTS_DOCUMENTS = frozenset({
    "avis", "rapport", "rapports", "lettre", "lettres", "courrier",
    "courriers", "mail", "email", "ordonnance", "ordonnances", "prescription",
    "bilan", "bilans", "compte", "rendu", "comptes", "rendus", "cr",
    "resultat", "resultats", "analyse", "analyses", "laboratoire", "labo",
    "ecg", "eeg", "emg", "enmg", "consultation", "consultations", "consult",
    "document", "documents", "doc", "docs", "ancien", "ancienne", "anciens",
    "anciennes", "dossier", "dossiers", "copie", "copies", "court", "long",
    "imagerie", "irm", "ct", "scanner", "scan", "radio", "radiographie",
    "echographie", "echo", "sortie", "entree", "hospitalisation", "sejour",
    "certificat", "certificats", "attestation", "facture", "factures",
    "demande", "reponse", "suivi", "note", "notes", "protocole", "examen",
    "examens", "fonction", "fonctions", "pulmonaire", "pulmonaires",
    "spirometrie", "polysomnographie", "oxymetrie", "gazometrie", "epreuve",
    "effort", "holter", "biopsie", "histologie", "pathologie", "cytologie",
    "anamnese", "synthese", "resume", "transmission", "transfert",
    "admission", "formulaire", "questionnaire", "annexe", "annexes", "page",
    "pages", "version", "final", "finale", "nouveau", "nouvelle", "recent",
    "complet", "complete", "partie", "extrait", "divers", "autre", "autres",
    "original", "thorax", "garde", "patient", "patiente", "medecin",
    "medecins", "medecine", "interne", "generale", "famille", "travail",
    "conseil", "specialiste", "fmh", "assurance", "assurances"})

#: Services et genres d'etablissement : gardes en clair seuls, absorbes avec
#: le nom propre d'un etablissement connu (« Hopital_Pourtales »).
MOTS_SERVICES = MOTS_ETABLISSEMENT | frozenset({
    "urgence", "urgences", "policlinique", "polyclinique", "unite",
    "departement", "division", "secteur", "ambulatoire", "hospitalier",
    "hospitaliere", "universitaire", "cabinet", "permanence"})

MOIS = frozenset({
    "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
    "septembre", "octobre", "novembre", "decembre"})

#: Mots jamais remplaces dans un nom, quoi que la table contienne.
MOTS_PROTEGES = (
    frozenset(SPECIALITES_DES_NOMS)
    | frozenset(m for f in SPECIALITES for m in _mots_sans_accents(f))
    | frozenset(_cle(f) for f in PROFESSIONS_SOIGNANTES)
    | MOTS_DOCUMENTS | MOTS_SERVICES | MOIS) - MOTS_OUTILS


def _majuscules(s: str) -> str:
    """Comme `pseudonymes._majuscules` : « sage-femme » -> « SAGE_FEMME »."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.upper().replace(" ", "_").replace("-", "_").replace("'", "")


def _alternance(mots) -> str:
    return "|".join(re.escape(m) for m in sorted(set(mots), key=len,
                                                 reverse=True))


#: Un pseudonyme deja pose : jamais relu comme un nom. « PATIENT » seul est
#: celui que pose ce module.
_PSEUDONYME = re.compile(
    r"(?<![^\W_])(?:(?:"
    + _alternance([_majuscules(c.value) for c in Categorie]
                  + ["PERSONNE", "PATIENT", "MEDECIN", "SOIGNANT"])
    + r")_\d+(?:_(?:"
    + _alternance(_majuscules(v) for v in (*SPECIALITES.values(),
                                           *PROFESSIONS_SOIGNANTES.values()))
    + r"))?|PATIENT)(?![^\W_])")

_PSEUDO, _TITRE, _CIVILITE, _PROTEGE, _OUTIL, _CHIFFRES, _RANG, _CANDIDAT = (
    "pseudo", "titre", "civilite", "protege", "outil", "chiffres", "rang",
    "candidat")

#: Ce qui peut separer deux mots d'un meme nom : « Jean-Bidule_Zorglub »,
#: « J-C._Juvalier ».
_SEPARATEURS_D_UN_NOM = frozenset("_- .,'’")

_ALNUM = re.compile(r"[^\W_]+")


@dataclass
class _Mot:
    texte: str
    debut: int
    fin: int
    #: Colle au mot precedent, sans separateur : « Zq2024 », « VeroniqueNegrel ».
    souple: bool
    cle: str = ""
    cle_tr: str = ""
    classe: str = _CANDIDAT


def _coupure(mot: str, i: int) -> bool:
    """Frontiere entre lettres et chiffres, ou entre deux mots en CamelCase."""
    a, b = mot[i - 1], mot[i]
    if a.isdigit() != b.isdigit():
        return True
    if a.islower() and b.isupper():
        return True
    return (a.isupper() and b.isupper() and i + 1 < len(mot)
            and mot[i + 1].islower())


def _mots_du_nom(texte: str) -> list[_Mot]:
    """Les mots d'un nom (deja en NFC), classes."""
    mots: list[_Mot] = []
    for m in _ALNUM.finditer(texte):
        bloc, base, debut = m.group(), m.start(), 0
        for i in range(1, len(bloc) + 1):
            if i == len(bloc) or _coupure(bloc, i):
                mots.append(_Mot(bloc[debut:i], base + debut, base + i,
                                 souple=debut > 0))
                debut = i
    pseudos = [(m.start(), m.end()) for m in _PSEUDONYME.finditer(texte)]
    for mot in mots:
        mot.cle, mot.cle_tr = _cle(mot.texte), _cle_transcrite(mot.texte)
        if any(d <= mot.debut and mot.fin <= f for d, f in pseudos):
            mot.classe = _PSEUDO
        elif mot.cle in TITRES_PROFESSIONNELS:
            mot.classe = _TITRE
        elif mot.cle in CIVILITES:
            mot.classe = _CIVILITE
        elif mot.texte.isdigit():
            mot.classe = _CHIFFRES
        elif mot.cle in MOTS_PROTEGES:
            mot.classe = _PROTEGE
        elif mot.cle in MOTS_OUTILS or not mot.cle:
            mot.classe = _OUTIL
    if len(mots) >= 2:
        dernier, avant = mots[-1], mots[-2]
        # « Rapport_2 » : un rang, preserve et prolonge en cas de collision.
        if (dernier.classe == _CHIFFRES and len(dernier.texte) <= 3
                and dernier.fin == len(texte)
                and texte[avant.fin:dernier.debut] in ("_", "-", " ")):
            dernier.classe = _RANG
    return mots


def _lie(texte: str, a: _Mot, b: _Mot) -> bool:
    """Les mots consecutifs ``a`` et ``b`` peuvent-ils appartenir au meme nom ?"""
    if b.souple:
        return True
    sep = texte[a.fin:b.debut]
    return 0 < len(sep) <= 3 and all(c in _SEPARATEURS_D_UN_NOM for c in sep)


def _est_professionnel(pseudo: str) -> bool:
    return pseudo.startswith(("MEDECIN_", "SOIGNANT_"))


def _specialites_presentes(texte: str, mots: list[_Mot]) -> list[tuple[int, str]]:
    """(position, specialite) de chaque specialite nommee dans le nom."""
    trouvees = []
    i = 0
    while i < len(mots):
        for long in (3, 2, 1):
            bloc = mots[i:i + long]
            if len(bloc) < long or any(not _lie(texte, a, b)
                                       for a, b in zip(bloc, bloc[1:])):
                continue
            cle = "".join(m.cle for m in bloc)
            if cle in SPECIALITES_DES_NOMS:
                trouvees.append((i, SPECIALITES_DES_NOMS[cle]))
                i += long - 1
                break
        i += 1
    return trouvees


@dataclass
class _Organisation:
    pseudonyme: str
    #: Formes canoniques de la partie propre, du premier au dernier mot
    #: significatif de la cle.
    cibles: frozenset[str]
    #: Mots de la cle, hors partie propre, absorbes s'ils la jouxtent.
    absorbables: frozenset[str]
    significatifs: int


@dataclass
class _Suite:
    """Mots consecutifs du nom remplaces par un seul pseudonyme."""
    debut: int
    fin: int
    identites: set[str] = field(default_factory=set)
    parties: int = 0
    titre: bool = False
    sortie: str = ""


#: Nombre maximal de mots d'un nom propre apres un titre inconnu de la table.
_MOTS_APRES_TITRE = 3


class AnonymiseurNoms:
    """Anonymise les noms de fichiers et de dossiers d'un lot.

    Construit une fois, a la fin du lot, a partir de la table de pseudonymes
    (ou, a defaut, de ses correspondances) :

    - une **personne** est l'ensemble des prenoms et patronymes que la table
      rattache a un meme pseudonyme ; « Negrel », « Veronique_Negrel » et
      « Dresse_Negrel » la designent tous, avec le pseudonyme du texte ;
    - un **titre** accole (Dr, Dresse, Pr...) est absorbe avec le nom et fait
      preferer, parmi les porteurs du nom, un professionnel ; un titre suivi
      d'un nom inconnu de la table nomme un medecin connu du seul nom de
      fichier : MEDECIN_n, avec la specialite lue dans le nom ou le dossier ;
    - les **mots proteges** (specialites, types de documents, services) ne
      sont jamais remplaces ;
    - une **organisation** n'est remplacee que si sa partie propre figure en
      entier dans le nom ;
    - le **patient du dossier** devient « PATIENT », sans numero (voir
      `patient`).

    ``contexte`` : noms des dossiers d'origine. Ils donnent la specialite d'un
    medecin quand le nom du fichier n'en dit rien, et le patient du dossier.
    """

    def __init__(self, correspondances: dict[str, str] | None = None,
                 personnes: frozenset[str] | set[str] = frozenset(), *,
                 table=None, contexte: tuple[str, ...] | list[str] = (),
                 longueur_min: int = LONGUEUR_MIN):
        if correspondances is None:
            correspondances = (table.correspondances if table is not None
                               else {})
        if not personnes and table is not None:
            personnes = getattr(table, "formes_personnes", frozenset())
        self._table = table
        self._citations = dict(getattr(table, "citations", None) or {})
        self._seuil = longueur_min
        self._contexte = [unicodedata.normalize("NFC", c) for c in contexte if c]
        self._nouveaux: dict[frozenset[str], str] = {}
        self._patient_calcule = False
        self._patient: str | None = None

        seuil_personne = min(longueur_min, LONGUEUR_MIN_PERSONNE)
        #: Prenom ou patronyme (forme canonique) -> pseudonymes qui le portent.
        self._parties: dict[str, list[str]] = {}
        #: Parties de personne qui sont aussi des mots proteges (« Court »,
        #: « Long », « Page ») : cle -> pseudonymes. Elles ne sont remplacees
        #: que si la personne s'impose (voir `_reveiller_proteges`).
        self._protegees: dict[str, list[str]] = {}
        #: Cles protegees que la table connait seules, comme forme entiere.
        self._solos: set[str] = set()
        #: Pseudonymes de personnes, dans l'ordre ou la table les a crees.
        self._ordre: dict[str, int] = {}
        for forme, pseudo in correspondances.items():
            if forme not in personnes:
                continue
            self._ordre.setdefault(pseudo, len(self._ordre))
            mots_forme = sans_titres(forme)
            for mot in mots_forme:
                mot = mot.strip(".,;:()")
                if not mot or _en_tete_de_nom(mot):
                    continue
                for cle in formes_canoniques(mot):
                    if (len(cle) < seuil_personne or cle.isdigit()
                            or cle in MOTS_OUTILS
                            or cle in TITRES_PROFESSIONNELS):
                        continue
                    if cle in MOTS_PROTEGES:
                        porteurs = self._protegees.setdefault(cle, [])
                        if pseudo not in porteurs:
                            porteurs.append(pseudo)
                        if len(mots_forme) == 1:
                            self._solos.add(cle)
                    porteurs = self._parties.setdefault(cle, [])
                    if pseudo not in porteurs:
                        porteurs.append(pseudo)

        # Un mot protege seul n'identifie une personne que si elle a aussi un
        # nom ordinaire : « Pneumologie » connu comme seule partie reste clair.
        propres = {p for c, ps in self._parties.items()
                   if c not in self._protegees for p in ps}
        self._solos = {c for c in self._solos
                       if propres & set(self._protegees[c])}

        self._organisations: list[_Organisation] = []
        for forme, pseudo in correspondances.items():
            if forme in personnes:
                continue
            mots = _mots_du_nom(unicodedata.normalize("NFC", forme))
            signif = [i for i, m in enumerate(mots)
                      if m.classe in (_CANDIDAT, _CHIFFRES, _RANG)]
            if not signif:
                # « Cabinet », « Service de pneumologie Dresse » : rien de
                # propre, rien a remplacer.
                continue
            if all(mots[i].classe == _CANDIDAT
                   and (mots[i].cle in self._parties
                        or mots[i].cle_tr in self._parties) for i in signif):
                # « Pneumologie Dresse Negrel » etiquete employeur : c'est une
                # personne, que les noms de personnes traitent.
                continue
            noyau = mots[signif[0]:signif[-1] + 1]
            hors = mots[:signif[0]] + mots[signif[-1] + 1:]
            self._organisations.append(_Organisation(
                pseudo,
                frozenset({"".join(m.cle for m in noyau),
                           "".join(m.cle_tr for m in noyau)}),
                frozenset(m.cle for m in hors
                          if m.classe in (_TITRE, _CIVILITE, _OUTIL)
                          or m.cle in MOTS_SERVICES),
                len(signif)))

    # -- API -------------------------------------------------------------- #

    def nom(self, nom_fichier: str) -> str:
        """Nom de fichier anonymise ; l'extension est preservee."""
        chemin = Path(nom_fichier)
        return self.tige(chemin.stem) + chemin.suffix

    def dossier(self, nom: str) -> str:
        """Nom de dossier anonymise : un dossier n'a pas d'extension."""
        return self.tige(nom)

    def tige(self, tige: str) -> str:
        famille, separateur, rang = self.decomposer(tige)
        return famille + separateur + rang

    def decomposer(self, tige: str) -> tuple[str, str, str]:
        """(famille, separateur, rang) du nom anonymise.

        « Pneumologie_Dresse_Negrel_2 » -> (« Pneumologie_MEDECIN_1_PNEUMOLOGUE »,
        « _ », « 2 »). Le rang d'origine est garde ; les collisions numerotent
        la famille a sa suite (voir `planifier_renommages`).
        """
        # Un nom venu du Mac est souvent decompose : « u » suivi d'un trema
        # combinant, que la lecture caractere par caractere ne sait pas lire.
        texte = unicodedata.normalize("NFC", tige)
        mots = _mots_du_nom(texte)
        suites = self._suites(texte, mots)
        sortie = self._reconstruire(texte, mots, suites) if suites else tige
        if (mots and mots[-1].classe == _RANG
                and all(s.fin < len(mots) - 1 for s in suites)):
            rang = mots[-1].texte
            separateur = texte[mots[-2].fin:mots[-1].debut]
            return sortie[:-len(separateur + rang)], separateur, rang
        return sortie, "", ""

    def patient(self) -> str | None:
        """Pseudonyme du patient du dossier, rendu « PATIENT » dans les noms.

        Dans l'ordre :

        1. la personne que les documents ont reconnue comme patient
           (PATIENT_n) ; s'il y en a plusieurs, celle que nomme le dossier
           d'origine, puis la plus citee ;
        2. a defaut, la personne non soignante que nomme le dossier d'origine ;
        3. a defaut, et seulement pour un lot venu d'un meme dossier, la
           personne non soignante la plus citee, si elle l'est strictement
           plus que les autres.

        Sinon personne : chacun garde son pseudonyme numerote.
        """
        if self._patient_calcule:
            return self._patient
        self._patient_calcule = True
        identites = list(self._ordre)
        patients = [p for p in identites if p.startswith("PATIENT_")]
        du_dossier = [p for p in self._identites_du_contexte()
                      if p.startswith(("PATIENT_", "PERSONNE_"))]
        if patients:
            candidats = [p for p in patients if p in du_dossier] or patients
        elif du_dossier:
            candidats = du_dossier
        elif self._contexte and self._citations:
            candidats = [p for p in identites if p.startswith("PERSONNE_")]
        else:
            candidats = []
        if len(candidats) == 1:
            self._patient = candidats[0]
        elif len(candidats) > 1 and self._citations:
            tri = sorted(candidats, key=lambda p: -self._citations.get(p, 0))
            if self._citations.get(tri[0], 0) > self._citations.get(tri[1], 0):
                self._patient = tri[0]
        return self._patient

    def professionnels_inconnus(self, tige: str) -> dict[str, tuple[str, str]]:
        """Noms precedes d'un titre et inconnus de la table : nom -> role.

        Sert avant le lot : la table apprend ainsi, avant de rendre le premier
        document, que « Negrel » de « Avis_pneumologique_Dresse_Negrel » est
        une pneumologue.
        """
        texte = unicodedata.normalize("NFC", tige)
        mots = _mots_du_nom(texte)
        pris = [False] * len(mots)
        for s in self._suites_personnes(texte, mots):
            for k in range(s.debut, s.fin + 1):
                pris[k] = True
        roles = {}
        for debut, fin, noms in self._titres_inconnus(texte, mots, pris):
            roles[" ".join(m.texte for m in noms)] = (
                "medecin", self._specialite(texte, mots, debut))
        return roles

    # -- Reperage --------------------------------------------------------- #

    def _suites(self, texte: str, mots: list[_Mot]) -> list[_Suite]:
        personnes = self._suites_personnes(texte, mots)
        pris = [False] * len(mots)
        for s in personnes:
            for k in range(s.debut, s.fin + 1):
                pris[k] = True

        retenues: list[_Suite] = []
        for d, f, org in sorted(self._organisations_trouvees(texte, mots),
                                key=lambda t: (t[0] - t[1], t[0])):
            if any(s.debut <= f and d <= s.fin for s in retenues):
                continue
            chevauchees = [s for s in personnes if s.debut <= f and d <= s.fin]
            if chevauchees and org.significatifs <= sum(s.parties
                                                        for s in chevauchees):
                continue
            for s in chevauchees:
                personnes.remove(s)
            retenues.append(_Suite(d, f, sortie=org.pseudonyme))

        for s in personnes:
            s.sortie = self._sortie_personne(texte, mots, s)
        suites = personnes + retenues
        pris = [False] * len(mots)
        for s in suites:
            for k in range(s.debut, s.fin + 1):
                pris[k] = True
        for debut, fin, noms in self._titres_inconnus(texte, mots, pris):
            forme = " ".join(m.texte for m in mots[debut:fin + 1]
                             if m.classe in (_TITRE, _CANDIDAT, _OUTIL))
            suites.append(_Suite(debut, fin, titre=True, sortie=self._professionnel(
                forme, ("medecin", self._specialite(texte, mots, debut)))))
        return sorted((s for s in suites if s.sortie), key=lambda s: s.debut)

    def _reveiller_proteges(self, texte: str, mots: list[_Mot]) -> None:
        """Un mot protege qui est le nom d'une personne de la table, jouxtant
        un autre mot de cette personne (titres exceptes) ou la designant seul,
        redevient candidat : l'identite l'emporte sur la protection.

        « Rapport_Jean_Court » avec la personne « Jean Court » ; mais
        « Court_Cabinet » reste intact si personne ne s'appelle Court.
        """
        for k, mot in enumerate(mots):
            if mot.classe != _PROTEGE or mot.cle not in self._protegees:
                continue
            personnes = set(self._protegees[mot.cle])
            reveille = mot.cle in self._solos
            for pas in (-1, 1):
                if reveille:
                    break
                j = k + pas
                while (0 <= j < len(mots) and mots[j].classe in (_TITRE, _CIVILITE)
                       and _lie(texte, *sorted((mots[j], mots[j - pas]),
                                               key=lambda m: m.debut))):
                    j += pas
                if not 0 <= j < len(mots) or mots[j].classe != _CANDIDAT:
                    continue
                if not _lie(texte, *sorted((mots[j], mots[j - pas]),
                                           key=lambda m: m.debut)):
                    continue
                voisines = set(self._parties.get(mots[j].cle, ()))
                voisines |= set(self._parties.get(mots[j].cle_tr, ()))
                reveille = bool(voisines & personnes)
            if reveille:
                mot.classe = _CANDIDAT

    def _suites_personnes(self, texte: str, mots: list[_Mot]) -> list[_Suite]:
        """Suites de prenoms et patronymes connus, titres de tete compris."""
        if not self._parties:
            return []
        self._reveiller_proteges(texte, mots)
        trouvees: list[tuple[int, int, list[str]]] = []
        i = 0
        while i < len(mots):
            meilleure = None
            if mots[i].classe == _CANDIDAT:
                for attribut in ("cle", "cle_tr"):
                    cumul = ""
                    for j in range(i, min(len(mots), i + 6)):
                        if j > i and not _lie(texte, mots[j - 1], mots[j]):
                            break
                        if mots[j].classe not in (_CANDIDAT, _PROTEGE, _OUTIL):
                            break
                        cumul += getattr(mots[j], attribut)
                        porteurs = self._parties.get(cumul)
                        if porteurs and (meilleure is None or j > meilleure[1]):
                            meilleure = (i, j, porteurs)
            if meilleure:
                trouvees.append(meilleure)
                i = meilleure[1] + 1
            else:
                i += 1

        suites: list[_Suite] = []
        for d, f, porteurs in trouvees:
            if suites:
                s = suites[-1]
                entre = mots[s.fin + 1:d]
                communs = s.identites & set(porteurs)
                if (communs and all(m.classe == _OUTIL for m in entre)
                        and all(_lie(texte, a, b)
                                for a, b in zip(mots[s.fin:d], mots[s.fin + 1:d + 1]))):
                    s.fin, s.identites, s.parties = f, communs, s.parties + 1
                    continue
            suites.append(_Suite(d, f, set(porteurs), 1))

        # Titre de tete, eventuellement separe du nom connu par un ou deux
        # mots inconnus : « Dr_Veronique_Negrel » ou seul « Negrel » est connu.
        limite = -1
        for s in suites:
            k = s.debut - 1
            sautes = 0
            while (k > limite and sautes < 2 and mots[k].classe == _CANDIDAT
                   and _lie(texte, mots[k], mots[k + 1])):
                k, sautes = k - 1, sautes + 1
            t = k
            while (t > limite and mots[t].classe in (_TITRE, _CIVILITE)
                   and _lie(texte, mots[t], mots[t + 1])):
                t -= 1
            if t < k:
                s.debut = t + 1
            s.titre = any(m.classe == _TITRE for m in mots[s.debut:s.fin + 1])
            limite = s.fin
        return suites

    def _organisations_trouvees(self, texte: str, mots: list[_Mot]
                                ) -> list[tuple[int, int, _Organisation]]:
        trouvees = []
        n = len(mots)
        for org in self._organisations:
            for i in range(n):
                if mots[i].classe == _PSEUDO or mots[i].souple:
                    continue
                for attribut in ("cle", "cle_tr"):
                    cumul, fin = "", None
                    for j in range(i, n):
                        if mots[j].classe == _PSEUDO or (
                                j > i and not _lie(texte, mots[j - 1], mots[j])):
                            break
                        cumul += getattr(mots[j], attribut)
                        if not any(c.startswith(cumul) for c in org.cibles):
                            break
                        # Une organisation ne finit pas au milieu d'un bloc :
                        # « Zorglubine2024 » n'est pas « Zorglubine ».
                        if cumul in org.cibles and (j + 1 == n
                                                    or not mots[j + 1].souple):
                            fin = j
                    if fin is None:
                        continue
                    d, f = i, fin
                    while (d > 0 and mots[d - 1].cle in org.absorbables
                           and mots[d - 1].classe != _PSEUDO
                           and _lie(texte, mots[d - 1], mots[d])):
                        d -= 1
                    while (f + 1 < n and mots[f + 1].cle in org.absorbables
                           and mots[f + 1].classe != _PSEUDO
                           and _lie(texte, mots[f], mots[f + 1])):
                        f += 1
                    if len("".join(m.cle for m in mots[d:f + 1])) >= self._seuil:
                        trouvees.append((d, f, org))
                    break
        return trouvees

    def _titres_inconnus(self, texte: str, mots: list[_Mot], pris: list[bool]
                         ) -> list[tuple[int, int, list[_Mot]]]:
        """Titres suivis d'un nom que rien d'autre n'a reconnu."""
        trouves = []
        k = 0
        while k < len(mots):
            if pris[k] or mots[k].classe != _TITRE:
                k += 1
                continue
            debut = k
            while (debut > 0 and not pris[debut - 1]
                   and mots[debut - 1].classe in (_TITRE, _CIVILITE)
                   and _lie(texte, mots[debut - 1], mots[debut])):
                debut -= 1
            j = k
            while (j + 1 < len(mots) and not pris[j + 1]
                   and mots[j + 1].classe in (_TITRE, _CIVILITE)
                   and _lie(texte, mots[j], mots[j + 1])):
                j += 1
            noms: list[_Mot] = []
            fin = j
            while (j + 1 < len(mots) and len(noms) < _MOTS_APRES_TITRE
                   and not pris[j + 1] and _lie(texte, mots[j], mots[j + 1])):
                suivant = mots[j + 1]
                if suivant.classe == _CANDIDAT:
                    noms.append(suivant)
                    j += 1
                    fin = j
                elif suivant.classe == _OUTIL:
                    # Particule : « Dr_de_Montmollin ». Retenue seulement si
                    # un nom la suit.
                    j += 1
                else:
                    break
            if any(len(m.cle) >= 2 for m in noms):
                trouves.append((debut, fin, noms))
                k = fin + 1
            else:
                k += 1
        return trouves

    def _identites_du_contexte(self) -> list[str]:
        trouvees: list[str] = []
        for nom in self._contexte:
            for s in self._suites_personnes(nom, _mots_du_nom(nom)):
                trouvees += [p for p in sorted(s.identites, key=self._ordre.get)
                             if p not in trouvees]
        return trouvees

    # -- Pseudonymes ------------------------------------------------------ #

    def _specialite(self, texte: str, mots: list[_Mot], pres_de: int) -> str:
        """Specialite nommee dans le nom, la plus proche ; sinon du dossier."""
        trouvees = _specialites_presentes(texte, mots)
        if trouvees:
            return min(trouvees, key=lambda t: abs(t[0] - pres_de))[1]
        for nom in self._contexte:
            trouvees = _specialites_presentes(nom, _mots_du_nom(nom))
            if trouvees:
                return trouvees[0][1]
        return ""

    def _sortie_personne(self, texte: str, mots: list[_Mot], s: _Suite) -> str:
        """Pseudonyme d'une suite reconnue : celui du texte, ou « PATIENT »."""
        candidats = sorted(s.identites, key=self._ordre.get)
        patient = self.patient()
        if s.titre:
            pros = [p for p in candidats if _est_professionnel(p)]
            autres = [p for p in candidats
                      if p != patient and not p.startswith("PATIENT_")]
            if pros:
                candidats = pros
            elif autres:
                candidats = autres
            elif s.parties < 2:
                # Seul le patient porte ce nom, et un titre le precede : c'est
                # un medecin homonyme, que le texte ne nomme pas.
                forme = " ".join(m.texte for m in mots[s.debut:s.fin + 1])
                return self._professionnel(
                    forme, ("medecin", self._specialite(texte, mots, s.debut)))
        specialite = (_specialites_presentes(texte, mots) or [(0, "")])[0][1]
        if specialite:
            suffixe = "_" + _majuscules(specialite)
            candidats.sort(key=lambda p: not p.endswith(suffixe))
        elif not s.titre:
            candidats.sort(key=lambda p: not (p == patient
                                              or p.startswith("PATIENT_")))
        choisi = candidats[0]
        return "PATIENT" if choisi == patient else choisi

    def _professionnel(self, forme: str, role: tuple[str, str]) -> str:
        """Pseudonyme d'un professionnel connu du seul nom de fichier."""
        creer = getattr(self._table, "professionnel_du_nom", None)
        if creer is not None:
            return creer(forme, role)
        cle = (frozenset(_cle(c) for c in composants_du_nom(forme))
               or frozenset({_cle(forme)}))
        if cle not in self._nouveaux:
            etiquette = _majuscules(role[0])
            motif = re.compile(rf"{etiquette}_(\d+)")
            existants = [int(m.group(1)) for p in
                         list(self._ordre) + list(self._nouveaux.values())
                         for m in [motif.match(p)] if m]
            numero = max(existants, default=0) + 1
            self._nouveaux[cle] = (f"{etiquette}_{numero}"
                                   + (f"_{_majuscules(role[1])}" if role[1] else ""))
        return self._nouveaux[cle]

    # -- Reconstruction --------------------------------------------------- #

    @staticmethod
    def _reconstruire(texte: str, mots: list[_Mot], suites: list[_Suite]) -> str:
        fusionnees: list[_Suite] = []
        for s in suites:
            # « PERSONNE_1_PERSONNE_1 » -> « PERSONNE_1 » : prenom et patronyme
            # d'une meme personne, separes par une autre cle.
            if (fusionnees and fusionnees[-1].sortie == s.sortie
                    and fusionnees[-1].fin + 1 == s.debut
                    and _lie(texte, mots[s.debut - 1], mots[s.debut])):
                fusionnees[-1].fin = s.fin
            else:
                fusionnees.append(_Suite(s.debut, s.fin, sortie=s.sortie))
        morceaux, position = [], 0
        for s in fusionnees:
            morceaux.append(texte[position:mots[s.debut].debut])
            # Un mot voisin colle sans separateur en recoit un :
            # « PERSONNE_1_2024 », pas « PERSONNE_12024 ».
            if mots[s.debut].souple:
                morceaux.append("_")
            morceaux.append(s.sortie)
            if s.fin + 1 < len(mots) and mots[s.fin + 1].souple:
                morceaux.append("_")
            position = mots[s.fin].fin
        morceaux.append(texte[position:])
        return "".join(morceaux)


def anonymiser_nom(nom_fichier: str, correspondances: dict[str, str] | None = None,
                   longueur_min: int = LONGUEUR_MIN,
                   personnes: frozenset[str] | set[str] = frozenset(), *,
                   table=None, contexte: tuple[str, ...] = ()) -> str:
    """Remplace dans ``nom_fichier`` les entites connues du lot.

    ``correspondances`` associe le texte original a son pseudonyme, par exemple
    ``{"Jean-Noel Zorglub": "PERSONNE_1"}`` ; ``personnes`` en designe les
    cles qui sont des personnes. ``table`` : la `TablePseudonymes` du lot, qui
    fournit les deux, les citations et les pseudonymes des medecins connus du
    seul nom de fichier. Voir `AnonymiseurNoms`.

    L'extension est preservee telle quelle.
    """
    return AnonymiseurNoms(correspondances, personnes, table=table,
                           contexte=contexte,
                           longueur_min=longueur_min).nom(nom_fichier)


def anonymiser_dossier(nom: str, correspondances: dict[str, str] | None = None,
                       longueur_min: int = LONGUEUR_MIN,
                       personnes: frozenset[str] | set[str] = frozenset(), *,
                       table=None, contexte: tuple[str, ...] = ()) -> str:
    """Comme `anonymiser_nom`, pour un nom de dossier.

    Un dossier n'a pas d'extension : « Dupont.Jean » est un nom entier, pas
    un fichier « Dupont » d'extension « .Jean » qu'il faudrait epargner.
    """
    return AnonymiseurNoms(correspondances, personnes, table=table,
                           contexte=contexte,
                           longueur_min=longueur_min).dossier(nom)


def roles_des_noms(noms: list[str], contexte: tuple[str, ...] = ()
                   ) -> dict[str, tuple[str, str]]:
    """Medecins nommes avec un titre dans des noms de fichiers : nom -> role.

    A passer a ``TablePseudonymes.qualifier(..., faible=True)`` avant le lot :
    le texte des documents donne alors a « Dresse Negrel » le meme pseudonyme
    MEDECIN_n_PNEUMOLOGUE que le nom du fichier.
    """
    vide = AnonymiseurNoms({}, contexte=contexte)
    roles: dict[str, tuple[str, str]] = {}
    for nom in noms:
        for forme, role in vide.professionnels_inconnus(Path(nom).stem).items():
            actuel = roles.get(forme)
            if actuel is None or (role[1] and not actuel[1]):
                roles[forme] = role
    return roles


def planifier_renommages(documents: list[tuple[Path, str]],
                         anonymiseur: AnonymiseurNoms) -> list[tuple[Path, Path]]:
    """Noms definitifs des fichiers produits d'un lot, sans collision.

    ``documents`` : (fichier produit sous son nom provisoire, nom d'origine).
    Le texte ecrit a cote (« .txt ») suit son document. Rend les couples
    (chemin actuel, chemin final) a renommer.

    Le nom final se calcule sur le nom **d'origine** : le nom provisoire peut
    porter un « (2) » de circonstance. Deux originaux qui donnent le meme nom
    — « Pneumologie_Dr_X » et « Pneumologie_Dresse_X » — sont departages de
    facon reproductible, dans l'ordre des originaux : d'abord, chaque
    original qui finit par un rang « _n » le garde s'il est libre ; ensuite
    les autres prennent le nom nu s'il est libre, sinon le plus petit « _n »
    libre (n >= 2). Un fichier deja present n'est jamais vise ; la casse ne
    distingue pas deux noms, comme sur un disque macOS ou Windows.
    """
    elements = []
    for provisoire, origine in documents:
        membres = [p for p in dict.fromkeys((provisoire,
                                             provisoire.with_suffix(".txt")))
                   if p.exists()]
        if not membres:
            continue
        famille, separateur, rang = anonymiseur.decomposer(Path(origine).stem)
        elements.append((provisoire, origine, membres, famille, separateur, rang))

    presents: dict[Path, set[str]] = {}
    for dossier in {e[0].parent for e in elements}:
        try:
            presents[dossier] = {n.casefold() for n in os.listdir(dossier)}
        except OSError:
            presents[dossier] = set()
    membres_de = {m.name.casefold(): m for e in elements for m in e[2]}

    ordre = sorted(elements, key=lambda e: (e[1].casefold(), e[1]))
    reserves: set[str] = set()
    attribue: dict[Path, str] = {}

    def pris(e, tige: str) -> bool:
        dossier = e[0].parent
        propres = {m.name.casefold() for m in e[2]}
        noms = {(tige + m.suffix).casefold() for m in e[2]}
        return any(n in reserves or (n in presents[dossier]
                                     and n not in propres) for n in noms)

    def attribuer(e, tige: str) -> None:
        for m in e[2]:
            reserves.add((tige + m.suffix).casefold())
        attribue[e[0]] = tige

    # Passe 1 : qui portait un rang « _n » le garde, le premier servi.
    for e in ordre:
        if e[5] and not pris(e, e[3] + e[4] + e[5]):
            attribuer(e, e[3] + e[4] + e[5])
    # Passe 2 : les autres prennent le nom nu, sinon le plus petit rang libre.
    for e in ordre:
        if e[0] in attribue:
            continue
        tige, n = e[3], 1
        while pris(e, tige):
            n += 1
            tige = f"{e[3]}_{n}"
        attribuer(e, tige)

    plan: list[tuple[Path, Path]] = []
    for e in elements:
        for m in e[2]:
            final = m.with_name(attribue[e[0]] + m.suffix)
            if final != m:
                plan.append((m, final))
    return plan


#: Pauses entre les tentatives de renommage, en secondes.
_PAUSES_RENOMMAGE = (0.1, 0.25, 0.5, 1.0)


def diagnostic_renommage(source: Path, exc: OSError) -> str:
    """Ce qu'on peut dire d'un renommage refuse, sans nommer le fichier.

    macOS repond « Operation not permitted » a un renommage dans son propre
    dossier quand un autre processus tient le fichier (antivirus, indexation)
    ou quand un attribut le protege. Le message de l'exception ne dit rien de
    tout cela : on releve donc l'etat du fichier et qui le tient.
    """
    infos = [f"errno={exc.errno}"]
    try:
        st = source.stat()
        infos.append(f"mode={oct(st.st_mode & 0o7777)} flags={getattr(st, 'st_flags', 0):#x}"
                     f" liens={st.st_nlink} taille={st.st_size}")
        infos.append(f"age={time.time() - st.st_mtime:.1f}s")
    except OSError as e:
        infos.append(f"stat impossible ({e.errno})")
    infos.append(f"dossier_ecriture={os.access(source.parent, os.W_OK)}")
    listxattr = getattr(os, "listxattr", None)
    if listxattr:
        try:
            infos.append("xattr=" + ",".join(listxattr(source)))
        except OSError:
            pass
    try:
        # -F c : un nom de commande par processus, jamais le chemin.
        sortie = subprocess.run(["lsof", "-F", "c", "--", str(source)],
                                capture_output=True, text=True, timeout=5).stdout
        tenants = sorted({l[1:] for l in sortie.splitlines() if l.startswith("c")})
        infos.append("tenu_par=" + (",".join(tenants) or "personne"))
    except (OSError, subprocess.SubprocessError):
        pass
    return " ".join(infos)


def nom_libre(voulu: Path, compagnons: tuple[str, ...] = ()) -> Path:
    """``voulu``, ou « nom (2).ext », « nom (3).ext »… s'il est deja pris.

    Deux fichiers d'origine distincts peuvent recevoir le meme nom anonymise —
    « Dupont.pdf » et « Jean Dupont.pdf » deviennent tous deux
    « PATIENT_1.pdf » —, et le dossier de sortie peut garder celui d'une
    execution precedente. Sous macOS le renommage ecrasait l'autre document
    sans rien dire ; sous Windows il levait FileExistsError, une fois tout le
    lot traite.

    ``compagnons`` : extensions des fichiers ecrits a cote du document, qui
    doivent etre libres eux aussi. « Lettre.pdf » et « Lettre.docx » du meme
    lot ecrivaient tous deux « Lettre.txt », et le second ecrasait le premier.
    """
    def pris(chemin: Path) -> bool:
        return chemin.exists() or any(chemin.with_suffix(s).exists()
                                      for s in compagnons)

    candidat, rang = voulu, 2
    while pris(candidat):
        candidat = voulu.with_name(f"{voulu.stem} ({rang}){voulu.suffix}")
        rang += 1
    return candidat


def renommer(source: Path, final: Path, journal: list[str] | None = None) -> Path:
    """Renomme un fichier produit, en reessayant si le systeme le refuse.

    Un refus passager (``PermissionError``) disparait en quelques centiemes de
    seconde quand un processus lache le fichier. S'il persiste, on copie puis
    on supprime l'original : l'ancien nom, qui porte le nom du patient, ne doit
    pas survivre. Si la suppression echoue aussi, l'erreur d'origine remonte.

    Un nom deja pris n'est jamais ecrase (voir `nom_libre`) : rend le chemin
    effectivement donne.
    """
    final = nom_libre(final)
    derniere: OSError | None = None
    for essai, pause in enumerate((0.0,) + _PAUSES_RENOMMAGE):
        if pause:
            time.sleep(pause)
        try:
            source.rename(final)
        except PermissionError as exc:
            derniere = exc
            if journal is not None and essai == 0:
                journal.append("renommage refuse : "
                               + diagnostic_renommage(source, exc))
        else:
            if derniere is not None and journal is not None:
                journal.append(f"renommage reussi a l'essai {essai + 1}")
            return final
    try:
        shutil.copyfile(source, final)
        source.unlink()
    except OSError:
        final.unlink(missing_ok=True)
        raise derniere
    if journal is not None:
        journal.append("renommage impossible : copie puis suppression utilisees")
    return final
