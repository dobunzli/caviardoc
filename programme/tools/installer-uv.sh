#!/bin/sh
# Pose uv dans ~/.local/bin, a une version epinglee et verifiee.
#
# On ne telecharge ni n'execute le script « install.sh » d'Astral, qui change
# sans prevenir : on prend l'archive de CETTE version, publiee sur GitHub, et on
# refuse d'y toucher si son SHA-256 n'est pas celui inscrit ci-dessous (publie
# par Astral dans le fichier .sha256 de la release). Aucune signature : le
# controle porte sur l'empreinte, qui est dans le depot.
#
# Les memes valeurs vivent dans src/caviardoc/installateur.py ; un test verifie
# qu'elles sont identiques.
UV_VERSION="0.12.14"

set -eu

cible="$(uname -s)-$(uname -m)"
case "$cible" in
  Darwin-arm64|Darwin-aarch64)
    triplet="aarch64-apple-darwin"
    sha256="dfed5683c5873d65eff8476eb93526059b81e5646a68305b5c94a26b29990894" ;;
  Darwin-x86_64)
    triplet="x86_64-apple-darwin"
    sha256="e11a70264ceca38e929ecaeed953c323e517af7b960ebf9722d5918e8c7f0375" ;;
  Linux-x86_64)
    triplet="x86_64-unknown-linux-gnu"
    sha256="18ef5c3888ae59828cb13f38d57e9389b8173ecc719eff163bfafc74b38f5936" ;;
  Linux-aarch64|Linux-arm64)
    triplet="aarch64-unknown-linux-gnu"
    sha256="7fb91bd5d10529c60723eaec3caf44726f89280e5aeed78af8fc63fcad004c9b" ;;
  *)
    echo "uv n'est pas fourni pour cette machine ($cible)." >&2
    echo "Installez-le a la main : https://docs.astral.sh/uv/" >&2
    exit 3 ;;
esac

archive="uv-$triplet.tar.gz"
url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$archive"
dossier="$(mktemp -d "${TMPDIR:-/tmp}/caviardoc-uv.XXXXXX")"
trap 'rm -rf "$dossier"' EXIT

curl -fsSL --proto '=https' --tlsv1.2 -o "$dossier/$archive" "$url" || {
  echo "Le telechargement de uv a echoue : verifiez la connexion." >&2
  exit 3
}

if command -v sha256sum >/dev/null 2>&1; then
  vu="$(sha256sum "$dossier/$archive" | cut -d' ' -f1)"
else
  vu="$(shasum -a 256 "$dossier/$archive" | cut -d' ' -f1)"
fi
if [ "$vu" != "$sha256" ]; then
  echo "uv NON installe : l'empreinte SHA-256 du fichier telecharge ne correspond" >&2
  echo "pas a celle attendue (version $UV_VERSION). Le fichier est refuse." >&2
  exit 6
fi

tar -xzf "$dossier/$archive" -C "$dossier"
destination="$HOME/.local/bin"
mkdir -p "$destination"
cp "$dossier/uv-$triplet/uv" "$destination/uv"
[ ! -f "$dossier/uv-$triplet/uvx" ] || cp "$dossier/uv-$triplet/uvx" "$destination/uvx"
chmod +x "$destination/uv"
echo "uv $UV_VERSION installe dans $destination."
