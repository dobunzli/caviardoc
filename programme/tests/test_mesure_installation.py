"""L'etape « Mesure sur cette machine » doit montrer qu'elle avance.

Cas reel : sur un Windows, la page restait sur « Mesure : en cours » sans rien
d'autre. La sortie d'un processus relie a un tube est tamponnee : rien
n'arrivait avant la fin, et une mesure lente ne se distinguait pas d'une mesure
bloquee.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import doctor, installateur, paths  # noqa: E402

#: Un faux outil : une ligne tout de suite, puis un long calcul, puis le bilan.
FAUX_OUTIL = """
print("  machine      : essai")
import time
time.sleep(3)
print("  ok  rappel_complet            97.3 %   (seuil 90 %)")
print("  ok  rappel_patient            95.0 %   (seuil 90 %)")
"""


def _installer_faux_outil(monkeypatch, tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "mesurer.py").write_text(FAUX_OUTIL, encoding="utf-8")
    monkeypatch.setattr(paths, "project_root", lambda: tmp_path)
    monkeypatch.setattr(doctor, "_venv_python", lambda _venv: Path(sys.executable))


def test_la_sortie_arrive_pendant_la_mesure_et_non_a_la_fin(monkeypatch, tmp_path):
    _installer_faux_outil(monkeypatch, tmp_path)
    etape = installateur.Etape("mesure", "Mesure", "")
    fil = threading.Thread(target=installateur.etape_mesure, args=(etape,))
    fil.start()
    try:
        # Le faux outil dort 3 s apres sa premiere ligne : si elle est
        # tamponnee, elle n'apparait qu'apres.
        attente = time.time() + 2.0
        while time.time() < attente and not any(
                "machine" in ligne for ligne in etape.journal):
            time.sleep(0.05)
        assert any("machine" in ligne for ligne in etape.journal), \
            "la premiere ligne doit arriver avant la fin de la mesure"
        assert fil.is_alive(), "la mesure devait encore tourner"
    finally:
        fil.join(timeout=20)
    assert etape.etat == "ok", etape.message


def test_une_mesure_qui_plante_montre_pourquoi(monkeypatch, tmp_path):
    """« La mesure n'a rien rendu » ne disait rien : la cause etait dans la
    sortie du processus, qu'il fallait aller lire dans le journal."""
    _installer_faux_outil(monkeypatch, tmp_path)
    (tmp_path / "tools" / "mesurer.py").write_text(
        "print('  machine      : essai')\n"
        "raise OSError('[WinError 126] le module est introuvable : c10.dll')\n",
        encoding="utf-8")
    etape = installateur.Etape("mesure", "Mesure", "")
    installateur.etape_mesure(etape)

    assert etape.etat == "echec"
    assert "code 1" in etape.message
    assert "c10.dll" in etape.remede, "la cause doit etre sous les yeux"
    assert "Dernières lignes" in etape.remede


def test_le_temps_ecoule_est_affiche_puis_remplace_par_le_resultat(
        monkeypatch, tmp_path):
    _installer_faux_outil(monkeypatch, tmp_path)
    etape = installateur.Etape("mesure", "Mesure", "")
    vus = []
    fil = threading.Thread(target=installateur.etape_mesure, args=(etape,))
    fil.start()
    while fil.is_alive():
        vus.append(etape.message)
        time.sleep(0.2)

    assert any("Mesure en cours depuis" in m for m in vus), vus
    # Le compteur ne doit pas ecraser le verdict.
    assert etape.message.startswith("réussie"), etape.message
    assert etape.etat == "ok"
    assert any("mesure terminée" in ligne for ligne in etape.journal)
