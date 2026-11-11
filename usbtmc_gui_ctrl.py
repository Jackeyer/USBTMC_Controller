import os
import pyvisa
import time
import sys
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import math
import tempfile
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from itertools import count
from pathlib import Path
from typing import Optional, Dict, Any, Union, List, Tuple
import queue
import threading
import concurrent.futures
import importlib.util

# PyQt5 GUI
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError:
    print("错误: 需要 PyQt5 支持，请安装: pip install PyQt5")
    sys.exit(1)

# 全局变量（用于线程间通信）
dmm_ready_event = threading.Event()
command_queue = queue.Queue()
dmm_dispatch_lock = threading.Lock()
DMM_QUEUE_TIMEOUT = 5.0
APP_VERSION = "2.0.1"
WINCOM_READ_TIMEOUT_VALUE = 99999
WINCOM_READ_TIMEOUT_MAX_MS = 60_000
dmm_request_ids = count(1)
gui_request_ids = count(1)
log_trace = ContextVar("log_trace", default="-")
# 后台 Wincom 线程通过该队列把测量结果安全地交给 Qt 主线程显示
measurement_ui_queue = queue.Queue()
# 所有后台日志/异步任务都通过队列回到 Qt 主线程，避免跨线程操作控件。
gui_action_queue = queue.Queue()
stop_event = threading.Event()
wincom_stop_event = threading.Event()   # 专门控制 wincom 线程停止

# Wincom 直接访问当前 GUI 中的 DG832 控制实例。
# 按测试机协议要求，DG832 func=7~12 会直接写在 wincom_worker() 主循环中处理。
dg832_controller = None

visa_manager = None
visa_manager_lock = threading.Lock()


def get_visa_manager():
    """在应用生命周期内共享 VISA 管理器，仪器只负责各自的会话。"""
    global visa_manager
    with visa_manager_lock:
        if visa_manager is None:
            visa_manager = pyvisa.ResourceManager()
        return visa_manager


def close_visa_manager():
    global visa_manager
    with visa_manager_lock:
        if visa_manager is not None:
            try:
                visa_manager.close()
            finally:
                visa_manager = None

