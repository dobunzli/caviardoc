"""Passage de Doc Anonymizer a Caviardoc, sur une machine deja installee.

Deux choses ne doivent pas se perdre au renommage : le modele et les
corrections retenues, qui vivent dans le repertoire de donnees de l'ancien nom,
et le redemarrage qui suit la mise a jour, que l'ancien code lance avec
`python -m docanon`.
"""

import os
import subprocess
import sys
from pathlib import Path

from caviardoc import paths

SRC = Path(__file__).resolve().parents[1] / "src"


def _machine(tmp_path, monkeypatch):
    nouveau = tmp_path / "caviardoc"
    monkeypatch.delenv(paths.ENV_DATA_DIR, raising=False)
    monkeypatch.setattr(paths, "data_dir", lambda: nouveau)
    ancien = tmp_path / "doc-anonymizer"
    (ancien / "models" / "gliner").mkdir(parents=True)
    (ancien / "models" / "gliner" / "poids.onnx").write_bytes(b"modele")
    (ancien / "exceptions.json").write_text("{}", encoding="utf-8")
    (ancien / "venv" / "bin").mkdir(parents=True)
    return ancien, nouveau


def test_le_modele_et_les_corrections_sont_repris(tmp_path, monkeypatch):
    ancien, nouveau = _machine(tmp_path, monkeypatch)
    repris = paths.reprendre_ancien()
    assert set(repris) == {"models", "exceptions.json"}
    assert (nouveau / "models" / "gliner" / "poids.onnx").read_bytes() == b"modele"
    assert (nouveau / "exceptions.json").is_file()
    # Le venv porte des chemins absolus : il reste ou il est, et sera refait.
    assert not (nouveau / "venv").exists()
    assert (ancien / "venv").is_dir()
    assert paths.reprendre_ancien() == []


def test_un_dossier_vide_cree_trop_tot_ne_bloque_pas_la_reprise(tmp_path,
                                                                monkeypatch):
    _, nouveau = _machine(tmp_path, monkeypatch)
    (nouveau / "models").mkdir(parents=True)
    assert "models" in paths.reprendre_ancien()
    assert (nouveau / "models" / "gliner" / "poids.onnx").is_file()


def test_rien_n_est_ecrase(tmp_path, monkeypatch):
    ancien, nouveau = _machine(tmp_path, monkeypatch)
    nouveau.mkdir()
    (nouveau / "exceptions.json").write_text('{"neuf": 1}', encoding="utf-8")
    assert "exceptions.json" not in paths.reprendre_ancien()
    assert (nouveau / "exceptions.json").read_text(encoding="utf-8") == '{"neuf": 1}'
    assert (ancien / "exceptions.json").is_file()


def test_rien_a_reprendre_sur_une_machine_neuve(tmp_path, monkeypatch):
    monkeypatch.delenv(paths.ENV_DATA_DIR, raising=False)
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "caviardoc")
    assert paths.ancien_data_dir() is None
    assert paths.reprendre_ancien() == []


def test_l_ancien_module_relaie_vers_caviardoc(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(SRC),
               CAVIARDOC_DATA_DIR=str(tmp_path))
    fini = subprocess.run([sys.executable, "-m", "docanon", "--help"],
                          capture_output=True, text=True, env=env,
                          cwd=SRC.parent, timeout=120)
    assert fini.returncode == 0, fini.stderr
    assert "caviarder" in fini.stdout
