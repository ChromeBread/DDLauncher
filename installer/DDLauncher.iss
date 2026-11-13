; Inno Setup 6.6+ script. Build the folder version first, then:
;   .venv\Scripts\pyinstaller DDLauncher.spec
;   iscc /DAppVersion=0.2.0 installer\DDLauncher.iss
; Installs for the current user only (no administrator rights), like the settings in %APPDATA%\CBApps\DDLauncher.
; Uninstall first offers the safe uninstaller (DDLauncher-Uninstall.exe) to export / remove the settings,
; then removes the program folder. The settings are never removed without asking.

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef StageDir
  #define StageDir "..\dist\DDLauncher"
#endif

[Setup]
AppId={{54D52A2C-A707-4EF5-A375-AED0DF6953C2}
AppName=DDLauncher
AppVersion={#AppVersion}
AppPublisher=ChromeBread feat. BlackDream
AppPublisherURL=https://github.com/ChromeBread/DDLauncher
AppSupportURL=https://github.com/ChromeBread/DDLauncher/issues
DefaultDirName={localappdata}\Programs\DDLauncher
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
OutputDir=..\dist\installer
OutputBaseFilename=DDLauncher-{#AppVersion}-windows-x64-setup
UninstallDisplayIcon={app}\DDLauncher.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; DDLauncher keeps running in the tray; close it before files are replaced
CloseApplications=yes
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "ja"; MessagesFile: "compiler:Languages\Japanese.isl"
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "ko"; MessagesFile: "compiler:Languages\Korean.isl"

[CustomMessages]
en.SafeUninstall=Do you want to open the safe uninstaller first?%n%nIt lets you export your DDLauncher settings (to move them to another PC) and choose whether to move them to the Recycle Bin. If you choose No, your settings stay on this PC.
ja.SafeUninstall=先に安全アンインストーラーを開きますか？%n%nDDLauncher の設定をエクスポート（別の PC への引っ越し用）し、設定をゴミ箱へ移すかどうかを選べます。「いいえ」を選ぶと、設定はこの PC に残ります。
fr.SafeUninstall=Ouvrir d'abord le désinstallateur sûr ?%n%nIl permet d'exporter vos réglages DDLauncher (pour les déplacer vers un autre PC) et de choisir s'ils vont à la Corbeille. Si vous répondez Non, vos réglages restent sur ce PC.
ko.SafeUninstall=먼저 안전 제거 프로그램을 열까요?%n%nDDLauncher 설정을 내보내고(다른 PC로 옮기기용) 설정을 휴지통으로 옮길지 고를 수 있습니다. "아니요"를 고르면 설정은 이 PC에 남습니다.
en.DesktopIcon=Create a desktop shortcut
ja.DesktopIcon=デスクトップにショートカットを作る
fr.DesktopIcon=Créer un raccourci sur le Bureau
ko.DesktopIcon=바탕 화면에 바로 가기 만들기

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"; Flags: unchecked

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD-PARTY-LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\DDLauncher"; Filename: "{app}\DDLauncher.exe"
Name: "{autodesktop}\DDLauncher"; Filename: "{app}\DDLauncher.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\DDLauncher.exe"; Description: "{cm:LaunchProgram,DDLauncher}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeUninstall(): Boolean;
var
  Code: Integer;
begin
  Result := True;
  if (not UninstallSilent) and FileExists(ExpandConstant('{app}\DDLauncher-Uninstall.exe')) and
     (MsgBox(CustomMessage('SafeUninstall'), mbConfirmation, MB_YESNO) = IDYES) then
    Exec(ExpandConstant('{app}\DDLauncher-Uninstall.exe'), '', ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cmd: String;
begin
  { Windows startup entry made by DDLauncher: remove it only when it points into this install folder }
  if (CurUninstallStep = usUninstall) and
     RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'DDLauncher', Cmd) and
     (Pos(Lowercase(ExpandConstant('{app}')), Lowercase(Cmd)) > 0) then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'DDLauncher');
end;
