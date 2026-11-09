# Caviardoc

**Caviardez des documents PDF et Word, directement sur votre ordinateur.**

Caviardoc repère les données personnelles dans des documents en français
(noms, adresses, dates de naissance, numéros AVS, IBAN, téléphones, e-mails…) et
les masque, sans abîmer le reste du texte. Il a été pensé pour des documents
médicaux suisses romands.

- **100 % local** : vos documents ne quittent jamais votre machine.
- **Sans compte, sans abonnement**, et sans droits d'administrateur.
- **Simple** : on dépose un fichier dans une page web, on relit, on récupère le résultat.
- **Au choix, un fichier ou du texte brut** : le résultat peut être un
  document ou un simple texte, pratique quand l'IA que vous utilisez ne permet
  pas de joindre des fichiers.
- Fonctionne sur **macOS** et **Windows**.

> ⚠️ **Masquer les noms ne rend pas un document anonyme.** Caviardoc
> retire ce qui désigne directement une personne (noms, adresses, dates,
> numéros, logos). Il laisse le contenu médical, et c'est voulu. Or un
> diagnostic rare, une profession, un événement daté ou un lieu d'accident
> peuvent suffire à retrouver quelqu'un, surtout pour une IA qui cherche sur
> internet. Le résultat est **pseudonymisé** : relisez-le aussi pour ce qu'il
> raconte, pas seulement pour les noms oubliés. La relecture vous y aide en
> signalant une profession, un sport ou une activité, une nationalité ou une
> origine, un événement, un âge. Un diagnostic rare, lui, reste à votre
> jugement : aucune liste ne le repère de façon fiable. Et la détection
> elle-même n'est jamais parfaite.

---

## Installer

L'installation tient en trois étapes. Rien à installer avant, pas même Python.

### 1. Télécharger

Sur cette page GitHub, cliquez sur le bouton vert **Code**, puis
**Download ZIP**. Décompressez l'archive où vous voulez (Documents, Bureau…).

*(Si vous utilisez git : `git clone https://github.com/dobunzli/caviardoc.git`.)*

### 2. Lancer

Dans le dossier décompressé, vous voyez deux fichiers commençant par « ▶ ».
Double-cliquez sur celui de votre système :

| Système | Fichier à ouvrir |
|---|---|
| macOS | `▶ Ouvrir Caviardoc (Mac)` |
| Windows | `▶ Ouvrir Caviardoc (Windows)` |

**Au premier lancement, votre système peut se méfier** du fichier, parce qu'il
vient d'internet. C'est normal :

- **macOS** : si un message dit qu'Apple n'a pas pu vérifier le fichier,
  cliquez sur **OK**, ouvrez **Réglages Système → Confidentialité et
  sécurité**, descendez jusqu'à la mention du fichier et cliquez sur **Ouvrir
  quand même**. Sur macOS 14, un clic droit sur le fichier → **Ouvrir** →
  **Ouvrir** suffit. Si le double-clic ne fait rien du tout, ouvrez le Terminal
  dans le dossier et tapez `chmod +x *.command`.
- **Windows** : voir la section ci-dessous.

#### Sous Windows : avertissements à l'ouverture

Windows affiche plusieurs messages au premier lancement. Aucun n'est un
problème : le fichier n'est simplement pas signé par un certificat payant.

1. **« Fichier ouvert - Avertissement de sécurité : l'éditeur n'a pas pu être
   vérifié »** : cliquez sur **Exécuter**. Vous pouvez décocher « Toujours
   demander avant d'ouvrir ce fichier » pour ne plus le revoir.
2. **Fenêtre bleue « Windows a protégé votre ordinateur »** (SmartScreen) :
   cliquez sur **Informations complémentaires**, puis **Exécuter quand même**.
   Un antivirus peut aussi demander confirmation : autorisez le fichier.
3. **Une fenêtre noire s'ouvre** et, si Python est absent, demande
   « Installer uv puis Python ? [O/n] ». Tapez **O** puis **Entrée**. uv (outil
   libre d'Astral, dans `C:\Users\<vous>\.local\bin`) et Python (dans votre
   profil utilisateur) sont installés sans droits d'administrateur.
