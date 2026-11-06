"""Moteur de detection, couche 2 : modele de spans multilingue.

Le remplacement se fait par offsets de caracteres, de droite a gauche. Aucune
expression reguliere n'est reconstruite a partir du texte detecte : une telle
reconstruction fait fuir les entites longues et detruit le texte environnant,
puisqu'un pseudonyme pose tot corrompt les correspondances suivantes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from . import models, telemetrie

#: Libelles en francais. GLiNER accepte des libelles arbitraires a l'execution ;
#: les formuler en francais ameliore nettement le rappel sur des documents
#: suisses romands — le numero AVS passe de non detecte a 0,96 de confiance.
#: Mesure sur corpus reel avec caviardage humain de reference (8 paires
#: exploitables, 270 mots attendus) : l'ajout de « rue », « numero de patient »,
#: « identifiant de dossier » et « numero d'assure » fait passer le rappel de
#: 56 % a 64 %. C'est le gain le plus important observe, devant l'abaissement
#: du seuil (+4 points) et la normalisation de casse (+2 points).
DEFAULT_LABELS: tuple[str, ...] = (
    "personne",
    "date de naissance",
    "adresse postale",
    "rue",
    "ville",
    "numero de telephone",
    "email",
    "numero AVS",
    "IBAN",
    "employeur",
    "numero de dossier",
    "numero de patient",
    "identifiant de dossier",
    "numero d'assure",
)

#: Seuil volontairement bas. Le cout d'une erreur est asymetrique : un faux
#: positif masque un mot de trop, un faux negatif laisse fuir l'identite d'un
#: patient. La quantification int8 abaisse en outre tous les scores (numero
#: AVS : 0,96 en fp32 contre 0,57 en int8), ce qui renforce ce choix.
#:
#: Mesure sur corpus reel, rappel contre caviardage humain de reference :
#: 0,20 -> 52 % ; 0,12 -> 56 %. Descendre plus bas n'apporte plus rien et
#: multiplie les faux positifs.
DEFAULT_THRESHOLD = 0.12


class Categorie(str, Enum):
    """Vocabulaire des etiquettes produites par le systeme.

    Les valeurs sont les libelles envoyes tels quels a GLiNER — c'est pourquoi
    ce sont des chaines et non des symboles opaques. L'enumeration ne contraint
    pas ``Span.label``, qui reste un ``str`` : le modele peut rendre n'importe
    quelle etiquette, et une fusion peut en composer de nouvelles. Elle
    documente le vocabulaire attendu et sert de source unique aux predicats
    ci-dessous.
    """

    PERSONNE = "personne"
    DATE_NAISSANCE = "date de naissance"
    DATE = "date"
    #: Date a rendre en J+n (`dates_relatives`). Jamais demandee au modele :
    #: posee par `pipeline.filtrer_dates`.
    DATE_RELATIVE = "date relative"
    ADRESSE = "adresse postale"
    RUE = "rue"
    VILLE = "ville"
    CODE_POSTAL = "code postal et localite"
    TELEPHONE = "numero de telephone"
    EMAIL = "email"
    AVS = "numero AVS"
    IBAN = "IBAN"
    CARTE = "numero de carte"
    EMPLOYEUR = "employeur"
    DOSSIER = "numero de dossier"
    PATIENT = "numero de patient"
    IDENTIFIANT_DOSSIER = "identifiant de dossier"
    ASSURE = "numero d'assure"
    # Quasi-identifiants : voir `QUASI_IDENTIFIANTS`.
    PROFESSION = "profession"
    ACTIVITE = "sport ou activite"
    NATIONALITE = "nationalite ou origine"
    EVENEMENT = "evenement"
    AGE = "age"


#: Identifiants toujours rattaches a la personne concernee, jamais a un tiers.
IDENTIFIANTS_PERSONNELS = (Categorie.AVS, Categorie.PATIENT, Categorie.ASSURE)

#: Etiquettes designant une organisation plutot qu'une personne.
ORGANISATIONS = (Categorie.EMPLOYEUR,)

#: Quasi-identifiants : ce qui ne nomme personne, mais permet un recoupement.
#: Un extrait d'arret caviarde, sans un seul nom — un cycliste, le Giro 2022,
#: une blessure a la cuisse —, a suffi a une IA pour retrouver la personne.
#: Le modele ne cherchait rien de tout cela.
#:
#: Ces passages ne sont **pas masques par defaut** : masquer toute profession,
#: tout age et toute activite viderait un dossier medical de ce qui le rend
#: utile. Ils sont montres a la relecture, « a juger », et le medecin decide.
#: Une passe du modele leur est reservee (`pipeline.quasi_identifiants`) :
#: melangees a `DEFAULT_LABELS`, ces etiquettes disputeraient les memes mots
#: aux identifiants directs, dont le rappel ne serait plus garanti.
#:
#: « evenement » et non « evenement date » : `est_date` cherche « date » dans
#: l'etiquette, et la date d'un evenement n'est pas une date a masquer.
QUASI_IDENTIFIANTS = (Categorie.PROFESSION, Categorie.ACTIVITE,
                      Categorie.NATIONALITE, Categorie.EVENEMENT, Categorie.AGE)
QUASI_LABELS: tuple[str, ...] = tuple(c.value for c in QUASI_IDENTIFIANTS)

#: Plus haut que `DEFAULT_THRESHOLD` : un quasi-identifiant manque ne fait
#: rien fuir de plus qu'avant, mais un signalement de trop dilue l'attention.
#: Mesure : « Le patient » sortait en « profession » a 0,25, « cycliste
#: professionnel » a 0,86, « ancien conseiller communal » a 0,50.
SEUIL_QUASI = 0.4


# --------------------------------------------------------------------------- #
# Predicats sur les etiquettes
#
# Ces fonctions existent pour une raison precise : les comparaisons de chaines
# sur ``label`` etaient auparavant disseminees dans trois modules, avec trois
# styles differents — sous-chaine, appartenance a un ensemble, appartenance
# partielle. Renommer une etiquette cassait alors cinq endroits **en silence**,
# sans exception ni test rouge : des entites cessaient simplement d'etre
# masquees. Pour un outil de confidentialite, c'est le pire mode de defaillance
# possible. Tout passe desormais par ici.
# --------------------------------------------------------------------------- #

def est_personne(label: str) -> bool:
    """Etiquette designant une personne physique."""
    return Categorie.PERSONNE.value in label.lower()


def est_date(label: str) -> bool:
    """Etiquette designant une date, de naissance ou non."""
    return Categorie.DATE.value in label.lower()


def est_naissance(label: str) -> bool:
    """Etiquette designant explicitement une date de naissance."""
    return "naissance" in label.lower()


def est_identifiant_personnel(label: str) -> bool:
    """Numero rattache a la personne : AVS, patient, assure."""
    bas = label.lower()
    return any(c.value.lower() in bas for c in IDENTIFIANTS_PERSONNELS)


def est_organisation(label: str) -> bool:
    """Etiquette designant un etablissement ou un employeur."""
    bas = label.lower()
    return any(c.value.lower() in bas for c in ORGANISATIONS)


def est_quasi_identifiant(label: str) -> bool:
    """Profession, activite, origine, evenement, age : a juger, pas a masquer.

    Egalite stricte, et non sous-chaine comme ailleurs : « age » se lit dans
    « page » ou « message », et une etiquette inconnue doit tomber du cote
    grave (`est_identifiant_direct`), pas du cote « laisse visible ».
    """
    return label.strip().lower() in QUASI_LABELS


#: Categories qui n'identifient personne a elles seules, mais y contribuent
#: une fois recoupees : une ville, un code postal, un employeur, une date qui
#: n'est pas celle de naissance.
CONTEXTUELLES = (Categorie.VILLE, Categorie.CODE_POSTAL, Categorie.EMPLOYEUR)


def est_contextuel(label: str) -> bool:
    """Etiquette qui situe sans designer."""
    bas = label.lower()
    if est_quasi_identifiant(bas):
        return True
    if est_naissance(bas):
        return False
    if est_date(bas):
        return True
    return any(c.value.lower() in bas for c in CONTEXTUELLES)


def est_identifiant_direct(label: str) -> bool:
    """Une donnee de cette categorie suffit, seule, a reconnaitre la personne.

    Defini par la negative : est contextuel ce qu'on a explicitement classe
    comme tel, et tout le reste est tenu pour identifiant. Une etiquette que
    personne n'a prevue — le modele peut en rendre — tombe donc du cote grave.
    C'est le sens dans lequel un outil de confidentialite doit se tromper.
    """
    return not est_contextuel(label)


class Origine(str, Enum):
    """D'ou vient une detection.

    Cette information vivait auparavant dans ``score``, ou la valeur 1,0 servait
    de sentinelle pour « detecte par une regle deterministe ». La propagation
    attribuait elle aussi 1,0, si bien que la sentinelle ne distinguait plus
    rien et que l'arbitrage de fusion se jouait sur une egalite fortuite.
    ``score`` redevient une confiance ; ``origine`` porte la provenance.
    """

    REGLE = "regle"
    MODELE = "modele"
    PROPAGE = "propage"
    #: Pose par une personne — a la relecture, ou par la liste d'exceptions
    #: qui rejoue ses decisions. Une intention explicite, pas une estimation.
    HUMAIN = "humain"
    #: Texte recouvert par une zone noircie (logo, bloc d'en-tete) : il n'a pas
    #: ete reconnu comme une donnee, il est masque parce que ses pixels le sont.
    ZONE = "zone"


#: Ordre d'autorite pour designer l'etiquette d'un groupe fusionne. Une somme
#: de controle valide l'emporte sur toute estimation ; une propagation, qui
#: reporte une etiquette deja etablie ailleurs dans le document, l'emporte sur
#: une detection isolee du modele.
AUTORITE: dict[str, int] = {
    # Une decision humaine explicite prime : elle n'estime pas, elle designe.
    Origine.HUMAIN.value: 3,
    Origine.REGLE.value: 2,
    Origine.PROPAGE.value: 1,
    Origine.MODELE.value: 0,
    # Un defaut, jamais un arbitre : toute detection qui recoupe une zone
    # garde son etiquette.
    Origine.ZONE.value: -1,
}


@dataclass(frozen=True)
class Span:
    """Une entite detectee, reperee par offsets de caracteres."""

    start: int
    end: int
    label: str
    score: float
    text: str
    #: Provenance de la detection. Par defaut le modele : c'est le cas le plus
    #: frequent, et les regles comme la propagation la renseignent explicitement.
    origine: str = Origine.MODELE.value

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end

    @property
    def autorite(self) -> int:
        """Poids de la provenance dans un arbitrage de fusion."""
        return AUTORITE.get(self.origine, 0)

    def est_personne(self) -> bool:
        return est_personne(self.label)

    def est_date(self) -> bool:
        return est_date(self.label)

    def est_naissance(self) -> bool:
        return est_naissance(self.label)

    def est_identifiant_personnel(self) -> bool:
        return est_identifiant_personnel(self.label)

    def est_organisation(self) -> bool:
        return est_organisation(self.label)

    def est_quasi_identifiant(self) -> bool:
        return est_quasi_identifiant(self.label)


class EngineUnavailable(RuntimeError):
    """Le moteur ne peut pas etre charge (dependances ou modele absents)."""


def _faire_taire_avertissements() -> None:
    """Coupe les avertissements benins du chargement (type `gliner` inconnu de
    transformers, regex Mistral detectee a tort, noyau ReduceMean fp16 absent
    sur CPU). Les erreurs restent affichees."""
    try:
        from transformers.utils import logging as hf_logging
        hf_logging.set_verbosity_error()
    except ImportError:
        pass
    try:
        import onnxruntime
        onnxruntime.set_default_logger_severity(3)
    except ImportError:
        pass
    # La bibliotheque est chargee : l'API coupe en plus de la variable.
    telemetrie.couper()


def load_model(model_key: str | None = None):
    """Charge le modele ONNX depuis le repertoire de donnees local."""
    spec = models.REGISTRY[model_key or models.modele_actif()]
    status = models.check(spec)
    if not status.complete:
        raise EngineUnavailable(
            f"modele {spec.key} absent ou incomplet "
            f"({models.human(status.missing_bytes)} manquants). "
            f"Lancez `caviardoc setup --modele {spec.key}`.")

    # Avant l'import de gliner, qui charge onnxruntime : la variable doit
    # preceder l'initialisation de la bibliotheque (voir `telemetrie`). Deja
    # posee a l'import du paquet ; reposee si un appelant l'a retiree depuis.
    telemetrie.couper()
    try:
        from gliner import GLiNER
    except ImportError as exc:
        raise EngineUnavailable(
            "dependances du moteur absentes. Lancez `caviardoc setup`.") from exc

    _faire_taire_avertissements()
    return GLiNER.from_pretrained(
        str(spec.local_dir()),
        load_onnx_model=True,
        onnx_model_file=models.FICHIER_ONNX[spec.key],
        load_tokenizer=True,
        local_files_only=True,
    )


def detect(model, text: str,
           labels: Sequence[str] = DEFAULT_LABELS,
           threshold: float = DEFAULT_THRESHOLD) -> list[Span]:
    """Detecte les entites dans un texte court (un bloc)."""
    from .concurrence import modele
    with modele():
        raw = model.predict_entities(text, list(labels), threshold=threshold)
    spans = [Span(start=e["start"], end=e["end"], label=e["label"],
                  score=float(e["score"]), text=e["text"],
                  origine=Origine.MODELE.value) for e in raw]
    return resolve_overlaps(spans)


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Conserve, en cas de chevauchement, le span le plus long puis le mieux note."""
    ordered = sorted(spans, key=lambda s: (-(s.end - s.start), -s.score, s.start))
    kept: list[Span] = []
    for s in ordered:
        if not any(s.overlaps(k) for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s.start)


