"""Corpus de regression : integrite, et couche deterministe seule.

La mesure complete demande le modele — 350 Mo — et vit dans `tools/mesurer.py`.
Ces tests-ci tournent partout, sans modele, et couvrent deux choses qu'il ne
faut pas laisser deriver :

- **l'integrite du corpus** : une entite ou une chaine a preserver qui ne
  figure plus dans son texte rend la mesure fausse sans rien casser ;
- **la couche 1 seule** : ce que les regles deterministes doivent trouver, sans
  aucune aide du modele. Une somme de controle ne devrait jamais regresser.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc.pipeline import analyse  # noqa: E402

CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "corpus.json").read_text(encoding="utf-8"))
DOCUMENTS = CORPUS["documents"]

#: Types que la couche 1 reconnait seule, par somme de controle ou par forme.
TYPES_DETERMINISTES = {"numero AVS", "IBAN", "numero de telephone", "email",
                       "date de naissance"}


def _ids():
    return [d["id"] for d in DOCUMENTS]


class TestIntegrite:
    """Un corpus incoherent produit une mesure fausse sans rien signaler."""

    @pytest.mark.parametrize("doc", DOCUMENTS, ids=_ids())
    def test_les_entites_figurent_dans_le_texte(self, doc):
        for e in doc["entites"]:
            assert e["texte"] in doc["texte"], f'{doc["id"]} : {e["texte"]!r}'

    @pytest.mark.parametrize("doc", DOCUMENTS, ids=_ids())
    def test_les_chaines_a_preserver_figurent_dans_le_texte(self, doc):
        for s in doc["preserver"]:
            assert s in doc["texte"], f'{doc["id"]} : {s!r}'

    @pytest.mark.parametrize("doc", DOCUMENTS, ids=_ids())
    def test_chaque_entite_porte_un_role_connu(self, doc):
        for e in doc["entites"]:
            assert e["role"] in ("patient", "soignant", "etablissement")

    def test_chaque_document_a_un_patient(self):
        """Sans patient, le niveau « patient » n'a rien a mesurer."""
        for d in DOCUMENTS:
            assert any(e["role"] == "patient" for e in d["entites"]), d["id"]

    def test_le_corpus_est_fabrique(self):
        """Garde-fou : le corpus ne doit jamais accueillir de donnee reelle."""
        assert "fabrique" in CORPUS["avertissement"].lower()


class TestCoucheDeterministe:
    """Sans modele, les regles doivent tenir seules leur part."""

    @pytest.mark.parametrize("doc", DOCUMENTS, ids=_ids())
    def test_les_identifiants_structures_sont_trouves(self, doc):
        spans = analyse(None, doc["texte"])
        for e in doc["entites"]:
            if e["type"] not in TYPES_DETERMINISTES:
                continue
            i = doc["texte"].find(e["texte"])
            f = i + len(e["texte"])
            assert any(sp.start < f and i < sp.end for sp in spans), \
                f'{doc["id"]} : {e["type"]} {e["texte"]!r} manque a la couche 1'

    @pytest.mark.parametrize("doc", DOCUMENTS, ids=_ids())
    def test_les_regles_seules_ne_detruisent_rien(self, doc):
        """La couche deterministe ne doit toucher aucun contenu clinique."""
        spans = analyse(None, doc["texte"])
        for s in doc["preserver"]:
            i = doc["texte"].find(s)
            f = i + len(s)
            touche = [sp for sp in spans if sp.start < f and i < sp.end]
            assert not touche, f'{doc["id"]} : {s!r} masque par {touche}'


class TestGardeFouMemoire:
    """Le plancher memoire ne doit pas confondre cible et minimum vital."""

    def test_une_machine_de_7_go_peut_installer(self):
        """Cas reel : un runner macOS de 7 Go se voyait refuser l'installation
        alors que le moteur occupe 1 Go."""
        from caviardoc.doctor import MIN_RAM_GB
        assert MIN_RAM_GB <= 7

    def test_le_plancher_laisse_place_au_moteur(self):
        from caviardoc.doctor import MIN_RAM_GB
        assert MIN_RAM_GB >= 2, "le moteur occupe environ 1 Go"
