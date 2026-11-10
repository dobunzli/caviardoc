"""Mesure du moteur sur le corpus de regression.

Ce corpus est entierement fabrique, avec des noms inventes : il reproduit
des *formes* de documents medicaux, sans aucune donnee de personne reelle.

Trois grandeurs sont rendues, et elles ne disent pas la meme chose :

- **rappel** : part des entites attendues qui sont effectivement masquees.
  C'est ce qui protege le patient.
- **preservation** : part des chaines qui devaient rester lisibles et le sont.
  C'est ce qui rend le document encore exploitable.
- **soignants preserves** : au niveau « patient » seulement, part des noms de
  soignants laisses en clair, comme ce niveau le promet.

Usage :
    python tools/mesurer.py            # affiche le tableau
    python tools/mesurer.py --seuils   # echoue si un seuil est franchi
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.engine import load_model  # noqa: E402
from caviardoc.pipeline import PolitiqueDates, _spans_bruts, analyse  # noqa: E402
from caviardoc.roles import Niveau, filtrer_par_role, noms_du_patient  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "corpus" / "corpus.json"

#: Seuils de non-regression. **Un seul jeu**, pour toutes les architectures.
#:
#: Ils etaient scindes par famille de processeur, et cette scission etait un
#: constat d'echec : sur x86-64 ils descendaient jusqu'a 10 % de preservation,
#: non pour valider quoi que ce soit mais pour enregistrer un defaut sans
#: faire echouer la mesure.
#:
#: La campagne du 2026-09-06 a leve le defaut, en montrant d'ou il venait.
#: A code, corpus et versions identiques :
#:
#:                             int8            fp16
#:   arm64  · runner GitHub    62,2 %          97,3 %
#:   arm64  · poste M4 Pro     62,2 %          97,3 %
#:   x86-64 · runner GitHub    51,4 %          97,3 %
#:
#: fp16 rend le meme chiffre partout, au dixieme pres. La dependance au
#: processeur n'etait donc une propriete ni du modele, ni du systeme, ni
#: d'onnxruntime — ecartes un par un — mais **de la quantification entiere
#: elle-meme** : les noyaux entiers choisissent leur implementation selon les
#: capacites du processeur, et le seuil de detection volontairement bas
#: transforme un ecart numerique modeste en basculement de detection. En
#: flottant, il n'y a plus d'ecart a transformer.
#:
#: fp16 etant devenu le modele par defaut, un seuil unique redevient possible,
#: et redevient ce qu'un seuil doit etre : une porte, et non un proces-verbal.
#: Mesurer int8 reste possible — `--modele gliner-multi-pii-int8` — et le
#: montrera sous ces seuils, ce qui est exactement le propos.
SEUILS = {
    "rappel_complet": 0.90,
    "rappel_patient": 0.90,
    "preservation_complet": 0.80,
    "preservation_patient": 0.85,
    "soignants_preserves": 0.50,
}


#: Modele effectivement mesure, pour que l'empreinte le nomme.
_modele_mesure: str | None = None


def empreinte_environnement() -> str:
    """Ce qui pourrait expliquer un ecart de mesure d'une machine a l'autre.

    Les premieres mesures en integration continue ont diverge fortement de
    celles obtenues en developpement — sans que la version de Python, le
    nombre de fils d'execution ni la version d'onnxruntime l'expliquent. Tant
    que la cause n'est pas trouvee, chaque mesure publie son environnement :
    une divergence sans contexte n'est pas exploitable.
    """
    import hashlib
    import importlib.metadata as meta
    import platform

    from caviardoc import models as reg

    lignes = [f"machine      : {platform.machine()} · {platform.system()}",
              f"python       : {platform.python_version()}"]
    for paquet in ("onnxruntime", "gliner", "transformers", "tokenizers", "torch"):
        try:
            lignes.append(f"{paquet:12s} : {meta.version(paquet)}")
        except meta.PackageNotFoundError:
            lignes.append(f"{paquet:12s} : absent")

    spec = reg.REGISTRY[_modele_mesure or reg.modele_actif()]
    onnx = spec.local_dir() / reg.FICHIER_ONNX[spec.key]
    lignes.append(f"modele cle   : {spec.key}")
    if onnx.is_file():
        h = hashlib.sha256()
        with onnx.open("rb") as fh:
            for bloc in iter(lambda: fh.read(1 << 20), b""):
                h.update(bloc)
        attendu = next(f.sha256 for f in spec.files
                       if f.name == reg.FICHIER_ONNX[spec.key])
        conforme = "conforme" if h.hexdigest() == attendu else "NON CONFORME"
        lignes.append(f"modele       : {h.hexdigest()[:16]}… {conforme}")
    else:
        lignes.append("modele       : ABSENT")
    return "\n".join("  " + l for l in lignes)


def architecture() -> str:
    """Famille d'architecture. Affichee, mais ne choisit plus les seuils."""
    import platform
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x86_64"


