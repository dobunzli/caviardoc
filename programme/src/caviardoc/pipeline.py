"""Chaine de detection complete : couche 1 (regles) + couche 2 (modele), avec decoupage.

Le decoupage est indispensable : l'encodeur mdeberta-v3-base a une fenetre de
384 tokens. Sans lui, tout ce qui depasse la fenetre n'est jamais analyse — en
silence — sans erreur, sans avertissement, et sans que rien ne le laisse
voir dans le document produit.

Les blocs se recouvrent pour qu'une entite a cheval sur une frontiere soit vue
entiere par au moins un bloc. Les offsets sont ensuite ramenes en coordonnees
globales, puis dedoublonnes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from .detect import rules
from .detect.generique import est_generique
from .engine import (DEFAULT_LABELS, DEFAULT_THRESHOLD, QUASI_LABELS,
                     SEUIL_QUASI, Categorie, Origine, Span, apply_spans,
                     detect, resolve_overlaps)
from .noms import (MOTS_ETABLISSEMENT, SALUTATIONS, TITRES,
                   contient_mot_etablissement)
from .normalise import normalise_casse
from .roles import (MARQUEURS_PROFESSIONNEL, PORTEE_APRES,
                    SEPARATEUR_NAISSANCE, est_poste_soignant, est_professionnel)

#: Blocs volontairement courts — environ 100 tokens, bien en deca de la
#: fenetre de 384 du modele.
#:
#: Mesure sur corpus medical reel (4 documents, 19 occurrences du nom du
#: patient) : des blocs de 1000 caracteres ne masquent que 11 occurrences sur
#: 19. Des blocs de 350 en masquent 18, et 19 avec un seuil abaisse. La raison
#: est que GLiNER note les spans relativement a leur contexte : dans un
#: document ou l'en-tete du medecin domine, le nom du patient tombe a 0,32
#: alors qu'il atteint 0,95 sur sa ligne seule. Reduire le contexte le remonte.
#:
#: Le surcout est negligeable : 3,3 s contre 3,1 s sur le meme corpus.
CHUNK_CHARS = 350
OVERLAP_CHARS = 90


@dataclass(frozen=True)
class Chunk:
    text: str
    offset: int


def split_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = OVERLAP_CHARS) -> list[Chunk]:
    """Decoupe en blocs recouvrants, en coupant de preference sur un blanc."""
    if len(text) <= size:
        return [Chunk(text, 0)]

    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Reculer jusqu'au dernier blanc pour ne pas couper un mot.
            window = text.rfind("\n", start + size // 2, end)
            if window == -1:
                window = text.rfind(" ", start + size // 2, end)
            if window != -1:
                end = window
        chunks.append(Chunk(text[start:end], start))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _rules_to_spans(text: str) -> list[Span]:
    return [Span(m.start, m.end, m.label, m.score, m.text, Origine.REGLE.value)
            for m in rules.find_all(text)]


class PolitiqueDates(Enum):
    """Que faire des dates trouvees dans un document."""

    #: Tout masquer. Le choix le plus sur, et le defaut historique.
    MASQUER = "masquer"
    #: Conserver les dates, **sauf** la date de naissance.
    GARDER = "garder"
    #: Tout conserver, date de naissance comprise.
    GARDER_TOUT = "garder-tout"
    #: Remplacer chaque date par son ecart a la plus ancienne : J0, J+4.
    #: La date de naissance reste masquee. Voir `dates_relatives`.
    RELATIVES = "relatives"


#: Mentions qui, juste avant une date, en font une date de naissance.
MARQUEURS_NAISSANCE = (
    "ne le", "nee le", "ne(e) le", "né le", "née le", "né(e) le",
    "date de naissance", "naissance", "ddn", "naiss.", "naiss",
)

#: Fenetre, en caracteres, ou l'on cherche ces mentions avant la date.
PORTEE_MARQUEUR = 40

#: Ce qui peut separer le marqueur de la date : espaces et ponctuation, rien
#: d'autre. Sans cette exigence, le « ne le » d'une ligne precedente ferait
#: passer la date de prelevement pour une date de naissance — le cas s'est
#: presente des le premier test.
_SEPARATEUR = re.compile(r"^[\s:;,.\u2013\u2014-]*$")


#: Mots qui font d'un « nom » un etablissement. Le modele etiquette volontiers
#: « personne » un « Cabinet Medical Dupont Martin » de pied de page, suivi de
#: sa date d'impression : ce n'est pas un patient suivi de sa naissance.
ETABLISSEMENTS = MOTS_ETABLISSEMENT


def _est_etablissement(nom: str) -> bool:
    return any(_sans_accents(m) in ETABLISSEMENTS
               for m in re.findall(r"[^\W\d_]+", nom))


#: Une heure juste apres la date : « 30.09.2026, 14:32 ». C'est un horodatage
#: — impression, signature, prelevement —, jamais une date de naissance.
_HEURE_APRES = re.compile(r"^[\s,;]*(?:a\s+|à\s+)?\d{1,2}\s*[:h]\s*\d{2}")


def est_date_de_naissance(texte: str, span: Span,
                          personnes: list[Span] | None = None) -> bool:
    """La date designee par ``span`` est-elle une date de naissance ?

    Trois indices, du plus sur au moins sur :

    1. la mention qui precede immediatement la date — un « ne le » ou un
       « Date de naissance : » ne laisse aucune ambiguite ;
    2. un nom de personne juste avant elle, comme dans un en-tete
       « Dupont Jean 26.05.1955 » (meme critere que `roles` pour reconnaitre
       le patient) — seulement si ``personnes`` est donne ;
    3. l'etiquette du modele, qui hesite souvent entre « date » et « date de
       naissance ».

    Une date accolee au seul nom d'un soignant ou d'un etablissement n'est
    jamais une date de naissance, quelle que soit l'etiquette du modele.

    Le troisieme ne suffit plus seul quand ``personnes`` est donne. Sur un
    tableau d'antecedents reel, le modele etiquetait « date de naissance » une
    date sur trois de la colonne Date : « la date de naissance seulement »
    les masquait presque toutes. Il n'est alors retenu que pour une annee a
    quatre chiffres sans heure accolee — la forme d'une date de naissance,
    pas celle d'un antecedent date « 15.9.11 » ni d'un horodatage.
    """
    debut_fenetre = max(0, span.start - PORTEE_MARQUEUR)
    avant = texte[debut_fenetre:span.start].lower()
    for marqueur in MARQUEURS_NAISSANCE:
        i = avant.rfind(marqueur)
        if i == -1:
            continue
        # Le marqueur doit toucher la date : seuls des blancs ou de la
        # ponctuation peuvent s'intercaler.
        if _SEPARATEUR.match(avant[i + len(marqueur):]):
            return True

    if personnes is None:
        return span.est_naissance()

    accolees = [p for p in personnes
                if 0 <= span.start - p.end <= PORTEE_APRES
                and SEPARATEUR_NAISSANCE.match(texte[p.end:span.start])]
    if any(not est_professionnel(texte, p) and not _est_etablissement(p.text)
           for p in accolees):
        return True
    if accolees:
        # Accolee a un soignant ou a un etablissement, et a lui seul : c'est
        # une date d'impression ou de signature. Un soignant ne porte jamais
        # sa propre date de naissance (voir `roles.noms_du_patient`).
        return False

    annee = re.split(r"[./-]", span.text.strip())[-1]
    return (span.est_naissance() and len(annee) == 4
            and not _HEURE_APRES.match(texte[span.end:]))


def _etendre_au_jeton(texte: str, debut: int, fin: int) -> tuple[int, int]:
    """Etend ``[debut:fin]`` aux chiffres et separateurs de date qui le collent.

    Le modele ne rend parfois qu'un morceau de date : le « 16 » de
    « 28.12.16 ». Seule la date entiere peut etre reconnue comme telle.
    """
    while debut > 0 and (texte[debut - 1].isdigit() or texte[debut - 1] in "./-"):
        debut -= 1
    while fin < len(texte) and (texte[fin].isdigit() or texte[fin] in "./-"):
        fin += 1
    return debut, fin


#: Ce qui peut rester d'un span une fois ses dates retirees sans qu'il en
#: subsiste rien : blancs et ponctuation.
_RESIDU_VIDE = re.compile(r"^[\s,;:.()\-\u2013\u2014/]*$")


def separer_dates(texte: str, sp: Span) -> tuple[list[Span], Span | None]:
    """Les dates ecrites en chiffres que porte ``sp``, et ce qui reste de lui.

    Le modele ne pose pas toujours l'etiquette « date » sur une date : sur un
    dossier reel, « 11.2.16 » ou « 23.9.14 » sortaient en « numero AVS ». Le
    reglage des dates, qui ne lisait que l'etiquette, ne les voyait pas, et
    « Aucune » laissait la colonne Date a moitie noircie. C'est donc la forme
    du texte qui fait la date, quelle que soit l'etiquette.

    Rend les dates trouvees, etiquetees « date » (ou « date de naissance » si
    ``sp`` l'etait), et le span ampute de ces dates — ``None`` s'il n'en reste
    rien. Une date au milieu d'un span ne peut pas en etre retiree sans le
    couper en deux : le span est alors rendu entier, et reste masque.
    """
    zd, zf = _etendre_au_jeton(texte, sp.start, sp.end)
    trouvees = [m for m in rules.MOTIF_DATE.finditer(texte, zd, zf)
                if rules.valid_date(m.group())]
    if not trouvees:
        return [], sp
    label = (Categorie.DATE_NAISSANCE.value if sp.est_naissance()
             else Categorie.DATE.value)
    dates = [Span(m.start(), m.end(), label, sp.score, m.group(), sp.origine)
             for m in trouvees]

    morceaux = []
    curseur = zd
    for m in trouvees:
        morceaux.append((curseur, m.start()))
        curseur = m.end()
    morceaux.append((curseur, zf))
    pleins = [(a, b) for a, b in morceaux
              if not _RESIDU_VIDE.match(texte[a:b])]
    if not pleins:
        return dates, None
    if len(pleins) > 1:
        return [], sp
    a, b = pleins[0]
    corps = texte[a:b]
    a += len(corps) - len(corps.lstrip(" \t\n,;:()-"))
    b -= len(corps) - len(corps.rstrip(" \t\n,;:()-"))
    return dates, Span(a, b, sp.label, sp.score, texte[a:b], sp.origine)


def filtrer_dates(texte: str, spans: list[Span],
                  politique: PolitiqueDates) -> list[Span]:
    """Retire les spans de date que la politique retenue laisse visibles.

    Le filtrage a lieu **avant** la fusion. Apres, une date voisine d'un nom
    forme un span unique avec lui : la demasquer rendrait le nom lisible.

    Une date est reconnue a sa forme autant qu'a son etiquette : voir
    `separer_dates`. Les autres detections deterministes — une somme de
    controle valide — ne sont jamais touchees.

    Avec `RELATIVES`, rien n'est demasque : les dates qui ne sont pas de
    naissance recoivent l'etiquette `dates_relatives.ETIQUETTE`, qui les fera
    rendre en J+n ; les dates de naissance gardent la leur, et leur masque.
    """
    if politique is PolitiqueDates.MASQUER:
        return spans

    relatives = politique is PolitiqueDates.RELATIVES
    personnes = [sp for sp in spans if sp.est_personne()]
    gardes: list[Span] = []
    for sp in spans:
        if sp.est_date():
            dates, reste = [sp], None
        elif sp.origine == Origine.REGLE.value:
            gardes.append(sp)
            continue
        else:
            dates, reste = separer_dates(texte, sp)
        if reste is not None:
            gardes.append(reste)
        for d in dates:
            naissance = est_date_de_naissance(texte, d, personnes)
            if relatives:
                etiquette = (Categorie.DATE_NAISSANCE.value if naissance
                             else Categorie.DATE_RELATIVE.value)
                gardes.append(Span(d.start, d.end, etiquette, d.score, d.text,
                                   d.origine))
            elif politique is PolitiqueDates.GARDER and naissance:
                gardes.append(d)  # la date de naissance reste masquee
    if relatives:
        gardes = _naissance_l_emporte(gardes)
    return gardes


def _naissance_l_emporte(spans: list[Span]) -> list[Span]:
    """Une date relative recouverte par une date de naissance en est une aussi.

    La regle et le modele classent chacun leur span : si le modele seul a vu la
    naissance, la fusion donnerait l'etiquette de la regle (plus d'autorite),
    et la date deviendrait un J0 possible.
    """
    naissances = [s for s in spans if s.label == Categorie.DATE_NAISSANCE.value]
    if not naissances:
        return spans
    return [Span(s.start, s.end, Categorie.DATE_NAISSANCE.value, s.score,
                 s.text, s.origine)
            if s.label == Categorie.DATE_RELATIVE.value
            and any(s.overlaps(n) for n in naissances) else s
            for s in spans]


#: Longueur minimale d'une chaine propagee. En deca, le risque de masquer des
#: mots courants l'emporte.
PROPAGATION_MIN = 4

#: Confiance minimale d'une detection du modele pour qu'elle se propage. Une
#: lecture a 0,2 est un doute : le repeter sur tout le document masque des
#: mots sains (une couche texte brouillee en a donne la preuve).
PROPAGATION_SCORE_MIN = 0.5

#: Forme d'une chaine propageable : lettres, chiffres et la ponctuation des
#: noms, adresses, courriels et identifiants. Tout autre signe (puce, arobase
#: perdu, symbole) trahit une lecture brouillee.
_PROPAGEABLE = re.compile(r"[\w .,'’\-/@+:()#°]+")
_COURRIEL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")


def propageable_sans_doute(sp: Span) -> bool:
    """La detection est-elle assez sure et assez lisible pour etre propagee ?"""
    if sp.origine == Origine.MODELE.value and sp.score < PROPAGATION_SCORE_MIN:
        return False
    brut = sp.text.strip()
    if not _PROPAGEABLE.fullmatch(brut):
        return False
    if sp.label == Categorie.EMAIL.value and not _COURRIEL.fullmatch(brut):
        return False
    return True


def entre_frontieres(texte: str, debut: int, fin: int) -> bool:
    """Le passage ``texte[debut:fin]`` commence-t-il et finit-il un mot ?

    Sans ce controle, une detection courte se propage au milieu des mots : un
    « cile » lu en debut de ligne, apres la coupure de « domi-cile », faisait
    noircir la fin de chaque « domicile » d'un livre de consultations reel.

    Seule une lettre collee a une lettre fait un interieur de mot. Un chiffre
    colle a une lettre ne rompt rien : l'OCR soude volontiers « ne le12.03.1940 »,
    et la date doit y rester trouvable.
    """
    if debut >= fin:
        return False
    avant = debut > 0 and texte[debut - 1].isalpha() and texte[debut].isalpha()
    apres = (fin < len(texte) and texte[fin].isalpha()
             and texte[fin - 1].isalpha())
    return not avant and not apres


#: Caracteres qui ne se lisent pas : trait d'union conditionnel, espaces et
#: joigneurs de largeur nulle, marque d'ordre des octets, apostrophe de
#: lecture. Un document les glisse n'importe ou sans rien changer a ce que
#: l'oeil lit.
_INVISIBLES = frozenset("\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"
                        "\u180e\u034f")

#: Apostrophes typographiques, ramenees a l'apostrophe droite.
_APOSTROPHES = dict.fromkeys(map(ord, "\u2018\u2019\u201b\u02bc\u02b9\u2032"),
                             "'")


def _ignore(ch: str) -> bool:
    """Le caractere est-il sans valeur pour la comparaison ?

    Les blancs, les caracteres invisibles et tous les traits d'union — typed,
    insecable, typographique : la cesure de fin de ligne (« Dupont-\\nMartin »
    pour « DupontMartin ») et la saisie de l'un pour l'autre ne doivent pas
    faire echapper un identifiant.
    """
    return (ch.isspace() or ch in _INVISIBLES
            or unicodedata.category(ch) == "Pd" or ch == "\u2011")


def compacter(texte: str) -> tuple[str, list[int]]:
    """Texte replie pour la comparaison, et position d'origine de chaque caractere.

    Sert a chercher une chaine sans se laisser arreter par la mise en page ni
    par la facon dont le document a code le meme texte : blancs, casse, traits
    d'union (cesure comprise), caracteres invisibles, formes de compatibilite
    (ligatures, pleine chasse) et accents decomposes — « e » suivi de l'accent
    combinant, typique d'un texte colle depuis macOS, vaut « é ».

    La liste rendue permet de reprojeter toute correspondance sur les offsets
    du texte d'origine, donc de continuer a caviarder au bon endroit. Une marque
    combinante est fondue dans la lettre qui la precede et partage sa
    position : `fin_de_correspondance` etend alors l'intervalle jusqu'a elle.
    """
    compact: list[str] = []
    positions: list[int] = []
    for i, ch in enumerate(texte):
        if _ignore(ch):
            continue
        ch = ch.translate(_APOSTROPHES)
        if unicodedata.combining(ch) and compact:
            fondu = unicodedata.normalize("NFKC", compact[-1] + ch).lower()
            if len(fondu) == 1:
                compact[-1] = fondu
                continue
        for plie in unicodedata.normalize("NFKC", ch).lower():
            if not _ignore(plie):
                compact.append(plie)
                positions.append(i)
    return "".join(compact), positions


def replier(texte: str) -> str:
    """Le texte tel que `compacter` le compare, sans les positions.

    Une seule definition de l'equivalence : la propagation, la correction et la
    verification la partagent, sans quoi l'une laisserait en place ce que
    l'autre denonce.
    """
    return compacter(texte)[0]


def fin_de_correspondance(texte: str, dernier: int) -> int:
    """Fin d'une correspondance dont le dernier caractere replie est en ``dernier``.

    Etend l'intervalle aux marques combinantes et caracteres invisibles qui
    suivent : laisser un accent isole derriere un masque serait une fuite
    minuscule, mais une fuite.
    """
    fin = dernier + 1
    while fin < len(texte) and (unicodedata.combining(texte[fin])
                                or texte[fin] in _INVISIBLES):
        fin += 1
    return fin


def propager(texte: str, spans: list[Span]) -> list[Span]:
    """Etend chaque detection a toutes ses autres occurrences dans le document.

    Si « Zorglub » est une donnee personnelle a la ligne 3, il l'est aussi a
    la ligne 40. Le modele, lui, note chaque occurrence independamment et en
    manque regulierement : c'est ainsi qu'un nom masque en en-tete reapparait
    en clair dans le corps du document.

    La recherche ignore les blancs et la casse : les pieds de page composes en
    lettres espacees — « C a b i n e t d ' i m a g e r i e » — sont frequents
    dans les en-tetes professionnels, et une recherche litterale n'y trouve
    rien.

    Elle exige en revanche une frontiere de mot, que la verification finale
    n'exige pas : voir `entre_frontieres`. L'ecart est voulu. Ce qu'il laisse
    passer — un nom soude par l'OCR au mot suivant — la verification le relit,
    et `caviardage.corriger_fuites` le noircit avec le meme critere qu'elle.

    La propagation est deterministe et gratuite. Elle corrige de surcroit une
    incoherence genante : un document ou la meme chaine est tantot masquee,
    tantot lisible, revele par difference ce qu'il pretend cacher.
    """
    compact, positions = compacter(texte)
    ajouts: list[Span] = []
    vues: set[str] = set()
    for sp in spans:
        brut = sp.text.strip()
        aiguille = replier(brut)
        if not propageable_sans_doute(sp):
            continue
        if len(brut) < PROPAGATION_MIN or not aiguille or aiguille in vues:
            continue
        vues.add(aiguille)
        depart = 0
        while True:
            i = compact.find(aiguille, depart)
            if i == -1:
                break
            debut = positions[i]
            fin = fin_de_correspondance(texte, positions[i + len(aiguille) - 1])
            if entre_frontieres(texte, debut, fin):
                ajouts.append(Span(debut, fin, sp.label, sp.score,
                                   texte[debut:fin], Origine.PROPAGE.value))
            depart = i + 1
    return spans + ajouts


# --------------------------------------------------------------------------- #
# Composants d'un nom de personne
#
# Un courriel reel adressait « A: Paul-Machin Tartempion » puis commencait par
# « Salut Paul-Machin, ». Le modele notait le nom complet a 0,99 et le
# prenom seul, dans ce contexte, a 0,09 — sous le seuil. La propagation ne
# cherchait que la chaine complete : le prenom restait lisible.
# --------------------------------------------------------------------------- #

#: Un mot qui a la forme d'un nom propre : initiale capitale, lettres, et
#: eventuellement d'autres parties jointes par un trait d'union ou une
#: apostrophe — « Paul-Machin », « D'Amato ».
_NOM = r"[A-ZÀ-ÖØ-Ý][^\W\d_]+(?:[-'’][A-ZÀ-ÖØ-Ý][^\W\d_]+)*"
_MOT_DE_NOM = re.compile(_NOM)

#: Confiance minimale d'une detection pour que ses composants soient cherches
#: partout. Un nom propage mot a mot multiplie ses erreurs : sur un livre de
#: consultations reel, « Investigation Directe », note 0,26, aurait fait
#: noircir chaque intitule « Investigation » du document.
SEUIL_COMPOSANTS = 0.5


def _sans_accents(mot: str) -> str:
    decompose = unicodedata.normalize("NFKD", mot.lower())
    return "".join(c for c in decompose if not unicodedata.combining(c))


def _peut_etre_un_nom(mot: str) -> bool:
    """Le mot peut-il, seul, designer une personne ?

    Ni un titre (« Docteur »), ni une fonction (« Medecin »), ni une formule
    d'appel (« Salut »), ni du vocabulaire neutre (« Madame », « Patient ») :
    ces mots accompagnent un nom sans en faire partie, et les chercher partout
    masquerait tout le document.
    """
    bas = _sans_accents(mot.rstrip("."))
    return (len(bas) >= 3
            and bas not in TITRES
            and bas not in MARQUEURS_PROFESSIONNEL
            and bas not in SALUTATIONS
            and not est_generique(mot))


def composants(nom: str) -> list[str]:
    """Prenoms et patronymes d'un nom complet, a chercher chacun pour soi.

    Le nom doit n'etre fait que de titres et de mots en forme de nom propre.
    Un span qui porte autre chose — un chiffre, un mot en minuscules, un
    intitule fusionne a un nom comme « Arret Dupont » sous une etiquette
    « personne » — n'est pas un nom sur lequel on puisse raisonner mot a mot.
    """
    noms: list[str] = []
    for mot in nom.split():
        brut = mot.strip(",;:")
        if _sans_accents(brut.rstrip(".")) in TITRES:
            continue
        if not _MOT_DE_NOM.fullmatch(brut) or not _peut_etre_un_nom(brut):
            return []
        noms.append(brut)
    if len(nom.split()) < 2:
        return []  # un nom d'un seul mot est deja propage tel quel
    return [m for m in noms if len(m) >= PROPAGATION_MIN]


def occurrences_du_mot(texte: str, mot: str) -> list[tuple[int, int]]:
    """Occurrences de ``mot`` comme mot entier, ecrit avec une capitale.

    Les deux contraintes sont indispensables a un composant isole, la ou le
    nom complet s'en passe. Sans frontiere de mot, « Marc » se trouve dans
    « Marche » ; sans capitale, le prenom « Claire » noircirait chaque « urine
    claire » du document. La casse est ignoree au-dela de l'initiale, pour
    reconnaitre aussi « TARTEMPION ».
    """
    motif = re.compile(rf"(?<![^\W\d_]){re.escape(mot)}(?![^\W\d_])",
                       re.IGNORECASE)
    return [(m.start(), m.end()) for m in motif.finditer(texte)
            if m.group()[0].isupper()]


#: Longueur minimale (lettres et chiffres) d'un composant de nom pour qu'il
#: soit aussi cherche sous une forme approchee, et ressemblance exigee.
#: L'OCR abime les noms qu'il lit mal : « JEAN-—NOEI » pour « Jean-Bidule ».
#: Court, un nom ressemble a trop de mots ; long, il ne ressemble qu'a
#: lui-meme mal lu.
APPROCHE_MIN = 6
RESSEMBLANCE_NOM = 0.85


def _cle_nom(texte: str) -> str:
    return "".join(c for c in texte.lower() if c.isalnum())


def occurrences_approchees(texte: str, mot: str) -> list[tuple[int, int]]:
    """Occurrences de ``mot`` mal lues par l'OCR, ecrites avec une capitale.

    Chaque suite de caracteres sans blanc est comparee au mot, ponctuation
    retiree. Rend le passage sans la ponctuation qui l'entoure.
    """
    from difflib import SequenceMatcher

    cle = _cle_nom(mot)
    if len(cle) < APPROCHE_MIN:
        return []
    trouves: list[tuple[int, int]] = []
    for m in re.finditer(r"\S+", texte):
        brut = m.group()
        lettres = [i for i, c in enumerate(brut) if c.isalnum()]
        if not lettres:
            continue
        debut, fin = m.start() + lettres[0], m.start() + lettres[-1] + 1
        if not texte[debut].isupper():
            continue
        candidat = _cle_nom(brut)
        if abs(len(candidat) - len(cle)) > 2:
            continue
        if SequenceMatcher(None, candidat, cle).ratio() >= RESSEMBLANCE_NOM:
            trouves.append((debut, fin))
    return trouves


def propager_composants(texte: str, spans: list[Span]) -> list[Span]:
    """Etend chaque composant d'un nom de personne a toutes ses occurrences.

    ``spans`` doit etre fait de detections **brutes**, avant fusion : l'union
    de deux detections voisines n'est plus un nom, meme etiquetee comme tel.
    """
    ajouts: list[Span] = []
    vus: set[str] = set()
    for sp in spans:
        if (not sp.est_personne() or sp.score < SEUIL_COMPOSANTS
                or sp.origine == Origine.PROPAGE.value):
            continue
        for mot in composants(sp.text):
            cle = mot.lower()
            if cle in vus:
                continue
            vus.add(cle)
            ajouts.extend(Span(d, f, sp.label, sp.score, texte[d:f],
                               Origine.PROPAGE.value)
                          for d, f in occurrences_du_mot(texte, mot))
    return ajouts


#: Le nom propre qui suit immediatement un span, separe par des espaces sur la
#: meme ligne — et celui qui le precede.
_VOISIN_APRES = re.compile(rf"[ \t]+({_NOM})(?![^\W\d_])")
_VOISIN_AVANT = re.compile(rf"(?<![^\W\d_])({_NOM})[ \t]+$")


def etendre_aux_voisins(texte: str, spans: list[Span]) -> list[Span]:
    """Spans couvrant un nom d'un seul mot et le nom propre qui le jouxte.

    Un rapport hospitalier (exemple invente) portait « Dresse Brontosaure » en signature, puis
    « Valide electroniquement par Brontosaure Bidule (Medecin chef de
    clinique) ». Le patronyme, connu, etait masque a la seconde ligne par
    propagation ; le prenom accole, que le modele n'avait pas releve, restait
    lisible.

    Seul un nom reduit a un mot est etendu : un nom de deux mots est deja
    complet, et l'etendre emporterait le service ou la ville qui le suit.

    Ne rend que les spans ajoutes.
    """
    ajouts: list[Span] = []
    for sp in spans:
        if not sp.est_personne() or len(sp.text.split()) != 1:
            continue
        debut, fin = sp.start, sp.end
        apres = _VOISIN_APRES.match(texte, sp.end)
        if apres and _peut_etre_un_nom(apres.group(1)):
            fin = apres.end(1)
        avant = _VOISIN_AVANT.search(texte, max(0, sp.start - 60), sp.start)
        if avant and _peut_etre_un_nom(avant.group(1)):
            debut = avant.start(1)
        if (debut, fin) != (sp.start, sp.end):
            ajouts.append(Span(debut, fin, sp.label, sp.score,
                               texte[debut:fin], Origine.PROPAGE.value))
    return ajouts


def filtrer_generiques(spans: list[Span]) -> list[Span]:
    """Ecarte les spans qui ne portent que du vocabulaire de metier.

    Le filtrage precede la fusion : un span generique fusionne a une vraie
    entite en etendrait inutilement la couverture, et ne serait plus separable.

    Les detections deterministes sont exemptees. Une somme de controle valide
    ne se discute pas — et si une regle reconnait un identifiant dans un texte
    qui ressemble a du vocabulaire courant, c'est la regle qui a raison.
    """
    return [sp for sp in spans
            if sp.origine == Origine.REGLE.value
            or not est_generique(sp.text)
            or _civilite_et_nom_propre(sp)]


def _civilite_et_nom_propre(sp: Span) -> bool:
    """Un nom de personne apres une civilite, meme fait de vocabulaire.

    « Monsieur Page », « Madame Marche » : le mot est un nom de famille des
    qu'une civilite le precede et qu'il porte une majuscule. Garder un faux
    positif coute moins qu'un nom qui survit.
    """
    if not sp.est_personne():
        return False
    mots = sp.text.split()
    return (len(mots) >= 2 and mots[0].rstrip(".").lower() in TITRES
            and any(m[:1].isupper() and m.rstrip(".").lower() not in TITRES
                    for m in mots[1:]))


#: Etiquettes qui designent un numero. Le modele les pose volontiers, a faible
#: confiance, sur des fragments de mot ou des termes medicaux.
ETIQUETTES_NUMERO = frozenset({"numero de dossier", "numero de patient",
                               "identifiant de dossier", "numero d'assure"})

#: Au-dessus, une detection de numero n'est plus discutee.
SEUIL_NUMERO_DOUTEUX = 0.5

#: Niveau vertebral : C2, L4, L5, D12, S1. Courant dans un courrier
#: d'orthopedie, jamais un numero de dossier.
NIVEAU_VERTEBRAL = re.compile(r"[CDLST]\d{1,2}")


def _recadrer_sur_les_mots(text: str, sp: Span) -> Span | None:
    """Retire d'un span les fragments de mot qui le debordent.

    « fractur[e C2] » : le modele a pris la fin du mot qui precede. Masquer ce
    span coupe « fracture » en deux. Rend ``None`` s'il ne reste rien.
    """
    debut, fin = sp.start, sp.end
    if debut > 0 and text[debut - 1].isalnum() and text[debut].isalnum():
        blanc = re.search(r"\s", text[debut:fin])
        if blanc is None:
            return None
        debut += blanc.end()
    if fin < len(text) and text[fin].isalnum() and text[fin - 1].isalnum():
        blancs = list(re.finditer(r"\s", text[debut:fin]))
        if not blancs:
            return None
        fin = debut + blancs[-1].start()
    corps = text[debut:fin]
    debut += len(corps) - len(corps.lstrip())
    fin -= len(corps) - len(corps.rstrip())
    if debut >= fin:
        return None
    return Span(debut, fin, sp.label, sp.score, text[debut:fin], sp.origine)


def _ressemble_a_un_numero(chaine: str) -> bool:
    """La chaine porte-t-elle un numero, et pas seulement de la ponctuation ?"""
    jetons = [j.strip(".,;:()") for j in chaine.split()
              if any(c.isdigit() for c in j)]
    if not jetons:
        return False
    if all(NIVEAU_VERTEBRAL.fullmatch(j) for j in jetons):
        return False
    return sum(c.isalnum() for c in chaine) >= 3


#: Etiquettes qui designent un numero fait de chiffres par definition : un
#: numero AVS, un IBAN, un telephone ou une carte sans un seul chiffre n'existe
#: pas. Le modele les pose pourtant sur des mots : « Avis ORL » et « Avis
#: osteoporose » sortaient en « numero AVS » sur un dossier reel, a 0,29.
#: Idem pour les numeros de dossier, de patient et d'assure : « assura » sortait
#: en « numero d'assure » a 0,69. Un vrai numero, meme alphanumerique, a des chiffres.
ETIQUETTES_A_CHIFFRES = frozenset({"numero AVS", "IBAN", "numero de telephone",
                                   "numero de carte", "numero d'assure",
                                   "numero de dossier", "numero de patient",
                                   "identifiant de dossier"})

def filtrer_numeros_douteux(text: str, spans: list[Span]) -> list[Span]:
    """Ecarte ou recadre les numeros que le modele ne tient qu'a peine.

    Le seuil est bas a dessein (voir `DEFAULT_THRESHOLD`), et une detection
    faible suffit a masquer un nom. Pour un numero, elle suffit aussi a abimer
    le texte : sur un courrier reel, « degre I, L4 sur L5 », « decompensatio[n
    musculaire] » ou « fractur[e C2] » sortaient masques comme numero de
    dossier ou de patient — et propages a toutes les pages a confiance 1,0.

    Un numero AVS, un IBAN, un telephone ou une carte sans aucun chiffre est
    ecarte quelle que soit sa confiance : voir `ETIQUETTES_A_CHIFFRES`.

    Pour le reste, seuls les spans du modele, d'etiquette numerique et sous
    `SEUIL_NUMERO_DOUTEUX` sont touches. Un numero que ce filtre garde est
    masque comme avant ; une regle deterministe ou une confiance elevee
    echappent a tout examen.
    """
    gardes: list[Span] = []
    for sp in spans:
        if (sp.origine == Origine.MODELE.value
                and sp.label in ETIQUETTES_A_CHIFFRES
                and not any(c.isdigit() for c in sp.text)):
            continue  # quelle que soit sa confiance
        if (sp.origine != Origine.MODELE.value or sp.label not in ETIQUETTES_NUMERO
                or sp.score >= SEUIL_NUMERO_DOUTEUX):
            gardes.append(sp)
            continue
        sp = _recadrer_sur_les_mots(text, sp)
        if sp is not None and _ressemble_a_un_numero(sp.text):
            gardes.append(sp)
    return gardes


#: Ce qu'il faut au moins, autour d'une detection du modele, pour qu'elle
#: puisse etre un numero de ce format : (caracteres comptes, nombre). Un numero
#: AVS fait 13 chiffres (756 et dix autres), un IBAN suisse 21 caracteres (le
#: plus court au monde 15), une carte d'assure 20 chiffres ; les minimums
#: tolerent quelques caracteres mal lus. Sur un electrocardiogramme scanne,
#: des bouts de legende d'un a trois caracteres sortaient en « numero AVS »,
#: et chacun posait un aplat noir sur le trace.
FORMATS_MINIMAUX = {
    "numero AVS": ("chiffres", 10),
    "IBAN": ("alnum", 12),
    "numero de carte": ("chiffres", 10),
    "numero de telephone": ("chiffres", 6),
    "email": ("alnum", 5),
}


def _autour(text: str, sp: Span, label: str) -> str:
    """Les mots que touche le span, et ses voisins a chiffres d'un meme numero.

    Un numero ecrit en groupes (« 756 1234 5678 97 ») peut sortir du modele en
    morceaux : c'est le numero entier qu'on juge, pas le morceau. Une adresse
    e-mail, d'un seul tenant, est jugee sur ses seuls mots.
    """
    mots = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    touches = [i for i, (d, f) in enumerate(mots) if d < sp.end and sp.start < f]
    if not touches:
        return sp.text
    debut, fin = touches[0], touches[-1]
    if label != "email":
        def numerique(i):
            return any(c.isdigit() for c in text[mots[i][0]:mots[i][1]])
        while debut > 0 and numerique(debut - 1) \
                and "\n" not in text[mots[debut - 1][1]:mots[debut][0]]:
            debut -= 1
        while fin < len(mots) - 1 and numerique(fin + 1) \
                and "\n" not in text[mots[fin][1]:mots[fin + 1][0]]:
            fin += 1
    return text[mots[debut][0]:mots[fin][1]]


def filtrer_formats_impossibles(text: str, spans: list[Span]) -> list[Span]:
    """Ecarte les detections du modele qui ne peuvent pas avoir le format de
    leur etiquette : un « numero AVS » de deux chiffres n'en est pas un.

    Seul le modele est juge ; une regle a verifie le format, et souvent la
    somme de controle. Voir `FORMATS_MINIMAUX`.
    """
    gardes: list[Span] = []
    for sp in spans:
        format_ = FORMATS_MINIMAUX.get(sp.label)
        if sp.origine == Origine.MODELE.value and format_ is not None:
            compte, minimum = format_
            autour = _autour(text, sp, sp.label)
            n = sum(c.isdigit() if compte == "chiffres" else c.isalnum()
                    for c in autour)
            if n < minimum:
                continue
        gardes.append(sp)
    return gardes


#: Une prise d'un schema posologique : un chiffre, une fraction (« 1/2 »,
#: « 0,5 ») ou un signe de fraction. Jamais deux chiffres : « 01-02-24 » est
#: une date, pas une posologie.
_PRISE = r"(?:\d(?:[.,/]\d)?|[½¼¾])"

#: Schema posologique matin-midi-soir(-nuit) : « 1-0-1-0 », « 0 - 0 - 2 - 0 »,
#: « ½-0-½ ». Une cellule de plan de medication, pas un identifiant.
_POSOLOGIE = rf"{_PRISE}(?:[ \t]*[-–][ \t]*{_PRISE}){{2,3}}"

#: Un ou plusieurs schemas, une colonne de plan de medication lue d'un bloc.
POSOLOGIES = re.compile(rf"{_POSOLOGIE}(?:\s+{_POSOLOGIE})*")


def filtrer_posologies(spans: list[Span]) -> list[Span]:
    """Ecarte les spans qui ne portent qu'un ou des schemas posologiques.

    Sur un plan de medication reel, « 1 - 0 - 0 - 0 » et la colonne
    « 0-0-2-0 / 1-0-1-0 / 1-0-0-0 » sortaient du modele en numero de dossier
    ou AVS, a 0,19 et 0,35 : assez de chiffres pour `filtrer_numeros_douteux`,
    et la colonne Posologie entiere etait noircie. Un schema de prises n'est
    ni une date, ni un telephone, ni un identifiant, quelle que soit
    l'origine de la detection.
    """
    return [sp for sp in spans if not POSOLOGIES.fullmatch(sp.text.strip())]


def _detect_chunked(model, text: str, labels, threshold: float) -> list[Span]:
    out: list[Span] = []
    for chunk in split_text(text):
        for s in detect(model, chunk.text, labels, threshold):
            out.append(Span(s.start + chunk.offset, s.end + chunk.offset,
                            s.label, s.score, s.text, s.origine))
    return out


def detections_modele(model, text: str, labels=DEFAULT_LABELS,
                      threshold: float = DEFAULT_THRESHOLD,
                      normalise_case: bool = True) -> list[Span]:
    """Ce que le modele trouve dans ``text``, normalisation de casse comprise.

    C'est l'etape couteuse de la detection, et la seule. `analyse` et
    `_spans_bruts` en ont besoin toutes deux : la calculer une fois et la leur
    passer evite de faire tourner le modele deux fois sur le meme texte — ce
    que la chaine PDF faisait sur chaque page, pour un resultat identique.
    """
    found = _detect_chunked(model, text, labels, threshold)
    if normalise_case:
        variante = normalise_casse(text)
        if variante != text:
            for s in _detect_chunked(model, variante, labels, threshold):
                # Le texte rendu est celui de l'original, pas de la copie.
                found.append(Span(s.start, s.end, s.label, s.score,
                                  text[s.start:s.end]))
    return found


def quasi_identifiants(model, text: str, masques: list[Span],
                       labels=QUASI_LABELS,
                       threshold: float = SEUIL_QUASI) -> list[Span]:
    """Passages qui peuvent aider a reconnaitre la personne, sans la nommer.

    Profession, sport ou activite, nationalite ou origine, evenement, age :
    voir `engine.QUASI_IDENTIFIANTS`. Rien de ce qui sort d'ici n'est masque
    d'office ; la relecture les montre, et le medecin decide.

    Une passe a part, et non des etiquettes de plus dans `detections_modele` :
    sur le corpus de regression, les deux donnent le meme rappel, mais dans une
    passe commune « Agriculteur de 63 ans », jusque-la masque comme personne,
    devenait une profession laissee lisible. Separees, les detections qui
    masquent restent exactement celles d'avant. Le prix est une passe du
    modele en plus, sur le texte d'origine seulement — la variante en casse
    de titre sert aux patronymes en capitales, pas a ceci —, et seulement
    quand il y a une relecture pour les montrer.

    ``masques`` sont les spans deja retenus : un passage qui en recoupe un est
    deja couvert, ou trop mele a un nom pour etre juge a part.

    Deux ecarts, mesures sur le corpus de regression ou ils faisaient presque
    tout le bruit : le poste du medecin qui ecrit (« Specialiste en
    ophtalmologie », voir `roles.est_poste_soignant`), et la « profession »
    ecrite en capitales — un titre d'examen, une rubrique de laboratoire
    (« ECHO-DOPPLER VEINEUX… », « CHIMIE CLINIQUE »).
    """
    if model is None or not text.strip():
        return []
    trouves = resolve_overlaps(_detect_chunked(model, text, labels, threshold))
    return [sp for sp in trouves
            if not est_generique(sp.text)
            and not (sp.label == Categorie.PROFESSION.value
                     and (est_poste_soignant(sp.text) or _en_capitales(sp.text)))
            and not any(sp.overlaps(m) for m in masques)]


def _en_capitales(texte: str) -> bool:
    lettres = [c for c in texte if c.isalpha()]
    return bool(lettres) and all(c.isupper() for c in lettres)


def analyse(model, text: str,
            labels=DEFAULT_LABELS,
            threshold: float = DEFAULT_THRESHOLD,
            use_rules: bool = True,
            normalise_case: bool = True,
            dates: PolitiqueDates = PolitiqueDates.MASQUER,
            exceptions=None,
            modele: list[Span] | None = None) -> list[Span]:
    """Detecte toutes les entites d'un texte de longueur quelconque.

    Les spans de la couche 1 sont prioritaires : une somme de controle valide
    l'emporte sur une estimation statistique.

    Avec ``normalise_case``, une seconde passe est faite sur une copie du texte
    dont les patronymes en capitales sont remis en casse de titre. Les
    documents medicaux suisses ecrivent constamment les noms en capitales, et
    une fuite de ce type a ete observee sur un document reel. La transformation
    preservant la longueur, les offsets obtenus sur la copie s'appliquent tels
    quels a l'original.

    Le gain de cette passe n'est pas encore demontre sur jeu de test — voir
    l'avertissement en tete de `normalise.py`. Elle est active par defaut par
    prudence, et desactivable avec ``normalise_case=False``.

    ``exceptions`` est le repertoire des corrections retenues sur cette
    machine (`exceptions.Repertoire`). Absent, rien n'est rejoue : c'est le
    defaut, et il masque davantage.

    ``modele`` porte les detections du modele deja calculees sur ce texte
    (`detections_modele`) ; absent, elles sont calculees ici.
    """
    found: list[Span] = []

    if use_rules:
        found.extend(_rules_to_spans(text))

    if model is not None:
        found.extend(modele if modele is not None else detections_modele(
            model, text, labels, threshold, normalise_case))

    found = filtrer_dates(text, found, dates)
    found = filtrer_generiques(found)
    found = filtrer_numeros_douteux(text, found)
    found = filtrer_formats_impossibles(text, found)
    found = filtrer_posologies(found)

    # --- ce que cette machine a retenu des relectures precedentes ----------
    # Les ajouts d'abord, le filtre ensuite, et jamais l'inverse : une chaine
    # apprise comme « toujours masquer » ne doit pas pouvoir etre retiree par
    # une entree « jamais masquer » — le sens sur l'emporte.
    if exceptions is not None:
        found.extend(exceptions.spans_toujours(text))
        found = exceptions.filtrer(found)

    spans = _merge(propager(text, _merge(found, text))
                   + propager_composants(text, found), text)
    # L'extension vient apres la propagation, qui a pu poser le patronyme la
    # ou le modele l'avait manque ; puis le nom etendu est propage a son tour.
    # Seuls les ajouts repassent par `propager`, dont la recherche ignore les
    # frontieres de mot : un composant isole comme « Marc » y mordrait dans
    # « Marche ».
    return _merge(spans + propager(text, etendre_aux_voisins(text, spans)),
                  text)


def analyse_detaillee(model, text: str, **kw) -> tuple[list[Span], list[Span]]:
    """Rend les spans fusionnes **et** les spans bruts, avant fusion.

    La fusion par union perd l'etiquette des spans absorbes : un nom colle a sa
    date de naissance forme un span unique, etiquete « date » parce que la
    regle qui a reconnu la date porte un score de 1,0. Le span « personne »
    disparait alors — et avec lui le signal qui sert a reconnaitre le patient.

    Les traitements qui raisonnent sur les roles doivent donc travailler sur
    les detections brutes, pas sur le resultat de la fusion.
    """
    if model is not None and kw.get("modele") is None:
        kw["modele"] = detections_modele(
            model, text, kw.get("labels", DEFAULT_LABELS),
            kw.get("threshold", DEFAULT_THRESHOLD),
            kw.get("normalise_case", True))
    bruts = _spans_bruts(model, text, **kw)
    return analyse(model, text, **kw), bruts


def _spans_bruts(model, text: str,
                 labels=DEFAULT_LABELS,
                 threshold: float = DEFAULT_THRESHOLD,
                 use_rules: bool = True,
                 normalise_case: bool = True,
                 dates=None,
                 exceptions=None,
                 modele: list[Span] | None = None) -> list[Span]:
    found: list[Span] = []
    if use_rules:
        found.extend(_rules_to_spans(text))
    if exceptions is not None:
        # Les ajouts appris, mais pas le filtre : ces spans servent au
        # raisonnement par roles, et en retirer un ferait perdre le signal qui
        # designe le patient — sans pour autant decouvrir quoi que ce soit,
        # puisque ces spans-la ne sont jamais masques directement.
        found.extend(exceptions.spans_toujours(text))
    if model is not None:
        found.extend(modele if modele is not None else detections_modele(
            model, text, labels, threshold, normalise_case))
    return found


def _merge(spans: list[Span], texte: str) -> list[Span]:
    """Fusionne les spans qui se chevauchent en prenant leur union.

    Arbitrer entre deux detections qui se chevauchent — garder la plus longue,
    ou la mieux notee — revient a jeter du texte que l'une des deux jugeait
    sensible. Un cas reel l'a montre : sur un en-tete de laboratoire, la passe
    brute detectait ``Dominique\nArnold-Vautier`` et la passe normalisee
    ``Bunzli Dominique``. La plus longue l'emportait, et le patronyme en
    capitales restait lisible en clair sur le document caviarde.

    L'union est la seule politique sure pour de l'anonymisation : en cas de
    desaccord entre deux detecteurs, on masque la reunion de ce qu'ils
    designent. Le cout d'un caractere masque en trop est sans commune mesure
    avec celui d'un nom qui subsiste.

    L'etiquette retenue est celle du span le mieux note du groupe — les
    detections par somme de controle, notees 1,0, l'emportent naturellement.

    Un span qui franchit une ligne vide est ensuite coupe en morceaux (voir
    `couper_aux_lignes_vides`). La coupe a lieu ici, et non une fois en fin
    d'analyse, parce que les chaines .docx et PDF refusionnent avec les
    detections brutes : une coupe faite plus tot y etait defaite.
    """
    if not spans:
        return []

    groupes: list[list[Span]] = []
    for s in sorted(spans, key=lambda s: (s.start, -s.end)):
        if groupes and s.start < max(x.end for x in groupes[-1]):
            groupes[-1].append(s)
        else:
            groupes.append([s])

    fusionnes: list[Span] = []
    for groupe in groupes:
        debut = min(x.start for x in groupe)
        fin = max(x.end for x in groupe)
        # L'autorite prime sur le score : une somme de controle valide
        # l'emporte sur une estimation, quelle que soit la confiance affichee.
        chef = max(groupe, key=lambda x: (x.autorite, x.score, x.end - x.start))
        # Le texte doit refleter l'union, pas le span qui a donne l'etiquette :
        # la verification finale s'appuie dessus pour chercher d'eventuelles
        # chaines residuelles dans le document produit.
        etiquette = chef.label
        # Un « employeur » qui nomme un hopital, un cabinet, un laboratoire
        # est un etablissement : le modele n'a qu'une etiquette pour les deux.
        # Un assureur n'est pas un employeur non plus ; il est teste d'abord.
        if etiquette == Categorie.EMPLOYEUR.value:
            if rules.est_assureur(texte[debut:fin]):
                etiquette = Categorie.ASSUREUR.value
            elif contient_mot_etablissement(texte[debut:fin]):
                etiquette = Categorie.ETABLISSEMENT.value
        fusionnes.append(Span(debut, fin, etiquette, chef.score,
                              texte[debut:fin], chef.origine))
    return couper_aux_lignes_vides(texte, fusionnes)


#: Une ligne vide, espaces compris : la frontiere entre deux blocs du document.
_LIGNE_VIDE = re.compile(r"\n[ \t]*\n\s*")


def couper_aux_lignes_vides(texte: str, spans: list[Span]) -> list[Span]:
    """Coupe en morceaux les spans qui franchissent une ligne vide.

    Une ligne vide separe deux blocs ; un span qui la franchit reunit presque
    toujours deux entites. Sur un rapport type, le modele lisait
    « 1510 Moudon\\n\\nGrandvaux, le » comme une seule adresse postale : le
    texte produit fondait alors l'adresse du destinataire et le lieu de la
    lettre en un seul repere, et la ligne vide disparaissait avec eux.

    Chaque morceau garde l'etiquette du span : rien de ce qui etait masque ne
    cesse de l'etre, seuls les blancs entre les blocs sont rendus. Un saut de
    ligne simple ne coupe pas : un nom renvoye a la ligne reste un seul nom.
    """
    coupes: list[Span] = []
    for sp in spans:
        bornes, debut = [], sp.start
        for m in _LIGNE_VIDE.finditer(texte, sp.start, sp.end):
            bornes.append((debut, m.start()))
            debut = m.end()
        if not bornes:
            coupes.append(sp)
            continue
        bornes.append((debut, sp.end))
        for debut, fin in bornes:
            while debut < fin and texte[debut].isspace():
                debut += 1
            while fin > debut and texte[fin - 1].isspace():
                fin -= 1
            if debut < fin:
                coupes.append(Span(debut, fin, sp.label, sp.score,
                                   texte[debut:fin], sp.origine))
    return coupes


def anonymise(model, text: str, **kw) -> tuple[str, list[Span]]:
    spans = analyse(model, text, **kw)
    return apply_spans(text, spans), spans