# 导入 pywincom 接入测试机系统（如果存在）
def get_runtime_base_dir() -> str:
    """
    获取程序运行根目录。

    普通 Python 运行时返回脚本所在目录；
    PyInstaller --onefile 打包后返回临时解包目录，确保 --add-data 打入的 Libs 可以被找到。
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


base_dir = get_runtime_base_dir()
pyd_path = os.path.join(base_dir, "Libs", "pywincom.cp313-win_amd64.pyd")
wincom = None
try:
    spec = importlib.util.spec_from_file_location("pywincom", pyd_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wincom = module
    wincom_load_error = None
except Exception as exc:
    wincom_load_error = str(exc)

# 配置日志系统
class GuiQueueLogHandler(logging.Handler):
    """界面日志缓存。普通日志和告警各自保留最近的记录。"""

    def __init__(self):
        super().__init__()
        self._normal = deque(maxlen=1000)
        self._warnings = deque(maxlen=250)
        self._dropped = 0
        self._sequence = count()

    def emit(self, record):
        buffer = self._warnings if record.levelno >= logging.WARNING else self._normal
        self._dropped += int(len(buffer) == buffer.maxlen)
        buffer.append((next(self._sequence), self.format(record)))

    def drain(self, limit=100):
        with self.lock:
            batch = [self._warnings.popleft() for _ in range(min(limit, len(self._warnings)))]
            batch.extend(self._normal.popleft() for _ in range(min(limit - len(batch), len(self._normal))))
            dropped, self._dropped = self._dropped, 0
        lines = [line for _, line in sorted(batch)]
        if dropped:
            lines.insert(0, f"界面日志已省略 {dropped} 条较早记录，可查看文件日志。")
        return lines


def _prune_logs(log_dir):
    cutoff = time.time() - 14 * 24 * 60 * 60
    try:
        for path in log_dir.glob("usbtmc_*.log*"):
            _, _, suffix = path.name.partition(".log")
            if suffix and not (suffix.startswith(".") and suffix[1:].isdigit()):
                continue
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                logging.getLogger(__name__).warning("无法清理旧日志 %s: %s", path, exc)
    except OSError as exc:
        logging.getLogger(__name__).warning("无法扫描旧日志 %s: %s", log_dir, exc)


def _attach_log_trace(record):
    record.trace = log_trace.get()
    return True


@contextmanager
def setup_logging():
    """仪器线程投递日志，后台线程负责写文件和格式化界面日志。"""
    gui_handler = GuiQueueLogHandler()
    sinks = [gui_handler]
    if sys.stderr is not None:
        sinks.append(logging.StreamHandler())

    log_dir = None
    path_errors = []
    local_root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for root in (local_root, Path(tempfile.gettempdir())):
        candidate = root / "USBTMC_Controller" / "logs"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            log_file = candidate / f"usbtmc_{timestamp}_{os.getpid()}.log"
            sinks.append(RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=4, encoding="utf-8"))
        except OSError as exc:
            path_errors.append(f"{candidate}: {exc}")
        else:
            log_dir = candidate
            break

    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s %(trace)s] %(message)s")
    for sink in sinks:
        sink.setFormatter(formatter)
    records = queue.SimpleQueue()
    queue_handler = QueueHandler(records)
    queue_handler.addFilter(_attach_log_trace)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(queue_handler)
    listener = QueueListener(records, *sinks)
    listener.start()
    try:
        logger = logging.getLogger(__name__)
        for error in path_errors:
            logger.warning("日志目录不可用: %s", error)
        if log_dir is not None:
            _prune_logs(log_dir)
        yield log_dir, gui_handler
    finally:
        root_logger.removeHandler(queue_handler)
        listener.stop()
        queue_handler.close()
        for sink in sinks:
            sink.close()
        root_logger.setLevel(previous_level)

# Wincom/SCPI 参数解析工具
def normalize_range_value(range_val: Any = "AUTO") -> str:
    """
    将 Wincom/GUI 下发的量程参数统一转换为 Keysight SCPI 可接受的字符串。

    兼容：
      - "AUTO" / "" / None：自动量程
      - int / float：固定量程，例如 10.0 -> "10"
      - <=0、NaN、inf：按自动量程处理，避免向仪器发送非法量程
    """
    if range_val is None:
        return "AUTO"

    if isinstance(range_val, str):
        text = range_val.strip()
        if not text:
            return "AUTO"
        if text.upper() == "AUTO":
            return "AUTO"
        try:
            value = float(text)
        except ValueError:
            return text
    else:
        try:
            value = float(range_val)
        except (TypeError, ValueError):
            return str(range_val)

    if not math.isfinite(value) or value <= 0:
        return "AUTO"
    return f"{value:.15g}"


def normalize_nplc_value(nplc: Any = None, default: float = 1.0) -> float:
    """
    将 NPLC 转换为 float；空值或非法值使用默认值。
    """
    if nplc is None:
        return float(default)
    if isinstance(nplc, str) and not nplc.strip():
        return float(default)
    try:
        value = float(nplc)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value <= 0:
        return float(default)
    return value


def get_task_param_obj(task: Dict[str, Any]) -> Any:
    """
    兼容不同 Wincom 字段名：
      - params：推荐字段名
      - parmas：兼容用户现有描述中的拼写
      - param：兼容可能的旧字段名
    """
    for key in ("params", "parmas", "param"):
        if key in task:
            return task.get(key)
    return None


def parse_wincom_range_nplc(task: Dict[str, Any],
                            default_range: Any = "AUTO",
                            default_nplc: float = 1.0) -> Dict[str, Any]:
    """
    从 wincom task 的参数中解析 range_val 和 nplc。

    支持格式：
      1) {'params': [range_val, nplc]}
      2) {'params': {'range_val': range_val, 'nplc': nplc}}
      3) {'parmas': [range_val, nplc]}  # 兼容拼写
    其中 range_val/nplc 原始值可以是 float。
    """
    raw_params = get_task_param_obj(task)
    range_raw = default_range
    nplc_raw = default_nplc

    if isinstance(raw_params, dict):
        range_raw = raw_params.get("range_val",
                    raw_params.get("range",
                    raw_params.get("rangeValue", default_range)))
        nplc_raw = raw_params.get("nplc",
                   raw_params.get("NPLC", default_nplc))
    elif isinstance(raw_params, (list, tuple)):
        if len(raw_params) >= 1:
            range_raw = raw_params[0]
        if len(raw_params) >= 2:
            nplc_raw = raw_params[1]
    elif raw_params is not None:
        # 只有一个裸值时按量程处理，NPLC 保持默认，避免误判。
        range_raw = raw_params

    return {
        "range_val": normalize_range_value(range_raw),
        "nplc": normalize_nplc_value(nplc_raw, default_nplc)
    }



# 配置文件管理器
class ConfigManager:
    """配置文件管理器"""
    def __init__(self, config_file="config.json"):
        self.config_file = Path(config_file)
        self.logger = logging.getLogger(__name__)
        self.config = self.load_config()

    def load_config(self) -> Dict[str, Any]:
        default_config = {
            "instruments": [
                {
                    "name": "Keysight_34461A",
                    "vid": "0x2A8D",
                    "pid": "0x1301",
                    "serial": None,
                    "description": "Keysight 34461A 6.5位数字万用表"
                }
            ],
            "measurement": {
                "default_timeout": 5000,
                "default_nplc": 1,
                "log_level": "INFO",
                "auto_connect": True
            },
            "measurement_modes": {
                "dcv": {"range": "AUTO", "nplc": 1},
                "acv": {"range": "AUTO", "bandwidth": "DEF"},
                "dci": {"range": "AUTO", "nplc": 1},
                "aci": {"range": "AUTO"},
                "res": {"range": "AUTO", "nplc": 10},
                "freq": {"range": "AUTO"}
            }
        }
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                config = self._merge_configs(default_config, user_config)
                self.logger.info(f"配置文件加载成功: {self.config_file}")
                return config
            except Exception as e:
                self.logger.error(f"配置文件加载失败，使用默认配置: {e}")
                return default_config
        else:
            self._save_config(default_config)
            self.logger.info(f"创建默认配置文件: {self.config_file}")
            return default_config

    def _merge_configs(self, default: Dict, user: Dict) -> Dict:
        merged = default.copy()
        for key, value in user.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self._merge_configs(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _save_config(self, config: Dict):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")

    def get_instrument_config(self, name: str = None) -> Optional[Dict]:
        if name:
            for instrument in self.config.get("instruments", []):
                if instrument.get("name") == name:
                    return instrument
            self.logger.warning(f"未找到仪器配置: {name}")
            return None
        instruments = self.config.get("instruments", [])
        return instruments[0] if instruments else None

    def update_instrument_config(self, name: str, **kwargs):
        for instrument in self.config.get("instruments", []):
            if instrument.get("name") == name:
                instrument.update(kwargs)
                self._save_config(self.config)
                self.logger.info(f"仪器配置已更新: {name}")
                return True
        self.logger.error(f"未找到仪器: {name}")
        return False

# 仪器控制类
class Keysight34461A:
    """支持兼容 SCPI 指令的数字万用表控制器。"""
    # ... 新增 connect_resource 和 disconnect ...
    def __init__(self, config_manager: ConfigManager = None, instrument_name: str = None):
        self.logger = logging.getLogger(__name__)
        self.config_manager = config_manager or ConfigManager()
        self.instrument_config = self.config_manager.get_instrument_config(instrument_name)
        if not self.instrument_config:
            raise ValueError("未找到仪器配置")
        self.vid = self.instrument_config.get("vid", "0x2A8D")
        self.pid = self.instrument_config.get("pid", "0x1301")
        self.serial = self.instrument_config.get("serial")
        self.instrument_name = self.instrument_config.get("name", "Unknown")
        self.preferred_resource = self.instrument_config.get("preferred_resource")
        self.last_connected_resource = None
        self.instrument = None
        self.measurement_config = self.config_manager.config.get("measurement", {})
        self.mode_config = self.config_manager.config.get("measurement_modes", {})
        # 高阻模式默认关闭：DCV 使用固定 10 MΩ 输入阻抗。
        # *RST/CONF 会改变仪器内部状态，因此由 configure_* 在配置完成后重新应用这里的期望状态。
        self.high_impedance_enabled = False
        self.external_trigger_enabled = False
        self.external_trigger_slope = "NEG"
        # 记录当前由 CONF 选中的测量功能。高阻只对 DCV/Ratio 有意义，
        # 手动切换高阻时若当前不是 DCV，需要先切到 CONF:VOLT:DC。
        self.current_measurement_mode = None
        self.logger.info(f"初始化仪器: {self.instrument_name}")

    def _safe_query_instrument(self, resource_str: str, timeout: int = 1000) -> Optional[str]:
        """
        安全地查询仪器信息（带超时）
        
        参数:
            resource_str: VISA资源字符串
            timeout: 超时时间(毫秒)
            
        返回:
            Optional[str]: 设备ID字符串，失败返回None
        """
        try:
            with get_visa_manager().open_resource(resource_str) as instr:
                instr.timeout = timeout
                return instr.query("*IDN?").strip()
        except Exception as e:
            self.logger.debug(f"查询设备失败: {resource_str} - {e}")
            return None

    def _find_matching_resources(self) -> List[Dict[str, Any]]:
        """
        查找所有匹配的VISA资源（优化版，使用线程池避免阻塞）
        
        返回:
            List[Dict]: 匹配的资源列表，每个元素包含资源字符串和设备信息
        """
        matching_resources = []
        try:
            all_resources = get_visa_manager().list_resources()
            self.logger.info(f"发现 {len(all_resources)} 个VISA资源")
            if not all_resources:
                return matching_resources
            usb_resources = [r for r in all_resources if 'USB' in r.upper()]
            if not usb_resources:
                self.logger.info("未发现USB设备")
                return matching_resources
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_resource = {executor.submit(self._safe_query_instrument, res): res for res in usb_resources}
                for future in concurrent.futures.as_completed(future_to_resource):
                    resource = future_to_resource[future]
                    try:
                        idn = future.result(timeout=2)
                        resource_info = self._parse_resource_string(resource)
                        vid_match = self._normalize_vid_pid(resource_info.get('vid', '')) == self._normalize_vid_pid(self.vid)
                        pid_match = self._normalize_vid_pid(resource_info.get('pid', '')) == self._normalize_vid_pid(self.pid)
                        if idn and ((not self.vid and not self.pid) or (vid_match and pid_match)):
                            idn_parts = idn.split(',')
                            manufacturer = idn_parts[0].strip() if len(idn_parts) > 0 else ''
                            model = idn_parts[1].strip() if len(idn_parts) > 1 else ''
                            serial = idn_parts[2].strip() if len(idn_parts) > 2 else ''
                            firmware = idn_parts[3].strip() if len(idn_parts) > 3 else ''
                            device_info = {
                                'resource': resource,
                                'idn': idn,
                                'manufacturer': manufacturer,
                                'model': model,
                                'serial': serial,
                                'firmware': firmware,
                                'vid': resource_info.get('vid', ''),
                                'pid': resource_info.get('pid', ''),
                                'description': f"{manufacturer} {model} (SN: {serial})"
                            }
                            matching_resources.append(device_info)
                            self.logger.debug(f"发现设备: {resource} - {idn}")
                    except Exception as e:
                        self.logger.debug(f"处理设备查询结果出错: {resource} - {e}")
            return matching_resources
        except Exception as e:
            self.logger.error(f"查找匹配资源时出错: {e}", exc_info=True)
            return []

    def _parse_resource_string(self, resource_str: str) -> Dict[str, str]:
        """
        解析VISA资源字符串
        
        参数:
            resource_str: VISA资源字符串
            
        返回:
            Dict: 解析后的资源信息
        """
        info = {'resource': resource_str, 'vid': '', 'pid': '', 'serial': '', 'interface': ''}
        try:
            if '::' in resource_str:
                parts = resource_str.split('::')
                if len(parts) >= 5 and parts[0].upper().startswith('USB'):
                    info['vid'] = parts[1] if len(parts) > 1 else ''
                    info['pid'] = parts[2] if len(parts) > 2 else ''
                    info['serial'] = parts[3] if len(parts) > 3 else ''
                    info['interface'] = parts[4] if len(parts) > 4 else ''
        except Exception as e:
            self.logger.warning(f"解析资源字符串失败: {resource_str} - {e}")
        return info

    def _normalize_vid_pid(self, vid_pid: str) -> str:
        """
        标准化VID/PID字符串（移除0x前缀，转换为大写）
        
        参数:
            vid_pid: VID或PID字符串
            
        返回:
            str: 标准化后的字符串
        """
        if not vid_pid:
            return ''
        vid_pid = vid_pid.upper()
        if vid_pid.startswith('0X'):
            vid_pid = vid_pid[2:]
        return vid_pid

    def _select_device_interactive(self, matching_resources: List[Dict]) -> Optional[str]:
        """
        交互式选择设备       gui中不一定用
        
        参数:
            matching_resources: 匹配的设备列表
            
        返回:
            Optional[str]: 选择的资源字符串
        """
        if not matching_resources:
            return None
        
        print(f"\n发现 {len(matching_resources)} 个匹配的设备:")
        for i, device in enumerate(matching_resources):
            idn = device.get('idn', '未知')
            serial = device.get('serial', '未知')
            model = device.get('model', '未知')
            print(f"  [{i}] {device['resource']}")
            print(f"      型号: {model}")
            print(f"      序列号: {serial}")
            if idn and idn != '无法查询' and idn != '查询超时':
                print(f"      设备ID: {idn}")
            print()
        
        # 优先使用配置中指定的资源
        if self.preferred_resource:
            for device in matching_resources:
                if device['resource'] == self.preferred_resource:
                    print(f"使用配置的优先资源: {self.preferred_resource}")
                    return self.preferred_resource
        
        # 优先使用之前成功连接的资源
        if self.last_connected_resource:
            for device in matching_resources:
                if device['resource'] == self.last_connected_resource:
                    print(f"使用上次成功连接的资源: {self.last_connected_resource}")
                    return self.last_connected_resource
        
        # 如果只有一个设备，自动选择
        if len(matching_resources) == 1:
            print(f"自动选择唯一设备: {matching_resources[0]['resource']}")
            return matching_resources[0]['resource']
        
        # 如果有序列号配置，尝试匹配
        if self.serial:
            for device in matching_resources:
                device_serial = device.get('serial', '')
                if device_serial == self.serial:
                    print(f"根据序列号匹配设备: {device['resource']}")
                    return device['resource']
        
        # 多个设备，让用户选择
        while True:
            try:
                selection = input(f"请选择设备 (0-{len(matching_resources)-1}) 或输入 'q' 退出: ")
                
                if selection.lower() == 'q':
                    return None
                
                idx = int(selection)
                if 0 <= idx < len(matching_resources):
                    selected_resource = matching_resources[idx]['resource']
                    print(f"已选择: {selected_resource}")
                    
                    # 询问是否保存为优先资源
                    save = input("是否保存此设备为优先连接设备? (y/n): ")
                    if save.lower() == 'y':
                        self.config_manager.update_instrument_config(
                            self.instrument_name,
                            preferred_resource=selected_resource
                        )
                        print(f"已保存 {selected_resource} 为优先连接设备")
                    
                    return selected_resource
                else:
                    print(f"无效选择，请输入 0-{len(matching_resources)-1} 之间的数字")
            except ValueError:
                print("无效输入，请输入数字或 'q'")
            except KeyboardInterrupt:
                print("\n用户中断选择")
                return None

    def connect(self, timeout: int = None, interactive: bool = True) -> bool:
        matching_resources = self._find_matching_resources()
        if not matching_resources:
            raise RuntimeError("未找到匹配的 DMM")
        if interactive and len(matching_resources) > 1:
            resource = self._select_device_interactive(matching_resources)
            if not resource:
                raise RuntimeError("未选择 DMM 资源")
        else:
            resource = matching_resources[0]["resource"]
        self.connect_resource(resource, timeout)
        return True

    def connect_resource(self, resource: str, timeout: int = None) -> str:
        """确认所选 VISA 资源可以通信，完成初始化后再提交会话。"""
        if timeout is None:
            timeout = self.measurement_config.get("default_timeout", 5000)
        if self.instrument is not None:
            raise RuntimeError("请先断开当前 DMM")

        instrument = get_visa_manager().open_resource(resource)
        try:
            instrument.timeout = int(timeout)
            self.logger.debug("[DMM] resource=%s SCPI QUERY >> *IDN?", resource)
            idn = instrument.query("*IDN?").strip()
            if not idn:
                raise RuntimeError(f"VISA 资源 {resource} 的 *IDN? 响应为空")
            self._reset_instrument(instrument)
        except Exception:
            try:
                instrument.close()
            except Exception:
                self.logger.exception("关闭初始化失败的 DMM 会话时出错: %s", resource)
            raise

        self.instrument = instrument
        self.last_connected_resource = resource
        self.logger.info("DMM 已连接 %s | %s", resource, idn)
        return idn

    @staticmethod
    def _parse_on_off(value: Any, name: str = "enable") -> bool:
        """兼容 Wincom 的 0/1、bool 以及常见字符串开关值。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().upper()
            if text in ("1", "ON", "TRUE", "ENABLE", "ENABLED"):
                return True
            if text in ("0", "OFF", "FALSE", "DISABLE", "DISABLED"):
                return False
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{name} 仅支持 0/1 或 ON/OFF，当前: {value}")
        if numeric not in (0, 1):
            raise ValueError(f"{name} 仅支持 0/1，当前: {value}")
        return bool(numeric)

    @staticmethod
    def _parse_trigger_slope(value: Any) -> str:
        """外触发沿：0/NEG=下降沿，1/POS=上升沿。"""
        if value is None:
            return "NEG"
        if isinstance(value, str):
            text = value.strip().upper()
            if text in ("NEG", "NEGATIVE", "FALL", "FALLING", "0"):
                return "NEG"
            if text in ("POS", "POSITIVE", "RISE", "RISING", "1"):
                return "POS"
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"trigger slope 仅支持 0/1、NEG/POS，当前: {value}")
        if numeric == 0:
            return "NEG"
        if numeric == 1:
            return "POS"
        raise ValueError(f"trigger slope 仅支持 0/1，当前: {value}")

    def _apply_high_impedance(self, ensure_dcv: bool = False) -> None:
        """
        将当前高阻期望状态写入仪器。

        高阻仅用于 DC 电压/Ratio。若 ``ensure_dcv`` 为 True 且当前测量功能
        不是 DCV，则先发送 ``CONF:VOLT:DC``，再发送 ``INP:IMP:AUTO``。

        注意：``configure_dcv()`` 已经先执行 CONF 并配置量程/NPLC，因此它调用
        本函数时必须传 ensure_dcv=False，避免再次 CONF 把量程/NPLC 恢复默认值。
        """
        if not self.instrument:
            raise RuntimeError("仪器未连接")

        if ensure_dcv and self.current_measurement_mode != "DCV":
            self.logger.info(
                "设置高阻前当前模式=%s，先执行 CONF:VOLT:DC",
                self.current_measurement_mode or "UNKNOWN"
            )
            self._write("CONF:VOLT:DC")
            self.current_measurement_mode = "DCV"

        state = "ON" if self.high_impedance_enabled else "OFF"
        self._write(f"INP:IMP:AUTO {state}")
        self.logger.debug("DMM 输入阻抗命令: INP:IMP:AUTO %s", state)

    def _apply_trigger_config(self, ensure_measurement_mode: bool = False) -> None:
        """
        应用触发配置。

        Keysight DMM 的触发系统应在测量功能已经由 CONF 指令选定后配置。
        - configure_dcv()/configure_dci()/... 已经先执行了 CONF，因此调用本函数时
          使用 ensure_measurement_mode=False，避免再次 CONF 把量程/NPLC 恢复默认。
        - GUI/Wincom 独立切换外触发时使用 ensure_measurement_mode=True；如果当前
          尚未配置任何测量功能，则先默认执行 CONF:VOLT:DC，再设置触发。
        - 如果当前已经是 DCV/ACV/DCI/ACI/RES/FREQ，则说明此前已经执行过对应 CONF，
          此时不重复 CONF，直接修改 TRIG:SOUR / TRIG:SLOP。
        """
        if not self.instrument:
            raise RuntimeError("仪器未连接")

        if ensure_measurement_mode and self.current_measurement_mode is None:
            self.logger.info("设置触发前尚未配置测量模式，先执行 CONF:VOLT:DC")
            self._write("CONF:VOLT:DC")
            self.current_measurement_mode = "DCV"
            # CONF 会恢复 DCV 输入阻抗默认值，因此按应用层状态重新应用高阻。
            self._apply_high_impedance(ensure_dcv=False)

        if self.external_trigger_enabled:
            self._write("TRIG:SOUR EXT")
            self._write(f"TRIG:SLOP {self.external_trigger_slope}")
        else:
            self._write("TRIG:SOUR IMM")

    def set_high_impedance(self, enabled: Any) -> Dict[str, Any]:
        """
        设置 DCV 自动高阻模式。
        enabled=0 -> 固定 10 MΩ；
        enabled=1 -> AUTO，高阻在 100 mV/1 V/10 V 量程为 >10 GΩ。
        """
        if not self.instrument:
            raise RuntimeError("仪器未连接")
        self.high_impedance_enabled = self._parse_on_off(enabled, "high_impedance")
        # 高阻命令只能在 DCV/Ratio 功能下使用。手动/Wincom 独立切换高阻时，
        # 如果当前不是 DCV，先用 CONF:VOLT:DC 选择电压测量功能。
        self._apply_high_impedance(ensure_dcv=True)
        # CONF 会把触发系统恢复默认，因此如上一步发生了功能切换，这里恢复
        # 当前应用层期望的 IMM/EXT 触发状态；已经处于 DCV 时重复写入也无害。
        self._apply_trigger_config(ensure_measurement_mode=False)
        state = "HI-Z AUTO" if self.high_impedance_enabled else "10 MOhm"
        self.logger.info("DMM 高阻模式设置: %s", state)
        return {"enabled": self.high_impedance_enabled, "mode": state}

    def set_external_trigger(self, enabled: Any, slope: Any = None) -> Dict[str, Any]:
        """
        设置外部触发。
        enabled=0 -> TRIG:SOUR IMM；
        enabled=1 -> TRIG:SOUR EXT。
        slope 可选：0/NEG=下降沿，1/POS=上升沿；省略时保留上一次设置。
        """
        if not self.instrument:
            raise RuntimeError("仪器未连接")
        enable_flag = self._parse_on_off(enabled, "external_trigger")
        if slope is not None:
            self.external_trigger_slope = self._parse_trigger_slope(slope)
        self.external_trigger_enabled = enable_flag
        # 外触发与高阻一样，必须在 CONF 已选定测量功能后设置。
        # 若当前尚未配置任何测量功能，则默认先进入 DCV；已有模式时不重复 CONF，
        # 避免破坏已经设置好的量程/NPLC。
        self._apply_trigger_config(ensure_measurement_mode=True)
        source = "EXT" if self.external_trigger_enabled else "IMM"
        self.logger.info("DMM 触发设置: source=%s, slope=%s", source, self.external_trigger_slope)
        return {
            "enabled": self.external_trigger_enabled,
            "source": source,
            "slope": self.external_trigger_slope,
        }

    def _reset_instrument(self, instrument):
        self.current_measurement_mode = None
        for command in ("*CLS", "*RST"):
            self.logger.debug("[DMM] SCPI WRITE >> %s", command)
            instrument.write(command)
        time.sleep(1)
        self.high_impedance_enabled = False
        self.external_trigger_enabled = False
        self.external_trigger_slope = "NEG"
        self.logger.info("DMM 复位完成，高阻=OFF，触发源=IMM")

    def reset(self):
        self._reset_instrument(self.instrument)

    def configure(self, mode: str, range_val=None, nplc=None) -> None:
        mode_map = {
            'dcv': self.configure_dcv,
            'acv': self.configure_acv,
            'dci': self.configure_dci,
            'aci': self.configure_aci,
            'res': self.configure_resistance,
            'freq': self.configure_frequency
        }
        if mode not in mode_map:
            raise ValueError(f"不支持的测量模式: {mode}")
        defaults = self.mode_config.get(mode, {})
        params = {"range_val": defaults.get("range", "AUTO") if range_val is None else range_val}
        if mode in ("dcv", "dci", "res"):
            default_nplc = 10 if mode == "res" else self.measurement_config.get("default_nplc", 1)
            params["nplc"] = defaults.get("nplc", default_nplc) if nplc is None else nplc
        elif mode == "acv":
            params["bandwidth"] = defaults.get("bandwidth", "DEF")
        mode_map[mode](**params)

    def configure_dcv(self, range_val: Union[str, int, float] = "AUTO", nplc: float = None):
        range_cmd = normalize_range_value(range_val)
        if nplc is None:
            nplc = self.measurement_config.get("default_nplc", 1)
        nplc = normalize_nplc_value(nplc, self.measurement_config.get("default_nplc", 1))
        try:
            self._write("CONF:VOLT:DC")
            self.current_measurement_mode = "DCV"
            if range_cmd.upper() == "AUTO":
                self._write("VOLT:DC:RANG:AUTO ON")
            else:
                self._write(f"VOLT:DC:RANG {range_cmd}")
            self._write(f"VOLT:DC:NPLC {nplc}")
            # CONF 会自动关闭高阻并重置触发系统，这里按当前应用状态恢复。
            self._apply_high_impedance(ensure_dcv=False)
            self._apply_trigger_config(ensure_measurement_mode=False)
            self.logger.debug(
                f"配置为直流电压测量，量程: {range_cmd}, NPLC: {nplc}, "
                f"HighZ: {self.high_impedance_enabled}, "
                f"Trigger: {'EXT' if self.external_trigger_enabled else 'IMM'}"
            )
        except Exception as e:
            self.logger.error(f"DCV配置失败: {e}")
            raise

    def configure_acv(self, range_val: Union[str, int, float] = "AUTO", bandwidth: str = "DEF"):
        range_cmd = normalize_range_value(range_val)
        try:
            self._write("CONF:VOLT:AC")
            self.current_measurement_mode = "ACV"
            if range_cmd.upper() == "AUTO":
                self._write("VOLT:AC:RANG:AUTO ON")
            else:
                self._write(f"VOLT:AC:RANG {range_cmd}")
            self._write(f"VOLT:AC:BAND {bandwidth}")
            self._apply_trigger_config(ensure_measurement_mode=False)
            self.logger.debug("配置为交流电压测量，量程: %s，带宽: %s", range_cmd, bandwidth)
        except Exception as e:
            self.logger.error(f"ACV配置失败: {e}")
            raise

    def configure_dci(self, range_val: Union[str, int, float] = "AUTO", nplc: float = None):
        range_cmd = normalize_range_value(range_val)
        if nplc is None:
            nplc = self.measurement_config.get("default_nplc", 1)
        nplc = normalize_nplc_value(nplc, self.measurement_config.get("default_nplc", 1))
        try:
            self._write("CONF:CURR:DC")
            self.current_measurement_mode = "DCI"
            if range_cmd.upper() == "AUTO":
                self._write("CURR:DC:RANG:AUTO ON")
            else:
                self._write(f"CURR:DC:RANG {range_cmd}")
            self._write(f"CURR:DC:NPLC {nplc}")
            self._apply_trigger_config(ensure_measurement_mode=False)
            self.logger.debug("配置为直流电流测量，量程: %s，NPLC: %s", range_cmd, nplc)
        except Exception as e:
            self.logger.error(f"DCI配置失败: {e}")
            raise

    def configure_aci(self, range_val: Union[str, int, float] = "AUTO"):
        range_cmd = normalize_range_value(range_val)
        try:
            self._write("CONF:CURR:AC")
            self.current_measurement_mode = "ACI"
            if range_cmd.upper() == "AUTO":
                self._write("CURR:AC:RANG:AUTO ON")
            else:
                self._write(f"CURR:AC:RANG {range_cmd}")
            self._apply_trigger_config(ensure_measurement_mode=False)
            self.logger.debug("配置为交流电流测量，量程: %s", range_cmd)
        except Exception as e:
            self.logger.error(f"ACI配置失败: {e}")
            raise

    def configure_resistance(self, range_val: Union[str, int, float] = "AUTO", nplc: float = 10):
        range_cmd = normalize_range_value(range_val)
        nplc = normalize_nplc_value(nplc, 10)
        try:
            self._write("CONF:RES")
            self.current_measurement_mode = "RES"
            if range_cmd.upper() == "AUTO":
                self._write("RES:RANG:AUTO ON")
            else:
                self._write(f"RES:RANG {range_cmd}")
            self._write(f"RES:NPLC {nplc}")
            self._apply_trigger_config(ensure_measurement_mode=False)
            self.logger.debug("配置为电阻测量，量程: %s，NPLC: %s", range_cmd, nplc)
        except Exception as e:
            self.logger.error(f"电阻测量配置失败: {e}")
            raise

    def configure_frequency(self, range_val: Union[str, int, float] = "AUTO"):
        range_cmd = normalize_range_value(range_val)
        try:
            self._write("CONF:FREQ")
            self.current_measurement_mode = "FREQ"
            if range_cmd.upper() == "AUTO":
                self._write("FREQ:RANG:AUTO ON")
            else:
                self._write(f"FREQ:VOLT:RANG {range_cmd}")
            self._apply_trigger_config(ensure_measurement_mode=False)
            self.logger.debug("配置为频率测量，量程: %s", range_cmd)
        except Exception as e:
            self.logger.error(f"频率测量配置失败: {e}")
            raise

    def _write(self, command: str):
        self.logger.debug("[DMM] SCPI WRITE >> %.500r", command)
        return self.instrument.write(command)

    def _query(self, command: str) -> str:
        self.logger.debug("[DMM] SCPI QUERY >> %.500r", command)
        response = self.instrument.query(command)
        self.logger.debug("[DMM] SCPI RESPONSE << %.500r", response)
        return response

    def single_measurement(self) -> float:
        return float(self._query("READ?").strip())

    def continuous_measurement(self, interval: float = 1.0, count: int = 10) -> List[Dict]:
        measurements = []
        try:
            self.logger.info(f"开始连续测量，间隔: {interval}秒，次数: {count}")
            for i in range(count):
                try:
                    value = self.single_measurement()
                except Exception as exc:
                    self.logger.warning("连续测量第 %d 次读取失败: %s", i + 1, exc)
                    value = None
                timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                if value is not None:
                    measurement = {
                        'index': i + 1,
                        'timestamp': timestamp,
                        'value': value,
                        'instrument': self.instrument_name
                    }
                    measurements.append(measurement)
                    self.logger.debug("测量 %d/%d: %s - %s", i + 1, count, timestamp, value)
                if i < count - 1:
                    time.sleep(interval)
            self.logger.info(f"连续测量完成，共{len(measurements)}个有效数据")
            return measurements
        except Exception as e:
            self.logger.error(f"连续测量失败: {e}")
            return measurements

    def set_display_text(self, text: str):
        try:
            if len(text) > 12:
                text = text[:12]
            self._write(f'DISP:TEXT "{text}"')
            self.logger.info(f"前面板显示: {text}")
        except Exception as e:
            self.logger.error(f"设置显示文本失败: {e}")

    def clear_display_text(self):
        try:
            self._write("DISP:TEXT:CLEAR")
            self.logger.info("已清除前面板显示文本")
        except Exception as e:
            self.logger.error(f"清除显示文本失败: {e}")

    def get_error(self) -> Optional[str]:
        try:
            error = self._query("SYST:ERR?").strip()
            return error if error else None
        except Exception as e:
            self.logger.error(f"获取错误失败: {e}")
            return None

    def check_errors(self):
        errors = []
        while True:
            error = self.get_error()
            if error and "+0," not in error:
                errors.append(error)
                self.logger.warning(f"仪器错误: {error}")
            else:
                break
        return errors

    def disconnect(self):
        instrument = self.instrument
        if instrument is None:
            return
        try:
            self.clear_display_text()
        finally:
            try:
                instrument.close()
            finally:
                self.instrument = None
                self.last_connected_resource = None
                self.current_measurement_mode = None
        self.logger.info("DMM 已断开")

