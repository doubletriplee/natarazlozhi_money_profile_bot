[CmdletBinding()]
param(
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot",
    [Parameter(Mandatory)]
    [string]$MerchantLogin,
    [Parameter(Mandatory)]
    [Security.SecureString]$TestPassword1,
    [Parameter(Mandatory)]
    [Security.SecureString]$TestPassword2,
    [Parameter(Mandatory)]
    [long]$TelegramId,
    [string]$BotUsername = "money_profile_test_bot"
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
$normalizedBotUsername = $BotUsername.Trim().TrimStart('@')
if ($normalizedBotUsername -notmatch '^[A-Za-z][A-Za-z0-9_]{4,31}$') {
    throw "BotUsername is not a valid Telegram bot username."
}

$plainPassword1 = ConvertFrom-SecureValue -Value $TestPassword1
$plainPassword2 = ConvertFrom-SecureValue -Value $TestPassword2
try {
    if ($plainPassword1 -notmatch '^[A-Za-z0-9]+$' -or $plainPassword2 -notmatch '^[A-Za-z0-9]+$') {
        throw "Robokassa test passwords must be non-empty alphanumeric values."
    }

    $payload = @{
        merchant_login = $MerchantLogin
        test_password1 = $plainPassword1
        test_password2 = $plainPassword2
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
backup_path = deploy_directory / ".env.before-staging"

if not deploy_directory.is_absolute() or not env_path.is_file():
    raise SystemExit("The remote deployment directory or .env does not exist.")

payload = json.load(sys.stdin)
expected = {"merchant_login", "test_password1", "test_password2", "telegram_id", "bot_username"}
if set(payload) != expected:
    raise SystemExit("Unexpected staging configuration fields.")
if not re.fullmatch(r"[A-Za-z0-9_.-]+", payload["merchant_login"]):
    raise SystemExit("Invalid MerchantLogin.")
if not re.fullmatch(r"[A-Za-z0-9]+", payload["test_password1"]):
    raise SystemExit("Invalid Robokassa test password #1.")
if not re.fullmatch(r"[A-Za-z0-9]+", payload["test_password2"]):
    raise SystemExit("Invalid Robokassa test password #2.")
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
if not current.get("BOT_TOKEN"):
    raise SystemExit("The existing server .env has no BOT_TOKEN.")

def new_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")

updates = {
    "APP_ENV": "staging",
    "BOT_USERNAME": payload["bot_username"],
    "ADMIN_TELEGRAM_IDS": payload["telegram_id"],
    "TEST_ACCESS_TELEGRAM_IDS": payload["telegram_id"],
    "PILOT_ACCESS_TELEGRAM_IDS": "",
    "BOOTSTRAP_ADMIN_ON_FIRST_START": "false",
    "PAYMENT_MODE": "robokassa",
    "ROBOKASSA_MERCHANT_LOGIN": payload["merchant_login"],
    "ROBOKASSA_PASSWORD1": "",
    "ROBOKASSA_PASSWORD2": "",
    "ROBOKASSA_PASSWORD3": "",
    "ROBOKASSA_TEST_PASSWORD1": payload["test_password1"],
    "ROBOKASSA_TEST_PASSWORD2": payload["test_password2"],
    "ROBOKASSA_TEST_MODE": "true",
    "ROBOKASSA_HASH_ALGORITHM": "sha256",
    "LIVE_PAYMENTS_ENABLED": "false",
    "PILOT_LIVE_PAYMENT_REVIEWED": "false",
    "PRODUCTION_LIVE_PAYMENT_REVIEWED": "false",
    "PAYMENT_PLATFORM_RISK_ACKNOWLEDGED": "false",
    "DATABASE_URL": "sqlite+aiosqlite:////data/money_profile_staging.sqlite3",
}
if current.get("APP_ENV") != "staging":
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

next_path = deploy_directory / f".env.staging-next-{os.getpid()}"
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

print("Staging settings validated. The service has not been restarted.")
'@

    $utf8 = [Text.UTF8Encoding]::new($false)
    $remoteScriptBase64 = [Convert]::ToBase64String($utf8.GetBytes($remoteScript))
    $remoteCommand = "python3 -c `"import base64;exec(base64.b64decode('$remoteScriptBase64'))`" '$RemoteDirectory'"

    $payload | & ssh -o BatchMode=yes -o ConnectTimeout=10 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "Staging configuration failed; the running service was not restarted."
    }
}
finally {
    $plainPassword1 = $null
    $plainPassword2 = $null
    $payload = $null
}
