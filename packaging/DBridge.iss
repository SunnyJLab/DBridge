; Inno Setup script for the DBridge Windows installer.
;
;   iscc /DAppVersion=0.3.0 packaging\DBridge.iss
;
; Expects the PyInstaller one-folder build in dist\DBridge (see DBridge.spec).
; Installs per user, without administrator rights: DBridge writes config.json,
; logs\ and backup\ next to its exe, so the install folder must stay writable.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=x.y.z packaging\DBridge.iss
#endif

#define AppName "DBridge"
#define AppExe "DBridge.exe"
#define AppUrl "https://github.com/SunnyJLab/DBridge"

[Setup]
AppId={{CD72A8E4-38D2-483D-83DF-87CA6B83F8C7}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Sunny J Lab
AppCopyright=Copyright (c) 2026 Sunny J Lab
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=DBridge-{#AppVersion}-win-x64-setup
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Drop the previous build's runtime so stale modules do not linger after an upgrade.
; config.json, logs\, backup\ and relationships.d\ are left untouched.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\DBridge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

; Uninstall removes only the files installed above. User data written at run time
; (config.json, logs\, backup\, relationships.d\) stays in the folder on purpose.
