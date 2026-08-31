[CmdletBinding()]
param(
    [string]$Server = "root@195.19.7.56",
    [string]$RemoteDirectory = "/opt/natarazlozhi_money_profile_bot",
    [string]$HealthUrl = "https://money.natarazlozhi.ru/healthz"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$Command,
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

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

function Read-EnvExampleValue {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Name
    )

    $line = Get-Content -LiteralPath $Path -Encoding UTF8 |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -Last 1
    if (-not $line) {
        throw "Missing $Name in $Path."
    }
    return $line.Substring($Name.Length + 1)
}

if ($Server -notmatch '^[A-Za-z0-9._@-]+$') {
    throw "Server must be an SSH host or user@host without shell characters."
}
if ($RemoteDirectory -notmatch '^/[A-Za-z0-9._/-]+$') {
    throw "RemoteDirectory must be an absolute Unix path without shell characters."
}
if ($HealthUrl -notmatch '^https://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+$') {
    throw "HealthUrl must be an HTTPS URL without whitespace."
}

$repositoryRoot = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "--show-toplevel")
Push-Location $repositoryRoot
try {
    & git diff --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "Tracked files have uncommitted changes. Commit them before deployment."
    }
    & git diff --cached --quiet --
    if ($LASTEXITCODE -ne 0) {
        throw "The index has uncommitted changes. Commit them before deployment."
    }

    $branch = Read-NativeCommand -Command "git" -Arguments @("branch", "--show-current")
    if ($branch -ne "main") {
        throw "Routine production deployment is allowed only from main; current branch is '$branch'."
    }

    $commit = Read-NativeCommand -Command "git" -Arguments @("rev-parse", "HEAD")
    if ($commit -notmatch '^[0-9a-f]{40}$') {
        throw "Git returned an invalid commit SHA: '$commit'."
    }

    Write-Host "Pushing commit $commit to origin/main..."
    Invoke-NativeCommand -Command "git" -Arguments @("push", "origin", "HEAD:main")

    $composePath = Join-Path $repositoryRoot "compose.yaml"
    $remoteScriptPath = Join-Path $repositoryRoot "scripts/deploy_remote.sh"
    $envExamplePath = Join-Path $repositoryRoot ".env.example"
    $legalDocsVersion = Read-EnvExampleValue -Path $envExamplePath -Name "LEGAL_DOCS_VERSION"
    $operatorEmail = Read-EnvExampleValue -Path $envExamplePath -Name "OPERATOR_EMAIL"
    if ($legalDocsVersion -notmatch '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$') {
        throw "LEGAL_DOCS_VERSION has an invalid release format."
    }
    if ($operatorEmail -notmatch '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$') {
        throw "OPERATOR_EMAIL has an invalid format."
    }
    $composeBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($composePath))
    $scriptBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($remoteScriptPath))
    $stagingDirectory = "/tmp/money-profile-deploy-$commit"

    $remoteCommand = @(
        "set -eu"
        "umask 077"
        "mkdir -p '$stagingDirectory'"
        "printf '%s' '$composeBase64' | base64 -d > '$stagingDirectory/compose.yaml'"
        "printf '%s' '$scriptBase64' | base64 -d > '$stagingDirectory/deploy_remote.sh'"
        "chmod 700 '$stagingDirectory/deploy_remote.sh'"
        "exec bash '$stagingDirectory/deploy_remote.sh' '$commit' '$RemoteDirectory' '$HealthUrl' '$stagingDirectory/compose.yaml' '$legalDocsVersion' '$operatorEmail'"
    ) -join "; "

    Write-Host "Deploying the same commit through one SSH session..."
    & ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 $Server $remoteCommand
    if ($LASTEXITCODE -ne 0) {
        throw "SSH deployment failed. Configure the deployment key for $Server once, then rerun this same command."
    }
    Write-Host "Deployment completed: $commit"
}
finally {
    Pop-Location
}
