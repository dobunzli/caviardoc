"""Vocabulaire neutre : ce qui n'identifie jamais personne.

Mesure sur corpus medical reel, avec caviardage humain de reference : sur 459
mots masques a politique alignee, **299 etaient des faux positifs isoles** —
c'est-a-dire ni corrects, ni simplement debordants d'une vraie entite voisine.
Les plus frequents ne sont pas des noms mal lus, mais du vocabulaire de metier
que le modele prend pour des entites : « patient » (20 occurrences),
« medecin », « Non valide », « SIC ».

Le cout de ces faux positifs n'est pas seulement esthetique. Un document ou
« patient » est noirci vingt fois devient penible a relire, et surtout la
verification finale signale chacune de ces chaines comme une fuite potentielle
des qu'elle reapparait ailleurs — noyant les vraies alertes sous le bruit.

**Cette liste ne s'applique jamais aux detections deterministes.** Un numero AVS
valide par sa somme de controle n'est pas soumis a l'avis d'une liste de mots :
la couche 1 est certaine, et rien ne doit pouvoir la contredire.
"""

from __future__ import annotations

import re

#: Mots grammaticaux. Un span qui n'en contient pas d'autres ne designe rien.
MOTS_VIDES = {
    "a", "au", "aux", "avec", "ce", "ces", "cet", "cette", "dans", "de", "des",
    "du", "en", "et", "eux", "il", "elle", "ils", "elles", "la", "le", "les",
    "leur", "leurs", "lui", "ma", "mais", "me", "mes", "mon", "ne", "nos",
    "notre", "nous", "on", "ou", "par", "pas", "pour", "qu", "que", "qui",
    "sa", "se", "ses", "son", "sur", "ta", "te", "tes", "toi", "ton", "tu",
    "un", "une", "vos", "votre", "vous", "y", "d", "l", "n", "s", "c", "j",
    # « Je » en tete de phrase passe pour une personne : sur un courrier
    # reel, le modele l'a note jusqu'a 1,0 et masque 23 fois.
    "je", "moi",
    "monsieur", "madame", "mademoiselle", "mr", "mme", "mlle", "m",
}

#: Vocabulaire de metier releve comme faux positif sur corpus reel. Chaque
#: terme y figure parce qu'il a ete masque a tort, pas par anticipation.
TERMES_METIER = {
    # roles et personnes, au sens generique
    "patient", "patiente", "patients", "patientes", "medecin", "medecins",
    "infirmier", "infirmiere", "infirmiers", "infirmieres", "soignant",
    "soignante", "confrere", "consoeur", "assure", "assuree", "resident",
    "residente", "client", "cliente", "beneficiaire", "personne",
    # actes et documents
    "consultation", "rapport", "fiche", "transfert", "prescription",
    "ordonnance", "examen", "controle", "diagnostic", "traitement", "soins",
    "sejour", "admission", "sortie", "suivi", "bilan", "dossier",
    # qualificatifs de resultat
    "normal", "normale", "anormal", "anormale", "valide", "validee",
    "negatif", "negative", "positif", "positive", "resultat", "resultats",
    "non", "oui",
    # divers releves
    "domicile", "perimetre", "marche", "ans", "an", "page", "total",
}

#: Formules d'appel d'un courrier. Le modele les prend pour des noms des
#: qu'elles occupent seules une ligne : « Cher Confrere, », « Chere Collegue, »,
#: « Chere Doctoresse, » devenaient « Chere PERSONNE_2 ». Elles ne nomment
#: personne.
FORMULES_APPEL = {
    "cher", "chere", "chers", "cheres", "collegue", "collegues", "confreres",
    "consoeurs", "docteur", "doctoresse", "professeur", "professeure", "ami",
    "amie", "amis", "amies",
}

VOCABULAIRE_NEUTRE = MOTS_VIDES | TERMES_METIER | FORMULES_APPEL

_MOT = re.compile(r"[^\W\d_]+", re.UNICODE)
_CHIFFRES = re.compile(r"\d")


def _sans_accents(mot: str) -> str:
    import unicodedata
    # Les ligatures ne sont pas des accents : NFKD laisse « œ » tel quel, et
    # « Consœur » echappait a « consoeur ».
    mot = mot.lower().replace("œ", "oe").replace("æ", "ae")
    decompose = unicodedata.normalize("NFKD", mot)
    return "".join(c for c in decompose if not unicodedata.combining(c))


def est_generique(texte: str) -> bool:
    """Le texte n'est-il compose que de vocabulaire neutre ?

    Un seul mot hors vocabulaire suffit a rendre le span significatif :
    « Monsieur Zorglub » est conserve, « Monsieur » seul ne l'est pas.

    Tout texte portant un chiffre est conserve, quels que soient ses mots.
    « 93 ans » n'a pour mot que « ans », qui est generique — mais l'age est un
    quasi-identifiant, et l'ecarter emporterait le nombre avec lui. Une liste
    de mots n'a aucune competence sur des chiffres : dates, numeros et ages
    relevent des regles deterministes.
    """
    if _CHIFFRES.search(texte):
        return False
    mots = _MOT.findall(texte)
    if not mots:
        return False
    return all(_sans_accents(m) in VOCABULAIRE_NEUTRE for m in mots)
