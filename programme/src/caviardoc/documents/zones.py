"""Zones a masquer que la detection textuelle ne peut pas voir.

Deux angles morts sont apparus a l'usage sur des documents medicaux reels.

**Les logos.** Un en-tete comme « Centre lacustre d'ophtalmologie » est une
image, pas du texte. Ni l'OCR ni le modele ne le voient, et il identifie
pourtant l'etablissement aussi surement qu'une adresse. Dans un PDF compose,
le logo est une image a part (`boites_logos`) ; dans un scan, il n'est qu'une
tache dans l'image de la page, qu'on repere a sa couleur ou a son aplat
(`boites_logos_dessines`).

**Les blocs expediteur et destinataire.** Meme quand le texte en est masque mot
a mot, la structure subsiste : une specialite medicale, une mention « Case
postale », la disposition caracteristique d'un en-tete a fenetre. Masquer le
bloc entier est plus sur et plus lisible qu'un damier de rectangles.

Ces deux traitements sont **optionnels**. Ils retirent du document des elements
qui ne sont pas des donnees personnelles au sens strict, et cette decision
appartient a l'utilisateur, pas a l'outil.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..engine import Categorie, Origine, Span, est_date

#: Fraction haute de la page ou l'on cherche les blocs d'en-tete.
HAUT_DE_PAGE = 0.35

#: Distance, en points, en deca de laquelle un bloc de texte pose a cote d'un
#: logo en fait partie : « Departement de medecine » colle au logo de l'hopital
#: est la suite de son nom, pas un paragraphe.
ECART_LOGO = 40.0

#: Part minimale d'un mot que doit recouvrir une zone pour que le texte, lui
#: aussi, soit masque. En dessous, la zone ne fait qu'effleurer le mot.
RECOUVREMENT_MOT = 0.5

#: Une image couvrant plus que cette fraction de la page est le scan lui-meme,
#: pas un logo. La masquer noircirait le document entier.
COUVERTURE_MAXIMALE = 0.55

#: Une image plus petite que cela est une puce ou un filet, pas un logo.
SURFACE_MINIMALE = 0.0005

#: Bas de page ou l'on cherche aussi les logos dessines : pieds de page
#: institutionnels, labels de certification.
BAS_DE_PAGE = 0.10

#: Saturation (0-255) au-dela de laquelle un pixel est colore : l'encre d'un
#: logo, pas le papier jauni d'un scan ni le gris du texte.
SATURATION_COULEUR = 90

#: Luminosite (0-255) sous laquelle un pixel colore est trop sombre pour que
#: sa teinte veuille dire quelque chose : le bruit du noir d'un scan.
LUMINOSITE_COULEUR = 70

#: Gris (0-255) sous lequel un pixel est de l'encre sombre.
GRIS_SOMBRE = 110

#: Part d'encre a partir de laquelle une case de la grille compte : un peu de
#: couleur suffit, alors qu'il faut un aplat pour le sombre, que le texte noir
#: n'atteint pas.
CASE_COULEUR = 0.15
CASE_SOMBRE = 0.5

#: Un aplat sombre doit etre rempli a ce point : un logo en noir et blanc, ou
#: un logo colore scanne en niveaux de gris, l'est ; un paragraphe ne l'est pas.
REMPLISSAGE_SOMBRE = 0.5

#: Plus petit cote d'un logo dessine, en hauteurs de mot. Une ligne de texte,
#: meme coloree ou en gras, n'a qu'une hauteur de mot.
COTE_MINIMAL = 2.0

#: Au-dela de ce rapport longueur/largeur, la tache est un filet ou l'ombre du
#: bord d'un scan, pas un logo.
ALLONGEMENT_MAXIMAL = 6.0

#: Part de la tache couverte par des mots lus au-dela de laquelle c'est du
#: texte en couleur, que la detection traite deja mot a mot.
PART_TEXTE_MAXIMALE = 0.6

#: Haut de page ou l'on masque l'encre que l'OCR n'explique pas
#: (`boites_encre_non_lue`) : le logo, le nom et la date ecrits a la main. Plus
#: bas, la meme encre est une coche, un score ou un dessin du patient, qui font
#: le contenu du document.
HAUT_ENCRE = 0.14

#: Confiance a partir de laquelle un mot lu explique l'encre qu'il couvre. En
#: dessous, c'est l'OCR qui devine : « Thoms » pour « Zorglub » ecrit a la
#: main.
CONFIANCE_LUE = 60

#: Gris (0-255) sous lequel un pixel est de l'encre, trait de stylo compris.
GRIS_ENCRE = 140

#: Part d'encre d'une case de grille pour qu'elle compte, et part minimale
#: d'encre d'une tache : un cadre vide n'en est pas une.
CASE_ENCRE = 0.08
REMPLISSAGE_ENCRE = 0.12

#: Plus petit cote d'une tache d'encre non lue, en hauteurs de mot : assez
#: pour les initiales d'un nom ecrit a la main (« J-P »), pas pour une
#: poussiere du scanner.
COTE_ENCRE = 0.6

#: Libelle de la case ou le soignant appose son timbre et signe
#: (`boites_timbres`) : « Timbre et signature du medecin », « Cachet »,
#: « Stempel und Unterschrift ».
LIBELLE_TIMBRE = re.compile(r"^(?:timbre|cachet|signature|stempel|unterschrift)",
                            re.IGNORECASE)

#: Etendue de la case du timbre sous son libelle, en part de la hauteur de
#: page : un timbre et sa signature mesurent quelques centimetres, quelle que
#: soit la taille du texte imprime autour.
HAUTEUR_TIMBRE = 0.15

#: Marge au-dessus du libelle, en hauteurs de mot : un timbre pose de travers
#: depasse la ligne du libelle.
AU_DESSUS_TIMBRE = 3.0

#: Ecart, en hauteurs de mot, sous lequel deux taches d'encre de la case du
#: timbre sont le meme timbre. Masquees une a une, ses lignes palies restaient
#: lisibles entre les taches.
ECART_TIMBRE = 3.0

#: Haut des pages suivantes ou l'on cherche encore les blocs d'en-tete. Un
#: dossier de laboratoire assemble des comptes rendus, chacun avec l'en-tete de
#: son laboratoire ; la premiere page seule etait traitee, et « Service
#: regional de transfusion sanguine » restait lisible aux suivantes. La bande
#: est etroite, pour ne pas toucher au corps d'une lettre qui continue.
HAUT_PAGES_SUIVANTES = 0.15

#: Espace vertical entre deux lignes, en hauteurs de mot, au-dela duquel un bloc
#: de texte change de paragraphe. L'interligne d'une adresse reste en dessous.
SAUT_PARAGRAPHE = 0.75


@dataclass(frozen=True)
class OptionsZones:
    """Ce que l'utilisateur choisit de retirer en plus des donnees detectees."""

    logos: bool = False
    entetes: bool = False
    #: Fraction haute de chaque page a noircir sans condition. Recours brutal,
    #: utile sur les scans ou le logo fait partie de l'image de la page.
    bande_haute: float = 0.0
    #: N'appliquer `entetes` et `bande_haute` qu'a la premiere page.
    premiere_page_seulement: bool = True

    @property
    def actif(self) -> bool:
        return self.logos or self.entetes or self.bande_haute > 0


