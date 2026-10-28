param([switch]$KeepStack, [string]$ResumeProject)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$testProject = 'education-acceptance-' + [guid]::NewGuid().ToString('N').Substring(0,8)
if ($ResumeProject) {
    if ($ResumeProject -notmatch '^education-acceptance-[0-9a-f]{8}$') {
        throw 'Only a generated acceptance project can be resumed'
    }
    $testProject = $ResumeProject
}
$composeArgs = @('--project-directory', $projectRoot, '-p', $testProject,
                 '-f', (Join-Path $projectRoot 'compose.yaml'),
                 '-f', (Join-Path $projectRoot 'compose.test.yaml'))
function DC {
    param([string[]]$DockerArgs)
    & docker compose @composeArgs @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw "Compose failed: $($DockerArgs -join ' ')" }
}
function Verify-Checkpoint {
    param([string]$Checkpoint, [string]$Module)
    $Checkpoint | & docker compose @composeArgs exec -T api python -m $Module verify
    if ($LASTEXITCODE -ne 0) { throw "Checkpoint failed: $Module" }
}
function Wait-Ready {
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:18000/health/ready' -TimeoutSec 5
            if ($response.StatusCode -eq 200) { return }
        } catch {}
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Isolated API did not become ready'
}
Write-Output "Isolated test project: $testProject (ports 18000/18080)"
try {
    DC @('up','--build','-d')
    Wait-Ready
    DC @('exec','-T','api','pytest','-q','-m','not integration')
    DC @('exec','-T','api','ruff','check','app','scripts','tests','migrations')
    DC @('exec','-T','-e','RUN_INTEGRATION=1','api','pytest','-q','-m','integration')
    DC @('exec','-T','api','python','-m','scripts.smoke')
    $domain = DC @('exec','-T','api','python','-m','scripts.stage2_checkpoint','prepare')
    $schemas = DC @('exec','-T','api','python','-m','scripts.schema_checkpoint','prepare')
    DC @('restart','api')
    Wait-Ready
    Verify-Checkpoint ($domain -join [Environment]::NewLine) 'scripts.stage2_checkpoint'
    foreach ($dependency in @('temporal','chroma','postgres')) {
        try {
            DC @('stop',$dependency)
            DC @('exec','-T','api','python','-m','scripts.check_dependency_failure',$dependency)
        } finally {
            DC @('start',$dependency)
        }
        Wait-Ready
    }
    $recovery = DC @('exec','-T','api','python','-m','scripts.recovery','prepare')
    DC @('restart','worker')
    Verify-Checkpoint ($recovery -join [Environment]::NewLine) 'scripts.recovery'
    $storage = DC @('exec','-T','api','python','-m','scripts.persistence','prepare')
    $recovery = DC @('exec','-T','api','python','-m','scripts.recovery','prepare')
    DC @('down')
    DC @('up','-d')
    Wait-Ready
    Verify-Checkpoint ($recovery -join [Environment]::NewLine) 'scripts.recovery'
    Verify-Checkpoint ($domain -join [Environment]::NewLine) 'scripts.stage2_checkpoint'
    Verify-Checkpoint ($storage -join [Environment]::NewLine) 'scripts.persistence'
    DC @('run','--rm','--no-deps','app-migrate')
    Verify-Checkpoint ($schemas -join [Environment]::NewLine) 'scripts.schema_checkpoint'
    Verify-Checkpoint ($domain -join [Environment]::NewLine) 'scripts.stage2_checkpoint'
    $storage | & docker compose @composeArgs exec -T api python -m scripts.persistence cleanup
    if ($LASTEXITCODE -ne 0) { throw 'Probe cleanup failed' }
    DC @('exec','-T','api','python','-m','scripts.audit_originals')
    Write-Output 'INTEGRATION PASSED: API, transactions, files, Temporal retries, recovery and persistence'
    if (-not $KeepStack) {
        if ($testProject -notmatch '^education-acceptance-[0-9a-f]{8}$') {
            throw 'Refusing cleanup of an unexpected project'
        }
        # Удаляем только тома тестового проекта, созданного скриптом, а не рабочего проекта.
        DC @('down','-v')
    }
} catch {
    Write-Output "Test stack preserved for diagnosis: $testProject"
    throw
}
