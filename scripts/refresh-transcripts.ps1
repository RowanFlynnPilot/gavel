<#
.SYNOPSIS
    Fetch transcripts for stuck meetings from this machine, commit, and push.

.DESCRIPTION
    YouTube blocks GitHub Actions from captions and per-video metadata, so this
    machine's residential IP is the deployment's only transcript path. Run it
    from Task Scheduler several times a day.

    engine.fetch_transcripts --all pulls captions for agenda-only and
    never-ingested YouTube meetings, matches agenda-platform meetings to their
    channel recordings, and Whisper-transcribes audio-only jurisdictions
    (capped per run by MAX_AUDIO_JOBS). This script then commits everything
    new or changed under transcripts/ and pushes; CI re-summarizes on its next
    run. No API key is needed here.

    A scheduled task has no console, so every run writes
    logs\refresh-<time>.log (kept 14 days), the only record of what it did.

.PARAMETER NoPush
    Fetch transcripts but don't commit or push. Useful for dry-runs.

.EXAMPLE
    .\scripts\refresh-transcripts.ps1

.EXAMPLE
    .\scripts\refresh-transcripts.ps1 -NoPush
#>

[CmdletBinding()]
param(
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$LogDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("refresh-{0}.log" -f (Get-Date -Format "yyyy-MM-dd_HHmm"))
Get-ChildItem $LogDir -Filter "refresh-*.log" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -Confirm:$false

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# cmd.exe does the redirection so a native command's stderr never becomes a
# PowerShell ErrorRecord ($ErrorActionPreference = "Stop" would turn git's
# informational stderr into a crash). Returns the exit code.
function Invoke-Logged([string]$CommandLine) {
    Add-Content -Path $LogFile -Value "> $CommandLine" -Encoding UTF8
    cmd /c "$CommandLine >> `"$LogFile`" 2>&1"
    return $LASTEXITCODE
}

$env:PYTHONIOENCODING = "utf-8"
# Unbuffered, so the log is live and a run killed at the time limit still
# shows how far it got.
$env:PYTHONUNBUFFERED = "1"
$exitCode = 0
Write-Log "refresh-transcripts: $ProjectRoot"

$VenvScripts = Join-Path $ProjectRoot ".venv\Scripts"
$Python = Join-Path $VenvScripts "python.exe"
if (-not (Test-Path $Python)) {
    Write-Log "[error] Python venv not found at $Python"
    exit 2
}
# The engine's channel listing runs a bare "yt-dlp", and the venv is not
# activated here: put the venv's copy (the one the weekly update below keeps
# current) first on PATH.
$env:Path = "$VenvScripts;$env:Path"

# Weekly: keep the YouTube-facing libraries current. yt-dlp has to track
# YouTube's changes; a copy a few months old stops extracting.
$Stamp = Join-Path $LogDir ".deps-updated"
if (-not (Test-Path $Stamp) -or ((Get-Date) - (Get-Item $Stamp).LastWriteTime).TotalDays -ge 7) {
    Write-Log "[pip] Weekly update of yt-dlp and youtube-transcript-api"
    $code = Invoke-Logged "`"$Python`" -m pip install --upgrade --quiet yt-dlp youtube-transcript-api"
    if ($code -eq 0) {
        Set-Content -Path $Stamp -Value (Get-Date -Format o)
    } else {
        Write-Log "[warn] pip upgrade failed (exit $code); continuing with installed versions"
    }
}

Write-Log "[git] Pulling latest from origin/main"
$code = Invoke-Logged "git pull --rebase --autostash origin main"
if ($code -ne 0) {
    Write-Log "[warn] git pull failed (exit $code); proceeding with the local copy"
}

Write-Log "[fetch] python -m engine.fetch_transcripts --all"
$fetchExit = Invoke-Logged "`"$Python`" -m engine.fetch_transcripts --all"
if ($fetchExit -ne 0) {
    Write-Log "[error] engine.fetch_transcripts exited $fetchExit (details above)"
    $exitCode = 1
}

# Commit everything new or changed under transcripts/, not just what this run
# created: a run killed at the time limit leaves finished transcripts on disk
# that a before/after comparison would never push.
Invoke-Logged "git add transcripts/" | Out-Null
$staged = @(git diff --cached --name-only -- transcripts/)
if ($staged.Count -eq 0) {
    Write-Log "[done] Nothing new under transcripts/."
    exit $exitCode
}
$txt = @($staged | Where-Object { $_ -like "*.txt" })
Write-Log "[ok] $($staged.Count) changed file(s) under transcripts/, $($txt.Count) transcript(s)"
$staged | ForEach-Object { Write-Log "       $_" }

if ($NoPush) {
    Write-Log "[skip] -NoPush set; changes left staged."
    exit $exitCode
}

$MsgFile = Join-Path $LogDir ".commit-msg"
if ($txt.Count -gt 0) {
    $names = ($txt | ForEach-Object { Split-Path $_ -Leaf }) -join "`n"
    $msg = "chore: fetch transcripts for $($txt.Count) stuck meeting(s) [skip ci]`n`n$names"
} else {
    $msg = "chore: update transcript metadata [skip ci]"
}
Set-Content -Path $MsgFile -Value $msg -Encoding ASCII

$code = Invoke-Logged "git commit -F `"$MsgFile`""
if ($code -ne 0) {
    Write-Log "[error] git commit failed (exit $code)"
    exit 1
}

$code = Invoke-Logged "git push origin main"
if ($code -ne 0) {
    Write-Log "[warn] Push rejected; rebasing on origin/main and retrying"
    Invoke-Logged "git pull --rebase --autostash origin main" | Out-Null
    $code = Invoke-Logged "git push origin main"
}
if ($code -ne 0) {
    Write-Log "[error] git push failed (exit $code)"
    exit 1
}

Write-Log "[done] Pushed. CI ingests these on its next run."
exit $exitCode