# 通用 SCPI 仪器（信号发生器/示波器）
class GenericSCPIInstrument:
    """
    通用 VISA + SCPI 控制器。
    不绑定具体厂商型号，GUI 中所有快捷控制最终仍以 SCPI 文本发送。
    """
    def __init__(self, role: str):
        self.role = role
        self.logger = logging.getLogger(__name__)
        self.instrument = None
        self.resource = None
        self.idn = None
        self._lock = threading.RLock()

    @property
    def connected(self) -> bool:
        return self.instrument is not None

    def connect(self, resource: str, timeout: int = 5000) -> str:
        with self._lock:
            self.disconnect()
            instrument = get_visa_manager().open_resource(resource)
            try:
                instrument.timeout = int(timeout)
            except Exception:
                instrument.close()
                raise
            self.instrument = instrument
            self.resource = resource
            try:
                self.idn = self.instrument.query("*IDN?").strip()
            except Exception:
                # 极少数设备不支持 *IDN?，连接本身仍然保留。
                self.idn = "*IDN? 查询失败/设备未实现"
                self.logger.warning("[%s] *IDN? 查询失败: %s", self.role, resource, exc_info=True)
            self.logger.info("[%s] 已连接 %s | %s", self.role, resource, self.idn)
            return self.idn

    def write(self, command: str) -> int:
        command = command.strip()
        if not command:
            raise ValueError("SCPI 命令不能为空")
        with self._lock:
            if not self.instrument:
                raise RuntimeError(f"{self.role} 尚未连接")
            self.logger.debug("[%s] resource=%s SCPI WRITE >> %.500r", self.role, self.resource, command)
            result = self.instrument.write(command)
            return int(result) if result is not None else 0

    def write_sequence(self, commands) -> None:
        with self._lock:
            for command in commands:
                self.write(command)

    def query(self, command: str, *, log_response: bool = True) -> str:
        command = command.strip()
        if not command:
            raise ValueError("SCPI 查询命令不能为空")
        with self._lock:
            if not self.instrument:
                raise RuntimeError(f"{self.role} 尚未连接")
            self.logger.debug("[%s] resource=%s SCPI QUERY >> %.500r", self.role, self.resource, command)
            response = self.instrument.query(command).strip()
            if log_response:
                self.logger.debug("[%s] SCPI RESPONSE << %.500r (%d 字符)", self.role, response, len(response))
            else:
                self.logger.debug("[%s] SCPI RESPONSE << %d 字符", self.role, len(response))
            return response

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self.instrument:
                    self.instrument.close()
            finally:
                self.instrument = None
                if self.resource:
                    self.logger.info("[%s] 已断开 %s", self.role, self.resource)
                self.resource = None
                self.idn = None

