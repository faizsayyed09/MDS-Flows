$Root = "C:\Projects\MDS Flows"
$OutputRoot = Join-Path $Root "custom_components"

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

Write-Host ""
Write-Host "========================================"
Write-Host " MDS JSON Component Extractor"
Write-Host "========================================"
Write-Host ""

$ExtractedCount = 0

$FlowFolders = @(
    "CEQ Audit",
    "My Flows",
    "Inactive flows"
)

foreach ($FlowFolder in $FlowFolders)
{
    $CurrentFolder = Join-Path $Root $FlowFolder

    if (-not (Test-Path $CurrentFolder))
    {
        continue
    }

    Get-ChildItem $CurrentFolder -Filter *.json | ForEach-Object {

        $FlowFile = $_
        $FlowName = $FlowFile.BaseName

        Write-Host ""
        Write-Host "Processing: $FlowFolder\$FlowName"

        try
        {
            $Json = Get-Content $FlowFile.FullName -Raw | ConvertFrom-Json
        }
        catch
        {
            Write-Warning "Unable to parse JSON: $($FlowFile.Name)"
            continue
        }

        $FlowOutputFolder = Join-Path `
            (Join-Path $OutputRoot $FlowFolder) `
            $FlowName

        New-Item `
            -ItemType Directory `
            -Force `
            -Path $FlowOutputFolder | Out-Null

        foreach ($Node in $Json.data.nodes)
        {
            try
            {
                if ($null -eq $Node.data) { continue }
                if ($null -eq $Node.data.node) { continue }
                if ($null -eq $Node.data.node.metadata) { continue }

                $Module = $Node.data.node.metadata.module

                if ([string]::IsNullOrWhiteSpace($Module))
                {
                    continue
                }

                if ($Module -notlike "custom_components.*")
                {
                    continue
                }

                if ($null -eq $Node.data.node.template) { continue }
                if ($null -eq $Node.data.node.template.code) { continue }
                if ($null -eq $Node.data.node.template.code.value) { continue }

                $ComponentName = $Module.Replace(
                    "custom_components.",
                    ""
                )

                $CodeLines = @(
                    $Node.data.node.template.code.value
                )

                $Code = $CodeLines -join "`r`n"

                # ==================================================
                # SANITIZATION
                # ==================================================

                # Matcha Keys
                $Code = $Code -replace `
                    'mk_live_[A-Za-z0-9_\-]+',
                    'MATCHA_API_KEY_HERE'

                $Code = $Code -replace `
                    'mk_test_[A-Za-z0-9_\-]+',
                    'MATCHA_API_KEY_HERE'

                # MDS Keys
                $Code = $Code -replace `
                    '(?i)mds_key\s*=\s*"[^"]+"',
                    'mds_key = "MDS_KEY"'

                # Manual Tokens
                $Code = $Code -replace `
                    '(?i)manual_token\s*=\s*"[^"]+"',
                    'manual_token = "GENESYS_MANUAL_TOKEN_HERE"'

                # Access Tokens
                $Code = $Code -replace `
                    '(?i)access_token\s*=\s*"[^"]+"',
                    'access_token = "ACCESS_TOKEN_HERE"'

                # Refresh Tokens
                $Code = $Code -replace `
                    '(?i)refresh_token\s*=\s*"[^"]+"',
                    'refresh_token = "REFRESH_TOKEN_HERE"'

                # OAuth Client Secret
                $Code = $Code -replace `
                    '(?i)client_secret\s*=\s*"[^"]+"',
                    'client_secret = "CLIENT_SECRET_HERE"'

                # Generic API Key
                $Code = $Code -replace `
                    '(?i)api_key\s*=\s*"[^"]+"',
                    'api_key = "API_KEY_HERE"'

                # Matcha API Key Variables
                $Code = $Code -replace `
                    '(?i)matcha_api_key\s*=\s*"[^"]+"',
                    'matcha_api_key = "MATCHA_API_KEY_HERE"'

                # Flow API Key
                $Code = $Code -replace `
                    '(?i)flow_api_key\s*=\s*"[^"]+"',
                    'flow_api_key = "FLOW_API_KEY_HERE"'

                # Bearer Tokens
                $Code = $Code -replace `
                    '(?i)Bearer\s+[A-Za-z0-9\-\._]+',
                    'Bearer BEARER_TOKEN_HERE'

                $OutputFile = Join-Path `
                    $FlowOutputFolder `
                    ($ComponentName + ".py")

                Set-Content `
                    -Path $OutputFile `
                    -Value $Code `
                    -Encoding UTF8

                $LineCount = (
                    Get-Content $OutputFile
                ).Count

                Write-Host `
                    "  Extracted: $ComponentName.py ($LineCount lines)"

                $ExtractedCount++
            }
            catch
            {
                Write-Warning "Component extraction failed."
                Write-Warning $_.Exception.Message
            }
        }
    }
}

Write-Host ""
Write-Host "========================================"
Write-Host " COMPLETE"
Write-Host "========================================"
Write-Host ""
Write-Host "Components Extracted: $ExtractedCount"
Write-Host "Output Folder:"
Write-Host $OutputRoot