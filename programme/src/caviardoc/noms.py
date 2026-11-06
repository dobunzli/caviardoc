"""Anonymisation du nom de fichier lui-meme.

Un document parfaitement caviarde dont le fichier s'appelle
``5092_Jean-Bidule_Zorglub_Ordonnance.pdf`` n'est pas anonymise. Le nom
apparait dans les listings, les pieces jointes, les journaux de serveur et les
sauvegardes — souvent plus visible que le contenu.

Deux difficultes propres aux noms de fichiers :

1. Les separateurs. « Jean-Noel_Zorglub » ne correspond a aucune chaine du
   document, qui contient « Jean-Bidule Zorglub ». On detecte donc sur une
   copie ou les separateurs sont remplaces par des espaces. Comme pour la
   normalisation de casse, la substitution se fait **caractere pour
   caractere** : la longueur est preservee et les offsets restent valides.

2. La coherence. Le nom de fichier doit recevoir le meme pseudonyme que le
   contenu, sans quoi le lien entre les deux est rompu. La fonction accepte
   donc la table de correspondance deja construite pour le document.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import unicodedata
from pathlib import Path

from .engine import est_personne

#: Separateurs usuels des noms de fichiers, remplaces par une espace pour la
#: detection. Tous font un seul caractere : la longueur est preservee.
SEPARATEURS = "_-."


def pour_detection(nom: str) -> str:
    """Copie du nom ou les separateurs deviennent des espaces, meme longueur."""
    return "".join(" " if c in SEPARATEURS else c for c in nom)


def _cle(s: str) -> str:
    """Forme canonique servant a rapprocher deux ecritures d'une meme entite."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


#: Voyelles inflechies, transcrites comme on les ecrit sans clavier allemand.
_TREMAS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue",
                         "Ä": "AE", "Ö": "OE", "Ü": "UE"})


def _cle_transcrite(s: str) -> str:
    """Comme `_cle`, mais « ü » vaut « ue » : « Tartempünz » -> « tartempuenz ».

    Un meme nom s'ecrit « Tartempünz » dans le nom du fichier et « TARTEMPUENZ » dans
    un en-tete en capitales, ou encore « Tartempunz ». `_cle` seule ramene le
    premier a « tartempunz » et le second a « tartempuenz » : le nom du patient
    restait alors dans le nom du fichier produit.
    """
    return _cle(unicodedata.normalize("NFC", s).translate(_TREMAS))


def formes_canoniques(s: str) -> list[str]:
    """Les formes canoniques de ``s`` : trema retire, puis trema transcrit.

    Deux ecritures designent la meme entite des qu'elles partagent l'une de
    ces formes : « Tartempünz » rejoint « Tartempunz » par la premiere, « TARTEMPUENZ »
    par la seconde.
    """
    return [c for c in dict.fromkeys((_cle(s), _cle_transcrite(s))) if c]


#: Longueur minimale de la forme canonique d'une entite pour etre cherchee dans
#: un nom de fichier. En deca, les collisions sont massives : « Ele », « Pre »
#: ou « Rue » se retrouvent au milieu de « Electroneuromyographie »,
#: « Prescription » ou « Ruz », et mutilent le nom au lieu de l'anonymiser.
LONGUEUR_MIN = 5

#: Seuil des noms deja trouves comme personnes dans le document lui-meme :
#: « Wu » dans « rapport_Wu.pdf » est le patient, pas un mot courant. Les
#: frontieres de mot restent exigees, donc « wu » dans « Wuhan » est epargne.
LONGUEUR_MIN_PERSONNE = 2


