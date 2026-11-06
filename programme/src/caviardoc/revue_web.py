"""Page de revue et serveur local.

La page est servie depuis la machine, sur une boucle locale, et n'est jamais
publiee : elle affiche les documents en clair, entites comprises. Le serveur
s'arrete des que les decisions sont enregistrees.

Le rendu est une fonction pure — `page_html` — pour rester testable sans
ouvrir de socket. Seul `servir` touche au reseau.
"""

from __future__ import annotations

import html
import json
import threading
import webbrowser
from urllib.parse import parse_qs, urlparse

from .revue import SessionRevue, ajouter, decider
from .web_commun import (ENTETE_JETON, HOTE, PoigneeBase,  # noqa: F401
                         ServeurLocal, hotes_admis, nouveau_jeton)

_STYLE = """
:root{--fond:#f6f7f8;--carte:#fff;--encre:#1a1d21;--gris:#5d6672;
--trait:#dde2e7;--regle:#0f5c63;--modele:#8a5a11;--propage:#4a4a8f;
--rejet:#9e2b2b;--ajout:#256b4a;--juger:#8a3a78}
*{box-sizing:border-box}
body{margin:0;background:var(--fond);color:var(--encre);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;background:var(--carte);border-bottom:1px solid var(--trait);
padding:14px 22px;display:flex;gap:18px;align-items:center;flex-wrap:wrap;z-index:5}
h1{font-size:1.05rem;margin:0;font-weight:600}
.compte{color:var(--gris);font-size:.88rem}
main{max-width:60rem;margin:0 auto;padding:22px}
.partie{background:var(--carte);border:1px solid var(--trait);border-radius:4px;
margin-bottom:18px;overflow:hidden}
.partie h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;
color:var(--gris);margin:0;padding:10px 16px;border-bottom:1px solid var(--trait);
background:#eef1f3;font-weight:600}
.texte{padding:16px;white-space:pre-wrap;word-break:break-word;
font:13.5px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace}
mark{padding:1px 2px;border-radius:2px;cursor:pointer;border-bottom:2px solid}
mark.regle{background:#dcecec;border-color:var(--regle)}
mark.modele{background:#f7ebd8;border-color:var(--modele)}
mark.propage{background:#e6e6f2;border-color:var(--propage)}
mark.humain{background:#deeee4;border-color:var(--ajout)}
mark.rejetee{background:transparent;border-color:var(--rejet);
border-bottom-style:dashed;opacity:.55;text-decoration:line-through}
mark.juger{background:#f4e3ef;border-color:var(--juger)}
mark.juger.rejetee{background:#fbf2f8;border-color:var(--juger);
border-bottom-style:dotted;opacity:1;text-decoration:none}
button{font:inherit;padding:7px 14px;border:1px solid var(--trait);background:var(--carte);
border-radius:3px;cursor:pointer}
button.primaire{background:var(--regle);color:#fff;border-color:var(--regle);font-weight:600}
button:disabled{opacity:.45;cursor:default}
.legende{display:flex;gap:14px;font-size:.78rem;color:var(--gris);flex-wrap:wrap}
.legende span::before{content:"";display:inline-block;width:10px;height:10px;
margin-right:5px;border-radius:2px;vertical-align:-1px}
.l-regle::before{background:#dcecec;border:1px solid var(--regle)}
.l-modele::before{background:#f7ebd8;border:1px solid var(--modele)}
.l-propage::before{background:#e6e6f2;border:1px solid var(--propage)}
.l-humain::before{background:#deeee4;border:1px solid var(--ajout)}
.l-juger::before{background:#f4e3ef;border:1px solid var(--juger)}
#etat{padding:22px;text-align:center;color:var(--gris)}
.avis{background:#fff8e6;border:1px solid #e0c98a;border-radius:4px;
padding:12px 16px;margin-bottom:18px;font-size:.9rem}
"""