4. Une ligne jaune « `… is not on your PATH` » peut apparaître : **ignorez-la**,
   elle est sans conséquence.

**Gardez la fenêtre noire ouverte** tant que vous utilisez Caviardoc : la
fermer arrête l'application.

*Astuce pour éviter les messages 1 et 2 :* avant de décompresser, faites un
clic droit sur le fichier ZIP → **Propriétés**, cochez **Débloquer** en bas,
cliquez sur **OK**, puis décompressez.

### 3. Se laisser guider

Une page s'ouvre dans votre navigateur, sur l'onglet **Installation**. Elle
indique ce qui manque et l'installe pour vous, étape par étape. Chaque étape
annonce **ce qui sera téléchargé, sa taille et sa provenance**, et rien n'est
téléchargé sans votre accord.

Au total, comptez :

| | |
|---|---|
| Téléchargement et espace disque | plus d'un gigaoctet (modèle de détection, bibliothèques, reconnaissance de texte) ; la taille exacte de chaque étape est affichée avant de commencer |
| Mémoire | 8 Go de RAM minimum conseillés |
| Durée | quelques minutes, selon votre connexion |
| Connexion internet | nécessaire **uniquement** pour l'installation |

Quand tout est prêt, la page bascule d'elle-même sur l'onglet **Caviarder**.
Les fois suivantes, le même double-clic ouvre directement l'application.

**Si Python n'est pas installé** sur votre machine, le lanceur le détecte et
propose de l'installer pour vous (via [uv](https://github.com/astral-sh/uv)),
après avoir demandé votre accord. Il s'installe dans votre dossier personnel :
pas de mot de passe administrateur, et le Python du système n'est pas touché.

Chaque fichier téléchargé est vérifié par empreinte SHA-256 : un fichier altéré
est rejeté.

---

## Utiliser

1. **Déposez** un fichier — ou un dossier entier — dans l'onglet **Caviarder**.
   Les PDF et les .docx sont traités ; les autres fichiers sont comptés et
   signalés, jamais ignorés en silence.
2. **Relisez** : les données détectées sont surlignées. Un clic retire un masque ;
   sélectionner un texte et cliquer sur « Masquer la sélection » rattrape ce que
   l'outil a manqué. En violet, les passages **à juger** : une profession, un
   sport ou une activité, une nationalité ou une origine, un événement, un âge.
   Ils ne nomment personne mais **peuvent aider à reconnaître la personne**. Ils
   restent lisibles tant que vous ne cliquez pas dessus ; un clic les masque
   (dans le texte, « cycliste professionnel » devient `[PROFESSION_1]`).
   Sans relecture, ils ne sont ni cherchés ni signalés.
3. **Caviardez**, puis téléchargez le résultat (ou tout le dossier en .zip, avec
   son arborescence).

Chaque document produit est relu automatiquement pour vérifier qu'aucune donnée
masquée n'a survécu. Un document **non vérifié** est signalé comme tel.

### Ce que vous obtenez

- **PDF** : le résultat est une image de chaque page, caviardée au pixel. Il ne
  contient aucun texte caché sous les rectangles noirs.
- **.docx** : les données sont remplacées dans le fichier lui-même, par des
  pseudonymes (`PATIENT_1`, `MEDECIN_1_RHUMATOLOGUE`, `PERSONNE_1`…). Le
  document reste modifiable. Les métadonnées qui peuvent nommer le patient
  (auteur, aperçu, propriétés, historique de révisions, EXIF des images…) sont
  nettoyées.
- **Texte seul** : pratique pour une IA qui ne permet pas de pièces jointes, avec des pseudonymes cohérents d'un document à l'autre. Le
  bouton **Copier le texte** le met directement dans le presse-papiers.

> **Attention au cumul.** Une personne garde le même pseudonyme dans tous les
> documents d'une même demande. C'est ce qui permet à une IA de suivre le
> patient d'un document à l'autre ; c'est aussi ce qui lui permet
> d'additionner ce que chacun dit de lui. Deux documents anodins séparément
> peuvent, ensemble, le rendre reconnaissable. Pour des documents qui n'ont
> pas à être rapprochés, faites une **nouvelle demande** : les pseudonymes
> repartent de zéro.

