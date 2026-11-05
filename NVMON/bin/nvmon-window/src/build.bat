@echo off
REM Rebuild nvmon-window.exe with the C# compiler that ships with Windows
REM (.NET Framework 4). No SDK, no Visual Studio. Output goes one level up,
REM next to the WebView2 DLLs it references.
setlocal
set "HERE=%~dp0"
set "OUT=%HERE%.."
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo csc.exe not found at %CSC%
    exit /b 1
)
"%CSC%" /nologo /target:winexe /platform:x64 /optimize+ ^
    /out:"%OUT%\nvmon-window.exe" ^
    /r:"%OUT%\Microsoft.Web.WebView2.Core.dll" ^
    /r:"%OUT%\Microsoft.Web.WebView2.WinForms.dll" ^
    /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
    "%HERE%NvmonWindow.cs"
exit /b %errorlevel%
