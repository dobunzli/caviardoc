"""OCR avec coordonnees, et correspondance entre offsets de caracteres et boites.

C'est le pivot de la chaine image. Le moteur de detection raisonne en offsets
de caracteres ; le caviardage, lui, a besoin de rectangles en pixels. Ce module
tient les deux bouts : il reconstruit un texte lisible a partir des mots
reconnus, tout en memorisant pour chaque mot l'intervalle de caracteres qu'il
occupe dans ce texte et le rectangle qu'il occupe dans l'image.

Un point de forme compte plus qu'il n'y parait : les mots sont assembles en
respectant les lignes et les blocs reconnus par Tesseract. Les regles de la
couche 1 dependent des sauts de ligne — celle du code postal exige que le
numero et la localite soient sur la meme ligne — et le modele lit mieux un
texte correctement structure qu'une suite de mots.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field

from .. import tesseract

#: Tesseract attribue -1 aux elements de structure sans texte.
CONF_MINIMALE = 0

#: Part de la page en dessous de laquelle une image n'est pas lue : puces,
#: filets, icones. Une etiquette patient en couvre deux a trois pour cent.
SURFACE_IMAGE_LUE = 0.003

#: Decalage des numeros de bloc des mots lus dans les images, pour qu'aucun ne
#: se confonde avec un bloc de la couche texte : les en-tetes se masquent par
#: bloc entier.
BLOC_IMAGES = 100_000

#: Meme decalage pour les mots apportes par la seconde lecture d'une page
#: (`ocr_page`) et par une couche texte invisible (`completer`). Bien au-dela
#: des blocs d'images, qui en comptent un par image.
BLOC_EPARS = 1_000_000_000
BLOC_COUCHE = 2_000_000_000

#: Seconde lecture : Tesseract en mode « texte epars ». Le mode par defaut
#: analyse d'abord la mise en page, et un en-tete a deux colonnes de tailles
#: differentes le met en echec : il fusionne les lignes des deux colonnes et
#: en saute d'entieres — sur une lettre d'hopital, l'adresse e-mail de
#: l'expediteur et le nom du destinataire. Le mode epars cherche chaque ligne
#: pour elle-meme et les retrouve, mais perd l'ordre de lecture : il complete
#: le mode par defaut, il ne le remplace pas.
PSM_EPARS = "--psm 11"

#: Ressemblance a partir de laquelle deux lectures du meme endroit sont tenues
#: pour le meme mot : « Amold-Vautier » et « Arnold-Vautier » le sont, « Fa » et
#: « Tél. » non.
RESSEMBLANCE = 0.6

#: Confiance minimale d'un mot apporte par la seconde lecture. Le mode epars
#: lit tout ce qui ressemble a de l'encre : les pointilles d'un formulaire,
#: une coche, un trait de stylo. Ces fragments (« EPPLEELELEES », « gegre »)
#: ne disent rien, mais le modele les prend pour des identifiants et masque
#: avec eux les mots qui les entourent. Les lignes que le mode par defaut
#: saute, elles, sont lues avec assurance : 42 % pour un prenom, plus de 90 %
#: pour le reste.
CONFIANCE_EPARS = 40

#: Langue par defaut. « fra » seul donne de meilleurs resultats que « fra+eng »
#: sur des documents francophones : le melange favorise les confusions.
LANGUE = "fra"


@dataclass(frozen=True)
class MotOCR:
    """Un mot reconnu, avec sa boite en pixels dans l'image rendue."""

    texte: str
    x0: float
    y0: float
    x1: float
    y1: float
    confiance: int
    bloc: int
    ligne: int

    def dilatee(self, marge: float) -> tuple[float, float, float, float]:
        """Boite elargie, pour couvrir les bords adoucis des glyphes."""
        return (self.x0 - marge, self.y0 - marge, self.x1 + marge, self.y1 + marge)


