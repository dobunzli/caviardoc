"""Telechargement du modele : reprise apres coupure, erreurs reseau lisibles."""

from __future__ import annotations

import hashlib
import io

import pytest

from caviardoc import models

CONTENU = bytes(range(256)) * 40


class _Reponse(io.BytesIO):
    def __init__(self, corps: bytes, status: int):
        super().__init__(corps)
        self.status = status
        self.headers = {"Content-Length": str(len(corps))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("CAVIARDOC_DATA_DIR", str(tmp_path))
    fichier = models.RemoteFile("onnx/m.onnx", hashlib.sha256(CONTENU).hexdigest(),
                                len(CONTENU))
    return models.ModelSpec("essai", "x/y", "main", "MIT", "essai", (fichier,))


def _partiel(spec, n: int):
    part = spec.local_dir() / "onnx" / "m.onnx.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(CONTENU[:n])


def test_reprend_un_fichier_interrompu(spec, monkeypatch):
    _partiel(spec, 3000)
    demandes = []

    def urlopen(req, timeout):
        demandes.append(req.get_header("Range"))
        return _Reponse(CONTENU[3000:], 206)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    models.download(spec)
    assert demandes == ["bytes=3000-"]
    assert (spec.local_dir() / "onnx" / "m.onnx").read_bytes() == CONTENU
    assert models.check(spec, deep=True).complete


def test_repart_de_zero_si_le_serveur_ignore_la_plage(spec, monkeypatch):
    _partiel(spec, 3000)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: _Reponse(CONTENU, 200))
    models.download(spec)
    assert models.check(spec, deep=True).complete


def test_un_debut_corrompu_est_rejete(spec, monkeypatch):
    part = spec.local_dir() / "onnx" / "m.onnx.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"\0" * 3000)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: _Reponse(CONTENU[3000:], 206))
    with pytest.raises(models.IntegrityError):
        models.download(spec)
    assert not part.exists()


def test_une_coupure_en_cours_devient_une_erreur_reseau(spec, monkeypatch):
    class Coupee(_Reponse):
        def read(self, n=-1):
            raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: Coupee(b"", 200))
    with pytest.raises(models.DownloadError):
        models.download(spec)
