param(
    [string]$Repo,
    [string]$BacklogPath = "ops/github-backlog.json",
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

function Require-Gh {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI ('gh') is required to sync milestones and issues."
    }
}

function Invoke-GhJson {
    param(
        [string[]]$Arguments
    )

    $output = & gh @Arguments
    if ([string]::IsNullOrWhiteSpace($output)) {
        return $null
    }
    return $output | ConvertFrom-Json
}

function Resolve-Repo {
    param(
        [string]$ExplicitRepo
    )

    if ($ExplicitRepo) {
        return $ExplicitRepo
    }

    $repoInfo = Invoke-GhJson -Arguments @("repo", "view", "--json", "nameWithOwner")
    if (-not $repoInfo.nameWithOwner) {
        throw "Unable to resolve the current GitHub repository. Pass -Repo owner/name."
    }
    return $repoInfo.nameWithOwner
}

function Get-ExistingMilestones {
    param(
        [string]$ResolvedRepo
    )

    $items = Invoke-GhJson -Arguments @("api", "--paginate", "repos/$ResolvedRepo/milestones?state=all&per_page=100")
    $lookup = @{}
    foreach ($item in @($items)) {
        $lookup[$item.title] = $item
    }
    return $lookup
}

function Get-ExistingIssues {
    param(
        [string]$ResolvedRepo
    )

    $items = Invoke-GhJson -Arguments @("issue", "list", "--repo", $ResolvedRepo, "--limit", "500", "--state", "all", "--json", "title,number")
    $lookup = @{}
    foreach ($item in @($items)) {
        $lookup[$item.title] = $item
    }
    return $lookup
}

function Ensure-Labels {
    param(
        [string]$ResolvedRepo,
        [string[]]$Labels
    )

    foreach ($label in $Labels | Sort-Object -Unique) {
        if (-not $label) {
            continue
        }
        $existing = & gh label list --repo $ResolvedRepo --limit 500 --json name --jq ".[].name" 2>$null
        if ($existing -and ($existing -split "`r?`n" | Where-Object { $_ -eq $label })) {
            continue
        }
        & gh label create $label --repo $ResolvedRepo --color BFD4F2 --description "Created by Telchines backlog sync" | Out-Null
    }
}

function Ensure-Milestone {
    param(
        [string]$ResolvedRepo,
        [hashtable]$ExistingMilestones,
        [pscustomobject]$Milestone,
        [bool]$DryRun
    )

    if ($ExistingMilestones.ContainsKey($Milestone.title)) {
        Write-Host "Milestone exists: $($Milestone.title)"
        return $ExistingMilestones[$Milestone.title]
    }

    if ($DryRun) {
        Write-Host "[WhatIf] Create milestone: $($Milestone.title)"
        return [pscustomobject]@{ title = $Milestone.title; number = -1 }
    }

    $created = Invoke-GhJson -Arguments @(
        "api",
        "repos/$ResolvedRepo/milestones",
        "--method", "POST",
        "-f", "title=$($Milestone.title)",
        "-f", "description=$($Milestone.description)"
    )
    Write-Host "Created milestone: $($Milestone.title)"
    $ExistingMilestones[$Milestone.title] = $created
    return $created
}

function Ensure-Issue {
    param(
        [string]$ResolvedRepo,
        [hashtable]$ExistingIssues,
        [pscustomobject]$Issue,
        [string]$MilestoneTitle,
        [bool]$DryRun
    )

    if ($ExistingIssues.ContainsKey($Issue.title)) {
        Write-Host "Issue exists: $($Issue.title)"
        return
    }

    $labels = @($Issue.labels | Where-Object { $_ })
    if ($DryRun) {
        Write-Host "[WhatIf] Create issue: $($Issue.title)"
        return
    }

    $args = @(
        "issue", "create",
        "--repo", $ResolvedRepo,
        "--title", $Issue.title,
        "--body", $Issue.body
    )

    if ($MilestoneTitle) {
        $args += @("--milestone", $MilestoneTitle)
    }

    foreach ($label in $labels) {
        $args += @("--label", $label)
    }

    & gh @args | Out-Null
    Write-Host "Created issue: $($Issue.title)"
    $ExistingIssues[$Issue.title] = [pscustomobject]@{ title = $Issue.title }
}

Require-Gh

$resolvedRepo = Resolve-Repo -ExplicitRepo $Repo
$backlogFile = Resolve-Path $BacklogPath
$backlog = Get-Content $backlogFile -Raw | ConvertFrom-Json

$allLabels = @()
foreach ($milestone in $backlog.milestones) {
    foreach ($issue in $milestone.issues) {
        $allLabels += @($issue.labels)
    }
}

if (-not $WhatIf) {
    Ensure-Labels -ResolvedRepo $resolvedRepo -Labels $allLabels
}

$existingMilestones = Get-ExistingMilestones -ResolvedRepo $resolvedRepo
$existingIssues = Get-ExistingIssues -ResolvedRepo $resolvedRepo

foreach ($milestone in $backlog.milestones) {
    $createdMilestone = Ensure-Milestone -ResolvedRepo $resolvedRepo -ExistingMilestones $existingMilestones -Milestone $milestone -DryRun $WhatIf.IsPresent
    foreach ($issue in $milestone.issues) {
        Ensure-Issue -ResolvedRepo $resolvedRepo -ExistingIssues $existingIssues -Issue $issue -MilestoneTitle $createdMilestone.title -DryRun $WhatIf.IsPresent
    }
}

Write-Host "Backlog sync complete for $resolvedRepo"
