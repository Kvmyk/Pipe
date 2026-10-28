# install.ps1 - Instaluje skrot "pipe" w PowerShell
# Uruchom raz: .\install.ps1 (po polsku: $env:PIPE_LANG="pl"; .\install.ps1)
# Potem wystarczy wpisac: pipe

param(
    [string]$VpsHost = "",
    [string]$SshPort = "22",
    [string]$SshKey = ""
)

# Komunikaty po angielsku, po polsku z PIPE_LANG=pl (tylko ASCII: PowerShell 5.1 czyta pliki bez BOM jako ANSI)
function T([string]$Pl, [string]$En) { if ("$env:PIPE_LANG" -like "pl*") { $Pl } else { $En } }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CliPath = Join-Path $ScriptDir "clients\cli\cli.py"

# Zapytaj o adres serwera jesli nie podano. Pusty = bez serwera: `pipe web` zapyta, co chcesz robic,
# i postawi Pipe sam (lokalnie albo na serwerze).
if (-not $VpsHost) {
    $VpsHost = Read-Host (T "Podaj adres serwera (np. root@serwer.example.com) albo Enter, zeby skonfigurowac w aplikacji" "Server address (e.g. root@server.example.com), or Enter to set it up in the app")
}

# Zapytaj o port SSH (czesc dostawcow serwerow nie uzywa portu 22)
if ($VpsHost -and $SshPort -eq "22") {
    $inputPort = Read-Host (T "Podaj port SSH (znajdziesz w panelu dostawcy serwera) [domyslnie: 22]" "SSH port (see your server provider's panel) [default: 22]")
    if ($inputPort) { $SshPort = $inputPort }
}

# Sprawdz czy Python jest dostepny
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host (T "BLAD: Python nie jest zainstalowany lub nie jest w PATH." "ERROR: Python is not installed or not in PATH.") -ForegroundColor Red
    exit 1
}

# Zainstaluj zaleznosci
Write-Host (T "Instaluje zaleznosci CLI..." "Installing the CLI dependencies...") -ForegroundColor Cyan
$RequirementsPath = Join-Path $ScriptDir "clients\cli\requirements.txt"
python -m pip install -r $RequirementsPath --quiet

# Zbuduj argumenty dla funkcji 'pipe'
$PipeArgs = ""
if ($VpsHost) {
    $PipeArgs = "--host `"$VpsHost`""
}
if ($VpsHost -and $SshPort -ne "22") {
    $PipeArgs += " --ssh-port $SshPort"
}
if ($SshKey) {
    $PipeArgs += " --key `"$SshKey`""
}

# Przygotuj profil PowerShell
$ProfileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $ProfileDir)) {
    New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
}
if (-not (Test-Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force | Out-Null
}

$Marker = "# --- VPS Management Agent (Pipe) ---"
$MarkerEnd = "# --- end Pipe ---"
# Przedrostki lapia tez blok ze starsza nazwa komendy — ponowna instalacja
# zastepuje go, zamiast zostawiac dwie funkcje w profilu.
$MarkerPrefix = "^" + [regex]::Escape("# --- VPS Management Agent (Pipe")
$MarkerEndPrefix = "^" + [regex]::Escape("# --- end Pipe")

$FunctionBlock = @"

$Marker
function pipe {
    python "$CliPath" $PipeArgs `$args
}
$MarkerEnd
"@

# Sprawdz czy funkcja juz istnieje - jesli tak, zastap
$ProfileContent = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue

if ($ProfileContent -and ($ProfileContent -split "`r?`n" | Where-Object { $_ -match $MarkerPrefix })) {
    # Usun stary blok za pomoca linii po linii
    $lines = Get-Content $PROFILE
    $newLines = @()
    $skip = $false
    foreach ($line in $lines) {
        if ($line -match $MarkerPrefix) { $skip = $true }
        if (-not $skip) { $newLines += $line }
        if ($skip -and $line -match $MarkerEndPrefix) { $skip = $false }
    }
    Set-Content $PROFILE $newLines
    Add-Content $PROFILE $FunctionBlock
    Write-Host (T "Zaktualizowano istniejacy skrot 'pipe' w profilu." "Updated the existing 'pipe' shortcut in the profile.") -ForegroundColor Yellow
} else {
    Add-Content $PROFILE $FunctionBlock
    Write-Host (T "Dodano skrot 'pipe' do profilu PowerShell." "Added the 'pipe' shortcut to the PowerShell profile.") -ForegroundColor Green
}

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
if ($VpsHost) {
    Write-Host (T " Gotowe! Skrot 'pipe' bedzie laczyc sie z:" " Done! The 'pipe' shortcut will connect to:") -ForegroundColor Cyan
    Write-Host "   $VpsHost" -ForegroundColor White
} else {
    Write-Host (T " Gotowe! Wpisz 'pipe web' - strona zapyta, co chcesz robic," " Done! Type 'pipe web': the page asks what you want to do") -ForegroundColor Cyan
    Write-Host (T " i postawi Pipe na tym komputerze albo na Twoim serwerze." " and sets Pipe up on this computer or on your server.") -ForegroundColor Cyan
}
Write-Host ""
Write-Host (T " Aby zastosowac teraz (bez restartu terminala):" " To use it now (without restarting the terminal):") -ForegroundColor Cyan
Write-Host "   . `$PROFILE" -ForegroundColor Yellow
Write-Host (T " Potem wpisz:" " Then type:") -ForegroundColor Cyan
Write-Host "   pipe" -ForegroundColor Yellow
Write-Host "=================================================" -ForegroundColor Cyan
