"""Chaine bureautique : anonymisation en place d'un document Word (.docx).

La chaine image noircit des pixels parce qu'un PDF scanne n'offre rien d'autre.
Un .docx, lui, **est** son texte : il n'y a pas de couche sous-jacente a
laisser fuir, et le geste sur consiste a remplacer les caracteres eux-memes
par des pseudonymes stables. Le document reste modifiable, lisible et
exploitable, alors qu'un aplat noir sur un traitement de texte n'aurait
masque que l'affichage.

Le danger se deplace ailleurs. Un .docx est une archive zip, et le texte
visible dans la fenetre de Word n'en est qu'une partie :

- les en-tetes, pieds de page, notes et commentaires vivent dans des parties
  separees de l'archive ;
- les proprietes du document (auteur, societe, titre, dernier
  enregistrement) nomment souvent le patient ou le praticien sans qu'aucune
  fenetre les montre ;
- les revisions non acceptees conservent le texte supprime, dans `w:delText`,
  ainsi que le nom de leur auteur ;
- les codes de champ, les textes alternatifs d'images et les cibles de liens
  (`mailto:`) portent des donnees en clair ;
- Word coupe les mots en fragments a la moindre correction : « Zorglub »
  s'ecrit couramment `<w:t>Zorg</w:t><w:t>lub</w:t>`. Une recherche naive
  dans le XML ne le trouve jamais.

Ce module traite ces cinq points. Il reconstitue d'abord le **texte logique**
de chaque partie de l'archive — la suite des noeuds de texte, avec les
separateurs de paragraphe et de cellule — puis fait porter la detection sur ce
texte, et enfin reecrit les noeuds concernes, en repartissant un span sur les
fragments qu'il chevauche.

Le remplacement est chirurgical : les portions de XML qui n'ont pas a changer
ne sont jamais reserialisees. Un aller-retour par un analyseur XML perdrait les
declarations d'espaces de noms auxquelles Word tient (celles que `mc:Ignorable`
mentionne sans les employer), et produirait un fichier que Word refuse
d'ouvrir. On ne modifie donc que des tranches d'octets reperees par leurs
positions.

Les images incorporees ne sont pas du texte : un rapport colle en capture
d'ecran ne se reecrit pas, il se noircit. `images.py` s'en charge, en empruntant
l'OCR de la chaine image, et la verification les relit. `--logos` reste
disponible pour les retirer purement et simplement.

Les objets incorpores — un classeur, un PDF colle, un contenu altChunk — ne se
lisent pas : ils sont retires de l'archive, remplaces par une mention visible,
et le rapport en donne le compte (le document est alors « a verifier »).

Ce qui reste hors de portee : les images qu'aucun decodeur ne sait ouvrir. Le
rapport les compte et le signale.
"""

from __future__ import annotations

import io
import posixpath
import re
import time
import zipfile
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from ..dates_relatives import TableDatesRelatives, echelle
from ..detect.rules import find_all
from ..engine import Span
from ..pipeline import (PolitiqueDates, _merge, analyse, analyse_detaillee,
                        quasi_identifiants, replier)
from ..pseudonymes import TablePseudonymes
from ..tesseract import TesseractIndisponible
from ..roles import (Niveau, filtrer_par_role, noms_du_patient,
                     qualifier_personnes)
from .commun import (assembler_texte, chaines_connues, chaines_lisibles,
                     occurrences_relues,
                     propager_chaines)
from .fuites import Fuite, depuis_chaines

RAISON_XML = "encore présente dans le fichier après remplacement"
RAISON_IMAGE = "relue dans une image incorporée, même après noircissement"
RAISON_TEXTE_EXPORTE = "encore présente dans le texte exporté après remplacement"
from .images import (RapportImage, caviarder_image, fuites_dans_images,
                     noircir_relues)
from .metadonnees import nettoyer_image, porte_des_metadonnees
from .ocr import LANGUE
from .zones import OptionsZones

#: Parties de l'archive qui portent du texte redige par un humain. Ce sont les
#: seules sur lesquelles le modele est appele : le faire tourner sur
#: `styles.xml` couterait du temps pour ne rien trouver.
PARTIE_REDIGEE = re.compile(
    r"^word/(glossary/)?"
    r"(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$"
    # Graphiques et schemas : leur texte (`a:t`) est lu comme celui du corps.
    r"|^word/(charts/chart|diagrams/(data|drawing))\d*\.xml$")

#: En-tetes et pieds de page, que `--entetes` vide entierement.
PARTIE_ENTETE = re.compile(r"^word/(glossary/)?(header|footer)\d*\.xml$")

#: Noeuds de texte. `w:delText` porte le texte *supprime* d'une revision non
#: acceptee : invisible a l'ecran, present dans le fichier, et tout aussi
#: identifiant que le reste. `w:instrText` (code de champ), `a:t` (graphique,
#: schema), `c:v` (valeurs en cache d'un graphique : etiquette de serie,
#: categories) et `m:t` (formule) portent du texte tout aussi lisible une fois le
#: fichier ouvert ou le champ mis a jour.
_OUVREUR_TEXTE = re.compile(
    r"<(w:t|w:delText|w:instrText|a:t|m:t|c:v)((?:\s[^>]*?)?)(/>|>)")


class _NoeudTexte:
    """Noeud de texte trouve : memes groupes que l'ancienne expression."""

    __slots__ = ("_debut", "_fin", "_groupes")

    def __init__(self, debut: int, fin: int, groupes: tuple):
        self._debut, self._fin, self._groupes = debut, fin, groupes

    def start(self) -> int:
        return self._debut

    def end(self) -> int:
        return self._fin

    def group(self, n: int = 0):
        return self._groupes[n]


class _ExpressionNoeudTexte:
    """Equivalent lineaire de ``<(balise)(attrs)(/>|>(.*?)</\\1>)`` (DOTALL).

    La forme paresseuse avec reference arriere relisait tout le reste de la
    partie a chaque ouvreur non ferme : quadratique, avec le GIL retenu. Ici la
    fermeture se cherche par ``str.find``, et un echec vaut pour tous les
    ouvreurs suivants de la meme balise.
    """

    def finditer(self, xml: str):
        sans_fermeture: set[str] = set()
        numeriques = ([m.span() for m in _CACHE_NUMERIQUE.finditer(xml)]
                      if "<c:v" in xml else [])
        position = 0
        while True:
            m = _OUVREUR_TEXTE.search(xml, position)
            if m is None:
                return
            balise, attrs, bout = m.group(1), m.group(2), m.group(3)
            if balise == "c:v" and any(a <= m.start() < b for a, b in numeriques):
                position = m.end()
                continue
            if bout == "/>":
                yield _NoeudTexte(m.start(), m.end(), (
                    xml[m.start():m.end()], balise, attrs, bout, None))
                position = m.end()
                continue
            if balise not in sans_fermeture:
                ferme = f"</{balise}>"
                i = xml.find(ferme, m.end())
                if i < 0:
                    sans_fermeture.add(balise)
                else:
                    fin = i + len(ferme)
                    yield _NoeudTexte(m.start(), fin, (
                        xml[m.start():fin], balise, attrs,
                        xml[m.end() - 1:fin], xml[m.end():i]))
                    position = fin
                    continue
            position = m.start() + 1


NOEUD_TEXTE = _ExpressionNoeudTexte()

#: Valeurs numeriques en cache d'un graphique : jamais passees au modele, un
#: nombre remplace par un pseudonyme rendrait le graphique invalide. Seules
#: les regles deterministes y passent (`_masquer_caches_numeriques`).
_CACHE_NUMERIQUE = re.compile(
    r"<c:(numCache|numLit)\b.*?</c:\1>", re.DOTALL)
_VALEUR_NUMERIQUE = re.compile(r"(<c:v(?:\s[^>]*)?>)([^<]*)(</c:v>)")
_CODE_FORMAT = re.compile(r"(<c:formatCode(?:\s[^>]*)?>)([^<]*)(</c:formatCode>)")


def _identifiant_numerique(valeur: str) -> bool:
    """Vrai si un nombre en cache est un identifiant (AVS, telephone, date...).

    Un numero de telephone stocke comme nombre perd son zero initial : on
    teste aussi la forme avec « 0 » ou « + » restituee. Les mesures ordinaires
    (5.4, 120) ne correspondent a aucune regle.
    """
    v = _desechapper(valeur).strip()
    if not v:
        return False
    candidats = [v]
    if v.isdigit():
        candidats += ["0" + v, "+" + v]
    return any(find_all(c) for c in candidats)