@dataclass
class PageOCR:
    """Resultat de l'OCR d'une page : texte reconstruit et geometrie."""

    texte: str = ""
    mots: list[MotOCR] = field(default_factory=list)
    #: Intervalle de caracteres occupe par chaque mot dans ``texte``.
    offsets: list[tuple[int, int]] = field(default_factory=list)
    largeur: int = 0
    hauteur: int = 0
    dpi: int = 300

    @property
    def confiance_moyenne(self) -> float:
        c = [m.confiance for m in self.mots if m.confiance > 0]
        return sum(c) / len(c) if c else 0.0

    def boites_pour(self, spans) -> list[tuple[float, float, float, float]]:
        """Rectangles couvrant les mots touches par les spans donnes.

        Un mot est retenu des qu'il chevauche un span, meme partiellement : il
        vaut mieux noircir un caractere de trop que d'en laisser un lisible.
        """
        retenus: list[MotOCR] = []
        for sp in spans:
            for mot, (d, f) in zip(self.mots, self.offsets):
                if d < sp.end and sp.start < f:
                    retenus.append(mot)
        return [m.dilatee(2.0) for m in _uniques(retenus)]


def _uniques(mots: list[MotOCR]) -> list[MotOCR]:
    vus: set[tuple] = set()
    out: list[MotOCR] = []
    for m in mots:
        cle = (m.x0, m.y0, m.x1, m.y1, m.texte)
        if cle not in vus:
            vus.add(cle)
            out.append(m)
    return out


def confiner_temporaires(dossier) -> str | None:
    """Range les fichiers temporaires du processus sous ``dossier``.

    `pytesseract` ecrit la page rendue et la sortie de Tesseract dans le
    repertoire temporaire du systeme, et ne les efface que si le fil atteint son
    `finally`. Un fil de travail tue avec le processus les laisserait la, hors
    de tout balayage. Rend l'ancienne valeur, pour `restaurer_temporaires`.
    """
    ancien = tempfile.tempdir
    os.makedirs(dossier, exist_ok=True)
    tempfile.tempdir = str(dossier)
    return ancien


def restaurer_temporaires(ancien: str | None) -> None:
    tempfile.tempdir = ancien


def _configurer(pytesseract) -> None:
    """Pointe pytesseract sur notre Tesseract, si nous en avons installe un.

    pytesseract cherche `tesseract` dans le PATH. Le notre n'y est pas — c'est
    voulu : on n'ajoute rien au PATH de la machine — donc on lui donne le
    chemin, et le dossier de langues qui va avec. Sans cette seconde partie,
    le binaire chercherait ses donnees a l'emplacement compile en dur.
    """
    exe = tesseract.binaire_local()
    if exe is None:
        return                       # Tesseract du systeme : on n'y touche pas
    pytesseract.pytesseract.tesseract_cmd = str(exe)
    donnees = tesseract.tessdata()
    if donnees is not None:
        os.environ["TESSDATA_PREFIX"] = str(donnees)


def ocr_page(image, langue: str = LANGUE, dpi: int = 300) -> PageOCR:
    """Lit une page entiere : le mode par defaut, complete par le mode epars.

    C'est la lecture a employer pour une page, a la detection comme a la
    verification : un mot que Tesseract saute n'est ni detecte ni relu, et
    sort en clair avec la mention « verifie ». Elle coute une seconde lecture.
    """
    lecture = ocr_image(image, langue=langue, dpi=dpi)
    epars = ocr_image(image, langue=langue, dpi=dpi, config=PSM_EPARS)
    epars.mots = [m for m in epars.mots if m.confiance >= CONFIANCE_EPARS]
    completer(lecture, epars, BLOC_EPARS)
    return lecture


