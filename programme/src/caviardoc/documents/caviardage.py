"""Chaine image : rendu, OCR, detection, caviardage des pixels, verification.

Le principe est brutal et c'est sa vertu : la page est rendue en image, les
zones sensibles sont noircies dans les pixels, et le PDF est reconstruit a
partir de ces images. **Le document produit n'a plus de couche texte du tout.**
Il n'y a donc rien a extraire, rien a copier-coller, rien sous les rectangles.

C'est la reponse a l'echec de caviardage le plus repandu, observe dans le
corpus de reference : un rectangle noir dessine par-dessus un texte qui reste
integralement extractible. Ici la question ne se pose pas, puisque le texte
n'existe plus.

Le prix a payer est reel : le texte n'est plus selectionnable et le fichier
grossit. Pour les documents que cette chaine traite — scannes ou porteurs d'une
couche texte corrompue — il n'y avait de toute facon pas de texte exploitable a
preserver.
"""

from __future__ import annotations

import io
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..concurrence import hors_pdf, journaliser_messages_mupdf, sous_pdf
from ..engine import Span, est_identifiant_direct
from ..dates_relatives import ETIQUETTE as DATE_RELATIVE
from ..dates_relatives import TableDatesRelatives, echelle
from .. import tesseract
from . import codes_barres
from ..pipeline import (PolitiqueDates, _merge, analyse, analyse_detaillee,
                        propageable_sans_doute, quasi_identifiants, replier)
from ..pseudonymes import TablePseudonymes
from ..revue import CLE_ZONES
from ..roles import (Niveau, filtrer_par_role, noms_du_patient,
                     qualifier_personnes)
from .commun import (assembler_texte as _assembler_texte, chaines_lisibles,
                     occurrences_relues, propageable, propager_chaines)
from .fuites import Fuite, depuis_chaines
from .ocr import (BLOC_COUCHE, PageOCR, completer, completer_par_la_page,
                  couche_invisible, mots_depuis_couche_texte, ocr_page)
from .qualite import Nature, diagnostiquer
from .structure_pdf import controler_structure, poser_mentions, sans_mentions
from .zones import (OptionsZones, boites_supplementaires, spans_des_zones,
                    zones_supplementaires)

#: 300 dpi : le meilleur compromis mesure entre qualite d'OCR et temps.
#: En dessous de 200, la reconnaissance se degrade nettement sur les petits
#: caracteres des en-tetes de laboratoire.
DPI = 300

#: Marge ajoutee autour de chaque boite, en pixels a la resolution de rendu.
MARGE = 2.0

#: Qualite JPEG des pages reconstruites.
QUALITE_JPEG = 85


@dataclass
class RapportPage:
    numero: int
    mots_reconnus: int = 0
    confiance: float = 0.0
    entites: list[Span] = field(default_factory=list)
    boites: int = 0
    duree: float = 0.0
    #: « couche texte », « couche texte + images » ou « OCR » : d'ou vient le
    #: texte analyse.
    source: str = ""
    #: Zones masquees en supplement des entites : logos, en-tetes, bandeau.
    zones: int = 0
    #: Detections avant fusion, necessaires au raisonnement par roles.
    bruts: list[Span] = field(default_factory=list)
    #: Le texte sur lequel la detection a travaille. Conserve pour distinguer
    #: une page blanche d'une page lisible restee muette.
    texte_lu: str = ""
    #: Pourquoi cette page merite un coup d'oeil, ou "" si elle n'en merite pas.
    #: Renseigne a l'echelle du document : c'est la comparaison avec les autres
    #: pages qui rend le silence d'une page suspect.
    reserve: str = ""
    #: Codes-barres et QR codes recouverts sur cette page. Une information, pas
    #: une reserve : le code est masque, il n'y a rien a aller verifier.
    codes_barres: int = 0


@dataclass
class RapportDocument:
    source: Path
    sortie: Path
    pages: list[RapportPage] = field(default_factory=list)
    duree: float = 0.0
    #: Chaines detectees qui reapparaissent apres caviardage. Doit rester vide.
    fuites: list[Fuite] = field(default_factory=list)
    #: Chaines que la verification relisait encore, et que la correction
    #: automatique a noircies la ou elle les relisait. Plus lisibles, donc
    #: plus des fuites ; mais elles disent qu'un passage avait echappe.
    corrigees: list[Fuite] = field(default_factory=list)
    verifie: bool = False
    #: Ce que le PDF produit contient qui n'a pas sa place dans un PDF fait
    #: d'images (script, lien, fichier joint, texte...). Liste vide : rien.
    #: ``None`` : le controle n'a pas pu avoir lieu, ou pas de PDF produit.
    structure_pdf: list[str] | None = None
    #: Noms sous lesquels le patient a ete reconnu, au niveau « patient ».
    noms_patient: set[str] = field(default_factory=set)
    #: Texte anonymise, si le format « texte » a ete demande.
    texte: str = ""
    #: Chaines sensibles qui subsistent dans le texte produit. Contrairement au
    #: PDF, la verification est ici exacte : on compare des chaines, sans
    #: repasser par une reconnaissance optique.
    fuites_texte: list[Fuite] = field(default_factory=list)
    #: Chaines restees en clair dans le texte produit, puis remplacees.
    corrigees_texte: list[Fuite] = field(default_factory=list)
    #: Vrai si le patient n'a pas pu etre identifie et que le niveau
    #: « complet » a ete applique par securite.
    repli_complet: bool = False
    #: Renseigne si la verification n'a pas pu s'executer. Un document non
    #: verifie n'est pas un document sain : il est simplement inconnu.
    erreur_verification: str = ""
    #: Dates rendues en J+n (`PolitiqueDates.RELATIVES`).
    dates_relatives: int = 0

    @property
    def entites_total(self) -> int:
        return sum(len(p.entites) for p in self.pages)

    @property
    def succes(self) -> bool:
        return self.verifie and not self.fuites


#: Pages d'un PDF au-dela desquelles on refuse : un dossier medical en compte
#: quelques dizaines, rarement plus de quelques centaines.
PAGES_MAX = 500

#: Pixels d'une page rendue. Une page A3 a 600 dpi en fait 70 millions ; la
#: geometrie vient du document, donc d'un tiers : elle est bornee avant que
#: MuPDF n'alloue.
PIXELS_MAX_PAGE = 100_000_000