def _masquer_caches_numeriques(xml: str) -> tuple[str, int]:
    """Neutralise (`0`) les identifiants stockes comme nombres dans un graphique."""
    if "<c:num" not in xml:
        return xml, 0
    n = 0

    def valeur(m):
        nonlocal n
        if _identifiant_numerique(m.group(2)):
            n += 1
            return m.group(1) + "0" + m.group(3)
        return m.group(0)

    def cache(m):
        return _VALEUR_NUMERIQUE.sub(valeur, m.group(0))

    def code(m):
        nonlocal n
        if find_all(_desechapper(m.group(2))):
            n += 1
            return m.group(1) + "General" + m.group(3)
        return m.group(0)

    xml = _CACHE_NUMERIQUE.sub(cache, xml)
    return _CODE_FORMAT.sub(code, xml), n

#: Noeud de texte d'un prefixe que l'on ne sait pas lire : le texte qu'il porte
#: ne serait ni analyse ni verifie.
#: `dgm:t` (SmartArt) n'est pas un noeud de texte mais l'enveloppe d'un corps de
#: texte : le texte y est dans `a:t`, lu comme tel.
NOEUD_INCONNU = re.compile(
    r"<(?!(?:w|a|m|dgm):)[A-Za-z][\w.-]*:(?:t|delText)[\s/>]")

#: Alias et etiquette d'un controle de contenu : champs libres, saisis a la main.
CONTROLE = re.compile(
    r'''<(w:alias|w:tag)(\s[^>]*?)\bw:val=(?:"[^"]*"|'[^']*')([^>]*/>)''')

#: Entites XML, pour retrouver le texte tel qu'un lecteur le voit.
ENTITE = re.compile(r"&(?:#(\d+)|#x([0-9a-fA-F]+)|(lt|gt|amp|quot|apos));")

_ENTITES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}

#: Attributs qui nomment l'auteur d'une revision, d'un commentaire ou d'une
#: session de coedition. `word/people.xml`, que Word cree des qu'un document a
#: circule, porte le nom complet de chaque relecteur.
AUTEUR = re.compile(r'\b(w:author|w15:author|w:initials|w15:userId|'
                    r'w:lastModifiedBy)="[^"]*"')

#: Proprietes du document, dans `docProps/core.xml` et `docProps/app.xml`.
CHAMPS_PROPRIETES = ("dc:creator", "cp:lastModifiedBy", "dc:title",
                     "dc:subject", "cp:keywords", "dc:description",
                     "cp:category", "cp:contentStatus", "Company", "Manager",
                     "Template", "HyperlinkBase")

#: Parties qui ne portent que des metadonnees, retirees de l'archive. Word
#: n'en affiche aucune, et chacune peut nommer le patient :
#:
#: - la **vignette d'apercu**, image de la premiere page *avant* caviardage,
#:   que Word enregistre quand l'option est cochee ;
#: - les **proprietes personnalisees**, ou un logiciel de cabinet ou une GED
#:   range le numero de dossier ;
#: - les **donnees liees** (`customXml/`), qui alimentent les champs d'un
#:   modele : Word y reprend le nom du patient a l'ouverture, et le
#:   reecrirait dans le corps que l'on vient de caviarder ;
#: - les **destinataires d'un publipostage**, c'est-a-dire une liste de
#:   patients.
PARTIES_METADONNEES = (
    (re.compile(r"^docProps/thumbnail\.[A-Za-z0-9]+$"), "vignette d'aperçu"),
    (re.compile(r"^docProps/custom\.xml$"), "propriétés personnalisées"),
    (re.compile(r"^customXml/"), "données liées aux champs du document"),
    (re.compile(r"^word/recipientData\.xml$"), "destinataires de publipostage"),
)

#: Elements de `word/settings.xml` etrangers a la mise en page : variables de
#: document, ou les logiciels de cabinet rangent volontiers le nom ou le
#: numero du patient ; publipostage et sa source de donnees ; chemin du modele,
#: qui nomme le poste et l'utilisateur.
ELEMENTS_REGLAGES = {"w:docVars": "variables de document",
                     "w:mailMerge": "réglages de publipostage",
                     "w:attachedTemplate": "chemin du modèle"}

#: Relations vers le modele attache ou la source d'un publipostage, retirees
#: quelle que soit leur cible.
TYPES_RELATION_RETIRES = ("/attachedTemplate", "/mailMergeSource",
                          "/mailMergeHeaderSource", "/recipientData")

#: Dates d'enregistrement et d'impression, dans `docProps/core.xml` : elles
#: datent souvent la consultation aussi surement que le texte.
DATES_PROPRIETES = ("dcterms:created", "dcterms:modified", "cp:lastPrinted")

#: Listes de `docProps/app.xml` ou Word recopie le titre du document.
LISTES_APP = ("TitlesOfParts", "HeadingPairs")

RELATION = re.compile(r"<Relationship\b[^>]*?/>")

#: Relations vers un contenu incorpore que la chaine ne sait pas lire.
TYPES_RELATION_OBJET = ("/oleObject", "/package", "/aFChunk")

#: Ce qui remplace un objet incorpore retire.
MENTION_OBJET = "[objet incorporé retiré]"

_OBJET_OLE = re.compile(r"<w:object\b(?:[^>]*?/>|.*?</w:object>)", re.DOTALL)
_ALTCHUNK = re.compile(r"<w:altChunk\b(?:[^>]*?/>|.*?</w:altChunk>)", re.DOTALL)
_IDS_RELATION = re.compile(r'\b(?:r:id|r:embed|r:link|r:pict|o:relid)="([^"]*)"')

#: Attributs dont la valeur est structurelle, jamais du texte : un
#: identifiant numerique detecte par erreur ne doit pas y etre remplace.
ATTRIBUT_STRUCTUREL = re.compile(
    r'\b(w:rsid[A-Za-z]*|w14:paraId|w14:textId|w:id|r:id|r:embed|Id)="[^"]*"')

#: Prefixes d'entrees de l'archive qui ne sont pas du XML et que la chaine
#: texte ne sait pas traiter.
PREFIXE_MEDIA = "word/media/"
PREFIXE_OBJET = "word/embeddings/"

#: Nom lisible des parties, pour la sortie texte. Un .docx n'a pas de pages.
INTITULES = {"document": "corps du document", "header": "en-tete",
             "footer": "pied de page", "footnotes": "notes de bas de page",
             "endnotes": "notes de fin", "comments": "commentaires"}

#: Sous ce nombre de mots, un .docx qui contient des images n'est qu'une
#: enveloppe autour de pages scannees.
MOTS_ENVELOPPE = 20


class ChaineImageRequise(RuntimeError):
    """Le contenu du document est dans ses images, pas dans son texte.

    Anonymiser le texte d'un tel document produirait un fichier qui parait
    traite alors que tout y reste lisible. C'est exactement l'echec que la
    chaine image existe pour eviter ; on refuse plutot que de le commettre.
    """


@dataclass(frozen=True)
class Segment:
    """Un noeud de texte du XML, et sa place dans le texte logique.

    Deux systemes de coordonnees coexistent : celles du XML, qui servent a
    reecrire le fichier, et celles du texte logique, sur lesquelles porte la
    detection. Le segment fait le lien entre les deux.
    """

    #: Position de l'element complet, `<w:t …>…</w:t>`, dans la chaine XML.
    debut_xml: int
    fin_xml: int
    balise: str
    attrs: str
    #: Position du contenu dans le texte logique de la partie.
    debut: int
    fin: int


@dataclass
class RapportPartie:
    """Ce qui a ete fait d'une partie de l'archive."""

    nom: str
    caracteres: int = 0
    entites: list[Span] = field(default_factory=list)
    bruts: list[Span] = field(default_factory=list)
    remplacements: int = 0
    duree: float = 0.0
    #: « redigee » (detection complete) ou « propagation » (chaines deja
    #: connues seulement).
    source: str = "redigee"


