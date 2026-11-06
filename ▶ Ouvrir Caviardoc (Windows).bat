@echo off
rem Double-cliquez ce fichier. C'est le seul dont vous ayez besoin.
rem
rem Il installe ce qui manque, puis ouvre Caviardoc dans votre
rem navigateur. La page a deux onglets — l'application et son entretien — et
rem s'ouvre sur celui qui sert.
rem
rem Windows peut afficher un avertissement SmartScreen au premier lancement :
rem « Informations complementaires » puis « Executer quand meme ».
setlocal
rem Tout le reste vit dans programme\, pour que ce dossier-ci ne montre
rem que les deux lanceurs.
cd /d "%~dp0programme"

echo Caviardoc
echo.

rem Les .pyc n'ont rien a faire dans le depot.
if "%PYTHONPYCACHEPREFIX%"=="" set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\caviardoc\pycache"

rem --- 1. Un Python convenable est-il deja la ? -------------------------------
rem On verifie la version, pas la seule presence. Cela ecarte aussi le faux
rem python.exe du Microsoft Store, qui existe dans le PATH mais ouvre le Store
rem au lieu de s'executer.
set "PY="
rem Celui de l'application d'abord, une fois installee : c'est lui que la page
rem utilise de toute facon. Pose par uv, Python n'est pas dans le PATH, et sans
rem ce raccourci chaque lancement annoncait « aucun Python » puis le reinstallait.
set "PY_APP=%LOCALAPPDATA%\caviardoc\venv\Scripts\python.exe"
if defined CAVIARDOC_DATA_DIR set "PY_APP=%CAVIARDOC_DATA_DIR%\venv\Scripts\python.exe"
if not exist "%PY_APP%" goto :chercher
"%PY_APP%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%PY_APP%"
:chercher
call :essayer python
call :essayer py
if not "%PY%"=="" goto :lancer

set "UV="
where uv >nul 2>&1 && set "UV=uv"
if "%UV%"=="" if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
rem Un Python deja pose par uv, a un lancement precedent : rien a installer.
if not "%UV%"=="" call :python_de_uv
if not "%PY%"=="" goto :lancer

rem --- 2. Sinon, uv sait en installer un -------------------------------------
rem uv telecharge un Python autonome, sans droits d'administrateur et sans
rem toucher au systeme. C'est ce qui evite d'envoyer l'utilisateur telecharger
rem un installateur a la main.
echo Aucun Python 3.11 ou superieur n'a ete trouve.
echo.

if not "%UV%"=="" goto :installer_python

rem Telecharger et executer un script distant ne se fait pas en silence dans un
rem outil de confidentialite : on dit quoi, d'ou, et on demande.
echo Caviardoc peut installer Python pour vous, avec uv.
echo   uv est un outil libre d'Astral. Il sera telecharge depuis :
echo       https://github.com/astral-sh/uv/releases (version 0.12.14, SHA-256 verifie)
echo   Il s'installe dans votre dossier personnel, sans droits
echo   administrateur et sans modifier le systeme.
echo.
set "reponse="
set /p "reponse=Installer uv puis Python ? [O/n] "
if /i "%reponse%"=="n" goto :manuel
if /i "%reponse%"=="non" goto :manuel

echo.
echo Telechargement de uv...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0programme\tools\installer-uv.ps1"
if errorlevel 6 goto :echec_empreinte
if errorlevel 1 goto :echec_uv
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" goto :echec_uv
rem uv est a nous : la desinstallation pourra le retirer.
set "UV_POSE=1"

:installer_python
echo.
echo Installation de Python...
rem Un Python 3.14 deja connu de uv n'est pas a nous : on ne le notera pas.
set "PY_AVANT="
"%UV%" python find 3.14 >nul 2>&1 && set "PY_AVANT=1"
rem --no-bin : ne pas poser python.exe dans .local\bin, ce qui evite l'avertissement
rem "not on your PATH" de uv. Un uv ancien ne le connait pas : on retente sans.
"%UV%" python install --no-bin 3.14
if errorlevel 1 "%UV%" python install 3.14
if errorlevel 1 goto :echec_python