class DocumentTropLong(ValueError):
    """Le document depasse `PAGES_MAX` ; le message est ecrit pour l'utilisateur."""


def _borner_pages(nombre: int) -> None:
    if nombre > PAGES_MAX:
        raise DocumentTropLong(
            f"Document trop long : {nombre} pages (maximum {PAGES_MAX}). "
            "Découpez-le en plusieurs fichiers.")


def _rendu(page, dpi: int):
    """Rend une page PDF en image PIL."""
    from PIL import Image
    rect = page.rect
    pixels = (rect.width * dpi / 72) * (rect.height * dpi / 72)
    if not pixels < PIXELS_MAX_PAGE:       # `not <` : refuse aussi NaN
        raise ValueError(f"page trop grande ({pixels / 1e6:.0f} millions de "
                         f"pixels a {dpi} dpi, maximum "
                         f"{PIXELS_MAX_PAGE // 1_000_000} millions de pixels)")
    pix = page.get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def _recouvre(span: Span, spans: list[Span]) -> bool:
    """Le span chevauche-t-il l'un de ``spans`` ?"""
    return any(sp.start < span.end and span.start < sp.end for sp in spans)


def _cle_de_zone(boite) -> tuple[int, ...]:
    """Identifie un rectangle d'une page a l'autre du calcul, au pixel pres."""
    return tuple(round(v) for v in boite)


def _texte_de_zone(lecture, boite, limite: int = 60) -> str:
    """Le texte lu sous un rectangle, abrege, pour le reconnaitre en relecture."""
    texte = " / ".join(sp.text.strip() for sp in spans_des_zones(lecture, [boite]))
    return texte if len(texte) <= limite else texte[:limite - 1] + "…"


def _noircir(image, boites) -> None:
    from PIL import ImageDraw
    d = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in boites:
        d.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))


def _premiere_ligne(boites):
    """Le rectangle qui couvre les mots de la premiere ligne d'un passage.

    « 2 avril 2024 » se lit en trois mots, donc trois rectangles : le libelle
    tient sur leur ensemble, pas dans celui du « 2 ».
    """
    x0, y0, x1, y1 = boites[0]
    ligne = [b for b in boites if b[1] < y1 and y0 < b[3]]
    return (min(b[0] for b in ligne), min(b[1] for b in ligne),
            max(b[2] for b in ligne), max(b[3] for b in ligne))


def _inscrire(image, libelles: list[tuple[tuple, str]]) -> None:
    """Ecrit en blanc, dans le rectangle noir d'une date, son « J+4 ».

    Le PDF produit n'est fait que d'images : le libelle y est dessine, pas
    pose en texte. Il ne dit rien que le texte exporte ne dise aussi.
    """
    from PIL import ImageDraw, ImageFont
    d = ImageDraw.Draw(image)
    for (x0, y0, x1, y1), texte in libelles:
        # Les blancs entre les mots d'une date ecrite en lettres font partie
        # du passage : sans eux, le libelle s'ecrirait en blanc sur blanc.
        d.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))
        taille = max(8, int((y1 - y0) * 0.9))
        while True:
            try:
                police = ImageFont.load_default(size=taille)
            except TypeError:                      # Pillow < 10.1
                police = ImageFont.load_default()
                break
            if d.textlength(texte, font=police) <= (x1 - x0) or taille <= 8:
                break
            taille -= 2
        d.text((x0 + 2, (y0 + y1) / 2), texte, fill=(255, 255, 255),
               font=police, anchor="lm")


#: Part de caracteres de remplacement ou de la zone privee, a partir de
#: laquelle la couche texte est tenue pour illisible. Une police sans table
#: Unicode donne des jetons de longueur normale, que les statistiques de
#: `diagnostiquer` jugent saines : la detection ne verrait alors rien, et rien
#: ne serait masque.
PART_COUCHE_ILLISIBLE = 0.1

#: Part de pixels sombres (rendu a 72 dpi) au-dela de laquelle une page dont
#: l'OCR n'a presque rien lu n'est plus tenue pour blanche : un manuscrit, une
#: page tournee ou un scan pale portent de l'encre que personne n'a lue.
ENCRE_SANS_TEXTE = 0.01


def _couche_illisible(texte: str) -> bool:
    """Le texte de la couche est-il fait de caracteres sans valeur ?"""
    signes = [c for c in texte if not c.isspace()]
    if not signes:
        return False
    vides = sum(1 for c in signes
                if c == "\ufffd" or "\ue000" <= c <= "\uf8ff")
    return vides / len(signes) >= PART_COUCHE_ILLISIBLE


#: Nombre minimal de mots comparables (couche et OCR au meme endroit) pour
#: juger la couche sur son accord avec l'OCR, et part d'accord en deca de
#: laquelle elle est tenue pour brouillee. Une police a encodage decale donne
#: « UüFRPPIQZIWL » pour « Posologie » : la couche existe, elle ne dit rien.
COUCHE_MOTS_COMPARABLES_MIN = 20
COUCHE_ACCORD_OCR_MIN = 0.3

#: Repli sans OCR : sur au moins ce nombre de jetons de 4 lettres ou plus, la
#: couche est brouillee si la part de jetons invraisemblables atteint le
#: premier seuil ET si moins de la seconde part des jetons sont des mots
#: courants du francais (un texte brouille n'en porte presque aucun).
COUCHE_JETONS_MIN = 20
COUCHE_PART_INVRAISEMBLABLE = 0.5
COUCHE_PART_MOTS_COURANTS_MIN = 0.1

_MOTS_COURANTS = frozenset("""
le la les de des du un une et en au aux que qui pour par avec sans dans sur
vous nous votre vos elle ils est sont pas plus mais ou donc
patient patiente date page jour mois
""".split())
_VOYELLES = set("aeiouyàâäéèêëîïôöùûüœ")


def _sans_accent(texte: str) -> str:
    base = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in base if c.isalnum() and not unicodedata.combining(c))


