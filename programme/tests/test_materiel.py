"""Le materiel decide du modele.

Cas reel : la variante fp16 met 922 s sur un seul document sur un Windows x86,
contre 4 s sur un Mac, faute de noyau fp16 pour MatMul sur CPU x86. fp32 y rend
les memes chiffres, plus vite.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import interface, materiel, models  # noqa: E402


@pytest.fixture(autouse=True)
def _sans_cache():
    materiel.detecter.cache_clear()
    yield
    materiel.detecter.cache_clear()


@pytest.mark.parametrize("brut, attendu", [
    ("arm64", "arm64"), ("aarch64", "arm64"), ("ARM64", "arm64"),
    ("x86_64", "x86_64"), ("AMD64", "x86_64"), ("x64", "x86_64"),
])
def test_architecture_normalisee(monkeypatch, brut, attendu):
    monkeypatch.setattr(materiel.platform, "machine", lambda: brut)
    monkeypatch.setattr(materiel.sys, "platform", "linux")
    assert materiel.architecture() == attendu


def test_windows_arm_sous_python_x64_est_reconnu(monkeypatch):
    """Un Python x64 emule sur une machine ARM annonce AMD64 : la vraie machine
    est dans PROCESSOR_ARCHITEW6432."""
    monkeypatch.setattr(materiel.sys, "platform", "win32")
    monkeypatch.setattr(materiel.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", "ARM64")
    assert materiel.architecture() == "arm64"


def test_le_modele_depend_de_l_architecture():
    assert models.modele_pour("arm64") == models.GLINER_MULTI_PII_FP16.key
    assert models.modele_pour("x86_64") == models.GLINER_MULTI_PII_FP32.key
    # Une architecture inconnue prend le chemin sur : celui que tous les
    # processeurs savent calculer.
    assert models.modele_pour("riscv64") == models.GLINER_MULTI_PII_FP32.key


def test_le_defaut_du_registre_suit_cette_machine():
    assert models.DEFAULT_MODEL == models.modele_pour(materiel.architecture())


def test_fp32_donne_le_meme_rappel_de_reference_que_fp16():
    """Verifie le 2026-10-04 sur arm64 : memes chiffres, ligne par ligne."""
    fp32 = models.REGISTRY["gliner-multi-pii-fp32"]
    fp16 = models.REGISTRY["gliner-multi-pii-fp16"]
    assert fp32.rappel_reference == fp16.rappel_reference
    assert fp32.files[0].name == "onnx/model.onnx"
    assert models.FICHIER_ONNX[fp32.key] == "onnx/model.onnx"


def test_detecter_decrit_la_machine_sur_windows(monkeypatch):
    monkeypatch.setattr(materiel, "systeme", lambda: "windows")
    monkeypatch.setattr(materiel, "architecture", lambda: "x86_64")
    sorties = {
        "(Get-CimInstance Win32_Processor).Name": ["Intel(R) Core(TM) i7-12700K"],
        "(Get-CimInstance Win32_VideoController).Name": ["NVIDIA GeForce RTX 3070"],
    }
    monkeypatch.setattr(materiel, "_powershell", lambda cmd: sorties[cmd])

    m = materiel.detecter()
    assert (m.systeme, m.architecture, m.fabricant) == ("windows", "x86_64", "intel")
    assert m.gpus == ("NVIDIA GeForce RTX 3070",)
    assert m.libelle == ("Windows · x86-64 · Intel(R) Core(TM) i7-12700K · "
                         "NVIDIA GeForce RTX 3070")


def test_amd_est_reconnu():
    assert materiel._fabricant("AMD Ryzen 7 5800X 8-Core Processor") == "amd"
    assert materiel._fabricant("AuthenticAMD") == "amd"
    assert materiel._fabricant("Apple M4 Pro") == "apple"
    assert materiel._fabricant("") == ""


def test_une_detection_qui_echoue_ne_casse_rien(monkeypatch):
    """Renseignement, jamais condition : PowerShell absent ou bloque."""
    monkeypatch.setattr(materiel, "systeme", lambda: "windows")
    monkeypatch.setattr(materiel, "_executer", lambda *a, **k: "")
    monkeypatch.delenv("PROCESSOR_IDENTIFIER", raising=False)

    m = materiel.detecter()
    assert m.processeur == "" and m.gpus == ()
    assert m.libelle.startswith("Windows")


def test_une_exception_pendant_la_detection_est_absorbee(monkeypatch):
    def casse():
        raise RuntimeError("boum")

    monkeypatch.setattr(materiel, "systeme", lambda: "windows")
    monkeypatch.setattr(materiel, "_processeur_et_gpus_windows", casse)
    assert materiel.detecter().gpus == ()


def test_la_puce_apple_n_est_pas_comptee_comme_carte_graphique(monkeypatch):
    monkeypatch.setattr(materiel, "systeme", lambda: "macos")
    monkeypatch.setattr(materiel, "_processeur_et_gpus_macos",
                        lambda: ("Apple M2 Pro", ("Apple M2 Pro",)))
    m = materiel.detecter()
    assert m.gpus == ()
    assert m.libelle.count("Apple M2 Pro") == 1


GO = 1024 ** 3


def _evaluer(**kw):
    # Windows x86 par defaut : ni Mac Intel ni macOS ancien ne s'en melent.
    base = dict(ram=16 * GO, disque_libre=100 * GO, ram_minimale=4 * GO,
                disque_necessaire=3 * GO, architecture_="x86_64",
                systeme_="windows", version_macos_=None, coeurs=8)
    base.update(kw)
    ram, disque = base.pop("ram"), base.pop("disque_libre")
    return materiel.evaluer(ram, disque, **base)


def test_une_machine_confortable_est_compatible():
    assert _evaluer().niveau == "ok"


def test_pas_assez_de_memoire_est_incompatible_et_dit_combien():
    v = _evaluer(ram=2 * GO)
    assert v.niveau == "incompatible"
    assert "2.0 Go" in v.raisons[0] and "4 Go" in v.raisons[0]


def test_pas_assez_de_disque_est_incompatible():
    v = _evaluer(disque_libre=1 * GO)
    assert v.niveau == "incompatible" and "disque" in v.raisons[0]


def test_une_architecture_non_prise_en_charge_est_incompatible():
    v = _evaluer(architecture_="x86")
    assert v.niveau == "incompatible" and "x86" in v.raisons[0]


def test_un_seul_coeur_est_juste_pas_incompatible():
    v = _evaluer(coeurs=1)
    assert v.niveau == "juste" and "très lente" in v.raisons[0]


def test_une_valeur_inconnue_ne_condamne_pas_la_machine():
    """On ne refuse pas une machine sur ce qu'on n'a pas su lire."""
    assert _evaluer(ram=None, disque_libre=None).niveau == "ok"


def test_plusieurs_raisons_sont_toutes_dites():
    v = _evaluer(ram=1 * GO, disque_libre=1 * GO, coeurs=1)
    assert v.niveau == "incompatible" and len(v.raisons) == 3


def test_la_vue_modeles_porte_le_verdict():
    vue = interface.vue_modeles(ram=2 * GO)
    assert vue["machine"]["compatibilite"]["niveau"] == "incompatible"
    assert interface.vue_modeles(ram=32 * GO)["machine"]["compatibilite"]["niveau"] in (
        "ok", "incompatible"), "incompatible seulement si le disque manque"


# --- Ce qui rend l'installation impossible ---------------------------------
# Lu dans uv.lock : onnxruntime et torch n'ont de paquet macOS que pour
# macosx_14_0_arm64. Un Mac Intel, ou un macOS plus ancien, echouerait a l'etape
# « Bibliotheques » apres un long telechargement.

def test_un_mac_intel_est_impossible():
    raisons = materiel.obstacles("macos", "x86_64", 15)
    assert len(raisons) == 1 and "Intel" in raisons[0]
    assert "Apple Silicon" in raisons[0], "on dit ce qui, lui, fonctionne"


def test_un_macos_trop_ancien_est_impossible():
    (raison,) = materiel.obstacles("macos", "arm64", 13)
    assert "macOS 13" in raison and "14" in raison


def test_un_mac_arm_recent_est_possible():
    assert materiel.obstacles("macos", "arm64", 14) == ()
    assert materiel.obstacles("macos", "arm64", 26) == ()


def test_une_version_de_macos_inconnue_ne_condamne_pas():
    assert materiel.obstacles("macos", "arm64", None) == ()


def test_un_pc_x86_n_est_pas_un_mac_intel():
    """Windows et Linux x86 ont leurs paquets : seul macOS en est prive."""
    assert materiel.obstacles("windows", "x86_64", None) == ()
    assert materiel.obstacles("linux", "x86_64", None) == ()


def test_une_architecture_inconnue_est_impossible():
    assert materiel.obstacles("linux", "riscv64", None)


def test_le_verdict_dit_qu_un_mac_intel_bloque_mais_pas_la_memoire():
    mac = _evaluer(systeme_="macos", version_macos_=15)
    assert mac.niveau == "incompatible" and mac.bloquant
    petite = _evaluer(ram=2 * GO)
    assert petite.niveau == "incompatible" and not petite.bloquant, \
        "la memoire se dit, elle n'interdit pas"


def test_l_installateur_refuse_de_lancer_une_etape_sur_un_mac_intel(monkeypatch):
    from caviardoc import installateur
    raison = "Ce Mac a un processeur Intel."
    monkeypatch.setattr(materiel, "obstacles", lambda *a, **k: (raison,))
    lances = []
    monkeypatch.setattr(installateur, "ACTIONS",
                        {"uv": lambda e: lances.append("uv")})
    inst = installateur.etapes_par_defaut()

    etape = installateur.executer(inst, "uv")

    assert etape.etat == "echec" and raison in etape.message
    assert "Rien n'a été installé" in etape.remede
    assert lances == [], "aucune action ne doit partir"
    assert inst.vue()["bloquee"] == [raison]


def test_une_machine_possible_n_est_pas_bloquee(monkeypatch):
    from caviardoc import installateur
    monkeypatch.setattr(materiel, "obstacles", lambda *a, **k: ())
    assert installateur.etapes_par_defaut().vue()["bloquee"] == []


def test_setup_s_arrete_avant_tout_telechargement(monkeypatch, capsys):
    import argparse
    from caviardoc import cli
    monkeypatch.setattr(materiel, "obstacles",
                        lambda *a, **k: ("Ce Mac a un processeur Intel.",))
    monkeypatch.setattr(cli.doctor, "run_all",
                        lambda **k: pytest.fail("rien ne doit etre verifie"))

    code = cli.cmd_setup(argparse.Namespace(modele=None, yes=True))

    assert code == 1
    sortie = capsys.readouterr().out
    assert "Intel" in sortie and "Rien n'a été installé" in sortie


def test_la_page_grise_les_boutons_quand_l_installation_est_impossible():
    from caviardoc import installateur
    script = installateur._SCRIPT
    assert "vue.bloquee" in script
    assert "Installation impossible sur cet ordinateur" in script


def test_la_detection_reelle_rend_quelque_chose():
    m = materiel.detecter()
    assert m.systeme in ("macos", "windows", "linux")
    assert m.architecture


def test_a_rappel_egal_le_modele_conseille_est_celui_de_la_machine(monkeypatch):
    """fp16 et fp32 rendent le meme rappel : sans departage, le registre
    conseillait fp16 partout, y compris la ou il met 922 s."""
    for defaut in ("gliner-multi-pii-fp32", "gliner-multi-pii-fp16"):
        monkeypatch.setattr(models, "DEFAULT_MODEL", defaut)
        vue = interface.vue_modeles(ram=32 * 1024 ** 3)
        conseilles = [m["cle"] for m in vue["modeles"] if m["avis"] == "conseille"]
        assert conseilles == [defaut]
    assert "materiel" in vue["machine"]


def test_un_pc_windows_arm_est_impossible():
    """torch n'est pas publie pour Windows ARM64 : l'installation echouait
    apres un long telechargement."""
    (raison,) = materiel.obstacles("windows", "arm64", None)
    assert "ARM" in raison and "torch" in raison


def test_le_verrou_suit_uv_lock_pour_windows_arm():
    """Si torch publie un jour pour Windows ARM64, ce test le dira : le
    blocage ci-dessus pourra alors etre leve."""
    from pathlib import Path
    verrou = (Path(__file__).resolve().parents[1] / "uv.lock").read_text()
    assert not re.search(r"torch-[^\"]*win_arm64", verrou)
