# Caviardoc — documentation technique

> Cette page s'adresse à qui veut comprendre ou auditer l'outil. Pour l'installer et
> s'en servir, voir le [README](../../README.md).

Anonymiseur de documents fonctionnant entierement en local. Detecte les
donnees personnelles dans des documents francais et suisses romands, et les
remplace sans detruire le reste du texte.

**Plateformes visees : macOS et Windows.** Ce sont elles qui seront
distribuees, et elles seules engagent le projet. Le code tourne aussi sous
Linux — l'integration continue l'y verifie — mais aucune version n'y est
publiee.

> **Etat : en developpement.** Le moteur de detection, la chaine PDF — OCR
> comprise — et la chaine .docx fonctionnent. Les deux premieres sont mesurees
> sur un corpus de regression **fabrique**, reproductible : `python
> programme/tools/mesurer.py`. Un corpus medical reel a servi une fois, en septembre
> 2026, puis a ete detruit — ses chiffres figurent plus bas, mais ils ne
> peuvent plus etre refaits. L'interface locale — installation guidee, depot,
> relecture, caviardage — fonctionne dans le navigateur, sans dependance
> graphique. La mesure guidee de l'onglet Installation est enregistree (reussie ou
> non) ; si elle n'est pas tenue, la console affiche un bandeau rouge et marque
> chaque document « a verifier », sans bloquer le traitement.

## Principes

**La detection rend des offsets, jamais du texte reecrit.** Chaque entite est
reperee par ses positions exactes dans le document ; le remplacement se fait de
droite a gauche, ce qui garantit qu'aucune substitution n'en corrompt une
autre. Un modele generatif n'intervient jamais dans cette etape : il ne produit
pas d'offsets fiables.

**Le caviardage porte sur les pixels.** Le PDF produit ne contient aucune couche
texte : il n'y a rien a extraire, rien sous les rectangles. C'est la reponse a
l'echec de caviardage le plus repandu — un rectangle noir dessine par-dessus un
texte qui reste integralement selectionnable.

**Chaque document produit est relu.** Absence de couche texte residuelle, puis
nouvelle reconnaissance optique pour confirmer que les chaines masquees ont
bien disparu. Un document non verifie est signale comme tel : ce n'est pas un
document sain, c'est un document inconnu.

**En cas de doute, on masque.** Deux detecteurs qui se chevauchent voient leurs
spans reunis plutot qu'arbitres ; un document dont le patient n'est pas
identifie repasse au niveau complet ; le seuil de detection est volontairement
bas. Le cout d'un mot masque en trop est sans commune mesure avec celui d'une
identite qui subsiste.

## Architecture

Trois couches, dont la derniere est facultative.

| Couche | Role | Sur 8 Go |
|---|---|---|
| 1. Regles deterministes | AVS, IBAN, cartes, telephones, e-mails — avec sommes de controle | negligeable |
| 2. Modele de spans multilingue | noms, adresses, fonctions, employeurs | ~1,8 Go |
| 3. Arbitre generatif | levee d'ambiguite sur les cas douteux | desactive |

La couche 3 s'appuie sur Apple Foundation Models (macOS 26) ou llama.cpp
ailleurs. Elle est **desactivee par defaut sous 12 Go** : un modele generatif
local ajoute 2,5 Go et fait basculer une machine a 8 Go dans le swap.

Le modele de la couche 2 est `gliner_multi_pii-v1` en ONNX (Apache-2.0), dans
une precision qui depend du processeur : **fp16** sur arm64 (Mac, Windows ARM),
ou il est natif, **fp32** ailleurs, parce qu'onnxruntime n'a pas de noyau fp16
pour `MatMul` sur un processeur x86 (922 s pour un seul document sur un Windows
reel). Les deux rendent 97,3 % de rappel. Voir `materiel.py`.

La telemetrie d'onnxruntime (client 1DS vers `mobile.events.data.microsoft.com`)
est coupee : `ORT_DISABLE_TELEMETRY=1` est pose a l'import du paquet, avant tout
chargement de la bibliotheque. Voir `telemetrie.py`.

Le defaut a longtemps ete int8, pour tenir sous 8 Go. La campagne de mesure du
6 septembre 2026 l'a ecarte, et il a ete retire du registre le 4 octobre :
**int8 rend des resultats dependants du processeur** et n'a atteint le seuil de
90 % de rappel sur aucune machine mesuree — 62 % sur deux machines arm64
independantes, 51 % sur x86-64. Le surcout en memoire des modeles flottants
achete un outil qui fait ce qu'il annonce ; c'est un prix, pas un arbitrage.
Le seuil de detection reste volontairement bas (0,35) : la quantification
abaisse les scores de confiance, et le cout d'un mot masque en trop est sans
commune mesure avec celui d'une identite qui subsiste. Les variantes GLiNER
plus legeres (44 Mo, 79 Mo) reposent sur des encodeurs anglophones et ne
conviennent pas au francais.

### Et les autres modeles ?

Etat des lieux du 1er octobre 2026. Aucun ne remplace aujourd'hui le modele
retenu sans avoir ete **mesure sur le corpus de l'outil** : un chiffre annonce
par son auteur, sur ses propres donnees, ne dit rien de ce qu'il fera sur une
lettre de sortie romande.

| Modele | Francais | Licence | Ce qui l'ecarte pour l'instant |
|---|---|---|---|
| `knowledgator/gliner-pii` base/large/edge v1.0 | non | Apache-2.0 | encodeur anglophone (deberta-v3-small) |
| `nvidia/gliner-PII` | non | NVIDIA Open Model | anglophone, 570 M de parametres |
| `knowledgator/gliner-x` base (988 Mo) / large (1,7 Go), en fp16 | oui | Apache-2.0 | NER generaliste, pas specialise PII ; pas d'ONNX ; exige `stanza` pour decouper les mots ; a mesurer |
| `fastino/gliner2-privacy-filter-PII-multi` (205 M) | oui | Apache-2.0 | architecture GLiNER2 : autre bibliotheque, pas d'ONNX |
| OpenMed PII French, SuperClinical Small (540 Mo) a Large (1,7 Go) | oui | Apache-2.0 | classification de jetons : demande un adaptateur ; encodeurs DeBERTa-v3 anglophones affines sur des donnees francaises synthetiques generalistes (AI4Privacy) ; pas d'ONNX |
| `AP-HP/eds-pseudo-public` | oui, clinique | BSD-3-Clause | acces soumis a conditions et jeton Hugging Face ; bibliotheque edsnlp ; formats francais (NIR, IPP) |

