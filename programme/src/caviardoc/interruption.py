"""L'arret d'un traitement a la demande de l'utilisateur.

Module a part, sans dependance : l'interface le charge avant que le moteur
soit installe, et la chaine de caviardage le leve sans connaitre l'interface.
"""

from __future__ import annotations


class Interrompu(Exception):
    """Le traitement a ete interrompu ; rien n'a ete ecrit pour ce document."""
