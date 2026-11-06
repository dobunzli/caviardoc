"""Socle commun aux serveurs locaux : boucle locale, jeton, en-tetes.

Deux surfaces web coexistent dans ce projet — la revue d'un document et
l'interface d'accueil — et elles affichent l'une comme l'autre des donnees de
patients en clair. Les regles d'acces ne doivent pas etre reecrites deux fois :
une divergence entre les deux serait une faille, et une faille silencieuse.

Ce module n'utilise que la bibliotheque standard.
"""

from __future__ import annotations

import hmac
import os
import secrets
import shutil
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

HOTE = "127.0.0.1"

#: En-tete portant le jeton sur les requetes d'ecriture.
#:
#: Un en-tete, et non un champ de formulaire : un en-tete non standard oblige
#: le navigateur a un controle prealable CORS, auquel ces serveurs ne repondent
#: pas. Une page tierce ne peut donc pas poster ici a l'insu de l'utilisateur.
ENTETE_JETON = "X-Caviardoc-Jeton"


def nouveau_jeton() -> str:
    """Jeton d'une session. Tire au hasard, jamais reutilise, jamais journalise."""
    return secrets.token_urlsafe(32)


def desactiver_telemetrie() -> None:
    """Coupe la telemetrie d'ONNX Runtime : la page annonce que tout reste ici.

    Les versions officielles de la bibliotheque envoient des evenements
    d'utilisation a un tiers. Deja fait a l'import du paquet ; repete ici
    par prudence, par le meme et unique mecanisme (`telemetrie.couper`).
    """
    from . import telemetrie
    telemetrie.couper()


class ServeurLocal(socketserver.ThreadingMixIn, HTTPServer):
    """`HTTPServer` sans resolution DNS inverse, une requete par fil.

    `HTTPServer.server_bind` appelle `socket.getfqdn(host)` pour renseigner
    `server_name`. Sur une machine dont le resolveur ne repond pas — un runner
    d'integration continue, un poste hors ligne, un reseau lent — cette
    resolution inverse d'une adresse de boucle locale peut bloquer plusieurs
    dizaines de secondes avant d'echouer. Le serveur mettait alors ce temps a
    demarrer, et l'utilisateur attendait devant un navigateur vide.

    `server_name` ne sert qu'a renseigner l'en-tete `Server:` et les CGI. On
    l'ecrit directement : personne n'interroge ces pages depuis l'exterieur.

    Un fil par requete : la page interroge, a l'ouverture, le depot distant
    (`git fetch`) et la taille de l'installation. Servies l'une apres l'autre,
    ces requetes lentes retenaient les depots de documents derriere elles.
    """

    daemon_threads = True

    #: Au plus ce nombre de requetes servies en meme temps ; les suivantes sont
    #: refusees poliment (503) plutot que de creer un fil de plus.
    fils_max = 16

    #: Delai par operation sur une connexion (lecture ou ecriture), pas par
    #: requete : un long telechargement qui avance n'expire pas.
    delai_connexion = 30.0

    #: `SO_REUSEADDR` sous macOS et Linux seulement. Sous Windows il autorise un
    #: second processus a se lier au meme port que l'ecouteur vivant, et a
    #: recevoir une partie des requetes, jeton compris. Ailleurs il refuse
    #: toujours un ecouteur vivant, mais laisse reprendre un port dont les
    #: connexions fermees attendent encore (TIME_WAIT) : sans lui, le
    #: redemarrage apres l'installation du moteur, qui rouvre le meme port pour
    #: garder la page, echouait sous macOS (« Address already in use »).
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, *args, **kwargs):
        # Tout serveur local passe ici : l'accueil, la revue et l'installateur.
        desactiver_telemetrie()
        self._places = threading.BoundedSemaphore(self.fils_max)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._places.acquire(blocking=False):
            try:
                request.settimeout(2)
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._places.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._places.release()

    def _preparer_socket(self):
        """Sous Windows, reserve l'adresse a ce processus (`SO_EXCLUSIVEADDRUSE`)."""
        exclusif = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusif is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusif, 1)

    def server_bind(self):
        self._preparer_socket()
        socketserver.TCPServer.server_bind(self)
        hote, port = self.server_address[:2]
        self.server_name = hote
        self.server_port = port