@dataclass
class RapportDocx:
    source: Path
    sortie: Path
    parties: list[RapportPartie] = field(default_factory=list)
    duree: float = 0.0
    fuites: list[Fuite] = field(default_factory=list)
    verifie: bool = False
    noms_patient: set[str] = field(default_factory=set)
    texte: str = ""
    fuites_texte: list[Fuite] = field(default_factory=list)
    #: Chaines que la verification relisait encore, puis remplacees dans le
    #: XML ou noircies dans les images. Voir `_corriger_fuites`.
    corrigees: list[Fuite] = field(default_factory=list)
    #: Chaines restees en clair dans le texte produit, puis remplacees.
    corrigees_texte: list[Fuite] = field(default_factory=list)
    repli_complet: bool = False
    erreur_verification: str = ""
    #: Images presentes dans l'archive, retirees ou non.
    images: int = 0
    #: Compte rendu de chaque image lue : ce qu'on y a masque, ce qu'on y relit.
    images_lues: list[RapportImage] = field(default_factory=list)
    #: Objets incorpores (classeur, PDF colle) : idem.
    objets: int = 0
    #: Proprietes de document neutralisees.
    proprietes: int = 0
    #: Options de zones sans equivalent dans un traitement de texte.
    zones_ignorees: list[str] = field(default_factory=list)
    #: Metadonnees retirees, en clair : « vignette d'aperçu »...
    metadonnees: list[str] = field(default_factory=list)
    #: Images d'un format qu'on ne sait pas nettoyer et qui portent des
    #: metadonnees : elles y restent, et cela doit se voir.
    images_metadonnees: list[str] = field(default_factory=list)
    #: Dates rendues en J+n (`PolitiqueDates.RELATIVES`).
    dates_relatives: int = 0

    @property
    def entites_total(self) -> int:
        """Entites masquees dans le document, texte et images confondus.

        Une entite noircie dans une capture d'ecran est masquee au meme titre
        qu'un nom reecrit dans le corps : les compter a part reviendrait a
        annoncer moins de travail qu'il n'en a ete fait.
        """
        return (sum(len(p.entites) for p in self.parties)
                + sum(len(i.entites) for i in self.images_lues))

    @property
    def images_masquees(self) -> int:
        """Images dont au moins un mot a ete noirci."""
        return sum(1 for i in self.images_lues if i.boites)

    @property
    def images_illisibles(self) -> int:
        """Images qu'on n'a su ni lire ni reecrire : leur contenu reste opaque."""
        return sum(1 for i in self.images_lues if not i.lue)

    @property
    def succes(self) -> bool:
        return not self.fuites and not self.fuites_texte


# --------------------------------------------------------------------------- #
# Lecture du texte logique
# --------------------------------------------------------------------------- #

def _desechapper(brut: str) -> str:
    """Texte d'un noeud, entites resolues."""
    def _un(m: re.Match) -> str:
        if m.group(1):
            return chr(int(m.group(1)))
        if m.group(2):
            return chr(int(m.group(2), 16))
        return _ENTITES[m.group(3)]
    return ENTITE.sub(_un, brut)


#: Fin d'un paragraphe : « </w:p> », ou « <w:p/> » pour un paragraphe vide
#: ecrit sous sa forme courte.
_FIN_DE_PARAGRAPHE = re.compile(r"</w:p>|</a:p>|<w:p\b[^>]*/>")




def _separateur(intervalle: str, avant: str = "", apres: str = "") -> str:
    """Ce qui separe deux noeuds de texte, vu du lecteur.

    Sans ces separateurs, deux paragraphes se collent et les reperes de
    contexte disparaissent : « Ne le » et « 12.03.1958 » doivent rester sur la
    meme ligne pour que la politique des dates reconnaisse une date de
    naissance, et deux noms de paragraphes differents ne doivent pas fusionner
    en une chaine unique.

    L'ordre des essais suit celui des structures, de la plus englobante a la
    plus fine. Une cellule de tableau contient toujours un paragraphe : sans
    cette precedence, chaque cellule tomberait sur sa propre ligne et
    l'intitule « Date de naissance » se retrouverait separe de la date qu'il
    annonce, dans la colonne d'a cote.

    Un paragraphe vide entre les deux rend une ligne vide, comme a l'ecran :
    sans elle, « 1510 Moudon », une ligne blanche, puis « Grandvaux, le … »
    se lisaient comme une seule adresse sur deux lignes.
    """
    if "</w:tr>" in intervalle:
        return "\n"
    # Texte supprime puis texte insere : deux valeurs distinctes, que le
    # lecteur ne voit pas collees. Recollees, « 12.03.1974 » et « 13.03.1974 »
    # echapperaient aux regles, qui refusent un chiffre accole.
    # Seule la frontiere suppression/insertion separe : deux suppressions de
    # suite, ou deux insertions de suite, sont un meme mot coupe en deux
    # (« Dup » + « ont »), et « Dupon » + « t » insere reste « Dupont ».
    avant_supprime = avant == "w:delText"
    apres_supprime = apres == "w:delText"
    avant_insere = not avant_supprime and re.search(r"</w:ins\b", intervalle)
    apres_insere = not apres_supprime and re.search(r"<w:ins\b", intervalle)
    if (avant_supprime and apres_insere) or (avant_insere and apres_supprime):
        return " "
    if "</w:tc>" in intervalle:
        return "\t"
    if len(_FIN_DE_PARAGRAPHE.findall(intervalle)) > 1:
        return "\n\n"
    if "</w:p>" in intervalle or "</a:p>" in intervalle or "<w:br" in intervalle:
        return "\n"
    if "<w:tab" in intervalle:
        return "\t"
    # Un trait d'union insecable est un tiret pour le lecteur ; le conditionnel
    # n'apparait qu'en fin de ligne et ne s'ecrit pas.
    if "<w:noBreakHyphen" in intervalle:
        return "-"
    return ""


def lire_partie(xml: str) -> tuple[str, list[Segment]]:
    """Texte logique d'une partie de l'archive, et ses segments."""
    morceaux: list[str] = []
    segments: list[Segment] = []
    position = 0
    curseur = 0

    for m in NOEUD_TEXTE.finditer(xml):
        if morceaux:
            sep = _separateur(xml[curseur:m.start()], balise_avant, m.group(1))
            if sep:
                morceaux.append(sep)
                position += len(sep)
        curseur = m.end()
        balise_avant = m.group(1)

        contenu = _desechapper(m.group(4) or "")
        segments.append(Segment(m.start(), m.end(), m.group(1), m.group(2),
                                position, position + len(contenu)))
        morceaux.append(contenu)
        position += len(contenu)

    return "".join(morceaux), segments


# --------------------------------------------------------------------------- #
# Reecriture
# --------------------------------------------------------------------------- #

def _intitule(nom: str) -> str:
    """Nom lisible d'une partie de l'archive."""
    base = nom.rsplit("/", 1)[-1].removesuffix(".xml")
    racine = base.rstrip("0123456789")
    numero = base[len(racine):]
    lisible = INTITULES.get(racine, base)
    if "/glossary/" in nom:
        # Sans ce prefixe, le glossaire prend l'intitule du corps et masque la
        # relecture de ce dernier.
        lisible = f"glossaire : {lisible}"
    return f"{lisible} {numero}" if numero else lisible


def _element(balise: str, attrs: str, contenu: str) -> str:
    """Reconstruit un noeud de texte, en preservant les espaces s'il en porte.

    Word supprime les espaces de tete et de fin d'un noeud qui ne porte pas
    `xml:space="preserve"`. Un pseudonyme pose sur « M. Dupont » peut laisser
    l'espace qui le precedait : sans cet attribut, deux mots se colleraient.
    """
    if contenu != contenu.strip() and "xml:space" not in attrs:
        attrs = f'{attrs} xml:space="preserve"'
    return f"<{balise}{attrs}>{escape(contenu)}</{balise}>"


def remplacer(xml: str, texte: str, segments: list[Segment],
              spans: list[Span], rendre) -> tuple[str, int]:
    """Remplace dans le XML les portions couvertes par les spans.

    Un span chevauche souvent plusieurs noeuds, parce que Word decoupe les mots
    a chaque correction. Le remplacement est alors emis en entier par le
    premier noeud touche, et les suivants sont vides de la portion qui les
    concerne. Rien n'est laisse derriere : c'est la seule facon de ne pas
    laisser la moitie d'un nom dans le fichier produit.
    """
    retouches: dict[int, list[tuple[int, int, str]]] = {}
    remplaces = 0
    # Les segments se suivent sans se chevaucher : une dichotomie donne
    # directement le premier que le span touche.
    fins = [seg.fin for seg in segments]

    for sp in spans:
        rendu = rendre(sp)
        premier = True
        for i in range(bisect_right(fins, sp.start), len(segments)):
            seg = segments[i]
            if seg.debut >= sp.end:
                break
            if seg.fin == seg.debut:
                continue
            debut = max(seg.debut, sp.start) - seg.debut
            fin = min(seg.fin, sp.end) - seg.debut
            retouches.setdefault(i, []).append(
                (debut, fin, rendu if premier else ""))
            premier = False
        if not premier:
            remplaces += 1

    # De droite a gauche : les positions restantes gardent leur sens.
    resultat = xml
    for i in sorted(retouches, reverse=True):
        seg = segments[i]
        contenu = texte[seg.debut:seg.fin]
        for debut, fin, rendu in sorted(retouches[i], reverse=True):
            contenu = contenu[:debut] + rendu + contenu[fin:]
        resultat = (resultat[:seg.debut_xml]
                    + _element(seg.balise, seg.attrs, contenu)
                    + resultat[seg.fin_xml:])
    return resultat, remplaces