def _jeton_invraisemblable(jeton: str) -> bool:
    """Un jeton qui ne peut pas etre du francais (ni un nom)."""
    if any(not (c.isalpha() or c in "'’-.") for c in jeton):
        return True                      # signe etranger au milieu d'un mot
    bas = "".join(c for c in jeton.lower() if c.isalpha())
    if len(bas) < 3:
        return False
    voyelles = sum(1 for c in bas if c in _VOYELLES) / len(bas)
    if not 0.2 <= voyelles <= 0.8:
        return True
    if re.search(r"[^\W\d_aeiouyàâäéèêëîïôöùûüœ]{6,}", bas):
        return True
    # Casse qui alterne a l'interieur du mot, hors mot tout en capitales.
    interieur = jeton[1:]
    return (any(c.isupper() for c in interieur)
            and any(c.islower() for c in interieur)
            and sum(1 for a, b in zip(jeton, jeton[1:])
                    if a.islower() and b.isupper()) >= 1)


def _couche_brouillee_par_les_mots(texte: str) -> bool:
    """Heuristique linguistique, sans OCR : le texte est-il du charabia ?"""
    jetons = [t for t in (m.strip(".,;:!?()[]{}«»\"'-/") for m in texte.split())
              if len(t) >= 4 and not any(c.isdigit() for c in t)
              and "@" not in t]
    if len(jetons) < COUCHE_JETONS_MIN:
        return False
    invraisemblables = sum(1 for t in jetons if _jeton_invraisemblable(t))
    courants = sum(1 for t in jetons if _sans_accent(t) in _MOTS_COURANTS)
    return (invraisemblables / len(jetons) >= COUCHE_PART_INVRAISEMBLABLE
            and courants / len(jetons) < COUCHE_PART_MOTS_COURANTS_MIN)


def _couche_en_desaccord_avec_l_ocr(couche: PageOCR, ocr: PageOCR) -> bool:
    """La couche dit-elle autre chose que l'OCR, aux memes endroits ?

    Mots compares apres repli de la casse et des accents. En deca de
    `COUCHE_MOTS_COMPARABLES_MIN` mots superposes, on ne conclut pas.
    """
    from .ocr import _semblables

    lus = [(m.x0, m.y0, m.x1, m.y1, _sans_accent(m.texte)) for m in ocr.mots]
    lus = [m for m in lus if m[4]]
    comparables = accords = 0
    for mot in couche.mots:
        cle = _sans_accent(mot.texte)
        if len(cle) < 3:
            continue
        x, y = (mot.x0 + mot.x1) / 2, (mot.y0 + mot.y1) / 2
        voisins = [m[4] for m in lus if m[0] <= x <= m[2] and m[1] <= y <= m[3]]
        if not voisins:
            continue
        comparables += 1
        if any(_semblables(cle, v) for v in voisins):
            accords += 1
    if comparables < COUCHE_MOTS_COMPARABLES_MIN:
        return False
    return accords / comparables < COUCHE_ACCORD_OCR_MIN


def _encre_non_lue(page, lecture: PageOCR, source: str) -> str:
    """Pourquoi la page a l'air lue alors qu'elle ne l'est pas, sinon « ».

    Une page presque sans mot reconnu passait pour blanche : pas de detection,
    donc rien a verifier, donc « verifie ». On regarde les pixels : si la page
    porte de l'encre, le silence n'est pas celui d'une page blanche.
    """
    if not source.startswith("OCR"):
        return ""
    if diagnostiquer(lecture.texte).nature is Nature.TEXTE:
        return ""
    try:
        gris = _rendu(page, 72).convert("L")
    except Exception:
        return ""
    histogramme = gris.histogram()
    sombres = sum(histogramme[:128]) / max(1, gris.width * gris.height)
    if sombres < ENCRE_SANS_TEXTE:
        return ""
    return (f"page peu ou pas lue ({len(lecture.mots)} mots reconnus) alors "
            f"qu'elle porte de l'encre ({sombres:.0%} des pixels) : manuscrit, "
            "page tournee ou scan pale — a verifier a l'oeil")


def _lire_page(page, dpi: int, langue: str, rendu=None) -> tuple[PageOCR, str]:
    """Le texte de la page, et d'ou il vient.

    La detection se fait sur la meilleure source disponible : la couche du PDF
    si elle est saine, l'OCR sinon — et, sur une page a couche texte, l'OCR des
    images qui y sont posees, que la couche ne decrit pas. Le caviardage, lui,
    porte toujours sur les pixels : c'est ce qui garantit qu'il ne reste rien.

    Une couche invisible est l'OCR d'un scan, fait par un autre : la page est
    relue, et cette couche ne sert plus qu'a completer la lecture.
    """
    couche = mots_depuis_couche_texte(page, dpi=dpi)
    if (diagnostiquer(couche.texte).nature is not Nature.TEXTE
            or _couche_illisible(couche.texte)
            or _couche_brouillee_par_les_mots(couche.texte)):
        return ocr_page(rendu or _rendu(page, dpi), langue=langue, dpi=dpi), "OCR"
    if couche_invisible(page):
        lecture = ocr_page(rendu or _rendu(page, dpi), langue=langue, dpi=dpi)
        completer(lecture, couche, BLOC_COUCHE)
        return lecture, "OCR + couche invisible"
    # Toute la page est relue, pas seulement les images assez grandes : la
    # passe par images devient redondante.
    try:
        image = rendu or _rendu(page, dpi)
        lue = ocr_page(image, langue=langue, dpi=dpi)
        if _couche_en_desaccord_avec_l_ocr(couche, lue):
            # Encodage decale : la couche et la page ne disent pas la meme
            # chose. On ne garde que l'OCR, qui lit ce que le lecteur voit.
            return lue, "OCR"
        ajoutes = completer_par_la_page(couche, image, langue=langue, dpi=dpi,
                                        lue=lue)
    except Exception as exc:
        # Langue manquante, Tesseract qui plante : la page n'est pas relue,
        # donc un texte present seulement dans les pixels echapperait. Le
        # document est refuse, rien n'est ecrit.
        raise tesseract.TesseractIndisponible(
            "La lecture par OCR a échoué sur une page "
            f"({type(exc).__name__}) : Caviardoc refuse de produire un PDF "
            "dont toutes les pages n'ont pas été relues. Aucun fichier n'a "
            "été écrit. " + tesseract.REMEDE) from exc
    if ajoutes:
        return couche, "couche texte + images"
    return couche, "couche texte"


