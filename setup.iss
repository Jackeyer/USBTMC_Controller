[Setup]
AppName=USB 仪器控制器
AppVersion=2.0.1
DefaultDirName={pf}\USBTMC_Controller
DefaultGroupName=USB 仪器控制器
UninstallDisplayIcon={app}\USBTMC_Controller.exe
Compression=lzma2
SolidCompression=yes
OutputDir=new_installer
OutputBaseFilename=USBTMC_Controller_Setup

[Files]
Source: "dist\USBTMC_Controller.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dmm_config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "Libs\*"; DestDir: "{app}\Libs"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\USB 仪器控制器"; Filename: "{app}\USBTMC_Controller.exe"; WorkingDir: "{app}"
Name: "{group}\卸载"; Filename: "{uninstallexe}"
Name: "{commondesktop}\USB 仪器控制器"; Filename: "{app}\USBTMC_Controller.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\USBTMC_Controller.exe"; WorkingDir: "{app}"; Description: "运行 USB 仪器控制器"; Flags: postinstall nowait skipifsilent