def vider_texte(xml: str) -> str:
    """Vide tous les noeuds de texte d'une partie, en gardant sa structure."""
    _, segments = lire_partie(xml)
    for seg in reversed(segments):
        vide = f"<{seg.balise}{seg.attrs}></{seg.balise}>"
        xml = (xml[:seg.debut_xml] + vide
               + xml[seg.fin_xml:])
    return xml


def supprimer_element(xml: str, nom: str) -> tuple[str, int]:
    """Retire tous les elements ``nom``, imbrications comprises.

    Une expression non gourmande couperait au premier `</w:drawing>` venu et
    laisserait une balise fermante orpheline dans un fichier qui doit rester
    valide. On compte donc les ouvertures et les fermetures.
    """
    ouvrant = re.compile(rf"<{nom}(\s[^>]*?)?(/?)>")
    fermant = f"</{nom}>"
    retires = 0

    while True:
        m = ouvrant.search(xml)
        if m is None:
            return xml, retires
        if m.group(2) == "/":          # <w:drawing/> : rien a l'interieur
            xml = xml[:m.start()] + xml[m.end():]
            retires += 1
            continue

        profondeur = 1
        curseur = m.end()
        while profondeur:
            suivant = ouvrant.search(xml, curseur)
            ferme = xml.find(fermant, curseur)
            if ferme == -1:
                return xml, retires     # XML incoherent : on n'y touche pas
            if suivant is not None and suivant.start() < ferme:
                profondeur += 0 if suivant.group(2) == "/" else 1
                curseur = suivant.end()
            else:
                profondeur -= 1
                curseur = ferme + len(fermant)
        xml = xml[:m.start()] + xml[curseur:]
        retires += 1


def neutraliser_auteurs(xml: str) -> str:
    """Remplace le nom des auteurs de revisions et de commentaires."""
    return AUTEUR.sub(lambda m: f'{m.group(1)}="ANONYME"', xml)


def neutraliser_proprietes(xml: str) -> tuple[str, int]:
    """Vide les proprietes du document : auteur, societe, titre, mots-cles."""
    vides = 0
    for champ in CHAMPS_PROPRIETES:
        motif = re.compile(rf"<{champ}(\s[^>]*?)?>(.*?)</{champ}>", re.DOTALL)

        def _vider(m: re.Match) -> str:
            nonlocal vides
            if m.group(2):
                vides += 1
            return f"<{champ}{m.group(1) or ''}></{champ}>"

        xml = motif.sub(_vider, xml)
    return xml, vides


def effacer_chaines(xml: str, remplacements: dict[str, str],
                    ignorer_casse: bool = False) -> str:
    """Remplace les chaines connues partout ailleurs dans le XML.

    Les noeuds de texte sont deja traites ; restent les endroits qu'aucun
    lecteur ne regarde et ou l'identite subsiste pourtant : texte alternatif
    d'une image (`descr="Scan Zorglub.jpg"`), code de champ d'un
    publipostage, cible d'un lien `mailto:`, nom d'un signet.

    Les chaines concernees sont filtrees en amont par ``propageable`` : assez
    longues et assez caracterisees pour ne pas croiser du vocabulaire de
    balisage. Les pseudonymes qui les remplacent ne contiennent ni chevron ni
    guillemet, donc le document reste valide.

    Les identifiants internes de Word sont mis a l'abri le temps du
    remplacement : une detection purement numerique pourrait sinon tomber sur
    un `w:rsidR` et produire un fichier que Word refuse.

    ``ignorer_casse`` aligne la recherche sur celle de la verification, qui
    compare en minuscules et lit le XML tel qu'il est ecrit, entites
    comprises : « ZORGLUB » ou « Dupont &amp; Fils » y sont alors trouves.
    """
    if not remplacements:
        return xml

    gardes: list[str] = []

    def _garder(m: re.Match) -> str:
        gardes.append(m.group(0))
        return f"\x00{len(gardes) - 1}\x00"

    # Le caractere nul est interdit en XML : il ne peut pas entrer en conflit.
    xml = ATTRIBUT_STRUCTUREL.sub(_garder, xml)
    for chaine, pseudonyme in remplacements.items():
        if ignorer_casse:
            for forme in {chaine, escape(chaine, {'"': "&quot;"})}:
                xml = re.sub(re.escape(forme), lambda _: pseudonyme, xml,
                             flags=re.IGNORECASE)
        elif chaine in xml:
            xml = xml.replace(chaine, pseudonyme)
    return re.sub(r"\x00(\d+)\x00", lambda m: gardes[int(m.group(1))], xml)


# --------------------------------------------------------------------------- #
# Chaine complete
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Phases du caviardage
#
# Chaque phase est nommee et isolee. Le graphe de connaissances du projet avait
# designe `caviarder_docx` comme le point le plus connecte du code — 33 aretes
# pour une fonction unique de 168 lignes qui lisait l'archive, appliquait les
# zones, detectait, arbitrait les roles, propageait, reecrivait le XML,
# neutralisait les annexes, reassemblait et verifiait. Aucune de ces etapes
# n'etait atteignable seule, donc aucune n'etait testable seule.
# --------------------------------------------------------------------------- #

#: Octets decompresses lus au total dans une archive, et nombre d'entrees. Le
#: meme plafond que `interface.controler_docx`, mais applique aux octets
#: reellement lus : l'en-tete d'un ZIP peut mentir sur la taille gonflee.
INFLATION_MAX_DOCX = 1024 * 1024 * 1024
ENTREES_MAX_DOCX = 10_000


def _lire_borne(z: zipfile.ZipFile, infos) -> dict[str, bytes]:
    """Lit les entrees en bornant les octets effectivement decompresses."""
    if len(infos) > ENTREES_MAX_DOCX:
        raise ValueError(f"document trop complexe ({len(infos)} entrees, "
                         f"maximum {ENTREES_MAX_DOCX} entrees)")
    reste = INFLATION_MAX_DOCX
    contenu: dict[str, bytes] = {}
    for info in infos:
        with z.open(info) as f:
            octets = f.read(reste + 1)
        reste -= len(octets)
        if reste < 0:
            raise ValueError("document trop volumineux une fois decompresse "
                             f"(maximum {INFLATION_MAX_DOCX // (1 << 20)} Mo)")
        contenu[info.filename] = octets
    return contenu


def _lire_archive(source: Path) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    """Ouvre le .docx et rend ses entrees et leur contenu brut.

    Tout est charge en memoire : un .docx depasse rarement quelques megaoctets,
    et les passes suivantes le parcourent plusieurs fois.
    """
    with zipfile.ZipFile(source) as z:
        entrees = z.infolist()
        contenu = _lire_borne(z, entrees)
    return entrees, contenu


