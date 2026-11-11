#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
自动打包脚本 for USBTMC 仪器控制器
功能：
1. 检查并安装必要的 Python 包
2. 使用 PyInstaller 打包为独立 EXE
3. 可选生成 Inno Setup 安装包
4. Wincom 回读超时与后台日志使用现有 PyQt5/PyVISA 依赖和 Python 标准库
"""

import ast
from importlib.machinery import EXTENSION_SUFFIXES
import os
import sys
import subprocess
import shutil
import platform
from pathlib import Path

# ---------- 配置区域 ----------
PROJECT_DIR = Path(__file__).resolve().parent
SCRIPT_NAME = "usbtmc_gui_ctrl.py"            # 主脚本名称
LIBS_DIR = "Libs"                            # 存放 .pyd 等依赖的文件夹
WINCOM_PYD = "pywincom.cp313-win_amd64.pyd"
CONFIG_FILE = "dmm_config.json"               # 配置文件（可选，需与主程序一致）
OUTPUT_DIR = "dist"                           # 打包输出目录

# PyInstaller 参数（可根据需要修改）
PYINSTALLER_ARGS = [
    "--onefile",             # 单文件模式，若需文件夹模式改为 "--onedir"
    "--windowed",            # PyQt GUI 不弹控制台窗口
    "--noconfirm",
    "--clean",
    "--name", "USBTMC_Controller",
    "--distpath", OUTPUT_DIR,
    "--add-data", f"{LIBS_DIR}{os.pathsep}{LIBS_DIR}",  # 打入 pywincom 等自定义 .pyd
    "--hidden-import", "PyQt5.sip",
    "--hidden-import", "pyvisa_py",
    "--hidden-import", "serial",
    "--hidden-import", "usb",
    "--hidden-import", "pyvisa.resources",
    "--collect-all", "PyQt5",
    "--collect-all", "pyvisa_py",
    "--exclude-module", "tkinter",
    SCRIPT_NAME
]

# Inno Setup 配置
INNO_SETUP_SCRIPT = "setup.iss"                # 生成的 Inno Setup 脚本名
INNO_SETUP_OUTPUT = "new_installer"                 # 安装包输出目录
# ---------------------------------

def check_platform():
    """检查是否为 Windows 系统（打包目标为 Windows）"""
    if platform.system() != "Windows":
        print("警告：打包脚本主要为 Windows 设计，当前系统为 {}".format(platform.system()))
        response = input("是否继续？(y/n): ").strip().lower()
        if response != 'y':
            sys.exit(1)

def check_files():
    """检查必要的文件是否存在"""
    if not (PROJECT_DIR / SCRIPT_NAME).is_file():
        print(f"错误：找不到主脚本 {SCRIPT_NAME}")
        return False
    libs_dir = PROJECT_DIR / LIBS_DIR
    if not libs_dir.is_dir():
        print(f"错误：找不到依赖库文件夹 {LIBS_DIR}")
        return False
    if not (libs_dir / WINCOM_PYD).is_file():
        print(f"警告：未找到 {LIBS_DIR}/{WINCOM_PYD}，生成的程序无法启用 Wincom")
    elif WINCOM_PYD.removeprefix("pywincom") not in EXTENSION_SUFFIXES:
        print(f"错误：{WINCOM_PYD} 需要 Windows x64 CPython 3.13，请使用该解释器打包。")
        return False
    return True

def read_app_version():
    """从主脚本读取版本，避免为生成安装包加载 PyQt5 或 Wincom。"""
    source = PROJECT_DIR / SCRIPT_NAME
    for statement in ast.parse(source.read_text(encoding="utf-8"), filename=str(source)).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str) and value:
                return value
            break
    raise ValueError(f"{SCRIPT_NAME} 缺少有效的 APP_VERSION")

def install_dependencies():
    """安装所需的 Python 包（如果尚未安装）。"""
    # (pip 包名, import 模块名)；避免 pyserial/pyusb/PyInstaller 的模块名判断错误。
    required_packages = [
        ("PyQt5", "PyQt5"),
        ("pyvisa", "pyvisa"),
        ("pyvisa-py", "pyvisa_py"),
        ("pyinstaller", "PyInstaller"),
        ("pyusb", "usb"),
        ("pyserial", "serial"),
    ]
    print("检查并安装依赖包...")
    for pip_name, import_name in required_packages:
        try:
            __import__(import_name)
            print(f"  {pip_name} 已安装")
        except ImportError:
            print(f"  正在安装 {pip_name}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])

def run_pyinstaller():
    """执行 PyInstaller 打包"""
    print("\n开始 PyInstaller 打包...")
    cmd = [sys.executable, "-m", "PyInstaller"] + PYINSTALLER_ARGS
    print("执行命令: " + " ".join(cmd))
    try:
        subprocess.check_call(cmd, cwd=PROJECT_DIR)
    except subprocess.CalledProcessError as e:
        print(f"PyInstaller 打包失败: {e}")
        sys.exit(1)

    # 在 dist 目录启动便携版时，程序可读取同目录下的配置。
    config_path = PROJECT_DIR / CONFIG_FILE
    if config_path.is_file():
        shutil.copy2(config_path, PROJECT_DIR / OUTPUT_DIR / CONFIG_FILE)
        print(f"已复制 {CONFIG_FILE} 到 {OUTPUT_DIR}")

    print(f"打包完成！输出文件位于 {OUTPUT_DIR} 文件夹中。")

def generate_inno_setup():
    """生成 Inno Setup 脚本并尝试编译"""
    print("\n准备生成 Inno Setup 安装包...")

    # 检查 iscc 编译器是否在 PATH 中
    iscc_path = shutil.which("iscc")
    if not iscc_path:
        # 尝试默认安装路径
        possible_paths = [
            r"C:\Program Files (x86)\Inno Setup 6\iscc.exe",
            r"C:\Program Files\Inno Setup 6\iscc.exe",
        ]
        for p in possible_paths:
            if Path(p).exists():
                iscc_path = p
                break
    if not iscc_path:
        print("未找到 Inno Setup 编译器 (iscc.exe)。")
        print("请先安装 Inno Setup (https://jrsoftware.org/isinfo.php) 并确保 iscc.exe 在 PATH 中。")
        return False

    # 检查打包输出文件是否存在
    exe_name = "USBTMC_Controller.exe"
    exe_path = PROJECT_DIR / OUTPUT_DIR / exe_name
    if not exe_path.exists():
        print(f"错误：找不到打包后的 EXE 文件 {exe_path}")
        return False

    version = read_app_version()
    iss_content = f"""[Setup]
