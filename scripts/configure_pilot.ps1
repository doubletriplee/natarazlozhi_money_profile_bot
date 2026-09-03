[CmdletBinding()]
param(
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot",
    [Parameter(Mandatory)]
    [Security.SecureString]$BotToken,
    [Parameter(Mandatory)]
    [string]$MerchantLogin,
    [Parameter(Mandatory)]
    [Security.SecureString]$Password1,
    [Parameter(Mandatory)]
    [Security.SecureString]$Password2,
    [Parameter(Mandatory)]
    [Security.SecureString]$Password3,
    [Parameter(Mandatory)]
    [long]$TelegramId,
    [string]$BotUsername = "money_profile_pilot_bot",
    [Parameter(Mandatory)]
    [switch]$AcknowledgePlatformRisk
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function ConvertFrom-SecureValue {
    param(
        [Parameter(Mandatory)]
        [Security.SecureString]$Value
    )

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

if ($Server -notmatch '^[A-Za-z0-9._@-]+$') {
    throw "Server must be an SSH host or user@host without shell characters."
}
if ($RemoteDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteDirectory must be an absolute Unix path without shell characters."
}
if ($MerchantLogin -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "MerchantLogin contains characters that are unsafe for the server env file."
}
if ($TelegramId -le 0) {
    throw "TelegramId must be a positive numeric Telegram ID."
}
if (-not $AcknowledgePlatformRisk.IsPresent) {
    throw "AcknowledgePlatformRisk is required for a live Robokassa pilot."
}
$normalizedBotUsername = $BotUsername.Trim().TrimStart('@')
if ($normalizedBotUsername -notmatch '^[A-Za-z][A-Za-z0-9_]{4,31}$') {
    throw "BotUsername is not a valid Telegram bot username."
}

$plainBotToken = ConvertFrom-SecureValue -Value $BotToken
$plainPassword1 = ConvertFrom-SecureValue -Value $Password1
$plainPassword2 = ConvertFrom-SecureValue -Value $Password2
$plainPassword3 = ConvertFrom-SecureValue -Value $Password3
try {
    if ($plainBotToken -notmatch '^[1-9][0-9]{5,}:[A-Za-z0-9_-]+$') {
        throw "BotToken is not a valid Telegram bot token."
    }
    if (
        $plainPassword1 -notmatch '^[A-Za-z0-9]+$' -or
        $plainPassword2 -notmatch '^[A-Za-z0-9]+$' -or
        $plainPassword3 -notmatch '^[A-Za-z0-9]+$'
    ) {
        throw "Robokassa live passwords must be non-empty alphanumeric values."
    }

    $payload = @{
        bot_token = $plainBotToken
        merchant_login = $MerchantLogin
        password1 = $plainPassword1
        password2 = $plainPassword2
        password3 = $plainPassword3
        telegram_id = $TelegramId.ToString([Globalization.CultureInfo]::InvariantCulture)
        bot_username = $normalizedBotUsername
    } | ConvertTo-Json -Compress

    $remoteScript = @'
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

deploy_directory = Path(sys.argv[1]).resolve()
env_path = deploy_directory / ".env"
backup_path = deploy_directory / ".env.before-pilot"

if not deploy_directory.is_absolute() or not env_path.is_file():
    raise SystemExit("The remote deployment directory or .env does not exist.")

payload = json.load(sys.stdin)
expected = {
    "bot_token", "merchant_login", "password1", "password2", "password3",
    "telegram_id", "bot_username",
}
if set(payload) != expected:
    raise SystemExit("Unexpected pilot configuration fields.")
if not re.fullmatch(r"[1-9][0-9]{5,}:[A-Za-z0-9_-]+", payload["bot_token"]):
    raise SystemExit("Invalid Telegram bot token.")
if not re.fullmatch(r"[A-Za-z0-9_.-]+", payload["merchant_login"]):
    raise SystemExit("Invalid MerchantLogin.")
for number in ("1", "2", "3"):
    if not re.fullmatch(r"[A-Za-z0-9]+", payload[f"password{number}"]):
        raise SystemExit(f"Invalid Robokassa live password #{number}.")
if not re.fullmatch(r"[1-9][0-9]*", payload["telegram_id"]):
    raise SystemExit("Invalid Telegram ID.")
if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", payload["bot_username"]):
    raise SystemExit("Invalid Telegram bot username.")

original = env_path.read_text(encoding="utf-8")
current = {}
for line in original.splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        name, value = line.split("=", 1)
        current[name] = value

def new_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")

updates = {
    "APP_ENV": "pilot",
    "BOT_TOKEN": payload["bot_token"],
    "BOT_USERNAME": payload["bot_username"],
    "ADMIN_TELEGRAM_IDS": payload["telegram_id"],
    "TEST_ACCESS_TELEGRAM_IDS": "",
    "PILOT_ACCESS_TELEGRAM_IDS": payload["telegram_id"],
    "BOOTSTRAP_ADMIN_ON_FIRST_START": "false",
    "METHODOLOGY_APPROVED": "true",
    "PAYMENT_MODE": "robokassa",
    "ROBOKASSA_MERCHANT_LOGIN": payload["merchant_login"],
    "ROBOKASSA_PASSWORD1": payload["password1"],
    "ROBOKASSA_PASSWORD2": payload["password2"],
    "ROBOKASSA_PASSWORD3": payload["password3"],
    "ROBOKASSA_TEST_PASSWORD1": "",
    "ROBOKASSA_TEST_PASSWORD2": "",
    "ROBOKASSA_TEST_MODE": "false",
    "ROBOKASSA_HASH_ALGORITHM": "sha256",
    "LIVE_PAYMENTS_ENABLED": "false",
    "PILOT_LIVE_PAYMENT_REVIEWED": "false",
    "PAYMENT_PLATFORM_RISK_ACKNOWLEDGED": "true",
    "DATABASE_URL": "sqlite+aiosqlite:////data/money_profile_pilot.sqlite3",
}
if current.get("APP_ENV") != "pilot":
    updates.update({
        "APP_ENCRYPTION_KEY": new_key(),
        "LOOKUP_HMAC_KEY": new_key(),
        "BACKUP_ENCRYPTION_KEY": new_key(),
    })
else:
    for name in ("APP_ENCRYPTION_KEY", "LOOKUP_HMAC_KEY", "BACKUP_ENCRYPTION_KEY"):
        if not current.get(name):
            updates[name] = new_key()

result = []
written = set()
for line in original.splitlines():
    name = line.split("=", 1)[0] if "=" in line else ""
    if name in updates:
        if name not in written:
            result.append(f"{name}={updates[name]}")
            written.add(name)
        continue
    result.append(line)
for name, value in updates.items():
    if name not in written:
        result.append(f"{name}={value}")

if not backup_path.exists():
    backup_path.write_text(original, encoding="utf-8")
    os.chmod(backup_path, 0o600)

next_path = deploy_directory / f".env.pilot-next-{os.getpid()}"
try:
    next_path.write_text("\n".join(result) + "\n", encoding="utf-8")
    os.chmod(next_path, 0o600)
    os.replace(next_path, env_path)
    os.chmod(env_path, 0o600)

    subprocess.run(
        ["docker", "compose", "--env-file", ".env", "config"],
        cwd=deploy_directory,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        [
            "docker", "compose", "--env-file", ".env", "run", "--rm", "--no-deps",
            "--entrypoint", "python", "app", "-c",
            "from money_profile_bot.config import Settings; Settings()",
        ],
        cwd=deploy_directory,
        stdout=subprocess.DEVNULL,
        check=True,
    )
except Exception:
    restore_path = deploy_directory / f".env.restore-{os.getpid()}"
    restore_path.write_text(original, encoding="utf-8")
    os.chmod(restore_path, 0o600)
    os.replace(restore_path, env_path)
    raise
finally:
    if next_path.exists():
        next_path.unlink()

print("Pilot settings validated with live payments paused. The service has not been restarted.")
'@

    $utf8 = [Text.UTF8Encoding]::new($false)
    $remoteScriptBase64 = [Convert]::ToBase64String($utf8.GetBytes($remoteScript))
    $remoteCommand = "python3 -c `"import base64;exec(base64.b64decode('$remoteScriptBase64'))`" '$RemoteDirectory'"

    $payload | & ssh -o BatchMode=yes -o ConnectTimeout=10 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Pilot configuration failed; the running service was not restarted."
    }
}
finally {
    $plainBotToken = $null
    $plainPassword1 = $null
    $plainPassword2 = $null
    $plainPassword3 = $null
    $payload = $null
}
