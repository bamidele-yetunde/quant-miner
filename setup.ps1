# One-time setup: connects this miner to a BRAIN account and starts it on GitHub Actions.
# Run from this folder in PowerShell:   powershell -ExecutionPolicy Bypass -File .\setup.ps1
# Your password is typed here, sent only to BRAIN (login test) and to GitHub Secrets. Never to anyone else.

# "Continue": git/gh write progress to stderr, which PowerShell 5.1 would treat as fatal. Exit codes are checked instead.
$ErrorActionPreference = "Continue"
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
Set-Location $PSScriptRoot

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Plain($secure) {
    [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
function SetSecret($name, $value) {
    $value | gh secret set $name --repo $script:Repo
    if ($LASTEXITCODE -ne 0) { throw "could not set secret $name" }
    Write-Host "  secret $name set"
}

Step "Checking tools"
python --version
gh auth status
if ($LASTEXITCODE -ne 0) { throw "Log in to GitHub first:  gh auth login" }
python -m pip install -q -r requirements.txt

Step "BRAIN account"
$email = Read-Host "BRAIN login email"
$password = Plain (Read-Host "BRAIN password" -AsSecureString)
$env:BRAIN_EMAIL = $email
$env:BRAIN_PASSWORD = $password
python brain.py whoami
if ($LASTEXITCODE -ne 0) { throw "BRAIN login failed - check the email and password" }

Step "Encryption key (MINER_STATE_KEY)"
if (Test-Path .miner_key) {
    $key = (Get-Content .miner_key -Raw).Trim()
    Write-Host "  using the existing key in .miner_key"
} else {
    $key = (python vault.py newkey --save | Select-Object -Last 1).Trim()
}
Write-Host "  KEEP A COPY OF THIS KEY (password manager / notes). Without it the saved progress is lost:" -ForegroundColor Yellow
Write-Host "  $key" -ForegroundColor Yellow

Step "Telegram (optional, recommended)"
Write-Host "  Create a bot with @BotFather (/newbot), then send your bot any message."
$tgToken = Read-Host "Bot token (Enter to skip)"
$tgChat = ""
if ($tgToken) {
    $env:TELEGRAM_BOT_TOKEN = $tgToken
    python notify.py chats
    $tgChat = Read-Host "TELEGRAM_CHAT_ID from the line above"
}
$label = Read-Host "Short label for this account's messages, e.g. ACC2 (Enter to skip)"

Step "GitHub repository"
$me = (gh api user --jq .login).Trim()
$name = Read-Host "New repository name [quant-miner]"
if (-not $name) { $name = "quant-miner" }
$script:Repo = "$me/$name"
if (-not (Test-Path .git)) { git init -b main | Out-Null }
if (-not (git config user.name)) { git config user.name $me }
if (-not (git config user.email)) { git config user.email "$me@users.noreply.github.com" }
git add -A
git commit -q -m "Alpha miner for WorldQuant BRAIN"
gh repo view $script:Repo 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  $($script:Repo) already exists - pushing to it"
    if (-not (git remote)) { git remote add origin "https://github.com/$($script:Repo).git" }
    git push -u origin main
} else {
    # public = unlimited free Actions minutes; results stay encrypted (see README, Privacy model)
    gh repo create $script:Repo --public --source . --push
}
if ($LASTEXITCODE -ne 0) { throw "could not create/push the repository" }

Step "Secrets and variables"
SetSecret "BRAIN_EMAIL" $email
SetSecret "BRAIN_PASSWORD" $password
SetSecret "MINER_STATE_KEY" $key
if ($tgToken -and $tgChat) {
    SetSecret "TELEGRAM_BOT_TOKEN" $tgToken
    SetSecret "TELEGRAM_CHAT_ID" $tgChat
    $env:TELEGRAM_CHAT_ID = $tgChat
    $env:MINER_LABEL = $label
    python notify.py test
}
if ($label) { gh variable set MINER_LABEL --body $label --repo $script:Repo }

Step "Starting the miner"
gh workflow run alpha-miner.yml --repo $script:Repo
Write-Host "`nDone. Watch it at https://github.com/$($script:Repo)/actions" -ForegroundColor Green
Write-Host "If BRAIN asks for an identity check, the link arrives on Telegram (or run: python brain.py whoami)."