def ocr_image(image, langue: str = LANGUE, dpi: int = 300,
              config: str = "") -> PageOCR:
    """Reconnait le texte d'une image PIL et rend texte, mots et offsets."""
    import pytesseract
    from pytesseract import Output

    _configurer(pytesseract)
    from ..concurrence import hors_pdf
    # Tesseract tourne dans son propre processus, sur une image deja rendue :
    # un autre document peut se servir de PyMuPDF pendant ce temps.
    with hors_pdf():
        d = pytesseract.image_to_data(image, lang=langue, config=config,
                                      output_type=Output.DICT)

    page = PageOCR(largeur=image.size[0], hauteur=image.size[1], dpi=dpi)
    morceaux: list[str] = []
    position = 0
    bloc_courant: int | None = None
    ligne_courante: int | None = None

    for i, brut in enumerate(d["text"]):
        mot = brut.strip()
        if not mot:
            continue
        conf = int(float(d["conf"][i]))
        if conf < CONF_MINIMALE:
            continue

        bloc, ligne = d["block_num"][i], d["line_num"][i]

        # Separateur : rien au tout debut, un saut de ligne en changeant de
        # ligne, deux en changeant de bloc, une espace sinon.
        if bloc_courant is None:
            sep = ""
        elif bloc != bloc_courant:
            sep = "\n\n"
        elif ligne != ligne_courante:
            sep = "\n"
        else:
            sep = " "
        if sep:
            morceaux.append(sep)
            position += len(sep)

        debut = position
        morceaux.append(mot)
        position += len(mot)

        page.mots.append(MotOCR(
            texte=mot,
            x0=float(d["left"][i]), y0=float(d["top"][i]),
            x1=float(d["left"][i] + d["width"][i]),
            y1=float(d["top"][i] + d["height"][i]),
            confiance=conf, bloc=bloc, ligne=ligne))
        page.offsets.append((debut, position))
        bloc_courant, ligne_courante = bloc, ligne

    page.texte = "".join(morceaux)
    return page


#: Hauteur maximale d'une boite de la couche texte au-dessus et au-dessous de
#: la ligne de base, en corps de police. Les glyphes latins, capitales
#: accentuees comprises, tiennent sous 0,95 corps, et les metriques des polices
#: standard (Helvetica : 1,075 et 0,299) passent intactes. Certaines polices
#: embarquees declarent des metriques gonflees — 1,49 et 0,43 corps pour une
#: Arial — et `get_text("words")` en tire des boites de deux interlignes :
#: celles d'un nom noircissaient le bas de la ligne du dessus.
COUCHE_ASCENDANTE_MAX = 1.08
COUCHE_DESCENDANTE_MAX = 0.3


def _bornes_des_lignes(page) -> list[tuple[float, float, float, float, float, float]]:
    """Pour chaque span horizontal de la couche : sa boite et ses bornes utiles.

    Rend ``(x0, y0, x1, y1, haut, bas)`` dans le repere brut du contenu, ou
    ``haut`` et ``bas`` bornent l'encre plausible autour de la ligne de base
    (`COUCHE_ASCENDANTE_MAX`, `COUCHE_DESCENDANTE_MAX`).
    """
    bornes = []
    try:
        blocs = page.get_text("dict")["blocks"]
    except Exception:
        return bornes
    for bloc in blocs:
        for ligne in bloc.get("lines", ()):
            dx, dy = ligne.get("dir", (1, 0))
            if abs(dy) > 1e-3 or dx <= 0:
                continue                 # texte vertical ou renverse : intact
            for span in ligne.get("spans", ()):
                taille = span.get("size", 0)
                if taille <= 0:
                    continue
                base = span["origin"][1]
                x0, y0, x1, y1 = span["bbox"]
                bornes.append((x0, y0, x1, y1,
                               base - taille * COUCHE_ASCENDANTE_MAX,
                               base + taille * COUCHE_DESCENDANTE_MAX))
    return bornes


def _resserrer(x0, y0, x1, y1, bornes) -> tuple[float, float]:
    """Les bornes verticales du mot, ramenees a l'encre plausible de ses spans.

    Une boite n'est jamais agrandie : seules les metriques qui depassent les
    plafonds sont ramenees. Un mot sans span reconnu garde sa boite.
    """
    hauts, bas = [], []
    for sx0, sy0, sx1, sy1, haut, bas_ in bornes:
        if (sx1 > x0 and sx0 < x1 and sy0 >= y0 - 0.5 and sy1 <= y1 + 0.5):
            hauts.append(haut)
            bas.append(bas_)
    if not hauts:
        return y0, y1
    haut, fond = max(y0, min(hauts)), min(y1, max(bas))
    return (haut, fond) if haut < fond else (y0, y1)


