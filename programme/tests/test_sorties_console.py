"""Une console qui ne sait pas ecrire un caractere ne doit pas tout arreter.

Constate par la CI sur un runner Windows : `caviardoc setup` plantait avec une
`UnicodeEncodeError` en affichant « ▶ » — le nom du lanceur, cite dans le
remede de Tesseract. La console (cp1252, cp850) ne l'a pas. L'erreur rendait le
code de sortie 1 : l'installation s'arretait pour un probleme d'affichage.
"""

import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import cli, doctor  # noqa: E402


def _console_cp1252(monkeypatch):
    tampon = io.BytesIO()
    flux = io.TextIOWrapper(tampon, encoding="cp1252", write_through=True)
    monkeypatch.setattr(sys, "stdout", flux)
    return flux, tampon


def test_sans_correctif_cp1252_refuse_le_triangle(monkeypatch):
    """Le mal, reproduit : c'est ce qui plantait sur Windows."""
    flux, _ = _console_cp1252(monkeypatch)
    with pytest.raises(UnicodeEncodeError):
        print("▶ Ouvrir Caviardoc", file=flux)


def test_la_sortie_tolerante_remplace_au_lieu_de_planter(monkeypatch):
    flux, tampon = _console_cp1252(monkeypatch)
    cli._sorties_tolerantes()
    print("▶ Ouvrir — é à ç", file=flux)
    flux.flush()
    ecrit = tampon.getvalue().decode("cp1252")
    assert "Ouvrir" in ecrit
    assert "é à ç" in ecrit, "les accents, eux, passent"
    assert "?" in ecrit, "le caractere impossible est remplace"


def test_les_verifications_s_affichent_sur_une_console_cp1252(monkeypatch):
    """Le cas exact de la CI : un remede qui cite « ▶ »."""
    flux, tampon = _console_cp1252(monkeypatch)
    cli._sorties_tolerantes()
    cli.print_checks([doctor.Check(
        "Tesseract (OCR)", doctor.Level.WARN, "absent",
        fix="« ▶ Ouvrir Caviardoc », onglet Installation.")])
    flux.flush()
    assert "Tesseract" in tampon.getvalue().decode("cp1252")


def test_un_flux_sans_reconfigure_ne_casse_pas_le_demarrage(monkeypatch):
    class Nu:
        def write(self, _):
            pass

    monkeypatch.setattr(sys, "stdout", Nu())
    monkeypatch.setattr(sys, "stderr", Nu())
    cli._sorties_tolerantes()       # ne doit pas lever
