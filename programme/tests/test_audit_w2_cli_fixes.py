"""Audit vague 2, voie F : CLI, exceptions, revue, anciens repertoires.

Donnees factices seulement, repertoire de donnees dans tmp, aucun modele.
"""

import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from caviardoc import cli, desinstallation, paths, revue  # noqa: E402
from caviardoc.exceptions import Repertoire  # noqa: E402
from test_cli_caviarder import _args, _sans_modele  # noqa: E402
from test_docx import AVS, _docx, _p, _t  # noqa: E402


@pytest.fixture(autouse=True)
def _donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))


# --- 1. fichier d'exceptions abime ---------------------------------------

def test_exceptions_abimees_signalees_et_conservees():
    rep = Repertoire.charger()
    assert rep.retenir_toujours("Zorglub Martin")[0]
    chemin = rep.enregistrer()
    abime = chemin.read_bytes()[:20]
    chemin.write_bytes(abime)

    rep2 = Repertoire.charger()
    assert rep2.degat, "aucun signal de degat"
    # Apprendre autre chose ne doit pas detruire silencieusement l'ancien contenu.
    rep2.retenir_toujours("Autre Nom")
    rep2.enregistrer()
    sauvegardes = [p for p in chemin.parent.iterdir()
                   if p.name.startswith("exceptions.json.abime")]
    assert sauvegardes and sauvegardes[0].read_bytes() == abime


@pytest.mark.parametrize("contenu", [
    {"sel": "s", "toujours": [{"empreinte": "e" * 8, "mots": "abc"}]},
    {"sel": "s", "jamais": [{"chaine": "mot", "inconnue": 1}]},
], ids=["toujours-mots-illisible", "jamais-cle-inconnue"])
def test_exceptions_entree_illisible_ne_leve_pas(contenu):
    chemin = Repertoire.chemin()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(contenu), encoding="utf-8")
    original = chemin.read_bytes()

    rep = Repertoire.charger()
    assert rep.degat.startswith("entree illisible")

    rep.enregistrer()
    sauvegardes = [p for p in chemin.parent.iterdir()
                   if p.name.startswith("exceptions.json.abime")]
    assert sauvegardes and sauvegardes[0].read_bytes() == original


def test_exceptions_absentes_ne_sont_pas_un_degat():
    assert not Repertoire.charger().degat


def test_exceptions_sans_sel_sont_un_degat():
    chemin = Repertoire.chemin()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text('{"toujours": []}', encoding="utf-8")
    assert Repertoire.charger().degat


# --- 2. le CLI rejoue les « toujours » -----------------------------------

def test_cli_rejoue_les_toujours(tmp_path, monkeypatch):
    _sans_modele(monkeypatch)
    rep = Repertoire.charger()
    assert rep.retenir_toujours("Zorglub")[0]
    rep.enregistrer()
    source = tmp_path / "doc.docx"
    source.write_bytes(_docx(_p(_t("Patient Zorglub vu"))))
    dest = tmp_path / "sortie"

    cli.cmd_caviarder(_args([source], dest))

    produit = next(dest.iterdir())
    with zipfile.ZipFile(produit) as z:
        assert "Zorglub" not in z.read("word/document.xml").decode("utf-8")


# --- 3. renommage differe perdu en sortie anticipee ----------------------

def test_renommage_apres_interruption(tmp_path, monkeypatch):
    _sans_modele(monkeypatch)
    from caviardoc.documents import docx as mod_docx
    vraie = mod_docx.caviarder_docx
    appels = []

    def espion(*a, **kw):
        appels.append(1)
        if len(appels) == 2:
            raise KeyboardInterrupt
        return vraie(*a, **kw)

    monkeypatch.setattr(mod_docx, "caviarder_docx", espion)
    un = tmp_path / f"rapport_{AVS}.docx"
    un.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))
    deux = tmp_path / "autre.docx"
    deux.write_bytes(_docx(_p(_t("x"))))
    dest = tmp_path / "sortie"

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_caviarder(_args([un, deux], dest))

    assert all(AVS not in p.name for p in dest.iterdir())


def test_pdf_vide_n_interrompt_pas_le_lot(tmp_path, monkeypatch):
    _sans_modele(monkeypatch)
    vide = tmp_path / "vide.pdf"
    vide.write_bytes(b"")
    un = tmp_path / f"rapport_{AVS}.docx"
    un.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))
    dest = tmp_path / "sortie"

    code = cli.cmd_caviarder(_args([vide, un], dest))

    assert code == 1
    assert all(AVS not in p.name for p in dest.iterdir())
    assert len(list(dest.glob("*.docx"))) == 1


