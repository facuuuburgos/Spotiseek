; Script de Inno Setup. Abrelo con Inno Setup y pulsa "Compile" (F9).
; Se instala SOLO para el usuario actual (no pide permisos de administrador)
; en %LOCALAPPDATA%\Programs, asi la app puede escribir descargas, logs y config.
[Setup]
AppName=Spotify Soulseek
AppVersion=1.0
DefaultDirName={autopf}\SpotifySoulseek
DefaultGroupName=Spotify Soulseek
PrivilegesRequired=lowest
OutputDir=salida
OutputBaseFilename=Instalador_SpotifySoulseek
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\SpotifySoulseek.exe
CloseApplications=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "icono"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
; Todo lo de dist\SpotifySoulseek. NUNCA se incluye slskd.yml (cada usuario crea el suyo
; al abrir la app por primera vez), ni datos, ni la carpeta "actualizacion".
Source: "..\dist\SpotifySoulseek\*"; DestDir: "{app}"; Excludes: "slskd.yml,slskd.yml.bak,*.log,actualizacion,actualizacion\*,actualizacion_anterior\*,actualizacion_fallida\*,downloads\*"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Spotify Soulseek"; Filename: "{app}\SpotifySoulseek.exe"
Name: "{autodesktop}\Spotify Soulseek"; Filename: "{app}\SpotifySoulseek.exe"; Tasks: icono

[Run]
Filename: "{app}\SpotifySoulseek.exe"; Description: "Abrir Spotify Soulseek"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM slskd.exe"; Flags: runhidden; RunOnceId: "MatarSlskd"