def _retirer_objets(contenu: dict[str, bytes], noms: list[str],
                    rapport: RapportDocx) -> set[str]:
    """Retire les objets incorpores : OLE, paquets, altChunk et leurs apercus.

    Ces contenus ne sont ni lus ni verifies ; les recopier tels quels dans un
    document « anonymise » serait une fuite. Chaque `w:object` devient une
    mention visible (son image d'apercu peut montrer le contenu de l'objet),
    chaque `w:altChunk` un paragraphe de mention. Rend les parties a omettre de
    l'archive et porte le compte dans `rapport.objets`.
    """
    mention = f'<w:t xml:space="preserve">{MENTION_OBJET}</w:t>'
    paragraphe = f"<w:p><w:r>{mention}</w:r></w:p>"

    def rels_de(partie: str) -> str:
        dossier, _, fichier = partie.rpartition("/")
        return f"{dossier}/_rels/{fichier}.rels" if dossier else f"_rels/{fichier}.rels"

    def cible(base: str, relation: str) -> str:
        t = _attribut(relation, "Target")
        if _attribut(relation, "TargetMode") == "External":
            return ""
        return (t.lstrip("/") if t.startswith("/")
                else posixpath.normpath(posixpath.join(base, t)))

    # --- 1. les parties de l'archive a retirer --------------------------------
    parties: set[str] = {n for n in noms if n.startswith(PREFIXE_OBJET)}
    for nom in noms:
        if not nom.endswith(".rels"):
            continue
        base = posixpath.dirname(posixpath.dirname(nom))
        for relation in RELATION.findall(contenu[nom].decode("utf-8")):
            if _attribut(relation, "Type").endswith(TYPES_RELATION_OBJET):
                if (c := cible(base, relation)) in contenu:
                    parties.add(c)

    # --- 2. le XML : chaque objet devient une mention -------------------------
    elements = 0
    liees: set[str] = set()          # parties d'objet atteintes par un element
    apercus: set[str] = set()        # images d'apercu, a retirer si orphelines
    ids_par_rels: dict[str, set[str]] = {}
    for nom in noms:
        if not (nom.startswith("word/") and nom.endswith(".xml")):
            continue
        xml = contenu[nom].decode("utf-8", "replace")
        if "<w:object" not in xml and "<w:altChunk" not in xml:
            continue
        ids: set[str] = set()

        def noter(m: re.Match, remplacement: str) -> str:
            ids.update(_IDS_RELATION.findall(m.group(0)))
            return remplacement

        xml, n_ole = _OBJET_OLE.subn(lambda m: noter(m, mention), xml)
        xml, n_alt = _ALTCHUNK.subn(lambda m: noter(m, paragraphe), xml)
        elements += n_ole + n_alt
        contenu[nom] = xml.encode("utf-8")
        ids_par_rels[rels_de(nom)] = ids

    # --- 3. les relations ------------------------------------------------------
    retirees = set(parties)
    orphelins: dict[str, set[str]] = {}   # rels -> identifiants retires
    for nom in noms:
        if not nom.endswith(".rels"):
            continue
        base = posixpath.dirname(posixpath.dirname(nom))
        ids = ids_par_rels.get(nom, set())

        def filtrer(m: re.Match) -> str:
            relation = m.group(0)
            c = cible(base, relation)
            objet = (_attribut(relation, "Type").endswith(TYPES_RELATION_OBJET)
                     or c in parties)
            if objet or _attribut(relation, "Id") in ids:
                orphelins.setdefault(nom, set()).add(_attribut(relation, "Id"))
                if c in parties and _attribut(relation, "Id") in ids:
                    liees.add(c)
                elif c and not objet:
                    apercus.add(c)
                return ""
            return relation

        xml = contenu[nom].decode("utf-8")
        purge = RELATION.sub(filtrer, xml)
        if purge != xml:
            contenu[nom] = purge.encode("utf-8")

    # --- 3b. plus aucun identifiant ne doit viser une relation retiree --------
    for rels, retires in orphelins.items():
        dossier, _, fichier = rels.rpartition("/_rels/")
        partie = f"{dossier}/{fichier[:-5]}" if dossier else rels[len("_rels/"):-5]
        if partie not in contenu or not partie.endswith(".xml"):
            continue
        attrs = "|".join(re.escape(i) for i in retires if i)
        if not attrs:
            continue
        visant = r'\s+(?:r:id|r:embed|r:link|r:pict|o:relid)="(?:%s)"' % attrs
        xml = contenu[partie].decode("utf-8", "replace")
        # Un element dont le seul lien est la relation retiree (`c:externalData`,
        # `o:OLEObject`...) disparait en entier : sans son r:id, Word le juge
        # corrompu. Seul un attribut isole est retire.
        xml = re.sub(r"<([\w:]+)\b[^<>]*?" + visant + r"[^<>]*?/>", "", xml)
        xml = re.sub(r"<([\w:]+)\b[^<>]*?" + visant + r"[^<>]*?>.*?</\1>", "",
                     xml, flags=re.DOTALL)
        xml = re.sub(visant, "", xml)
        contenu[partie] = xml.encode("utf-8")

    # --- 4. les apercus que plus rien ne reference ----------------------------
    restantes = set()
    for nom in noms:
        if nom.endswith(".rels") and nom in contenu:
            base = posixpath.dirname(posixpath.dirname(nom))
            restantes.update(cible(base, r) for r in
                             RELATION.findall(contenu[nom].decode("utf-8")))
    retirees |= {a for a in apercus if a in contenu and a not in restantes}

    types = "[Content_Types].xml"
    if retirees and types in contenu:
        xml = contenu[types].decode("utf-8")
        xml = re.sub(r'<Override\b[^>]*?PartName="/([^"]+)"[^>]*/>',
                     lambda m: "" if m.group(1) in retirees else m.group(0), xml)
        contenu[types] = xml.encode("utf-8")

    rapport.objets = elements + len(parties - liees)
    return retirees


def _appliquer_zones(contenu: dict[str, bytes], noms: list[str],
                     zones: OptionsZones, rapport: RapportDocx) -> set[str]:
    """Retire logos et en-tetes avant toute lecture.

    L'ordre compte : ce qui est retire n'a plus a etre analyse, ni par le
    modele ni par la verification finale. Rend les noms des parties a exclure
    de l'archive produite.
    """
    if zones.bande_haute:
        rapport.zones_ignorees.append("bande-haute")

    supprimees: set[str] = set()
    if zones.logos and rapport.images:
        for nom in noms:
            if PARTIE_REDIGEE.match(nom):
                xml = contenu[nom].decode("utf-8")
                xml, _ = supprimer_element(xml, "w:drawing")
                xml, _ = supprimer_element(xml, "w:pict")
                contenu[nom] = xml.encode("utf-8")
        supprimees = {n for n in noms if n.startswith(PREFIXE_MEDIA)}
        rapport.images = 0

    if zones.entetes:
        for nom in noms:
            if PARTIE_ENTETE.match(nom):
                contenu[nom] = vider_texte(
                    contenu[nom].decode("utf-8")).encode("utf-8")
    return supprimees


def _lire_parties(contenu: dict[str, bytes],
                  noms: list[str]) -> dict[str, tuple[str, list[Segment]]]:
    """Extrait le texte logique et la carte des segments de chaque partie XML."""
    return {nom: lire_partie(contenu[nom].decode("utf-8"))
            for nom in noms
            if nom.endswith(".xml") and nom.startswith("word/")}


def _refuser_si_scan(source: Path, lectures: dict[str, tuple[str, list[Segment]]],
                     redigees: list[str], rapport: RapportDocx,
                     contenu: dict[str, bytes] | None = None) -> None:
    """Rejette un .docx qui n'est qu'une enveloppe autour d'une image.

    Le refus precede la detection : inutile de faire tourner le modele sur un
    document dont on sait deja qu'il ne sera pas traite. Et un tel document
    doit etre refuse plutot que traite a moitie — son contenu est dans le
    scan, que la chaine bureautique ne sait pas lire.
    """
    if not redigees:
        raise ChaineImageRequise(
            f"{source.name} ne contient aucune partie de texte exploitable")

    # Ce que la lecture ne sait pas voir ne doit pas passer pour « verifie » :
    # un document qui cache son contenu hors de `w:t` est refuse.
    if "word/document.xml" not in redigees:
        raise ChaineImageRequise(
            f"{source.name} n'a pas de corps `word/document.xml` lisible")
    for nom, octets in (contenu or {}).items():
        if not (nom.startswith("word/") and nom.endswith(".xml")):
            continue
        brut = octets.decode("utf-8", "replace")
        if "<w:altChunk" in brut:
            raise ChaineImageRequise(
                f"{source.name} incorpore un contenu externe (altChunk) que "
                "la chaine bureautique ne sait pas lire")
        if nom in redigees and NOEUD_INCONNU.search(brut):
            raise ChaineImageRequise(
                f"{source.name} porte du texte sous un prefixe XML inconnu "
                f"({nom}) : il ne serait ni analyse ni verifie")
    if not any(lectures[n][0].strip() for n in redigees):
        raise ChaineImageRequise(f"{source.name} ne contient aucun texte")

    mots = sum(len(lectures[n][0].split()) for n in redigees)
    if mots < MOTS_ENVELOPPE and rapport.images:
        raise ChaineImageRequise(
            f"{source.name} ne porte presque pas de texte ({mots} mots) mais "
            f"contient {rapport.images} image(s) : son contenu est "
            "probablement scanne. Exportez-le en PDF et traitez-le par la "
            "chaine image.")