# --------------------------------------------------------------------------- #
# Une seule page a la fois
# --------------------------------------------------------------------------- #
#
# Un second double-clic sur le lanceur — « rien ne se passe, je reclique » —
# ouvrait une seconde page, servie par un second processus : deux sessions,
# deux tables de pseudonymes, et le lot de la premiere dans un onglet oublie.
# La page ouverte note donc ou elle ecoute ; le lancement suivant la rouvre.
#
# Le jeton est ecrit sur le disque, dans le repertoire de donnees de
# l'utilisateur. Il protege la page des autres sites web, pas des autres
# programmes de la meme session : ceux-la lisent deja ses fichiers.

FICHIER_SESSION = "session.json"

#: Reponse de `/vivant` : ce qui distingue notre page d'un autre programme
#: qui aurait repris le port depuis.
SIGNE_VIVANT = b"caviardoc"


def _fichier_session() -> Path:
    from . import paths
    return paths.data_dir() / FICHIER_SESSION


def noter_session(port: int, jeton: str) -> None:
    """Retient l'adresse de la page ouverte. Sans bruit si c'est impossible."""
    import json
    import os
    try:
        fichier = _fichier_session()
        fichier.parent.mkdir(parents=True, exist_ok=True)
        descripteur = os.open(fichier, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                              0o600)
        with os.fdopen(descripteur, "w", encoding="utf-8") as flux:
            json.dump({"port": port, "jeton": jeton}, flux)
    except OSError:
        pass


def oublier_session(jeton: str) -> None:
    """Efface la note, si c'est bien la notre : une page plus recente a pu
    la remplacer."""
    import json
    fichier = _fichier_session()
    try:
        if json.loads(fichier.read_text(encoding="utf-8")).get("jeton") == jeton:
            fichier.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def preuve_du_jeton(jeton: str, defi: str, port: int) -> str:
    """HMAC-SHA256(jeton, "preuve:<port>:<defi>"), en hexadecimal.

    Le port de celui qui repond entre dans le message : une reponse qu'un
    intrus aurait relayee depuis la vraie console, sur un autre port, ne
    verifie donc pas.
    """
    message = f"preuve:{int(port)}:{defi}"
    return hmac.new(jeton.encode("utf-8", "replace"),
                    message.encode("utf-8", "replace"), "sha256").hexdigest()


