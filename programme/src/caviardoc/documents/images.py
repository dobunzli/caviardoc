"""Caviardage des images incorporees dans un document bureautique.

Un .docx **est** son texte, et c'est ce que la chaine bureautique reecrit. Mais
une archive peut aussi porter des pixels : un rapport colle en capture d'ecran,
la photo d'une lettre d'adressage, un fax numerise glisse dans le corps du
document. Ces pixels etaient hors de portee — comptes, signales, et laisses
lisibles. Le seul recours, `--logos`, retirait toutes les images sans
distinction : le rapport scanne comme le logo du cabinet et la courbe de
laboratoire.

Les pieces pour faire mieux existaient deja, dans la chaine image : lire une
image par OCR, detecter sur ce qu'elle rend, noircir les mots retenus. Ce
module les emprunte, et y ajoute ce que la chaine bureautique sait et que la
chaine image ignore — les chaines deja trouvees dans le texte du document. Un
modele lit une capture de quatre lignes bien moins bien qu'une page entiere ;
un nom reconnu dans le corps puis propage sur l'image ne coute rien et ne
depend pas de cette seconde lecture.

La verification est l'autre moitie du travail. Elle cherchait les chaines
interdites dans les parties XML de l'archive, ou un pixel ne dit rien : une
fuite dans une image y etait structurellement invisible, et le rapport
annoncait « succes » sur un document ou le nom de la patiente, sa date de
naissance et son numero AVS restaient parfaitement lisibles. `fuites_dans_images`
relit les images produites, et nomme celles ou une chaine subsiste.

Deux prudences guident le code. Une image ou rien n'a ete retenu n'est pas
touchee : ses octets d'origine sont recopies tels quels, donc un logo ou un
graphique ne subit aucune recompression. Et une image qu'on ne sait ni ouvrir
ni reecrire — un metafichier Windows, une animation — est signalee comme telle
plutot que traitee a moitie : c'est le cas ou l'ancien avertissement garde tout
son sens.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field

from ..engine import Span
from ..pipeline import _merge, analyse, replier
from ..roles import Niveau, filtrer_par_role
from . import codes_barres
from .caviardage import QUALITE_JPEG
from .commun import occurrences_relues, propager_chaines
from .fuites import Fuite
from .. import tesseract
from .ocr import LANGUE, ocr_image

#: Modes ou l'on peut noircir et reenregistrer sans surprise. Ailleurs — une
#: palette, du bitonal, du CMJN — on passe par RGB : le contenu de ces images
#: est du texte a masquer, pas un aplat dont la fidelite colorimetrique compte.
MODES_SURS = frozenset({"RGB", "RGBA", "L"})


@dataclass
class RapportImage:
    """Ce qu'on a lu et masque dans une image de l'archive."""

    nom: str
    mots_reconnus: int = 0
    confiance: float = 0.0
    entites: list[Span] = field(default_factory=list)
    boites: int = 0
    duree: float = 0.0
    #: Chaines interdites relues dans l'image produite. Doit rester vide.
    #: Renseigne par la verification, qui relit l'archive entiere.
    fuites: list[Fuite] = field(default_factory=list)
    #: Renseigne si l'image n'a pu etre ni lue ni reecrite. Son contenu reste
    #: alors ce qu'il etait : invisible a la chaine, donc a signaler.
    illisible: str = ""
    #: Codes-barres et QR codes recouverts dans cette image. Information, pas
    #: reserve : ils sont masques.
    codes_barres: int = 0

    @property
    def lue(self) -> bool:
        return not self.illisible


def caviarder_image(model, nom: str, octets: bytes,
                    connues: dict[str, str] | None = None,
                    langue: str = LANGUE,
                    niveau: Niveau = Niveau.COMPLET,
                    noms_patient=frozenset(),
                    **kw_analyse) -> tuple[bytes, RapportImage]:
    """Lit une image et masque ce qu'elle porte de personnel.

    Rend les octets a enregistrer — ceux d'origine si rien n'etait a masquer —
    et le compte rendu de l'operation. La verification n'a pas lieu ici : elle
    porte sur l'archive assemblee, et c'est `fuites_dans_images` qui la fait.

    ``connues`` porte les chaines deja reconnues dans le texte du document,
    avec leur etiquette : elles sont cherchees ici aussi, par le meme critere
    que partout ailleurs. ``noms_patient`` vient du document entier, seule
    echelle a laquelle le patient s'identifie ; sans lui, le filtrage par role
    ne saurait pas quoi conserver et l'on masque tout, comme ailleurs.
    """
    tesseract.exiger(langue)
    codes_barres.exiger()
    depart = time.monotonic()
    rapport = RapportImage(nom=nom)
    connues = connues or {}

    try:
        image = _ouvrir(octets)
        rvb = image.convert("RGB")
    except Exception as exc:
        rapport.illisible = _raison(exc)
        rapport.duree = time.monotonic() - depart
        return octets, rapport
    try:
        lecture = ocr_image(rvb, langue=langue)
    except Exception as exc:
        # Image decodee mais non lue : elle n'a pas ete cherchee, donc le
        # document est refuse plutot que livre avec une image non relue.
        raise tesseract.TesseractIndisponible(
            "La lecture par OCR a échoué sur une image du document "
            f"({type(exc).__name__}) : Caviardoc refuse de la livrer non "
            "relue. Aucun fichier n'a été écrit. " + tesseract.REMEDE) from exc

    rapport.mots_reconnus = len(lecture.mots)
    rapport.confiance = lecture.confiance_moyenne

    spans: list[Span] = []
    if lecture.texte.strip():
        spans = analyse(model, lecture.texte, **kw_analyse)
        # Meme regle que sur le texte du document : au niveau « patient », on
        # ne filtre par role que si l'on sait qui est le patient.
        if niveau is not Niveau.COMPLET and noms_patient:
            spans = filtrer_par_role(lecture.texte, spans, niveau,
                                     set(noms_patient))
        spans = _merge(propager_chaines(lecture.texte, spans, connues),
                       lecture.texte)
    rapport.entites = spans

    # Apres l'OCR, qui lit les pixels d'origine ; ``rvb`` porte ensuite les
    # aplats, ``image`` les recevra dans `_noircir`.
    codes = codes_barres.masquer(rvb)
    rapport.codes_barres = len(codes)
    boites = lecture.boites_pour(spans) + codes
    if not boites:
        # Rien a masquer : on rend les octets d'origine, sans recompression. Un
        # logo ou un graphique traverse la chaine intact.
        rapport.duree = time.monotonic() - depart
        return octets, rapport

    try:
        produits = _noircir(image, boites)
        rapport.boites = len(boites)
    except Exception as exc:
        # L'image se lit mais ne se reecrit pas : ses donnees restent visibles.
        # Le dire, et laisser la verification le confirmer.
        rapport.illisible = _raison(exc)
        produits = octets

    rapport.duree = time.monotonic() - depart
    return produits, rapport


def fuites_dans_images(medias: dict[str, bytes],
                       chaines_interdites,
                       langue: str = LANGUE) -> dict[str, list[str]]:
    """Relit les images produites et rend, par chaine, les images ou on la lit.

    Nommer l'image compte, pour la meme raison que la page compte dans la
    chaine image : « cette date subsiste » n'aide personne, « dans image2.png »
    se verifie en dix secondes.

    Meme critere que la chaine image : on compare en ignorant les blancs et la
    casse, parce que l'OCR d'un mot a demi masque rend volontiers « M artine ».

    Une image qu'aucun decodeur n'ouvre est passee : elle n'est pas verifiable,
    et le compte rendu du caviardage l'a deja signalee comme telle.
    """
    interdites = {c.strip() for c in chaines_interdites if c and c.strip()}
    codes_barres.exiger()
    if interdites:
        tesseract.exiger(langue)
    fuites: dict[str, list[str]] = {}
    for nom, octets in medias.items():
        try:
            rvb = _ouvrir(octets).convert("RGB")
        except Exception:
            continue
        # Un code encore decodable est une fuite, quelles que soient les
        # chaines cherchees : on ne sait pas ce qu'il dit, et c'est le point.
        if codes_barres.compter(rvb):
            fuites.setdefault(codes_barres.CODE_ENCORE_LISIBLE, []).append(nom)
        if not interdites:
            continue
        try:
            relu = ocr_image(rvb, langue=langue)
        except Exception as exc:
            raise tesseract.TesseractIndisponible(
                "La vérification par OCR a échoué sur une image du document "
                f"({type(exc).__name__}) : Caviardoc refuse de la livrer non "
                "contrôlée. Aucun fichier n'a été écrit. "
                + tesseract.REMEDE) from exc
        compact = replier(relu.texte)
        for chaine in interdites:
            if (a := replier(chaine)) and a in compact:
                fuites.setdefault(chaine, []).append(nom)
    return fuites


def noircir_relues(octets: bytes, chaines: dict[str, str],
                   langue: str = LANGUE) -> bytes | None:
    """Noircit les mots ou la verification relit encore ces chaines.

    Meme lecture que `fuites_dans_images` — meme conversion, meme OCR, meme
    critere de recherche —, donc les memes mots : ce qu'elle denoncait est
    exactement ce qu'on noircit. Rend les octets reecrits, ou None si rien n'a
    pu etre situe ou reecrit ; l'image reste alors signalee.
    """
    try:
        image = _ouvrir(octets)
        lecture = ocr_image(image.convert("RGB"), langue=langue)
        boites = lecture.boites_pour(occurrences_relues(lecture.texte, chaines))
        return _noircir(image, boites) if boites else None
    except Exception:
        return None


#: Formats qu'un .docx porte legitimement. Sans liste, Pillow choisit le
#: decodeur sur le contenu et non sur le nom : un media en PostScript (EPS)
#: lancerait Ghostscript, sans delai.
FORMATS_IMAGE = ("PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP")

#: Plafond de pixels d'une image decodee. Une page A3 a 600 dpi en fait 70
#: millions ; Pillow refuse au-dela du double de ce plafond.
PIXELS_MAX_IMAGE = 100_000_000


def _ouvrir(octets: bytes):
    """Ouvre et charge l'image, ou refuse celles qu'on ne saurait pas rendre."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = PIXELS_MAX_IMAGE
    image = Image.open(io.BytesIO(octets), formats=FORMATS_IMAGE)
    image.load()
    if getattr(image, "n_frames", 1) > 1:
        raise ValueError(f"image animee ({image.n_frames} vues)")
    return _comme_rendue(image)


