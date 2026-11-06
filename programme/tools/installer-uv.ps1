# Pose uv dans %USERPROFILE%\.local\bin, a une version epinglee et verifiee.
#
# On ne telecharge ni n'execute le script « install.ps1 » d'Astral : on prend
# l'archive de CETTE version, publiee sur GitHub, et on la refuse si son SHA-256
# n'est pas celui inscrit ci-dessous (publie par Astral dans le .sha256 de la
# release). Les memes valeurs vivent dans src/caviardoc/installateur.py ; un
# test verifie qu'elles sont identiques.
$UvVersion = "0.12.14"
$ErrorActionPreference = "Stop"

$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
switch ($arch) {
  "AMD64" {
    $triplet = "x86_64-pc-windows-msvc"
    $sha256 = "02cecb254f37f3396da625680796d3990164a590fb5611d46f719ad83e7b9a89"
  }
  "ARM64" {
    $triplet = "aarch64-pc-windows-msvc"
    $sha256 = "46b930bdbb338099c7d9da711b751240eb97876eda6a553a2c28323967232041"
  }
  default {
    Write-Host "uv n'est pas fourni pour cette machine ($arch)."
    Write-Host "Installez-le a la main : https://docs.astral.sh/uv/"
    exit 3
  }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$archive = "uv-$triplet.zip"
$url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/$archive"
$dossier = Join-Path ([System.IO.Path]::GetTempPath()) ("caviardoc-uv-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $dossier | Out-Null
try {
  $fichier = Join-Path $dossier $archive
  try {
    Invoke-WebRequest -Uri $url -OutFile $fichier -UseBasicParsing
  } catch {
    Write-Host "Le telechargement de uv a echoue : verifiez la connexion."
    exit 3
  }
  $vu = (Get-FileHash -Algorithm SHA256 -Path $fichier).Hash.ToLower()
  if ($vu -ne $sha256) {
    Write-Host "uv NON installe : l'empreinte SHA-256 du fichier telecharge ne"
    Write-Host "correspond pas a celle attendue (version $UvVersion). Fichier refuse."
    exit 6
  }
  Expand-Archive -Path $fichier -DestinationPath $dossier -Force
  $destination = Join-Path $env:USERPROFILE ".local\bin"
  New-Item -ItemType Directory -Force -Path $destination | Out-Null
  foreach ($nom in @("uv.exe", "uvx.exe")) {
    $source = Join-Path $dossier $nom
    if (Test-Path $source) { Copy-Item $source (Join-Path $destination $nom) -Force }
  }
  Write-Host "uv $UvVersion installe dans $destination."
} finally {
  Remove-Item -Recurse -Force $dossier -ErrorAction SilentlyContinue
}