def _detecter_parties(model, redigees: list[str],
                      lectures: dict[str, tuple[str, list[Segment]]],
                      niveau: Niveau, rapport: RapportDocx,
                      kw_analyse: dict) -> None:
    """Detecte les entites dans chaque partie redigee."""
    for nom in redigees:
        t0 = time.monotonic()
        texte, _ = lectures[nom]
        rp = RapportPartie(nom=nom, caracteres=len(texte))
        if texte.strip():
            # Les detections brutes ne servent qu'au raisonnement par roles ;
            # au niveau complet, on s'en passe.
            if niveau is Niveau.COMPLET:
                rp.entites = analyse(model, texte, **kw_analyse)
            else:
                rp.entites, rp.bruts = analyse_detaillee(model, texte,
                                                         **kw_analyse)
        rp.duree = time.monotonic() - t0
        rapport.parties.append(rp)


def _appliquer_roles(lectures: dict[str, tuple[str, list[Segment]]],
                     rapport: RapportDocx, niveau: Niveau) -> None:
    """Identifie le patient sur l'ensemble du document, puis filtre.

    L'identification est faite document par document et non partie par partie :
    un nom peut n'etre accompagne de sa date de naissance que dans le corps, et
    apparaitre seul dans un en-tete.
    """
    if niveau is Niveau.COMPLET:
        return

    for rp in rapport.parties:
        texte = lectures[rp.nom][0]
        rapport.noms_patient |= noms_du_patient(texte, rp.bruts + rp.entites)

    if rapport.noms_patient:
        for rp in rapport.parties:
            rp.entites = filtrer_par_role(lectures[rp.nom][0], rp.entites,
                                          niveau, rapport.noms_patient)
    else:
        # Sans patient identifie, le filtrage par role n'a aucun sens : on
        # masque tout plutot que de choisir au hasard ce qui reste lisible.
        rapport.repli_complet = True


def _propager(lectures: dict[str, tuple[str, list[Segment]]],
              rapport: RapportDocx, table: TablePseudonymes
              ) -> tuple[dict[str, list[Span]], dict[str, str], dict[str, str]]:
    """Etend les detections a toutes les parties, y compris non redigees.

    Une chaine sensible dans le corps l'est aussi dans un en-tete, un pied de
    page ou une note — parties que la detection ne parcourt pas. Rend les spans
    finaux par partie, les chaines connues et leur pseudonyme.
    """
    connues = chaines_connues(sp for rp in rapport.parties for sp in rp.entites)
    table.qualifier(qualifier_personnes(
        [(lectures[rp.nom][0], rp.entites) for rp in rapport.parties
         if rp.nom in lectures]))
    table.enregistrer(sp for rp in rapport.parties for sp in rp.entites)

    finaux: dict[str, list[Span]] = {}
    for nom, (texte, _segments) in lectures.items():
        rp = next((p for p in rapport.parties if p.nom == nom), None)
        spans = list(rp.entites) if rp else []
        spans = _merge(propager_chaines(texte, spans, connues), texte)
        finaux[nom] = spans
        if rp is None and spans:
            rapport.parties.append(
                RapportPartie(nom=nom, caracteres=len(texte),
                              entites=spans, source="propagation"))

    pseudonymes = {chaine: f"[{table.pseudonyme(chaine, etiquette)}]"
                   for chaine, etiquette in connues.items()}
    return finaux, connues, pseudonymes


def _reecrire_parties(contenu: dict[str, bytes],
                      lectures: dict[str, tuple[str, list[Segment]]],
                      finaux: dict[str, list[Span]],
                      pseudonymes: dict[str, str], table: TablePseudonymes,
                      rapport: RapportDocx) -> None:
    """Reecrit chaque partie XML : spans remplaces, auteurs neutralises."""
    for nom, (texte, segments) in lectures.items():
        xml, n = remplacer(contenu[nom].decode("utf-8"), texte, segments,
                           finaux[nom], table.rendre)
        xml = effacer_chaines(neutraliser_auteurs(xml), pseudonymes)
        xml, nb = _masquer_caches_numeriques(xml)
        contenu[nom] = xml.encode("utf-8")
        for rp in rapport.parties:
            if rp.nom == nom:
                rp.remplacements = n + nb


def _neutraliser_annexes(contenu: dict[str, bytes], noms: list[str],
                         pseudonymes: dict[str, str],
                         rapport: RapportDocx) -> None:
    """Nettoie les parties que personne ne regarde : proprietes et relations.

    Un nom d'auteur dans `docProps/core.xml` ou une cible de lien dans un
    `.rels` survit a tout caviardage du corps. Ce sont les recoins ou une
    identite subsiste le plus souvent.
    """
    for nom in noms:
        if PARTIE_REDIGEE.match(nom):
            contenu[nom] = CONTROLE.sub(
                r'<\1\2w:val=""\3', contenu[nom].decode("utf-8")
            ).encode("utf-8")
        elif nom.startswith("docProps/") and nom.endswith(".xml"):
            xml, vides = neutraliser_proprietes(contenu[nom].decode("utf-8"))
            rapport.proprietes += vides
            contenu[nom] = effacer_chaines(xml, pseudonymes).encode("utf-8")
        elif nom.endswith(".rels"):
            contenu[nom] = effacer_chaines(
                contenu[nom].decode("utf-8"), pseudonymes).encode("utf-8")


def _caviarder_images(model, contenu: dict[str, bytes], noms: list[str],
                      supprimees: set[str], connues: dict[str, str],
                      langue: str, niveau: Niveau,
                      rapport: RapportDocx, kw_analyse: dict) -> None:
    """Masque dans chaque image ce que le document porte de personnel.

    La passe vient apres la reecriture du texte, donc apres la propagation et
    apres la relecture humaine : les chaines cherchees dans les images sont
    exactement celles que le document a retenues, decisions de l'utilisateur
    comprises. L'inverse — une chaine trouvee dans une image et propagee sur le
    texte — n'est pas fait : il demanderait une seconde propagation, et le cas
    est etroit puisqu'un nom present dans une image l'est presque toujours
    aussi dans le corps, ou la detection le lit mieux.

    Les images que `--logos` a retirees ne sont pas lues : elles ne seront pas
    dans l'archive produite.
    """
    for nom in noms:
        if not nom.startswith(PREFIXE_MEDIA) or nom in supprimees:
            continue
        contenu[nom], compte_rendu = caviarder_image(
            model, nom, contenu[nom], connues=connues, langue=langue,
            niveau=niveau, noms_patient=rapport.noms_patient, **kw_analyse)
        rapport.images_lues.append(compte_rendu)


def _attribut(element: str, nom: str) -> str:
    m = re.search(rf'\b{nom}="([^"]*)"', element)
    return m.group(1) if m else ""


