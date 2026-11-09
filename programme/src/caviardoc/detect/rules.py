"""Couche 1 : identifiants structures, detectes par regle et valides par somme de controle.

Cette couche ne fait aucune inference. Elle ne trouve que ce qu'elle sait
reconnaitre, mais ce qu'elle trouve est certain — et le rappel est total sur les
formats couverts. Chaque detection est explicable en audit, ce qu'un modele
statistique ne permet pas.

La validation par somme de controle est essentielle : sans elle, une expression
reguliere sur treize chiffres attraperait tous les numeros de facture du
document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

# Import paresseux pour eviter une dependance circulaire avec engine.
Span = None  # renseigne a l'import depuis engine


@dataclass(frozen=True)
class Rule:
    label: str
    pattern: re.Pattern[str]
    #: Validation optionnelle ; recoit le texte capture, renvoie True s'il est plausible.
    validate: Callable[[str], bool] | None = None
    #: Groupe de capture a retenir (0 = toute la correspondance).
    group: int = 0


# --------------------------------------------------------------------------- #
# Sommes de controle
# --------------------------------------------------------------------------- #

def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def valid_avs(raw: str) -> bool:
    """Numero AVS suisse a 13 chiffres, prefixe 756, cle EAN-13."""
    d = _digits(raw)
    if len(d) != 13 or not d.startswith("756"):
        return False
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d[:12]))
    return (10 - total % 10) % 10 == int(d[12])


def valid_iban(raw: str) -> bool:
    """IBAN, validation modulo 97 (ISO 13616)."""
    s = re.sub(r"\s+", "", raw).upper()
    if not (15 <= len(s) <= 34) or not s[:2].isalpha() or not s[2:4].isdigit():
        return False
    rearranged = s[4:] + s[:4]
    converted = "".join(str(ord(ch) - 55) if ch.isalpha() else ch for ch in rearranged)
    if not converted.isdigit():
        return False
    return int(converted) % 97 == 1


def valid_luhn(raw: str) -> bool:
    """Numero de carte (jusqu'a 20 chiffres : carte d'assure suisse), algorithme de Luhn."""
    d = _digits(raw)
    if not (13 <= len(d) <= 20):
        return False
    total, parity = 0, len(d) % 2
    for i, ch in enumerate(d):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


#: Bornes reelles des NPA suisses. Sans ce controle, toute annee du XXe siecle
#: suivie d'un mot capitalise serait prise pour une adresse.
NPA_MIN, NPA_MAX = 1000, 9658

#: Mots qui suivent frequemment un nombre a quatre chiffres sans former une
#: localite : titres de sections, en-tetes de rapports.
_NOT_A_LOCALITY = {
    "fax", "tel", "tél", "telephone", "téléphone", "renseignements", "page",
    "rue", "case", "du", "de", "le", "la", "au", "et",
}


def valid_npa(raw: str) -> bool:
    """Code postal suisse plausible, suivi d'un mot pouvant etre une localite."""
    parts = raw.split()
    if len(parts) < 2 or not parts[0].isdigit():
        return False
    if not (NPA_MIN <= int(parts[0]) <= NPA_MAX):
        return False
    return parts[1].lower() not in _NOT_A_LOCALITY


def valid_date(raw: str) -> bool:
    """Date calendaire reellement existante.

    Une annee a deux chiffres est lue au XXIe siecle : seul compte que le jour
    et le mois existent, le siecle n'y change rien hors du 29 fevrier.
    """
    import datetime
    sep = next((c for c in ".-/" if c in raw), None)
    if sep is None:
        return False
    try:
        d, m, y = (int(x) for x in raw.split(sep))
        datetime.date(y if y >= 100 else 2000 + y, m, d)
    except ValueError:
        return False
    return True


def valid_date_compacte(raw: str) -> bool:
    """« 13122021 » : jour, mois et annee existent."""
    import datetime
    try:
        datetime.date(int(raw[4:]), int(raw[2:4]), int(raw[:2]))
    except ValueError:
        return False
    return True


def valid_ch_phone(raw: str) -> bool:
    """Numero suisse : 10 chiffres en national, ou 11 avec l'indicatif 41."""
    d = _digits(raw)
    if d.startswith("41"):
        d = d[2:]
    elif d.startswith("0041"):
        d = d[4:]
    d = d.lstrip("0")
    return len(d) == 9


