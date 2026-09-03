[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Enable", "Disable")]
    [string]$State,
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot",
    [switch]$AcknowledgeProfessionalReview,
    [switch]$AcknowledgeOwnerOnlyPilot,
    [switch]$AcknowledgeRealCharge
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
if (
    $State -eq "Enable" -and
    (
        -not $AcknowledgeProfessionalReview.IsPresent -or
        -not $AcknowledgeOwnerOnlyPilot.IsPresent -or
        -not $AcknowledgeRealCharge.IsPresent
    )
) {
    throw (
        "Enabling pilot payments requires acknowledgements for professional review, " +
        "the owner-only allowlist, and a real charge."
    )
}

$repositoryRoot = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "--show-toplevel")
Push-Location $repositoryRoot
try {
    & git diff --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "Tracked files have uncommitted changes. Commit them before requesting a payment state change."
    }
    & git diff --cached --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "The index has uncommitted changes. Commit them before requesting a payment state change."
    }

    $branch = Read-NativeCommand -Command "git" -Arguments @("branch", "--show-current")
    if ($branch -ne "main") {
        throw "Pilot payment changes are allowed only from main; current branch is '$branch'."
    }
    $commit = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "HEAD")
    if ($commit -notmatch '^[0-9a-f]{40}$') {
        throw "Git returned an invalid commit SHA."
    }

    $action = $State.ToLowerInvariant()
    $remoteScript = @'
import os
from pathlib import Path
import re
import sys

deploy_directory = Path(sys.argv[1]).resolve()
action = sys.argv[2]
commit = sys.argv[3]
env_path = deploy_directory / ".env"
request_path = deploy_directory / ".pilot-payment-request"

if action not in {"enable", "disable"}:
    raise SystemExit("Invalid pilot payment action.")
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit("Invalid commit SHA.")
if not deploy_directory.is_absolute() or not env_path.is_file():
    raise SystemExit("The remote deployment directory or .env does not exist.")

values = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        name, value = line.split("=", 1)
        values[name] = value

if values.get("APP_ENV") != "pilot":
    raise SystemExit("Pilot payment changes require APP_ENV=pilot.")
if values.get("PAYMENT_MODE") != "robokassa" or values.get("ROBOKASSA_TEST_MODE") != "false":
    raise SystemExit("Pilot payment changes require live Robokassa mode.")
if values.get("SOURCE_COMMIT") != commit:
    raise SystemExit("Deploy the target commit before requesting a pilot payment state change.")
owner_id = values.get("ADMIN_TELEGRAM_IDS", "")
pilot_id = values.get("PILOT_ACCESS_TELEGRAM_IDS", "")
if not re.fullmatch(r"[1-9][0-9]*", owner_id) or pilot_id != owner_id:
    raise SystemExit("Pilot payments require one identical owner ID in the admin and pilot allowlists.")
for name in (
    "ROBOKASSA_MERCHANT_LOGIN",
    "ROBOKASSA_PASSWORD1",
    "ROBOKASSA_PASSWORD2",
    "ROBOKASSA_PASSWORD3",
):
    if not values.get(name):
        raise SystemExit(f"Pilot payment configuration is missing {name}.")
if values.get("PAYMENT_PLATFORM_RISK_ACKNOWLEDGED") != "true":
    raise SystemExit("Pilot payment configuration is missing the platform-risk acknowledgement.")

next_path = deploy_directory / f".pilot-payment-request-{os.getpid()}"
try:
    next_path.write_text(f"{action}:{commit}\n", encoding="ascii")
    os.chmod(next_path, 0o600)
    os.replace(next_path, request_path)
    os.chmod(request_path, 0o600)
finally:
    if next_path.exists():
        next_path.unlink()

print(f"Pilot payment {action} request prepared for commit {commit}.")
'@
    $utf8 = [Text.UTF8Encoding]::new($false)
    $remoteScriptBase64 = [Convert]::ToBase64String($utf8.GetBytes($remoteScript))
    $remoteCommand = (
        "python3 -c `"import base64;exec(base64.b64decode('$remoteScriptBase64'))`" " +
        "'$RemoteDirectory' '$action' '$commit'"
    )

    & ssh -o BatchMode=yes -o ConnectTimeout=10 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Pilot payment request failed; the running service was not changed."
    }
    Write-Host "Run pwsh -NoProfile -File scripts/deploy.ps1 to apply the requested state."
}
finally {
    Pop-Location
}
