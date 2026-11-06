"""Banc de mesure : compare la detection automatique a un caviardage manuel de reference.

La verite terrain est deduite par difference : ce qui figure dans l'original et
a disparu de la copie caviardee est ce qu'un humain a juge devoir etre masque.

Usage : python tools/evaluer.py <dossier>
Les paires sont reperees par le suffixe « copie » sur le fichier caviarde.
Aucun contenu n'est ecrit sur disque ; seules des statistiques sont affichees.
"""

from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pymupdf  # noqa: E402

from caviardoc.engine import load_model  # noqa: E402
from caviardoc.pipeline import analyse  # noqa: E402

MOT = re.compile(r"\S+")


def texte(path: Path) -> str:
    d = pymupdf.open(path)
    try:
        return "\n".join(p.get_text() for p in d)
    finally:
        d.close()


def mots(t: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group()) for m in MOT.finditer(t)]


def verite_terrain(original: str, caviarde: str) -> list[tuple[int, int, str]]:
    """Mots presents dans l'original et absents de la copie caviardee."""
    a, b = mots(original), mots(caviarde)
    sm = difflib.SequenceMatcher(None, [w for _, _, w in a], [w for _, _, w in b],
                                 autojunk=False)
    supprimes: list[tuple[int, int, str]] = []
    for tag, i1, i2, _, _ in sm.get_opcodes():
        if tag in ("delete", "replace"):
            supprimes.extend(a[i1:i2])
    return supprimes


@dataclass
class Resultat:
    nom: str
    attendus: int = 0
    couverts: int = 0
    detectes: int = 0
    hors_reference: list[str] = field(default_factory=list)
    manques: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def rappel(self) -> float:
        return self.couverts / self.attendus if self.attendus else float("nan")


def evaluer(model, orig: Path, cav: Path) -> Resultat:
    to, tc = texte(orig), texte(cav)
    r = Resultat(nom=orig.stem)

    if len(to.strip()) < 100:
        r.note = "aucune couche texte — hors perimetre de cette mesure"
        return r
    if to == tc:
        r.note = "copie identique a l'original — caviardage jamais applique"
        return r

    attendus = verite_terrain(to, tc)
    # Ne garder que les suppressions porteuses d'information : un mot d'au
    # moins deux caracteres contenant une lettre ou un chiffre.
    attendus = [(s, e, w) for s, e, w in attendus
                if len(w) >= 2 and any(c.isalnum() for c in w)]
    r.attendus = len(attendus)

    spans = analyse(model, to)
    r.detectes = len(spans)
    couvert = [False] * len(to)
    for sp in spans:
        for i in range(sp.start, min(sp.end, len(to))):
            couvert[i] = True

    for s, e, w in attendus:
        # Un mot est considere couvert si la majorite de ses caracteres le sont.
        n = sum(1 for i in range(s, min(e, len(to))) if couvert[i])
        if n >= (e - s) * 0.6:
            r.couverts += 1
        else:
            r.manques.append(w)
    return r


def main(dossier: str) -> int:
    d = Path(dossier).expanduser()
    paires = []
    for cav in sorted(d.glob("* copie.pdf")):
        orig = cav.with_name(cav.name.replace(" copie.pdf", ".pdf"))
        if orig.exists():
            paires.append((orig, cav))
    if not paires:
        print("aucune paire original/copie trouvee")
        return 1

    model = load_model()
    resultats = [evaluer(model, o, c) for o, c in paires]

    print(f"\n{'document':46s} {'attendus':>9s} {'couverts':>9s} {'rappel':>8s}  note")
    print("-" * 104)
    tot_a = tot_c = 0
    for r in resultats:
        court = re.sub(r"^\d+_[^_]+_[^_]+_", "", r.nom)[:44]
        if r.note:
            print(f"{court:46s} {'-':>9s} {'-':>9s} {'-':>8s}  {r.note}")
            continue
        tot_a += r.attendus
        tot_c += r.couverts
        print(f"{court:46s} {r.attendus:9d} {r.couverts:9d} {r.rappel*100:7.0f}%")
    print("-" * 104)
    if tot_a:
        print(f"{'TOTAL':46s} {tot_a:9d} {tot_c:9d} {tot_c/tot_a*100:7.0f}%")

    print("\n=== MOTS ATTENDUS ET NON COUVERTS ===")
    for r in resultats:
        if r.manques:
            court = re.sub(r"^\d+_[^_]+_[^_]+_", "", r.nom)[:44]
            apercu = ", ".join(repr(m) for m in r.manques[:14])
            suite = f" … (+{len(r.manques)-14})" if len(r.manques) > 14 else ""
            print(f"  {court}\n    {apercu}{suite}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "~/Desktop"))
