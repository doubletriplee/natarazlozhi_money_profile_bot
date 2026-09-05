[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [AllowEmptyString()]
    [string]$TelegramIds,
    [switch]$IncludeTest,
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if ($Server -notmatch '^[A-Za-z0-9._@-]+$' -or $RemoteDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "Invalid deployment server or absolute remote directory."
}
$recipientIds = @()
foreach ($item in $TelegramIds.Split(',')) {
    $value = $item.Trim()
    if ($value -eq '') { continue }
    if ($value -notmatch '^[1-9][0-9]*$' -or $value.Length -gt 16 -or [long]$value -ge 4503599627370496) {
        throw "Recipients must be positive Telegram user IDs."
    }
    $recipientIds += [long]$value
}
$normalizedIds = ($recipientIds | Sort-Object -Unique) -join ','
$testFlag = if ($IncludeTest.IsPresent) { "true" } else { "false" }
$repositoryRoot = (& git rev-parse --show-toplevel | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw "Cannot find the repository." }
Push-Location $repositoryRoot
try {
    & git diff --quiet --
    if ($LASTEXITCODE -ne 0) { throw "Commit tracked changes before configuring notifications." }
    & git diff --cached --quiet --
    if ($LASTEXITCODE -ne 0) { throw "Commit staged changes before configuring notifications." }
    $branch = (& git branch --show-current | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "main") { throw "Use main for deployment requests." }
    $commit = (& git rev-parse HEAD | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw "Invalid commit SHA." }

    $remoteScript = @'
import os
from pathlib import Path
import re
import sys

directory = Path(sys.argv[1]).resolve()
recipients, include_test, commit = sys.argv[2:5]
if not (directory / ".env").is_file():
    raise SystemExit("Deployment .env does not exist.")
if not re.fullmatch(r"(?:[1-9][0-9]*(?:,[1-9][0-9]*)*)?", recipients):
    raise SystemExit("Invalid recipients.")
if any(int(value) >= 2**52 for value in recipients.split(",") if value):
    raise SystemExit("Invalid Telegram user ID.")
if include_test not in {"true", "false"} or not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("Invalid notification request.")
request = directory / ".payment-notifications-request"
temporary = directory / f".payment-notifications-request-{os.getpid()}"
try:
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(f"{recipients}:{include_test}:{commit}\n")
    os.replace(temporary, request)
finally:
    temporary.unlink(missing_ok=True)
print("Payment notification configuration request prepared; running service unchanged.")
'@
    $encoded = [Convert]::ToBase64String([Text.UTF8Encoding]::new($false).GetBytes($remoteScript))
    $remoteCommand = (
        "python3 -c `"import base64;exec(base64.b64decode('$encoded'))`" " +
        "'$RemoteDirectory' '$normalizedIds' '$testFlag' '$commit'"
    )
    & ssh -o BatchMode=yes -o ConnectTimeout=10 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) { throw "Cannot prepare notification configuration." }
    Write-Host "Run pwsh -NoProfile -File scripts/deploy.ps1 to apply the recipient list."
}
finally {
    Pop-Location
}