def _comme_rendue(image):
    """L'image telle que Word l'affiche : orientee, et posee sur du blanc.

    L'OCR et les aplats travaillent sur ces pixels. Une couleur stockee noire
    sous un fond transparent se lisait en noir sur noir (``convert('RGB')``
    jette l'alpha), et une photo EXIF couchee se lisait de travers. Le format
    d'origine est conserve : `_noircir` reencode dedans.
    """
    from PIL import Image, ImageOps

    format_origine = image.format
    if image.getexif().get(0x0112, 1) != 1:
        # exif_transpose copie l'image : seulement si elle est a tourner, pour
        # garder ses tables de quantification quand il n'y a rien a faire.
        image = ImageOps.exif_transpose(image)
    transparente = (
        image.mode in ("RGBA", "LA", "PA")
        or (image.mode == "P" and "transparency" in image.info))
    if transparente:
        rgba = image.convert("RGBA")
        fond = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(fond, rgba).convert("RGB")
    image.format = format_origine
    return image


def _noircir(image, boites) -> bytes:
    """Pose les aplats noirs et reencode dans le format d'origine.

    Le format doit etre conserve : l'archive nomme l'image par son extension et
    declare son type dans `[Content_Types].xml`. Un PNG ecrit sous un nom en
    .jpeg serait une image que Word n'affiche plus.
    """
    from PIL import ImageDraw

    format_origine = image.format
    if image.mode not in MODES_SURS:
        image = image.convert("RGB")

    dessin = ImageDraw.Draw(image)
    for x0, y0, x1, y1 in boites:
        # « black » plutot qu'un triplet : la couleur est alors interpretee dans
        # le mode de l'image, quel qu'il soit.
        dessin.rectangle([x0, y0, x1, y1], fill="black")

    tampon = io.BytesIO()
    image.save(tampon, format=format_origine, **_encodage(image, format_origine))
    return tampon.getvalue()


def _encodage(image, format_origine: str | None) -> dict:
    """Options d'enregistrement, pour ne pas perdre une generation au passage.

    Un JPEG reenregistre a la qualite par defaut de la bibliotheque perd de
    l'information sans rien y gagner : le document devient plus flou a chaque
    traitement. On reutilise ses tables de quantification quand elles sont
    encore la, et a defaut la qualite de la chaine image.
    """
    if format_origine != "JPEG":
        return {}
    if getattr(image, "quantization", None):
        return {"quality": "keep"}
    # Le mode a ete converti : les tables d'origine ne s'appliquent plus.
    return {"quality": QUALITE_JPEG}


def _raison(exc: Exception) -> str:
    return str(exc) or type(exc).__name__