@dataclass
class Bilan:
    attendus: int = 0
    masques: int = 0
    a_preserver: int = 0
    preserves: int = 0
    soignants: int = 0
    soignants_preserves: int = 0
    manques: list[str] = None
    detruits: list[str] = None

    def __post_init__(self):
        self.manques = self.manques or []
        self.detruits = self.detruits or []

    @property
    def rappel(self) -> float:
        return self.masques / self.attendus if self.attendus else 1.0

    @property
    def preservation(self) -> float:
        return self.preserves / self.a_preserver if self.a_preserver else 1.0

    @property
    def taux_soignants(self) -> float:
        return (self.soignants_preserves / self.soignants
                if self.soignants else 1.0)


def _couvert(texte: str, aiguille: str, spans) -> bool:
    """Toutes les occurrences de l'aiguille sont-elles masquees ?"""
    depart = 0
    vues = 0
    while True:
        i = texte.find(aiguille, depart)
        if i == -1:
            break
        vues += 1
        f = i + len(aiguille)
        if not any(sp.start < f and i < sp.end for sp in spans):
            return False
        depart = i + 1
    return vues > 0


def _touche(texte: str, aiguille: str, spans) -> bool:
    """Au moins une occurrence de l'aiguille est-elle masquee ?"""
    depart = 0
    while True:
        i = texte.find(aiguille, depart)
        if i == -1:
            return False
        f = i + len(aiguille)
        if any(sp.start < f and i < sp.end for sp in spans):
            return True
        depart = i + 1


def evaluer(model, document: dict, niveau: Niveau) -> Bilan:
    texte = document["texte"]
    dates = (PolitiqueDates.MASQUER if niveau is Niveau.COMPLET
             else PolitiqueDates.GARDER)
    spans = analyse(model, texte, dates=dates)
    if niveau is not Niveau.COMPLET:
        noms = noms_du_patient(texte, _spans_bruts(model, texte, dates=dates) + spans)
        if noms:
            spans = filtrer_par_role(texte, spans, niveau, noms)

    b = Bilan()
    for e in document["entites"]:
        doit_masquer = (niveau is Niveau.COMPLET or e["role"] == "patient")
        if doit_masquer:
            b.attendus += 1
            if _couvert(texte, e["texte"], spans):
                b.masques += 1
            else:
                b.manques.append(f'{document["id"]}: {e["texte"]!r}')
        elif e["role"] == "soignant":
            b.soignants += 1
            if not _touche(texte, e["texte"], spans):
                b.soignants_preserves += 1

    for s in document["preserver"]:
        b.a_preserver += 1
        if not _touche(texte, s, spans):
            b.preserves += 1
        else:
            b.detruits.append(f'{document["id"]}: {s!r}')
    return b