def caviarder_page(model, page, dpi: int = DPI, langue: str = "fra",
                   numero: int = 1, **kw_analyse):
    """Traite une page : rendu, OCR, detection, noircissement.

    Rend l'image caviardee, le rapport, et le resultat d'OCR (utile pour la
    verification et pour l'audit).
    """
    t0 = time.time()
    image = _rendu(page, dpi)
    ocr, source = _lire_page(page, dpi, langue, rendu=image)

    spans: list[Span] = []
    if ocr.texte.strip():
        spans = analyse(model, ocr.texte, **kw_analyse)

    boites = ocr.boites_pour(spans)
    if boites:
        _noircir(image, boites)

    rapport = RapportPage(numero=numero, mots_reconnus=len(ocr.mots),
                          confiance=ocr.confiance_moyenne, entites=spans,
                          boites=len(boites), duree=time.time() - t0,
                          source=source,
                          reserve=_encre_non_lue(page, ocr, source))
    return image, rapport, ocr


def signaler_pages_muettes(pages) -> None:
    """Marque les pages lisibles ou rien n'a ete detecte.

    Une page peut legitimement ne rien contenir de personnel — un tableau de
    valeurs, une page de references. Mais quand le reste du document en est
    plein, ce silence merite un coup d'oeil : une mise en page inhabituelle,
    une lecture qui s'est mal passee, une detection qui a manque.

    Trois conditions, pour ne pas crier au loup :

    - la page a du texte exploitable. Une page blanche — un verso de scan, un
      separateur — n'a rien a signaler, et `diagnostiquer` la reconnait ;
    - elle ne porte aucune entite, apres propagation. Une page couverte par la
      propagation depuis une autre page est deja traitee ;
    - d'autres pages, elles, en portent. Sur un document sans aucune donnee
      personnelle, toutes les pages seraient muettes et l'alerte serait vide de
      sens.

    On signale, on ne bloque pas : c'est un doute a lever par un humain, pas un
    verdict.
    """
    ailleurs = sum(len(p.entites) for p in pages)
    if not ailleurs:
        return
    for page in pages:
        if page.entites:
            continue
        if diagnostiquer(page.texte_lu).nature is Nature.ABSENTE:
            continue          # page blanche : rien a dire
        page.reserve = ("aucune donnee detectee sur cette page, alors que le "
                        "document en contient ailleurs — a verifier a l'oeil")


def metadonnees_pdf() -> dict[str, str]:
    """Les seules metadonnees d'un PDF produit.

    La date est celle du traitement, au jour pres : elle ne dit rien du
    document d'origine. Un PDF sans date ressemble a un fichier fabrique de
    toutes pieces, ce que les antivirus a base d'apprentissage relevent.
    """
    jour = time.strftime("D:%Y%m%d000000Z", time.gmtime())
    return {"title": "Document anonymisé", "producer": "Caviardoc",
            "creator": "Caviardoc", "creationDate": jour, "modDate": jour}


@sous_pdf
def serialiser_pdf(doc) -> bytes:
    """Le PDF en octets, avec ses metadonnees neutres et une structure propre.

    MuPDF range, dans un document neuf, un ``/Info`` dans le catalogue — place
    ou la norme n'en veut pas, et ou ses seules metadonnees sont deja dans le
    trailer. On l'annule : une valeur nulle vaut une cle absente.

    Chaque page recoit enfin une mention invisible et sans donnee, sans
    laquelle certains antivirus le prennent pour un PDF d'hameconnage : voir
    `structure_pdf.poser_mentions`.
    """
    poser_mentions(doc)
    doc.set_metadata(metadonnees_pdf())
    doc.xref_set_key(doc.pdf_catalog(), "Info", "null")
    return doc.tobytes(garbage=4, deflate=True)


@sous_pdf
def lire_pdf(source, dpi: int = DPI,
             langue: str = "fra") -> list[tuple[PageOCR, str]]:
    """Lit chaque page d'un PDF — couche texte ou OCR —, sans rien detecter.

    C'est la moitie la plus longue de la premiere passe, et la seule qui ne
    depende de rien d'autre que du fichier. L'interface s'en sert pour lire le
    document suivant pendant que l'utilisateur relit le precedent : la
    detection, elle, attend la relecture, dont les corrections valent pour
    les documents suivants.
    """
    import pymupdf

    tesseract.exiger(langue)
    codes_barres.exiger()
    with pymupdf.open(Path(source)) as entree:
        _borner_pages(len(entree))
        return [_lire_page(page, dpi, langue) for page in entree]


def analyser_page(model, page, dpi: int = DPI, langue: str = "fra",
                  numero: int = 1, lu: tuple[PageOCR, str] | None = None,
                  **kw_analyse):
    """Premiere passe : lit la page et detecte, sans encore la caviarder.

    ``lu`` est la lecture de la page deja faite par `lire_pdf`, avec les
    memes ``dpi`` et ``langue`` ; absente, la page est lue ici.
    """
    t0 = time.time()
    if lu is None:
        tesseract.exiger(langue)
    lecture, source = lu if lu is not None else _lire_page(page, dpi, langue)

    spans: list[Span] = []
    bruts: list[Span] = []
    if lecture.texte.strip():
        spans, bruts = analyse_detaillee(model, lecture.texte, **kw_analyse)

    rapport = RapportPage(numero=numero, mots_reconnus=len(lecture.mots),
                          confiance=lecture.confiance_moyenne, entites=spans,
                          duree=time.time() - t0, source=source,
                          bruts=bruts, texte_lu=lecture.texte,
                          reserve=_encre_non_lue(page, lecture, source))
    return lecture, rapport