def boites_logos(page, dpi: int) -> list[tuple[float, float, float, float]]:
    """Rectangles des images integrees, en pixels de l'image rendue.

    Les images couvrant presque toute la page sont ignorees : sur un document
    scanne, la page *est* une image, et la masquer reviendrait a rendre une
    feuille noire. Sur ces documents, c'est `boites_logos_dessines` qui trouve
    le logo dans les pixels.
    """
    echelle = dpi / 72.0
    rotation = getattr(page, "rotation_matrix", None)
    surface_page = page.rect.width * page.rect.height
    if surface_page <= 0:
        return []

    boites: list[tuple[float, float, float, float]] = []
    for info in page.get_images(full=True):
        try:
            rects = page.get_image_rects(info[0])
        except Exception:
            continue
        for brut in rects:
            # get_image_rects rend le repere de la page non pivotee ; le rendu
            # est pivote (voir `ocr.zones_images`).
            r = ((brut * rotation) if rotation is not None else brut) & page.rect
            if r.is_empty:
                continue
            part = (r.width * r.height) / surface_page
            if part > COUVERTURE_MAXIMALE or part < SURFACE_MINIMALE:
                continue
            boites.append((r.x0 * echelle, r.y0 * echelle,
                           r.x1 * echelle, r.y1 * echelle))
    return boites


