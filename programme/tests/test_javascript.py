"""Le JavaScript et le CSS des pages sont generes par interpolation depuis Python.

C'est un montage ou les erreurs d'echappement passent inapercues : les tests
Python voient une chaine bien formee, le navigateur voit un script casse, et
**toute** la page cesse de fonctionner d'un coup — pas seulement la ligne
fautive.

Le cas rencontre : un `\\n` ecrit dans un litteral Python `\"\"\"` devient un vrai
saut de ligne au milieu d'une chaine JavaScript. Le script ne se parse plus, la
page ne repond plus a rien, et aucun test ne bronche.

Ces tests sautent quand Node est absent — ils s'executent sur les machines de
developpement et sur les runners.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from caviardoc import installateur, interface  # noqa: E402

besoin_de_node = pytest.mark.skipif(shutil.which("node") is None,
                                    reason="Node absent")

#: Les scripts des deux pages, avec le nom qu'ils portent dans les messages.
SCRIPTS = {"interface": interface._SCRIPT,
           "installateur": installateur._SCRIPT}

#: Et les pages entieres, telles qu'elles partent au navigateur. C'est le seul
#: niveau qui compte vraiment : un fragment peut se parser isolement et casser
#: une fois compose avec les autres — c'est arrive a la console, dont le script
#: d'onglets n'etait couvert par aucun des deux ensembles ci-dessus.
PAGES = {
    "installateur": lambda: installateur.page_html("jeton"),
    "interface": lambda: interface.page_html("jeton", "modele"),
    "console": lambda: interface.page_console("jeton", "modele", "app"),
}

#: Leurs feuilles de style, sujettes au meme genre d'accident.
STYLES = {"interface": interface._STYLE,
          "installateur": installateur._STYLE}


@besoin_de_node
@pytest.mark.parametrize("nom", sorted(SCRIPTS))
def test_le_script_se_parse(nom, tmp_path):
    fichier = tmp_path / f"{nom}.js"
    # `const JETON` est fourni par la page ; on le declare pour que le
    # fragment soit un programme complet.
    fichier.write_text('const JETON = "x";\n' + SCRIPTS[nom], encoding="utf-8")
    r = subprocess.run([shutil.which("node"), "--check", str(fichier)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("nom", sorted(SCRIPTS))
def test_aucun_saut_de_ligne_dans_un_litteral(nom):
    """Le controle statique, pour les machines sans Node.

    Une chaine JavaScript entre guillemets ne peut pas contenir un saut de
    ligne. En trouver un signale un `\\n` qui a ete interprete trop tot.
    """
    for numero, ligne in enumerate(SCRIPTS[nom].splitlines(), 1):
        # Un nombre impair de guillemets non echappes laisse une chaine
        # ouverte a la fin de la ligne.
        sans_echappes = ligne.replace('\\"', "")
        assert sans_echappes.count('"') % 2 == 0, f"{nom}, ligne {numero} : {ligne}"


@besoin_de_node
def test_le_controle_mord_vraiment(tmp_path):
    """Sans quoi ces tests pourraient passer sur n'importe quoi."""
    casse = tmp_path / "casse.js"
    casse.write_text('const x = "un saut\nde ligne";\n', encoding="utf-8")
    r = subprocess.run([shutil.which("node"), "--check", str(casse)],
                       capture_output=True, text=True)
    assert r.returncode != 0


@pytest.mark.parametrize("nom", sorted(STYLES))
def test_aucun_caractere_de_controle_dans_le_css(nom):
    """Un echappement CSS peut etre mange par Python avant d'arriver au CSS.

    Le cas rencontre : `content:"\\25b8"` pour un triangle. Python y a lu un
    echappement **octal** — `\\25` vaut 0x15 — et le navigateur a recu un
    caractere de controle suivi de « b8 », qu'il a affiche tel quel. Le
    marqueur de depli disait « b8 » sur toutes les sections.
    """
    fautifs = [(i, repr(c)) for i, c in enumerate(STYLES[nom])
               if ord(c) < 32 and c != "\n"]
    assert not fautifs, f"{nom} : {fautifs[:5]}"


@besoin_de_node
@pytest.mark.parametrize("nom", sorted(PAGES))
def test_le_script_de_la_page_entiere_se_parse(nom, tmp_path):
    """Chaque page est servie d'un bloc : c'est ce bloc qu'il faut verifier."""
    page = PAGES[nom]()
    debut = page.index("<script>") + len("<script>")
    fin = page.rindex("</script>")
    fichier = tmp_path / f"{nom}-page.js"
    fichier.write_text(page[debut:fin], encoding="utf-8")
    r = subprocess.run([shutil.which("node"), "--check", str(fichier)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@besoin_de_node
def test_chronometre_du_lot():
    """Le chronometre de fin de lot rend des durees lisibles d'un coup d'oeil."""
    debut = interface._SCRIPT.index("function chrono(")
    fin = interface._SCRIPT.index("\n}\n", debut) + 2
    code = interface._SCRIPT[debut:fin] + """
const cas = [[45, '45 s'], [185, '3 min 05 s'], [3720, '1 h 02 min'], [59.6, '1 min 00 s']];
for (const [s, attendu] of cas)
  if (chrono(s) !== attendu) { console.log(s, chrono(s)); process.exit(1); }
"""
    res = subprocess.run(["node", "-e", code], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