def cumuler(bilans: list[Bilan]) -> Bilan:
    total = Bilan()
    for b in bilans:
        total.attendus += b.attendus
        total.masques += b.masques
        total.a_preserver += b.a_preserver
        total.preserves += b.preserves
        total.soignants += b.soignants
        total.soignants_preserves += b.soignants_preserves
        total.manques += b.manques
        total.detruits += b.detruits
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seuils", action="store_true",
                    help="echouer si un seuil de non-regression est franchi")
    ap.add_argument("--details", action="store_true",
                    help="lister les entites manquees et le contenu detruit")
    ap.add_argument("--enregistrer", action="store_true",
                    help="enregistrer la mesure, et retenir le modele mesure "
                         "pour cette machine — seulement s'il tient les seuils")
    ap.add_argument("--modele", default=None,
                    help="cle du modele a mesurer (defaut : celui retenu sur "
                         "cette machine, donc celui que `caviarder` utilisera)")
    args = ap.parse_args()

    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    documents = corpus["documents"]
    global _modele_mesure
    _modele_mesure = args.modele
    print(empreinte_environnement())
    print(f"\n  architecture : {architecture()}\n")
    import time
    debut = time.time()
    print("  chargement du modele...", flush=True)
    model = load_model(args.modele)
    print(f"  modele charge en {time.time() - debut:.0f} s", flush=True)

    resultats: dict[Niveau, Bilan] = {}
    print(f"{'document':34s} {'complet':>18s} {'patient':>18s}")
    print(f"{'':34s} {'rappel  preserv.':>18s} {'rappel  preserv.':>18s}")
    print("-" * 72)
    par_doc = {n: [] for n in Niveau}
    for d in documents:
        # Annonce avant de calculer : sur une machine lente, la ligne du
        # tableau n'arrive qu'apres plusieurs minutes, et l'on ne saurait pas
        # si la mesure travaille ou est bloquee.
        print(f"  analyse de {d['id'][:32]}...", flush=True)
        t_doc = time.time()
        ligne = f"{d['id'][:32]:34s}"
        for niveau in (Niveau.COMPLET, Niveau.PATIENT):
            b = evaluer(model, d, niveau)
            par_doc[niveau].append(b)
            ligne += f" {b.rappel*100:6.0f}% {b.preservation*100:8.0f}%"
        print(f"{ligne}   ({time.time() - t_doc:.0f} s)", flush=True)
    print("-" * 72)

    for niveau in (Niveau.COMPLET, Niveau.PATIENT):
        resultats[niveau] = cumuler(par_doc[niveau])

    c, p = resultats[Niveau.COMPLET], resultats[Niveau.PATIENT]
    mesures = {
        "rappel_complet": c.rappel,
        "preservation_complet": c.preservation,
        "rappel_patient": p.rappel,
        "preservation_patient": p.preservation,
        "soignants_preserves": p.taux_soignants,
    }
    print()
    for nom, valeur in mesures.items():
        seuil = SEUILS[nom]
        etat = "ok " if valeur >= seuil else "BAS"
        print(f"  {etat} {nom:24s} {valeur*100:5.1f} %   (seuil {seuil*100:.0f} %)")

    if args.details:
        for niveau in (Niveau.COMPLET, Niveau.PATIENT):
            b = resultats[niveau]
            if b.manques:
                print(f"\n  entites manquees ({niveau.value}) :")
                for m in b.manques:
                    print(f"    {m}")
            if b.detruits:
                print(f"\n  contenu detruit ({niveau.value}) :")
                for m in b.detruits:
                    print(f"    {m}")

    sous = [n for n, v in mesures.items() if v < SEUILS[n]]

    if args.enregistrer:
        return _enregistrer(mesures, sous)

    if args.seuils:
        if sous:
            print(f"\nREGRESSION : {', '.join(sous)}")
            return 1
        print("\nTous les seuils sont tenus.")
    return 0


def _enregistrer(mesures: dict, sous: list[str]) -> int:
    """Enregistre la mesure, et ne retient le modele que s'il a tenu.

    C'est le maillon qui manquait entre « mesurer » et « faire confiance ».
    Une machine pouvait pointer vers un modele qui n'y avait jamais ete
    mesure, et rien ne le signalait : le chiffre affiche par la documentation
    n'engageait qu'elle.
    """
    from caviardoc import models as reg

    cle = _modele_mesure or reg.modele_actif()
    tenu = not sous
    reg.noter_mesure(cle, mesures, tenu,
                     corpus=hashlib.sha256(CORPUS.read_bytes()).hexdigest())
    print()

    if not tenu:
        print(f"  Mesure enregistree, et elle ne tient pas : "
              f"{', '.join(sous)}.")
        print(f"  {cle} n'est PAS retenu pour cette machine.")
        if cle == reg.modele_actif():
            print("  Or c'est le modele actuellement retenu ici : ne confiez "
                  "aucun document")
            print("  a cette machine avant d'avoir compris pourquoi.")
        else:
            print(f"  Le modele retenu reste {reg.modele_actif()}.")
        return 1

    ancien = reg.modele_actif()
    reg.definir_modele_actif(cle)
    print("  Mesure enregistree, tous les seuils tenus.")
    if cle != ancien:
        print(f"  Modele retenu pour cette machine : {cle} (etait {ancien}).")
    else:
        print(f"  Modele retenu confirme : {cle}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