Les deux derniers sont les plus prometteurs pour des documents medicaux
francophones, OpenMed parce qu'il est entraine pour le francais et eds-pseudo
par le domaine. Ni l'un ni l'autre n'est un GLiNER, mais le moteur n'appelle le
modele qu'a un seul endroit (`predict_entities`) : il suffit d'un adaptateur
qui traduise leurs etiquettes dans les notres, puis d'une campagne de mesure.
Les tailles annoncees en millions de parametres trompent : le « Small 44M »
d'OpenMed pese 540 Mo, autant que le modele actuel. L'onglet
Installation est pret a en proposer plusieurs, avec leur taille, la memoire
qu'ils demandent et le modele conseille pour la machine, des qu'un deuxieme
aura passe le seuil de 90 %.

## Un seul fichier

Double-cliquez **`▶ Ouvrir Caviardoc (Mac)`** ou
**`▶ Ouvrir Caviardoc (Windows)`**, en tete du dossier. C'est le seul
fichier dont vous ayez besoin : il trouve ou installe Python, puis ouvre la page
dans votre navigateur. Le « ▶ » les place en tete de la liste.

A cote d'eux, il n'y a que ce README, la licence et le dossier `programme/`.
Celui-ci contient tout le reste — le code, les scripts que le lanceur utilise
lui-meme, les tests — et il n'y a pas a l'ouvrir.

La page a deux onglets — **Caviarder** pour travailler, **Installation**
pour installer, mettre a jour et desinstaller — et s'ouvre sur celui qui sert :
l'installation tant qu'il manque quelque chose, l'application des que tout est
pret. Il n'y a plus a savoir lequel de deux fichiers ouvrir, ni a se souvenir
qu'une commande existe pour la mise a jour.

## Installation

Aucun prerequis. Si Python 3.11 ou superieur est deja la, il est utilise ; sinon
le script propose de l'installer, puis le fait.

```bash
./programme/bootstrap.sh
```

Sous Windows :

```powershell
.\programme\bootstrap.ps1
```