def _purger_metadonnees(contenu: dict[str, bytes], noms: list[str],
                        supprimees: set[str], rapport: RapportDocx) -> set[str]:
    """Retire ce que le document porte sans l'afficher. Rend les parties retirees.

    Le caviardage du corps n'y touche pas, et la relecture finale ne les voit
    qu'a moitie : elle cherche les chaines *connues*, alors qu'un numero de
    dossier range dans une propriete personnalisee n'a jamais ete detecte, et
    qu'aucune lecture du texte ne voit les coordonnees GPS d'une photo.

    On retire plutot qu'on ne caviarde : rien de tout cela ne sert au
    destinataire d'un document anonymise.
    """
    def noter(libelle: str) -> None:
        if libelle not in rapport.metadonnees:
            rapport.metadonnees.append(libelle)

    if rapport.proprietes:
        noter("propriétés du document (auteur, titre…)")

    # --- 1. les parties entieres -------------------------------------------
    retirees: set[str] = set()
    for nom in noms:
        if nom in supprimees:
            continue
        for motif, libelle in PARTIES_METADONNEES:
            if motif.match(nom):
                retirees.add(nom)
                noter(libelle)
                break
    vivantes = [n for n in noms if n not in supprimees and n not in retirees]

    # --- 2. les elements, dans les parties qui restent ----------------------
    for nom in vivantes:
        if not nom.endswith(".xml"):
            continue
        xml = avant = contenu[nom].decode("utf-8")
        if PARTIE_REDIGEE.match(nom):
            # Une liaison sans ses donnees n'a plus rien a lier : on la retire
            # pour que Word garde le texte caviarde tel qu'il est.
            for element in ("w:dataBinding", "w15:dataBinding"):
                xml, _ = supprimer_element(xml, element)
        elif nom == "word/settings.xml":
            for element, libelle in ELEMENTS_REGLAGES.items():
                xml, n = supprimer_element(xml, element)
                if n:
                    noter(libelle)
        elif nom == "docProps/core.xml":
            for element in DATES_PROPRIETES:
                xml, n = supprimer_element(xml, element)
                if n:
                    noter("dates d'enregistrement")
        elif nom == "docProps/app.xml":
            for element in LISTES_APP:
                xml, _ = supprimer_element(xml, element)
        if xml != avant:
            contenu[nom] = xml.encode("utf-8")

    # --- 3. les relations ----------------------------------------------------
    for nom in vivantes:
        if not nom.endswith(".rels"):
            continue
        # « word/_rels/document.xml.rels » decrit les cibles de « word/ ».
        base = posixpath.dirname(posixpath.dirname(nom))

        def filtrer(m: re.Match) -> str:
            relation = m.group(0)
            cible = _attribut(relation, "Target")
            if _attribut(relation, "Type").endswith(TYPES_RELATION_RETIRES):
                return ""
            if _attribut(relation, "TargetMode") == "External":
                # Un lien vers un fichier local nomme le poste, l'utilisateur
                # et souvent le dossier du patient : « C:/Patients/Dupont/ ».
                if cible.lower().startswith("file:"):
                    noter("chemins de fichiers locaux")
                    return relation.replace(f'Target="{cible}"', 'Target="file:///"')
                return relation
            chemin = (cible.lstrip("/") if cible.startswith("/")
                      else posixpath.normpath(posixpath.join(base, cible)))
            return "" if chemin in retirees else relation

        xml = contenu[nom].decode("utf-8")
        purge = RELATION.sub(filtrer, xml)
        if purge != xml:
            contenu[nom] = purge.encode("utf-8")

    types = "[Content_Types].xml"
    if retirees and types in contenu:
        xml = contenu[types].decode("utf-8")
        xml = re.sub(r'<Override\b[^>]*?PartName="/([^"]+)"[^>]*/>',
                     lambda m: "" if m.group(1) in retirees else m.group(0), xml)
        contenu[types] = xml.encode("utf-8")

    # --- 4. les images ---------------------------------------------------------
    nettoyees = 0
    for nom in vivantes:
        if not nom.startswith(PREFIXE_MEDIA):
            continue
        resultat = nettoyer_image(contenu[nom])
        if resultat.retires:
            contenu[nom] = resultat.octets
            nettoyees += 1
        elif resultat.non_pris_en_charge and porte_des_metadonnees(contenu[nom]):
            rapport.images_metadonnees.append(nom.rsplit("/", 1)[-1])
    if nettoyees:
        noter("métadonnées d'une image (auteur, date, GPS…)" if nettoyees == 1
              else f"métadonnées de {nettoyees} images (auteur, date, GPS…)")

    return retirees


def _assembler_archive(entrees: list[zipfile.ZipInfo],
                       contenu: dict[str, bytes],
                       supprimees: set[str]) -> bytes:
    """Reassemble le .docx, en omettant les parties retirees."""
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for info in entrees:
            if info.filename in supprimees:
                continue
            # Entree neuve : la date, le champ extra et le commentaire de
            # l'original ne sont pas recopies.
            neuve = zipfile.ZipInfo(info.filename)
            neuve.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(neuve, contenu[info.filename])
    return tampon.getvalue()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def caviarder_docx(model, source, sortie, verifier: bool = True,
                   zones: OptionsZones | None = None,
                   langue: str = LANGUE,
                   niveau: Niveau = Niveau.COMPLET,
                   formats: frozenset[str] = frozenset({"pdf"}),
                   table: TablePseudonymes | None = None,
                   revue=None,
                   **kw_analyse) -> RapportDocx:
    """Anonymise un .docx en place, et rend le compte rendu de l'operation.

    ``formats`` suit la convention de la chaine image : « pdf » designe le
    document dans son format d'origine — ici un .docx —, « texte » la version
    pseudonymisee destinee a un modele de langage.

    ``langue`` ne sert qu'aux images incorporees, seule partie du document que
    l'on doive reconnaitre optiquement.
    """
    depart = time.monotonic()
    source, sortie = Path(source), Path(sortie)
    rapport = RapportDocx(source=source, sortie=sortie)
    table = table if table is not None else TablePseudonymes()
    zones = zones or OptionsZones()

    entrees, contenu = _lire_archive(source)
    noms = list(contenu)
    objets_retires = _retirer_objets(contenu, noms, rapport)
    rapport.images = sum(1 for n in noms if n.startswith(PREFIXE_MEDIA)
                         and n not in objets_retires)

    supprimees = _appliquer_zones(contenu, noms, zones, rapport) | objets_retires
    lectures = _lire_parties(contenu, noms)
    redigees = sorted(n for n in noms if PARTIE_REDIGEE.match(n))

    _refuser_si_scan(source, lectures, redigees, rapport, contenu)
    _detecter_parties(model, redigees, lectures, niveau, rapport, kw_analyse)
    _appliquer_roles(lectures, rapport, niveau)

    # --- relecture humaine --------------------------------------------------
    # Meme regle que la chaine image : apres que tout a ete detecte et
    # propage, avant qu'un seul caractere ait ete reecrit. La relecture porte
    # sur les parties redigees, les seules dont l'utilisateur voit le texte ;
    # la propagation reportera ses decisions sur les en-tetes et les notes.
    finaux, connues, pseudonymes = _propager(lectures, rapport, table)
    if revue is not None:
        # L'utilisateur voit des intitules lisibles — « corps », « en-tete 1 » —
        # et non les noms de l'archive : la correspondance se fait ici.
        par_intitule = {_intitule(nom): nom for nom in redigees}
        # Les quasi-identifiants sont montres, pas masques : voir la chaine
        # image, qui fait de meme.
        approuves = revue([(intitule, lectures[nom][0],
                            finaux[nom] + quasi_identifiants(
                                model, lectures[nom][0], finaux[nom]))
                           for intitule, nom in par_intitule.items()])
        decide = {par_intitule[i]: spans for i, spans in approuves.items()
                  if i in par_intitule}
        for partie in rapport.parties:
            if partie.nom in decide:
                partie.entites = decide[partie.nom]
        # Les decisions changent l'ensemble des chaines connues : il faut
        # repropager, sinon un masque retire ici resterait pose ailleurs, et
        # la comparaison entre les deux occurrences trahirait ce qu'on cache.
        finaux, connues, pseudonymes = _propager(lectures, rapport, table)

    if kw_analyse.get("dates") is PolitiqueDates.RELATIVES:
        # Apres la relecture : J0 est la plus ancienne des dates reellement
        # masquees, sur tout le document.
        table = TableDatesRelatives(table, echelle(
            sp for spans in finaux.values() for sp in spans))
        rapport.dates_relatives = len(table.libelles)
        pseudonymes = {chaine: f"[{table.pseudonyme(chaine, etiquette)}]"
                       for chaine, etiquette in connues.items()}

    _reecrire_parties(contenu, lectures, finaux, pseudonymes, table, rapport)
    _neutraliser_annexes(contenu, noms, pseudonymes, rapport)
    _caviarder_images(model, contenu, noms, supprimees, connues, langue,
                      niveau, rapport, kw_analyse)
    supprimees = supprimees | _purger_metadonnees(contenu, noms, supprimees,
                                                  rapport)

    octets = _assembler_archive(entrees, contenu, supprimees)

    if "texte" in formats:
        _produire_texte(lectures, redigees, finaux, connues, table, rapport)

    if verifier:
        try:
            # `connues` porte deja l'etiquette de chaque chaine : c'est elle
            # qui dira, en cas de fuite, si l'on a laisse passer un nom ou une
            # ville. Un .docx n'est pas pagine avant d'etre rendu : pas de
            # numero de page a donner ici.
            #
            # Les images sont relues au meme titre que le XML — une fuite en
            # pixels est une fuite du document — et l'on reporte au passage,
            # sur chaque image, ce qu'on y a retrouve : c'est le « ou » sans
            # lequel une alerte ne se leve pas.
            xml, par_image = _relire_archive(octets, connues, langue)
            if xml or par_image:
                corrigees, xml, par_image = _corriger_fuites(
                    contenu, supprimees, xml, par_image, connues, table,
                    langue)
                if corrigees:
                    octets = _assembler_archive(entrees, contenu, supprimees)
                    rapport.corrigees = depuis_chaines(corrigees, connues)
            _reporter_fuites(par_image, connues, rapport)
            rapport.fuites = depuis_chaines(
                xml | set(par_image), connues,
                raisons={**{c: RAISON_XML for c in xml},
                         **{c: RAISON_IMAGE for c in par_image}})
            rapport.verifie = True
        except TesseractIndisponible:
            # Sans OCR, les images ne sont pas verifiees : on s'arrete, on ne
            # le range pas parmi les erreurs de verification.
            raise
        except Exception as exc:                      # pragma: no cover
            rapport.erreur_verification = f"{type(exc).__name__}: {exc}"

    if formats & {"pdf", "docx", "document"}:
        sortie.write_bytes(octets)
    if "texte" in formats:
        sortie.with_suffix(".txt").write_text(rapport.texte, encoding="utf-8")

    rapport.duree = time.monotonic() - depart
    return rapport


