"""Codes-barres et codes QR : trouves dans les pixels, masques sans condition.

Une etiquette de patient porte un code-barres ou un QR qui encode l'identite,
le numero de dossier ou un lien : lisible par n'importe quel telephone, il
annule le caviardage du texte a cote. L'OCR ne le voit pas, la detection non
plus. On le cherche donc dans l'image rendue, avec zxing-cpp, et on le recouvre
quel que soit son contenu — decider si un code est sensible reviendrait a le
decoder et a deviner ce qu'il veut dire.

Les codes dessines en vectoriel sont couverts de la meme facon : on cherche dans
la page rendue, pas dans le fichier.
"""

from __future__ import annotations

from .. import tesseract

#: Marge ajoutee autour de chaque code, en part de sa taille : zones de silence,
#: et un code de travers dont la boite serait un peu juste.
MARGE = 0.08

#: Marge minimale, en pixels, quand le code est petit.
MARGE_MIN = 6

#: Surplus de marge, en part de la hauteur, pour un code lineaire : les chiffres
#: imprimes sous les barres disent la meme chose que les barres.
MARGE_CHIFFRES = 0.30

#: Nombre maximal de balayages d'une meme image. Apres avoir recouvert les codes
#: trouves, on regarde encore : un code cache derriere un autre, ou trop petit
#: pour avoir ete vu, apparait parfois au passage suivant.
PASSES_MAX = 3

#: Libelle de la note de rapport et de la fuite de verification.
NOTE = "code-barres masqué"
CODE_ENCORE_LISIBLE = "code-barres encore lisible"


class CodesBarresIndisponibles(tesseract.TesseractIndisponible):
    """zxing-cpp manque : aucun document ne peut etre traite.

    Meme logique que pour Tesseract : un code qu'on ne sait pas chercher serait
    laisse lisible, et le fichier produit passerait pour sur sans l'etre. Le
    document est refuse. Sous-classe de `TesseractIndisponible` pour etre
    presentee partout comme un refus, avec son message tel quel.
    """


REMEDE = ("Relancez « caviardoc setup » (ou l'installation de Caviardoc) pour "
          "installer le paquet zxing-cpp.")


def _zxing():
    try:
        import zxingcpp
    except Exception as exc:                  # ImportError, ou une lib cassee
        raise CodesBarresIndisponibles(
            "La lecture des codes-barres (zxing-cpp) est indisponible "
            f"({type(exc).__name__}) : Caviardoc refuse de traiter un "
            "document sans elle, car un code-barres ou un QR code d'étiquette "
            "resterait lisible dans le fichier produit. Aucun fichier n'a été "
            "écrit. " + REMEDE) from exc
    return zxingcpp


def exiger() -> None:
    """Leve `CodesBarresIndisponibles` si zxing-cpp ne s'importe pas."""
    _zxing()


def _lire(image) -> list:
    """Tous les codes lisibles de l'image, y compris ceux qui echouent au controle.

    Un code abime (somme de controle fausse) se lit encore au telephone, ou
    avec un peu de chance : il est masque comme les autres. Deux binarisations,
    parce que ni l'une ni l'autre ne les trouve tous (fond degrade, scan pale).
    Rotation, inversion et reduction sont celles par defaut de la bibliotheque,
    toutes activees.

    Sauf un candidat en erreur de format dont rien n'a ete lu : sa structure
    meme (dimension, version) n'est pas celle d'un code, et aucun lecteur n'en
    tire quoi que ce soit. Des lignes de texte ordinaires en produisent — un
    « DataMatrix » de dimension impossible, large comme la page —, et le
    recouvrir noircissait tout un paragraphe.
    """
    zx = _zxing()
    gris = image if image.mode == "L" else image.convert("L")
    trouves = []
    for binariseur in (zx.Binarizer.LocalAverage, zx.Binarizer.GlobalHistogram):
        trouves += zx.read_barcodes(
            gris, try_rotate=True, try_downscale=True, try_invert=True,
            binarizer=binariseur, return_errors=True)
    return [c for c in trouves if not _sans_structure(c, zx)]


def _sans_structure(code, zx) -> bool:
    """Le candidat a-t-il echoue des le format, sans rien livrer ?"""
    return (bool(code.error) and code.error.type == zx.ErrorType.Format
            and not code.bytes)


def _brute(code) -> tuple[int, int, int, int]:
    """Le plus petit rectangle qui contient les quatre coins du code."""
    p = code.position
    coins = (p.top_left, p.top_right, p.bottom_right, p.bottom_left)
    xs, ys = [c.x for c in coins], [c.y for c in coins]
    return min(xs), min(ys), max(xs), max(ys)


def _marge(code, brute, largeur: int, hauteur: int) -> tuple[int, int, int, int]:
    """Le rectangle du code, marges comprises, borne a l'image."""
    x0, y0, x1, y1 = brute
    w, h = x1 - x0 + 1, y1 - y0 + 1
    mx = max(MARGE_MIN, round(MARGE * w))
    my = max(MARGE_MIN, round(MARGE * h))
    if int(code.format) & int(_zxing().BarcodeFormat.AllLinear):
        # Barres horizontales : les chiffres sont au-dessus ou au-dessous.
        # Barres verticales (code couche) : a droite ou a gauche.
        if w >= h:
            my += round(MARGE_CHIFFRES * h)
        else:
            mx += round(MARGE_CHIFFRES * w)
    return (max(0, x0 - mx), max(0, y0 - my),
            min(largeur - 1, x1 + mx), min(hauteur - 1, y1 + my))


def _distincts(codes) -> list[tuple]:
    """Les codes sans doublon : les deux binarisations trouvent les memes."""
    gardes: list[tuple] = []
    for code in codes:
        brute = _brute(code)
        cx, cy = (brute[0] + brute[2]) / 2, (brute[1] + brute[3]) / 2
        if not any(g[1][0] <= cx <= g[1][2] and g[1][1] <= cy <= g[1][3]
                   for g in gardes):
            gardes.append((code, brute))
    return gardes


def boites(image) -> list[tuple[int, int, int, int]]:
    """Les rectangles (x0, y0, x1, y1) de tous les codes de l'image, en pixels."""
    return [_marge(c, b, image.width, image.height)
            for c, b in _distincts(_lire(image))]


def compter(image) -> int:
    """Combien de codes l'image laisse-t-elle lisibles ? Sert a verifier."""
    return len(_distincts(_lire(image)))


def masquer(image) -> list[tuple[int, int, int, int]]:
    """Recouvre en noir, dans ``image``, tous les codes qu'elle porte.

    ``image`` est modifiee sur place (mode RVB ou L). Rend les rectangles
    recouverts, pour qu'on puisse les poser aussi sur une autre copie de la
    meme image.
    """
    from PIL import ImageDraw

    pose: list[tuple[int, int, int, int]] = []
    for _ in range(PASSES_MAX):
        trouvees = boites(image)
        if not trouvees:
            break
        dessin = ImageDraw.Draw(image)
        for x0, y0, x1, y1 in trouvees:
            dessin.rectangle([x0, y0, x1, y1],
                             fill=0 if image.mode == "L" else (0, 0, 0))
        pose += trouvees
    return pose
