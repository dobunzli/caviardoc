"""Ecriture manuscrite que l'OCR ne lit pas : masquee, et signalee.

Un nom ecrit a la main en haut d'un electrocardiogramme, une date griffonnee dans un coin : l'OCR n'en tire que des bribes sans
confiance, la detection n'a donc rien a masquer, et le nom restait lisible dans
un document presente comme verifie. On cherche dans les pixels l'encre que
l'OCR n'explique pas, haute comme une ecriture, et on la recouvre. Comme on ne
sait pas ce qu'elle dit, la page est signalee : a verifier a l'oeil.

Prudent sur l'imprime : l'encre des mots bien lus est retiree d'abord, et ce
qui ne depasse pas la hauteur des mots de la page reste. Les filets sont
retires, l'interieur d'une grille est ecarte, et une courbe n'est pas une
ecriture : une seule epaisseur de trait par colonne, la ou les lettres font
des boucles.

Une ecriture posee sur la grille elle-meme y est cherchee a part, dans l'encre
plus sombre que la grille (`ECART_SOMBRE`), avec des boucles plus marquees.
Ce qu'on n'a pas su trancher — une encre haute dans la grille, une tache a
quelques boucles — est rendu a part (`analyser`) : la page est alors signalee
(`NOTE_DOUTE`), meme si rien n'a ete masque.
"""

from __future__ import annotations

from .zones import _bien_lu, _fusionner, _taches

#: Libelle de la note de rapport.
NOTE = "écriture manuscrite masquée, à vérifier"
#: Libelle de la note quand de l'encre reste qu'on n'a pas su ecarter.
NOTE_DOUTE = "écriture manuscrite possible non masquée, à vérifier à l'œil"

#: Est encre ce qui est plus sombre que le papier de `ECART_PAPIER` niveaux de
#: gris, et au plus `GRIS_MAX` : une ecriture au crayon ou au stylo pale compte.
ECART_PAPIER = 35
GRIS_MAX = 215

#: Cote d'une case de la grille d'analyse, en mm, et part d'encre qui la fait
#: compter comme encree.
CASE_MM = 0.5
PART_CASE = 0.12

#: Un filet est une suite de cases encrees sur `FILET_MM`, couverte a
#: `COUVERTURE_FILET` au moins, a une case pres de travers.
FILET_MM = 25.0
COUVERTURE_FILET = 0.9

#: L'interieur d'une grille : des filets sur trois des quatre cotes (dessus,
#: dessous, gauche, droite), a moins de `MAILLE_MM`. Une grille d'ECG a
#: ses traits forts tous les 5 mm ; la marge couvre un trait que le carre
#: d'etalonnage ou une legende interrompt.
MAILLE_MM = 12.0

#: Hauteur d'une ecriture : au moins `HAUTEUR_MIN_MM`, et `HAUTEUR_MOT` fois la
#: hauteur mediane des mots lus de la page ; un texte imprime n'y atteint pas.
HAUTEUR_MIN_MM = 4.0
HAUTEUR_MOT = 1.25
HAUTEUR_MAX_MM = 40.0
LARGEUR_MAX_MM = 120.0
#: Au-dela, c'est un filet ou un trait, pas une ecriture.
ALLONGEMENT_MAX = 8.0
#: Au-dela, c'est un aplat : carre d'etalonnage, logo plein, tampon encre.
#: Seulement pour une tache epaisse d'au moins `APLAT_MM` : un « 1 » ou un
#: « l » manuscrit, un trait, est plein aussi.
REMPLISSAGE_MAX = 0.6
APLAT_MM = 3.0
#: Plus basse, une tache n'est qu'un point : ignoree.
GRAIN_MM = 1.0
#: Une tache haute fait une ligne d'ecriture si elle est large d'au moins
#: `LARGEUR_ANCRE_MM` et que le trait passe deux fois ou plus dans
#: `BOUCLES_MIN` de ses colonnes : les boucles des lettres. Un trace d'ECG, une
#: courbe, n'en a presque pas. Sinon (un « 1 », un « M », une pointe du
#: trace), elle ne fait que s'ajouter a une ligne voisine, si elle est large
#: d'au plus `LARGEUR_VOISINE_MM`.
BOUCLES_MIN = 0.2
LARGEUR_ANCRE_MM = 3.0
LARGEUR_VOISINE_MM = 10.0