_SCRIPT = """
const decisions = {};
function entetes(){
  return {'Content-Type':'application/json', 'X-Caviardoc-Jeton': JETON};
}
function bascule(el){
  const id = el.dataset.id;
  const rejete = el.classList.toggle('rejetee');
  if (rejete){
    decisions[id] = {etat:'rejetee', motif:''};
  } else {
    decisions[id] = {etat:'retenue', motif:''};
  }
  majCompte();
}
function majCompte(){
  const total = document.querySelectorAll('mark').length;
  const rejets = document.querySelectorAll('mark.rejetee').length;
  document.getElementById('compte').textContent =
    (total - rejets) + " masquées · " + rejets + " laissées visibles";
}
async function ajouterSelection(){
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { alert("Sélectionnez d'abord le texte à masquer."); return; }
  const bloc = sel.anchorNode.parentElement.closest('.texte');
  if (!bloc){ alert("Sélection hors d'un document."); return; }
  const plage = sel.getRangeAt(0);
  const avant = plage.cloneRange();
  avant.selectNodeContents(bloc); avant.setEnd(plage.startContainer, plage.startOffset);
  const debut = avant.toString().length;
  const r = await fetch('/ajout', {method:'POST', headers: entetes(),
    body: JSON.stringify({partie: bloc.dataset.partie, debut: debut,
                          fin: debut + sel.toString().length})});
  if (r.ok) location.reload(); else alert("Ajout refusé : " + await r.text());
}
async function valider(){
  document.getElementById('valider').disabled = true;
  await fetch('/decisions', {method:'POST', headers: entetes(),
                             body: JSON.stringify(decisions)});
  document.body.innerHTML =
    "<div id='etat'><h1>Décisions enregistrées.</h1>" +
    "<p>Vous pouvez fermer cet onglet — le traitement se poursuit dans le terminal.</p></div>";
}
document.addEventListener('DOMContentLoaded', majCompte);
"""


def surligner(texte: str, detections) -> str:
    """Rend le texte d'une partie avec ses detections cliquables."""
    morceaux: list[str] = []
    curseur = 0
    for d in sorted(detections, key=lambda x: x.debut):
        if d.debut < curseur:      # chevauchement : deja couvert
            continue
        morceaux.append(html.escape(texte[curseur:d.debut]))
        if d.a_juger:
            classes = "juger"
            titre = f"{d.etiquette} · peut aider à reconnaître la personne"
        else:
            classes = (d.origine if d.origine in ("regle", "modele", "propage",
                                                  "humain") else "modele")
            titre = f"{d.etiquette} · {d.origine} · {d.score:.2f}"
        if not d.masquee:
            classes += " rejetee"
        morceaux.append(
            f'<mark class="{classes}" data-id="{html.escape(d.identifiant)}" '
            f'title="{html.escape(titre)}" onclick="bascule(this)">'
            f'{html.escape(texte[d.debut:d.fin])}</mark>')
        curseur = d.fin
    morceaux.append(html.escape(texte[curseur:]))
    return "".join(morceaux)


def page_html(session: SessionRevue, textes: dict[str, str],
              jeton: str = "") -> str:
    """Page de revue complete. Fonction pure : aucun acces reseau ni disque."""
    parties = []
    for partie, texte in textes.items():
        dets = [d for d in session.detections if d.partie == partie]
        parties.append(
            f'<section class="partie"><h2>{html.escape(partie)} — '
            f'{len(dets)} détection(s)</h2>'
            f'<div class="texte" data-partie="{html.escape(partie)}">'
            f'{surligner(texte, dets)}</div></section>')

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<title>Revue — {html.escape(session.document)}</title>
<style>{_STYLE}</style></head><body>
<header>
  <h1>{html.escape(session.document)}</h1>
  <span class="compte" id="compte"></span>
  <div class="legende">
    <span class="l-regle">règle</span><span class="l-modele">modèle</span>
    <span class="l-propage">propagé</span><span class="l-humain">ajouté</span>
    <span class="l-juger">à juger : peut aider à reconnaître la personne</span>
  </div>
  <span style="flex:1"></span>
  <button onclick="ajouterSelection()">Masquer la sélection</button>
  <button class="primaire" id="valider" onclick="valider()">Valider</button>