def mots_depuis_couche_texte(page, dpi: int = 300) -> PageOCR:
    """Meme structure que l'OCR, mais alimentee par la couche texte du PDF.

    Quand un document porte une couche texte saine, elle est meilleure que
    l'OCR : pas d'erreur de reconnaissance, accents corrects, ponctuation
    exacte. On s'en sert donc pour la detection, tout en caviardant malgre tout
    les pixels — c'est la seule facon de garantir qu'il ne subsiste rien.

    Les coordonnees sont converties de points PDF en pixels de l'image rendue,
    pour etre directement utilisables par le caviardage.

    **La rotation de la page est appliquee.** `get_text("words")` rend des
    coordonnees dans le repere brut du contenu, alors que `get_pixmap` dessine
    la page telle qu'elle s'affiche, rotation comprise. Sur une page pivotee —
    le cas ordinaire d'un document scanne de travers, redresse par un
    `/Rotate` — les deux reperes divergent, et les rectangles noirs
    atterrissaient a cote : le document sortait avec ses donnees intactes.

    Le pire etait la suite : la verification finale reocerise la meme image
    pivotee, sur laquelle Tesseract ne lit rien de fiable. Elle ne signalait
    donc aucune fuite, et l'outil annoncait « relu et verifie » sur un document
    ou le nom du patient restait parfaitement lisible.
    """
    import pymupdf

    echelle = dpi / 72.0
    # Le pont entre les deux reperes. Identite quand la page n'est pas pivotee.
    rotation = page.rotation_matrix
    page_ocr = PageOCR(largeur=int(page.rect.width * echelle),
                       hauteur=int(page.rect.height * echelle), dpi=dpi)
    morceaux: list[str] = []
    position = 0
    bloc_courant: int | None = None
    ligne_courante: int | None = None
    bornes = _bornes_des_lignes(page)

    for x0, y0, x1, y1, mot, bloc, ligne, _ in page.get_text("words"):
        mot = mot.strip()
        if not mot:
            continue

        y0, y1 = _resserrer(x0, y0, x1, y1, bornes)
        boite = pymupdf.Rect(x0, y0, x1, y1) * rotation

        if bloc_courant is None:
            sep = ""
        elif bloc != bloc_courant:
            sep = "\n\n"
        elif ligne != ligne_courante:
            sep = "\n"
        else:
            sep = " "
        if sep:
            morceaux.append(sep)
            position += len(sep)

        debut = position
        morceaux.append(mot)
        position += len(mot)

        page_ocr.mots.append(MotOCR(
            texte=mot,
            x0=boite.x0 * echelle, y0=boite.y0 * echelle,
            x1=boite.x1 * echelle, y1=boite.y1 * echelle,
            # La couche texte n'est pas une reconnaissance : confiance maximale.
            confiance=100, bloc=bloc, ligne=ligne))
        page_ocr.offsets.append((debut, position))
        bloc_courant, ligne_courante = bloc, ligne

    page_ocr.texte = "".join(morceaux)
    return page_ocr


def couche_invisible(page) -> bool:
    """La couche texte de la page est-elle entierement invisible ?

    C'est la marque d'un scan rendu « interrogeable » : le scanner ou le
    logiciel d'archivage a pose sous l'image le texte qu'il y a reconnu, en
    mode de rendu 3. Cette reconnaissance n'est pas une couche texte : elle
    vaut ce que vaut son OCR. Sur un compte rendu de neurologie, elle lisait
    « Sop.h:9.e.. ‘Kelle: » la ou Tesseract lit « Sophie Keller ».
    """
    try:
        traces = page.get_texttrace()
    except Exception:
        return False
    return bool(traces) and all(t["type"] == 3 for t in traces)