def anonymiser_nom(nom_fichier: str, correspondances: dict[str, str],
                   longueur_min: int = LONGUEUR_MIN,
                   personnes: frozenset[str] | set[str] = frozenset()) -> str:
    """Remplace dans ``nom_fichier`` toute entite connue de ``correspondances``.

    ``correspondances`` associe le texte original a son pseudonyme, par exemple
    ``{"Jean-Noel Zorglub": "PERSONNE_1"}``. Le rapprochement ignore la
    casse, les accents et les separateurs, de sorte que « Jean-Bidule_Zorglub »,
    « jean-noel zorglub » et « JEAN-NOEL.ZORGLUB » sont tous reconnus.

    Toutes les correspondances sont d'abord reperees sur le nom **d'origine**,
    puis substituees en une seule passe, de droite a gauche. Proceder par
    remplacements successifs, comme le faisait la premiere version, conduit a
    reinjecter des pseudonymes les uns dans les autres : « adresse postale »
    devenait « ADRPERSONNE_13E_POSTALE_2 ».

    ``personnes`` : les cles de ``correspondances`` qui designent une personne
    trouvee dans le document. Pour celles-la seulement, le seuil de longueur
    tombe a `LONGUEUR_MIN_PERSONNE`, pour le nom entier comme pour chacun de
    ses mots.

    L'extension est preservee telle quelle.
    """
    chemin = Path(nom_fichier)
    return _anonymiser(chemin.stem, correspondances, longueur_min,
                       personnes) + chemin.suffix


def anonymiser_dossier(nom: str, correspondances: dict[str, str],
                       longueur_min: int = LONGUEUR_MIN,
                       personnes: frozenset[str] | set[str] = frozenset()) -> str:
    """Comme `anonymiser_nom`, pour un nom de dossier.

    Un dossier n'a pas d'extension : « Dupont.Jean » est un nom entier, pas
    un fichier « Dupont » d'extension « .Jean » qu'il faudrait epargner.
    """
    return _anonymiser(nom, correspondances, longueur_min, personnes)


def _anonymiser(tige: str, correspondances: dict[str, str],
                longueur_min: int,
                personnes: frozenset[str] | set[str] = frozenset()) -> str:
    # Un nom de fichier venu du Mac est souvent decompose : « u » suivi d'un
    # trema combinant, que la lecture caractere par caractere ne sait pas lire.
    texte = unicodedata.normalize("NFC", tige)
    trouvailles: list[tuple[int, int, str]] = []
    for original, pseudonyme in correspondances.items():
        if original in personnes:
            seuil = min(longueur_min, LONGUEUR_MIN_PERSONNE)
            # Chaque mot du nom : « Wu_Li » pour la personne « Li Wu ».
            sources = [original] + [
                m.strip(".,;:()") for m in sans_titres(original)]
        else:
            seuil, sources = longueur_min, [original]
        for source in sources:
            for canon in (_cle, _cle_transcrite):
                cible = canon(source)
                if len(cible) < seuil:
                    continue
                trouvailles.extend((d, f, pseudonyme)
                                   for d, f in _occurrences(texte, cible, canon,
                                                            original in personnes))

    if not trouvailles:
        return tige

    # La plus longue d'abord, puis la plus a gauche : « Jean-Bidule Zorglub »
    # doit primer sur « Zorglub » seul.
    retenues: list[tuple[int, int, str]] = []
    for d, f, pseudo in sorted(trouvailles, key=lambda t: (-(t[1] - t[0]), t[0])):
        if not any(d < rf and rd < f for rd, rf, _ in retenues):
            retenues.append((d, f, pseudo))

    resultat = texte
    for d, f, pseudo in sorted(retenues, key=lambda t: -t[0]):
        # Un chiffre voisin (frontiere reservee aux personnes) est separe du
        # pseudonyme : « PERSONNE_1_2024 », pas « PERSONNE_12024 ».
        avant = "_" if d > 0 and texte[d - 1].isdigit() else ""
        apres = "_" if f < len(texte) and texte[f].isdigit() else ""
        resultat = resultat[:d] + avant + pseudo + apres + resultat[f:]
    return _compacter(resultat)


def _compacter(tige: str) -> str:
    """Reduit « PERSONNE_1_PERSONNE_1 » a « PERSONNE_1 ».

    Le prenom et le patronyme d'une meme personne recoivent le meme pseudonyme.
    Les voir se suivre est correct mais inutilement verbeux.
    """
    return re.sub(r"([A-Z]+(?:_[A-Z]+)*_\d+)(?:[ _.-]+\1)+", r"\1", tige)


def _frontiere(texte: str, i: int, chiffres: bool = False) -> bool:
    """Vrai si la position ``i`` est un debut ou une fin de mot.

    ``chiffres`` : un chiffre voisin compte aussi comme frontiere. Reserve aux
    personnes : un nom ne contient pas de chiffre, donc « X2024 » ou « 12X »
    nomment bien « X », sans risque de coupure au milieu d'un mot.
    """
    if i <= 0 or i >= len(texte):
        return True
    if chiffres and (texte[i - 1].isdigit() or texte[i].isdigit()):
        return True
    return not _cle(texte[i - 1]) or not _cle(texte[i])