# --------------------------------------------------------------------------- #
# Regles
# --------------------------------------------------------------------------- #

#: Une date en chiffres, annee sur quatre chiffres ou sur deux : « 26.05.1955 »,
#: mais aussi « 26.1.26 », la forme courante des antecedents d'un dossier
#: medical. Limitee a l'annee a quatre chiffres, la regle laissait ces dates a
#: un modele qui les manquait ou les prenait pour un numero AVS — et le reglage
#: des dates ne pouvait plus rien en faire.
#:
#: Pas de chiffre ni de point colle de part et d'autre : « 1.2.16.3 » n'est pas
#: une date. Une lettre collee devant, si : l'OCR soude « ne le12.03.1940 ».
MOTIF_DATE = re.compile(
    r"(?<![\d./-])[0-3]?\d[./-][01]?\d[./-](?:(?:19|20)\d{2}|\d{2})"
    r"(?!\d|[./-]\d)")

#: Une date ecrite en toutes lettres : « 5 avril 2014 », « 1er mars »,
#: « 12 janv. 2024 », « mars 2025 ». La regle en chiffres ne les voyait pas, et
#: le modele les laissait passer : sur un rapport reel, « Examen du 2 avril
#: 2014 » restait lisible alors que toutes les dates devaient etre masquees.
#:
#: Le jour seul ne suffit pas a faire une date sans mois, ni le mois seul sans
#: jour ou annee : « mars » ou « mai » isoles ne sont pas des dates.
_MOIS_LONGS = (r"janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t"
               r"|septembre|octobre|novembre|d[ée]cembre")
_MOIS_COURTS = r"janv|f[ée]vr?|avr|juil|sept|oct|nov|d[ée]c"
_MOIS = rf"(?:(?:{_MOIS_LONGS})|(?:{_MOIS_COURTS})\.?)"
MOTIF_DATE_ECRITE = re.compile(
    rf"(?<![\w.])(?:(?:1er|[0-3]?\d)\s+{_MOIS}(?:\s+(?:19|20)\d{{2}})?"
    rf"|{_MOIS}\s+(?:19|20)\d{{2}})(?![\w])",
    re.IGNORECASE)


#: Une date dont l'OCR a perdu les points : « 13.12.2021 » lu « 13122021 »,
#: le cas ordinaire d'un pied de page imprime petit puis scanne. Sur un compte
#: rendu reel, la meme date etait masquee aux pages ou l'OCR lisait les
#: points, et restait lisible a celle ou il les perdait. Annee sur quatre
#: chiffres seulement : six chiffres sans separateur sont trop souvent autre
#: chose.
MOTIF_DATE_COMPACTE = re.compile(r"(?<![\d./-])[0-3]\d[01]\d(?:19|20)\d{2}(?![\d]|[./-]\d)")

#: Un site web : « www.hlac.ch », « https://www.h-lac.ch/medecine ». Il designe
#: l'etablissement aussi surement que son nom, et suit donc le meme sort : la
#: categorie est celle des employeurs, masques avec « Toutes les personnes »,
#: conserves avec « Le patient seulement ». Un nom de domaine nu n'est retenu
#: qu'avec une extension suisse ou generique : « 10 mg.de » n'est pas une
#: adresse. L'OCR met parfois une capitale a « Www », un trait devant, ou une
#: espace pour un point : « www.hlac ch ». Derriere « www », il n'y a pas de
#: doute ; ailleurs, l'espace n'est pas admise.
MOTIF_SITE = re.compile(
    r"(?<![A-Za-z0-9@.-])"
    r"(?:https?://(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+"
    r"(?:ch|com|org|net|fr|de|it|swiss|eu|info)"
    r"|www[. ](?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?[. ])+"
    r"(?:ch|com|org|net|fr|de|it|swiss|eu|info)"
    r"|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:ch|com|org|net|swiss))"
    r"\b(?![@.-]\w)(?:/[^\s]*)?",
    re.IGNORECASE)