#: Un mot que l'OCR a lu de facon plausible, a hauteur de la tache, l'explique.
CONFIANCE_PLAUSIBLE = 50
LETTRES_PLAUSIBLES = 3
RECOUVREMENT_PLAUSIBLE = 0.6

#: Les taches d'une meme ligne, a moins de `ECART_MOTS_MM` les unes des autres,
#: forment une boite, elargie de `MARGE_MM`.
ECART_MOTS_MM = 6.0
MARGE_MM = 1.5

#: L'encre sombre : plus sombre que le papier de `ECART_SOMBRE` niveaux. Une
#: grille d'ECG scannee reste au-dessus (des points epars, pas des traits) ;
#: une ecriture au stylo, meme tramee, et le trace passent dessous. Comme le
#: trace y est aussi, en morceaux, une tache n'y fait une ligne d'ecriture que
#: si le trait passe deux fois dans `BOUCLES_SOMBRE` de ses colonnes, et la
#: ligne ne s'etend qu'aux taches voisines de ses taches hautes, pas de proche
#: en proche le long du trace.
ECART_SOMBRE = 110
BOUCLES_SOMBRE = 0.45

#: Dans une grille, le retrait des filets par cases efface une ecriture pale
#: avec eux. Pour le doute, les traits sont retires au pixel pres, ceux qui
#: couvrent `COUVERTURE_TRAIT` de `FILET_MM` (une grille scannee en
#: pointilles compte), et les morceaux restants regroupes a
#: `PORTEE_DOUTE_MM` pres.
COUVERTURE_TRAIT = 0.5
PORTEE_DOUTE_MM = 1.5


def boites_manuscrit(image, lecture, dpi: int) -> list[tuple[float, float, float, float]]:
    """Les rectangles (x0, y0, x1, y1), en pixels, de l'ecriture non lue."""
    return analyser(image, lecture, dpi)[0]


def analyser(image, lecture, dpi: int):
    """L'ecriture non lue a masquer, et l'encre douteuse qui reste.

    Rend deux listes de rectangles (x0, y0, x1, y1) en pixels : les lignes
    d'ecriture, marge comprise, et les taches qu'on n'a pas su trancher.
    ``image`` est la page d'origine, rendue a ``dpi`` comme ``lecture``.

    L'interieur d'une grille est ecarte de l'encre pale : ses restes de
    quadrillage et les morceaux de trace qui la parcourent ressemblent trop a
    une ecriture. Ce qui y aurait fait une ligne d'ecriture est douteux.
    L'encre sombre est cherchee partout, grille comprise.
    """
    from PIL import ImageChops

    gris = image if image.mode == "L" else image.convert("L")
    mm = dpi / 25.4
    case = max(2, round(CASE_MM * mm))
    if gris.width // case < 3 or gris.height // case < 3:
        return [], []
    longueur = max(2, round(FILET_MM * mm / case))
    papier = _mediane(gris.histogram())
    seuil = min(GRIS_MAX, papier - ECART_PAPIER)
    if seuil <= 0:
        return [], []
    pale = gris.point(lambda v: 255 if v < seuil else 0)
    cases = _en_cases(pale, case)
    horizontaux = _filets(cases, longueur, True)
    verticaux = _filets(cases, longueur, False)
    filets = ImageChops.lighter(horizontaux, verticaux)
    grille = _interieur_de_grille(horizontaux, verticaux,
                                  round(MAILLE_MM * mm / case))

    lecteur = _Lecteur(lecture, case, mm)
    sans_filets = ImageChops.subtract(cases, filets)
    lignes, douteuses = lecteur.lignes(ImageChops.subtract(sans_filets, grille))
    douteuses += lecteur.lignes(sans_filets)[0]
    trait = max(2, round(FILET_MM * mm))
    traits = ImageChops.lighter(_filets(pale, trait, True, COUVERTURE_TRAIT),
                                _filets(pale, trait, False, COUVERTURE_TRAIT))
    entre_les_traits = _en_cases(ImageChops.subtract(pale, traits), case)
    douteuses += lecteur.amas(ImageChops.multiply(entre_les_traits, grille),
                              max(1, round(PORTEE_DOUTE_MM * mm / case)))
    sombre = gris.point(lambda v: 255 if v < papier - ECART_SOMBRE else 0)
    trouvees, incertaines = lecteur.lignes(
        _en_cases(sombre, case), boucles=BOUCLES_SOMBRE, de_proche_en_proche=False)
    lignes += trouvees
    douteuses += incertaines

    marge = MARGE_MM * mm

    def elargies(boites):
        return [(max(0.0, x0 - marge), max(0.0, y0 - marge),
                 min(float(gris.width), x1 + marge),
                 min(float(gris.height), y1 + marge))
                for x0, y0, x1, y1 in _fusionner(boites)]

    return elargies(lignes), elargies(douteuses)


