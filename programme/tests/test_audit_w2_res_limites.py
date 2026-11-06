"""Audit de securite, vague 2, voie E1 : bornes de ressources.

Aucune bombe reelle : les limites sont abaissees a quelques octets par
`monkeypatch`, et les entrees restent minuscules.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import threading
import time
import types
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from caviardoc import interface, web_commun  # noqa: E402
from caviardoc.documents import docx as docx_mod  # noqa: E402
from caviardoc.interface import Atelier, deposer  # noqa: E402


def _docx(nb_parties: int = 1, taille: int = 2000) -> bytes:
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(nb_parties):
            z.writestr(f"word/p{i}.xml", "a" * taille)
    return tampon.getvalue()


# --- 1. DOCX : taille inflatee et nombre d'entrees ------------------------- #

class TestDocxInflation:
    def test_taille_inflatee_bornee(self, tmp_path, monkeypatch):
        monkeypatch.setattr(docx_mod, "INFLATION_MAX_DOCX", 1000)
        octets = _docx(taille=5000)
        with pytest.raises(ValueError, match="decompress|inflat|volumineux"):
            deposer(Atelier(racine=tmp_path), "a.docx", io.BytesIO(octets),
                    len(octets))
        assert list(Atelier(racine=tmp_path).entrees().iterdir()) == []

    def test_nombre_d_entrees_borne(self, tmp_path, monkeypatch):
        monkeypatch.setattr(docx_mod, "ENTREES_MAX_DOCX", 3)
        octets = _docx(nb_parties=10, taille=10)
        with pytest.raises(ValueError, match="entrees|parties"):
            deposer(Atelier(racine=tmp_path), "a.docx", io.BytesIO(octets),
                    len(octets))

    def test_un_docx_ordinaire_passe(self, tmp_path):
        octets = _docx(nb_parties=5)
        assert deposer(Atelier(racine=tmp_path), "a.docx", io.BytesIO(octets),
                       len(octets)).nom == "a.docx"


# --- 2. PDF : pages et pixels --------------------------------------------- #

pymupdf = pytest.importorskip("pymupdf")


def _pdf(pages: int = 1, largeur: float = 200, hauteur: float = 200) -> bytes:
    d = pymupdf.open()
    for _ in range(pages):
        d.new_page(width=largeur, height=hauteur)
    octets = d.tobytes()
    d.close()
    return octets


class TestPdfBornes:
    def test_pixels_par_page_bornes(self, monkeypatch):
        from caviardoc.documents import caviardage
        monkeypatch.setattr(caviardage, "PIXELS_MAX_PAGE", 1000)
        d = pymupdf.open(stream=_pdf(), filetype="pdf")
        with pytest.raises(ValueError, match="pixels"):
            caviardage._rendu(d[0], 300)

    def test_nombre_de_pages_borne(self, tmp_path, monkeypatch):
        from caviardoc.documents import caviardage
        monkeypatch.setattr(caviardage, "PAGES_MAX", 2)
        chemin = tmp_path / "a.pdf"
        chemin.write_bytes(_pdf(pages=4))
        with pytest.raises(ValueError, match="pages"):
            caviardage.lire_pdf(chemin)

    def test_une_page_ordinaire_se_rend(self):
        from caviardoc.documents import caviardage
        d = pymupdf.open(stream=_pdf(largeur=595, hauteur=842), filetype="pdf")
        assert caviardage._rendu(d[0], 100).size[0] > 0


# --- 3. Pillow : liste de formats ----------------------------------------- #

class TestFormatsImage:
    def test_eps_refuse(self):
        from caviardoc.documents import images
        eps = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
               b"newpath 0 0 moveto 10 10 lineto stroke\n")
        with pytest.raises(Exception):
            images._ouvrir(eps)

    def test_eps_n_atteint_pas_le_decodeur(self, monkeypatch):
        from PIL import EpsImagePlugin
        from caviardoc.documents import images
        appels = []
        monkeypatch.setattr(EpsImagePlugin.EpsImageFile, "load",
                            lambda self, *a, **k: appels.append(1))
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
        with pytest.raises(Exception):
            images._ouvrir(eps)
        assert not appels

    @pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP"])
    def test_formats_courants_acceptes(self, fmt):
        from PIL import Image
        from caviardoc.documents import images
        tampon = io.BytesIO()
        Image.new("RGB", (8, 8), "white").save(tampon, fmt)
        assert images._ouvrir(tampon.getvalue()).size == (8, 8)

    def test_plafond_de_pixels_pose(self):
        from PIL import Image
        from caviardoc.documents import images
        tampon = io.BytesIO()
        Image.new("RGB", (8, 8)).save(tampon, "PNG")
        images._ouvrir(tampon.getvalue())
        assert Image.MAX_IMAGE_PIXELS is not None
        assert Image.MAX_IMAGE_PIXELS <= 200_000_000


# --- 4. Noms d'entree du ZIP ---------------------------------------------- #

class TestNomZip:
    @pytest.mark.parametrize("nom", ["..\\..\\evil.pdf", "a/../b.pdf",
                                     "x\\y.pdf", "../../z.pdf"])
    def test_le_nom_du_depot_est_sans_separateur(self, tmp_path, nom):
        octets = _pdf()
        d = deposer(Atelier(racine=tmp_path), nom, io.BytesIO(octets),
                    len(octets))
        assert "\\" not in d.nom and "/" not in d.nom and ".." not in d.nom
        assert d.nom.lower().endswith(".pdf")


# --- 5. Regex quadratique : bornee, test de non-regression ------------------------------------ #

def test_noeud_texte_lineaire():
    from caviardoc.documents.docx import NOEUD_TEXTE
    xml = "<w:t>" * 6000
    t0 = time.perf_counter()
    list(NOEUD_TEXTE.finditer(xml))
    assert time.perf_counter() - t0 < 0.2


# --- 6. Temporaires de l'OCR ---------------------------------------------- #

def test_temporaires_dans_l_atelier(monkeypatch):
    vu = {}
    prete, arret = threading.Event(), threading.Event()

    def pret(url):
        vu["tmp"] = tempfile.gettempdir()
        prete.set()

    fil = threading.Thread(target=lambda: interface.servir(
        ouvrir=False, pret=pret, arret=arret), daemon=True)
    avant = tempfile.gettempdir()
    fil.start()
    assert prete.wait(5)
    arret.set(); fil.join(5)
    from caviardoc import paths
    assert Path(vu["tmp"]).is_relative_to(paths.cache_dir())
    assert Path(vu["tmp"]).name != ""
    assert tempfile.gettempdir() == avant


# --- 7. Telemetrie ONNX Runtime ------------------------------------------- #

def test_telemetrie_onnxruntime_coupee(monkeypatch):
    appels = []
    faux = types.ModuleType("onnxruntime")
    faux.disable_telemetry_events = lambda: appels.append(1)
    monkeypatch.setitem(sys.modules, "onnxruntime", faux)
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)
    web_commun.desactiver_telemetrie()
    assert appels == [1]
    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"


def test_telemetrie_survit_a_une_erreur(monkeypatch):
    faux = types.ModuleType("onnxruntime")

    def casse():
        raise RuntimeError("non")
    faux.disable_telemetry_events = casse
    monkeypatch.setitem(sys.modules, "onnxruntime", faux)
    web_commun.desactiver_telemetrie()


# --- 8. Port de la boucle locale ------------------------------------------ #

def test_adresse_non_partageable():
    # Sous Windows SO_REUSEADDR laisserait un second processus ecouter sur le
    # port vivant ; ailleurs il ne sert qu'a reprendre un port en TIME_WAIT.
    assert web_commun.ServeurLocal.allow_reuse_address is (sys.platform != "win32")


class _Muet(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="SO_REUSEADDR coupe sous Windows")
def test_redemarrage_sur_le_meme_port():
    # La console se relance sur le meme port pour garder la page ouverte.
    srv = web_commun.ServeurLocal(("127.0.0.1", 0), _Muet)
    port = srv.server_address[1]
    fil = threading.Thread(target=srv.serve_forever, daemon=True)
    fil.start()
    for _ in range(3):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
            assert r.status == 200
    srv.shutdown()
    srv.server_close()
    fil.join(5)
    web_commun.ServeurLocal(("127.0.0.1", port), _Muet).server_close()


def test_port_vivant_refuse():
    srv = web_commun.ServeurLocal(("127.0.0.1", 0), _Muet)
    try:
        with pytest.raises(OSError):
            web_commun.ServeurLocal(("127.0.0.1", srv.server_address[1]), _Muet)
    finally:
        srv.server_close()


def test_so_exclusiveaddruse_sur_windows(monkeypatch):
    poses = []
    monkeypatch.setattr(web_commun.sys, "platform", "win32")
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)

    class Faux:
        def setsockopt(self, *a):
            poses.append(a)

    class S(web_commun.ServeurLocal):
        def __init__(self):
            self.socket = Faux()

    web_commun.ServeurLocal._preparer_socket(S())
    assert (socket.SOL_SOCKET, -5, 1) in poses