def _occurrences(texte: str, cle_cible: str,
                 canon=_cle, chiffres: bool = False) -> list[tuple[int, int]]:
    """Positions ou la forme canonique du texte, selon ``canon``, vaut ``cle_cible``.

    La correspondance doit commencer et finir sur une frontiere de mot : sans
    cela « Rue » se reconnait dans « Rueyres », et « Ele » au milieu
    de « Electroneuromyographie ».
    """
    trouvees: list[tuple[int, int]] = []
    n = len(texte)
    for i in range(n):
        if not canon(texte[i]) or not _frontiere(texte, i, chiffres):
            continue
        cle = ""
        for j in range(i, n):
            c = canon(texte[j])
            if not c and texte[j] not in SEPARATEURS and not texte[j].isspace():
                break
            cle += c
            if cle == cle_cible and c and _frontiere(texte, j + 1, chiffres):
                trouvees.append((i, j + 1))
                break
            if not cle_cible.startswith(cle):
                break
    return trouvees


#: Civilites et titres qui precedent frequemment un nom dans un document
#: medical, mais jamais dans un nom de fichier.
TITRES = {
    "m", "mr", "mme", "mlle", "monsieur", "madame", "mademoiselle",
    "dr", "dre", "dresse", "docteur", "doctoresse", "pr", "prof", "professeur",
    "med", "méd",
}

#: Formules d'appel, qui precedent un prenom sans en faire partie : « Salut
#: Paul-Machin, » ne doit pas devenir un nom de deux mots, ni « Cher
#: Dominique » un certain « Cher ». Sans accents.
SALUTATIONS = {"salut", "bonjour", "bonsoir", "coucou", "hello", "cher",
               "chere", "merci"}


def _en_tete_de_nom(mot: str) -> bool:
    """Titre ou formule d'appel : un mot qui precede un nom sans en etre."""
    return mot.rstrip(".").lower() in TITRES or _cle(mot) in SALUTATIONS


def sans_titres(texte: str) -> list[str]:
    """Les mots d'un nom, civilites et titres de tete retires.

    « M. Jean-Bidule Zorglub » -> ["Jean-Bidule", "Zorglub"].
    """
    mots = [m for m in re.split(r"[\s,]+", texte.strip()) if m]
    i = 0
    while i < len(mots) and _en_tete_de_nom(mots[i]):
        i += 1
    return mots[i:] if i < len(mots) else []


def composants_du_nom(texte: str) -> list[str]:
    """Prenoms et patronymes d'un nom de personne, sans titre ni initiale."""
    noyaux = (m.strip(".,;:()").strip() for m in sans_titres(texte))
    return [n for n in noyaux
            if len(n) >= 3 and not _en_tete_de_nom(n)]


def variantes(texte: str, etiquette: str = "") -> list[str]:
    """Formes sous lesquelles une entite peut apparaitre dans un nom de fichier.

    Une detection rend « M. Jean-Bidule Zorglub », alors que le fichier
    s'appelle « Jean-Bidule_Zorglub_Ordonnance.pdf ». Sans cette normalisation,
    le rapprochement echoue et le nom de fichier continue de nommer le patient.

    Pour une personne, chaque composant du nom est egalement enregistre : les
    fichiers ne reprennent souvent que le patronyme, ou l'inversent.
    """
    formes: list[str] = []
    brut = texte.strip()
    if brut:
        formes.append(brut)

    mots = [m for m in re.split(r"[\s,]+", brut) if m]
    sans_titre = sans_titres(brut)
    if sans_titre and len(sans_titre) < len(mots):
        formes.append(" ".join(sans_titre))

    if est_personne(etiquette):
        formes += composants_du_nom(brut)

    # Dedoublonner en conservant l'ordre, du plus long au plus court.
    vues: set[str] = set()
    uniques: list[str] = []
    for f in sorted(formes, key=len, reverse=True):
        cle = _cle(f)
        if cle and cle not in vues:
            vues.add(cle)
            uniques.append(f)
    return uniques