class _Lecteur:
    """Trouve les lignes d'ecriture d'un masque d'encre en cases."""

    def __init__(self, lecture, case: int, mm: float):
        self.lus = [m for m in lecture.mots if _bien_lu(m)]
        hauteurs = sorted(m.y1 - m.y0 for m in self.lus)
        self.mot = hauteurs[len(hauteurs) // 2] if hauteurs else 0.0
        self.plausibles = [
            m for m in lecture.mots if m.confiance >= CONFIANCE_PLAUSIBLE
            and sum(c.isalpha() for c in m.texte) >= LETTRES_PLAUSIBLES]
        self.case, self.mm = case, mm
        self.haut_min = max(HAUTEUR_MIN_MM * mm, HAUTEUR_MOT * self.mot)

    def _sans_mots_lus(self, encre):
        """``encre`` (en cases), privee de l'encre des mots bien lus."""
        from PIL import ImageDraw

        case = self.case
        reste = encre.copy()
        dessin = ImageDraw.Draw(reste)
        marge = 0.15 * self.mot
        for m in self.lus:
            dessin.rectangle([int((m.x0 - marge) / case),
                              int((m.y0 - marge) / case),
                              int((m.x1 + marge) / case),
                              int((m.y1 + marge) / case)], fill=0)
        return reste

    def amas(self, encre, portee: int) -> list[tuple[float, float, float, float]]:
        """Les amas d'encre hauts comme une ecriture, morceaux a ``portee``
        cases les uns des autres reunis, que rien n'explique : ni un mot lu,
        ni un trait (trop allonge). Sans test de boucles : c'est un doute."""
        case, mm = self.case, self.mm
        boites = []
        for gx0, gy0, gx1, gy1, _ in _taches(self._sans_mots_lus(encre), 127,
                                             portee=portee):
            x0, y0, x1, y1 = gx0 * case, gy0 * case, gx1 * case, gy1 * case
            w, h = x1 - x0, y1 - y0
            if h < self.haut_min or w < LARGEUR_ANCRE_MM * mm:
                continue
            if max(w, h) > ALLONGEMENT_MAX * min(w, h):
                continue                                # un trait, un trace
            if any(_explique(m, (x0, y0, x1, y1)) for m in self.plausibles):
                continue
            boites.append((float(x0), float(y0), float(x1), float(y1)))
        return boites

    def lignes(self, encre, boucles: float = BOUCLES_MIN,
               de_proche_en_proche: bool = True):
        """Les rectangles, en pixels et sans marge, des lignes d'ecriture, et
        ceux des taches douteuses : hautes et larges comme une lettre, avec
        des boucles, mais moins que ``boucles``.

        Sans ``de_proche_en_proche``, une ligne ne s'etend qu'aux taches
        voisines de ses taches hautes, une seule fois."""
        case, mm = self.case, self.mm
        reste = self._sans_mots_lus(encre)

        ancres: list[tuple[float, float, float, float]] = []
        voisines: list[tuple[float, float, float, float]] = []
        douteuses: list[tuple[float, float, float, float]] = []
        for gx0, gy0, gx1, gy1, nombre in _taches(reste, 127, portee=1):
            x0, y0, x1, y1 = gx0 * case, gy0 * case, gx1 * case, gy1 * case
            w, h = x1 - x0, y1 - y0
            if h < GRAIN_MM * mm or h > HAUTEUR_MAX_MM * mm \
                    or w > LARGEUR_MAX_MM * mm:
                continue
            if max(w, h) > ALLONGEMENT_MAX * min(w, h):
                continue                                # un trait
            if min(w, h) >= APLAT_MM * mm \
                    and nombre > REMPLISSAGE_MAX * (gx1 - gx0) * (gy1 - gy0):
                continue                                # un aplat
            if any(_explique(m, (x0, y0, x1, y1)) for m in self.plausibles):
                continue
            boite = (float(x0), float(y0), float(x1), float(y1))
            haute = h >= self.haut_min and w >= LARGEUR_ANCRE_MM * mm
            part = _boucles(reste.crop((gx0, gy0, gx1, gy1))) if haute else 0.0
            if haute and part >= boucles:
                ancres.append(boite)
                continue
            if haute and part >= BOUCLES_MIN:
                douteuses.append(boite)
            if w <= LARGEUR_VOISINE_MM * mm:
                voisines.append(boite)

        # Une ligne : les taches hautes, puis, de proche en proche, les plus
        # petites a leur hauteur (les minuscules d'un nom, un « 1 »).
        ecart = ECART_MOTS_MM * mm
        lignes = _rapprocher(ancres, ecart)
        if not de_proche_en_proche:
            voisines = [v for v in voisines
                        if any(_sur_la_ligne(v, a, ecart) for a in ancres)]
        change = True
        while change:
            change = False
            for v in list(voisines):
                for i, ligne in enumerate(lignes):
                    if _sur_la_ligne(v, ligne, ecart):
                        lignes[i] = (min(ligne[0], v[0]), min(ligne[1], v[1]),
                                     max(ligne[2], v[2]), max(ligne[3], v[3]))
                        voisines.remove(v)
                        change = True
                        break
        # Deux lignes nees de deux taches hautes se rejoignent par le milieu.
        return _rapprocher(lignes, ecart), douteuses


def _rapprocher(boites, ecart: float):
    """Reunit les boites a moins de ``ecart`` l'une de l'autre en largeur."""
    return [(x0 + ecart / 2, y0, x1 - ecart / 2, y1)
            for x0, y0, x1, y1 in _fusionner(
                [(x0 - ecart / 2, y0, x1 + ecart / 2, y1)
                 for x0, y0, x1, y1 in boites])]


def _mediane(histogramme) -> int:
    """Le niveau median d'un histogramme : celui du papier, sur une page."""
    total, cumul = sum(histogramme), 0
    for niveau, nombre in enumerate(histogramme):
        cumul += nombre
        if 2 * cumul >= total:
            return niveau
    return 255


def _en_cases(encre, case: int):
    """Le masque d'encre en cases de ``case`` pixels, allumees au-dela de
    `PART_CASE`."""
    from PIL import Image

    largeur, hauteur = encre.width // case, encre.height // case
    return encre.crop((0, 0, largeur * case, hauteur * case)).resize(
        (largeur, hauteur), Image.BOX).point(
        lambda v: 255 if v >= PART_CASE * 255 else 0)


def _filets(cases, longueur: int, horizontal: bool,
            couverture: float = COUVERTURE_FILET):
    """Les cases des filets droits, horizontaux ou verticaux, d'au moins
    ``longueur`` cases, elargies d'une case de part et d'autre en travers.

    On mesure la couverture par troncons de ``longueur`` cases, decales d'un
    quart : un filet plus long que cinq quarts en remplit au moins un. Une
    bande de deux cases de haut tolere un filet un peu de travers.
    """
    from PIL import Image, ImageChops, ImageFilter

    largeur, hauteur = cases.size
    bande = ImageChops.lighter(cases, _decaler(cases, 0, 1) if horizontal
                               else _decaler(cases, 1, 0))
    filets = Image.new("L", cases.size, 0)
    etendue = largeur if horizontal else hauteur
    for debut in range(0, longueur, max(1, longueur // 4)):
        nombre = (etendue - debut) // longueur
        if nombre <= 0:
            continue
        if horizontal:
            boite = (debut, 0, debut + nombre * longueur, hauteur)
            reduite, pleine = (nombre, hauteur), (nombre * longueur, hauteur)
        else:
            boite = (0, debut, largeur, debut + nombre * longueur)
            reduite, pleine = (largeur, nombre), (largeur, nombre * longueur)
        couverts = bande.crop(boite).resize(reduite, Image.BOX).point(
            lambda v: 255 if v >= couverture * 255 else 0)
        couverts = couverts.resize(pleine, Image.NEAREST)
        filets.paste(ImageChops.lighter(filets.crop(boite), couverts), boite[:2])
    noyau = [0, 1, 0, 0, 1, 0, 0, 1, 0] if horizontal \
        else [0, 0, 0, 1, 1, 1, 0, 0, 0]
    # Deux fois : la bande couvre aussi la case voisine du filet.
    for _ in range(2):
        filets = filets.filter(ImageFilter.Kernel((3, 3), noyau, scale=1)) \
            .point(lambda v: 255 if v else 0)
    return filets


def _decaler(masque, dx: int, dy: int):
    """Le masque decale de (dx, dy) cases, sans retour par l'autre bord."""
    from PIL import Image

    decale = Image.new("L", masque.size, 0)
    decale.paste(masque, (dx, dy))
    return decale


def _interieur_de_grille(horizontaux, verticaux, maille: int):
    """Les cases bordees de filets sur trois cotes au moins, a moins de
    ``maille`` : l'interieur d'une grille, ses bords compris, la ou un trait
    manque. Au-dessus d'une grille, on n'a qu'un cote."""
    from PIL import ImageChops

    def atteint(masque, dx, dy):
        vu = masque
        for d in range(1, maille + 1):
            vu = ImageChops.lighter(vu, _decaler(masque, dx * d, dy * d))
        return vu.point(lambda v: 64 if v else 0)

    cotes = None
    for masque, dx, dy in ((horizontaux, 0, 1), (horizontaux, 0, -1),
                           (verticaux, 1, 0), (verticaux, -1, 0)):
        vu = atteint(masque, dx, dy)
        cotes = vu if cotes is None else ImageChops.add(cotes, vu)
    return cotes.point(lambda v: 255 if v >= 3 * 64 else 0)


def _boucles(tache) -> float:
    """Part des colonnes de la tache que le trait traverse deux fois ou plus."""
    largeur, hauteur = tache.size
    px = tache.load()
    doubles = 0
    for x in range(largeur):
        passages, dedans = 0, False
        for y in range(hauteur):
            if px[x, y] and not dedans:
                passages += 1
            dedans = bool(px[x, y])
        doubles += passages >= 2
    return doubles / max(1, largeur)


def _sur_la_ligne(tache, ligne, ecart: float) -> bool:
    """La tache est-elle a hauteur de la ligne, a moins de ``ecart`` d'elle ?"""
    x0, y0, x1, y1 = tache
    if x0 > ligne[2] + ecart or x1 < ligne[0] - ecart:
        return False
    commun = min(y1, ligne[3]) - max(y0, ligne[1])
    return commun >= 0.5 * (y1 - y0)


def _explique(mot, boite) -> bool:
    """Le mot, lu de facon plausible, couvre-t-il la hauteur de la tache ?"""
    x0, y0, x1, y1 = boite
    if mot.x1 <= x0 or mot.x0 >= x1:
        return False
    commun = min(mot.y1, y1) - max(mot.y0, y0)
    return commun >= RECOUVREMENT_PLAUSIBLE * (y1 - y0)
