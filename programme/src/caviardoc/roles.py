"""Qui est le patient, qui est le soignant.

Masquer le nom du medecin traitant comme celui du patient est parfois excessif.
Le confrere referent est une information professionnelle, publique dans
l'annuaire FMH ; le patient, lui, est la personne a proteger. Distinguer les
deux permet un caviardage moins destructeur, qui laisse le document lisible et
exploitable par un confrere ou par un modele externe.

Le probleme est d'identifier le patient sans se tromper. Trois signaux sont
combines, du plus fiable au moins fiable :

1. **La date de naissance accolee.** Un nom suivi de « ne le 23.04.1931 » est
   celui du patient, presque sans exception. C'est le signal le plus sur : un
   en-tete de medecin ne porte jamais sa propre date de naissance.
2. **Les mentions explicites** : « concernant : », « le patient », « Nom et
   prenom ».
3. **Les titres professionnels** — « Dr », « Dre », « FMH », « Specialiste » —
   qui disqualifient au contraire une personne comme patient.

En cas de doute, **on masque**. Une personne non classee reste caviardee : le
niveau « patient » reduit le sur-masquage, il ne doit pas creer de fuite.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace
from enum import Enum

from .engine import Span


class Niveau(Enum):
    """Etendue du caviardage."""

    #: Tout ce qui est detecte est masque. Le plus sur.
    COMPLET = "complet"
    #: Seules les donnees identifiant le patient sont masquees. Les soignants,
    #: les etablissements et leurs coordonnees restent lisibles.
    PATIENT = "patient"


#: Mentions qui designent le patient, cherchees avant le nom.
MARQUEURS_PATIENT = (
    "concernant", "patient", "patiente", "nom et prenom", "assure", "assuree",
    "resident", "residente", "beneficiaire",
)

#: Titres qui designent un professionnel de sante.
MARQUEURS_PROFESSIONNEL = (
    "dr", "dre", "dresse", "docteur", "doctoresse", "pr", "prof", "professeur",
    "med", "fmh", "specialiste", "medecin", "confrere", "consoeur",
    "infirmier", "infirmiere", "physiotherapeute", "responsable",
)

PORTEE_AVANT = 70
PORTEE_APRES = 60

#: Nombre de mots examines avant un nom pour y chercher un titre. Un seul ne
#: suffit pas : « Au medecin responsable du patient / COSTA Ana » place le
#: titre a quatre mots de distance, et le dernier mot avant le nom y est
#: justement « patient ».
MOTS_AVANT_TITRE = 4

#: Ce qui peut separer un nom de sa date de naissance : de la ponctuation, des
#: blancs, et eventuellement la mention qui l'introduit. Rien d'autre.
#:
#: Une simple fenetre de proximite ne suffit pas. Sur un rapport de
#: readaptation, la date de naissance figure dans l'en-tete de chaque page :
#: tout nom situe a moins de soixante caracteres devenait « le patient », y
#: compris des soignants et « Tiffeneau », qui est un indice spirometrique.
SEPARATEUR_NAISSANCE = re.compile(
    r"^[\s,;:.()\-\u2013\u2014]*"
    r"(?:n[ée]{1,2}\(?e?\)?\s+le|date\s+de\s+naissance|ddn|le)?"
    r"[\s,;:.()\-]*$",
    re.IGNORECASE)

#: Fenetre, apres le nom, ou chercher une qualification entre parentheses.
#: Les tableaux hospitaliers qualifient le soignant apres son nom plutot
#: qu'avant : « LAMY AMANDINE (PRT) », « COSTA Ana (Medecin hospitalier) ».
PORTEE_QUALIFICATIF = 40

_MOT = re.compile(r"[a-z]+")


def _sans_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _mots(s: str) -> list[str]:
    return _MOT.findall(_sans_accents(s))


def est_professionnel(texte: str, span: Span) -> bool:
    """Le nom designe-t-il un soignant plutot que le patient ?

    Trois indices, tous locaux au nom :

    1. un titre dans le nom lui-meme — « Dr Mercier », « DRE FERREIRA » ;
    2. une qualification entre parentheses juste apres — c'est la forme des
       tableaux hospitaliers, ou le soignant est nomme puis qualifie :
       « LAMY AMANDINE (PRT) » ;
    3. un titre parmi les quelques mots qui precedent.

    Le troisieme indice regarde plusieurs mots en arriere et non le seul mot
    adjacent. Un rapport reel adressait « Au medecin responsable du patient »
    puis nommait le destinataire a la ligne suivante : le mot colle au nom
    etait « patient », et le destinataire — un medecin — etait pris pour le
    patient lui-meme.
    """
    propres = _mots(span.text)[:2]
    if any(m in MARQUEURS_PROFESSIONNEL for m in propres):
        return True

    apres = texte[span.end:span.end + PORTEE_QUALIFICATIF]
    entre_parentheses = re.match(r"\s*\(([^)]{1,40})\)", apres)
    if entre_parentheses:
        qualif = _mots(entre_parentheses.group(1))
        if any(m in MARQUEURS_PROFESSIONNEL for m in qualif):
            return True
        # Un sigle court entre parentheses juste apres un nom qualifie une
        # fonction : « (PRT) », « (IDE) ». Il n'identifie pas une personne.
        brut = entre_parentheses.group(1).strip()
        if brut.isupper() and 2 <= len(brut) <= 5:
            return True

    avant = _mots(texte[max(0, span.start - PORTEE_AVANT):span.start])
    return any(m in MARQUEURS_PROFESSIONNEL for m in avant[-MOTS_AVANT_TITRE:])


def _porte_une_naissance(texte: str, span: Span,
                         naissances: list[Span]) -> bool:
    """Une date de naissance suit-elle immediatement ce nom ?

    « Immediatement » veut dire : rien entre les deux qu'une ponctuation, des
    blancs, ou la mention qui introduit la date. Un autre mot rompt le lien.
    """
    for d in naissances:
        ecart = d.start - span.end
        if 0 <= ecart <= PORTEE_APRES:
            if SEPARATEUR_NAISSANCE.match(texte[span.end:d.start]):
                return True
    return False


def _mention_de_patient(texte: str, span: Span) -> bool:
    """Une mention explicite designe-t-elle ce nom comme le patient ?"""
    avant = _sans_accents(texte[max(0, span.start - PORTEE_AVANT):span.start])
    return any(m in avant for m in MARQUEURS_PATIENT)


def est_patient(texte: str, span: Span, dates_naissance: list[Span]) -> bool:
    """Le nom designe-t-il le patient ?

    Conserve pour l'usage direct sur un span isole. `noms_du_patient` lui
    prefere un raisonnement a l'echelle du document, qui n'emploie les
    mentions textuelles qu'a defaut de date de naissance.
    """
    if est_professionnel(texte, span):
        return False
    return (_porte_une_naissance(texte, span, dates_naissance)
            or _mention_de_patient(texte, span))


def noms_du_patient(texte: str, spans: list[Span]) -> set[str]:
    """Chaines qui nomment le patient dans ce texte.

    La date de naissance accolee est un signal bien plus sur que toute mention
    textuelle : un en-tete de soignant ne porte jamais sa propre date de
    naissance, alors qu'un dossier medical emploie le mot « patient » a chaque
    paragraphe. Quand au moins un nom porte ce signal, **lui seul est retenu**
    et les mentions textuelles sont ignorees. Elles ne servent que de repli,
    pour les documents ou aucune date de naissance n'apparait.
    """
    from .pipeline import est_date_de_naissance

    naissances = [s for s in spans
                  if s.est_date() and est_date_de_naissance(texte, s)]

    personnes = [sp for sp in spans
                 if sp.est_personne() and not est_professionnel(texte, sp)]

    par_naissance = {sp.text.strip() for sp in personnes
                     if _porte_une_naissance(texte, sp, naissances)}
    mentions = {sp.text.strip() for sp in personnes
                if _mention_de_patient(texte, sp)}
    if par_naissance:
        # Etiquette du modele seule (fausse environ une fois sur trois) :
        # signature ou date d'impression possibles. On protege alors aussi
        # les mentions textuelles, l'union favorisant le masquage.
        confirmees = [s for s in naissances
                      if est_date_de_naissance(texte, replace(s, label="date"))]
        if confirmees:
            return par_naissance
        return par_naissance | mentions

    return mentions


def _contient(hote: str, aiguille: str) -> bool:
    return _sans_accents(aiguille) in _sans_accents(hote)


#: Taille maximale d'un bloc d'identite patient. Au-dela, on ne suit plus une
#: adresse mais le corps du document.
BLOC_MAX = 400


def _blocs_patient(texte: str, noms_patient: set[str]) -> list[tuple[int, int]]:
    """Etendue du bloc d'identite autour de chaque occurrence du nom du patient.

    Le bloc court du debut de la ligne portant le nom jusqu'au prochain saut de
    paragraphe. Se limiter a la ligne suivante ne suffit pas : une adresse
    patient tient couramment sur trois ou quatre lignes — nom, rue, localite,
    telephone — et le telephone restait alors lisible.
    """
    plat = _sans_accents(texte)
    zones: list[tuple[int, int]] = []
    for nom in noms_patient:
        cible = _sans_accents(nom)
        depart = 0
        while True:
            i = plat.find(cible, depart)
            if i == -1:
                break
            debut = texte.rfind("\n", 0, i) + 1
            fin = texte.find("\n\n", i)
            fin = len(texte) if fin == -1 else fin
            zones.append((debut, min(fin, debut + BLOC_MAX)))
            depart = i + 1
    return zones


def filtrer_par_role(texte: str, spans: list[Span], niveau: Niveau,
                     noms_patient: set[str]) -> list[Span]:
    """Au niveau « patient », ne conserve que ce qui identifie le patient.

    Une entite est masquee si elle nomme le patient, si elle est un
    identifiant qui lui est propre, ou si elle figure dans le meme voisinage
    immediat qu'un de ses noms — c'est ainsi que l'adresse et le telephone du
    bloc patient sont emportes, sans toucher a ceux de l'en-tete du cabinet.
    """
    if niveau is Niveau.COMPLET:
        return spans

    zones = _blocs_patient(texte, noms_patient)

    gardes: list[Span] = []
    for sp in spans:
        if sp.est_identifiant_personnel():
            gardes.append(sp)
            continue
        if any(_contient(sp.text, n) or _contient(n, sp.text)
               for n in noms_patient if len(n) >= 4):
            gardes.append(sp)
            continue
        if sp.est_organisation():
            continue  # etablissement : conserve
        if sp.est_personne():
            # Un patient est identifie : seul un soignant positivement
            # reconnu reste lisible, une personne non classee (proche,
            # accompagnant) reste masquee. Sans patient identifie, le filtre
            # ne garde rien et l'appelant se replie sur le niveau complet.
            if noms_patient and not est_professionnel(texte, sp):
                gardes.append(sp)
            continue
        if any(d <= sp.start < f for d, f in zones):
            gardes.append(sp)  # adresse, telephone, date du bloc patient

    return gardes


# --------------------------------------------------------------------------- #
# Qualifier les personnes masquees
#
# Dans le texte exporte, « PERSONNE_3 » ne dit pas a un modele de langage qui
# est le patient et qui l'a examine : un rapport AI redige a partir de ce texte
# confond alors le rhumatologue et le chirurgien. « MEDECIN_2_RHUMATOLOGUE »
# rend cette structure sans rien reveler de plus que le document lui-meme, ou
# la specialite figure deja en clair a cote du nom.
# --------------------------------------------------------------------------- #

#: Formes ecrites (sans accents, minuscules) -> specialite du pseudonyme. Des
#: mots entiers plutot que des racines : « examen neurologique » ou « bilan
#: cardiologique » decrivent un examen, pas la personne nommee a cote.
SPECIALITES: dict[str, str] = {
    **dict.fromkeys(("rhumatologue", "rhumatologie"), "rhumatologue"),
    **dict.fromkeys(("cardiologue", "cardiologie"), "cardiologue"),
    **dict.fromkeys(("neurologue", "neurologie"), "neurologue"),
    **dict.fromkeys(("psychiatre", "psychiatrie"), "psychiatre"),
    **dict.fromkeys(("pneumologue", "pneumologie"), "pneumologue"),
    **dict.fromkeys(("gastroenterologue", "gastroenterologie",
                     "gastro-enterologue", "gastro-enterologie"),
                    "gastroenterologue"),
    **dict.fromkeys(("endocrinologue", "endocrinologie"), "endocrinologue"),
    **dict.fromkeys(("diabetologue", "diabetologie"), "diabetologue"),
    **dict.fromkeys(("nephrologue", "nephrologie"), "nephrologue"),
    **dict.fromkeys(("oncologue", "oncologie"), "oncologue"),
    **dict.fromkeys(("hematologue", "hematologie"), "hematologue"),
    **dict.fromkeys(("dermatologue", "dermatologie"), "dermatologue"),
    **dict.fromkeys(("ophtalmologue", "ophtalmologie"), "ophtalmologue"),
    **dict.fromkeys(("orl", "oto-rhino-laryngologie"), "orl"),
    **dict.fromkeys(("orthopediste", "orthopedie", "orthopedique"),
                    "orthopediste"),
    **dict.fromkeys(("neurochirurgien", "neurochirurgienne", "neurochirurgie"),
                    "neurochirurgien"),
    **dict.fromkeys(("chirurgien", "chirurgienne", "chirurgie"), "chirurgien"),
    **dict.fromkeys(("gynecologue", "gynecologie", "obstetrique"),
                    "gynecologue"),
    **dict.fromkeys(("pediatre", "pediatrie"), "pediatre"),
    **dict.fromkeys(("geriatre", "geriatrie"), "geriatre"),
    **dict.fromkeys(("urologue", "urologie"), "urologue"),
    **dict.fromkeys(("radiologue", "radiologie"), "radiologue"),
    **dict.fromkeys(("anesthesiste", "anesthesiologie", "anesthesie"),
                    "anesthesiste"),
    **dict.fromkeys(("angiologue", "angiologie"), "angiologue"),
    **dict.fromkeys(("allergologue", "allergologie"), "allergologue"),
    **dict.fromkeys(("infectiologue", "infectiologie"), "infectiologue"),
    **dict.fromkeys(("physiatre", "readaptation"), "physiatre"),
    **dict.fromkeys(("generaliste", "medecine generale", "medecine de famille",
                     "medecin de famille", "medecine interne generale"),
                    "generaliste"),
    **dict.fromkeys(("interniste", "medecine interne"), "interniste"),
    **dict.fromkeys(("medecin-conseil", "medecin conseil"), "conseil"),
    **dict.fromkeys(("medecine du travail", "medecin du travail"), "travail"),
}

#: Professions de sante qui ne sont pas medicales : la personne est alors un
#: soignant, quel que soit son titre — une psychologue docteure reste une
#: psychologue.
PROFESSIONS_SOIGNANTES: dict[str, str] = {
    **dict.fromkeys(("physiotherapeute", "physiotherapie"),
                    "physiotherapeute"),
    **dict.fromkeys(("ergotherapeute", "ergotherapie"), "ergotherapeute"),
    **dict.fromkeys(("infirmier", "infirmiere"), "infirmier"),
    **dict.fromkeys(("psychologue", "psychotherapeute"), "psychologue"),
    **dict.fromkeys(("dieteticien", "dieteticienne"), "dieteticien"),
    **dict.fromkeys(("logopediste",), "logopediste"),
    **dict.fromkeys(("sage-femme",), "sage-femme"),
    **dict.fromkeys(("pharmacien", "pharmacienne"), "pharmacien"),
    **dict.fromkeys(("osteopathe",), "osteopathe"),
    **dict.fromkeys(("podologue",), "podologue"),
}

#: Titres qui font d'une personne un medecin plutot qu'un soignant.
TITRES_MEDICAUX = frozenset({
    "dr", "dre", "dresse", "docteur", "doctoresse", "pr", "prof", "professeur",
    "med", "fmh", "medecin", "specialiste", "confrere", "consoeur",
})

#: Ce qui qualifie un poste dans un en-tete, sans rien dire d'un patient :
#: « Medecin hospitalier », « Chef de clinique », « Medecin adjoint ».
QUALIFICATIFS_DE_POSTE = frozenset({
    "hospitalier", "hospitaliere", "chef", "cheffe", "adjoint", "adjointe",
    "assistant", "assistante", "clinique", "cadre", "agree", "agreee",
    "consultant", "consultante", "responsable", "service", "specialise",
    "specialisee", "en", "de", "du", "des", "la", "le", "et", "ch",
})


def est_poste_soignant(texte: str) -> bool:
    """Le texte ne nomme-t-il qu'un poste ou une specialite medicale ?

    « Specialiste en ophtalmologie », « Medecine Interne Generale (CH) » : le
    modele les rend en « profession », et ils figurent dans l'en-tete de
    presque chaque courrier. Ils designent l'expediteur, pas le patient.

    Les professions de sante non medicales (`PROFESSIONS_SOIGNANTES`) n'y sont
    pas : « infirmiere de 45 ans » decrit souvent la patiente elle-meme. Un
    patient medecin, lui, n'est plus signale par sa profession — l'age et le
    reste le sont encore.
    """
    bas = _sans_accents(texte)
    mots = _MOT.findall(_SPECIALITE.sub(" ", bas))
    if not mots and not _SPECIALITE.search(bas):
        return False
    return all(m in TITRES_MEDICAUX or m in QUALIFICATIFS_DE_POSTE
               for m in mots)


#: Fenetres ou chercher la specialite d'un soignant. Apres le nom d'abord :
#: c'est la forme des en-tetes, « Dr Jean Mercier / Specialiste FMH en
#: rhumatologie ». Avant ensuite : « le rhumatologue, Dr Mercier ».
PORTEE_SPECIALITE_APRES = 90
PORTEE_SPECIALITE_AVANT = 50

_EXPRESSIONS = sorted({**SPECIALITES, **PROFESSIONS_SOIGNANTES},
                      key=len, reverse=True)
_METIER = re.compile(r"(?<![a-z-])(?:"
                     + "|".join(re.escape(e) for e in _EXPRESSIONS)
                     + r")(?![a-z-])")
#: Les seules specialites medicales, sans les professions de sante : voir
#: `est_poste_soignant`.
_SPECIALITE = re.compile(r"(?<![a-z-])(?:"
                         + "|".join(re.escape(e) for e in
                                    sorted(SPECIALITES, key=len, reverse=True))
                         + r")(?![a-z-])")

#: Role d'une personne : « patient », « medecin », « soignant », avec
#: eventuellement une precision (la specialite).
Role = tuple[str, str]


def _metier(extrait: str, dernier: bool = False) -> Role | None:
    """Metier nomme dans cet extrait, le plus proche du nom."""
    trouves = list(_METIER.finditer(_sans_accents(extrait)))
    if not trouves:
        return None
    mot = (trouves[-1] if dernier else trouves[0]).group(0)
    if mot in PROFESSIONS_SOIGNANTES:
        return "soignant", PROFESSIONS_SOIGNANTES[mot]
    return "medecin", SPECIALITES[mot]


def _role_professionnel(texte: str, span: Span,
                        voisins: list[Span]) -> Role:
    """Medecin ou soignant, et sa specialite quand le texte la donne a cote.

    La fenetre s'arrete au nom suivant (ou precedent) : dans un en-tete qui
    aligne deux medecins et leurs deux specialites, chacun garde la sienne.
    """
    fin = min([span.end + PORTEE_SPECIALITE_APRES]
              + [v.start for v in voisins if v.start >= span.end])
    apres = texte[span.end:fin].split("\n\n")[0]
    debut = max([span.start - PORTEE_SPECIALITE_AVANT, 0]
                + [v.end for v in voisins if v.end <= span.start])
    avant = texte[debut:span.start].split("\n\n")[-1]

    metier = _metier(apres) or _metier(avant, dernier=True)
    if metier is not None:
        return metier

    proches = (_mots(span.text)[:MOTS_AVANT_TITRE]
               + _mots(texte[max(0, span.start - PORTEE_AVANT):span.start])
               [-MOTS_AVANT_TITRE:])
    if any(m in TITRES_MEDICAUX for m in proches):
        return "medecin", ""
    return "soignant", ""


def _titre_en_tete(nom: str) -> bool:
    """« Mr le Docteur MERCIER » : le titre est dans le nom, mais au troisieme
    mot. `est_professionnel` ne regarde que les deux premiers, et n'a pas a
    changer : il decide de ce qui reste masque. Ici, se tromper ne coute rien.
    """
    return any(m in MARQUEURS_PROFESSIONNEL
               for m in _mots(nom)[:MOTS_AVANT_TITRE])


#: Ordre de preference quand un meme nom recoit plusieurs roles dans le lot.
_PRIORITE = {"patient": 3, "medecin": 1, "soignant": 1}


def qualifier_personnes(unites: list[tuple[str, list[Span]]]) -> dict[str, Role]:
    """Role de chaque personne masquee, pour nommer son pseudonyme.

    Le patient est reconnu a l'echelle de toutes les unites, comme pour le
    filtrage par role. Un soignant garde la specialite trouvee a l'une
    quelconque de ses occurrences : l'en-tete la donne, le corps du texte ne
    la repete pas. Une personne ni patiente ni soignante — un proche — n'est
    pas qualifiee et reste « PERSONNE ».

    Se tromper ici ne fait rien fuir : le role remplace un nom deja masque.
    """
    patients: set[str] = set()
    for texte, spans in unites:
        patients |= noms_du_patient(texte, spans)

    roles: dict[str, Role] = {}

    def noter(nom: str, role: Role) -> None:
        actuel = roles.get(nom)
        if (actuel is None
                or _PRIORITE[role[0]] > _PRIORITE[actuel[0]]
                or (role[0] == actuel[0] and role[1] and not actuel[1])):
            roles[nom] = role

    for texte, spans in unites:
        personnes = [sp for sp in spans if sp.est_personne()]
        for sp in personnes:
            nom = sp.text.strip()
            if nom in patients:
                noter(nom, ("patient", ""))
            elif (est_professionnel(texte, sp)
                  or _titre_en_tete(sp.text)):
                voisins = [v for v in personnes if v is not sp]
                noter(nom, _role_professionnel(texte, sp, voisins))
    return roles
