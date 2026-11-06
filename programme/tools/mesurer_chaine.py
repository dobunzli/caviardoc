"""Mesure de la chaine documentaire complete, et non du seul detecteur.

`mesurer.py` mesure la couche 2 sur du texte : le corpus est fait de chaines,
passees directement au modele. Rien n'y touche PyMuPDF, Tesseract, les zones
ni les rectangles noirs. Son 97 % dit ce que vaut la detection, pas ce que
vaut l'outil.

Le present outil part du meme corpus et de la meme verite terrain, mais
fabrique d'abord un PDF **rasterise** — sans couche texte, comme un document
scanne — puis le passe par `caviarder_pdf` et relit le fichier produit. Le
chiffre obtenu couvre donc l'OCR, la detection, la propagation a l'echelle du
document, le noircissement et la verification.

Il est plus bas que celui de `mesurer.py`. C'est le but : **l'ecart entre les
deux est la part du risque que la mesure du detecteur ne voit pas.**

Trois colonnes, et non deux, parce qu'une entite peut echouer pour deux
raisons qu'il serait trompeur de confondre :

- **lisible** : part des entites attendues que l'OCR retrouve dans le PDF
  d'entree. Ce qu'il n'a pas lu, le modele ne pouvait pas le detecter — c'est
  une defaillance d'OCR, pas de detection.
- **rappel** : part des entites attendues absentes du document produit.
- **preservation** : part des chaines qui devaient rester lisibles et le sont.

Une reserve sur la methode, qui vaut aussi pour `verifier_sortie` : la lecture
du document produit passe par l'OCR, et un OCR qui echoue sur une chaine
intacte la compte comme masquee. Le rappel mesure ici est donc une borne
haute. La colonne « lisible » sert precisement a estimer cette marge.

Usage :
    python tools/mesurer_chaine.py            # affiche le tableau
    python tools/mesurer_chaine.py --details  # entites manquees, contenu detruit
    python tools/mesurer_chaine.py --garder DOSSIER  # conserve les PDF produits
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE / "src"))
sys.path.insert(0, str(RACINE / "tools"))

from caviardoc.documents.caviardage import caviarder_pdf  # noqa: E402
from caviardoc.documents.ocr import ocr_image  # noqa: E402
from caviardoc.engine import load_model  # noqa: E402
from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402
from caviardoc.roles import Niveau  # noqa: E402
from mesurer import CORPUS, architecture, empreinte_environnement  # noqa: E402

#: Marge de la page fabriquee, en points. Large : un document reel n'ecrit pas
#: jusqu'au bord, et l'OCR se degrade nettement sur les caracteres coupes.
MARGE = 56
CORPS = 11
DPI = 300

#: Une police vectorielle est indispensable : le corpus contient des
#: caracteres hors Latin-1 (« Œdeme »), que les polices de base d'un PDF ne
#: savent pas encoder. Faute de police embarquable, la mesure serait faussee
#: par des caracteres perdus avant meme le rendu.
POLICES = {
    "darwin": ("/System/Library/Fonts/Supplemental/Arial.ttf",
               "/Library/Fonts/Arial.ttf"),
    "win32": ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf"),
}
POLICES_DEFAUT = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def police() -> str:
    """Chemin d'une police vectorielle utilisable pour fabriquer les pages.

    Les listes propres a chaque systeme sont essayees d'abord, puis la liste
    portable. Sans ce repli, un macOS sans Arial echouait alors qu'une police
    parfaitement utilisable pouvait etre installee ailleurs.
    """
    for chemin in _candidates():
        if Path(chemin).is_file():
            return chemin
    raise SystemExit(
        "Aucune police vectorielle trouvee. Installez-en une, ou indiquez-la "
        "avec --police. Cherchees : " + ", ".join(_candidates()))


def _candidates() -> tuple[str, ...]:
    """Polices a essayer : celles du systeme, puis les portables."""
    propres = POLICES.get(sys.platform, ())
    return tuple(dict.fromkeys(propres + POLICES_DEFAUT))


def compact(chaine: str) -> str:
    """Forme comparable : sans espaces, sans casse.

    Les espaces sautent parce que l'OCR en invente et en perd, et que le corpus
    contient justement un pied de page compose en lettres espacees.
    """
    return "".join(chaine.split()).lower()


def fabriquer_pdf(texte: str, chemin: Path, fichier_police: str,
                  dpi: int = DPI) -> None:
    """Ecrit ``texte`` dans un PDF rasterise, sans couche texte.

    Deux temps, comme un scanner : la page est composee, puis rendue en image
    et reinseree. Ce qui en sort ne contient plus un seul caractere extractible
    — l'OCR est le seul moyen d'y acceder, ce qui est precisement la situation
    qu'on veut mesurer.
    """
    import pymupdf

    compose = pymupdf.open()
    page = compose.new_page()
    cadre = pymupdf.Rect(MARGE, MARGE,
                         page.rect.width - MARGE, page.rect.height - MARGE)
    reste = page.insert_textbox(cadre, texte, fontsize=CORPS,
                                fontname="mesure", fontfile=fichier_police)
    if reste < 0:
        # Le corpus tient sur une page a 11 points ; si un document venait a
        # grandir, mieux vaut le signaler que le tronquer en silence.
        raise SystemExit(
            f"{chemin.name} : le texte deborde de la page ({reste:.0f} points "
            "manquants). Reduisez CORPS ou paginez.")

    rasterise = pymupdf.open()
    for p in compose:
        pix = p.get_pixmap(dpi=dpi)
        nouvelle = rasterise.new_page(width=p.rect.width, height=p.rect.height)
        nouvelle.insert_image(nouvelle.rect, stream=pix.tobytes("png"))
    rasterise.save(chemin)
    rasterise.close()
    compose.close()


def lire(chemin: Path, dpi: int, langue: str) -> str:
    """Texte lu par OCR dans un PDF, page apres page."""
    import pymupdf

    d = pymupdf.open(chemin)
    try:
        morceaux = []
        for page in d:
            pix = page.get_pixmap(dpi=dpi)
            from io import BytesIO

            from PIL import Image
            img = Image.open(BytesIO(pix.tobytes("png")))
            morceaux.append(ocr_image(img, langue=langue, dpi=dpi).texte)
        return "\n".join(morceaux)
    finally:
        d.close()


@dataclass
class BilanChaine:
    """Comme ``mesurer.Bilan``, plus la part imputable a l'OCR."""

    attendus: int = 0
    lisibles: int = 0
    masques: int = 0
    a_preserver: int = 0
    preserves: int = 0
    soignants: int = 0
    soignants_preserves: int = 0
    manques: list[str] = field(default_factory=list)
    detruits: list[str] = field(default_factory=list)
    illisibles: list[str] = field(default_factory=list)
    fuites: list[str] = field(default_factory=list)

    @property
    def rappel(self) -> float:
        return self.masques / self.attendus if self.attendus else 1.0

    @property
    def lisibilite(self) -> float:
        return self.lisibles / self.attendus if self.attendus else 1.0

    @property
    def preservation(self) -> float:
        return self.preserves / self.a_preserver if self.a_preserver else 1.0

    @property
    def taux_soignants(self) -> float:
        return (self.soignants_preserves / self.soignants
                if self.soignants else 1.0)


