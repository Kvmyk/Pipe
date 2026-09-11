# install.ps1 - Instaluje skrot "pipe" w PowerShell
# Uruchom raz: .\install.ps1
# Potem wystarczy wpisac: pipe

param(
    [string]$VpsHost = "",
    [string]$SshPort = "22",
    [string]$SshKey = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CliPath = Join-Path $ScriptDir "clients\cli\cli.py"

# Zapytaj o adres serwera jesli nie podano
if (-not $VpsHost) {
    $VpsHost = Read-Host "Podaj adres serwera (np. root@serwer.example.com)"
}

if (-not $VpsHost) {
    Write-Host "BLAD: adres serwera jest wymagany." -ForegroundColor Red
    exit 1
}

# Zapytaj o port SSH (czesc dostawcow serwerow nie uzywa portu 22)
if ($SshPort -eq "22") {
    $inputPort = Read-Host "Podaj port SSH (znajdziesz w panelu dostawcy serwera) [domyslnie: 22]"
    if ($inputPort) { $SshPort = $inputPort }
}

# Sprawdz czy Python jest dostepny
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "BLAD: Python nie jest zainstalowany lub nie jest w PATH." -ForegroundColor Red
    exit 1
}

# Zainstaluj zaleznosci
Write-Host "Instaluje zaleznosci CLI..." -ForegroundColor Cyan
$RequirementsPath = Join-Path $ScriptDir "clients\cli\requirements.txt"
python -m pip install -r $RequirementsPath --quiet

# Zbuduj argumenty dla funkcji 'pipe'
$PipeArgs = "--host `"$VpsHost`""
if ($SshPort -ne "22") {
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
    Write-Host "Zaktualizowano istniejacy skrot 'pipe' w profilu." -ForegroundColor Yellow
} else {
    Add-Content $PROFILE $FunctionBlock
    Write-Host "Dodano skrot 'pipe' do profilu PowerShell." -ForegroundColor Green
}

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host " Gotowe! Skrot 'pipe' bedzie laczyc sie z:      " -ForegroundColor Cyan
Write-Host "   $VpsHost" -ForegroundColor White
Write-Host ""
Write-Host " Aby zastosowac teraz (bez restartu terminala): " -ForegroundColor Cyan
Write-Host "   . `$PROFILE" -ForegroundColor Yellow
Write-Host " Potem wpisz:                                   " -ForegroundColor Cyan
Write-Host "   pipe" -ForegroundColor Yellow
Write-Host "=================================================" -ForegroundColor Cyan
