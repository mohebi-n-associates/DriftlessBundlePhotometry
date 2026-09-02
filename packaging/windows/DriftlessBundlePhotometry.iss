; Inno Setup recipe. Values are supplied by the release workflow with /D switches.

#ifndef AppVersion
  #error AppVersion must be provided, for example /DAppVersion=0.2.1
#endif
#ifndef SourceDir
  #error SourceDir must point to the PyInstaller onedir output
#endif
#ifndef OutputDir
  #error OutputDir must point to the release artifact directory
#endif
#ifndef IconFile
  #error IconFile must point to the generated Windows icon
#endif

#define AppName "Driftless Bundle Photometry"
#define AppExeName "DriftlessBundlePhotometry.exe"

[Setup]
AppId={{E03D0CD9-CA39-4B57-9BB2-77626F96C84C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Driftless Bundle Photometry contributors
AppPublisherURL=https://github.com/mohebi-n-associates/DriftlessBundlePhotometry
AppSupportURL=https://github.com/mohebi-n-associates/DriftlessBundlePhotometry/issues
AppUpdatesURL=https://github.com/mohebi-n-associates/DriftlessBundlePhotometry/releases
DefaultDirName={localappdata}\Programs\Driftless Bundle Photometry
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=Driftless-Bundle-Photometry-{#AppVersion}-Windows-x64-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
ChangesAssociations=no
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{userdocs}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{userdocs}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