def _fusionner_chevauchements(spans: Sequence[Span]) -> list[Span]:
    """Rend des spans disjoints : les chevauchants sont remplaces par leur union.

    Le span le plus long du groupe fournit l'etiquette et le rendu ; l'union
    est la seule politique sure, un caractere masque en trop coutant moins
    qu'un fragment laisse en clair.
    """
    groupes: list[list[Span]] = []
    fin = -1
    for s in sorted(spans, key=lambda s: (s.start, -s.end)):
        if groupes and s.start < fin:
            groupes[-1].append(s)
            fin = max(fin, s.end)
        else:
            groupes.append([s])
            fin = s.end
    fusionnes: list[Span] = []
    for g in groupes:
        if len(g) == 1:
            fusionnes.append(g[0])
            continue
        tete = max(g, key=lambda s: s.end - s.start)
        debut, bout = min(s.start for s in g), max(s.end for s in g)
        fusionnes.append(Span(start=debut, end=bout, label=tete.label,
                              score=tete.score, text=tete.text,
                              origine=tete.origine))
    return fusionnes


def apply_spans(text: str, spans: Sequence[Span],
                render=lambda s: f"[{s.label.upper().replace(' ', '_')}]") -> str:
    """Remplace les spans dans le texte, de droite a gauche.

    Parcourir en ordre decroissant garantit que les offsets restants restent
    valides, a condition que les spans soient disjoints : un masque pose a la
    main peut chevaucher une detection, d'ou la fusion prealable.
    """
    out = text
    for s in sorted(_fusionner_chevauchements(spans), key=lambda s: -s.start):
        out = out[:s.start] + render(s) + out[s.end:]
    return out


