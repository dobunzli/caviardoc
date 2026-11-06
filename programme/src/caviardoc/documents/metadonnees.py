"""Metadonnees des images : ce qu'une photo porte sans le montrer.

Un appareil, un telephone ou un logiciel de retouche ecrit dans le fichier
bien plus que l'image : l'auteur, une description, la date de prise de vue,
le numero de serie de l'appareil — et souvent les **coordonnees GPS** du lieu.
Une photo de plaie prise a domicile donne ainsi l'adresse du patient, alors
qu'aucun pixel ne la montre et qu'aucune reconnaissance optique ne la lira.

Un .docx garde les images telles qu'on les a inserees, metadonnees comprises.
On les retire ici **sans reencoder** : on recopie l'image segment par segment
en laissant de cote ceux qui ne portent que des metadonnees. Les pixels restent
identiques a l'octet pres, et l'image ne perd pas une generation de qualite.

Le nettoyage n'utilise que la bibliotheque standard ; seul le constat sur les
autres formats ouvre l'image, avec Pillow.
"""

from __future__ import annotations

from dataclasses import dataclass

SIGNATURE_PNG = b"\x89PNG\r\n\x1a\n"

#: Blocs PNG de pure metadonnee : textes libres (auteur, description, logiciel),
#: EXIF, et date de derniere modification.
BLOCS_PNG_RETIRES = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


@dataclass
class Nettoyage:
    """Ce que le nettoyage d'une image a donne."""

    octets: bytes
    #: Segments ou blocs de metadonnees retires.
    retires: int = 0
    #: Format que l'on ne sait pas nettoyer sans reencoder : les metadonnees
    #: eventuelles y restent, et le compte rendu doit le dire.
    non_pris_en_charge: bool = False


def nettoyer_image(octets: bytes) -> Nettoyage:
    """Retire les metadonnees d'une image JPEG ou PNG, sans toucher aux pixels.

    Les autres formats sont rendus tels quels, marques comme non pris en
    charge. Un fichier incoherent aussi : mieux vaut le signaler que livrer un
    fichier que plus rien n'ouvre.
    """
    try:
        if octets.startswith(b"\xff\xd8"):
            return Nettoyage(*_nettoyer_jpeg(octets))
        if octets.startswith(SIGNATURE_PNG):
            return Nettoyage(*_nettoyer_png(octets))
    except ValueError:
        pass
    return Nettoyage(octets, 0, True)


def porte_des_metadonnees(octets: bytes) -> bool:
    """Une image d'un autre format porte-t-elle des metadonnees visibles ?

    Sert au compte rendu : un GIF ou un TIFF sans rien dedans ne merite pas
    d'alerte, un TIFF signe de son auteur, si. Une image qu'aucun decodeur
    n'ouvre est deja signalee ailleurs, comme illisible.
    """
    import io

    try:
        from PIL import Image

        from .images import FORMATS_IMAGE

        image = Image.open(io.BytesIO(octets), formats=FORMATS_IMAGE)
        return bool(image.getexif()) or any(
            cle in image.info
            for cle in ("exif", "xmp", "XML:com.adobe.xmp", "comment"))
    except Exception:
        return False


def _garder_segment_jpeg(marqueur: int, contenu: bytes) -> bool:
    """Un segment d'en-tete JPEG doit-il survivre ?

    Tout ce qui decrit le decodage reste (tables, dimensions). Des segments
    applicatifs APPn et des commentaires, seuls restent ceux dont depend le
    rendu : JFIF, le profil de couleur ICC et le marqueur Adobe, qui dit comment
    interpreter les canaux. Partent EXIF et XMP (APP1), IPTC et Photoshop
    (APP13), les commentaires, et les extensions des fabricants.
    """
    if not (0xE0 <= marqueur <= 0xEF or marqueur == 0xFE):
        return True
    if marqueur == 0xE0:
        return contenu.startswith(b"JFIF\x00")
    if marqueur == 0xE2:
        return contenu.startswith(b"ICC_PROFILE\x00")
    if marqueur == 0xEE:
        return contenu.startswith(b"Adobe")
    return False


def _nettoyer_jpeg(octets: bytes) -> tuple[bytes, int]:
    n = len(octets)
    sortie = bytearray(octets[:2])
    i, retires = 2, 0
    while i < n:
        if octets[i] != 0xFF:
            # Donnees compressees, apres un debut de balayage (SOS) : on les
            # recopie jusqu'au prochain vrai marqueur. Dans ces donnees, un
            # 0xFF est toujours suivi de 0x00 (octet bourre) ou d'un RSTn.
            j = i
            while True:
                j = octets.find(b"\xff", j)
                if j == -1 or j + 1 >= n:
                    raise ValueError("JPEG tronque")
                suivant = octets[j + 1]
                if suivant == 0x00 or 0xD0 <= suivant <= 0xD7:
                    j += 2
                    continue
                if suivant == 0xFF:          # remplissage avant un marqueur
                    j += 1
                    continue
                break
            sortie += octets[i:j]
            i = j
            continue

        while i < n and octets[i] == 0xFF:
            i += 1
        if i >= n:
            raise ValueError("JPEG tronque")
        marqueur = octets[i]
        i += 1

        if marqueur == 0xD9:                 # fin de l'image
            sortie += b"\xff\xd9"
            # Ce qui suit n'appartient pas a l'image : vignettes et cartes de
            # gain que les telephones y accolent, avec leurs propres EXIF.
            if i < n:
                retires += 1
            return bytes(sortie), retires
        if 0xD0 <= marqueur <= 0xD7 or marqueur == 0x01:
            sortie += bytes((0xFF, marqueur))
            continue

        if i + 2 > n:
            raise ValueError("JPEG tronque")
        longueur = int.from_bytes(octets[i:i + 2], "big")
        if longueur < 2 or i + longueur > n:
            raise ValueError("segment JPEG incoherent")
        if _garder_segment_jpeg(marqueur, octets[i + 2:i + longueur]):
            sortie += bytes((0xFF, marqueur)) + octets[i:i + longueur]
        else:
            retires += 1
        i += longueur
    raise ValueError("JPEG sans fin d'image")


def _nettoyer_png(octets: bytes) -> tuple[bytes, int]:
    n = len(octets)
    sortie = bytearray(SIGNATURE_PNG)
    i, retires = len(SIGNATURE_PNG), 0
    while i + 12 <= n:
        longueur = int.from_bytes(octets[i:i + 4], "big")
        type_bloc = octets[i + 4:i + 8]
        fin = i + 12 + longueur
        if fin > n:
            raise ValueError("bloc PNG tronque")
        if type_bloc in BLOCS_PNG_RETIRES:
            retires += 1
        else:
            sortie += octets[i:fin]
        i = fin
        if type_bloc == b"IEND":
            if i < n:
                retires += 1
            return bytes(sortie), retires
    raise ValueError("PNG sans fin d'image")