@sous_pdf
def caviarder_pdf(model, source, sortie, dpi: int = DPI, langue: str = "fra",
                  verifier: bool = True, zones: OptionsZones | None = None,
                  niveau: Niveau = Niveau.COMPLET,
                  formats: frozenset[str] = frozenset({"pdf"}),
                  table: TablePseudonymes | None = None,
                  revue=None, apercu=None, arret=None,
                  lu: list[tuple[PageOCR, str]] | None = None,
                  **kw_analyse) -> RapportDocument:
    """Caviarde un PDF entier par la chaine image, en deux passes.

    ``arret()``, s'il est donne, est appele entre deux pages et leve
    `interruption.Interrompu` pour abandonner le document : rien n'est alors ecrit.

    ``lu``, s'il est donne, est la lecture du document par `lire_pdf`, faite
    a l'avance avec les memes ``dpi`` et ``langue``.

    ``apercu(intitule, image, lecture)``, s'il est donne, est appele pour chaque
    page juste avant la relecture : de quoi montrer la page telle qu'elle est,
    avec les boites que chaque passage noircirait.

    La premiere passe lit et detecte page par page. La seconde propage
    l'ensemble des entites trouvees a **toutes** les pages avant de noircir.

    Cette propagation a l'echelle du document n'est pas un raffinement : un nom
    reconnu en en-tete de la page 1 et manque page 12 laisse le document
    partiellement identifiable, et la comparaison entre les deux occurrences
    trahit precisement ce qu'on voulait cacher. Le modele note chaque page
    independamment ; c'est a l'orchestration de retablir la coherence.

    Le fichier produit ne contient que des images, et pour seul texte une
    mention invisible et fixe par page (`structure_pdf.poser_mentions`) :
    aucune donnee residuelle extractible.
    """
    import pymupdf

    tesseract.exiger(langue)
    codes_barres.exiger()
    source, sortie = Path(source), Path(sortie)
    rapport = RapportDocument(source=source, sortie=sortie)
    t0 = time.time()

    entree = pymupdf.open(source)
    try:
        _borner_pages(len(entree))
    except ValueError:
        entree.close()
        raise
    resultat = pymupdf.open()
    #: Ce que l'utilisateur a choisi de laisser lisible a la relecture. La
    #: correction automatique n'y touche pas : elle repare un oubli de la
    #: machine, elle ne revient pas sur une decision humaine.
    laisses_visibles: set[str] = set()

    try:
        # --- passe 1 : lecture et detection ---------------------------------
        lectures: list[PageOCR] = []
        pages_texte: list[tuple[str, list[Span]]] = []
        for i, page in enumerate(entree):
            if arret is not None:
                arret()
            lecture, rp = analyser_page(model, page, dpi=dpi, langue=langue,
                                        numero=i + 1,
                                        lu=lu[i] if lu else None,
                                        **kw_analyse)
            lectures.append(lecture)
            rapport.pages.append(rp)

        # Le patient s'identifie a l'echelle du document, pas de la page : son
        # nom peut n'etre accompagne de sa date de naissance qu'en premiere
        # page, et apparaitre seul ensuite.
        if niveau is not Niveau.COMPLET:
            rapport.noms_patient = set()
            for lecture, rp in zip(lectures, rapport.pages):
                rapport.noms_patient |= noms_du_patient(lecture.texte,
                                                        rp.bruts or rp.entites)

            if rapport.noms_patient:
                for lecture, rp in zip(lectures, rapport.pages):
                    rp.entites = filtrer_par_role(lecture.texte, rp.entites,
                                                  niveau, rapport.noms_patient)
            else:
                # Repli en securite. Sans patient identifie, le filtre par role
                # ne conserverait rien : le document sortirait intact alors que
                # l'utilisateur a demande une anonymisation. Un outil de
                # confidentialite doit echouer du cote sur, jamais du cote
                # permissif. On masque donc tout, et on le dit.
                rapport.repli_complet = True

        # Chaine -> etiquette d'origine. Conserver l'etiquette importe : elle
        # nourrit le journal d'audit et la table de pseudonymes, qui sert a son
        # tour a anonymiser le nom du fichier. Un doute du modele reste ou il
        # a ete lu, comme dans `pipeline.propager` : propage d'ici, il
        # revenait ailleurs a confiance 1,0, sans plus rien de douteux.
        connues: dict[str, str] = {}
        for rp in rapport.pages:
            for sp in rp.entites:
                cle = sp.text.strip()
                if propageable(cle, sp.label) and propageable_sans_doute(sp):
                    connues.setdefault(cle, sp.label)

        # --- propagation ----------------------------------------------------
        for lecture, rp in zip(lectures, rapport.pages):
            rp.entites = _merge(
                propager_chaines(lecture.texte, list(rp.entites), connues),
                lecture.texte)

        # --- relecture humaine ----------------------------------------------
        # Le crochet s'insere ici, et nulle part ailleurs : apres que tout a
        # ete detecte et propage, avant qu'un seul pixel ait ete noirci. Relire
        # plus tot montrerait un etat incomplet ; relire plus tard n'aurait
        # plus rien a corriger.
        zones_refusees: dict[str, set] = {}
        if revue is not None:
            if apercu is not None:
                for page, lecture, rp in zip(entree, lectures, rapport.pages):
                    apercu(f"page {rp.numero}", _rendu(page, dpi), lecture)
            # Les quasi-identifiants ne se cherchent que pour etre montres :
            # sans relecture, personne ne serait la pour les juger. La
            # relecture les recoit avec le reste, et ne les masque pas d'office.
            signales = [quasi_identifiants(model, lecture.texte, rp.entites)
                        for lecture, rp in zip(lectures, rapport.pages)]
            # L'attente de la relecture peut durer des minutes : les autres
            # documents ne doivent pas attendre PyMuPDF pendant ce temps.
            # Les zones (blocs d'en-tete, logos) se montrent aussi : elles
            # noircissent d'un bloc, et l'utilisateur doit pouvoir les refuser.
            # Calculees ici sur les spans d'avant relecture, elles ne servent
            # qu'a etre proposees ; ce qui sera peint se recalcule apres.
            proposees: dict[str, list] = {}
            if zones is not None and zones.actif:
                for page, lecture, rp in zip(entree, lectures, rapport.pages):
                    candidates = zones_supplementaires(
                        page, lecture, rp.entites, zones, dpi,
                        premiere=(rp.numero == 1),
                        image=_rendu(page, dpi))
                    if candidates:
                        proposees[f"page {rp.numero}"] = [
                            (genre, boite, _texte_de_zone(lecture, boite))
                            for genre, boite in candidates]
            parties_revue = [(f"page {rp.numero}", lecture.texte,
                              rp.entites + quasi)
                             for lecture, rp, quasi in zip(
                                 lectures, rapport.pages, signales)]
            with hors_pdf():
                approuves = (revue(parties_revue, proposees) if proposees
                             else revue(parties_revue))
            # Zones proposees puis refusees, par intitule. Une relecture qui
            # ne rend pas de zones (cle absente) n'en refuse aucune.
            for intitule, lot in proposees.items():
                retenues = approuves.get(CLE_ZONES + intitule)
                if retenues is not None:
                    gardees = {_cle_de_zone(b) for b in retenues}
                    zones_refusees[intitule] = {
                        _cle_de_zone(b) for _, b, _ in lot} - gardees
            for rp in rapport.pages:
                avant = rp.entites
                rp.entites = approuves.get(f"page {rp.numero}", rp.entites)
                gardes = {(sp.start, sp.end) for sp in rp.entites}
                laisses_visibles |= {sp.text.strip() for sp in avant
                                     if (sp.start, sp.end) not in gardes}

        # Apres la relecture, donc sur l'etat reellement caviarde : une page
        # que l'utilisateur a videe de ses masques est aussi une page a
        # regarder.
        signaler_pages_muettes(rapport.pages)

        # Apres la relecture, sur tout le document : J0 est la plus ancienne
        # des dates reellement masquees, la meme a chaque page.
        libelles: dict[str, str] = {}
        if kw_analyse.get("dates") is PolitiqueDates.RELATIVES:
            libelles = echelle(sp for rp in rapport.pages for sp in rp.entites)
            rapport.dates_relatives = len(libelles)

        # --- passe 2 : caviardage et assemblage -----------------------------
        for page, lecture, rp in zip(entree, lectures, rapport.pages):
            if arret is not None:
                arret()
            spans = rp.entites

            # Les zones se calculent quel que soit le format : le texte doit
            # perdre ce que le PDF noircit, y compris quand on n'exporte que
            # le texte.
            # Rendue avant tout noircissement : les zones cherchent les logos
            # dans les pixels d'origine.
            image = None
            if "pdf" in formats or (zones is not None and zones.actif):
                image = _rendu(page, dpi)
            supplement = []
            if zones is not None:
                # Calcule sur les spans d'apres relecture : un bloc retenu
                # parce qu'un span le touchait n'est plus retenu si
                # l'utilisateur a retire tous les spans qui s'y trouvaient.
                supplement = boites_supplementaires(
                    page, lecture, spans, zones, dpi, premiere=(rp.numero == 1),
                    image=image)
                refusees = zones_refusees.get(f"page {rp.numero}", set())
                supplement = [b for b in supplement
                              if _cle_de_zone(b) not in refusees]
                rp.zones = len(supplement)
            spans_texte = spans
            if supplement:
                spans_texte = _merge(
                    list(spans) + spans_des_zones(lecture, supplement),
                    lecture.texte)
            pages_texte.append((lecture.texte, spans_texte))

            if "pdf" not in formats:
                continue

            # Les codes se cherchent dans la page rendue pour la sortie, la
            # meme que celle qu'on noircit : un code dessine en vectoriel y est
            # aussi des pixels. Avant les aplats de texte, sur les pixels
            # d'origine.
            rp.codes_barres = len(codes_barres.masquer(image))

            boites = lecture.boites_pour(spans) + supplement
            if boites:
                _noircir(image, boites)
            if libelles:
                _inscrire(image, [
                    (_premiere_ligne(cadres), libelles[sp.text.strip()])
                    for sp in spans
                    if sp.label == DATE_RELATIVE
                    and sp.text.strip() in libelles
                    and (cadres := lecture.boites_pour([sp]))])
            rp.boites = len(boites)

            tampon = io.BytesIO()
            image.save(tampon, format="JPEG", quality=QUALITE_JPEG)
            nouvelle = resultat.new_page(width=page.rect.width,
                                         height=page.rect.height)
            nouvelle.insert_image(nouvelle.rect, stream=tampon.getvalue())

        # Serialiser en memoire avant d'ecrire : la verification porte alors
        # sur exactement ce qui sera enregistre, sans relire le disque. C'est
        # aussi ce qui rend l'operation insensible aux antivirus, qui mettent
        # volontiers en quarantaine un PDF fraichement ecrit et le rendent
        # illisible pendant quelques secondes — la relecture echouait alors,
        # alors meme que le caviardage etait correct.
        # Titre et producteur neutres, jamais ceux du document d'origine : ils
        # peuvent porter un nom. Un PDF fait d'images sans aucune metadonnee
        # ressemble aux PDF d'hameconnage, et certains antivirus le signalent
        # a tort.
        octets = serialiser_pdf(resultat) if "pdf" in formats else b""
    finally:
        resultat.close()
        entree.close()

    if octets:
        try:
            rapport.structure_pdf = controler_structure(octets)
        except Exception:
            rapport.structure_pdf = None

    if "texte" in formats:
        table = table if table is not None else TablePseudonymes()
        if libelles:
            table = TableDatesRelatives(table, libelles)
        # Les personnes se lisent sur les detections brutes, avant toute
        # fusion. Un bloc d'en-tete masque d'un seul tenant, ou un nom colle a
        # sa date de naissance, forme un span unique etiquete « numero AVS »
        # ou « date », qui absorbe le nom du patient : « TARTEMPUENZ Quidam »
        # n'etait alors jamais vu entier, et « Machin » seul recevait un
        # autre pseudonyme que le nom complet de la page suivante.
        brutes = [(lecture.texte, rp.bruts or rp.entites)
                  for lecture, rp in zip(lectures, rapport.pages)]
        table.qualifier({**qualifier_personnes(pages_texte),
                         **qualifier_personnes(brutes)})
        # Toutes les pages d'un coup, les plus longs d'abord : le nom entier
        # lu page 2 doit preceder le prenom seul lu page 1. Seules comptent
        # les personnes effectivement masquees ; une personne laissee
        # lisible a la relecture ne doit pas disparaitre du nom du fichier.
        table.enregistrer(
            [sp for (_, bruts), rp in zip(brutes, rapport.pages)
             for sp in bruts
             if sp.est_personne() and _recouvre(sp, rp.entites)]
            + [sp for rp in rapport.pages for sp in rp.entites
               if sp.est_personne()])
        for _, spans in pages_texte:
            table.enregistrer(spans)
        etiquettes_texte = {sp.text.strip(): sp.label
                            for _, spans in pages_texte for sp in spans}

        def en_clair() -> set[str]:
            rapport.texte = _assembler_texte(pages_texte, table)
            # Meme critere que la verification du PDF : sans blancs, sans casse,
            # sans traits d'union. Un controle plus strict laisserait passer en
            # texte seul une occurrence que la correction sait pourtant noircir.
            return chaines_lisibles(
                rapport.texte,
                {sp.text.strip() for _, spans in pages_texte for sp in spans
                 if propageable(sp.text.strip(), sp.label)})

        restantes = en_clair()
        a_corriger = {c: etiquettes_texte[c] for c in restantes
                      if c not in laisses_visibles}
        if a_corriger:
            pages_texte = [(texte, _merge(spans + occurrences_relues(
                                texte, a_corriger), texte))
                           for texte, spans in pages_texte]
            restantes = en_clair()
            rapport.corrigees_texte = depuis_chaines(
                set(a_corriger) - restantes, etiquettes_texte)
        rapport.fuites_texte = depuis_chaines(
            restantes, etiquettes_texte,
            raisons={c: RAISON_LAISSEE if c in laisses_visibles
                     else RAISON_TEXTE_EXPORTE for c in restantes})
        # En texte seul, il n'y a pas de PDF a relire : le controle du texte
        # exporte — exact, puisqu'il compare des chaines — est la
        # verification. Sans cela, tout resultat en texte seul s'affichait
        # « NON VERIFIE », et l'alerte ne distinguait plus rien.
        if verifier and "pdf" not in formats:
            rapport.verifie = True

    if verifier and "pdf" in formats:
        # Meme filtre que pour la propagation : ne demander « cette donnee
        # a-t-elle survecu ? » que de ce qui ressemble a une donnee. Verifier
        # tous les mots detectes noie les vraies alertes sous les faux positifs
        # du detecteur — « patient », « soins » ou « medecin » figurent partout
        # dans un dossier medical et seraient signales a chaque page.
        a_masquer = etiquettes_a_masquer(rapport.pages)
        try:
            pages = pages_des_fuites(octets, a_masquer, dpi=dpi, langue=langue)
            raisons: dict[str, str] = {}
            octets, pages, corrigees = corriger_fuites(
                octets, pages, dpi=dpi, langue=langue,
                epargnees=laisses_visibles, raisons=raisons)
            rapport.fuites = depuis_chaines(pages, a_masquer, pages, raisons)
            rapport.corrigees = depuis_chaines(corrigees, a_masquer, corrigees)
            rapport.verifie = True
        except Exception as exc:
            rapport.erreur_verification = f"{type(exc).__name__}: {exc}"

    if "pdf" in formats:
        sortie.write_bytes(octets)
    if "texte" in formats:
        sortie.with_suffix(".txt").write_text(rapport.texte, encoding="utf-8")

    rapport.duree = time.time() - t0
    journaliser_messages_mupdf()

    return rapport


