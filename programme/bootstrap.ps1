# Premiere installation sur une machine Windows, depuis un terminal.
# N'exige rien : si Python manque, il est propose a l'installation.
#
# Le pendant de bootstrap.sh. Le lanceur a double-clic
# (« Installer sur Windows.bat ») fait la meme recherche en cmd, faute de
# pouvoir partager du code avec PowerShell ; un test verrouille leur accord.
$ErrorActionPreference = "Stop"
# Un interpreteur qui rend un code non nul est un resultat, pas une exception.
$PSNativeCommandUseErrorActionPreference = $false

# Sans ce piege, une erreur terminale affiche une trace .NET puis rend 0 :
# l'appelant croit a un succes alors que rien n'a ete fait. Verifie avec
# PowerShell 7.6 en supprimant LOCALAPPDATA, qui faisait exactement cela.
trap {
  Write-Host "`nL'installation s'est arretee : $_"
  exit 1
}

Set-Location -Path $PSScriptRoot

# Le plancher qu'on accepte, et la version qu'on installe quand il n'y a rien.
$VersionMinimale = "3.11"
$VersionInstallee = "3.14"

Write-Host "Caviardoc - installation`n"

# Les .pyc ne doivent pas atterrir dans le dossier synchronise.
if (-not $env:PYTHONPYCACHEPREFIX) {
  # LOCALAPPDATA est toujours defini sous Windows, mais Join-Path leve sur un
  # argument nul : un repli vaut mieux qu'un arret sur le cache de bytecode.
  $local = $env:LOCALAPPDATA
  if (-not $local) { $local = Join-Path $HOME "AppData\Local" }
  $env:PYTHONPYCACHEPREFIX = Join-Path $local "caviardoc\pycache"
}

# On verifie la version, et non la seule presence. Cela ecarte aussi le faux
# python.exe du Microsoft Store, present dans le PATH mais qui ouvre le Store
# au lieu de s'executer.
function Test-Python($chemin) {
  if (-not $chemin) { return $false }
  & $chemin -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Find-Python {
  foreach ($nom in @("python", "python3", "py")) {
    $cmd = Get-Command $nom -ErrorAction SilentlyContinue
    if ($cmd -and (Test-Python $cmd.Source)) { return $cmd.Source }
  }
  return $null
}

function Find-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
  if (Test-Path $local) { return $local }
  return $null
}

$py = Find-Python

if (-not $py) {
  Write-Host "Aucun Python $VersionMinimale ou superieur n'a ete trouve.`n"
  $uv = Find-Uv
  $uvPoseParNous = (-not $uv)

  if (-not $uv) {
    # Telecharger et executer un script distant ne se fait pas en silence dans
    # un outil de confidentialite : on dit quoi, d'ou, et on demande.
    Write-Host "Caviardoc peut installer Python pour vous, avec uv."
    Write-Host "  uv est un outil libre d'Astral. Il sera telecharge depuis :"
    Write-Host "      https://github.com/astral-sh/uv/releases (version 0.12.14, empreinte SHA-256 verifiee)"
    Write-Host "  Il s'installe dans votre dossier personnel, sans droits"
    Write-Host "  administrateur et sans modifier le systeme.`n"
    $reponse = Read-Host "Installer uv puis Python ? [O/n]"
    if ($reponse -match '^\s*n') {
      Write-Host "`nInstallez alors Python $VersionMinimale ou superieur depuis"
      Write-Host "https://www.python.org/downloads/windows/ puis relancez ce script."
      exit 1
    }
    Write-Host "`nTelechargement de uv..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "tools\installer-uv.ps1")
    if ($LASTEXITCODE -ne 0) {
      Write-Host "`nuv n'a pas ete installe (telechargement ou verification echoues)."
      Write-Host "Installez Python depuis https://www.python.org/downloads/windows/"
      exit 1
    }
    $uv = Find-Uv
    if (-not $uv) {
      Write-Host "`nuv a ete installe, mais reste introuvable. Fermez cette"
      Write-Host "fenetre, rouvrez-la, et relancez ce script."
      exit 1
    }
  }

  Write-Host "`nInstallation de Python $VersionInstallee..."
  # Un Python deja connu de uv n'est pas a nous : on ne le notera pas.
  # --system : sans lui, uv rend un .venv (dossier courant, VIRTUAL_ENV).
  $pythonPoseParNous = -not (& $uv python find --system $VersionInstallee 2>$null)
  & $uv python install $VersionInstallee
  if ($LASTEXITCODE -ne 0) {
    Write-Host "`nL'installation de Python a echoue."
    exit 1
  }

  $py = (& $uv python find --system $VersionInstallee 2>$null | Select-Object -First 1)
  if (-not (Test-Python $py)) {
    Write-Host "`nPython a ete installe, mais reste introuvable. Fermez cette"
    Write-Host "fenetre, rouvrez-la, et relancez ce script."
    exit 1
  }
  Write-Host "`nPython installe.`n"

  # Note ce que nous avons pose hors du dossier de l'application, pour que la
  # desinstallation sache le distinguer de ce qui etait deja la.
  # Chacun separement : un uv deja la n'empeche pas que Python soit de nous.
  $env:PYTHONPATH = "src"
  if ($uvPoseParNous) {
    & $py -c "from caviardoc import desinstallation as d; d.noter_installe('uv')" 2>$null
  }
  if ($pythonPoseParNous) {
    & $py -c "from caviardoc import desinstallation as d; d.noter_installe('python', '$VersionInstallee')" 2>$null
  }
}

$env:PYTHONPATH = "src"
& $py -m caviardoc doctor
Write-Host ""
& $py -m caviardoc setup @args