def evaluer_chaine(model, document: dict, niveau: Niveau, dossier: Path,
                   fichier_police: str, dpi: int, langue: str) -> BilanChaine:
    ident = f"{document['id']}-{niveau.value}"
    source = dossier / f"{ident}-entree.pdf"
    sortie = dossier / f"{ident}-sortie.pdf"

    fabriquer_pdf(document["texte"], source, fichier_police, dpi=dpi)
    lu_entree = compact(lire(source, dpi, langue))

    rapport = caviarder_pdf(model, source, sortie, dpi=dpi, langue=langue,
                            niveau=niveau, table=TablePseudonymes())
    lu_sortie = compact(lire(sortie, dpi, langue))

    # L'outil de mesure ne compte que les chaines : la gravite et la page
    # servent a l'utilisateur, pas au tableau de bord.
    b = BilanChaine(fuites=[f.texte for f in rapport.fuites])
    for e in document["entites"]:
        if niveau is Niveau.COMPLET or e["role"] == "patient":
            b.attendus += 1
            aiguille = compact(e["texte"])
            if aiguille in lu_entree:
                b.lisibles += 1
            else:
                b.illisibles.append(f'{document["id"]}: {e["texte"]!r}')
            if aiguille not in lu_sortie:
                b.masques += 1
            else:
                b.manques.append(f'{document["id"]}: {e["texte"]!r}')
        elif e["role"] == "soignant":
            b.soignants += 1
            if compact(e["texte"]) in lu_sortie:
                b.soignants_preserves += 1

    for s in document["preserver"]:
        # Une chaine que l'OCR ne lit deja pas dans l'entree ne prouve rien sur
        # le caviardage : on ne la compte pas.
        if compact(s) not in lu_entree:
            continue
        b.a_preserver += 1
        if compact(s) in lu_sortie:
            b.preserves += 1
        else:
            b.detruits.append(f'{document["id"]}: {s!r}')
    return b


def cumuler(bilans: list[BilanChaine]) -> BilanChaine:
    total = BilanChaine()
    for b in bilans:
        total.attendus += b.attendus
        total.lisibles += b.lisibles
        total.masques += b.masques
        total.a_preserver += b.a_preserver
        total.preserves += b.preserves
        total.soignants += b.soignants
        total.soignants_preserves += b.soignants_preserves
        total.manques += b.manques
        total.detruits += b.detruits
        total.illisibles += b.illisibles
        total.fuites += b.fuites
    return total


