[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Add", "Remove")]
    [string]$State,
    [Parameter(Mandatory)]
    [long]$TelegramId,
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot",
    [switch]$AcknowledgeFullAdminAccess
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Read-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$Command,
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $output = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
    return ($output | Out-String).Trim()
}

if ($Server -notmatch '^[A-Za-z0-9._@-]+$') {
    throw "Server must be an SSH host or user@host without shell characters."
}
if ($RemoteDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteDirectory must be an absolute Unix path without shell characters."
}
if ($TelegramId -le 0) {
    throw "TelegramId must be a positive integer."
}
if ($State -eq "Add" -and -not $AcknowledgeFullAdminAccess.IsPresent) {
    throw "Adding a production administrator requires acknowledgement of statistics and refund access."
}

$repositoryRoot = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "--show-toplevel")
Push-Location $repositoryRoot
try {
    & git diff --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "Tracked files have uncommitted changes. Commit them before requesting an admin change."
    }
    & git diff --cached --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "The index has uncommitted changes. Commit them before requesting an admin change."
    }

    $branch = Read-NativeCommand -Command "git" -Arguments @("branch", "--show-current")
    if ($branch -ne "main") {
        throw "Production admin changes are allowed only from main; current branch is '$branch'."
    }
    $commit = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "HEAD")
    if ($commit -notmatch '^[0-9a-f]{40}$') {
        throw "Git returned an invalid commit SHA."
    }
    $action = $State.ToLowerInvariant()
    $telegramIdText = $TelegramId.ToString([Globalization.CultureInfo]::InvariantCulture)

    $remoteScript = @'
import os
from pathlib import Path
import re
import sys

deploy_directory = Path(sys.argv[1]).resolve()
action = sys.argv[2]
telegram_id = sys.argv[3]
commit = sys.argv[4]
env_path = deploy_directory / ".env"
request_path = deploy_directory / ".production-admin-request"

if action not in {"add", "remove"}:
    raise SystemExit("Invalid production admin action.")
if not re.fullmatch(r"[1-9][0-9]*", telegram_id):
    raise SystemExit("Invalid Telegram ID.")
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("Invalid commit SHA.")
if not deploy_directory.is_absolute() or not env_path.is_file():
    raise SystemExit("The remote deployment directory or .env does not exist.")

values = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        name, value = line.split("=", 1)
        values[name] = value

if values.get("APP_ENV") != "production":
    raise SystemExit("Production admin changes require APP_ENV=production.")
if values.get("SOURCE_COMMIT") != commit:
    raise SystemExit("Deploy the target commit before requesting a production admin change.")

admin_ids = values.get("ADMIN_TELEGRAM_IDS", "")
if not re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", admin_ids):
    raise SystemExit("Production ADMIN_TELEGRAM_IDS is empty or invalid.")
if action == "remove" and admin_ids == telegram_id:
    raise SystemExit("Refusing to remove the last production administrator.")

for conflicting in (
    ".pilot-payment-request",
    ".production-transition-request",
    ".production-payment-request",
):
    if (deploy_directory / conflicting).exists():
        raise SystemExit(f"Apply the conflicting request {conflicting} first.")

next_path = deploy_directory / f".production-admin-request-{os.getpid()}"
try:
    next_path.write_text(f"{action}:{telegram_id}:{commit}\n", encoding="ascii")
    os.chmod(next_path, 0o600)
    os.replace(next_path, request_path)
    os.chmod(request_path, 0o600)
finally:
    if next_path.exists():
        next_path.unlink()

print(f"Production admin {action} request prepared for Telegram ID {telegram_id} and commit {commit}.")
'@
    $utf8 = [Text.UTF8Encoding]::new($false)
    $remoteScriptBase64 = [Convert]::ToBase64String($utf8.GetBytes($remoteScript))
    $remoteCommand = (
        "python3 -c `"import base64;exec(base64.b64decode('$remoteScriptBase64'))`" " +
        "'$RemoteDirectory' '$action' '$telegramIdText' '$commit'"
    )

    & ssh -o BatchMode=yes -o ConnectTimeout=10 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Production admin request failed; the running service was not changed."
    }
    Write-Host "Run pwsh -NoProfile -File scripts/deploy.ps1 to apply the requested admin list."
}
finally {
    Pop-Location
}