#: Nombre maximal de passes de correction. Une passe suffit presque toujours :
#: on noircit les mots ou l'OCR de verification relit la chaine, et c'est ce
#: meme OCR qui la cherchera ensuite. La seconde rattrape une chaine qu'un
#: premier noircissement aurait fait apparaitre autrement decoupee.
PASSES_CORRECTION = 2

#: Pourquoi une chaine reste lisible. Une phrase par cas, car c'est elle que
#: l'utilisateur lit a cote de la chaine.
RAISON_LAISSEE = "laissé visible par vous à la relecture"
RAISON_SANS_ZONE = ("la vérification la relit, mais aucune zone n'a pu être "
                    "retrouvée sur la page pour la noircir")
RAISON_RELUE = "noircie, mais la vérification la relit encore"
RAISON_TEXTE_EXPORTE = "encore présente dans le texte exporté après remplacement"
RAISON_COUCHE_TEXTE = "le PDF contient encore du texte extractible"
RAISON_CODE_BARRES = ("un lecteur de codes-barres décode encore un code sur la "
                      "page produite")


@sous_pdf
def corriger_fuites(octets: bytes, fuites: dict[str, list[int]],
                    dpi: int = DPI, langue: str = "fra",
                    epargnees=frozenset(), raisons: dict[str, str] | None = None
                    ) -> tuple[bytes, dict[str, list[int]], dict[str, list[int]]]:
    """Noircit ce que la verification relit encore, la ou elle le relit.

    La verification savait dire « Rue de la Sapiniere 45, page 1 » sans rien
    en faire : le document sortait avec une alerte, et c'etait a l'utilisateur
    de le reprendre. Or elle dispose de tout ce qu'il faut pour corriger — la
    chaine, la page, et un OCR de la page produite qui situe chaque mot. On
    noircit donc ces mots, sur le document produit lui-meme, puis on relit les
    pages touchees pour s'assurer que la chaine a bien disparu.

    ``epargnees`` sont les chaines que l'utilisateur a laissees visibles a la
    relecture : elles restent signalees, mais ne sont pas corrigees.

    Rend les octets du document, les fuites qui subsistent et les fuites
    corrigees, chacune avec ses pages. Si ``raisons`` est donne, il recoit pour
    chaque fuite qui subsiste la phrase qui dit pourquoi.
    """
    import pymupdf

    raisons = raisons if raisons is not None else {}
    restantes = {c: list(p) for c, p in fuites.items()}
    for chaine in restantes:
        if chaine in epargnees:
            raisons[chaine] = RAISON_LAISSEE
        elif chaine.startswith("couche texte residuelle"):
            raisons[chaine] = RAISON_COUCHE_TEXTE
        elif chaine.startswith(codes_barres.CODE_ENCORE_LISIBLE):
            raisons[chaine] = RAISON_CODE_BARRES
    corrigees: dict[str, list[int]] = {}
    for _ in range(PASSES_CORRECTION):
        par_page: dict[int, dict[str, str]] = {}
        for chaine, pages in restantes.items():
            if chaine in epargnees:
                continue
            for numero in pages:
                par_page.setdefault(numero, {})[chaine] = ""
        if not par_page:
            break

        d = pymupdf.open(stream=octets, filetype="pdf")
        try:
            images = {}
            for numero, chaines in par_page.items():
                image = _rendu(d[numero - 1], dpi)
                lecture = ocr_page(image, langue=langue, dpi=dpi)
                boites = lecture.boites_pour(occurrences_relues(lecture.texte,
                                                                chaines))
                if boites:
                    _noircir(image, boites)
                    images[numero] = image
            for numero, chaines in par_page.items():
                if numero not in images:
                    for chaine in chaines:
                        raisons[chaine] = RAISON_SANS_ZONE
            if not images:
                break
            octets = _remplacer_pages(d, images)
        finally:
            d.close()

        relues = _verifier(pymupdf.open(stream=octets, filetype="pdf"),
                           {c for n in images for c in par_page[n]},
                           dpi, langue, numeros=set(images))
        for chaine in list(restantes):
            touchees = set(restantes[chaine]) & set(images)
            encore = set(relues.get(chaine, ()))
            if touchees - encore:
                corrigees[chaine] = sorted(set(corrigees.get(chaine, ()))
                                           | (touchees - encore))
            if encore:
                raisons[chaine] = RAISON_RELUE
            reste = sorted((set(restantes[chaine]) - touchees) | encore)
            if reste or not restantes[chaine]:
                restantes[chaine] = reste
            else:
                del restantes[chaine]
    for chaine in list(raisons):
        if chaine not in restantes:
            del raisons[chaine]
    return octets, restantes, corrigees


