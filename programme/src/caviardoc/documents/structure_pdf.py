"""Ce que contient, et ne contient pas, un PDF produit.

Un PDF fait uniquement d'images de pages, sans texte ni police, est la forme
exacte que prennent certains PDF d'hameconnage. Des antivirus a base
d'apprentissage le signalent parfois (« TR/PDF.MalwareX », « Heur.PDF »), sans
qu'il y ait rien de dangereux dedans. Le document n'a pas besoin d'etre soupconne
sur parole : on peut le *controler*, et le dire a l'utilisateur.

Le controle cherche ce qu'un PDF malveillant utilise pour agir — script, action
a l'ouverture, lien, fichier joint, formulaire — et ce qu'un PDF image ne doit
pas porter : du texte. Il rend la liste de ce qu'il trouve ; vide, le document
n'est fait que de pages-images.

Seule exception, voulue : la mention invisible que chaque page recoit
(`poser_mentions`), qui ne porte aucune donnee.
"""

from __future__ import annotations

import re

#: Texte pose, invisible, sur chaque page produite. Il ne porte aucune donnee :
#: ni nom, ni date, ni titre du document d'origine.
MENTION = "Document anonymisé par Caviardoc - page {numero}/{total}"

_MENTION = re.compile(r"Document anonymis[ée] par Caviardoc\s*\S\s*"
                      r"page \d+/\d+")


def poser_mentions(doc) -> None:
    """Pose sur chaque page sans texte une mention invisible, sans donnee.

    Un PDF de plusieurs pages faites chacune d'une seule image, sans le moindre
    texte, est la forme exacte d'un PDF d'hameconnage. Mesure avec VirusBarrier
    (moteur Avira) sur un document produit de deux pages : signale
    « TR/PDF.MalwareX » avec ou sans metadonnees, en JPEG comme en PNG, en
    couleur comme en gris ; chaque page seule ne l'etait pas ; et le meme
    document muni de cette seule ligne de texte ne l'est plus.

    C'est ce que porte tout scan passe par un OCR : une couche de texte
    invisible. Le texte est en mode de rendu 3 — ni trace ni remplissage —, il
    ne change donc pas un pixel de la page, ni ce que la verification relit.
    Une page qui porte deja du texte n'en recoit pas : une page recopiee d'un
    document deja produit garde sa mention, sans la doubler.
    """
    total = len(doc)
    for numero, page in enumerate(doc, 1):
        if page.get_text().strip():
            continue
        page.insert_text((24, page.rect.height - 8),
                         MENTION.format(numero=numero, total=total),
                         fontsize=6, render_mode=3)


def sans_mentions(texte: str) -> str:
    """Le texte extrait d'un PDF produit, prive des mentions posees."""
    return _MENTION.sub("", texte)

#: Noms PDF par lesquels un document agit ou cache quelque chose. Aucun n'a de
#: raison d'etre dans un PDF reconstruit a partir de l'image de ses pages.
NOMS_ACTIFS = {
    "JavaScript": "script", "JS": "script", "OpenAction": "action à l'ouverture",
    "AA": "action automatique", "Launch": "lancement d'un programme",
    "URI": "lien", "GoToR": "lien vers un autre fichier",
    "SubmitForm": "envoi de formulaire", "ImportData": "import de données",
    "EmbeddedFile": "fichier joint", "EmbeddedFiles": "fichier joint",
    "RichMedia": "contenu multimédia", "XFA": "formulaire", "AcroForm": "formulaire",
    "Names": "dictionnaire de noms", "Annots": "annotation", "Font": "police de texte",
    "ObjStm": "objets compressés", "Encrypt": "chiffrement",
}


def controler_structure(octets: bytes) -> list[str]:
    """Rend ce qu'un PDF contient qui n'a pas sa place dans un PDF image.

    Une liste vide veut dire : seulement des images de pages. Chaque entree est
    une phrase lisible, sans doublon.
    """
    import pymupdf

    trouves: set[str] = set()
    d = pymupdf.open(stream=octets, filetype="pdf")
    try:
        for xref in range(1, d.xref_length()):
            try:
                objet = d.xref_object(xref, compressed=True)
            except Exception:
                continue
            for nom, sens in NOMS_ACTIFS.items():
                if re.search(rf"/{nom}(?![A-Za-z0-9])", objet):
                    trouves.add(sens)
        if sans_mentions("".join(page.get_text() for page in d)).strip():
            trouves.add("texte")
        else:
            # La police est celle de la mention : elle n'ecrit rien d'autre.
            trouves.discard("police de texte")
    finally:
        d.close()
    return sorted(trouves)