#: Seuils de non-regression de la chaine complete.
#:
#: Plus tolerants que ceux du detecteur, et pour une raison de fond : la mesure
#: passe par un rendu puis un OCR. La police disponible n'est pas la meme d'une
#: machine a l'autre — Arial ici, DejaVu sur un runner Linux — et Tesseract
#: n'en lit pas les glyphes de facon identique. Un ecart de quelques points sur
#: la preservation traduit donc une difference de lecture, pas une regression
#: du caviardage.
#:
#: Le rappel, lui, garde le seuil du detecteur : ce qui doit etre masque doit
#: l'etre, quelle que soit la police.
SEUILS = {
    "lisibilite_complet": 0.90,
    "rappel_complet": 0.90,
    "preservation_complet": 0.80,
    "lisibilite_patient": 0.90,
    "rappel_patient": 0.90,
    "preservation_patient": 0.80,
    "soignants_preserves": 0.50,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--details", action="store_true",
                    help="lister les entites manquees, illisibles et detruites")
    ap.add_argument("--modele", default=None,
                    help="cle du modele (defaut : celui retenu sur la machine)")
    ap.add_argument("--dpi", type=int, default=DPI,
                    help=f"resolution de rendu et d'OCR (defaut : {DPI})")
    ap.add_argument("--langue", default="fra", help="langue de l'OCR")
    ap.add_argument("--police", default=None,
                    help="police vectorielle pour fabriquer les pages")
    ap.add_argument("--garder", default=None, metavar="DOSSIER",
                    help="conserver les PDF fabriques et produits, pour les "
                         "regarder plutot que de les croire")
    ap.add_argument("--seuils", action="store_true",
                    help="echouer si un seuil est franchi, ou si la chaine "
                         "signale une fuite")
    args = ap.parse_args()

    documents = json.loads(CORPUS.read_text(encoding="utf-8"))["documents"]
    fichier_police = args.police or police()

    print(empreinte_environnement())
    print(f"\n  seuils       : {architecture()}")
    print(f"  chaine       : PDF rasterise · {args.dpi} dpi · OCR {args.langue}")
    print(f"  police       : {fichier_police}\n")

    model = load_model(args.modele)

    if args.garder:
        dossier = Path(args.garder)
        dossier.mkdir(parents=True, exist_ok=True)
        temporaire = None
    else:
        temporaire = tempfile.TemporaryDirectory()
        dossier = Path(temporaire.name)

    try:
        print(f"{'document':30s} {'complet':>22s} {'patient':>22s}")
        print(f"{'':30s} {'lis.  rappel  preserv.':>22s} "
              f"{'lis.  rappel  preserv.':>22s}")
        print("-" * 76)
        par_doc = {n: [] for n in Niveau}
        for d in documents:
            ligne = f"{d['id'][:28]:30s}"
            for niveau in (Niveau.COMPLET, Niveau.PATIENT):
                b = evaluer_chaine(model, d, niveau, dossier, fichier_police,
                                   args.dpi, args.langue)
                par_doc[niveau].append(b)
                ligne += (f" {b.lisibilite*100:4.0f}% {b.rappel*100:6.0f}%"
                          f" {b.preservation*100:8.0f}%")
            print(ligne)
        print("-" * 76)
    finally:
        if temporaire is not None:
            temporaire.cleanup()

    resultats = {n: cumuler(par_doc[n]) for n in Niveau}
    c, p = resultats[Niveau.COMPLET], resultats[Niveau.PATIENT]
    mesures = {
        "lisibilite_complet": c.lisibilite,
        "rappel_complet": c.rappel,
        "preservation_complet": c.preservation,
        "lisibilite_patient": p.lisibilite,
        "rappel_patient": p.rappel,
        "preservation_patient": p.preservation,
        "soignants_preserves": p.taux_soignants,
    }
    print()
    for nom, valeur in mesures.items():
        if args.seuils:
            seuil = SEUILS[nom]
            etat = "ok " if valeur >= seuil else "BAS"
            print(f"  {etat} {nom:24s} {valeur*100:5.1f} %   "
                  f"(seuil {seuil*100:.0f} %)")
        else:
            print(f"  {nom:24s} {valeur*100:5.1f} %")

    fuites = sorted(set(c.fuites + p.fuites))
    print()
    if fuites:
        print(f"  VERIFICATION : {len(fuites)} fuite(s) signalee(s) par la "
              "chaine elle-meme")
        for f in fuites:
            print(f"    {f}")
    else:
        print("  verification : aucune fuite signalee par la chaine")

    if args.details:
        for niveau in (Niveau.COMPLET, Niveau.PATIENT):
            b = resultats[niveau]
            for intitule, items in (("illisibles a l'OCR", b.illisibles),
                                    ("entites manquees", b.manques),
                                    ("contenu detruit", b.detruits)):
                if items:
                    print(f"\n  {intitule} ({niveau.value}) :")
                    for m in items:
                        print(f"    {m}")

    if args.seuils:
        sous = [n for n, v in mesures.items() if v < SEUILS[n]]
        if sous or fuites:
            print()
            if sous:
                print(f"REGRESSION : {', '.join(sous)}")
            if fuites:
                # Une fuite signalee par la chaine elle-meme n'est pas un
                # seuil a ajuster : c'est une chaine sensible retrouvee dans
                # le document produit.
                print(f"FUITE : {len(fuites)} chaine(s) sensible(s) "
                      "retrouvee(s) dans le document produit")
            return 1
        print("\nTous les seuils sont tenus, aucune fuite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
