# Signs the bot in to a YouTube channel and saves the 3 keys as GitHub secrets of this repo.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File D:\Automation\JASOOSVIKRAM\tools\switch-youtube-channel.ps1
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$client = Join-Path $PSScriptRoot "client_secret.json"
$tmp = Join-Path $env:TEMP ("yt-login-" + [guid]::NewGuid() + ".json")

Write-Host "A browser will open. Sign in with the Google account that owns the NEW channel,"
Write-Host "then pick the channel 'Vikram Jasoos' and allow access."
Write-Host "If Google says 'hasn't verified this app': click Advanced -> Go to the app.`n"

$py = @"
import json, sys
from autoyt import youtube
cid, secret, refresh = youtube.login(sys.argv[1])
json.dump({'YT_CLIENT_ID': cid, 'YT_CLIENT_SECRET': secret, 'YT_REFRESH_TOKEN': refresh}, open(sys.argv[2], 'w'))
"@
python -c $py $client $tmp
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $tmp)) { throw "Sign-in did not finish." }

try {
    $keys = Get-Content $tmp -Raw | ConvertFrom-Json
    if (-not $keys.YT_REFRESH_TOKEN) { throw "Google returned no refresh token. Run the script again." }
    Push-Location $repo
    foreach ($name in "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN") {
        # --body, never a pipe: PowerShell 5.1 pipes add a hidden BOM that breaks the key.
        gh secret set $name --body $keys.$name
        if ($LASTEXITCODE -ne 0) { throw "Could not save $name to GitHub." }
    }
    Pop-Location
} finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
Write-Host "`nDone. The bot now uploads to the channel you just picked. Tell Claude 'done'."
