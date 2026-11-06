"""Une personne ecrite « Hürlibrück » et « HUERLIBRUECK » garde un seul pseudonyme."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc.pseudonymes import TablePseudonymes  # noqa: E402


def test_trema_et_transcription_meme_personne():
    t = TablePseudonymes()
    premier = t.pseudonyme("Odile Hürlibrück", "personne")
    assert t.pseudonyme("HUERLIBRUECK Odile", "personne") == premier
    assert t.pseudonyme("M. HUERLIBRUECK", "personne") == premier
    assert t.pseudonyme("Hurlibruck", "personne") == premier


def test_personnes_distinctes_restent_distinctes():
    t = TablePseudonymes()
    assert t.pseudonyme("Odile Hürlibrück", "personne") != t.pseudonyme(
        "Paul Kuhn", "personne")