def zones_images(page, dpi: int = 300) -> list[tuple[float, float, float, float]]:
    """Les images de la page assez grandes pour porter du texte, en pixels.

    Meme repere que `mots_depuis_couche_texte` : rotation appliquee, puis mise
    a l'echelle du rendu.
    """
    echelle = dpi / 72.0
    rotation = page.rotation_matrix
    cadre = page.rect
    surface = cadre.width * cadre.height
    zones: list[tuple[float, float, float, float]] = []
    for info in page.get_images(full=True):
        try:
            rects = page.get_image_rects(info[0])
        except Exception:
            continue
        for brut in rects:
            r = (brut * rotation) & cadre
            if r.is_empty or r.width * r.height < surface * SURFACE_IMAGE_LUE:
                continue
            zones.append((r.x0 * echelle, r.y0 * echelle,
                          r.x1 * echelle, r.y1 * echelle))
    return zones


#: Jeu, en pixels, entre la boite d'un mot lu par l'OCR et celle, deja dilatee,
#: du meme mot dans la couche : au-dela, l'OCR voit de l'encre que la boite de
#: la couche ne couvre pas, et le mot est garde en double.
JEU_DEJA_LU = 2.0


def completer_par_les_images(lecture: PageOCR, rendu, zones,
                             langue: str = LANGUE, dpi: int = 300,
                             lecteur=None) -> int:
    """Ajoute a ``lecture`` le texte que l'OCR lit dans les images de la page.

    Une couche texte saine ne dit rien des images posees sur la page. Le cas
    qui l'a montre : une page scannee a laquelle le logiciel d'archivage avait
    ajoute une ligne « importe le 12.06.2024 ». Deux lignes de vrai texte
    suffisaient a juger la couche saine ; la detection ne lisait qu'elles, et
    le scan entier — nom, date de naissance, adresse — sortait en clair, avec
    la mention « verifie », puisque la verification ne cherche que ce qui a
    ete detecte. Une etiquette patient collee en image passait de meme.

    Les mots deja presents dans la couche texte ne sont pas repris : un scan
    rendu « interrogeable » porte sa propre reconnaissance, et la doubler ne
    ferait que dupliquer chaque mot. Rend le nombre de mots ajoutes.

    ``lecteur`` remplace `ocr_image` (la page entiere passe par `ocr_page`).
    """
    occupees = [(m.dilatee(2.0), m.texte) for m in lecture.mots]

    def deja_lu(mot: MotOCR) -> bool:
        # Egalite apres le repli commun, non ressemblance : garder un mot en
        # double coute peu, en perdre une lecture differente laisse fuir un nom.
        # Et la boite deja connue doit contenir le mot lu : c'est elle qui
        # sera noircie. Une signature en image, posee un peu au-dessus de sa
        # couche texte, montrait sinon le haut du nom au-dessus du rectangle.
        from ..pipeline import replier

        lu = replier(mot.texte)
        t = JEU_DEJA_LU
        return any(a - t <= mot.x0 and mot.x1 <= c + t
                   and b - t <= mot.y0 and mot.y1 <= d + t
                   and replier(texte) == lu
                   for (a, b, c, d), texte in occupees)

    morceaux = [lecture.texte]
    position = len(lecture.texte)
    ajoutes = 0
    for numero, (x0, y0, x1, y1) in enumerate(zones):
        cadre = (max(0, int(x0) - 4), max(0, int(y0) - 4),
                 min(rendu.width, int(x1) + 4), min(rendu.height, int(y1) + 4))
        if cadre[2] - cadre[0] < 16 or cadre[3] - cadre[1] < 8:
            continue
        lu = (lecteur or ocr_image)(rendu.crop(cadre), langue=langue, dpi=dpi)
        precedent: tuple[int, int] | None = None
        for mot in lu.mots:
            place = MotOCR(texte=mot.texte,
                           x0=mot.x0 + cadre[0], y0=mot.y0 + cadre[1],
                           x1=mot.x1 + cadre[0], y1=mot.y1 + cadre[1],
                           confiance=mot.confiance,
                           bloc=BLOC_IMAGES * (numero + 1) + mot.bloc,
                           ligne=mot.ligne)
            if deja_lu(place):
                continue
            # Chaque image commence un nouveau bloc, comme un paragraphe.
            if position == 0:
                sep = ""
            elif precedent is None or place.bloc != precedent[0]:
                sep = "\n\n"
            elif place.ligne != precedent[1]:
                sep = "\n"
            else:
                sep = " "
            morceaux.append(sep)
            position += len(sep)
            lecture.mots.append(place)
            lecture.offsets.append((position, position + len(place.texte)))
            morceaux.append(place.texte)
            position += len(place.texte)
            occupees.append((place.dilatee(2.0), place.texte))
            precedent = (place.bloc, place.ligne)
            ajoutes += 1
    lecture.texte = "".join(morceaux)
    return ajoutes