def _remplacer_pages(d, images: dict) -> bytes:
    """Le document ``d``, ou les pages numerotees dans ``images`` sont refaites."""
    import pymupdf

    sortie = pymupdf.open()
    try:
        for i, page in enumerate(d):
            image = images.get(i + 1)
            if image is None:
                sortie.insert_pdf(d, from_page=i, to_page=i)
                continue
            tampon = io.BytesIO()
            image.save(tampon, format="JPEG", quality=QUALITE_JPEG)
            nouvelle = sortie.new_page(width=page.rect.width,
                                       height=page.rect.height)
            nouvelle.insert_image(nouvelle.rect, stream=tampon.getvalue())
        return serialiser_pdf(sortie)
    finally:
        sortie.close()


@sous_pdf
def pages_des_fuites(octets: bytes, chaines_interdites, dpi: int = DPI,
                     langue: str = "fra") -> dict[str, list[int]]:
    """Meme verification, en conservant les pages. Utilisee par le caviardage."""
    import pymupdf
    return _verifier(pymupdf.open(stream=octets, filetype="pdf"),
                     chaines_interdites, dpi, langue)


@sous_pdf
def verifier_octets(octets: bytes, chaines_interdites, dpi: int = DPI,
                    langue: str = "fra") -> list[str]:
    """Verifie un PDF deja serialise, sans passer par le disque.

    Rend les chaines seules : la forme historique, que gardent les outils de
    mesure et les tests.
    """
    return sorted(pages_des_fuites(octets, chaines_interdites, dpi, langue))


