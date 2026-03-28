# install.ps1 — Instaluje skrót "pipe" w PowerShell
# Uruchom raz: .\install.ps1
# Potem wystarczy wpisać: pipe

param(
    [string]$VpsHost = "",
    [string]$SshPort = "22",
    [string]$SshKey = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$CliPath = Join-Path $ScriptDir "clients\cli\cli.py"

# ─── Zapytaj o adres serwera jeśli nie podano ────────────────────────────────
if (-not $VpsHost) {
    $VpsHost = Read-Host "Podaj adres serwera (np. root@mikrus.example.com)"
}

if (-not $VpsHost) {
    Write-Host "❌ Błąd: adres serwera jest wymagany." -ForegroundColor Red
    exit 1
}

# ─── Sprawdź czy Python jest dostępny ────────────────────────────────────────
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "❌ Błąd: Python nie jest zainstalowany lub nie jest w PATH." -ForegroundColor Red
    exit 1
}

# ─── Zainstaluj zależności ───────────────────────────────────────────────────
Write-Host "📦 Instaluję zależności CLI..." -ForegroundColor Cyan
$RequirementsPath = Join-Path $ScriptDir "clients\cli\requirements.txt"
python -m pip install -r $RequirementsPath --quiet

# ─── Buduj argumenty dla funkcji 'pipe' ──────────────────────────────────────
$PipeArgs = "--host `"$VpsHost`""
if ($SshPort -ne "22") {
    $PipeArgs += " --ssh-port $SshPort"
}
if ($SshKey) {
    $PipeArgs += " --key `"$SshKey`""
}

# ─── Dodaj funkcję do profilu PowerShell ─────────────────────────────────────
$ProfileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $ProfileDir)) {
    New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
}
if (-not (Test-Path $PROFILE)) {
    New-Item -ItemType File -Path $PROFILE -Force | Out-Null
}

$FunctionContent = @"

# ─── VPS Management Agent (Pipe) ──────────────────────────────────────────────
function pipe {
    python "$CliPath" $PipeArgs @args
}
"@

# Sprawdź czy funkcja już istnieje w profilu
$ProfileContent = Get-Content $PROFILE -Raw -ErrorAction SilentlyContinue
if ($ProfileContent -and $ProfileContent.Contains("VPS Management Agent (Pipe)")) {
    # Zastąp istniejący blok
    $ProfileContent = $ProfileContent -replace "(?s)# ─── VPS Management Agent \(Pipe\).*?^}", ""
    $ProfileContent = $ProfileContent.TrimEnd()
    Set-Content $PROFILE ($ProfileContent + $FunctionContent)
    Write-Host "🔄 Zaktualizowano istniejący skrót 'pipe' w profilu." -ForegroundColor Yellow
} else {
    Add-Content $PROFILE $FunctionContent
    Write-Host "✅ Dodano skrót 'pipe' do profilu PowerShell." -ForegroundColor Green
}

Write-Host ""
Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host " Gotowe! Załaduj profil i wpisz 'pipe' aby połączyć się z:" -ForegroundColor Cyan
Write-Host "   $VpsHost" -ForegroundColor White
Write-Host ""
Write-Host " Aby zastosować teraz (bez restartu terminala):" -ForegroundColor Cyan
Write-Host "   . `$PROFILE" -ForegroundColor Yellow
Write-Host " Lub po prostu:" -ForegroundColor Cyan
Write-Host "   pipe" -ForegroundColor Yellow
Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