AppName=USB 仪器控制器
AppVersion={version}
DefaultDirName={{pf}}\\USBTMC_Controller
DefaultGroupName=USB 仪器控制器
UninstallDisplayIcon={{app}}\\{exe_name}
Compression=lzma2
SolidCompression=yes
OutputDir={INNO_SETUP_OUTPUT}
OutputBaseFilename=USBTMC_Controller_Setup

[Files]
Source: "{OUTPUT_DIR}\\{exe_name}"; DestDir: "{{app}}"; Flags: ignoreversion
"""
    if (PROJECT_DIR / CONFIG_FILE).is_file():
        iss_content += f'Source: "{CONFIG_FILE}"; DestDir: "{{app}}"; Flags: onlyifdoesntexist\n'
    iss_content += f'Source: "{LIBS_DIR}\\*"; DestDir: "{{app}}\\{LIBS_DIR}"; Flags: ignoreversion recursesubdirs\n'

    iss_content += """
[Icons]
Name: "{group}\\USB 仪器控制器"; Filename: "{app}\\%s"; WorkingDir: "{app}"
Name: "{group}\\卸载"; Filename: "{uninstallexe}"
Name: "{commondesktop}\\USB 仪器控制器"; Filename: "{app}\\%s"; WorkingDir: "{app}"

[Run]
Filename: "{app}\\%s"; WorkingDir: "{app}"; Description: "运行 USB 仪器控制器"; Flags: postinstall nowait skipifsilent
""" % (exe_name, exe_name, exe_name)

    # 写入 .iss 文件
    (PROJECT_DIR / INNO_SETUP_SCRIPT).write_text(iss_content, encoding="utf-8-sig")
    print(f"已生成 Inno Setup 脚本: {INNO_SETUP_SCRIPT}")

    # 编译安装包
    print("正在编译安装包...")
    try:
        subprocess.check_call([iscc_path, INNO_SETUP_SCRIPT], cwd=PROJECT_DIR)
    except subprocess.CalledProcessError as e:
        print(f"编译安装包失败: {e}")
        return False

    print(f"安装包生成成功！位于 {INNO_SETUP_OUTPUT} 文件夹中。")
    return True

def main():
    check_platform()
    if not check_files():
        sys.exit(1)

    # 步骤1：安装依赖
    install_dependencies()

    # 步骤2：PyInstaller 打包
    run_pyinstaller()

    # 步骤3：询问是否生成安装包
    print("\n" + "="*50)
    answer = input("是否生成 Inno Setup 安装包？(y/n, 默认 n): ").strip().lower()
    if answer == 'y':
        if not generate_inno_setup():
            sys.exit(1)
    else:
        print("跳过安装包生成。")

    print("\n所有操作完成！")

if __name__ == "__main__":
    main()
