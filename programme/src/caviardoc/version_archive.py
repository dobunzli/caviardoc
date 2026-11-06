"""Version d'une copie telechargee en ZIP depuis GitHub.

`git archive` — dont se sert le bouton « Download ZIP » — remplace les deux
valeurs ci-dessous par le commit et sa date (attribut `export-subst` dans
`.gitattributes`). Dans une copie git, elles restent telles quelles : c'est git
qui sait alors de quelle version il s'agit. Voir `maj.commit_archive`.
"""

COMMIT = "$Format:%H$"
DATE = "$Format:%cs$"