# RIGOL DG832 专用控制器 -------------------------------------------------------
class DG832Instrument(GenericSCPIInstrument):
    """RIGOL DG832 双通道信号发生器控制器（VISA + SCPI）。"""

    # 与测试机 C 端 IsSine 定义保持完全一致：
    # 0-pulse, 1-sine, 2-square, 3-ramp, 4-noise, 5-dualtone, 6-harm, 7-DC
    WAVE_NAMES = {
        0: "PULSE",
        1: "SINE",
        2: "SQUARE",
        3: "RAMP",
        4: "NOISE",
        5: "DUALTONE",
        6: "HARM",
        7: "DC",
    }

    def __init__(self):
        super().__init__("信号发生器/DG832")

    @staticmethod
    def _channel(ch: Any) -> int:
        value = int(float(ch))
        if value not in (1, 2):
            raise ValueError(f"DG832 通道仅支持 1/2，当前: {ch}")
        return value

    @staticmethod
    def _number(value: Any, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是数字，当前: {value}")
        if not math.isfinite(number):
            raise ValueError(f"{name} 必须是有限数值，当前: {value}")
        return number

    def dg832_init(self) -> None:
        """对应 C 端 DG832_Init()."""
        self.logger.info("[DG832] Init")
        self.write_sequence(("*CLS", "*RST"))

    def dg832_sync_wave(self) -> None:
        """对应 C 端 DG832_SyncWave()：执行一次双通道同相位操作。"""
        self.logger.info("[DG832] SyncWave")
        self.write(":SOUR1:PHAS:SYNC")

    def dg832_set_phase(self, ch: Any, phase: Any) -> None:
        """对应 void DG832_SetPhase(uint8_t ch, int phase)."""
        channel = self._channel(ch)
        phase_value = self._number(phase, "phase")
        if not 0 <= phase_value <= 360:
            raise ValueError(f"phase 设置范围为 0~360°，当前: {phase_value}")
        self.logger.info("[DG832] SetPhase ch=%s phase=%s", channel, phase_value)
        self.write(f":SOUR{channel}:PHAS {phase_value:.12g}")

    def dg832_output_wave(self, ch: Any) -> None:
        """对应 DG832_OutputWave(ch)."""
        channel = self._channel(ch)
        self.logger.info("[DG832] OutputWave CH%s", channel)
        self.write(f":OUTP{channel} ON")

    def dg832_close_wave(self, ch: Any) -> None:
        """对应 DG832_CloseWave(ch)."""
        channel = self._channel(ch)
        self.logger.info("[DG832] CloseWave CH%s", channel)
        self.write(f":OUTP{channel} OFF")

    def dg832_set_wave(self,
                       ch: Any,
                       Freq: Any,
                       rms: Any,
                       AMP: Any,
                       dcyc: Any,
                       offset: Any,
                       IsSine: Any) -> List[str]:
        """
        对应测试机接口：
          DG832_set_wave(uint8_t ch, double Freq, uint8_t rms, double AMP,
                         int dcyc, double offset, WaveTypeDef IsSine)

        参数顺序严格保持 C 端定义：
          0 ch       : 1/2
          1 Freq     : 0~10 MHz
          2 rms      : 0=Vpp, 1=Vrms
          3 AMP      : 幅度数值（单位由 rms 决定）
          4 dcyc     : 1~100，仅 PULSE/SQUARE 使用；其它波形忽略
          5 offset   : Vdc
          6 IsSine   : 0-pulse,1-sine,2-square,3-ramp,4-noise,5-dualtone,6-harm,7-DC

        注意：phase 已不属于 SetWave；相位由 DG832_SetPhase(ch, phase) 单独设置。
        """
        channel = self._channel(ch)
        freq = self._number(Freq, "Freq")
        amp_value = self._number(AMP, "AMP")
        offset_value = self._number(offset, "offset")
        rms_flag = int(float(rms))
        wave_code = int(float(IsSine))

        if rms_flag not in (0, 1):
            raise ValueError(f"rms 仅支持 0/1，当前: {rms}")
        if wave_code not in self.WAVE_NAMES:
            raise ValueError(f"IsSine 仅支持 0~7，当前: {IsSine}")
        if not 0 <= freq <= 10_000_000:
            raise ValueError(f"Freq 设置范围为 0~10MHz，当前: {freq}")
        if not 0 <= amp_value <= 20:
            raise ValueError(f"AMP 设置范围为 0~20，当前: {amp_value}")

        amp_unit = "VRMS" if rms_flag else "VPP"
        commands: List[str] = []

        # DC 仅使用 offset；其它波形按 rms 选择 VPP/VRMS 幅度单位。
        if wave_code != 7:
            commands.append(f":SOUR{channel}:VOLT:UNIT {amp_unit}")

        freq_s = f"{freq:.12g}"
        amp_s = f"{amp_value:.12g}"
        offset_s = f"{offset_value:.12g}"

        # SetWave 不再携带 phase，因此 APPL 命令只下发该接口实际拥有的参数。
        if wave_code == 0:      # pulse
            commands.append(f":SOUR{channel}:APPL:PULS {freq_s},{amp_s},{offset_s}")
        elif wave_code == 1:    # sine
            commands.append(f":SOUR{channel}:APPL:SIN {freq_s},{amp_s},{offset_s}")
        elif wave_code == 2:    # square
            commands.append(f":SOUR{channel}:APPL:SQU {freq_s},{amp_s},{offset_s}")
        elif wave_code == 3:    # ramp
            commands.append(f":SOUR{channel}:APPL:RAMP {freq_s},{amp_s},{offset_s}")
        elif wave_code == 4:    # noise：手册中无频率参数
            commands.append(f":SOUR{channel}:APPL:NOIS {amp_s},{offset_s}")
        elif wave_code == 5:    # dualtone
            commands.append(f":SOUR{channel}:APPL:DUALT {freq_s},{amp_s},{offset_s}")
        elif wave_code == 6:    # harmonic
            commands.append(f":SOUR{channel}:APPL:HARM {freq_s},{amp_s},{offset_s}")
        elif wave_code == 7:    # DC：frequency/amplitude 仅作占位符
            commands.append(f":SOUR{channel}:APPL:DC DEF,DEF,{offset_s}")

        # 方波/脉冲才应用占空比。其它波形允许上位端传占位值，这里忽略。
        if wave_code in (0, 2):
            duty = self._number(dcyc, "dcyc")
            if not 1 <= duty <= 100:
                raise ValueError(f"PULSE/SQUARE 的 dcyc 设置范围为 1~100%，当前: {duty}")
            duty_s = f"{duty:.12g}"
            if wave_code == 0:
                if duty > 99.999:
                    self.logger.warning("[DG832] PULSE dcyc=100%%，按仪器 SCPI 上限 99.999%% 发送")
                    duty_s = "99.999"
                commands.append(f":SOUR{channel}:PULS:DCYC {duty_s}")
            else:
                commands.append(f":SOUR{channel}:FUNC:SQU:DCYC {duty_s}")

        self.logger.info(
            "[DG832] SetWave ch=%s freq=%sHz rms=%s(%s) amp=%s dcyc=%s offset=%sV IsSine=%s(%s)",
            channel, freq, rms_flag, amp_unit, amp_value, dcyc, offset_value,
            wave_code, self.WAVE_NAMES[wave_code]
        )
        self.write_sequence(commands)
        return commands


def _wincom_param_list(task: Dict[str, Any]) -> List[Any]:
    """将 Wincom task 的 params/parmas/param 统一转换为参数列表。"""
    raw = get_task_param_obj(task)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    # 某些上位端可能将单参数直接放成标量。
    return [raw]




# 简化版的设备管理器
class DeviceManager:
    """设备管理器，用于管理多台仪器"""
    def __init__(self, config_file="config.json"):
        self.config_manager = ConfigManager(config_file)
        self.logger = logging.getLogger(__name__)
        self.instruments = {}

    def quick_scan(self) -> List[Dict]:
        """
        快速扫描可用设备（不查询设备ID）
        
        返回:
            List[Dict]: 可用设备列表
        """
        try:
            resources = self.list_visa_resources()
            devices = []
            for resource in resources:
                if 'USB' in resource.upper():
                    parts = resource.split('::')
                    device_info = {
                        'resource': resource,
                        'vid': parts[1] if len(parts) > 1 else '',
                        'pid': parts[2] if len(parts) > 2 else '',
                        'serial': parts[3] if len(parts) > 3 else '',
                        'description': f"USB设备 ({resource})"
                    }
                    devices.append(device_info)
            return devices
        except Exception as e:
            self.logger.error(f"快速扫描设备时出错: {e}")
            return []

    def list_visa_resources(self) -> List[str]:
        """列出全部 VISA 资源（USB/TCPIP/GPIB/ASRL），供通用 SCPI 仪器选择。"""
        return list(get_visa_manager().list_resources())

    def create_instrument(self, instrument_name: str = None) -> Optional[Keysight34461A]:
        try:
            instrument = Keysight34461A(self.config_manager, instrument_name)
            self.instruments[instrument_name or instrument.instrument_name] = instrument
            return instrument
        except Exception as e:
            self.logger.error(f"创建仪器实例失败: {e}")
            return None

    def disconnect_all(self):
        for name, instrument in self.instruments.items():
            try:
                instrument.disconnect()
                self.logger.info(f"已断开仪器: {name}")
            except Exception as e:
                self.logger.error(f"断开仪器 {name} 时出错: {e}")

# 通信进程模拟器（修改，增加 connect_resource 和 disconnect 命令）
class DMMCommandHandler:
    def __init__(self, config_manager: ConfigManager, dmm: Keysight34461A = None):
        self.config_manager = config_manager
        self.dmm = dmm
        self.gui_measurement_config = None
        self.logger = logging.getLogger(__name__)
        self.command_map = {
            1: self._cmd_connect,          # 保留原无参连接（可能不再使用）
            2: self._cmd_disconnect,        # 断开连接
            3: self._cmd_configure_dcv,
            4: self._cmd_configure_acv,
            5: self._cmd_configure_dci,
            6: self._cmd_configure_aci,
            7: self._cmd_configure_res,
            8: self._cmd_configure_freq,
            9: self._cmd_single_measure,
            10: self._cmd_continuous_measure,
            11: self._cmd_set_display,
            12: self._cmd_clear_display,
            13: self._cmd_reset,
            14: self._cmd_get_errors,
            15: self._cmd_connect_resource,  # 连接指定资源
            16: self._cmd_set_high_impedance,
            17: self._cmd_set_external_trigger,
            18: self._cmd_gui_measurement,
        }

    def handle_command(self, function_code: int, **kwargs) -> Dict:
        operation = self.command_map.get(function_code)
        if operation is None:
            return {"success": False, "error": f"未知功能码: {function_code}"}
        if function_code not in (9, 10, 14, 18):
            self.gui_measurement_config = None
        try:
            if function_code not in (1, 2, 15) and (self.dmm is None or self.dmm.instrument is None):
                raise RuntimeError("DMM 尚未连接")
            return {"success": True, "data": operation(**kwargs)}
        except Exception as exc:
            if self.dmm is not None and function_code in (3, 4, 5, 6, 7, 8, 13, 16, 17):
                self.dmm.current_measurement_mode = None
            result = {
                "success": False,
                "error": str(exc),
                "connected": self.dmm is not None and self.dmm.instrument is not None,
            }
            visa_code = getattr(exc, "error_code", None)
            if visa_code is not None:
                result["visa_code"] = getattr(visa_code, "name", visa_code)
            return result

    def _cmd_connect(self, timeout=None):
        if self.dmm is not None:
            raise RuntimeError("请先断开当前 DMM")
        dmm = Keysight34461A(self.config_manager)
        dmm.connect(timeout, interactive=False)
        self.dmm = dmm
        return dmm.last_connected_resource

    def _cmd_connect_resource(self, resource: str, timeout=None):
        if self.dmm is not None:
            raise RuntimeError("请先断开当前 DMM")
        dmm = Keysight34461A(self.config_manager)
        idn = dmm.connect_resource(resource, timeout)
        self.dmm = dmm
        return {"resource": resource, "idn": idn}

    def _cmd_disconnect(self):
        if self.dmm is not None:
            try:
                self.dmm.disconnect()
            finally:
                self.dmm = None
        return True

    def _cmd_configure_dcv(self, range_val="AUTO", nplc=0.01):
        self.dmm.configure_dcv(range_val, nplc)
        return {"mode": "DCV", "range": range_val, "nplc": nplc}

    def _cmd_configure_acv(self, range_val="AUTO", bandwidth="DEF"):
        self.dmm.configure_acv(range_val, bandwidth)
        return {"mode": "ACV", "range": range_val, "bandwidth": bandwidth}

    def _cmd_configure_dci(self, range_val="AUTO", nplc=0.01):
        self.dmm.configure_dci(range_val, nplc)
        return {"mode": "DCI", "range": range_val, "nplc": nplc}

    def _cmd_configure_aci(self, range_val="AUTO"):
        self.dmm.configure_aci(range_val)
        return {"mode": "ACI", "range": range_val}

    def _cmd_configure_res(self, range_val="AUTO", nplc=10):
        self.dmm.configure_resistance(range_val, nplc)
        return {"mode": "RES", "range": range_val, "nplc": nplc}

    def _cmd_configure_freq(self, range_val="AUTO"):
        self.dmm.configure_frequency(range_val)
        return {"mode": "FREQ", "range": range_val}

    def _cmd_single_measure(self, expected_mode=None):
        mode = self.dmm.current_measurement_mode
        if expected_mode is not None and mode != expected_mode:
            raise RuntimeError(f"DMM 当前模式为 {mode or '未配置'}，回读要求 {expected_mode}")
        return self.dmm.single_measurement()

    def _cmd_gui_measurement(self, mode, range_val=None, nplc=None, reuse_config=False):
        config = (mode, range_val, nplc)
        if not reuse_config or self.gui_measurement_config != config:
            self.gui_measurement_config = None
            try:
                self.dmm.configure(mode, range_val, nplc)
            except Exception:
                self.dmm.current_measurement_mode = None
                raise
            self.gui_measurement_config = config
        start = time.perf_counter()
        try:
            value = self.dmm.single_measurement()
        except Exception:
            self.gui_measurement_config = None
            raise
        return {"value": value, "elapsed_ms": (time.perf_counter() - start) * 1000.0}

    def _cmd_continuous_measure(self, interval=1.0, count=10):
        return self.dmm.continuous_measurement(interval, count)

    def _cmd_set_display(self, text=""):
        self.dmm.set_display_text(text)
        return {"text": text[:12]}

    def _cmd_clear_display(self):
        self.dmm.clear_display_text()
        return True

    def _cmd_reset(self):
        self.dmm.reset()
        return True

    def _cmd_get_errors(self):
        return self.dmm.check_errors()

    def _cmd_set_high_impedance(self, enabled=0):
        return self.dmm.set_high_impedance(enabled)

    def _cmd_set_external_trigger(self, enabled=1, slope=None):
        return self.dmm.set_external_trigger(enabled, slope)

@dataclass
class DMMRequest:
    request_id: int
    function_code: int
    params: Dict[str, Any]
    source: str
    task_id: Optional[int]
    reply_timeout_ms: int
    parent_trace: str
    enqueued_at: float = field(default_factory=time.perf_counter)
    started: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    result: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    finished_at: Optional[float] = None

    def log_context(self):
        task = f" wincom_id={self.task_id}" if self.task_id is not None else ""
        return (f"request={self.request_id} source={self.source}{task} parent={self.parent_trace}"
                f" instrument=DMM func={self.function_code}")

    def reject(self, error):
        self.started.set_result(None)
        self.result.set_result({"success": False, "error": error})


def dmm_ctrl_thread_func(config_manager):
    logger = logging.getLogger(__name__)
    handler = DMMCommandHandler(config_manager)
    with dmm_dispatch_lock:
        dmm_ready_event.set()
    logger.info("DMM 控制线程已启动")
    try:
        while True:
            request = command_queue.get()
            try:
                if request is None:
                    break
                if not request.result.set_running_or_notify_cancel():
                    continue
                if stop_event.is_set():
                    request.reject("DMM 控制线程正在停止")
                    continue
                dmm = handler.dmm
                resource = request.params.get("resource") or (dmm.last_connected_resource if dmm else None)
                operation = handler.command_map.get(request.function_code)
                operation_name = operation.__name__.removeprefix("_cmd_") if operation else "unknown"
                start = time.perf_counter()
                request.started.set_result(start)
                token = log_trace.set(f"dmm:{request.request_id}")
                try:
                    result = handler.handle_command(request.function_code, **request.params)
                except Exception as exc:
                    logger.exception("DMM 命令处理异常 %s", request.log_context())
                    result = {"success": False, "error": str(exc)}
                finally:
                    log_trace.reset(token)
                request.finished_at = time.perf_counter()
                request.result.set_result(result)

                queue_ms = (start - request.enqueued_at) * 1000
                execution_ms = (request.finished_at - start) * 1000
                if not resource and handler.dmm:
                    resource = handler.dmm.last_connected_resource
                late = bool(request.reply_timeout_ms and execution_ms >= request.reply_timeout_ms)
                level = logging.WARNING if not result["success"] else logging.INFO if late else logging.DEBUG
                status = "late_ok" if late else "ok"
                if not result["success"]:
                    status = "late_error" if late else "error"
                logger.log(
                    level,
                    "%s operation=%s resource=%s queue_ms=%.1f execution_ms=%.1f status=%s visa_code=%s error=%r",
                    request.log_context(), operation_name, resource or "-", queue_ms, execution_ms, status,
                    result.get("visa_code", "-"), result.get("error", ""),
                )
                logger.debug("%s params=%.500r result=%.500r", request.log_context(), request.params, result)
            finally:
                command_queue.task_done()
    finally:
        with dmm_dispatch_lock:
            dmm_ready_event.clear()
            while True:
                try:
                    pending = command_queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not None and pending.result.set_running_or_notify_cancel():
                    pending.reject("DMM 控制线程已停止")
                command_queue.task_done()
        try:
            handler._cmd_disconnect()
        except Exception:
            logger.exception("DMM 退出时关闭会话失败")
        logger.info("DMM 控制线程已退出")


def send_dmm_command(function_code: int, *, source="GUI", task_id=None, reply_timeout_ms=0, **kwargs) -> Dict:
    """排队超时可取消命令，Wincom 回读超时只结束等待回执。"""
    logger = logging.getLogger(__name__)
    with dmm_dispatch_lock:
        request = DMMRequest(next(dmm_request_ids), function_code, kwargs, source, task_id, reply_timeout_ms,
                             log_trace.get())
        if stop_event.is_set() or not dmm_ready_event.is_set():
            logger.warning("%s status=unavailable", request.log_context())
            return {"request_id": request.request_id,
                    "result": {"success": False, "error": "DMM 控制线程未就绪"}}
        command_queue.put(request)
    try:
        start = request.started.result(timeout=DMM_QUEUE_TIMEOUT)
    except concurrent.futures.TimeoutError:
        if request.result.cancel():
            queue_ms = (time.perf_counter() - request.enqueued_at) * 1000
            logger.warning("%s queue_ms=%.1f status=queue_timeout", request.log_context(), queue_ms)
            return {"request_id": request.request_id,
                    "result": {"success": False, "error": "DMM 命令排队超时"}}
        start = request.started.result()

    if not reply_timeout_ms or start is None:
        result = request.result.result()
    else:
        deadline = start + reply_timeout_ms / 1000
        try:
            result = request.result.result(timeout=max(0, deadline - time.perf_counter()))
        except concurrent.futures.TimeoutError:
            result = None
        if result is None or request.finished_at >= deadline:
            logger.warning(
                "%s queue_ms=%.1f deadline_ms=%d status=reply_timeout reply=%d",
                request.log_context(), (start - request.enqueued_at) * 1000, reply_timeout_ms,
                WINCOM_READ_TIMEOUT_VALUE,
            )
            return {"request_id": request.request_id, "execution_ms": (time.perf_counter() - start) * 1000,
                    "result": {"success": False, "error": "Wincom 回读超时", "code": "reply_timeout"}}

    elapsed = (request.finished_at - start) * 1000 if start is not None else None
    return {"request_id": request.request_id, "execution_ms": elapsed, "result": result}


# Wincom 事件回调
def event_callback(type, slot, page, data):
    # 保持原样
    if type == 6:
        return 101
    elif type == 8:
        return 101
    elif type == 9:
        return 101
    elif type == 10:
        print("update done.")
        sys.exit()
        return 101

def _checked_wincom_params(task, valid_counts):
    params = _wincom_param_list(task)
    declared = task.get("param_len")
    if len(params) not in valid_counts or (declared is not None and int(declared) != len(params)):
        raise ValueError(f"Wincom func={task['func']} 参数数量无效: {len(params)}，声明: {declared}")
    return params


def _run_wincom_task(task, read_timeout_ms):
    func = task["func"]
    logger = logging.getLogger(__name__)
    context = {"source": "Wincom", "task_id": task.get("id")}
    if func == 0:
        response = send_dmm_command(13, **context)
    elif func in (1, 2, 5, 6):
        voltage = func in (1, 5)
        default_nplc = 0.1 if voltage else 1
        params = {"range_val": "AUTO", "nplc": default_nplc}
        if func in (5, 6):
            params = parse_wincom_range_nplc(task, default_nplc=default_nplc)
        response = send_dmm_command(3 if voltage else 5, **params, **context)
    elif func in (3, 4):
        expected_mode = "DCV" if func == 3 else "DCI"
        response = send_dmm_command(9, expected_mode=expected_mode, reply_timeout_ms=read_timeout_ms, **context)
        result = response["result"]
        ok = result["success"]
        timed_out = result.get("code") == "reply_timeout"
        value = result["data"] * (1 if func == 3 else 1000) if ok else None
        measurement_ui_queue.put({
            "success": ok,
            "mode": "WINCOM DCV" if func == 3 else "WINCOM DCI(mA)",
            "value": value,
            "elapsed_ms": response.get("execution_ms"),
            "reason": f"超过 Wincom 回读时限（超时值 {WINCOM_READ_TIMEOUT_VALUE}）" if timed_out else result.get("error"),
        })
        if timed_out:
            return [WINCOM_READ_TIMEOUT_VALUE]
        if not ok:
            raise RuntimeError(result["error"])
        return [value]
    elif 7 <= func <= 12:
        generator = dg832_controller
        if generator is None or not generator.connected:
            raise RuntimeError("DG832 尚未连接")
        operations = {
            7: (generator.dg832_init, 0),
            8: (generator.dg832_sync_wave, 0),
            9: (generator.dg832_set_wave, 7),
            10: (generator.dg832_set_phase, 2),
            11: (generator.dg832_output_wave, 1),
            12: (generator.dg832_close_wave, 1),
        }
        operation, count = operations[func]
        params = _checked_wincom_params(task, (count,)) if count else []
        operation(*params)
        return [0]
    elif func == 13:
        enabled, = _checked_wincom_params(task, (1,))
        response = send_dmm_command(16, enabled=enabled, **context)
    elif func == 14:
        params = _checked_wincom_params(task, (1, 2))
        slope = params[1] if len(params) == 2 else None
        response = send_dmm_command(17, enabled=params[0], slope=slope, **context)
    elif func in (0xEC, 0xEE):
        logger.debug("Wincom 功能码 %#x 已应答", func)
        return [0]
    else:
        raise ValueError(f"未知 Wincom 功能码: {func}")

    result = response["result"]
    if not result["success"]:
        raise RuntimeError(result["error"])
    return [0]


def wincom_worker(on_started, on_finished, get_read_timeout_ms):
    """轮询测试机，并独立处理每条任务的执行和回执。"""
    logger = logging.getLogger(__name__)
    error = None
    try:
        wincom.pycom_init_nocb(f"USBTMC_CTRL/V{APP_VERSION}/00{datetime.now():%y%m%d}", event_callback)
        logger.info("Wincom 通信已启动，DMM 回读时限=%d ms（0 表示关闭）", get_read_timeout_ms())
        gui_action_queue.put((on_started, None, None))
        while not wincom_stop_event.is_set():
            tasks = wincom.pycom_get_tasks(8)
            if not tasks:
                wincom_stop_event.wait(0.01)
                continue
            for task in tasks:
                task_id = task.get("id") if isinstance(task, dict) else None
                ret = [101]
                if not wincom_stop_event.is_set():
                    try:
                        logger.debug("Wincom task id=%s params=%.500r", task_id, task)
                        context = copy_context()
                        context.run(log_trace.set, f"wincom:{task_id}")
                        ret = context.run(_run_wincom_task, task, get_read_timeout_ms())
                    except Exception as exc:
                        logger.warning("Wincom 任务失败 id=%s: %s", task_id, exc)
                if task_id is not None:
                    try:
                        wincom.pycom_task_ret(task_id, ret)
                        logger.debug("Wincom 回执 id=%s payload=%s", task_id, ret)
                    except Exception:
                        logger.exception("Wincom 回执发送失败 id=%s", task_id)
    except Exception as exc:
        error = exc
        logger.exception("Wincom 工作线程异常")
    finally:
        if error is None:
            logger.info("Wincom 通信已停止")
        gui_action_queue.put((on_finished, None, error))


# GUI 应用程序类（PyQt5）
class USBTMCApp(QtWidgets.QMainWindow):
    def __init__(self, device_manager, log_dir, gui_log_handler):
        super().__init__()
        self.setWindowTitle("USB 仪器控制器")
        self.resize(1180, 820)

        self.logger = logging.getLogger(__name__)
        self.device_manager = device_manager
        self.log_dir = log_dir
        self.gui_log_handler = gui_log_handler
        self.settings = QtCore.QSettings("USBTMC", "USBTMC_Controller")
        self.connected = False
        self.device_list: List[str] = []
        self.control_thread = None
        self.wincom_thread = None
        self.continuous_thread = None
        self.continuous_stop_event = threading.Event()
        self._closing = False
        self._close_ready = threading.Event()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="gui-io")

        self.signal_generator = DG832Instrument()
        self.oscilloscope = GenericSCPIInstrument("示波器")
        # Wincom worker 直接使用该 DG832 实例处理 func 7~12。
        global dg832_controller
        dg832_controller = self.signal_generator

        self._create_ui()
        self._apply_style()

        self.poll_timer = QtCore.QTimer(self)
        self.poll_timer.timeout.connect(self._process_gui_queues)
        self.poll_timer.start(80)

        self.start_control_thread()
        self.refresh_devices()

    # ------------------------------ 基础 UI ------------------------------
    def _create_ui(self):
        central = QtWidgets.QWidget(self)
        root_layout = QtWidgets.QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        title_row = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("USB 仪器控制器")
        title.setObjectName("TitleLabel")
        subtitle = QtWidgets.QLabel("DMM · Signal Generator · Oscilloscope · Wincom")
        subtitle.setObjectName("SubtitleLabel")
        title_box = QtWidgets.QVBoxLayout()
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_row.addLayout(title_box)
        title_row.addStretch(1)
        self.global_status_label = QtWidgets.QLabel("就绪")
        self.global_status_label.setObjectName("StatusPill")
        title_row.addWidget(self.global_status_label)
        root_layout.addLayout(title_row)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_dmm_tab(), "数字万用表")
        self.tabs.addTab(self._build_generator_tab(), "信号发生器")
        self.tabs.addTab(self._build_scope_tab(), "示波器")
        self.tabs.addTab(self._build_wincom_tab(), "Wincom")
        splitter.addWidget(self.tabs)

        info_box = QtWidgets.QGroupBox("Info / 日志")
        info_layout = QtWidgets.QVBoxLayout(info_box)
        info_toolbar = QtWidgets.QHBoxLayout()
        level = logging.getLevelName(logging.getLogger().level)
        info_toolbar.addWidget(QtWidgets.QLabel(f"日志级别 {level}"))
        info_toolbar.addStretch(1)
        open_logs_btn = QtWidgets.QPushButton("打开日志目录")
        open_logs_btn.setEnabled(self.log_dir is not None)
        if self.log_dir is not None:
            open_logs_btn.setToolTip(str(self.log_dir))
        open_logs_btn.clicked.connect(self._open_logs)
        info_toolbar.addWidget(open_logs_btn)
        clear_btn = QtWidgets.QPushButton("清空 Info")
        clear_btn.clicked.connect(lambda: self.info_text.clear())
        info_toolbar.addWidget(clear_btn)
        info_layout.addLayout(info_toolbar)
        self.info_text = QtWidgets.QPlainTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumBlockCount(5000)
        info_layout.addWidget(self.info_text)
        splitter.addWidget(info_box)
        splitter.setSizes([560, 220])

        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _open_logs(self):
        if self.log_dir is not None:
            opened = QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.log_dir)))
            if not opened:
                QtWidgets.QMessageBox.warning(self, "打开日志目录失败", str(self.log_dir))

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f4f6f8; color: #1f2937; font-size: 13px; }
            QLabel#TitleLabel { font-size: 24px; font-weight: 700; color: #111827; }
            QLabel#SubtitleLabel { color: #6b7280; }
            QLabel#StatusPill { background: #e5e7eb; padding: 6px 12px; border-radius: 10px; font-weight: 600; }
            QGroupBox { background: white; border: 1px solid #dfe3e8; border-radius: 9px; margin-top: 10px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QPushButton { background: white; border: 1px solid #cfd5dc; border-radius: 6px; padding: 7px 12px; }
            QPushButton:hover { background: #f8fafc; }
            QPushButton:pressed { background: #eef2f7; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit { background: white; border: 1px solid #cfd5dc; border-radius: 6px; padding: 5px; }
            QTabWidget::pane { border: 1px solid #dfe3e8; background: white; border-radius: 8px; }
            QTabBar::tab { padding: 8px 16px; }
        """)

    def _device_row(self, role: str):
        box = QtWidgets.QGroupBox(f"{role} VISA 设备")
        layout = QtWidgets.QHBoxLayout(box)
        combo = QtWidgets.QComboBox()
        combo.setEditable(True)
        combo.setMinimumWidth(520)
        refresh_btn = QtWidgets.QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh_devices)
        connect_btn = QtWidgets.QPushButton("连接")
        layout.addWidget(QtWidgets.QLabel("资源:"))
        layout.addWidget(combo, 1)
        layout.addWidget(refresh_btn)
        layout.addWidget(connect_btn)
        return box, combo, connect_btn

    # ------------------------------ DMM ------------------------------
    def _build_dmm_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        device_box, self.dmm_resource_combo, self.dmm_connect_btn = self._device_row("DMM")
        self.dmm_connect_btn.clicked.connect(self.toggle_dmm_connection)
        layout.addWidget(device_box)

        control = QtWidgets.QGroupBox("测量控制")
        grid = QtWidgets.QGridLayout(control)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["dcv", "acv", "dci", "aci", "res", "freq"])
        self.range_edit = QtWidgets.QLineEdit()
        self.range_edit.setPlaceholderText("配置默认量程")
        self.nplc_edit = QtWidgets.QLineEdit()
        self.nplc_edit.setPlaceholderText("配置默认 NPLC")
        grid.addWidget(QtWidgets.QLabel("测量模式:"), 0, 0)
        grid.addWidget(self.mode_combo, 0, 1)
        grid.addWidget(QtWidgets.QLabel("量程:"), 0, 2)
        grid.addWidget(self.range_edit, 0, 3)
        grid.addWidget(QtWidgets.QLabel("NPLC:"), 0, 4)
        grid.addWidget(self.nplc_edit, 0, 5)

        # 测量控制区提供一个可直接手动切换的高阻按钮。
        # 默认 OFF（固定 10 MΩ），与 DMM 应用层 high_impedance_enabled=False 保持一致。
        self.dmm_highz_toggle_btn = QtWidgets.QPushButton("高阻：OFF (10 MΩ)")
        self.dmm_highz_toggle_btn.setCheckable(True)
        self.dmm_highz_toggle_btn.setChecked(False)
        self.dmm_highz_toggle_btn.setToolTip("手动切换 DCV 输入阻抗：ON=AUTO 高阻，OFF=固定 10 MΩ")
        self.dmm_highz_toggle_btn.clicked.connect(self.toggle_dmm_highz_manual)

        single_btn = QtWidgets.QPushButton("单次测量")
        single_btn.clicked.connect(self.single_measure)
        start_btn = QtWidgets.QPushButton("开始连续测量")
        start_btn.clicked.connect(self.start_continuous)
        stop_btn = QtWidgets.QPushButton("停止连续测量")
        stop_btn.clicked.connect(self.stop_continuous)
        grid.addWidget(self.dmm_highz_toggle_btn, 1, 0)
        grid.addWidget(single_btn, 1, 1)
        grid.addWidget(start_btn, 1, 2)
        grid.addWidget(stop_btn, 1, 3)
        self.measure_time_label = QtWidgets.QLabel("最近测量耗时: --")
        grid.addWidget(self.measure_time_label, 1, 4, 1, 2)
        layout.addWidget(control)

        advanced = QtWidgets.QGroupBox("DMM 高阻 / 触发设置")
        adv = QtWidgets.QGridLayout(advanced)
        self.dmm_highz_combo = QtWidgets.QComboBox()
        self.dmm_highz_combo.addItem("固定 10 MΩ (0)", 0)
        self.dmm_highz_combo.addItem("AUTO 高阻 >10 GΩ / 低量程 (1)", 1)
        self.dmm_highz_combo.setCurrentIndex(0)
        highz_btn = QtWidgets.QPushButton("应用高阻设置")
        highz_btn.clicked.connect(self.apply_dmm_highz)

        self.dmm_trigger_combo = QtWidgets.QComboBox()
        self.dmm_trigger_combo.addItem("Immediate (0)", 0)
        self.dmm_trigger_combo.addItem("External (1)", 1)
        self.dmm_trigger_slope_combo = QtWidgets.QComboBox()
        self.dmm_trigger_slope_combo.addItem("NEG / 下降沿 (0)", 0)
        self.dmm_trigger_slope_combo.addItem("POS / 上升沿 (1)", 1)
        trigger_btn = QtWidgets.QPushButton("应用触发设置")
        trigger_btn.clicked.connect(self.apply_dmm_trigger)

        adv.addWidget(QtWidgets.QLabel("DCV 输入阻抗:"), 0, 0)
        adv.addWidget(self.dmm_highz_combo, 0, 1)
        adv.addWidget(highz_btn, 0, 2)
        adv.addWidget(QtWidgets.QLabel("触发源:"), 1, 0)
        adv.addWidget(self.dmm_trigger_combo, 1, 1)
        adv.addWidget(QtWidgets.QLabel("外触发沿:"), 1, 2)
        adv.addWidget(self.dmm_trigger_slope_combo, 1, 3)
        adv.addWidget(trigger_btn, 1, 4)
        layout.addWidget(advanced)

        result_box = QtWidgets.QGroupBox("DMM 测量结果")
        result_layout = QtWidgets.QVBoxLayout(result_box)
        result_toolbar = QtWidgets.QHBoxLayout()
        result_toolbar.addStretch(1)
        clear_result_btn = QtWidgets.QPushButton("清空测量结果")
        clear_result_btn.clicked.connect(self.clear_dmm_results)
        result_toolbar.addWidget(clear_result_btn)
        result_layout.addLayout(result_toolbar)
        self.result_text = QtWidgets.QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMaximumBlockCount(3000)
        result_layout.addWidget(self.result_text)
        layout.addWidget(result_box, 1)
        return tab

    def start_control_thread(self):
        stop_event.clear()
        dmm_ready_event.clear()
        self.control_thread = threading.Thread(
            target=dmm_ctrl_thread_func,
            args=(self.device_manager.config_manager,),
            daemon=True,
            name="dmm-control",
        )
        self.control_thread.start()
        if dmm_ready_event.wait(timeout=5):
            self._set_status("DMM 控制线程已启动")
        else:
            self._set_status("DMM 控制线程启动超时")
            self.logger.error("DMM 控制线程启动超时")

    def toggle_dmm_connection(self):
        if self.connected:
            self.continuous_stop_event.set()
            self.dmm_connect_btn.setEnabled(False)
            self._run_async(lambda: send_dmm_command(2), self._dmm_disconnect_done)
            return
        resource = self.dmm_resource_combo.currentText().strip()
        if not resource:
            QtWidgets.QMessageBox.warning(self, "警告", "请先选择 DMM VISA 资源")
            return
        self._set_status(f"正在连接 DMM: {resource}")
        self.dmm_connect_btn.setEnabled(False)
        self._run_async(lambda: send_dmm_command(15, resource=resource), self._dmm_connect_done)

    def _dmm_connect_done(self, response, error):
        self.dmm_connect_btn.setEnabled(True)
        if error is None and response and response.get("result", {}).get("success"):
            self.connected = True
            self.dmm_connect_btn.setText("断开")
            self._sync_dmm_highz_ui(0)
            self.dmm_trigger_combo.setCurrentIndex(0)
            self.dmm_trigger_slope_combo.setCurrentIndex(0)
            resource = response["result"]["data"]["resource"]
            self._set_status(f"DMM 已连接: {resource}")
            self.logger.info("GUI DMM 连接成功: %s", resource)
        else:
            detail = str(error or (response or {}).get("result", {}).get("error", "未知错误"))
            self.logger.error("GUI DMM 连接失败: %s", detail)
            QtWidgets.QMessageBox.critical(self, "DMM 连接失败", detail)
            self._set_status("DMM 连接失败")

    def _dmm_disconnect_done(self, response, error):
        self.dmm_connect_btn.setEnabled(True)
        result = (response or {}).get("result", {})
        if result.get("success") or result.get("connected") is False:
            self.connected = False
            self.dmm_connect_btn.setText("连接")
            self._set_status("DMM 已断开")
            self.logger.info("GUI DMM 已断开")
        if error or not result.get("success"):
            detail = str(error or result.get("error", "未知错误"))
            QtWidgets.QMessageBox.critical(self, "错误", f"DMM 断开时出错: {detail}")

    def _sync_dmm_highz_ui(self, enabled: int):
        """同步测量控制按钮与高级设置中的高阻下拉框。"""
        enabled = 1 if int(enabled) else 0
        if hasattr(self, "dmm_highz_combo"):
            index = self.dmm_highz_combo.findData(enabled)
            if index >= 0:
                self.dmm_highz_combo.setCurrentIndex(index)
        if hasattr(self, "dmm_highz_toggle_btn"):
            self.dmm_highz_toggle_btn.setChecked(bool(enabled))
            self.dmm_highz_toggle_btn.setText("高阻：ON" if enabled else "高阻：OFF (10 MΩ)")

    def _dmm_highz_done(self, enabled: int, previous: int, response, error):
        """高阻设置完成：成功时同步两个 GUI 控件，失败时恢复原状态。"""
        ok = bool(error is None and response and response.get("result", {}).get("success"))
        if hasattr(self, "dmm_highz_toggle_btn"):
            self.dmm_highz_toggle_btn.setEnabled(True)
        if ok:
            self._sync_dmm_highz_ui(enabled)
            data = response.get("result", {}).get("data")
            mode_text = "AUTO 高阻" if enabled else "固定 10 MΩ"
            self._set_status(f"DMM 高阻模式已切换：{mode_text}")
            self.logger.info("GUI DMM 手动高阻切换完成: enabled=%s data=%s", enabled, data)
        else:
            # 命令失败时恢复到切换前的状态。
            self._sync_dmm_highz_ui(previous)
            detail = str(error or (response or {}).get("result", {}).get("error", "未知错误"))
            self._set_status("DMM 高阻模式切换失败")
            self.logger.error("GUI DMM 高阻模式切换失败: %s", detail)
            QtWidgets.QMessageBox.critical(self, "DMM 高阻模式切换失败", detail)

    def toggle_dmm_highz_manual(self, checked: bool):
        """测量控制区按钮：手动在 AUTO 高阻与固定 10 MΩ 之间切换。"""
        enabled = 1 if checked else 0
        previous = 0 if enabled else 1
        if not self.connected:
            # QPushButton 是 checkable 的，未连接时需要把刚刚的视觉切换还原。
            self._sync_dmm_highz_ui(0 if enabled else 1)
            QtWidgets.QMessageBox.warning(self, "警告", "请先连接 DMM")
            return
        self.dmm_highz_toggle_btn.setEnabled(False)
        self.dmm_highz_toggle_btn.setText("高阻切换中...")
        mode_text = "AUTO 高阻" if enabled else "固定 10 MΩ"
        self._set_status(f"正在切换 DMM 高阻模式：{mode_text}...")
        self._run_async(
            lambda: send_dmm_command(16, enabled=enabled),
            lambda response, error: self._dmm_highz_done(enabled, previous, response, error)
        )

    def apply_dmm_highz(self):
        if not self.connected:
            QtWidgets.QMessageBox.warning(self, "警告", "请先连接 DMM")
            return
        enabled = int(self.dmm_highz_combo.currentData())
        previous = 1 if getattr(self, "dmm_highz_toggle_btn", None) and self.dmm_highz_toggle_btn.isChecked() else 0
        if hasattr(self, "dmm_highz_toggle_btn"):
            self.dmm_highz_toggle_btn.setEnabled(False)
        self._set_status("正在设置 DMM 输入阻抗...")
        self._run_async(
            lambda: send_dmm_command(16, enabled=enabled),
            lambda response, error: self._dmm_highz_done(enabled, previous, response, error)
        )

    def apply_dmm_trigger(self):
        if not self.connected:
            QtWidgets.QMessageBox.warning(self, "警告", "请先连接 DMM")
            return
        enabled = int(self.dmm_trigger_combo.currentData())
        slope = int(self.dmm_trigger_slope_combo.currentData())
        self._set_status("正在设置 DMM 触发...")
        self._run_async(
            lambda: send_dmm_command(17, enabled=enabled, slope=slope),
            lambda response, error: self._dmm_advanced_done("触发设置", response, error)
        )

    def _dmm_advanced_done(self, name: str, response, error):
        ok = bool(error is None and response and response.get("result", {}).get("success"))
        if ok:
            data = response.get("result", {}).get("data")
            self._set_status(f"DMM {name}完成")
            self.logger.info("GUI DMM %s完成: %s", name, data)
        else:
            detail = str(error or (response or {}).get("result", {}).get("error", "未知错误"))
            self._set_status(f"DMM {name}失败")
            self.logger.error("GUI DMM %s失败: %s", name, detail)
            QtWidgets.QMessageBox.critical(self, f"DMM {name}失败", detail)

    def _dmm_measurement_settings(self):
        mode = self.mode_combo.currentText()
        nplc = None
        if mode in ("dcv", "dci", "res"):
            text = self.nplc_edit.text().strip()
            if text:
                try:
                    nplc = float(text)
                    if not math.isfinite(nplc) or nplc <= 0:
                        raise ValueError
                except ValueError:
                    QtWidgets.QMessageBox.warning(self, "参数错误", "NPLC 必须是大于零的有限数值")
                    return None
        return {"mode": mode, "range_val": self.range_edit.text().strip() or None, "nplc": nplc}

    def single_measure(self):
        if not self.connected:
            QtWidgets.QMessageBox.warning(self, "警告", "请先连接 DMM")
            return
        settings = self._dmm_measurement_settings()
        if settings is None:
            return

        self._set_status("DMM 单次测量中...")
        self._run_async(
            lambda: send_dmm_command(18, **settings),
            lambda response, error: self._single_measure_done(settings["mode"], response, error),
        )

    def _single_measure_done(self, mode, response, error):
        result = (response or {}).get("result", {})
        if result.get("success"):
            data = result["data"]
            self._show_measurement_result(mode, data["value"], data["elapsed_ms"])
        else:
            self._show_measurement_failure(mode, None)
            detail = error or result.get("error", "未知错误")
            self._set_status(f"DMM 单次测量失败: {detail}")
            self.logger.error("DMM 单次测量失败: %s", detail)

    def start_continuous(self):
        if not self.connected:
            QtWidgets.QMessageBox.warning(self, "警告", "请先连接 DMM")
            return
        if self.continuous_thread and self.continuous_thread.is_alive():
            QtWidgets.QMessageBox.information(self, "提示", "连续测量已经在运行")
            return
        settings = self._dmm_measurement_settings()
        if settings is None:
            return
        self.continuous_stop_event.clear()

        def cont_worker():
            successful = 0
            for i in range(1, 11):
                if self.continuous_stop_event.is_set() or stop_event.is_set():
                    break
                result = send_dmm_command(18, reuse_config=i > 1, **settings)["result"]
                data = result.get("data", {})
                successful += int(result["success"])
                measurement_ui_queue.put({
                    "success": result["success"],
                    "mode": settings["mode"],
                    "value": data.get("value"),
                    "elapsed_ms": data.get("elapsed_ms"),
                    "index": i,
                })
                if i < 10 and self.continuous_stop_event.wait(1.0):
                    break
            gui_action_queue.put((self._continuous_finished, (self.continuous_stop_event.is_set(), successful), None))

        self.continuous_thread = threading.Thread(target=cont_worker, daemon=True, name="dmm-continuous")
        self.continuous_thread.start()
        self._set_status("DMM 连续测量进行中...")
        self.logger.info("开始 DMM 连续测量 10 次，参数: %s", settings)

    def _continuous_finished(self, result, error):
        stopped, successful = result
        status = "DMM 连续测量已停止" if stopped else "DMM 连续测量结束"
        self._set_status(f"{status}，成功 {successful} 次")
        self.logger.info("%s，成功 %d 次", status, successful)

    def stop_continuous(self):
        self.continuous_stop_event.set()
        self._set_status("正在停止 DMM 连续测量...")
        self.logger.info("请求停止 DMM 连续测量")

    def clear_dmm_results(self):
        """仅清空 GUI 中的 DMM 测量结果，不影响日志文件。"""
        self.result_text.clear()
        self.measure_time_label.setText("最近测量耗时: --")
        self._set_status("DMM 测量结果已清空")
        self.logger.info("GUI DMM 测量结果已清空（日志文件保留）")

    def _show_measurement_result(self, mode: str, value: Any, elapsed_ms: float, index: Optional[int] = None):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        index_text = f" #{index}" if index is not None else ""
        line = f"[{timestamp}] {mode.upper()}{index_text} = {value} (测量耗时 {elapsed_ms:.2f} ms)"
        self.result_text.appendPlainText(line)
        self.measure_time_label.setText(f"最近测量耗时: {elapsed_ms:.2f} ms（{timestamp}）")
        self._set_status(f"DMM 测量值: {value}；耗时 {elapsed_ms:.2f} ms")
        self.logger.debug(line)

    def _show_measurement_failure(self, mode: str, elapsed_ms: Optional[float],
                                  index: Optional[int] = None, reason: Optional[str] = None):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        index_text = f" #{index}" if index is not None else ""
        elapsed_text = f"，等待 {elapsed_ms:.2f} ms" if elapsed_ms is not None else ""
        line = f"[{timestamp}] {mode.upper()}{index_text} 测量失败{elapsed_text}"
        self.result_text.appendPlainText(line)
        self.measure_time_label.setText(f"最近测量: 失败{elapsed_text}（{timestamp}）")
        if reason:
            self.result_text.appendPlainText(f"  {reason}")
        self._set_status(reason or "DMM 测量失败")

    # --------------------------- 信号发生器 ---------------------------
    def _build_generator_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        box, self.gen_resource_combo, self.gen_connect_btn = self._device_row("信号发生器 / RIGOL DG832")
        self.gen_connect_btn.clicked.connect(lambda: self._toggle_scpi_connection("generator"))
        layout.addWidget(box)

        quick = QtWidgets.QGroupBox("DG832 双通道控制")
        grid = QtWidgets.QGridLayout(quick)

        self.gen_channel = QtWidgets.QSpinBox()
        self.gen_channel.setRange(1, 2)
        self.gen_channel.setValue(1)

        self.gen_freq = QtWidgets.QLineEdit("1000")
        self.gen_rms = QtWidgets.QComboBox()
        self.gen_rms.addItem("Vpp (rms=0)", 0)
        self.gen_rms.addItem("Vrms (rms=1)", 1)
        self.gen_rms.setCurrentIndex(1)
        self.gen_amp = QtWidgets.QLineEdit("1")
        self.gen_duty = QtWidgets.QLineEdit("50")
        self.gen_offset = QtWidgets.QLineEdit("0")
        self.gen_phase = QtWidgets.QLineEdit("0")
        self.gen_waveform = QtWidgets.QComboBox()
        for label, code in [
            ("Pulse (0)", 0),
            ("Sine (1)", 1),
            ("Square (2)", 2),
            ("Ramp (3)", 3),
            ("Noise (4)", 4),
            ("DualTone (5)", 5),
            ("Harmonic (6)", 6),
            ("DC (7)", 7),
        ]:
            self.gen_waveform.addItem(label, code)
        self.gen_waveform.setCurrentIndex(1)

        # SetWave 显示顺序严格按 C 接口：ch, Freq, rms, AMP, dcyc, offset, IsSine
        fields = [
            ("ch", self.gen_channel),
            ("Freq (Hz)", self.gen_freq),
            ("rms", self.gen_rms),
            ("AMP", self.gen_amp),
            ("dcyc (%)", self.gen_duty),
            ("offset (V)", self.gen_offset),
            ("IsSine", self.gen_waveform),
        ]
        for col, (name, widget) in enumerate(fields):
            grid.addWidget(QtWidgets.QLabel(name), 0, col)
            grid.addWidget(widget, 1, col)

        apply_btn = QtWidgets.QPushButton("SetWave")
        apply_btn.clicked.connect(self.apply_generator_settings)
        init_btn = QtWidgets.QPushButton("Init")
        init_btn.clicked.connect(self.generator_init)
        sync_btn = QtWidgets.QPushButton("SyncWave")
        sync_btn.clicked.connect(self.generator_sync_wave)
        phase_btn = QtWidgets.QPushButton("SetPhase")
        phase_btn.clicked.connect(self.generator_set_phase)
        out_on = QtWidgets.QPushButton("OutputWave")
        out_on.clicked.connect(lambda: self.set_generator_output(True))
        out_off = QtWidgets.QPushButton("CloseWave")
        out_off.clicked.connect(lambda: self.set_generator_output(False))
        grid.addWidget(init_btn, 2, 0)
        grid.addWidget(sync_btn, 2, 1)
        grid.addWidget(apply_btn, 2, 2)
        grid.addWidget(QtWidgets.QLabel("SetPhase phase (°)"), 2, 3)
        grid.addWidget(self.gen_phase, 2, 4)
        grid.addWidget(phase_btn, 2, 5)
        grid.addWidget(out_on, 3, 5)
        grid.addWidget(out_off, 3, 6)
        layout.addWidget(quick)

        mapping_hint = QtWidgets.QLabel(
            "Wincom 固定映射：7=Init，8=SyncWave，9=SetWave(ch,Freq,rms,AMP,dcyc,offset,IsSine)，"
            "10=SetPhase(ch,phase)，11=OutputWave(ch)，12=CloseWave(ch)。"
        )
        mapping_hint.setWordWrap(True)
        #layout.addWidget(mapping_hint)

        raw, self.gen_scpi_edit, self.gen_scpi_response = self._build_raw_scpi_box("信号发生器 / DG832")
        raw.findChild(QtWidgets.QPushButton, "writeButton").clicked.connect(lambda: self.send_raw_scpi("generator", False))
        raw.findChild(QtWidgets.QPushButton, "queryButton").clicked.connect(lambda: self.send_raw_scpi("generator", True))
        layout.addWidget(raw, 1)
        return tab

    def _read_generator_setwave_args(self) -> Tuple[int, float, int, float, Any, float, int]:
        try:
            ch = int(self.gen_channel.value())
            freq = float(self.gen_freq.text().strip())
            rms = int(self.gen_rms.currentData())
            amp = float(self.gen_amp.text().strip())
            duty_text = self.gen_duty.text().strip()
            duty = float(duty_text) if duty_text else 0.0
            offset = float(self.gen_offset.text().strip())
            wave_code = int(self.gen_waveform.currentData())
            return ch, freq, rms, amp, duty, offset, wave_code
        except ValueError as exc:
            raise ValueError(f"DG832 参数必须为数字: {exc}")

    def apply_generator_settings(self):
        try:
            args = self._read_generator_setwave_args()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "DG832 参数错误", str(exc))
            return

        def done(commands, error):
            if error is None:
                self._set_status(f"DG832 CH{args[0]} SetWave 完成")
                self.logger.info("GUI DG832 SetWave 完成: %s", commands)
            else:
                self.logger.error("GUI DG832 SetWave 失败: %s", error)
                QtWidgets.QMessageBox.critical(self, "DG832 SetWave 失败", str(error))
                self._set_status("DG832 SetWave 失败")
        self._run_async(lambda: self.signal_generator.dg832_set_wave(*args), done)

    def generator_init(self):
        self._run_async(
            self.signal_generator.dg832_init,
            lambda _r, e: self._simple_scpi_done("DG832 Init", e)
        )

    def generator_sync_wave(self):
        self._run_async(
            self.signal_generator.dg832_sync_wave,
            lambda _r, e: self._simple_scpi_done("DG832 SyncWave", e)
        )

    def generator_set_phase(self):
        ch = self.gen_channel.value()
        try:
            phase = float(self.gen_phase.text().strip())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "参数错误", "phase 必须是数字")
            return
        self._run_async(
            lambda: self.signal_generator.dg832_set_phase(ch, phase),
            lambda _r, e: self._simple_scpi_done(f"DG832 CH{ch} SetPhase", e)
        )

    def set_generator_output(self, enabled: bool):
        ch = self.gen_channel.value()
        action = (lambda: self.signal_generator.dg832_output_wave(ch)) if enabled else (lambda: self.signal_generator.dg832_close_wave(ch))
        self._run_async(
            action,
            lambda _r, e: self._simple_scpi_done(
                f"DG832 CH{ch} {'OutputWave' if enabled else 'CloseWave'}", e
            )
        )

    def _simple_scpi_done(self, description: str, error: Optional[BaseException]):
        if error is None:
            self.logger.info("%s 完成", description)
            self._set_status(f"{description} 完成")
        else:
            self.logger.error("%s 失败: %s", description, error)
            self._set_status(f"{description} 失败")
            QtWidgets.QMessageBox.critical(self, "SCPI 执行失败", f"{description}: {error}")

    # ----------------------------- 示波器 -----------------------------
    def _build_scope_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        box, self.scope_resource_combo, self.scope_connect_btn = self._device_row("示波器")
        self.scope_connect_btn.clicked.connect(lambda: self._toggle_scpi_connection("scope"))
        layout.addWidget(box)

        quick = QtWidgets.QGroupBox("快捷 SCPI 控制（具体型号可直接在下方修改原始 SCPI）")
        grid = QtWidgets.QGridLayout(quick)
        self.scope_channel = QtWidgets.QSpinBox(); self.scope_channel.setRange(1, 8); self.scope_channel.setValue(1)
        self.scope_scale = QtWidgets.QLineEdit("1")
        self.scope_offset = QtWidgets.QLineEdit("0")
        self.scope_time_scale = QtWidgets.QLineEdit("0.001")
        for col, (name, widget) in enumerate([
            ("通道", self.scope_channel), ("垂直档位(V/div)", self.scope_scale),
            ("垂直 Offset(V)", self.scope_offset), ("时基(s/div)", self.scope_time_scale)
        ]):
            grid.addWidget(QtWidgets.QLabel(name), 0, col)
            grid.addWidget(widget, 1, col)
        apply_btn = QtWidgets.QPushButton("应用通道/时基")
        apply_btn.clicked.connect(self.apply_scope_settings)
        autoscale_btn = QtWidgets.QPushButton("AutoScale"); autoscale_btn.clicked.connect(lambda: self._send_scpi_sequence(self.oscilloscope, ["AUT"], "示波器 AutoScale"))
        run_btn = QtWidgets.QPushButton("RUN"); run_btn.clicked.connect(lambda: self._send_scpi_sequence(self.oscilloscope, ["RUN"], "示波器 RUN"))
        stop_btn = QtWidgets.QPushButton("STOP"); stop_btn.clicked.connect(lambda: self._send_scpi_sequence(self.oscilloscope, ["STOP"], "示波器 STOP"))
        single_btn = QtWidgets.QPushButton("SINGLE"); single_btn.clicked.connect(lambda: self._send_scpi_sequence(self.oscilloscope, ["SING"], "示波器 SINGLE"))
        grid.addWidget(apply_btn, 2, 0)
        grid.addWidget(autoscale_btn, 2, 1)
        grid.addWidget(run_btn, 2, 2)
        grid.addWidget(stop_btn, 2, 3)
        grid.addWidget(single_btn, 2, 4)
        layout.addWidget(quick)

        save_box = QtWidgets.QGroupBox("保存波形到本机目录（Keysight 风格 ASCII SCPI）")
        save_grid = QtWidgets.QGridLayout(save_box)
        self.scope_save_dir = QtWidgets.QLineEdit(str(Path.home() / "Documents"))
        browse_btn = QtWidgets.QPushButton("选择目录...")
        browse_btn.clicked.connect(self.choose_scope_save_directory)
        self.scope_save_name = QtWidgets.QLineEdit()
        self.scope_save_name.setPlaceholderText("留空则自动命名 scope_CHx_YYYYMMDD_HHMMSS.csv")
        self.scope_wave_query = QtWidgets.QLineEdit("WAV:DATA?")
        self.scope_wave_query.setToolTip("不同品牌示波器可在此修改实际波形数据查询命令")
        save_btn = QtWidgets.QPushButton("保存当前通道波形")
        save_btn.clicked.connect(self.save_scope_waveform)
        save_grid.addWidget(QtWidgets.QLabel("目标目录"), 0, 0)
        save_grid.addWidget(self.scope_save_dir, 0, 1, 1, 4)
        save_grid.addWidget(browse_btn, 0, 5)
        save_grid.addWidget(QtWidgets.QLabel("文件名"), 1, 0)
        save_grid.addWidget(self.scope_save_name, 1, 1, 1, 2)
        save_grid.addWidget(QtWidgets.QLabel("波形查询 SCPI"), 1, 3)
        save_grid.addWidget(self.scope_wave_query, 1, 4)
        save_grid.addWidget(save_btn, 1, 5)
        layout.addWidget(save_box)

        raw, self.scope_scpi_edit, self.scope_scpi_response = self._build_raw_scpi_box("示波器")
        raw.findChild(QtWidgets.QPushButton, "writeButton").clicked.connect(lambda: self.send_raw_scpi("scope", False))
        raw.findChild(QtWidgets.QPushButton, "queryButton").clicked.connect(lambda: self.send_raw_scpi("scope", True))
        layout.addWidget(raw, 1)
        return tab

    def apply_scope_settings(self):
        ch = self.scope_channel.value()
        cmds = [
            f"CHAN{ch}:DISP ON",
            f"CHAN{ch}:SCAL {self.scope_scale.text().strip()}",
            f"CHAN{ch}:OFFS {self.scope_offset.text().strip()}",
            f"TIM:SCAL {self.scope_time_scale.text().strip()}",
        ]
        self._send_scpi_sequence(self.oscilloscope, cmds, "示波器通道/时基")

    def choose_scope_save_directory(self):
        current = self.scope_save_dir.text().strip() or str(Path.home())
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "选择波形保存目录", current)
        if directory:
            self.scope_save_dir.setText(directory)
            self.logger.info("示波器波形保存目录: %s", directory)

    @staticmethod
    def _safe_waveform_filename(name: str, ch: int) -> str:
        name = (name or "").strip()
        if not name:
            name = f"scope_CH{ch}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        invalid = '<>:"/\\|?*'
        for c in invalid:
            name = name.replace(c, "_")
        if not Path(name).suffix:
            name += ".csv"
        return name

    def save_scope_waveform(self):
        """将当前通道波形通过 SCPI 读取并保存到用户选择的本机目录。"""
        if not self.oscilloscope.connected:
            QtWidgets.QMessageBox.warning(self, "警告", "请先连接示波器")
            return

        ch = self.scope_channel.value()
        target_dir = Path(self.scope_save_dir.text().strip()).expanduser()
        filename = self._safe_waveform_filename(self.scope_save_name.text(), ch)
        target_file = target_dir / filename
        query_cmd = self.scope_wave_query.text().strip() or "WAV:DATA?"

        def worker():
            target_dir.mkdir(parents=True, exist_ok=True)
            # 当前示波器快捷页本来就是 Keysight 风格，因此默认按该风格准备 ASCII 波形。
            with self.oscilloscope._lock:
                self.oscilloscope.write_sequence((f"WAV:SOUR CHAN{ch}", "WAV:FORM ASC"))
                resource = self.oscilloscope.resource
                preamble = ""
                try:
                    preamble = self.oscilloscope.query("WAV:PRE?")
                except Exception as exc:
                    self.logger.warning("示波器 WAV:PRE? 查询失败，将仅保存波形数据: %s", exc)
                data = self.oscilloscope.query(query_cmd, log_response=False)
            with open(target_file, "w", encoding="utf-8", newline="") as fp:
                fp.write(f"# SavedAt,{datetime.now().isoformat(timespec='seconds')}\n")
                fp.write(f"# Resource,{resource or ''}\n")
                fp.write(f"# Channel,CH{ch}\n")
                if preamble:
                    fp.write(f"# Preamble,{preamble}\n")
                fp.write(data)
                if data and not data.endswith("\n"):
                    fp.write("\n")
            return str(target_file)

        def done(path, error):
            if error is None:
                self.scope_scpi_response.appendPlainText(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 波形已保存: {path}"
                )
                self.logger.info("示波器波形已保存: %s", path)
                self._set_status(f"示波器波形已保存: {path}")
            else:
                self.logger.error("示波器波形保存失败: %s", error)
                self._set_status("示波器波形保存失败")
                QtWidgets.QMessageBox.critical(self, "保存波形失败", str(error))
        self._set_status("正在读取并保存示波器波形...")
        self._run_async(worker, done)

    # --------------------------- 通用 SCPI ---------------------------
    def _build_raw_scpi_box(self, role: str):
        box = QtWidgets.QGroupBox(f"{role} 原始 SCPI")
        layout = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        edit = QtWidgets.QLineEdit("*IDN?")
        write_btn = QtWidgets.QPushButton("Write")
        write_btn.setObjectName("writeButton")
        query_btn = QtWidgets.QPushButton("Query")
        query_btn.setObjectName("queryButton")
        row.addWidget(edit, 1)
        row.addWidget(write_btn)
        row.addWidget(query_btn)
        layout.addLayout(row)
        response = QtWidgets.QPlainTextEdit()
        response.setReadOnly(True)
        response.setMaximumBlockCount(2000)
        layout.addWidget(response)
        return box, edit, response

    def _toggle_scpi_connection(self, which: str):
        if which == "generator":
            inst, combo, btn, role = self.signal_generator, self.gen_resource_combo, self.gen_connect_btn, "信号发生器"
        else:
            inst, combo, btn, role = self.oscilloscope, self.scope_resource_combo, self.scope_connect_btn, "示波器"
        if inst.connected:
            btn.setEnabled(False)
            self._run_async(inst.disconnect, lambda result, error: self._scpi_disconnect_done(inst, btn, role, error))
            return
        resource = combo.currentText().strip()
        if not resource:
            QtWidgets.QMessageBox.warning(self, "警告", f"请先选择{role} VISA 资源")
            return
        btn.setEnabled(False)
        self._set_status(f"正在连接{role}: {resource}")
        self._run_async(lambda: inst.connect(resource), lambda idn, error: self._scpi_connect_done(inst, btn, role, resource, idn, error))

    def _scpi_connect_done(self, inst, btn, role, resource, idn, error):
        btn.setEnabled(True)
        if error is None:
            btn.setText("断开")
            self._set_status(f"{role}已连接: {idn}")
            self.logger.info("GUI %s 连接成功: %s | %s", role, resource, idn)
        else:
            btn.setText("连接")
            self._set_status(f"{role}连接失败")
            self.logger.error("GUI %s 连接失败: %s", role, error)
            QtWidgets.QMessageBox.critical(self, f"{role}连接失败", str(error))

    def _scpi_disconnect_done(self, inst, btn, role, error):
        btn.setEnabled(True)
        btn.setText("断开" if inst.connected else "连接")
        if error is None:
            self._set_status(f"{role}已断开")
        else:
            QtWidgets.QMessageBox.critical(self, "断开失败", f"{role}: {error}")

    def send_raw_scpi(self, which: str, query_mode: bool):
        if which == "generator":
            inst, edit, response_box, role = self.signal_generator, self.gen_scpi_edit, self.gen_scpi_response, "信号发生器"
        else:
            inst, edit, response_box, role = self.oscilloscope, self.scope_scpi_edit, self.scope_scpi_response, "示波器"
        command = edit.text().strip()
        if not command:
            return
        action = (lambda: inst.query(command)) if query_mode else (lambda: inst.write(command))

        def done(result, error):
            timestamp = datetime.now().strftime("%H:%M:%S")
            if error is None:
                if query_mode:
                    response_box.appendPlainText(f"[{timestamp}] >> {command}\n[{timestamp}] << {result}")
                else:
                    response_box.appendPlainText(f"[{timestamp}] >> {command}  [OK]")
                self._set_status(f"{role} SCPI {'Query' if query_mode else 'Write'} 完成")
            else:
                response_box.appendPlainText(f"[{timestamp}] !! {command}\nERROR: {error}")
                self.logger.error("%s SCPI 执行失败: %s", role, error)
                self._set_status(f"{role} SCPI 执行失败")
        self._run_async(action, done)

    def _send_scpi_sequence(self, inst: GenericSCPIInstrument, commands: List[str], description: str):
        def worker():
            inst.write_sequence(commands)

        def done(result, error):
            if error is None:
                self.logger.info("%s完成: %s", description, result)
                self._set_status(f"{description}完成")
            else:
                self.logger.error("%s失败: %s", description, error)
                self._set_status(f"{description}失败")
                QtWidgets.QMessageBox.critical(self, "SCPI 执行失败", f"{description}: {error}")
        self._run_async(worker, done)

    # ------------------------------ Wincom ------------------------------
    def _build_wincom_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        box = QtWidgets.QGroupBox("Wincom 通信")
        v = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        self.wincom_enable = QtWidgets.QCheckBox("启用 Wincom 通信")
        self.wincom_enable.toggled.connect(self.toggle_wincom)
        self.wincom_status_label = QtWidgets.QLabel("已停止")
        row.addWidget(self.wincom_enable)
        row.addWidget(self.wincom_status_label)
        row.addStretch(1)
        v.addLayout(row)
        timeout_row = QtWidgets.QHBoxLayout()
        timeout_row.addWidget(QtWidgets.QLabel("DMM 回读超时"))
        self.wincom_read_timeout_spin = QtWidgets.QSpinBox()
        self.wincom_read_timeout_spin.setRange(0, WINCOM_READ_TIMEOUT_MAX_MS)
        self.wincom_read_timeout_spin.setSuffix(" ms")
        self.wincom_read_timeout_spin.setSpecialValueText("关闭")
        self.wincom_read_timeout_spin.setKeyboardTracking(False)
        try:
            saved_timeout = int(self.settings.value("wincom/read_timeout_ms", 0))
        except (ValueError, TypeError):
            saved_timeout = 0
        self.wincom_read_timeout_spin.setValue(max(0, min(saved_timeout, WINCOM_READ_TIMEOUT_MAX_MS)))
        self.wincom_read_timeout_ms = self.wincom_read_timeout_spin.value()
        self.wincom_read_timeout_spin.valueChanged.connect(self._set_wincom_read_timeout)
        timeout_row.addWidget(self.wincom_read_timeout_spin)
        timeout_row.addStretch(1)
        v.addLayout(timeout_row)
        hint = QtWidgets.QLabel(
            f"适用于 Wincom 电压和电流回读。从 DMM 开始读取计时，超时回传 {WINCOM_READ_TIMEOUT_VALUE}。"
        )
        hint.setWordWrap(True)
        v.addWidget(hint)
        layout.addWidget(box)
        layout.addStretch(1)
        return tab

    def _set_wincom_read_timeout(self, timeout_ms):
        self.wincom_read_timeout_ms = timeout_ms
        self.settings.setValue("wincom/read_timeout_ms", timeout_ms)
        self.logger.info("Wincom DMM 回读超时设置: %s", f"{timeout_ms} ms" if timeout_ms else "关闭")

    def toggle_wincom(self, enabled: bool):
        if enabled:
            if not wincom:
                self._wincom_finished(None, RuntimeError(f"加载 pywincom 失败: {wincom_load_error}"))
                return
            self.wincom_enable.setEnabled(False)
            self.wincom_status_label.setText("正在启动")
            wincom_stop_event.clear()
            self.wincom_thread = threading.Thread(
                target=wincom_worker,
                args=(self._wincom_started, self._wincom_finished, lambda: self.wincom_read_timeout_ms),
                daemon=True,
                name="wincom-worker",
            )
            self.wincom_thread.start()
        else:
            wincom_stop_event.set()
            self.wincom_enable.setEnabled(False)
            self.wincom_status_label.setText("正在停止")

    def _wincom_started(self, result, error):
        self.wincom_status_label.setText("运行中")
        self.wincom_enable.setEnabled(True)
        self._set_status("Wincom 通信已启用")

    def _wincom_finished(self, result, error):
        self.wincom_enable.blockSignals(True)
        self.wincom_enable.setChecked(False)
        self.wincom_enable.blockSignals(False)
        self.wincom_enable.setEnabled(True)
        self.wincom_status_label.setText("异常退出" if error else "已停止")
        if error:
            self._set_status(f"Wincom 通信失败: {error}")
        else:
            self._set_status("Wincom 通信已停止")

    # ----------------------------- 公共方法 -----------------------------
    def refresh_devices(self):
        def worker():
            return self.device_manager.list_visa_resources()

        def done(resources, error):
            if error:
                self.logger.error("VISA 设备扫描失败: %s", error)
                self._set_status("VISA 设备扫描失败")
                return
            self.device_list = list(resources or [])
            combos = [self.dmm_resource_combo, self.gen_resource_combo, self.scope_resource_combo]
            for combo in combos:
                current = combo.currentText().strip()
                combo.blockSignals(True)
                combo.clear()
                combo.addItems(self.device_list)
                if current and current not in self.device_list:
                    combo.addItem(current)
                if current:
                    combo.setCurrentText(current)
                combo.blockSignals(False)
            self._set_status(f"发现 {len(self.device_list)} 个 VISA 设备")
            self.logger.info("VISA 扫描完成，共 %d 个资源", len(self.device_list))
        self._run_async(worker, done)

    def _run_async(self, fn, done_callback):
        submitted = time.perf_counter()
        context = copy_context()
        context.run(log_trace.set, f"gui:{next(gui_request_ids)}")

        def run():
            start = time.perf_counter()
            try:
                return fn()
            except Exception as exc:
                visa_code = getattr(exc, "error_code", "-")
                self.logger.warning("GUI 请求失败 visa_code=%s error=%r", getattr(visa_code, "name", visa_code), exc)
                raise
            finally:
                self.logger.debug("GUI 请求结束 queue_ms=%.1f execution_ms=%.1f",
                                  (start - submitted) * 1000, (time.perf_counter() - start) * 1000)

        future = self.executor.submit(context.run, run)
        callback = partial(context.run, done_callback)

        def on_done(fut):
            try:
                result = fut.result()
                gui_action_queue.put((callback, result, None))
            except Exception as exc:
                gui_action_queue.put((callback, None, exc))
        future.add_done_callback(on_done)

    def _process_gui_queues(self):
        if self._closing:
            if self._close_ready.is_set():
                self.close()
            return
        lines = self.gui_log_handler.drain()
        if lines:
            self.info_text.appendPlainText("\n".join(lines))

        try:
            for _ in range(100):
                event = measurement_ui_queue.get_nowait()
                mode = event.get("mode", "WINCOM")
                elapsed = event.get("elapsed_ms")
                index = event.get("index")
                if event.get("success"):
                    self._show_measurement_result(mode, event.get("value"), elapsed, index)
                else:
                    self._show_measurement_failure(mode, elapsed, index, event.get("reason"))
        except queue.Empty:
            pass

        try:
            while True:
                callback, result, error = gui_action_queue.get_nowait()
                callback(result, error)
        except queue.Empty:
            pass

    def _set_status(self, text: str):
        self.global_status_label.setText(text)
        self.statusBar().showMessage(text)

    def closeEvent(self, event):
        if self._close_ready.is_set():
            self.poll_timer.stop()
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        answer = QtWidgets.QMessageBox.question(
            self, "退出", "确定要退出吗？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return

        self._closing = True
        self.centralWidget().setEnabled(False)
        self._set_status("正在结束仪器任务并关闭 VISA 会话...")
        self.continuous_stop_event.set()
        stop_event.set()
        wincom_stop_event.set()
        command_queue.put(None)
        threading.Thread(target=self._shutdown, name="instrument-shutdown").start()

    def _shutdown(self):
        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
            for worker in (self.wincom_thread, self.continuous_thread, self.control_thread):
                if worker is not None:
                    worker.join()
            for instrument in (self.signal_generator, self.oscilloscope):
                try:
                    instrument.disconnect()
                except Exception:
                    self.logger.exception("关闭 %s 会话时异常", instrument.role)
            close_visa_manager()
        except Exception:
            self.logger.exception("关闭仪器控制器时异常")
        finally:
            global dg832_controller
            dg832_controller = None
            self._close_ready.set()

# 主程序入口
def main():
    with setup_logging() as (log_dir, gui_log_handler):
        device_manager = DeviceManager("dmm_config.json")
        settings = device_manager.config_manager.config.get("measurement", {})
        level_name = str(settings.get("log_level", "INFO")).strip().upper()
        level = logging.getLevelName(level_name)
        if not isinstance(level, int):
            logging.getLogger(__name__).warning("无效的 measurement.log_level=%r，使用 INFO", level_name)
            level = logging.INFO
        logging.getLogger().setLevel(level)
        logger = logging.getLogger(__name__)
        logger.info("USBTMC Controller V%s 启动，日志级别=%s，目录=%s",
                    APP_VERSION, logging.getLevelName(level), log_dir or "不可用")
        app = QtWidgets.QApplication(sys.argv)
        app.setApplicationName("USBTMC Controller")
        app.setStyle("Fusion")
        window = USBTMCApp(device_manager, log_dir, gui_log_handler)
        window.show()
        exit_code = app.exec_()
        logger.info("USBTMC Controller V%s 退出，code=%d", APP_VERSION, exit_code)
        return exit_code

if __name__ == "__main__":
    sys.exit(main())