# --- 4. session de revue supprimee apres application ---------------------

def test_session_revue_supprimee(tmp_path, monkeypatch):
    from caviardoc import revue_web
    _sans_modele(monkeypatch)
    monkeypatch.setattr(revue_web, "servir",
                        lambda session, textes, **kw: session)
    source = tmp_path / "rapport.docx"
    source.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))
    cli.cmd_caviarder(_args([source], tmp_path / "out", revue=True))
    assert list(revue.dossier_sessions().glob("*.json")) == []


# --- 5. sequences de terminal --------------------------------------------

def test_aucun_octet_de_controle_dans_le_rapport(tmp_path, monkeypatch, capsys):
    _sans_modele(monkeypatch)
    from caviardoc.documents import docx as mod_docx
    piege = SimpleNamespace(texte="http://x/\x1b[2Aeffacer", etiquette="url",
                            pages=[], raison="")
    rapport = SimpleNamespace(
        parties=[], noms_patient=set(), repli_complet=False, corrigees=[piege],
        corrigees_texte=[], fuites_texte=[], fuites=[],
        erreur_verification="", verifie=True)
    monkeypatch.setattr(mod_docx, "caviarder_docx", lambda *a, **kw: rapport)
    monkeypatch.setattr(cli, "_afficher_docx", lambda r: None)
    source = tmp_path / "d.docx"
    source.write_bytes(_docx(_p(_t("x"))))

    cli.cmd_caviarder(_args([source], tmp_path / "out"))

    sortie = capsys.readouterr().out
    assert "\x1b" not in sortie and "\r" not in sortie


# --- 6. plusieurs anciens repertoires ------------------------------------

def _machine(tmp_path, monkeypatch):
    racine = tmp_path / "machine"
    racine.mkdir()
    monkeypatch.delenv(paths.ENV_DATA_DIR, raising=False)
    nouveau = racine / "caviardoc"
    monkeypatch.setattr(paths, "data_dir", lambda: nouveau)
    for nom in paths.ANCIENS_APP_NAMES:
        (racine / nom).mkdir()
        (racine / nom / "exceptions.json").write_text("{}", encoding="utf-8")
    return racine


def test_tous_les_anciens_repertoires_sont_inventories(tmp_path, monkeypatch):
    _machine(tmp_path, monkeypatch)
    # Seuls les elements « ancien* » sont gardes : rien d'autre n'est examine.
    anciens = [e for e in desinstallation.inventaire()
               if e.cle.startswith("ancien")]
    assert {e.chemin.name for e in anciens} == set(paths.ANCIENS_APP_NAMES)
    assert all(tmp_path in e.chemin.parents for e in anciens)


def test_tous_les_anciens_repertoires_sont_supprimes(tmp_path, monkeypatch):
    racine = _machine(tmp_path, monkeypatch)
    anciens = [e for e in desinstallation.inventaire()
               if e.cle.startswith("ancien")]
    assert all(tmp_path in e.chemin.parents for e in anciens)
    assert desinstallation.supprimer(anciens) == []
    for nom in paths.ANCIENS_APP_NAMES:
        assert not (racine / nom).exists()


# --- (a) temporaires OCR confines et nettoyes ------------------------------

def test_temporaires_confines_pendant_le_lot_puis_nettoyes(tmp_path, monkeypatch):
    import tempfile
    from caviardoc.documents import docx as mod_docx
    _sans_modele(monkeypatch)
    avant = tempfile.tempdir
    vus = []
    vraie = mod_docx.caviarder_docx

    def espion(*a, **kw):
        vus.append(Path(tempfile.tempdir))
        assert vus[-1].is_dir()
        return vraie(*a, **kw)

    monkeypatch.setattr(mod_docx, "caviarder_docx", espion)
    source = tmp_path / "d.docx"
    source.write_bytes(_docx(_p(_t("x"))))

    cli.cmd_caviarder(_args([source], tmp_path / "out"))

    assert vus and vus[0] != (Path(avant) if avant else None)
    assert not vus[0].exists(), "le dossier de travail doit etre nettoye"
    assert tempfile.tempdir == avant


# --- (b) telemetrie coupee au demarrage ------------------------------------

def test_telemetrie_coupee_au_demarrage(monkeypatch):
    from caviardoc import web_commun
    appels = []
    monkeypatch.setattr(web_commun, "desactiver_telemetrie",
                        lambda: appels.append(1))
    cli.main(["info"])
    assert appels