#: Assureurs suisses : le nom designe l'assureur du patient, donc le patient ;
#: il est masque quoi qu'en dise le modele, sous la categorie des assureurs.
#: Sources (donnees OFSP) : liste des assureurs-maladie admis au 1.10.2026
#: (LAMal) et liste des assureurs-accidents 2025 (LAA). Pour chacun, la marque
#: courte et le nom legal complet (suffixes ci-dessous) ; les noms de marque
#: ajoutes hors liste (Easy Sana, Moove, Arcosana, Intras, Progres, Supra,
#: Cervino, Lumneziana, Kolping, Simplon) suivent l'usage courant.
#: Trois familles : noms distinctifs (seuls suffisent, casse et accents
#: ignores), sigles (casse exacte) et noms ambigus (mot courant, lieu ou nom de
#: famille), reconnus seulement avec un qualificatif d'assurance.

#: Marques sans ambiguite (LAMal et LAA : Assura, Atupri, Helsana, Sanitas,
#: Visana, Swica, Sympany, Vivao Sympany, Agrisano, OKK, Aquilana, Sodalis,
#: Suva, Rhenusana, sana24, vita surselva, Philos, Mutuel, Sumiswalder...).
_ASSUREURS_DISTINCTIFS = (
    r"assura|helsana|swica|sanitas|visana|groupe[ \t]+mutuel|atupri|[öo]kk|"
    r"sympany|agrisano|suva|sodalis|vivao|aquilana|mutuel[ \t]+assurance|"
    r"mutuel[ \t]+krankenversicherung|easy[ \t]+sana|moove|rhenusana|sana24|"
    r"vita[ \t]+surselva|philos|arcosana|sumiswalder|curaulta|"
    r"vall[ée]e[ \t]+d[’'][ \t]*entremont"
)
#: Sigles : casse exacte (CSS, KPT, EGK, SLKK ; CONCORDIA en capitales, tel
#: qu'imprime par l'assureur, alors que « Concordia » est un nom commun).
_ASSUREURS_SIGLES = r"CSS|KPT|EGK|SLKK|CONCORDIA"
#: Qualificatif d'assurance, apres ou avant le nom.
_QUALIF = (
    r"assurances?(?:[- ]maladie)?|versicherung(?:en|s-?gesellschaft)?|"
    r"kranken[- ]?(?:kasse|versicherung(?:en)?)|unfallversicherung(?:en)?|"
    r"gesundheits-?versicherung(?:en)?|caisse[- ]maladie|kasse|"
    r"assicurazion[ei]|insurance"
)
#: Noms ambigus de caisses (LAMal) et d'assureurs (LAA) : Avenir, Concordia,
#: Galenos, Glarner, Birchmeier, Wadenswil, Visperterminen, Steffisburg,
#: Luzerner Hinterland, Einsiedler, Simplon, Cervino, Lumneziana, Progres,
#: Supra, Intras, Kolping, Zurich, Emmental, Solida, Smile, Elvia, PAX.
_ASSUREURS_AMBIGUS = (
    r"avenir|concordia|galenos|glarner|birchmeier|w[äa]denswil|"
    r"visperterminen|steffisburg|luzerner[ \t]+hinterland|einsiedler|simplon|"
    r"cervino|lumneziana|progr[èe]s|supra|intras|kolping|z[üu]rich|emmental|"
    r"solida|smile|elvia|pax|branchen"
)
_LIEU = r"(?<![\w@.-])"
_FIN = r"(?![\w@-]|\.\w)"
_FORME = r"(?:[ \t]+(?:SA|AG|S\.A\.|S[àa]rl))?"
MOTIF_ASSUREUR = re.compile(
    _LIEU + r"(?:"
    r"(?:(?i:" + _ASSUREURS_DISTINCTIFS + r")|" + _ASSUREURS_SIGLES + r")"
    r"(?:[- ](?i:basis|" + _QUALIF + r"))?" + _FORME + r"|"
    # ambigu : nom puis qualificatif (« Zurich Assurances », « PAX Versicherung »)
    r"(?i:" + _ASSUREURS_AMBIGUS + r")[ \t-]+(?i:" + _QUALIF + r")" + _FORME + r"|"
    # ambigu : qualificatif puis nom (« Krankenkasse Birchmeier »,
    # « Caisse-maladie de la Vallee », « Assurance Avenir »)
    r"(?i:" + _QUALIF + r")[ \t]+(?i:(?:de[ \t]+(?:la[ \t]+)?)?"
    + _ASSUREURS_AMBIGUS + r")" + _FORME +
    r")" + _FIN)