Dans le texte et le .docx, une personne masquée garde son rôle quand le
document le dit : `PATIENT_1`, `MEDECIN_2_CARDIOLOGUE`,
`SOIGNANT_1_PHYSIOTHERAPEUTE`. Une IA comprend ainsi qui a examiné qui, sans
lire leurs noms. Un proche reste `PERSONNE_1`.

### Réglages principaux

| Réglage | Choix |
|---|---|
| Niveau | **toutes les personnes** (par défaut : patient, proches et soignants, mais aussi logos, photos, signatures, tampons et blocs d'adresse) ou **le patient seulement** (soignants, établissements, logos et en-têtes restent lisibles ; le nom du patient y est masqué) |
| Dates | masquées (par défaut) ; **en jours** : chaque date devient son écart avec la plus ancienne du document (J0, J+4, J+371), ce qui garde les intervalles sans donner de date à chercher, la date de naissance restant masquée ; conservées sauf la date de naissance ; ou toutes conservées. En chiffres (12.03.2025) comme en lettres (12 mars 2025) |

> Avant d'envoyer un document à une IA en ligne, gardez le niveau **toutes les
> personnes** : le nom d'un cabinet et sa localité suffisent souvent à
> ré-identifier un patient. Ce niveau est nécessaire, mais il ne suffit pas. Si
> le patient reste reconnaissable par son histoire, la transmission reste
> soumise au secret professionnel (art. 321 CP). Dans le doute, retirez ou
> généralisez les détails rares (« cycliste professionnel » → « patient
> sportif »), ou demandez l'accord du patient.

---

## En ligne de commande (facultatif)

L'interface web suffit pour tout. Pour automatiser, une fois l'installation faite :

```bash
caviardoc caviarder -o ~/anonymises ~/Documents/*.pdf ~/Documents/*.docx
```

Toutes les commandes et options sont décrites dans la
[documentation technique](programme/docs/TECHNIQUE.md#commandes).

---

## Mettre à jour

Onglet **Installation** de la page : l'outil vérifie s'il existe une nouvelle
version et l'applique sur demande, que vous l'ayez téléchargé en ZIP ou avec
git. La page redémarre d'elle-même, met à jour les bibliothèques, puis
remesure la détection sur cet ordinateur : laissez-la ouverte quelques
minutes. En ligne de commande : `caviardoc maj`.

> Caviardoc s'appelait auparavant **Doc Anonymizer**. Une copie installée sous
> l'ancien nom se met à jour de la même façon. Elle réinstalle une fois ses
> bibliothèques ; le modèle de détection et vos corrections sont repris.

## Désinstaller

Onglet **Installation**, section « Désinstallation ». Elle montre ce qui sera
supprimé (chemin et taille), puis demande confirmation. Tout ce que
l'installation a téléchargé est retiré. Le dossier que vous avez
décompressé n'est pas supprimé : effacez-le vous-même.

## Un problème ?

| Symptôme | À essayer |
|---|---|
| macOS refuse d'ouvrir le fichier | Réglages Système → Confidentialité et sécurité → **Ouvrir quand même** (macOS 14 : clic droit → Ouvrir) |
| Le double-clic ne fait rien (macOS) | `chmod +x *.command` dans le Terminal, depuis le dossier |
| « Python introuvable » | Relancez le fichier et acceptez l'installation proposée ; sinon installez Python 3.11 ou plus récent depuis [python.org](https://www.python.org/downloads/) en cochant « Add Python to PATH » |
| Un téléchargement échoue | Vérifiez votre connexion, puis relancez le lanceur : la page Installation indique ce qui manque encore |
| Savoir ce qui manque | Onglet **Installation**, ou `caviardoc doctor` |
| Vérifier que tout fonctionne | `caviardoc selftest` |

Pour tout autre souci, ouvrez une
[issue](https://github.com/dobunzli/caviardoc/issues) **sans y joindre de
document réel ni de donnée personnelle**.

---

## Pour aller plus loin

- [Documentation technique](programme/docs/TECHNIQUE.md) : architecture, choix du
  modèle, détail des chaînes PDF et .docx, apprentissage des corrections,
  qualité mesurée.

## Licence

© 2026 D. Bünzli — [AGPL-3.0-or-later](LICENSE), sans aucune garantie.