@sous_pdf
def verifier_sortie(chemin, chaines_interdites, dpi: int = DPI,
                    langue: str = "fra") -> list[str]:
    """Controle qu'aucune chaine sensible ne survit dans le fichier produit.

    Deux verifications, l'une bon marche et l'autre couteuse mais decisive :

    1. Le PDF ne doit contenir aucun texte extractible. C'est le controle qui
       aurait pris en defaut les caviardages du corpus de reference, ou un
       rectangle noir recouvrait un texte intact.
    2. Une nouvelle reconnaissance optique du document produit ne doit pas
       retrouver les chaines masquees. C'est ce qui prouve que les pixels ont
       bien ete noircis, et pas seulement recouverts.
    """
    import pymupdf
    return sorted(_verifier(pymupdf.open(chemin), chaines_interdites,
                            dpi, langue))


def etiquettes_a_masquer(pages) -> dict[str, str]:
    """Les chaines a verifier, avec l'etiquette qui dira la gravite d'une fuite.

    Deux filtres et un arbitrage :

    - meme filtre que pour la propagation, `propageable` : ne demander « cette
      donnee a-t-elle survecu ? » que de ce qui ressemble a une donnee.
      Verifier tous les mots detectes noierait les vraies alertes sous les faux
      positifs — « patient », « soins » ou « medecin » figurent partout dans un
      dossier medical et seraient signales a chaque page ;
    - un meme texte peut porter plusieurs etiquettes selon l'endroit ou il
      apparait. On garde la plus grave : c'est elle qui decide si la fuite se
      lit comme une re-identification ou comme un element de contexte, et l'on
      ne veut pas qu'une occurrence anodine efface une occurrence sensible.
    """
    retenues: dict[str, str] = {}
    for page in pages:
        for span in page.entites:
            texte = span.text.strip()
            if not propageable(texte, span.label):
                continue
            if texte not in retenues or est_identifiant_direct(span.label):
                retenues[texte] = span.label
    return retenues


def _verifier(d, chaines_interdites, dpi: int, langue: str,
              numeros=None) -> dict[str, list[int]]:
    """Rend, pour chaque chaine relue, les pages ou elle l'a ete.

    La page compte : « cette date subsiste » n'aide personne, « page 4 » se
    verifie en dix secondes. C'est la difference entre une alerte qu'on subit
    et une alerte qu'on instruit.
    """
    fuites: dict[str, list[int]] = {}
    try:
        # La mention invisible posee sur chaque page n'est pas un residu : elle
        # a ete ecrite ici, et ne porte aucune donnee.
        residuel = sans_mentions("\n".join(p.get_text() for p in d))
        if residuel.strip():
            fuites[f"couche texte residuelle : {len(residuel)} caracteres"] = []

        for numero, page in enumerate(d, 1):
            if numeros is not None and numero not in numeros:
                continue
            img = _rendu(page, dpi)
            # Un code encore decodable apres masquage est un echec de la
            # verification, au meme titre qu'un nom relu.
            if codes_barres.compter(img):
                fuites[f"{codes_barres.CODE_ENCORE_LISIBLE} "
                       f"(page {numero})"] = []
            relu = ocr_page(img, langue=langue, dpi=dpi).texte
            compact = replier(relu)
            for chaine in chaines_interdites:
                if (aiguille := replier(chaine)) and aiguille in compact:
                    fuites.setdefault(chaine, []).append(numero)
    finally:
        d.close()

    return fuites