</header>
<main>
<div class="avis"><strong>Cliquez une entité surlignée pour retirer son masque</strong>.
Sélectionnez du texte puis « Masquer la sélection »
pour rattraper ce qui a été manqué. Sans action, tout reste masqué, sauf les
passages <strong>à juger</strong> (profession, sport ou activité, nationalité ou
origine, événement, âge) : ils restent lisibles, et peuvent aider à reconnaître
la personne. Cliquez-les pour les masquer.</div>
{''.join(parties)}
</main>
<script>const JETON = {json.dumps(jeton)};
{_SCRIPT}</script></body></html>"""


def servir(session: SessionRevue, textes: dict[str, str],
           port: int = 0, ouvrir: bool = True, pret=None) -> SessionRevue:
    """Sert la page de revue et rend la session une fois validee.

    Bloque jusqu'a la validation. Le serveur n'ecoute que sur la boucle locale :
    la page affiche des donnees de patients en clair et ne doit sortir de la
    machine sous aucune forme.

    ``pret`` est appele avec l'adresse des que le serveur ecoute. Le port etant
    choisi par le systeme, c'est le seul moyen pour un appelant — un test, par
    exemple — de savoir ou frapper.
    """
    fini = threading.Event()

    # Un jeton par session, tire au hasard et jamais reutilise. Ecouter sur la
    # boucle locale n'est pas une protection : tout processus de la machine
    # peut frapper ce port, et la page affiche un dossier patient en clair.
    jeton_session = nouveau_jeton()

    class Poignee(PoigneeBase):
        jeton = jeton_session

        def do_GET(self):
            if not self.hote_admis():
                return self.refus(b"hote refuse")
            adresse = urlparse(self.path)
            if adresse.path not in ("/", "/index.html"):
                return self.repondre(404, b"introuvable")
            if not self.jeton_admis(parse_qs(adresse.query).get("jeton", [""])[0]):
                return self.refus()
            page = page_html(session, textes, self.jeton).encode("utf-8")
            self.repondre(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            if not self.hote_admis():
                return self.refus(b"hote refuse")
            if not self.jeton_admis(self.jeton_de_l_entete()):
                return self.refus()
            taille = int(self.headers.get("Content-Length") or 0)
            brut = self.rfile.read(taille).decode("utf-8") or "{}"
            try:
                charge = json.loads(brut)
            except json.JSONDecodeError:
                return self.repondre(400, b"json invalide")

            chemin = urlparse(self.path).path
            if chemin == "/ajout":
                partie = charge.get("partie", "")
                try:
                    ajouter(session, partie, textes[partie],
                            int(charge["debut"]), int(charge["fin"]))
                except (KeyError, ValueError) as exc:
                    return self.repondre(400, str(exc).encode("utf-8"))
                session.enregistrer()
                return self.repondre(200, b"ok")

            if chemin == "/decisions":
                decider(session, charge)
                session.enregistrer()
                self.repondre(200, b"ok")
                fini.set()
                return

            self.repondre(404, b"introuvable")

    serveur = ServeurLocal((HOTE, port), Poignee)
    # Renseigne apres le bind : le port est choisi par le systeme.
    Poignee.hotes = hotes_admis(serveur.server_port)
    adresse = f"http://{HOTE}:{serveur.server_port}/?jeton={jeton_session}"
    fil = threading.Thread(target=serveur.serve_forever, daemon=True)
    fil.start()
    if pret is not None:
        pret(adresse)
    else:
        print(f"  Revue ouverte sur {adresse}")
    if ouvrir:
        webbrowser.open(adresse)
    try:
        fini.wait()
    except KeyboardInterrupt:
        print("\n  Revue interrompue — les detections restent toutes masquees.")
    finally:
        serveur.shutdown()
    return session