def _cle(texte: str) -> str:
    return "".join(c for c in texte.lower() if c.isalnum())


def _semblables(a: str, b: str) -> bool:
    """Deux lectures du meme endroit disent-elles le meme mot ?"""
    from difflib import SequenceMatcher

    a, b = _cle(a), _cle(b)
    if len(a) < 2 or len(b) < 2:
        return bool(a) and a == b
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= RESSEMBLANCE


def completer(lecture: PageOCR, autre: PageOCR, decalage: int) -> int:
    """Ajoute a ``lecture`` les mots de ``autre`` qu'elle n'a pas lus.

    Un mot est deja lu si ``lecture`` porte, au meme endroit, un mot qui lui
    ressemble. La position seule ne suffit pas : la ou le mode par defaut a
    fusionne deux lignes, il a pose sur « Tél. 032 555 30 00 » un mot « Fa »
    qui en couvre toute la place. Les mots ajoutes forment leurs propres
    blocs, a la suite du texte, numerotes a partir de ``decalage``. Les deux
    lectures doivent etre dans le meme repere. Rend le nombre de mots ajoutes.
    """
    deja = [(m.dilatee(2.0), m.texte) for m in lecture.mots]

    def deja_lu(mot: MotOCR) -> bool:
        x, y = (mot.x0 + mot.x1) / 2, (mot.y0 + mot.y1) / 2
        return any(a <= x <= c and b <= y <= d and _semblables(mot.texte, t)
                   for (a, b, c, d), t in deja)

    nouveaux = [MotOCR(texte=m.texte, x0=m.x0, y0=m.y0, x1=m.x1, y1=m.y1,
                       confiance=m.confiance, bloc=decalage + m.bloc,
                       ligne=m.ligne)
                for m in autre.mots if _cle(m.texte) and not deja_lu(m)]
    _ajouter(lecture, nouveaux)
    return len(nouveaux)


def _ajouter(lecture: PageOCR, mots: list[MotOCR]) -> None:
    """Ajoute des mots a la suite du texte de ``lecture``, bloc par bloc."""
    morceaux = [lecture.texte]
    position = len(lecture.texte)
    precedent: tuple[int, int] | None = None
    for mot in mots:
        if position == 0:
            sep = ""
        elif precedent is None or mot.bloc != precedent[0]:
            sep = "\n\n"
        elif mot.ligne != precedent[1]:
            sep = "\n"
        else:
            sep = " "
        morceaux.append(sep)
        position += len(sep)
        lecture.mots.append(mot)
        lecture.offsets.append((position, position + len(mot.texte)))
        morceaux.append(mot.texte)
        position += len(mot.texte)
        precedent = (mot.bloc, mot.ligne)
    lecture.texte = "".join(morceaux)


def completer_par_la_page(lecture: PageOCR, rendu, langue: str = LANGUE,
                          dpi: int = 300, lue: PageOCR | None = None) -> int:
    """Ajoute a ``lecture`` ce que l'OCR lit sur toute la page rendue.

    La couche texte n'est pas ce que voit le lecteur : un tampon trop petit
    pour `zones_images`, un texte dessine en traces ou une police sans table
    Unicode restent dans les pixels sans que la detection les voie. On lit donc
    l'image meme qui devient la sortie, et on garde les mots que la couche ne
    porte pas deja. Rend le nombre de mots ajoutes.

    ``lue`` : lecture de la page deja faite par l'appelant, reprise telle
    quelle plutot que de relancer l'OCR.
    """
    lecteur = (lambda *a, **k: lue) if lue is not None else ocr_page
    return completer_par_les_images(
        lecture, rendu, [(0, 0, rendu.width, rendu.height)],
        langue=langue, dpi=dpi, lecteur=lecteur)