def session_ouverte() -> str | None:
    """L'adresse d'une page Caviardoc qui repond encore, ou None.

    Le port note peut avoir ete repris par un autre programme. Avant d'envoyer
    le jeton, on lui soumet un defi aleatoire : seul le vrai serveur, qui
    connait le jeton, sait rendre HMAC-SHA256(jeton, defi). Le jeton ne
    traverse la connexion qu'apres cette preuve. ``None`` si elle manque.
    """
    import json
    import urllib.parse
    import urllib.request
    try:
        note = json.loads(_fichier_session().read_text(encoding="utf-8"))
        port, jeton = int(note["port"]), str(note["jeton"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # Sans mandataire : un proxy d'entreprise configure sur le poste n'a rien
    # a voir avec la boucle locale.
    ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    defi = secrets.token_hex(16)
    try:
        with ouvreur.open(f"http://{HOTE}:{port}/preuve?"
                          + urllib.parse.urlencode({"defi": defi}),
                          timeout=3) as reponse:
            rendue = reponse.read(128).decode("ascii", "replace").strip()
    except (OSError, ValueError):
        return None
    if not hmac.compare_digest(
            rendue.encode("utf-8"),
            preuve_du_jeton(jeton, defi, port).encode("utf-8")):
        print("Une page Caviardoc enregistrée ne s'est pas authentifiée ; "
              "une nouvelle console est ouverte.", file=sys.stderr)
        return None
    requete = urllib.request.Request(f"http://{HOTE}:{port}/vivant",
                                     headers={ENTETE_JETON: jeton})
    try:
        with ouvreur.open(requete, timeout=3) as reponse:
            if reponse.read(64) != SIGNE_VIVANT:
                return None
    except (OSError, ValueError):
        return None
    return f"http://{HOTE}:{port}/?jeton={jeton}"


def hotes_admis(port: int) -> set[str]:
    """Valeurs acceptables de l'en-tete `Host` pour un serveur sur ``port``."""
    return {f"{HOTE}:{port}", f"localhost:{port}"}


class PoigneeBase(BaseHTTPRequestHandler):
    """Controles d'acces communs. Les sous-classes renseignent deux attributs.

    ``jeton`` et ``hotes`` sont poses par le serveur qui instancie la poignee ;
    ils ne peuvent pas etre connus a la definition de la classe, le port etant
    choisi par le systeme au moment du bind.
    """

    jeton: str = ""
    hotes: set[str] = frozenset()
    #: Delai de chaque lecture ou ecriture sur la connexion (cf. ServeurLocal).
    timeout = 30

    def log_message(self, *a):        # silence : le terminal sert au rapport
        pass

    def repondre(self, code: int, corps: bytes = b"",
                 type_: str = "text/plain; charset=utf-8",
                 entetes: dict[str, str] | None = None) -> None:
        self._debuter(code, type_, len(corps), entetes)
        self.wfile.write(corps)

    def envoyer_fichier(self, chemin: Path, nom: str) -> None:
        """Un fichier a telecharger, lu par blocs plutot que charge d'un coup.

        L'archive d'un dossier entier peut peser des centaines de mega-octets.
        """
        self._debuter(200, "application/octet-stream", chemin.stat().st_size,
                      {"Content-Disposition":
                       f"attachment; filename*=UTF-8''{quote(nom)}"})
        with chemin.open("rb") as flux:
            shutil.copyfileobj(flux, self.wfile, 1 << 20)

    def _debuter(self, code: int, type_: str, longueur: int,
                 entetes: dict[str, str] | None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(longueur))
        # La page ne doit etre ni mise en cache ni encadree par une autre.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for cle, valeur in (entetes or {}).items():
            self.send_header(cle, valeur)
        self.end_headers()

    def refus(self, motif: bytes = b"jeton absent ou invalide") -> None:
        self.repondre(403, motif)

    def hote_admis(self) -> bool:
        """L'appelant s'adresse-t-il bien a la boucle locale, nommement ?

        Sans ce controle, n'importe quelle page web peut faire pointer son
        propre domaine vers 127.0.0.1 — « DNS rebinding ». Le navigateur la
        traite alors comme de meme origine que celle-ci et peut lire le
        document affiche. L'attaque se reconnait a l'en-tete `Host`, qui porte
        le domaine de l'attaquant et non le notre.
        """
        return (self.headers.get("Host") or "").strip() in self.hotes

    def jeton_admis(self, fourni: str) -> bool:
        """Jeton fourni = jeton de la session ? Ne leve jamais.

        Compare des octets : ``compare_digest`` refuse les ``str`` non ASCII.
        """
        try:
            return hmac.compare_digest(fourni.encode("utf-8"),
                                       self.jeton.encode("utf-8"))
        except (AttributeError, UnicodeError, TypeError):
            return False

    def repondre_preuve(self, defi: str) -> None:
        """Repond au defi du client sans rien reveler du jeton."""
        if not defi or len(defi) > 256:
            return self.repondre(400, b"defi invalide")
        port = self.server.server_address[1]
        self.repondre(200,
                      preuve_du_jeton(self.jeton, defi, port).encode("ascii"))

    def jeton_de_l_entete(self) -> str:
        return self.headers.get(ENTETE_JETON) or ""