#: Autres assureurs (accident, choses, vie), reconnus seulement pour requalifier
#: un « employeur » rendu par le modele, jamais comme regle autonome. « AXA »
#: en capitales exactes ; « Zurich » seul est une ville (voir MOTIF_ASSUREUR).
MOTIF_ASSUREUR_AUTRE = re.compile(
    _LIEU + r"(?:(?i:allianz|generali|helvetia|vaudoise|mobili[èe]re|mobiliar|"
    r"b[âa]loise|winterthur|hotela|elips[ \t]+life|frv)|AXA)" + _FIN)


def est_assureur(texte: str) -> bool:
    """Le texte nomme un assureur suisse connu."""
    return bool(MOTIF_ASSUREUR.search(texte)
                or MOTIF_ASSUREUR_AUTRE.search(texte))


def valid_date_ecrite(raw: str) -> bool:
    """Le jour, s'il y en a un, existe : « 45 mars » n'est pas une date."""
    jour = re.match(r"\d+", raw)
    return jour is None or 1 <= int(jour.group()) <= 31


RULES: tuple[Rule, ...] = (
    Rule("numero AVS",
         re.compile(r"\b756[.\s-]?\d{4}[.\s-]?\d{4}[.\s-]?\d{2}\b"),
         validate=valid_avs),
    Rule("IBAN",
         re.compile(r"\b[A-Z]{2}\d{2}(?:[   ]?[A-Z0-9]{4}){2,7}[   ]?[A-Z0-9]{1,4}\b"),
         validate=valid_iban),
    Rule("numero de carte",
         re.compile(r"\b(?:\d[   -]?){13,20}\b"),
         validate=valid_luhn),
    Rule("email",
         re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    Rule("numero de telephone",
         re.compile(r"(?:\+41|0041|\b0)(?:\s?\(0\))?\s?\d{2}(?:\s?/\s?|[\s.]?)\d{3}[\s.]?\d{2}[\s.]?\d{2}\b"),
         validate=valid_ch_phone),
    # NPA suisse suivi d'une localite, sur la MEME ligne.
    #
    # La version initiale utilisait \s+, qui franchit les sauts de ligne : sur
    # un rapport reel, « ...23.04.1931\nECHO-DOPPLER... » etait capture comme
    # un code postal, detruisant le titre du document et amputant la date de
    # naissance de son annee. Un NPA et sa localite sont toujours sur la meme
    # ligne ; on l'impose, et on verifie que le NPA existe reellement.
    Rule("code postal et localite",
         re.compile(r"\b([1-9]\d{3}[ \t]{1,3}[A-ZÀ-Ý][\w'’-]+"
                    r"(?:[- ](?!Fax|Tel|Tél|Case|Page|Rue)[A-ZÀ-Ý][\w'’-]+)?)"),
         validate=lambda m: valid_npa(m), group=1),
    # Dates completes : trop structurees pour etre laissees a un modele.
    Rule("date", MOTIF_DATE, validate=valid_date),
    Rule("date", MOTIF_DATE_ECRITE, validate=valid_date_ecrite),
    Rule("date", MOTIF_DATE_COMPACTE, validate=valid_date_compacte),
    Rule("employeur", MOTIF_SITE),
    Rule("assureur", MOTIF_ASSUREUR),
)


@dataclass(frozen=True)
class RuleMatch:
    start: int
    end: int
    label: str
    text: str
    #: Toujours 1.0 : une somme de controle est vraie ou fausse, pas probable.
    score: float = 1.0


def find_all(text: str, rules: tuple[Rule, ...] = RULES) -> list[RuleMatch]:
    """Applique toutes les regles et renvoie les correspondances validees."""
    found: list[RuleMatch] = []
    for rule in rules:
        for m in rule.pattern.finditer(text):
            captured = m.group(rule.group)
            if rule.validate and not rule.validate(captured):
                continue
            found.append(RuleMatch(start=m.start(rule.group), end=m.end(rule.group),
                                   label=rule.label, text=captured))
    return _dedupe(found)


def _dedupe(matches: list[RuleMatch]) -> list[RuleMatch]:
    """En cas de chevauchement, garde la correspondance la plus longue."""
    ordered = sorted(matches, key=lambda m: (-(m.end - m.start), m.start))
    kept: list[RuleMatch] = []
    for m in ordered:
        if not any(m.start < k.end and k.start < m.end for k in kept):
            kept.append(m)
    return sorted(kept, key=lambda m: m.start)
