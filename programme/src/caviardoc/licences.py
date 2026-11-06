"""Auteur, licence, et composants dont Caviardoc depend.

L'AGPL demande que toute interface interactive affiche des « mentions legales
appropriees » (article 5 d) : l'auteur, la licence, l'absence de garantie, et
le moyen d'en lire le texte. Elle impose aussi d'offrir le code source a qui
utilise le programme (article 13). Les composants tiers, eux, demandent que
leur licence soit mentionnee — PyMuPDF en premier, puisque c'est elle qui
impose l'AGPL a l'ensemble.

Les licences ont ete relevees le 2026-10-01 dans les metadonnees des paquets
installes et sur les fiches des modeles ; les versions, elles, sont lues a
l'execution.

Ce module n'utilise que la bibliotheque standard.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import paths

AUTEUR = "D. Bünzli"
ANNEE = 2026
DEPOT_SOURCE = "https://github.com/dobunzli/caviardoc"
LICENCE = "AGPL-3.0-or-later"


@dataclass(frozen=True)
class Composant:
    nom: str
    role: str
    licence: str
    lien: str
    #: Nom de distribution Python, pour afficher la version installee.
    paquet: str = ""


COMPOSANTS = (
    Composant("GLiNER multi PII v1", "modèle de détection (U. Zaratiana ; "
              "export ONNX : onnx-community)", "Apache-2.0",
              "https://huggingface.co/urchade/gliner_multi_pii-v1"),
    Composant("mDeBERTa-v3", "encodeur du modèle (Microsoft)", "MIT",
              "https://huggingface.co/microsoft/mdeberta-v3-base"),
    Composant("GLiNER", "bibliothèque de détection", "Apache-2.0",
              "https://github.com/urchade/GLiNER", "gliner"),
    Composant("ONNX Runtime", "exécution du modèle (Microsoft)", "MIT",
              "https://onnxruntime.ai", "onnxruntime"),
    Composant("PyTorch", "calcul", "BSD-3-Clause", "https://pytorch.org",
              "torch"),
    Composant("Transformers", "découpage du texte (Hugging Face)",
              "Apache-2.0", "https://github.com/huggingface/transformers",
              "transformers"),
    Composant("Tokenizers", "découpage du texte (Hugging Face)",
              "Apache-2.0", "https://github.com/huggingface/tokenizers",
              "tokenizers"),
    Composant("SentencePiece", "découpage du texte (Google)", "Apache-2.0",
              "https://github.com/google/sentencepiece", "sentencepiece"),
    Composant("PyMuPDF / MuPDF", "lecture et reconstruction des PDF "
              "(Artifex)", "AGPL-3.0", "https://pymupdf.readthedocs.io",
              "PyMuPDF"),
    Composant("python-docx", "lecture et écriture des .docx", "MIT",
              "https://github.com/python-openxml/python-docx", "python-docx"),
    Composant("openpyxl", "classeurs Excel", "MIT",
              "https://openpyxl.readthedocs.io", "openpyxl"),
    Composant("Pillow", "images", "MIT-CMU", "https://python-pillow.github.io",
              "pillow"),
    Composant("NumPy", "calcul", "BSD-3-Clause", "https://numpy.org", "numpy"),
    Composant("pytesseract", "liaison avec Tesseract", "Apache-2.0",
              "https://github.com/madmaze/pytesseract", "pytesseract"),
    Composant("zxing-cpp", "recherche et masquage des codes-barres et des "
              "QR codes", "Apache-2.0",
              "https://github.com/zxing-cpp/zxing-cpp", "zxing-cpp"),
    Composant("Tesseract OCR", "lecture des documents scannés, données "
              "« fra » comprises", "Apache-2.0",
              "https://github.com/tesseract-ocr/tesseract"),
    Composant("micromamba / conda-forge", "installation de Tesseract",
              "BSD-3-Clause", "https://mamba.readthedocs.io"),
    Composant("uv", "installation des bibliothèques (Astral)",
              "MIT ou Apache-2.0", "https://docs.astral.sh/uv/"),
    Composant("Python", "langage", "PSF-2.0", "https://www.python.org"),
)


def version(paquet: str) -> str:
    """Version installee, ou "" si le paquet n'est pas la ou pas lisible."""
    if not paquet:
        return ""
    try:
        from importlib.metadata import version as lire

        return lire(paquet)
    except Exception:
        return ""


def texte_licence() -> bytes:
    """Le texte integral de la licence, tel que le depot le porte."""
    try:
        return (paths.repo_root() / "LICENSE").read_bytes()
    except OSError:
        return (b"GNU Affero General Public License, version 3 : "
                b"https://www.gnu.org/licenses/agpl-3.0.html\n")


def pied_de_page(lien_licence: str) -> str:
    """Les mentions legales, en une ligne, au bas de chaque page."""
    from . import __version__
    return (f'<footer class="pied">Caviardoc {__version__} · © {ANNEE} {AUTEUR} · '
            f'logiciel libre sous licence <a href="{lien_licence}" '
            f'target="_blank" rel="noopener">AGPL-3.0</a>, fourni sans aucune '
            f'garantie · <a href="{DEPOT_SOURCE}" target="_blank" '
            f'rel="noopener noreferrer">code source</a></footer>')