#: Pauses entre les tentatives de renommage, en secondes.
_PAUSES_RENOMMAGE = (0.1, 0.25, 0.5, 1.0)


def diagnostic_renommage(source: Path, exc: OSError) -> str:
    """Ce qu'on peut dire d'un renommage refuse, sans nommer le fichier.

    macOS repond « Operation not permitted » a un renommage dans son propre
    dossier quand un autre processus tient le fichier (antivirus, indexation)
    ou quand un attribut le protege. Le message de l'exception ne dit rien de
    tout cela : on releve donc l'etat du fichier et qui le tient.
    """
    infos = [f"errno={exc.errno}"]
    try:
        st = source.stat()
        infos.append(f"mode={oct(st.st_mode & 0o7777)} flags={getattr(st, 'st_flags', 0):#x}"
                     f" liens={st.st_nlink} taille={st.st_size}")
        infos.append(f"age={time.time() - st.st_mtime:.1f}s")
    except OSError as e:
        infos.append(f"stat impossible ({e.errno})")
    infos.append(f"dossier_ecriture={os.access(source.parent, os.W_OK)}")
    listxattr = getattr(os, "listxattr", None)
    if listxattr:
        try:
            infos.append("xattr=" + ",".join(listxattr(source)))
        except OSError:
            pass
    try:
        # -F c : un nom de commande par processus, jamais le chemin.
        sortie = subprocess.run(["lsof", "-F", "c", "--", str(source)],
                                capture_output=True, text=True, timeout=5).stdout
        tenants = sorted({l[1:] for l in sortie.splitlines() if l.startswith("c")})
        infos.append("tenu_par=" + (",".join(tenants) or "personne"))
    except (OSError, subprocess.SubprocessError):
        pass
    return " ".join(infos)


def nom_libre(voulu: Path, compagnons: tuple[str, ...] = ()) -> Path:
    """``voulu``, ou « nom (2).ext », « nom (3).ext »… s'il est deja pris.

    Deux fichiers d'origine distincts peuvent recevoir le meme nom anonymise —
    « Dupont.pdf » et « Jean Dupont.pdf » deviennent tous deux
    « PATIENT_1.pdf » —, et le dossier de sortie peut garder celui d'une
    execution precedente. Sous macOS le renommage ecrasait l'autre document
    sans rien dire ; sous Windows il levait FileExistsError, une fois tout le
    lot traite.

    ``compagnons`` : extensions des fichiers ecrits a cote du document, qui
    doivent etre libres eux aussi. « Lettre.pdf » et « Lettre.docx » du meme
    lot ecrivaient tous deux « Lettre.txt », et le second ecrasait le premier.
    """
    def pris(chemin: Path) -> bool:
        return chemin.exists() or any(chemin.with_suffix(s).exists()
                                      for s in compagnons)

    candidat, rang = voulu, 2
    while pris(candidat):
        candidat = voulu.with_name(f"{voulu.stem} ({rang}){voulu.suffix}")
        rang += 1
    return candidat


def renommer(source: Path, final: Path, journal: list[str] | None = None) -> Path:
    """Renomme un fichier produit, en reessayant si le systeme le refuse.

    Un refus passager (``PermissionError``) disparait en quelques centiemes de
    seconde quand un processus lache le fichier. S'il persiste, on copie puis
    on supprime l'original : l'ancien nom, qui porte le nom du patient, ne doit
    pas survivre. Si la suppression echoue aussi, l'erreur d'origine remonte.

    Un nom deja pris n'est jamais ecrase (voir `nom_libre`) : rend le chemin
    effectivement donne.
    """
    final = nom_libre(final)
    derniere: OSError | None = None
    for essai, pause in enumerate((0.0,) + _PAUSES_RENOMMAGE):
        if pause:
            time.sleep(pause)
        try:
            source.rename(final)
        except PermissionError as exc:
            derniere = exc
            if journal is not None and essai == 0:
                journal.append("renommage refuse : "
                               + diagnostic_renommage(source, exc))
        else:
            if derniere is not None and journal is not None:
                journal.append(f"renommage reussi a l'essai {essai + 1}")
            return final
    try:
        shutil.copyfile(source, final)
        source.unlink()
    except OSError:
        final.unlink(missing_ok=True)
        raise derniere
    if journal is not None:
        journal.append("renommage impossible : copie puis suppression utilisees")
    return final
