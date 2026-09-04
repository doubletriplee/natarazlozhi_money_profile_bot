[CmdletBinding()]
param(
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot",
    [Parameter(Mandatory)]
    [switch]$AcknowledgeGoldenCards,
    [Parameter(Mandatory)]
    [switch]$AcknowledgePublicAccess,
    [Parameter(Mandatory)]
    [switch]$AcknowledgePaymentsPaused
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
    -not $AcknowledgeGoldenCards.IsPresent -or
    -not $AcknowledgePublicAccess.IsPresent -or
    -not $AcknowledgePaymentsPaused.IsPresent
) {
    throw "Production transition requires all three explicit acknowledgements."
}

$repositoryRoot = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "--show-toplevel")
Push-Location $repositoryRoot
try {
    & git diff --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "Tracked files have uncommitted changes. Commit them before requesting production."
    }
    & git diff --cached --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "The index has uncommitted changes. Commit them before requesting production."
    }

    $branch = Read-NativeCommand -Command "git" -Arguments @("branch", "--show-current")
    if ($branch -ne "main") {
        throw "Production transition is allowed only from main; current branch is '$branch'."
    }
    $commit = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "HEAD")
    if ($commit -notmatch '^[0-9a-f]{40}$') {
        throw "Git returned an invalid commit SHA."
    }

    $remoteScript = @'
import os
from pathlib import Path
import re
import sys

deploy_directory = Path(sys.argv[1]).resolve()
commit = sys.argv[2]
env_path = deploy_directory / ".env"
request_path = deploy_directory / ".production-transition-request"

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
    raise SystemExit("Production transition requires the verified APP_ENV=pilot release.")
if values.get("SOURCE_COMMIT") != commit:
    raise SystemExit("Deploy the target commit in pilot before requesting production.")
if values.get("PAYMENT_MODE") != "robokassa" or values.get("ROBOKASSA_TEST_MODE") != "false":
    raise SystemExit("Production requires live Robokassa configuration.")
if values.get("LIVE_PAYMENTS_ENABLED") != "false":
    raise SystemExit("Disable pilot payments before requesting public production.")
if values.get("METHODOLOGY_APPROVED") != "true":
    raise SystemExit("The approved methodology flag is missing.")
if values.get("PAYMENT_PLATFORM_RISK_ACKNOWLEDGED") != "true":
    raise SystemExit("The platform-risk acknowledgement is missing.")
if values.get("LEGAL_DOCS_VERSION", "").upper() in {"", "DRAFT"}:
    raise SystemExit("Final legal documents are required.")
for name in (
    "BOT_TOKEN",
    "ADMIN_TELEGRAM_IDS",
    "ROBOKASSA_MERCHANT_LOGIN",
    "ROBOKASSA_PASSWORD1",
    "ROBOKASSA_PASSWORD2",
    "ROBOKASSA_PASSWORD3",
    "APP_ENCRYPTION_KEY",
    "LOOKUP_HMAC_KEY",
    "BACKUP_ENCRYPTION_KEY",
):
    if not values.get(name):
        raise SystemExit(f"Production configuration is missing {name}.")
for conflicting in (".pilot-payment-request", ".production-payment-request"):
    if (deploy_directory / conflicting).exists():
        raise SystemExit(f"Remove or apply the conflicting request {conflicting} first.")

next_path = deploy_directory / f".production-transition-request-{os.getpid()}"
try:
    next_path.write_text(f"prepare:{commit}\n", encoding="ascii")
    os.chmod(next_path, 0o600)
    os.replace(next_path, request_path)
    os.chmod(request_path, 0o600)
finally:
    if next_path.exists():
        next_path.unlink()

print(f"Paused-payment production transition prepared for commit {commit}.")
'@
    $utf8 = [Text.UTF8Encoding]::new($false)
    $remoteScriptBase64 = [Convert]::ToBase64String($utf8.GetBytes($remoteScript))
    $remoteCommand = (
        "python3 -c `"import base64;exec(base64.b64decode('$remoteScriptBase64'))`" " +
        "'$RemoteDirectory' '$commit'"
    )

    & ssh -o BatchMode=yes -o ConnectTimeout=10 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Production transition request failed; the running service was not changed."
    }
    Write-Host "Run pwsh -NoProfile -File scripts/deploy.ps1 to apply public production with payments paused."
}
finally {
    Pop-Location
}