def boites_logos_dessines(image, lecture) -> list[tuple[float, float, float, float]]:
    """Logos peints dans l'image de la page, en pixels de cette image.

    Sur un scan, le logo de l'hopital fait partie de l'image de la page :
    `boites_logos` ne le voit pas, et l'OCR n'en lit rien ou presque. On le
    cherche donc dans les pixels du haut et du bas de la page, sous deux
    formes : une tache de couleur, ou un aplat sombre — le meme logo scanne en
    niveaux de gris. Le texte en est exclu par la taille (au moins
    `COTE_MINIMAL` hauteurs de mot de cote) et par les mots lus dedans ; les
    filets et l'ombre du bord du scan, par leur allongement.

    ``image`` doit etre rendue a la resolution de ``lecture``.
    """
    from PIL import Image, ImageChops

    largeur, hauteur = image.size
    if not largeur or not hauteur or not lecture.mots:
        return []
    hauteurs = sorted(m.y1 - m.y0 for m in lecture.mots)
    cote_min = COTE_MINIMAL * hauteurs[len(hauteurs) // 2]
    # Une case de grille par quart de hauteur de mot, environ 1 mm a 300 dpi.
    case = max(2, int(hauteurs[len(hauteurs) // 2] / 4))

    boites: list[tuple[float, float, float, float]] = []
    bandes = ((0, int(hauteur * HAUT_DE_PAGE)),
              (int(hauteur * (1 - BAS_DE_PAGE)), hauteur))
    for haut, bas in bandes:
        if bas - haut < case:
            continue
        bande = image.crop((0, haut, largeur, bas))
        _, saturation, valeur = bande.convert("HSV").split()
        couleur = ImageChops.multiply(
            saturation.point(lambda x: 255 if x > SATURATION_COULEUR else 0),
            valeur.point(lambda x: 255 if x > LUMINOSITE_COULEUR else 0))
        sombre = bande.convert("L").point(lambda x: 255 if x < GRIS_SOMBRE else 0)
        grille = (max(1, largeur // case), max(1, (bas - haut) // case))
        for masque, seuil, sombre_seulement in ((couleur, CASE_COULEUR, False),
                                                (sombre, CASE_SOMBRE, True)):
            parts = masque.resize(grille, Image.BOX)
            for gx0, gy0, gx1, gy1, cases in _taches(parts, seuil * 255):
                x0, x1 = gx0 * case, min(largeur, gx1 * case)
                y0, y1 = haut + gy0 * case, min(hauteur, haut + gy1 * case)
                cote, long = sorted((x1 - x0, y1 - y0))
                if cote < cote_min or long > ALLONGEMENT_MAXIMAL * cote:
                    continue
                # Touche le bord de la feuille : l'ombre ou la pince du
                # scanner, un repere d'impression, pas un logo.
                if x0 <= case or y0 <= case or x1 >= largeur - case \
                        or y1 >= hauteur - case:
                    continue
                surface = (x1 - x0) * (y1 - y0)
                if sombre_seulement \
                        and cases * case * case / surface < REMPLISSAGE_SOMBRE:
                    continue
                boite = (float(x0), float(y0), float(x1), float(y1))
                # Seuls les mots bien lus font du texte. Le symbole d'un
                # assureur, lu « (ii) » a 25 %, passait pour du texte en
                # couleur : il n'etait pas un logo, et le mot-symbole
                # « Allianz », pose contre lui, restait lisible.
                texte = sum(_recouvrement_absolu(m, boite) for m in lecture.mots
                            if m.confiance >= CONFIANCE_LUE)
                if texte / surface > PART_TEXTE_MAXIMALE:
                    continue
                boites.append(boite)
    return _fusionner(boites)


def boites_encre_non_lue(image, lecture) -> list[tuple[float, float, float, float]]:
    """L'encre du haut et du bas de la page que l'OCR n'a pas su lire.

    Un logo en noir et blanc, trame ou fait de traits, n'est ni une tache de
    couleur ni un aplat : `boites_logos_dessines` le manquait, et « CHUV » ou
    la croix d'un laboratoire restaient en tete du document. Le nom du patient
    ecrit a la main en haut d'un formulaire passait de meme : l'OCR n'en lit
    rien, la detection n'a donc rien a masquer. On retire de l'image l'encre
    des mots bien lus ; ce qui reste, en haut (`HAUT_ENCRE`) et en bas
    (`BAS_DE_PAGE`) de la page, est masque. Les filets, trop allonges, et les
    cadres vides, trop peu encres, sont epargnes.

    ``image`` doit etre rendue a la resolution de ``lecture``.
    """
    largeur, hauteur = image.size
    if not largeur or not hauteur or not lecture.mots:
        return []
    encre, mot = _encre_inexpliquee(image, lecture)

    boites: list[tuple[float, float, float, float]] = []
    for haut, bas in ((0, int(hauteur * HAUT_ENCRE)),
                      (int(hauteur * (1 - BAS_DE_PAGE)), hauteur)):
        boites += _taches_d_encre(encre, (0, haut, largeur, bas), mot)
    return _fusionner(boites)


def boites_timbres(image, lecture) -> list[tuple[float, float, float, float]]:
    """L'encre non lue de la case « Timbre et signature » d'un formulaire.

    Un timbre de medecin pale et pose de travers, au milieu d'un formulaire
    de garantie de paiement, n'etait lu par l'OCR qu'a moitie : l'adresse et
    le telephone etaient masques, le nom du medecin restait lisible. Il est
    hors des bandes de `boites_encre_non_lue`, ou l'encre du milieu de page
    est une coche ou un dessin. Ici, le libelle dit ce que l'encre voisine
    est : on masque, a droite et sous le libelle, l'encre que l'OCR n'explique
    pas, et les taches proches d'un seul tenant, pour que les lignes palies
    entre elles ne restent pas lisibles.

    ``image`` doit etre rendue a la resolution de ``lecture``.
    """
    largeur, hauteur = image.size
    if not largeur or not hauteur or not lecture.mots:
        return []
    libelles = [m for m in lecture.mots if m.confiance >= CONFIANCE_LUE
                and LIBELLE_TIMBRE.match(m.texte)]
    if not libelles:
        return []
    encre, mot = _encre_inexpliquee(image, lecture)
    demi = ECART_TIMBRE * mot / 2
    # Une case de grille de marge : le bord adouci d'une lettre deborde la
    # case ou elle a ete comptee.
    retrait = demi - max(2, int(mot / 4))

    boites: list[tuple[float, float, float, float]] = []
    for m in libelles:
        zone = (max(0, int(m.x0)), max(0, int(m.y0 - AU_DESSUS_TIMBRE * mot)),
                largeur, min(hauteur, int(m.y0 + HAUTEUR_TIMBRE * hauteur)))
        taches = _taches_d_encre(encre, zone, mot)
        # Dilater, fusionner puis retracter : les taches a moins de
        # `ECART_TIMBRE` les unes des autres ne font plus qu'une boite.
        for x0, y0, x1, y1 in _fusionner([(x0 - demi, y0 - demi,
                                           x1 + demi, y1 + demi)
                                          for x0, y0, x1, y1 in taches]):
            boites.append((max(0.0, x0 + retrait), max(0.0, y0 + retrait),
                           min(float(largeur), x1 - retrait),
                           min(float(hauteur), y1 - retrait)))
    return _fusionner(boites)


def _encre_inexpliquee(image, lecture):
    """L'encre de la page moins celle des mots bien lus, et la hauteur de mot."""
    from PIL import ImageDraw

    hauteurs = sorted(m.y1 - m.y0 for m in lecture.mots)
    mot = hauteurs[len(hauteurs) // 2]
    marge = mot * 0.15

    encre = image.convert("L").point(lambda x: 255 if x < GRIS_ENCRE else 0)
    dessin = ImageDraw.Draw(encre)
    for m in lecture.mots:
        if m.confiance >= CONFIANCE_LUE:
            dessin.rectangle([m.x0 - marge, m.y0 - marge,
                              m.x1 + marge, m.y1 + marge], fill=0)
    return encre, mot


def _taches_d_encre(encre, zone, mot) -> list[tuple[float, float, float, float]]:
    """Les taches d'encre de ``zone`` qui ne sont ni un filet, ni un cadre
    vide, ni le bord du scan."""
    from PIL import Image

    largeur, hauteur = encre.size
    case = max(2, int(mot / 4))
    gauche, haut, droite, bas = zone
    if bas - haut < case or droite - gauche < case:
        return []
    bande = encre.crop(zone)
    grille = (max(1, (droite - gauche) // case), max(1, (bas - haut) // case))
    parts = bande.resize(grille, Image.BOX)

    boites: list[tuple[float, float, float, float]] = []
    for gx0, gy0, gx1, gy1, cases in _taches(parts, CASE_ENCRE * 255):
        x0, x1 = gauche + gx0 * case, min(largeur, gauche + gx1 * case)
        y0, y1 = haut + gy0 * case, min(hauteur, haut + gy1 * case)
        cote, long = sorted((x1 - x0, y1 - y0))
        if cote < COTE_ENCRE * mot or long > ALLONGEMENT_MAXIMAL * cote:
            continue
        if x0 <= case or y0 <= case or x1 >= largeur - case \
                or y1 >= hauteur - case:
            continue                # bord du scan
        if cases * case * case / ((x1 - x0) * (y1 - y0)) < REMPLISSAGE_ENCRE:
            continue                # cadre vide
        boites.append((float(x0), float(y0), float(x1), float(y1)))
    return boites


def _fusionner(boites):
    """Reunit les boites qui se chevauchent.

    Un logo bleu fonce est a la fois une tache de couleur et un aplat sombre :
    sans fusion, il compterait deux fois.
    """
    restantes = list(boites)
    fusionnees: list[tuple[float, float, float, float]] = []
    while restantes:
        x0, y0, x1, y1 = restantes.pop()
        change = True
        while change:
            change = False
            for autre in list(restantes):
                if autre[0] < x1 and x0 < autre[2] and autre[1] < y1 and y0 < autre[3]:
                    restantes.remove(autre)
                    x0, y0 = min(x0, autre[0]), min(y0, autre[1])
                    x1, y1 = max(x1, autre[2]), max(y1, autre[3])
                    change = True
        fusionnees.append((x0, y0, x1, y1))
    return fusionnees


def _taches(parts, seuil: float):
    """Groupes de cases voisines dont la part d'encre depasse ``seuil``.

    Deux cases separees d'une case vide restent dans la meme tache : les
    lettres d'un logo, ou son cadre et son contenu, ne forment qu'un tout.
    Rend, pour chaque tache, son rectangle en cases et son nombre de cases.
    """
    largeur, hauteur = parts.size
    px = parts.load()
    pleines = {(x, y) for y in range(hauteur) for x in range(largeur)
               if px[x, y] > seuil}
    vues: set[tuple[int, int]] = set()
    for depart in pleines:
        if depart in vues:
            continue
        vues.add(depart)
        pile, membres = [depart], []
        while pile:
            x, y = pile.pop()
            membres.append((x, y))
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-2, -1, 0, 1, 2):
                    voisine = (x + dx, y + dy)
                    if voisine in pleines and voisine not in vues:
                        vues.add(voisine)
                        pile.append(voisine)
        xs = [m[0] for m in membres]
        ys = [m[1] for m in membres]
        yield min(xs), min(ys), max(xs) + 1, max(ys) + 1, len(membres)


def _recouvrement_absolu(mot, boite) -> float:
    """Surface du mot qui tombe dans la boite."""
    largeur = min(mot.x1, boite[2]) - max(mot.x0, boite[0])
    hauteur = min(mot.y1, boite[3]) - max(mot.y0, boite[1])
    return largeur * hauteur if largeur > 0 and hauteur > 0 else 0.0


def _paragraphes(lecture) -> list[list[int]]:
    """Les mots de la page, groupes par bloc puis coupes aux sauts de paragraphe.

    Les indices rendus sont ceux de ``lecture.mots``, dans l'ordre de lecture.

    Le « bloc » d'un PDF n'est pas un paragraphe : un traitement de texte range
    parfois la date, la reference et tout le corps d'une lettre dans un seul
    bloc, dont le rectangle englobant couvre la moitie de la page. Masquer ce
    rectangle effacait la lettre, texte compris. On coupe donc chaque bloc
    la ou l'espace entre deux lignes depasse ``SAUT_PARAGRAPHE`` hauteurs de
    mot — une ligne blanche, pas l'interligne d'une adresse.
    """
    par_bloc: dict[int, list[int]] = {}
    for i, m in enumerate(lecture.mots):
        par_bloc.setdefault(m.bloc, []).append(i)

    paragraphes: list[list[int]] = []
    for indices in par_bloc.values():
        mots = lecture.mots
        hauteurs = sorted(mots[i].y1 - mots[i].y0 for i in indices)
        seuil = SAUT_PARAGRAPHE * hauteurs[len(hauteurs) // 2]
        courant: list[int] = []
        bas = 0.0
        for i in sorted(indices, key=lambda i: mots[i].y0):
            if courant and mots[i].y0 - bas > seuil:
                paragraphes.append(sorted(courant))
                courant, bas = [], 0.0
            courant.append(i)
            bas = max(bas, mots[i].y1)
        paragraphes.append(sorted(courant))
    return paragraphes


#: Marqueurs d'un bloc d'adresse ou d'en-tete professionnel, cherches comme
#: mots entiers et sans egard a la casse. Cherches comme sous-chaines, ils
#: trouvaient « tel » dans « hôtel » ou « tellement ».
INDICES_ENTETE = re.compile(
    r"\b(?:case postale|cp|tél|fax|e-?mail|fmh|dre?|docteur"
    r"|rue|chemin|route|avenue|monsieur|madame)\b",
    re.IGNORECASE)

#: Noms d'etablissement, qui sont aussi des mots du compte rendu : « examen
#: clinique », « renseignements cliniques », « bilan de laboratoire », « vu au
#: cabinet », « un tel tableau ». Seule la majuscule les distingue : « Clinique
#: du Verger », « Tel 032… ». Un titre de rubrique en capitales (« EXAMEN
#: CLINIQUE ») ne compte pas ; un nom en capitales ne compte qu'en tete de bloc
#: (« CLINIQUE DES GLYCINES »). Sans cela, la rubrique « Renseignements
#: cliniques » passait pour un en-tete et disparaissait avec sa premiere ligne.
ETABLISSEMENTS = (r"(?:Cabinet|Centre|Clinique|H[ôo]pital|Laboratoire"
                  r"|Sp[ée]cialiste|Tel)")
INDICES_MAJUSCULE = re.compile(rf"\b{ETABLISSEMENTS}\b")
DEBUT_CAPITALES = re.compile(rf"^{ETABLISSEMENTS.upper()}\b")

#: Debuts de bloc qui nomment l'etablissement ou son service. Cherches en tete
#: du bloc seulement : « service de » au milieu d'une phrase est du texte, au
#: debut d'une ligne d'en-tete c'est une adresse institutionnelle.
DEBUTS_ENTETE = (
    "département", "departement", "service ", "réseau",
    "reseau", "institut", "centre hospitalier", "unité de ", "unite de ",
    "policlinique", "hospitalier",
)


def _porte_un_indice(texte: str) -> bool:
    """Le texte d'un bloc porte-t-il un marqueur d'adresse ou d'en-tete ?"""
    return bool(INDICES_ENTETE.search(texte)
                or INDICES_MAJUSCULE.search(texte)
                or DEBUT_CAPITALES.match(texte)
                or texte.lower().startswith(DEBUTS_ENTETE))


#: Lignes du bloc patient qui decrivent l'examen, pas l'identite : « Examen du
#: 5 avril 2014 », « Date de l'examen : … ». Le bloc les entrainait avec lui,
#: et le compte rendu perdait la date de l'examen. Elles restent lisibles ; la
#: date qu'elles portent suit le reglage des dates, comme partout ailleurs.
LIGNE_LISIBLE = re.compile(
    r"(?:date\s+(?:de\s+l|d)['’]\s*examen"
    r"|examen\s+(?:du|le|réalisé|effectué|pratiqué)\b)",
    re.IGNORECASE)


#: Ce qui distingue un paragraphe redige d'un bloc d'adresse : assez de mots,
#: des lignes longues, et surtout des minuscules. Une adresse aligne des noms
#: propres sur des lignes courtes — « Dre Marie Tartempion / Rue du Moulin 12 /
#: 1040 Echallens » ; une phrase, des mots ordinaires sur toute la largeur.
PROSE_MOTS_MIN = 10
PROSE_MOTS_PAR_LIGNE = 7
PROSE_MINUSCULES = 0.6


def _est_de_la_prose(lecture, indices: list[int]) -> bool:
    """Le paragraphe est-il du texte redige plutot qu'un bloc d'adresse ?

    Sur une lettre courte, le corps commence dans le haut de la page. Il
    porte le nom du patient et un « Monsieur » : il passait pour un bloc
    destinataire, et disparaissait en entier — « J'ai vu en consultation
    votre patient… » devenait « [PERSONNE_1] ». Ses noms et ses dates restent
    masques un par un, comme dans le reste du document.
    """
    mots = [lecture.mots[i].texte for i in indices]
    alpha = [m for m in mots if any(c.isalpha() for c in m)]
    if len(alpha) < PROSE_MOTS_MIN:
        return False
    if len(mots) / len(_lignes(lecture, indices)) < PROSE_MOTS_PAR_LIGNE:
        return False
    minuscules = sum(next(c for c in m if c.isalpha()).islower() for m in alpha)
    return minuscules / len(alpha) >= PROSE_MINUSCULES


def _colle_a_un_logo(rect, logos, ecart: float) -> bool:
    """Le bloc est-il pose sur la meme ligne qu'un logo, tout contre lui ?

    Un en-tete d'etablissement associe presque toujours un logo et le nom du
    departement ou du service, compose a cote. Le logo est noirci ; son
    voisin de texte l'etait rarement, et le nom de l'hopital restait lisible.
    Le test porte sur le milieu du bloc, pour ne pas retenir le titre place
    sous le logo.
    """
    milieu = (rect[1] + rect[3]) / 2
    for x0, y0, x1, y1 in logos:
        if y0 <= milieu <= y1 and rect[0] < x1 + ecart and rect[2] > x0:
            return True
    return False


def boites_entetes(lecture, spans, hauteur_page: int,
                   haut: float = HAUT_DE_PAGE,
                   logos=()) -> list[tuple[float, float, float, float]]:
    """Blocs du haut de page qui portent une identite d'expediteur ou de destinataire.

    Un bloc est retenu s'il se situe dans le haut de la page et s'il contient
    soit une entite deja detectee, soit un marqueur d'adresse, soit s'il est
    pose contre un logo (``logos``, en pixels). On masque alors le bloc entier
    plutot que les seuls mots reconnus : c'est ce qui emporte aussi la
    specialite, la mention « Case postale » ou le titre professionnel, qui
    identifient l'expediteur sans etre des donnees personnelles. Seules les
    lignes qui decrivent l'examen (``LIGNE_LISIBLE``) en sont exceptees, et
    les paragraphes rediges (`_est_de_la_prose`) ne sont jamais retenus.
    """
    limite = hauteur_page * haut

    # Mots touches par une entite detectee. Une date n'en fait pas un
    # en-tete : elle ne designe ni expediteur ni destinataire. Dans un
    # tableau date — une liste de diagnostics, un historique —, chaque
    # ligne du haut de page portait sa date et disparaissait en entier ;
    # la date, elle, reste masquee mot a mot comme ailleurs.
    #
    # Une entite ne compte que si l'OCR a lu au moins un de ses mots avec
    # assurance. L'ecriture manuscrite lue « Tel 1 » passait pour un numero
    # de telephone, et le titre du formulaire, a cote, disparaissait. Ses
    # mots restent masques un par un.
    touches: set[int] = set()
    for sp in spans:
        if est_date(sp.label):
            continue
        mots = [i for i, (d, f) in enumerate(lecture.offsets)
                if d < sp.end and sp.start < f]
        if any(_bien_lu(lecture.mots[i]) for i in mots):
            touches.update(mots)

    ecart = ECART_LOGO * getattr(lecture, "dpi", 72) / 72.0

    retenus: list[tuple[float, float, float, float]] = []
    for indices in _paragraphes(lecture):
        # Seuls les mots du haut de la page comptent : un paragraphe qui
        # deborde plus bas ne doit pas entrainer le texte qui le suit.
        haut_de_page = [i for i in indices if lecture.mots[i].y0 <= limite]
        if not haut_de_page:
            continue  # trop bas dans la page
        # Les lignes a laisser lisibles coupent le bloc en morceaux, masques
        # chacun dans son propre rectangle.
        morceaux: list[list[int]] = [[]]
        for ligne in _lignes(lecture, haut_de_page):
            if LIGNE_LISIBLE.match(_texte(lecture, ligne)):
                morceaux.append([])
            else:
                morceaux[-1] += ligne
        morceaux = [m for m in morceaux if m]
        if not morceaux:
            continue
        masquables = [i for m in morceaux for i in m]
        if _est_de_la_prose(lecture, masquables):
            continue  # le corps de la lettre, pas son en-tete
        # Un mot devine par l'OCR ne designe rien : l'ecriture manuscrite
        # lue « Tel » faisait d'un titre de formulaire un en-tete.
        contenu = _texte(lecture, [i for i in masquables
                                   if lecture.mots[i].confiance >= CONFIANCE_LUE])
        if (touches.intersection(masquables)
                or _porte_un_indice(contenu)
                or _colle_a_un_logo(_rect(lecture, masquables), logos, ecart)):
            retenus += [_rect(lecture, m) for m in morceaux]
    return retenus


def _bien_lu(mot) -> bool:
    """L'OCR a-t-il lu ce mot, plutot que devine ?"""
    return (mot.confiance >= CONFIANCE_LUE
            and sum(c.isalnum() for c in mot.texte) >= 2)


def _lignes(lecture, indices: list[int]) -> list[list[int]]:
    """Les indices, regroupes par ligne : une suite de mots consecutifs qui
    partagent bloc et ligne."""
    lignes: list[list[int]] = []
    for i in indices:
        m = lecture.mots[i]
        if lignes:
            p = lecture.mots[lignes[-1][-1]]
            if lignes[-1][-1] == i - 1 and (p.bloc, p.ligne) == (m.bloc, m.ligne):
                lignes[-1].append(i)
                continue
        lignes.append([i])
    return lignes


def _texte(lecture, indices: list[int]) -> str:
    return " ".join(lecture.mots[i].texte for i in indices)


def _rect(lecture, indices: list[int]) -> tuple[float, float, float, float]:
    mots = [lecture.mots[i] for i in indices]
    return (min(m.x0 for m in mots), min(m.y0 for m in mots),
            max(m.x1 for m in mots), max(m.y1 for m in mots))


def boite_bande(largeur: int, hauteur: int, fraction: float):
    """Bandeau superieur, en pixels."""
    if fraction <= 0:
        return []
    return [(0.0, 0.0, float(largeur), hauteur * min(fraction, 1.0))]


#: Genres de zones, tels que la relecture les nomme.
GENRE_LOGO = "logo"
GENRE_ENTETE = "entete"
GENRE_BANDE = "bande"


def zones_supplementaires(page, lecture, spans, options: OptionsZones,
                          dpi: int, premiere: bool, image=None
                          ) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Les zones optionnelles de cette page, chacune avec son genre.

    ``image`` est la page rendue a ``dpi`` ; sans elle, les logos peints dans
    un scan passent inapercus. Le genre (logo, bloc d'en-tete, bandeau) permet
    a la relecture de dire a l'utilisateur ce qu'il est en train de juger.
    """
    if not options.actif:
        return []
    applicable = premiere or not options.premiere_page_seulement

    zones: list[tuple[str, tuple[float, float, float, float]]] = []
    logos = boites_logos(page, dpi)
    if image is not None:
        logos += boites_logos_dessines(image, lecture)
    if options.logos:
        # L'encre non lue n'est pas un logo au sens de `boites_entetes` : une
        # date manuscrite n'entraine pas le texte pose a cote. Un logo trouve
        # des deux facons ne compte qu'une fois. Un timbre est, comme un logo,
        # une image que l'option retire (« logos, tampons, signatures »).
        encre = (boites_encre_non_lue(image, lecture)
                 + boites_timbres(image, lecture)) if image is not None else []
        zones += [(GENRE_LOGO, b) for b in _fusionner(logos + encre)]
    if options.entetes:
        haut = HAUT_DE_PAGE if applicable else HAUT_PAGES_SUIVANTES
        zones += [(GENRE_ENTETE, b) for b in boites_entetes(
            lecture, spans, lecture.hauteur, haut=haut, logos=logos)]
    if options.bande_haute > 0 and applicable:
        zones += [(GENRE_BANDE, b) for b in
                  boite_bande(lecture.largeur, lecture.hauteur, options.bande_haute)]
    return zones


def boites_supplementaires(page, lecture, spans, options: OptionsZones,
                           dpi: int, premiere: bool,
                           image=None) -> list[tuple[float, float, float, float]]:
    """Assemble toutes les zones optionnelles a masquer sur cette page."""
    return [b for _, b in zones_supplementaires(
        page, lecture, spans, options, dpi, premiere, image=image)]


def _recouvrement(mot, boite) -> float:
    """Part de la surface du mot qui tombe dans la boite."""
    largeur = min(mot.x1, boite[2]) - max(mot.x0, boite[0])
    hauteur = min(mot.y1, boite[3]) - max(mot.y0, boite[1])
    surface = (mot.x1 - mot.x0) * (mot.y1 - mot.y0)
    if largeur <= 0 or hauteur <= 0 or surface <= 0:
        return 0.0
    return largeur * hauteur / surface


def spans_des_zones(lecture, boites) -> list[Span]:
    """Le texte que ces zones recouvrent, a masquer dans la sortie texte.

    Noircir un logo ou un bloc d'en-tete ne retire que des pixels. Le texte
    qu'on en tire — celui de la couche du PDF, ou celui que l'OCR lit dans le
    logo — resterait en clair dans le fichier ``.txt`` : le nom de l'hopital
    disparaissait du PDF et subsistait dans le texte. Les deux sorties doivent
    dire la meme chose.

    Chaque zone donne un span par suite continue de mots, de sorte qu'un logo
    se reduit a un seul pseudonyme plutot qu'a un par mot. Le score est nul :
    quand une detection recoupe la zone, c'est son etiquette qui l'emporte a la
    fusion, celle d'« employeur » n'etant qu'un defaut.
    """
    spans: list[Span] = []
    for boite in boites:
        suite: list[int] = []
        for i, mot in enumerate(lecture.mots):
            if _recouvrement(mot, boite) >= RECOUVREMENT_MOT:
                suite.append(i)
                continue
            if suite:
                spans.append(_span_de_mots(lecture, suite))
                suite = []
        if suite:
            spans.append(_span_de_mots(lecture, suite))
    return spans


def _span_de_mots(lecture, indices: list[int]) -> Span:
    debut = lecture.offsets[indices[0]][0]
    fin = lecture.offsets[indices[-1]][1]
    return Span(debut, fin, Categorie.EMPLOYEUR.value, 0.0,
                lecture.texte[debut:fin], Origine.ZONE.value)