def _produire_texte(lectures: dict[str, tuple[str, list[Segment]]],
                    redigees: list[str], finaux: dict[str, list[Span]],
                    connues: dict[str, str], table: TablePseudonymes,
                    rapport: RapportDocx) -> None:
    """Assemble la version texte, et y remplace ce qui resterait en clair."""
    morceaux = {n: (lectures[n][0], finaux[n]) for n in redigees}

    def en_clair() -> set[str]:
        rapport.texte = assembler_texte(
            [morceaux[n] for n in redigees], table,
            [_intitule(n) for n in redigees])
        return chaines_lisibles(rapport.texte, connues)

    restantes = en_clair()
    if restantes:
        a_corriger = {c: connues[c] for c in restantes}
        for n, (texte, spans) in morceaux.items():
            morceaux[n] = (texte, _merge(
                spans + occurrences_relues(texte, a_corriger), texte))
        avant, restantes = restantes, en_clair()
        rapport.corrigees_texte = depuis_chaines(avant - restantes, connues)
    rapport.fuites_texte = depuis_chaines(
        restantes, connues,
        raisons={c: RAISON_TEXTE_EXPORTE for c in restantes})


def _corriger_fuites(contenu: dict[str, bytes], supprimees: set[str],
                     xml: set[str], par_image: dict[str, list[str]],
                     connues: dict[str, str], table: TablePseudonymes,
                     langue: str
                     ) -> tuple[set[str], set[str], dict[str, list[str]]]:
    """Remplace ou noircit ce que la verification relit encore.

    Meme principe que la chaine image (`caviardage.corriger_fuites`), en deux
    gestes selon l'endroit :

    - dans le XML, la chaine est remplacee par son pseudonyme : dans le texte
      logique d'abord, ou elle peut etre repartie sur plusieurs noeuds, puis
      partout ou le XML brut la porte encore — attribut, code de champ, lien ;
    - dans une image, les mots ou l'OCR la relit sont noircis.

    Les recherches suivent exactement celles de la verification, puis les
    parties et les images touchees sont relues. ``contenu`` est modifie en
    place. Rend les chaines corrigees, puis ce qui subsiste dans le XML et
    dans les images.

    Il n'y a pas ici de decision humaine a epargner, contrairement a la chaine
    image : apres la relecture, `caviarder_docx` repropage les chaines connues
    sur tout le document, si bien qu'une occurrence laissee visible a la main
    l'est partout — et n'est alors plus cherchee par la verification.
    """
    corrigees: set[str] = set()

    if xml:
        cibles = {c: connues.get(c, "") for c in xml}
        for nom in _parties_relues(contenu, supprimees):
            try:
                brut = contenu[nom].decode("utf-8")
            except UnicodeDecodeError:
                continue
            texte, segments = lire_partie(brut)
            bas = (texte + "\n" + brut).lower()
            ici = {c: e for c, e in cibles.items() if c.lower() in bas}
            if not ici:
                continue
            spans = occurrences_relues(texte, ici)
            if spans:
                brut, _ = remplacer(brut, texte, segments,
                                    _merge(spans, texte), table.rendre)
            brut = effacer_chaines(
                brut, {c: f"[{table.pseudonyme(c, e)}]" for c, e in ici.items()},
                ignorer_casse=True)
            contenu[nom] = brut.encode("utf-8")
        restantes = _fuites_xml({n: contenu[n] for n in
                                 _parties_relues(contenu, supprimees)}, xml)
        corrigees |= xml - restantes
        xml = restantes

    if par_image:
        par_nom: dict[str, dict[str, str]] = {}
        for chaine, images in par_image.items():
            for nom in images:
                par_nom.setdefault(nom, {})[chaine] = connues.get(chaine, "")
        reecrites: dict[str, bytes] = {}
        for nom, chaines in par_nom.items():
            produit = noircir_relues(contenu[nom], chaines, langue)
            if produit is not None:
                contenu[nom] = reecrites[nom] = produit
        relues = fuites_dans_images(reecrites, set(par_image), langue)
        reste: dict[str, list[str]] = {}
        for chaine, images in par_image.items():
            encore = [n for n in images
                      if n not in reecrites or n in relues.get(chaine, ())]
            if encore:
                reste[chaine] = encore
            else:
                corrigees.add(chaine)
        par_image = reste

    # Une chaine corrigee dans le XML mais encore lue dans une image n'est pas
    # corrigee : elle reste une fuite, et une seule.
    return corrigees - xml - set(par_image), xml, par_image


def _parties_relues(contenu: dict[str, bytes], supprimees: set[str]) -> list[str]:
    """Les parties XML de l'archive produite, celles que la verification lit."""
    return [n for n in contenu if n not in supprimees
            and (n.endswith(".xml") or n.endswith(".rels"))]


def _fuites_xml(parties: dict[str, bytes], interdites) -> set[str]:
    """Chaines interdites lues dans le texte logique ou le XML brut des parties."""
    fuites: set[str] = set()
    for brut in (o.decode("utf-8", "replace") for o in parties.values()):
        texte, _ = lire_partie(brut)
        lus = [replier(texte), replier(brut)]
        fuites |= {c for c in interdites
                   if (a := replier(c)) and any(a in m for m in lus)}
    return fuites


def _relire_archive(octets: bytes, chaines_interdites,
                    langue: str) -> tuple[set[str], dict[str, list[str]]]:
    """Relit l'archive produite : le XML d'un cote, les images de l'autre.

    Rend les chaines retrouvees dans le XML, et pour celles retrouvees dans une
    image, les images en question. Les deux relectures sont separees parce
    qu'elles ne coutent pas la meme chose et ne disent pas la meme chose : l'une
    compare des caracteres, l'autre repasse par une reconnaissance optique.
    """
    interdites = {c.strip() for c in chaines_interdites if c and c.strip()}
    medias: dict[str, bytes] = {}
    parties: dict[str, bytes] = {}

    with zipfile.ZipFile(io.BytesIO(octets)) as z:
        infos = z.infolist()
        lus = _lire_borne(z, infos)
        for nom, octets in lus.items():
            if nom.startswith(PREFIXE_MEDIA):
                medias[nom] = octets
            elif nom.endswith(".xml") or nom.endswith(".rels"):
                parties[nom] = octets

    return (_fuites_xml(parties, interdites),
            fuites_dans_images(medias, interdites, langue))


def _reporter_fuites(par_image: dict[str, list[str]], connues: dict[str, str],
                     rapport: RapportDocx) -> None:
    """Range chaque fuite d'image dans le compte rendu de cette image."""
    if not par_image:
        return
    par_nom: dict[str, list[str]] = {}
    for chaine, images in par_image.items():
        for nom in images:
            par_nom.setdefault(nom, []).append(chaine)
    for compte_rendu in rapport.images_lues:
        chaines = par_nom.get(compte_rendu.nom)
        if chaines:
            compte_rendu.fuites = depuis_chaines(chaines, connues)


def verifier_octets(octets: bytes, chaines_interdites,
                    langue: str = LANGUE) -> list[str]:
    """Cherche les chaines interdites dans toute l'archive produite.

    La recherche porte sur trois representations : le texte logique de chaque
    partie, qui seul revele un nom reparti sur plusieurs noeuds ; le XML brut,
    ou subsistent les attributs, les codes de champ et les cibles de liens ; et
    les images, qu'il faut relire optiquement. Aucune des trois ne suffit
    seule — et omettre la troisieme rendait « succes » sur un document ou tout
    restait lisible dans une capture d'ecran.
    """
    xml, par_image = _relire_archive(octets, chaines_interdites, langue)
    return sorted(xml | set(par_image))


def verifier_sortie(chemin: Path | str, chaines_interdites,
                    langue: str = LANGUE) -> list[str]:
    """Meme verification, sur un fichier deja ecrit."""
    return verifier_octets(Path(chemin).read_bytes(), chaines_interdites,
                           langue)
