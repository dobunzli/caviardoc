"""Le modele retenu est une propriete de la machine, pas du depot.

int8 rend des resultats dependants du processeur. Le choix du modele est donc
mesure machine par machine, puis enregistre dans le repertoire de donnees
local. Ces tests verifient qu'il survit, qu'il est partage par toutes les
commandes, et qu'un etat abime ne fait jamais charger un modele absent.
"""

from __future__ import annotations

import json

import pytest

from caviardoc import models


@pytest.fixture
def donnees(tmp_path, monkeypatch):
    """Repertoire de donnees isole, pour ne pas toucher celui de la machine."""
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    return tmp_path


def test_defaut_sans_etat(donnees):
    assert models.modele_actif() == models.DEFAULT_MODEL


def test_le_choix_persiste(donnees):
    autre = next(k for k in models.REGISTRY if k != models.DEFAULT_MODEL)
    models.definir_modele_actif(autre)
    assert models.modele_actif() == autre


def test_le_choix_n_ecrase_pas_le_reste_de_l_etat(donnees):
    etat = donnees / "state.json"
    etat.parent.mkdir(parents=True, exist_ok=True)
    etat.write_text(json.dumps({"autre_clef": 42}), encoding="utf-8")

    models.definir_modele_actif(models.DEFAULT_MODEL)

    relu = json.loads(etat.read_text(encoding="utf-8"))
    assert relu["autre_clef"] == 42
    assert relu[models.CLE_ETAT_MODELE] == models.DEFAULT_MODEL


def test_modele_inconnu_refuse(donnees):
    with pytest.raises(KeyError):
        models.definir_modele_actif("gliner-inexistant")
    assert models.modele_actif() == models.DEFAULT_MODEL


@pytest.mark.parametrize("contenu", [
    "{ceci n'est pas du json",
    '"une chaine, pas un objet"',
    "[]",
])
def test_etat_abime_retombe_sur_le_defaut(donnees, contenu):
    """Un etat illisible ne doit pas empecher l'outil de fonctionner."""
    etat = donnees / "state.json"
    etat.parent.mkdir(parents=True, exist_ok=True)
    etat.write_text(contenu, encoding="utf-8")
    assert models.modele_actif() == models.DEFAULT_MODEL


def test_modele_disparu_du_registre_retombe_sur_le_defaut(donnees):
    """Un etat ecrit par une version anterieure peut nommer un modele retire."""
    etat = donnees / "state.json"
    etat.parent.mkdir(parents=True, exist_ok=True)
    etat.write_text(json.dumps({models.CLE_ETAT_MODELE: "gliner-retire"}),
                    encoding="utf-8")
    assert models.modele_actif() == models.DEFAULT_MODEL


def test_doctor_controle_le_modele_retenu(donnees):
    """Sans cela, `doctor` validerait un modele que `caviarder` n'utilise pas."""
    from caviardoc import doctor

    autre = next(k for k in models.REGISTRY if k != models.DEFAULT_MODEL)
    models.definir_modele_actif(autre)

    controle = next(ch for ch in doctor.run_all()
                    if ch.name == "Modele de detection")
    assert autre in controle.detail


def test_caviarder_accepte_un_modele_ponctuel():
    """`--modele` doit exister sur la commande qui traite les documents."""
    from caviardoc.cli import build_parser

    autre = next(k for k in models.REGISTRY if k != models.DEFAULT_MODEL)
    args = build_parser().parse_args(
        ["caviarder", "doc.pdf", "-o", "sortie", "--modele", autre])
    assert args.modele == autre


def test_setup_accepte_les_deux_orthographes():
    """`--model` figure dans la documentation deja diffusee."""
    from caviardoc.cli import build_parser

    autre = next(k for k in models.REGISTRY if k != models.DEFAULT_MODEL)
    for drapeau in ("--modele", "--model"):
        args = build_parser().parse_args(["setup", drapeau, autre])
        assert args.modele == autre


def test_setup_sans_drapeau_ne_force_rien():
    """None, et non le defaut : `setup` doit conserver le choix de la machine."""
    from caviardoc.cli import build_parser

    assert build_parser().parse_args(["setup"]).modele is None
