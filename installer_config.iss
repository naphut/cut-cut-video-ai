; ==============================================================================
; Inno Setup Script for Video AI Studio (Windows Professional Installer)
; Builds standalone Video_AI_Setup_v1.0.exe installer with Desktop Icon & Uninstaller
; ==============================================================================

#define MyAppName "Video AI Studio"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Khmer AI Studio"
#define MyAppExeName "Video_AI.exe"

[Setup]
; Basic Application Info
AppId={{D839E4F2-3B71-4A59-8802-99071DEAC38F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; Output Configuration
OutputDir=Output
OutputBaseFilename=Video_AI_Setup_v1.0
SetupIconFile=assets\app_icon.ico
UninstallDisplayIcon={app}\assets\app_icon.ico

; Ultra Compression (CapCut / Discord style)
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Privileges & Windows Integration
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible

; UI Polish
WizardStyle=modern
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; Copy all compiled binaries and assets from dist\Video_AI directory
Source: "dist\Video_AI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\app_icon.ico"
Name: "{autoprograms}\{#MyAppName}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\assets\app_icon.ico"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
