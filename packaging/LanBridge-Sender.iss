#define MyAppName "邻桥快传-发送端"
#define MyAppVersion "1.0.2.1"
#define MyAppPublisher "LanBridge Team"
#define MyAppExeName "邻桥快传-发送端.exe"

[Setup]
AppId={code:AppIdentity}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\LanBridge\Sender
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UsePreviousLanguage=no
OutputDir=..\dist
OutputBaseFilename=邻桥快传-发送端-安装包
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
MinVersion=6.1sp1
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
SetupIconFile=..\assets\sender.ico
VersionInfoDescription={#MyAppName}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName=邻桥快传
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}

[Languages]
Name: "chinesesimplified"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "..\docs\使用说明.md"; DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\卸载{#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动{#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function AppIdentity(Param: String): String;
begin
  Result := '{A6F0F8B2-52F1-4A27-A84D-4BFE07E8C6D1}';
  { Isolate automated install/uninstall checks from an existing user install. }
  if ExpandConstant('{param:LANBRIDGEVERIFY|0}') = '1' then
    Result := Result + '-Verification';
end;
