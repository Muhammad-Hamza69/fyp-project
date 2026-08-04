# Sets SUPABASE_ACCESS_TOKEN as a User environment variable.
#
# Why a script: the Supabase CLI cannot do browser login in a non-TTY session,
# so it needs a Personal Access Token. This prompts for it without echoing to
# screen, shell history, or any log.
#
# Usage:  powershell -ExecutionPolicy Bypass -File infra\scripts\set-supabase-token.ps1

Write-Host ""
Write-Host "Supabase access token setup" -ForegroundColor Cyan
Write-Host "---------------------------"
Write-Host "1. Open: https://supabase.com/dashboard/account/tokens"
Write-Host "2. Click 'Generate new token', name it e.g. 'neuroflow-cli'"
Write-Host "3. Copy the token and paste it below (input is hidden)"
Write-Host ""

$secure = Read-Host -Prompt "Paste SUPABASE_ACCESS_TOKEN" -AsSecureString
$bstr   = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$plain  = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)

if ([string]::IsNullOrWhiteSpace($plain)) {
    Write-Host "No token entered - nothing changed." -ForegroundColor Yellow
    exit 1
}

if ($plain -notmatch '^sbp_') {
    Write-Host "Warning: Supabase personal access tokens normally start with 'sbp_'." -ForegroundColor Yellow
    $ok = Read-Host "Continue anyway? (y/N)"
    if ($ok -ne 'y') { Write-Host "Aborted."; exit 1 }
}

[Environment]::SetEnvironmentVariable("SUPABASE_ACCESS_TOKEN", $plain, "User")
$env:SUPABASE_ACCESS_TOKEN = $plain
$plain = $null

Write-Host ""
Write-Host "Saved as a User environment variable." -ForegroundColor Green
Write-Host "Verifying with the Supabase CLI..." -ForegroundColor Cyan
Write-Host ""

& supabase projects list