Installer Python demande d'ordinaire d'aller chercher un installateur sur
python.org, avec les droits d'administrateur et une case a cocher qu'on oublie.
Le script s'en charge : quand aucun interpreteur ne convient, il annonce ce
qu'il va telecharger et d'ou — [uv](https://github.com/astral-sh/uv), sous
licence Apache-2.0/MIT — **et attend votre accord**. Rien ne part sur le reseau
avant. uv installe ensuite un Python autonome dans votre dossier personnel :
sans mot de passe administrateur, sans toucher au Python du systeme, et sans
rien ajouter au PATH. uv n'est pas pris par le script `install.sh` d'Astral mais
par l'archive GitHub de la version epinglee (0.12.14), dont le SHA-256 est
verifie avant toute extraction ; un fichier different est refuse.

C'est aussi ce qui ecarte un piege : sur un macOS d'origine, `python3` existe
mais vaut 3.9. L'ancien script le prenait et echouait plus loin, sur un message
sans rapport. Les lanceurs verifient desormais la version, pas la presence.

Le script commence ensuite par un diagnostic — ce qui est present, ce qui
manque, et comment le corriger — puis installe le reste dans un environnement
place **hors du dossier synchronise** : les dependances Python, le modele de
detection, et Tesseract, obligatoire pour tout PDF : sans lui (ou sans le francais), Caviardoc
refuse le document et n'ecrit rien. Chaque etape
annonce sa taille et sa provenance avant de telecharger quoi que ce soit.
Chaque fichier est verifie par empreinte SHA-256 ; un telechargement altere est
rejete sans rien installer.

**Rien n'exige de droits d'administrateur, et rien n'est pose hors du dossier
de l'application.** Tesseract vient de conda-forge plutot que de `brew` ou
`winget` pour cette raison : sur un poste de cabinet ces commandes echouent,
faute d'Homebrew ou faute de droits, et elles installeraient de toute facon
l'OCR pour la machine entiere — ce qui deborde de ce qu'un anonymiseur de
documents a a faire. Sur les 206 Mo telecharges, 129 sont conserves : les 124
langues dont nous ne nous servons pas sont retirees apres installation.

Un Tesseract deja present sur la machine et pourvu du francais est utilise tel
quel ; on n'y touche pas.

Une fois l'installation faite, la commande `caviardoc` est disponible dans
l'environnement :

```bash
# macOS et Linux
"$HOME/Library/Application Support/caviardoc/venv/bin/caviardoc" selftest
```

Le chemin exact sur votre machine est donne par `./programme/bootstrap.sh` ou
par `PYTHONPATH=programme/src python3 -m caviardoc info`.

## Desinstallation

Onglet **Installation** de la page, section « Desinstallation ». En dehors de
la page : `./programme/desinstaller.sh`, ou `caviardoc desinstaller`.

Elle montre ce qui sera supprime, chemin par chemin et taille par taille, puis
demande confirmation. Ce qui part : l'environnement virtuel, le modele,
Tesseract, l'etat de la machine, vos exceptions, le cache de bytecode — soit
tout ce que l'installation a telecharge, plus d'un gigaoctet en general.

Deux choses ne partent jamais, et sont montrees sans etre touchees :

- **Le dossier global de `uv`** (`~/.local/share/uv`). Il contient son cache et
  les interpreteurs Python qu'il gere, y compris ceux poses pour d'autres
  projets. Avoir installe `uv` ne fait pas de nous le proprietaire de tout ce
  que `uv` y range ensuite. La commande pour s'en occuper est affichee ; c'est
  `uv` qui sait ce qu'il peut retirer sans casser autre chose.
- **Le depot lui-meme**, que vous avez cree et depuis lequel le lanceur
  s'execute. Son chemin est rappele ; supprimez-le a la main.

Le binaire `uv` n'est repris que si un lanceur a note l'avoir installe. Trouve
sans cette trace — venu d'Homebrew, de winget, ou de vos propres mains — il est
montre mais laisse en place, sauf `--tout`.

## Mise a jour

Deux canaux, selon la facon dont le projet a ete obtenu :

- **une copie git** : `git fetch`, puis `git merge --ff-only` — jamais de
  fusion, et rien ne s'applique sur un arbre modifie ;
- **une copie telechargee en ZIP** depuis GitHub : `git archive`, qui produit
  ce ZIP, inscrit le commit dans `src/caviardoc/version_archive.py` (attribut
  `export-subst`). La verification le compare a la branche publiee par l'API
  de GitHub ; l'application telecharge l'archive de ce commit precis, en
  controle chaque chemin, puis remplace les fichiers. Les deux canaux passent
  par HTTPS : la confiance est la meme.

Depuis la page, la mise a jour se fait en deux temps, separes par un
redemarrage : le code d'abord, puis `uv sync`, le modele et une nouvelle
mesure, dans un processus neuf. Sous Windows, une bibliotheque deja chargee
par la page ne se remplace pas, et `uv sync` echouait des que la mise a jour
en changeait une. Les lanceurs, eux, sont lus en entier avant de lancer la
page : une mise a jour les remplace pendant qu'ils tournent.

## Commandes

| Commande | Role | Dependances |
|---|---|---|
| `caviardoc caviarder` | caviarder des PDF et des .docx | moteur + documents |
| `caviardoc doctor` | ce qui est present, ce qui manque | aucune |
| `caviardoc doctor --deep` | recalcule les empreintes du modele (2,5 s) | aucune |
| `caviardoc installer` | installation guidee dans le navigateur | aucune |
| `caviardoc maj` | verifie une nouvelle version ; `--appliquer` l'installe et remesure | aucune |
| `caviardoc interface` | deposer, relire, caviarder, recuperer | moteur + documents |
| `caviardoc setup` | installe les dependances et le modele | aucune |
| `caviardoc setup --modele CLE` | installe ce modele, et le retient pour cette machine | aucune |
| `caviardoc info` | emplacements et modele retenu sur cette machine | aucune |
| `caviardoc desinstaller` | reprend ce que l'installation a pose, apres l'avoir chiffre | aucune |
| `caviardoc selftest` | verifie le moteur de bout en bout | moteur |
| `python programme/tools/mesurer.py --enregistrer` | mesure cette machine, et retient le modele s'il tient | moteur |

Celles qui n'exigent aucune dependance fonctionnent sur un Python nu, ce qui
est precisement leur raison d'etre sur une machine neuve.
Sans installation prealable, prefixez par `PYTHONPATH=programme/src python3 -m`.

## Caviarder des documents

```bash
caviardoc caviarder -o ~/anonymises ~/Documents/*.pdf ~/Documents/*.docx
```

La chaine est choisie sur l'extension. Un format inconnu est signale et ignore
plutot que tente : mieux vaut un fichier non traite qu'un fichier qui parait
traite.

Dans l'interface, on peut aussi deposer un **dossier entier** : ses
sous-dossiers sont parcourus, seuls les PDF et les .docx sont traites, et les
autres fichiers sont comptes plutot qu'ignores en silence. « Tout telecharger »
rend une archive .zip qui reproduit l'arborescence d'origine ; les noms de
dossiers y sont anonymises comme les noms de fichiers.

### PDF : la chaine image

Chaque document est d'abord classe. Si sa couche texte est saine, elle sert a la
detection ; sinon le document est reconnu par OCR. Une couche texte saine ne dit
pourtant rien des **images posees sur la page** : sur une telle page, chaque
image assez grande pour porter du texte est lue par OCR en plus, et ce qu'elle
apporte s'ajoute a la detection. Sans cela, une page scannee a laquelle le
logiciel d'archivage avait ajoute deux lignes (« importe le… ») etait jugee
textuelle sur ces deux lignes, et le scan entier sortait en clair — annonce
« verifie », puisque la verification ne cherche que ce qui a ete detecte. Les
mots qu'un scan « interrogeable » porte deja dans sa couche invisible ne sont
pas lus deux fois. Dans tous les cas **le
caviardage porte sur les pixels** et le PDF produit ne contient aucune couche
texte : il n'y a rien a extraire, rien sous les rectangles. Le fichier produit
est ensuite relu — absence de texte residuel, puis nouvelle reconnaissance
optique — pour confirmer qu'aucune chaine masquee n'a survecu. Le nom du fichier
est anonymise lui aussi, avec les memes pseudonymes que le contenu.

Le PDF produit est un document neuf, fait des seules images des pages : rien de
l'original n'y est recopie. Proprietes (auteur, titre, mots-cles), metadonnees
XMP, pieces jointes, notes, signets et champs de formulaire restent en arriere,
sans traitement particulier. Le texte *visible* d'un champ rempli ou d'une note
posee sur la page fait, lui, partie de l'image : il est lu et caviarde comme le
reste.

### .docx : la chaine bureautique

Un .docx **est** son texte : il n'y a pas de couche sous-jacente a laisser
fuir, et un aplat noir n'y masquerait que l'affichage. Les donnees sont donc
remplacees par leurs pseudonymes dans le fichier lui-meme. Le document reste
modifiable et lisible, et son texte reste selectionnable — ce que la chaine
image, elle, ne peut pas offrir.

Le danger se deplace ailleurs. Un .docx est une archive, et ce que montre la
fenetre de Word n'en est qu'une partie. Sont donc traites aussi :

- les en-tetes, pieds de page, notes et commentaires, qui vivent dans des
  parties separees de l'archive ;
- les proprietes du document — auteur, societe, titre, dernier enregistrement —
  qui nomment souvent le patient ou le praticien sans qu'aucune fenetre les
  montre ;
- le texte *supprime* des revisions non acceptees, invisible a l'ecran et
  pourtant present dans le fichier, ainsi que le nom des relecteurs ;
- les codes de champ, les textes alternatifs d'images et les cibles de liens
  `mailto:`.

Et sont **retires**, parce qu'ils ne servent a rien au destinataire et que
chacun peut nommer le patient sans qu'aucune fenetre de Word le montre :

- la **vignette d'apercu**, image de la premiere page *avant* caviardage ;
- les **proprietes personnalisees**, ou une GED range le numero de dossier ;
- les **donnees liees aux champs** (`customXml/`), d'ou Word reprendrait a
  l'ouverture le nom que l'on vient de caviarder ;
- les **variables de document**, le **publipostage** et sa liste de
  destinataires, le **chemin du modele** ;
- les **dates** d'enregistrement et d'impression, et le titre recopie dans
  `app.xml` ;
- le chemin des liens vers des **fichiers locaux** (`C:/Patients/…`) ;
- les **metadonnees des images** : EXIF, XMP, IPTC, commentaires — auteur,
  appareil, date de prise de vue, **coordonnees GPS**. Elles sont retirees sans
  reencoder : les pixels restent identiques a l'octet pres. Seuls JPEG et PNG
  sont traites ainsi ; une image d'un autre format qui porte des metadonnees est
  signalee.

Word coupe les mots en fragments a la moindre correction : « Zorglub »
s'ecrit couramment `<w:t>Zorg</w:t><w:t>lub</w:t>`. La detection porte donc
sur le texte reconstitue, et un remplacement a cheval sur plusieurs fragments
les vide tous. Le fichier produit est relu de la meme facon : recherche dans le
texte reconstitue, qui seul revele un nom coupe en deux, **et** dans le XML
brut, ou subsistent les attributs et les cibles de liens.

Les images incorporees ne sont pas du texte : un rapport colle en capture
d'ecran ne se reecrit pas, il se noircit. Chacune est donc lue par
reconnaissance optique, comme une page de la chaine image, et ce qu'elle porte
de personnel y est masque — y compris les chaines deja reconnues dans le corps
du document, qu'un modele lit mal sur une capture de trois lignes. La relecture
du fichier produit repasse par les images : une fuite en pixels y etait
auparavant invisible, et le rapport annoncait « succes » sur un document ou le
nom du patient restait parfaitement lisible.

Une image ou rien n'a ete retenu n'est pas touchee : un logo ou une courbe de
laboratoire traverse la chaine sans une recompression. Une image qu'aucun
decodeur n'ouvre — un metafichier Windows, une animation — est signalee comme
telle : son contenu n'est ni masque ni verifie — sauf avec le masquage des
logos, actif par defaut, qui retire toutes les images d'un .docx.

Restent hors de portee les objets incorpores — un classeur, un PDF colle : leur
contenu n'est ni lu ni verifie, et le masquage des logos ne les touche pas.
Et un .docx qui
n'est qu'une enveloppe autour de pages scannees est refuse, avec la consigne de
l'exporter en PDF : la chaine image rasterise la page entiere et ne laisse
aucune couche texte residuelle, ce qu'un .docx ne peut pas offrir.

### Logos et en-tetes

Un logo est une image : ni l'OCR ni le modele ne le voient, et il identifie
pourtant l'etablissement. De meme, un bloc expediteur garde sa structure
lisible meme quand ses mots sont masques un a un. Les deux premiers masquages
sont **actifs par defaut** en ligne de commande : les laisser passer doit etre
un choix, pas l'oubli d'une option.

Dans l'interface, ils n'ont plus d'interrupteur : ils **suivent le niveau**
(`interface.zones_du_niveau`). « Toutes les personnes » les active ; « Le
patient seulement » les laisse, puisque logos et en-tetes designent les
soignants. Le nom du patient y est masque malgre tout : la detection lit les
en-tetes et pieds de page d'un .docx, le haut des pages d'un PDF et le texte
des images comme le reste du document. Le prix de la regle : au niveau complet,
une courbe ou une photo clinique integree part avec les logos.

| Masquage | Par defaut | Pour s'en passer | Effet |
|---|---|---|---|
| images et logos | actif | `--sans-logos` | masque les images integrees, en epargnant celles qui couvrent la page (le scan lui-meme) ; sur un scan, masque aussi les logos peints dans l'image de la page (voir ci-dessous) |
| en-tetes | actif | `--sans-entetes` | masque les blocs du haut de page portant une entite detectee ou un marqueur d'adresse, **entierement** — ce qui emporte aussi la specialite, la mention « Case postale » ou le titre professionnel |
| bande haute | inactif | — | `--bande-haute 0.2` noircit sans condition la fraction haute indiquee ; dernier recours, quand un logo de scan echappe a la detection ci-dessous |

Le masquage des en-tetes et `--bande-haute` ne s'appliquent qu'a la premiere
page, sauf avec `--toutes-pages`.

**Logos d'un scan.** Sur un scan, la page entiere est une seule image : le
logo n'y est qu'une tache de pixels, que la recherche d'images integrees ne
voit pas. `zones.boites_logos_dessines` le cherche dans le haut (35 %) et le
bas (10 %) de chaque page, sous deux formes : une tache de couleur, ou un aplat
sombre — le meme logo scanne en niveaux de gris. Sont ecartes le texte (moins
de deux hauteurs de mot de cote, ou couvert de mots lus), les filets et l'ombre
du bord du scan (trop allonges, ou touchant le bord de la feuille). Le milieu
de la page n'est pas examine : une courbe ou un schema du compte rendu y reste
lisible. Le logo trouve sert aussi au masquage des en-tetes, qui emporte le
nom du service compose a cote.

Sur un .docx, ou la geometrie de la page n'existe pas au moment du traitement,
ces masquages ont l'equivalent le plus proche : les images sont retirees, les
en-tetes et pieds de page sont vides, et `--bande-haute` est sans objet — la
commande le signale plutot que de l'appliquer en silence.

### Relecture avant ecriture

```bash
caviardoc caviarder -o ~/sortie --revue ~/docs/rapport.pdf
```

Ouvre le document dans le navigateur, entites surlignees, **avant** qu'un seul
pixel ait ete noirci. Un clic sur une entite retire son masque — un motif est
demande. Une selection de texte suivie de « Masquer la selection » rattrape ce
que l'outil a manque : c'est la moitie utile de l'exercice, puisque rejeter un
faux positif ameliore le confort quand rattraper un faux negatif evite une
fuite.

Dans l'interface, la relecture a trois niveaux : tout relire, ne relire que les
**doutes**, ou ne pas relire. Un doute est une detection que rien ne recoupe,
c'est-a-dire une estimation du modele (`origine == "modele"`, en orange) ;
`Detection.doute` est l'unique endroit ou ce critere se regle. En mode « doutes »,
un document sans aucun doute est valide sans etre montre, et la page de relecture
masque les parties sans doute. Pendant un lot, la relecture permet aussi de
valider d'un coup le document courant et tous les suivants, ou de ne relire que
leurs doutes ; les decisions deja prises sur le document courant sont conservees.

**Sans relecture, une confirmation a la sortie.** Le troisieme niveau
s'appelait « Je fais confiance », ce qui contredisait l'avertissement de la
page : masquer les noms ne rend pas un document anonyme. Il s'appelle « Sans
relecture ». Un document qui n'a pas ete montre a la relecture — sans
relecture, ou en relecture des doutes quand il n'en avait aucun — porte
`"relu": false` dans son bilan (`Tache.relu`). Pour le copier ou le
telecharger, il faut d'abord cocher, sous ses liens, « J'ai relu ce
document : ce qu'il raconte ne permet pas de reconnaitre la personne ».
L'archive .zip demande la meme confirmation, une fois pour tous les documents
non relus qu'elle emporte. Rien n'est bloque : la confirmation demande que le
geste soit voulu, comme celle qui precede un document a fuite.

Trois garanties :

- **Le defaut est de masquer.** Une detection non revue reste retenue, et une
  session abandonnee ne produit jamais un document moins masque qu'une
  execution sans relecture.
- **Le rejet est trace.** Retirer un masque se fait d'un clic, sans motif
  demande ; le champ `motif` reste accepte par la session mais n'est plus saisi.
- **Rien ne quitte la machine.** La page est servie sur la boucle locale et
  affiche le document en clair. La session, qui contient les entites detectees
  telles quelles, est ecrite dans le repertoire de donnees local — jamais dans
  le depot, jamais synchronisee — et supprimee des qu'elle est appliquee.

La couleur du surlignage indique la provenance : regle deterministe, modele,
propagation ou ajout humain. Retirer le masque d'une detection issue d'une
somme de controle valide est presque toujours une erreur, et se voit.

#### Quasi-identifiants : a juger, pas masques

Un extrait d'arret du Tribunal federal, caviarde sans un seul nom — un
cycliste, le Giro 2022, une blessure a la cuisse, une procedure
disciplinaire —, a suffi a une IA pour retrouver la personne. Rien de cela
n'etait cherche. La relecture signale desormais cinq categories qui ne
nomment personne mais permettent un recoupement :

| Etiquette (`engine.QUASI_IDENTIFIANTS`) | Exemple |
|---|---|
| `profession` | « cycliste professionnel », « pilote de ligne » |
| `sport ou activite` | « handball », « marathon » |
| `nationalite ou origine` | « nationalite italienne », « portugaise » |
| `evenement` | « Giro 2022 », « Fete des vignerons 2019 » |
| `age` | « 52 ans » |

Ces passages **ne sont pas masques par defaut** : masquer toute profession et
tout age viderait un dossier de ce qui le rend utile. Ils arrivent dans l'etat
`a_juger` (`revue.Etat.A_JUGER`), en violet, avec la mention « peut aider a
reconnaitre la personne ». Un clic les masque comme une detection ordinaire ;
dans le texte exporte, ils deviennent `[PROFESSION_1]`, `[AGE_1]`… Laisser
lisible un passage a juger n'est pas un rejet : rien n'en est appris, et la
machine ne retient pas « ne jamais masquer cycliste ». En relecture « doutes
seulement », un document qui porte un passage a juger est montre, et ces
passages restent en evidence. Un quasi-identifiant qui survit apres avoir ete
masque compte comme element de contexte, pas comme donnee identifiante
(`est_contextuel`).

Ils sont cherches **seulement s'il y a relecture**, par une passe du modele a
part (`pipeline.quasi_identifiants`), sur le texte d'origine, au seuil
`SEUIL_QUASI` = 0,4 au lieu de 0,12. Sans relecture, personne ne serait la
pour les juger, et le document sort exactement comme avant. Sont ecartes : ce
qui recoupe un passage deja masque, le vocabulaire neutre (`generique`), et
pour la profession le poste du medecin qui ecrit (`roles.est_poste_soignant` :
« Specialiste en ophtalmologie », « Medecin hospitalier ») ou un intitule en
capitales (« ECHO-DOPPLER VEINEUX… »). Un patient medecin n'est donc plus
signale par sa profession ; son age et le reste le sont.

Le diagnostic rare reste du ressort du medecin : aucune liste ne le couvre de
facon fiable. Generaliser un detail (« cycliste professionnel » -> « patient
sportif ») ne se fait pas dans l'outil : on masque, ou l'on reecrit soi-meme
le texte exporte.

### Format de sortie

| `--format` | Usage |
|---|---|
| `pdf` (defaut) | document caviarde, pour un lecteur humain : au pixel pour un PDF, par pseudonymes pour un .docx |
| `texte` | texte pseudonymise a pseudonymes stables — **le format le plus simple a coller dans un modele de langage** |
| `les-deux` | les deux fichiers |

Un PDF rasterise envoye a un modele de langage devra etre repasse par une
reconnaissance optique : couteux en jetons, et la structure du document se perd
en chemin, alors qu'elle a deja ete extraite proprement ici. L'export texte
evite ce detour.

Les entites y sont remplacees par des pseudonymes numerotes et **stables sur
tout le lot** : `PERSONNE_2` designe le meme patient d'un document a l'autre.
Un texte ou toutes les personnes s'appellent `[PERSONNE]` est inexploitable par
un modele, qui ne peut plus distinguer le patient du medecin.

**Le revers : le cumul.** Ce qui permet au modele de suivre `PATIENT_1` d'un
document a l'autre lui permet aussi d'additionner ce que chacun en dit — une
profession ici, un evenement la, un age ailleurs. Deux documents anodins
separement peuvent, ensemble, rendre la personne reconnaissable. La table de
pseudonymes vit le temps d'une demande (`Atelier.table`) : « Nouvelle
demande » la remet a zero, et c'est ce qu'il faut faire pour des documents
qui n'ont pas a etre rapproches. L'interface le dit sous le choix du format.
Les dates relatives, elles, ne se cumulent pas : chaque document a son J0.

Une personne recoit en outre son **role**, quand le document le donne
(`roles.qualifier_personnes`) : `PATIENT_1` pour le patient (reconnu comme au
niveau `patient`), `MEDECIN_2_CARDIOLOGUE` ou `SOIGNANT_1_PHYSIOTHERAPEUTE` pour
un soignant dont la specialite figure a cote du nom — apres lui dans un
en-tete, avant lui dans « le rhumatologue, Dr Mercier ». La specialite se
cherche en mots entiers (« cardiologue », « cardiologie ») : « examen
neurologique » decrit un examen, pas la personne voisine. Un proche reste
`PERSONNE_n`. Le role ne s'applique qu'a un pseudonyme neuf : un medecin deja
nomme dans le lot garde le sien. Se tromper de role ne fait rien fuir, le role
remplace un nom deja masque, et la specialite etait deja lisible en clair.

La verification du format texte est exacte — une comparaison de chaines, sans
repasser par une reconnaissance optique — donc plus fiable que celle du PDF.

### Niveau d'anonymisation

| `--niveau` | Effet |
|---|---|
| `complet` (defaut) | tout ce qui est detecte est masque : patient, soignants, etablissements, coordonnees |
| `patient` | seules les donnees identifiant le patient sont masquees ; les soignants, les etablissements et leurs coordonnees restent lisibles |

Le patient est reconnu a trois signaux : une date de naissance accolee au nom
(le plus sur — un en-tete de medecin ne porte jamais sa propre date de
naissance), une mention explicite (« concernant : », « le patient »), et
l'absence de titre professionnel. En cas de doute sur une personne, elle reste
masquee.

**Si aucun patient n'est identifie dans un document, le niveau `complet` lui est
applique d'office**, et le rapport le signale. Sans ce repli, un document dont
le patient n'est pas reconnu sortirait intact alors qu'une anonymisation a ete
demandee.

> **Avant un envoi vers une IA en ligne, utilisez `complet`.**
> Le nom d'un cabinet et sa localite suffisent souvent a re-identifier un
> patient par recoupement dans un canton de taille modeste, et les donnees d'un
> professionnel restent des donnees personnelles. Le niveau `patient` est
> destine au partage entre confreres, pas a l'export.
>
> `complet` est necessaire, pas suffisant. L'outil ne masque que les
> identifiants directs ; le recit clinique reste lisible, et un diagnostic
> rare, une profession ou un evenement date peuvent suffire a retrouver la
> personne, d'autant plus vite qu'une IA peut chercher sur internet. Le
> resultat est pseudonymise, pas anonyme au sens de la LPD. Si le patient reste
> reconnaissable, la transmission reste soumise au secret professionnel
> (art. 321 CP) : generaliser les details rares ou obtenir l'accord du patient.

### Dates

Une date de prelevement ou d'edition ne dit presque rien d'une personne, et
elle porte souvent l'information clinique utile. Une date de naissance, elle,
combinee au code postal et au sexe, identifie une personne dans la grande
majorite des cas. Les deux ne peuvent donc pas suivre la meme regle.

| `--dates` | Effet |
|---|---|
| `masquer` (defaut) | toutes les dates sont masquees |
| `relatives` | chaque date devient son ecart en jours avec la plus ancienne du document : `J0`, `J+4`, `J+371` ; **la date de naissance reste masquee** |
| `garder` | les dates restent visibles, **sauf la date de naissance** |
| `garder-tout` | toutes les dates restent visibles, date de naissance comprise |

**Dates relatives.** Dans l'extrait d'arret du Tribunal federal qu'une IA a
re-identifie, la date ancrait le recoupement : un evenement date se cherche.
L'information clinique, elle, est presque toujours dans l'intervalle — trois
jours entre la chute et l'operation, un an entre deux controles. `relatives`
garde les intervalles sans donner de date (`dates_relatives.py`) :

- J0 est la plus ancienne des dates masquees **apres la relecture**, sur tout
  le document. Chaque document a la sienne : deux documents d'un lot ne
  partagent pas d'origine, et leurs dates ne se recoupent pas.
- La date de naissance reste masquee comme avant (`[DATE_DE_NAISSANCE_1]`) :
  son ecart a J0 donnerait l'age au jour pres. Elle ne compte pas pour J0.
- Une date qu'on ne sait pas situer — « 1er mars » sans annee, « mars 2025 »
  sans jour — reste masquee (`[DATE_1]`) : un J+n approximatif ferait passer
  une estimation pour un intervalle.
- Annee a deux chiffres : au-dela de l'annee en cours, le siecle precedent
  (« 26.05.55 » = 1955).
- Texte et .docx : `[J+4]` a la place de la date. PDF : le rectangle noir
  porte le libelle ecrit en blanc ; le fichier reste fait d'images.

Les intervalles eux-memes restent une information : J+371 entre deux
evenements connus par ailleurs peut aider a les reconnaitre. C'est souvent un
meilleur choix que « toutes masquees » ou « toutes conservees », pas une
garantie.

Les dates ecrites en lettres sont reconnues comme celles en chiffres :
« 5 avril 2014 », « 1er mars », « 12 janv. 2024 », « mars 2025 ». Un mois seul
(« revu en mars ») n'en est pas une.

**Une date se reconnait a sa forme, pas a l'etiquette du modele.** Les dates
en chiffres — annee sur quatre chiffres ou sur deux, « 26.1.26 » comme
« 26.05.1955 » — sont trouvees par une regle. Le reglage s'applique aussi a
toute detection du modele qui a la forme d'une date, quelle que soit son
etiquette. Sur un tableau d'antecedents reel, le modele rendait « 11.2.16 » en
« numero AVS » et le seul « 16 » de « 28.12.16 » : avec `garder-tout`, la
colonne Date sortait a moitie noircie. Une date collee au bord d'un nom
(« Dupont 12.3.55 ») en est detachee ; une date au milieu d'un span le laisse
entier, donc masque.

La date de naissance est reconnue, dans l'ordre :

1. a la mention qui la precede immediatement — « ne le », « Date de
   naissance : ». Le marqueur doit toucher la date : seuls des blancs ou de la
   ponctuation peuvent s'intercaler, faute de quoi un « ne le » situe plus haut
   ferait passer la date de prelevement pour une date de naissance ;
2. au nom de personne qui la precede, comme dans un en-tete « Dupont Jean
   26.05.1955 ». Accolee au seul nom d'un soignant ou d'un etablissement
   (« Cabinet Medical … 30.09.2026 »), c'est une date d'impression ;
3. a l'etiquette du modele, retenue seulement pour une annee a quatre chiffres
   sans heure accolee. Le modele pose « date de naissance » sur des
   antecedents dates « 15.9.11 » : s'y fier seul masquait presque toute la
   colonne.

## Ce que la machine retient de vos corrections

Le modèle est figé : **il n'apprend pas**. Ce qui apprend, c'est le poste, en
gardant les décisions de relecture et en les rejouant sur les documents
suivants. Deux listes, dans le répertoire de données local — jamais dans le
dépôt.

Elles ne sont pas symétriques, et c'est le cœur du dispositif : se tromper
dans un sens masque un mot de trop, se tromper dans l'autre découvre une
chaîne dans tous les documents à venir, en silence.

| | « toujours masquer » | « jamais masquer » |
|---|---|---|
| Vient de | un masque ajouté à la main | un masque retiré |
| Sens de l'erreur | un mot masqué pour rien | une identité découverte partout |
| Actif | immédiatement | immédiatement |
| Conservation | empreinte SHA-256 salée | chaîne en clair |

**Rien de ce qu'une règle à somme de contrôle reconnaît n'est jamais appris.**
Numéro AVS, IBAN, carte, téléphone, e-mail : ces règles valident une somme de
contrôle, elles ne se trompent pratiquement pas, et ce qu'elles trouvent est
par construction un identifiant personnel. Retirer un tel masque vaut pour un
document, jamais pour tous les suivants. Il se trouve que cette règle règle
aussi la conservation : ce qui reste apprenable en clair est du vocabulaire —
« Macula », « Cher confrère », le nom d'un établissement — et non des
identités.

**La liste « toujours masquer » ne contient aucun nom lisible.** Elle est
faite, par nature, des identités que l'outil avait manquées : un fichier
énumérant tous les patients passés par le cabinet serait un problème plus
grave que celui qu'il résout. Seule une empreinte SHA-256 salée est conservée,
avec un sel propre à la machine — deux cabinets qui suivent le même patient
n'obtiennent pas la même empreinte, et le fichier ne peut pas servir à tester
si un nom donné y figure. La reconnaissance se fait dans l'autre sens : les
n-grammes du document sont hachés et comparés à l'index.

Quand les deux listes se contredisent sur la même chaîne, **« toujours »
l'emporte** — le sens sûr gagne.

L'interface montre ce qui a été retenu et permet de la retirer de la liste d'un clic. Une
correction écartée est signalée avec son motif : sans cela, l'utilisateur la
referait indéfiniment sans savoir qu'elle n'est pas gardée.

## Plusieurs machines

Git transporte le code ; le modele, le venv et les caches restent locaux a
chaque machine. Sur un nouveau poste :

```bash
gh repo clone dobunzli/caviardoc
cd caviardoc
./programme/bootstrap.sh
```


## Proposer un nouveau modele

Un modele plus performant s'annonce a l'utilisateur sans rien lui imposer. Le
catalogue (`REGISTRY` dans `models.py`) dit lui-meme quel modele en surpasse
d'autres ; la console fait le reste.

1. Ajouter un `ModelSpec` : depot, revision, `RemoteFile` avec `sha256` et
   taille de chaque fichier, `memoire`, licence.
2. Renseigner `remplace=("cle-ancienne",)` (les modeles qu'il surpasse) et, si
   utile, `nouveautes="..."` : une ou deux phrases montrees dans la proposition.
3. Mesurer avec `python programme/tools/mesurer.py` et reporter le rappel dans
   `rappel_reference` ; un modele sous le seuil de 90 % doit rester `deconseille`.
4. Publier la mise a jour de l'application.

Chez l'utilisateur, `models.modele_propose` retient le modele qui remplace celui
en service, n'est pas deconseille, tient en memoire, n'est pas installe et n'a
pas ete decline. L'onglet Installation affiche alors l'avis (et une pastille sur
l'onglet) avec « Installer » (qui le met en service), « Plus tard » (jusqu'a la
fin de la session) et « Ne plus proposer » (consigne dans `state.json`, cle
`modeles_refuses`). Rien n'est telecharge sans ce clic. L'ancien modele reste en
place ; une fois le nouveau installe, sa carte indique qu'on peut le supprimer.

## Licence

Caviardoc © 2026 D. Bünzli — AGPL-3.0-or-later, sans aucune garantie.
L'onglet Installation detaille la licence de chaque composant ; chaque page en
rappelle l'essentiel, comme l'AGPL le demande des interfaces interactives.

AGPL-3.0-or-later. Ce choix est delibere : il rend utilisable MuPDF, seule
bibliotheque mature capable de retirer reellement le texte du flux de contenu
d'un PDF plutot que de le recouvrir d'un rectangle noir. Un anonymiseur dont le
code n'est pas auditable n'inspire de toute facon pas confiance.

## Qualite mesuree

Deux relevés coexistent ici, et il ne faut pas les confondre : celui que vous
pouvez refaire, et celui qui a eu lieu une fois.

### Aujourd'hui — corpus de regression, reproductible

Le corpus qui sert a mesurer est **entierement fabrique** (`programme/tests/corpus/`). Il
reproduit les *formes* relevees sur des documents medicaux reels — couche texte
degradee, pied de page en lettres espacees, tableau a colonne « Prescripteur »,
destinataire medecin, patronyme en capitales — sans en conserver la moindre
donnee. Les numeros AVS et IBAN portent des sommes de controle valides mais ne
correspondent a aucun assure.

```bash
python programme/tools/mesurer.py --details
```

| | Complet | Patient |
|---|---|---|
| Rappel | 97,3 % | 95,0 % |
| Preservation du contenu | 97,1 % | 97,1 % |
| Soignants preserves | — | 100 % |

Ces chiffres mesurent la **detection**, sur du texte. Pour mesurer la chaine
entiere — PDF rasterise, OCR, rectangles, verification :

```bash
python programme/tools/mesurer_chaine.py --details
```

Le rappel y tient (97,3 %), la preservation y perd six points : l'OCR
reassemble les lignes, et le modele detecte alors autre chose.

Chaque entite du corpus porte son role — patient, soignant, etablissement — ce
qui permet de mesurer les deux niveaux avec la meme verite terrain. La mesure
tourne a chaque arrivee sur `main`, avec des seuils de non-regression.

> **Ces chiffres valent pour la machine qui les produit.** Le rappel a dependu
> du processeur tant que le modele par defaut etait quantifie en int8 ; fp16 a
> leve ce defaut sur les trois machines mesurees, mais la regle demeure :
> mesurez la votre avant de lui confier un document.

### Une fois — corpus medical reel, non reproductible

Un corpus de douze documents reels, compare a un caviardage effectue a la main,
a servi a regler le detecteur en septembre 2026. **Il a ete detruit, comme il
devait l'etre.** Les chiffres ci-dessous ne peuvent plus etre refaits, et ils
ne sont pas comparables a ceux du corpus de regression : la verite terrain y
etait deduite par difference, ce qui compte comme « attendu » des mots que le
rectangle noir d'un relecteur avait emportes au passage.

**Identifiants du patient masques** — la mesure qui comptait :

| | Niveau complet | Niveau patient |
|---|---|---|
| Nom, date de naissance, n° AVS | 99 % | 94 % |

Contre l'ensemble du caviardage humain, toutes entites confondues :

| | Niveau complet | Niveau patient |
|---|---|---|
| Rappel | 67 % | 48 % |
| Precision | 22 % | 38 % |
| Debordement | 3 % | 5 % |

Le rappel du niveau « patient » y etait mecaniquement penalise : la reference
etait un caviardage **complet**, qui masque aussi les soignants — ce que ce
niveau s'abstient de faire deliberement. C'est pourquoi la premiere table,
restreinte aux identifiants du patient, etait la seule comparable entre les
deux niveaux.

**La reference elle-meme etait imparfaite.** Sur les douze copies caviardees a
la main, quatre presentaient un defaut : deux n'avaient jamais ete traitees,
une portait un rectangle noir par-dessus un texte intact, une avait laisse
passer une occurrence. Ces chiffres sont une borne, pas une verite.

### Quasi-identifiants — le rappel des identifiants directs ne bouge pas

Mesure du 2026-10-16, poste M4 Pro, fp16. Deux facons d'ajouter les cinq
etiquettes ont ete comparees :

| | Rappel complet | Rappel patient | Temps modele |
|---|---|---|---|
| Avant | 97,3 % | 95,0 % | reference |
| Etiquettes ajoutees a `DEFAULT_LABELS` | 97,3 % | 95,0 % | +7 % |
| Passe a part (retenue) | 97,3 % | 95,0 % | +43 %, avec relecture seulement |

Le corpus ne fait pas la difference, des phrases d'essai la font : dans une
passe commune, les etiquettes se disputent les memes mots. « Agriculteur de
63 ans » et « institutrice », masques jusque-la comme personne, devenaient des
professions laissees lisibles. La passe a part laisse les detections qui
masquent **identiques par construction** : elle ne les voit pas. C'est elle
qui est retenue, au prix du temps.

Sur le corpus, la passe signalait d'abord sept passages, presque tous du
bruit : le poste des medecins dans les en-tetes, un titre d'examen en
capitales. Apres les ecarts decrits plus haut, il en reste un (« Final »,
statut d'un resultat de laboratoire, pris pour un evenement). Sur la phrase
du Tribunal federal, « cycliste professionnel », « nationalite italienne » et
« Giro 2022 » sont signales.

### Pourquoi la precision est basse, et le reste

Le cout d'une erreur est asymetrique : un mot masque en trop gene la lecture,
un mot manque laisse fuir une identite. Le seuil de detection est donc
volontairement bas, les spans qui se chevauchent sont reunis plutot
qu'arbitres, et une personne non reconnue reste masquee.

Le « debordement » compte les mots masques a tort mais colles a une vraie
entite — un titre, une preposition emportes par la reunion des spans. Ils sont
peu nombreux et sans consequence. Le reste est du faux positif franc, sur
lequel le filtre de vocabulaire neutre agit.

## Avertissement

Masquer les noms ne rend pas un document anonyme. L'outil retire les
identifiants directs et laisse le contenu medical, qui peut suffire a retrouver
une personne par recoupement. Ce qu'il produit est pseudonymise ; juger si un
document peut sortir du cabinet reste la decision de l'utilisateur.

Aucun modele ne detecte 100 % des donnees personnelles. Cet outil assiste le
caviardage, il ne le garantit pas. Une relecture humaine reste necessaire des
lors que la confidentialite est critique.