call :python_de_uv
if "%PY%"=="" goto :echec_chemin
echo.
echo Python installe.
echo.

rem Retient ce que nous avons pose hors du dossier de l'application, pour que la
rem desinstallation sache le distinguer de ce qui etait deja la. Sans bruit : une
rem trace manquante fait proposer moins, jamais supprimer plus.
set "PYTHONPATH=src"
if defined UV_POSE "%PY%" -c "from caviardoc import desinstallation as d; d.noter_installe('uv')" >nul 2>&1
if not defined PY_AVANT "%PY%" -c "from caviardoc import desinstallation as d; d.noter_installe('python', '3.14')" >nul 2>&1

rem --- 3. La page d'installation prend le relais ------------------------------
rem Elle n'a besoin que de la bibliotheque standard : elle s'ouvre meme quand il
rem manque tout le reste, ce qui est son objet.
:lancer
set "PYTHONPATH=src"
rem Entre parentheses : cmd lit tout le bloc avant de l'executer. Une mise a
rem jour remplace ce fichier pendant que la page est ouverte, et cmd reprendrait
rem sinon sa lecture, a la fermeture, au milieu d'un fichier qui a change.
rem `call echo` et les %% relisent le code de sortie au moment voulu, et non a
rem la lecture du bloc.
(
  "%PY%" -m caviardoc console
  if errorlevel 1 (
    echo.
    call echo Caviardoc s'est arrete ^(code %%errorlevel%%^).
    pause
  )
  exit /b
)

rem --- Sorties d'echec -------------------------------------------------------
:manuel
echo.
echo Installez alors Python 3.11 ou superieur depuis
echo https://www.python.org/downloads/windows/ en cochant
echo "Add Python to PATH", puis double-cliquez a nouveau ce fichier.
echo.
pause
exit /b 1

:echec_uv
echo.
echo Le telechargement de uv a echoue. Verifiez
echo votre connexion, ou installez
echo Python depuis https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:echec_empreinte
echo.
echo uv n'a PAS ete installe : le fichier telecharge ne correspond pas a
echo l'empreinte SHA-256 attendue, il a donc ete refuse. Rien n'a ete installe.
echo Reessayez dans un moment ; si cela persiste, signalez-le.
echo.
pause
exit /b 1

:echec_python
echo.
echo L'installation de Python a echoue.
echo.
pause
exit /b 1

:echec_chemin
echo.
echo Python a ete installe, mais reste introuvable. Fermez cette fenetre,
echo rouvrez-la, et double-cliquez a nouveau ce fichier.
echo.
pause
exit /b 1

rem --- Le Python 3.14 que uv connait, dans PY ; vide sinon ------------------
rem uv ecrit le chemin en UTF-8, alors que `set /p` lit dans la page de code de
rem la console (850). Un dossier personnel accentue — C:\Users\Helene avec son
rem accent — en sortait deforme, donc introuvable : on passe en UTF-8 le temps
rem de le lire, puis on remet la page d'origine.
:python_de_uv
set "PY="
set "trouve=%TEMP%\caviardoc-python.txt"
"%UV%" python find 3.14 > "%trouve%" 2>nul
set "PAGE="
for /f "tokens=2 delims=:." %%c in ('chcp') do set "PAGE=%%c"
chcp 65001 >nul
if exist "%trouve%" set /p "PY="<"%trouve%"
if defined PAGE chcp %PAGE% >nul
del "%trouve%" >nul 2>&1
if not "%PY%"=="" if not exist "%PY%" set "PY="
exit /b 0

rem --- Retient le premier interpreteur dont la version convient --------------
:essayer
if not "%PY%"=="" exit /b 0
where %1 >nul 2>&1 || exit /b 0
%1 -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1 || exit /b 0
set "PY=%~1"
exit /b 0
