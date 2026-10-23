; DocStudio installer (Inno Setup 7)
; 先构建 PyInstaller onedir 包，再运行：
;   ISCC.exe installer\DocStudio.iss

#define AppName "DocStudio"
#define AppVersion "1.0.0"
#define AppPublisher "Winkless213"
#define AppURL "https://github.com/Winkless213/doc-studio"
#define AppExe "DocStudio.exe"
#define AppSrc "..\dist\DocStudio"

[Setup]
AppId={{3A8F0C41-9E52-4D6B-9C0E-6F1B2E7A4D38}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion=1.0.0.0
VersionInfoDescription={#AppName} installer
VersionInfoProductName={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\work\release\installer
OutputBaseFilename=DocStudio-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: checkedonce

[Files]
Source: "{#AppSrc}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\DESIGN.md"; DestDir: "{app}\docs"; Flags: ignoreversion
Source: "..\PLAN_一体化整合.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\PLAN_阶段3_结构化入KB.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.md"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent; Check: ShouldLaunchApp

[Code]
var
  SelfTestPassed: Boolean;

function ShouldLaunchApp: Boolean;
begin
  Result := SelfTestPassed;
end;

function RuntimeFilesPresent: Boolean;
begin
  Result :=
    FileExists(ExpandConstant('{app}\{#AppExe}')) and
    FileExists(ExpandConstant('{app}\_internal\static\desktop.html')) and
    FileExists(ExpandConstant('{app}\_internal\glossary.json'));
end;

function WebView2Present: Boolean;
begin
  { Win10/11 自带 Edge WebView2 Runtime；企业精简系统可能缺失 }
  Result :=
    DirExists(ExpandConstant('{pf32}\Microsoft\EdgeWebView\Application')) or
    DirExists(ExpandConstant('{pf}\Microsoft\EdgeWebView\Application'));
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('DocStudio 需要 64 位 Windows 10 或更高版本。', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  if not RuntimeFilesPresent then
  begin
    MsgBox('安装文件不完整：缺少桌面页或术语库。' + #13#10 +
           '请重新下载安装包。', mbError, MB_OK);
    Exit;
  end;

  if Exec(ExpandConstant('{app}\{#AppExe}'), '--self-test',
          ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) and
     (ResultCode = 0) then
  begin
    SelfTestPassed := True;
  end
  else
  begin
    MsgBox('安装完成，但启动自检失败。' + #13#10 +
           '请查看 %TEMP%\DocStudio-self-test.log，或到 GitHub Issues 反馈。',
           mbError, MB_OK);
  end;

  if not WebView2Present then
    MsgBox('未检测到 Microsoft Edge WebView2 运行时。' + #13#10 +
           '桌面窗口依赖它；如窗口打不开，请从 https://developer.microsoft.com/microsoft-edge/webview2/ 安装后重试。',
           mbInformation, MB_OK);
end;
