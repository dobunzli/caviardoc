# Integration continue

Deux workflows, pour deux promesses distinctes du projet.

## Quand ils tournent

**Aucun de ces workflows ne se declenche sur un `push`.** Le depot sert d'abord
a travailler sur deux machines : un commit y est un transport, pas une
proposition de modification. Lancer quatorze jobs a chaque aller-retour — dont
quatre sur macOS, factures dix fois le tarif Linux — epuisait le quota sans
rien apprendre.

La boucle de retour rapide est locale : `pytest` rend son verdict en deux
minutes, et `python tools/mesurer.py` en une minute. L'integration continue
sert a autre chose : verifier Windows et Linux, que le poste de developpement
n'a pas.

A la demande, donc, et aux moments ou la question se pose :

```bash
gh workflow run tests     # avant une publication, apres un changement de dependance
gh workflow run mesure    # changement de modele, de seuil, d'onnxruntime
```

`tests.yml` se declenche aussi sur une proposition de modification, s'il y en a
un jour.

## Ce que chacun couvre

**`tests.yml` — matrice.** macOS, Windows et Linux, Python 3.11 et 3.14. La
matrice n'est pas decorative : macOS et Windows sont les plateformes
distribuees, et un installateur Windows jamais teste est ce qui condamne ce
genre d'outil. Un bug de separateur de chemin ou de fin de ligne ne se voit que
la. Linux y figure comme temoin bon marche — il coute dix fois moins qu'un
runner macOS et attrape les memes fautes de portabilite.

**`python-nu`.** Le diagnostic (`doctor`, `info`) doit fonctionner sur
un Python sans aucune dependance — c'est toute sa raison d'etre sur une machine
neuve. La promesse est verifiee plutot que supposee.

**`mesure.yml`.** Rappel et preservation sur le corpus de regression
(`tests/corpus/`) pour le detecteur seul, puis pour la chaine complete — PDF
rasterise, OCR, rectangles noirs —, celle-ci avec des seuils de
non-regression. Il a besoin du modele : il ne tourne donc que sur demande, et
le modele est mis en cache. Sans cette mesure, une degradation du rappel se
decouvre des sessions plus tard — c'est deja arrive.

Aucun des deux premiers ne telecharge le modele de detection : les tests passent `None`
comme modele et exercent les regles deterministes, la mecanique .docx et la
logique de spans. La CI tourne en quelques secondes.
