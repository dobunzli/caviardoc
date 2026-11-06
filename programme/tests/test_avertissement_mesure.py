"""Une machine dont la mesure n'est pas tenue avertit, sans rien bloquer.

Bandeau rouge dans la console, drapeau dans le bilan de chaque document,
avertissement unique en tete de `caviarder`. Donnees fictives, repertoire de
donnees temporaire, aucun lancement de la vraie mesure.
"""

import html
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from caviardoc import cli, doctor, engine, installateur, interface, models, paths  # noqa: E402

FLAG = "machine sous le seuil de détection mesuré"


@pytest.fixture
def donnees(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path / "donnees"))
    return tmp_path


def _noter(tenu, rappel=0.62):
    models.noter_mesure(models.modele_actif(),
                        {"rappel_complet": rappel, "rappel_patient": rappel},
                        tenu)


def test_bandeau_present_si_jamais_mesure(donnees):
    page = interface.page_console("jeton", "m", initial="app")
    assert 'id="bandeau-mesure"' in page
    page = html.unescape(page)
    assert "jamais été mesurée" in page
    assert "relancer la mesure dans l'onglet Installation" in page


def test_bandeau_present_sous_le_seuil_avec_le_rappel(donnees):
    _noter(False, 0.62)
    for page in (interface.page_console("j", "m", initial="app"),
                 interface.page_html("j", "m")):
        assert 'id="bandeau-mesure"' in page
        assert "62.0 %" in page
        assert "relancer la mesure dans l'onglet Installation" in html.unescape(page)


def test_bandeau_absent_si_la_mesure_est_tenue(donnees):
    _noter(True, 0.97)
    assert 'id="bandeau-mesure"' not in interface.page_console("j", "m", initial="app")
    assert 'id="bandeau-mesure"' not in interface.page_html("j", "m")


def _traiter(donnees, monkeypatch):
    from test_docx import AVS, _docx, _p, _t
    atelier = interface.Atelier(racine=donnees)
    monkeypatch.setattr(atelier, "modele", lambda: None)
    octets = _docx(_p(_t("Assure "), _t(AVS)))
    depot = interface.deposer(atelier, "cr.docx", io.BytesIO(octets), len(octets))
    return interface.traiter(atelier, depot.identifiant,
                             {"format": "texte", "revue": "aucune"}), atelier


def test_document_traite_sous_le_seuil_porte_le_drapeau(donnees, monkeypatch):
    _noter(False)
    bilan, atelier = _traiter(donnees, monkeypatch)
    assert FLAG in bilan["sous_seuil"]
    assert bilan["produits"], "le traitement n'est pas bloque"
    assert all(p.a_verifier for p in atelier.productions.values())
    assert interface.a_verifier(bilan)


def test_document_traite_machine_tenue_sans_drapeau(donnees, monkeypatch):
    _noter(True, 0.97)
    bilan, _ = _traiter(donnees, monkeypatch)
    assert bilan["sous_seuil"] is None


def test_caviarder_avertit_une_fois_et_continue(donnees, monkeypatch, capsys):
    from test_cli_caviarder import _args
    from test_docx import AVS, _docx, _p, _t
    monkeypatch.setattr(engine, "load_model", lambda *a, **kw: None)
    _noter(False, 0.62)
    fichiers = []
    for nom in ("a.docx", "b.docx"):
        f = donnees / nom
        f.write_bytes(_docx(_p(_t("Assure "), _t(AVS))))
        fichiers.append(f)
    code = cli.cmd_caviarder(_args(fichiers, donnees / "sortie"))
    sortie = capsys.readouterr().out
    assert code == 0
    assert sortie.count("ATTENTION") == 1
    assert "62.0 %" in sortie
    assert len(list((donnees / "sortie").iterdir())) == 2


def test_interface_demarre_malgre_une_mesure_sous_le_seuil(donnees, monkeypatch):
    """Seul le controle de mesure ne bloque plus ; un autre FAIL bloque encore."""
    mesure = doctor.Check("Mesure du rappel", doctor.Level.FAIL, "bas")
    autre = doctor.Check("Autre", doctor.Level.FAIL, "casse")
    appels = []
    monkeypatch.setattr(interface, "servir", lambda **k: appels.append(k))
    monkeypatch.setattr(doctor, "run_all", lambda *a, **k: [mesure])
    args = cli.build_parser().parse_args(["interface", "--sans-navigateur"])
    assert cli.cmd_interface(args) == 0 and appels
    monkeypatch.setattr(doctor, "run_all", lambda *a, **k: [mesure, autre])
    assert cli.cmd_interface(args) == 1


class _FauxProcessus:
    def __init__(self, sortie, code):
        self.stdout = iter(sortie)
        self.returncode = code

    def wait(self):
        pass


def _mesure_guidee(monkeypatch, tmp_path, rappel, code):
    """Rejoue la mesure guidee avec un faux sous-processus qui, comme le vrai
    `mesurer.py --enregistrer`, enregistre la mesure."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "mesurer.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor, "_venv_python", lambda _v: Path(sys.executable))
    commandes = []

    def faux_popen(cmd, **kw):
        commandes.append(cmd)
        if "--enregistrer" in cmd:
            models.noter_mesure(models.modele_actif(),
                                {"rappel_complet": rappel / 100,
                                 "rappel_patient": rappel / 100}, code == 0)
        return _FauxProcessus(
            [f"  rappel_complet   {rappel:.1f} %\n",
             f"  rappel_patient   {rappel:.1f} %\n"], code)

    monkeypatch.setattr(installateur.subprocess, "Popen", faux_popen)
    etape = installateur.Etape("mesure", "Mesure", "")
    installateur.etape_mesure(etape)
    return etape, commandes


def test_mesure_guidee_enregistre_un_echec(donnees, monkeypatch):
    etape, commandes = _mesure_guidee(monkeypatch, donnees, 62.0, 1)
    assert "--enregistrer" in commandes[0]
    assert etape.etat == "manuelle"
    mesure = models.derniere_mesure()
    assert mesure["tenu"] is False and mesure["rappel_complet"] == 62.0
    assert mesure["date"]
    assert doctor.avertissement_mesure()["rappel"] == 62.0


def test_mesure_guidee_enregistre_un_succes(donnees, monkeypatch):
    etape, commandes = _mesure_guidee(monkeypatch, donnees, 97.0, 0)
    assert "--enregistrer" in commandes[0]
    assert etape.etat == "ok"
    assert models.derniere_mesure()["tenu"] is True
    assert doctor.avertissement_mesure() is None
