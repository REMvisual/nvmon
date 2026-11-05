# dump_nvwmi.ps1 — Dump every NVWMI class + properties on this box.
#
# Run as (from inside the installed NVMON\ tree):
#     powershell -NoProfile -ExecutionPolicy Bypass -File bin\dump_nvwmi.ps1
#
# Outputs to stdout. Pipe to a file if you want to send it back:
#     powershell -NoProfile -ExecutionPolicy Bypass -File bin\dump_nvwmi.ps1 > nvwmi.txt
#
# This is read-only. It lists every class under the NVIDIA WMI namespace,
# then dumps the first row of each class as a property list. If the
# namespace doesn't exist on this driver version, we say so loudly so we
# know not to rely on NVWMI as a source.

$ErrorActionPreference = 'Continue'
$Namespace = 'ROOT\CIMV2\NV'

# Probe the namespace itself first.
try {
    $null = Get-CimClass -Namespace $Namespace -ErrorAction Stop | Out-Null
    Write-Host "[OK] NVWMI namespace exists: $Namespace"
    Write-Host ""
} catch {
    Write-Host "[MISSING] NVWMI namespace not found: $Namespace"
    Write-Host "          Reason: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "On this driver version NVWMI is unavailable. Use the NVCP"
    Write-Host "View System Topology -> Save Logfile JSON instead."
    exit 0
}

# Enumerate all classes in the namespace.
try {
    $classes = Get-CimClass -Namespace $Namespace -ErrorAction Stop |
        Select-Object -ExpandProperty CimClassName |
        Sort-Object
    Write-Host ("Found {0} class(es):" -f $classes.Count)
    foreach ($name in $classes) {
        Write-Host ("    {0}" -f $name)
    }
    Write-Host ""
} catch {
    Write-Host "[ERR] Could not enumerate classes: $($_.Exception.Message)"
    exit 1
}

# For each class, dump the first row's properties.
foreach ($name in $classes) {
    Write-Host "================================================================"
    Write-Host ("CLASS: {0}" -f $name)
    Write-Host "================================================================"
    try {
        $rows = Get-CimInstance -Namespace $Namespace -ClassName $name -ErrorAction Stop
        if ($null -eq $rows) {
            Write-Host "    (no rows)"
            Write-Host ""
            continue
        }
        # Format-List on the first row gives us every property name + value.
        $first = @($rows)[0]
        $first |
            Select-Object -Property * -ExcludeProperty PSComputerName,CimClass,CimInstanceProperties,CimSystemProperties |
            Format-List
    } catch {
        Write-Host ("    [skip] {0}" -f $_.Exception.Message)
    }
    Write-Host ""
}