# --------------------------------------------------------------------------- #
# Autotest
# --------------------------------------------------------------------------- #

REFERENCE_TEXT = (
    "Le patient Jean-Marc Dubois, ne le 12.03.1974 a Echallens, "
    "AVS 756.1234.5678.97, a ete recu par la Dre Sylvie Girard. "
    "IBAN CH93 0076 2011 6238 5295 7, tel 032 555 12 34, "
    "jm.dubois@example.com, rue des Moulins 12, 1040 Echallens."
)

#: Chaines qui ne doivent plus apparaitre dans la sortie anonymisee.
MUST_DISAPPEAR = (
    "Jean-Marc Dubois",
    "756.1234.5678.97",
    "032 555 12 34",
    "jm.dubois@example.com",
    "CH93 0076 2011 6238 5295 7",
)

#: Contenu utile qui doit survivre : un anonymiseur qui detruit le texte est
#: aussi defaillant qu'un anonymiseur qui laisse fuir.
MUST_SURVIVE = ("Le patient", "a ete recu par la")


def selftest(verbose: bool = True) -> int:
    """Charge le moteur, anonymise la phrase de reference, verifie le resultat."""
    try:
        t0 = time.time()
        model = load_model()
        load_s = time.time() - t0
    except EngineUnavailable as exc:
        if verbose:
            print(f"Moteur indisponible : {exc}")
        return 1

    t0 = time.time()
    spans = detect(model, REFERENCE_TEXT)
    infer_ms = (time.time() - t0) * 1000
    output = apply_spans(REFERENCE_TEXT, spans)

    leaked = [s for s in MUST_DISAPPEAR if s in output]
    lost = [s for s in MUST_SURVIVE if s not in output]

    if verbose:
        print(f"Chargement  {load_s:.1f} s")
        print(f"Inference   {infer_ms:.0f} ms")
        print(f"Entites     {len(spans)}")
        print()
        for s in spans:
            print(f"  [{s.start:3d}:{s.end:3d}]  {s.label:22s} {s.score:.2f}  {s.text!r}")
        print()
        print("Sortie :")
        print(f"  {output}")
        print()
        if leaked:
            print(f"ECHEC — donnees encore visibles : {leaked}")
        if lost:
            print(f"ECHEC — contenu utile detruit : {lost}")
        if not leaked and not lost:
            print("Autotest reussi.")

    return 0 if not leaked and not lost else 1
