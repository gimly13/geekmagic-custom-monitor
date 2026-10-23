import socket
import time
import threading
import psutil
from pynvml import *
import sys
import os
import pystray
from PIL import Image, ImageDraw

# ==================== НАСТРОЙКИ ====================
DEFAULT_PORT = 9998
MIN_CPU_FREQ = 800  # Минимальная частота для расчетов (в простое)
# ====================================================

# --- ДИНАМИЧЕСКОЕ ОПРЕДЕЛЕНИЕ МАКСИМАЛЬНОЙ ЧАСТОТЫ CPU ---
try:
    MAX_CPU_FREQ = psutil.cpu_freq().max
    # Если Windows не отдает максимум (часто бывает на AMD), берем текущую или ставим заглушку
    if MAX_CPU_FREQ == 0:
        MAX_CPU_FREQ = psutil.cpu_freq().current
    if MAX_CPU_FREQ == 0:
        MAX_CPU_FREQ = 3000  # Аварийный дефолт, если вообще ничего не известно
except Exception:
    MAX_CPU_FREQ = 3000
# ---------------------------------------------------------

try:
    nvmlInit()
    gpu_handle = nvmlDeviceGetHandleByIndex(0)
    has_gpu = True
except Exception as e:
    print(f"Предупреждение: NVIDIA GPU не обнаружен: {e}")
    has_gpu = False

def handle_client(conn):
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(1024)
            if not chunk:
                return
            req += chunk

        if b"GET /sse" not in req:
            conn.sendall(b"HTTP/1.1 404 Not Found\r\n\r\n")
            return

        real_cpu_util = psutil.cpu_percent(interval=0.1)
        
        # Формула: от MIN до MAX в зависимости от загрузки
        fake_freq = int(MIN_CPU_FREQ + (real_cpu_util / 100.0) * (MAX_CPU_FREQ - MIN_CPU_FREQ))

        ram_usage = psutil.virtual_memory().percent
        ram_used_mb = psutil.virtual_memory().used / (1024**2)

        if has_gpu:
            try:
                gpu_util = nvmlDeviceGetUtilizationRates(gpu_handle).gpu
                gpu_temp = nvmlDeviceGetTemperature(gpu_handle, NVML_TEMPERATURE_GPU)
                gpu_power = nvmlDeviceGetPowerUsage(gpu_handle) / 1000.0
                vram_info = nvmlDeviceGetMemoryInfo(gpu_handle)
                
                vram_total_mb = vram_info.total / (1024**2)
                vram_used_mb_val = vram_info.used / (1024**2)
                vram_free_mb_val = vram_total_mb - vram_used_mb_val
                
                vram_util = (vram_info.used / vram_info.total) * 100
            except Exception:
                gpu_util, gpu_temp, gpu_power, vram_util = 0, 0, 0.0, 0
                vram_total_mb, vram_used_mb_val, vram_free_mb_val = 0.0, 0.0, 0.0
        else:
            gpu_util, gpu_temp, gpu_power, vram_util = 0, 0, 0.0, 0
            vram_total_mb, vram_used_mb_val, vram_free_mb_val = 0.0, 0.0, 0.0

        headers = (
            "HTTP/1.1 200 OK\n"
            "Content-Type: text/event-stream\n"
            "Cache-Control: no-cache\n"
            "Access-Control-Allow-Origin: *\n"
            "Access-Control-Expose-Headers: *\n"
            "Access-Control-Allow-Credentials: true\n"
            "\n"
        )

        payload = (
            f"data: Page0|{{|}}"
            f"Simple1|F_CPU: {fake_freq} MHz{{|}}"
            f"Simple2|UP_CPU: {int(gpu_util)}%{{|}}"
            f"Simple3|S_FAN: 0 RPM{{|}}"
            f"Simple4|UP_RAM: {int(ram_usage)}%{{|}}"
            f"Simple5|U_DRIVE_C: {int(vram_used_mb_val)} GB{{|}}"
            f"Simple6|T_CPU: {int(gpu_temp)}\xd0\x8e\xd0\xb6{{|}}"
            f"Simple8|UP_DRIVE_C: {int(vram_util)}%{{|}}"
            f"Simple9|T_DRIVE_C: 0\xd0\x8e\xd0\xb6{{|}}"
            f"Simple10|FR_DRIVE_C: {int(vram_free_mb_val)} GB{{|}}"
            f"Simple11|U_RAM: {int(ram_used_mb)} MB{{|}}"
            f"Simple12|TIME: {time.strftime('%H:%M')}{{|}}"
            f"Simple13|GPU1: {int(vram_used_mb_val)} MB{{|}}"
            f"Simple14|GPU2: 0 MB{{|}}"
            f"Simple15|PW_CPU: {gpu_power:.2f} W{{|}}"
            f"Simple16|UP_GPU: {int(gpu_util)}%{{|}}"
            f"\n\n"
        )

        full_packet = (headers + payload).encode('latin-1')
        conn.sendall(full_packet)
        
    except Exception:
        pass
    finally:
        conn.close()

class MonitorServer(threading.Thread):
    def __init__(self, port):
        super().__init__()
        self.port = port
        self.running = False
        self.server_socket = None
        self.daemon = True

    def run(self):
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.server_socket.bind(('', self.port))
            self.server_socket.listen(5)
            self.server_socket.settimeout(1.0) 
            print(f"Server started on port {self.port}")
            print(f"Detected Max CPU Freq: {int(MAX_CPU_FREQ)} MHz")
        except Exception as e:
            print(f"Failed to bind port {self.port}: {e}")
            self.running = False
            return

        while self.running:
            try:
                conn, addr = self.server_socket.accept()
                threading.Thread(target=handle_client, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                if self.running:
                    pass

        if self.server_socket:
            self.server_socket.close()

    def stop(self):
        self.running = False

class TrayApp:
    def __init__(self, port):
        self.port = port
        self.server = MonitorServer(self.port)
        self.icon = pystray.Icon(
            "GM_monitor", 
            self.create_image(), 
            "GeekMagic Monitor", 
            menu=self.create_menu()
        )

    def create_image(self):
        for ext in ['png', 'ico']:
            icon_path = f"icon.{ext}"
            if os.path.exists(icon_path):
                try:
                    return Image.open(icon_path)
                except Exception:
                    pass
        
        image = Image.new('RGB', (64, 64), color=(30, 30, 30))
        d = ImageDraw.Draw(image)
        d.rectangle([16, 16, 48, 48], outline=(0, 255, 255), width=4)
        d.text((24, 24), "GM", fill=(0, 255, 255))
        return image

    def create_menu(self):
        return pystray.Menu(
            pystray.MenuItem("Start", self.on_start, enabled=lambda item: not self.server.running),
            pystray.MenuItem("Stop", self.on_stop, enabled=lambda item: self.server.running),
            pystray.MenuItem("Exit", self.on_exit)
        )

    def on_start(self, icon, item):
        if not self.server.running:
            self.server = MonitorServer(self.port)
            self.server.start()

    def on_stop(self, icon, item):
        if self.server.running:
            self.server.stop()

    def on_exit(self, icon, item):
        self.server.stop()
        icon.stop()

    def run(self):
        self.server.start()
        self.icon.run()

if __name__ == "__main__":
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass

    print("GeekMagic monitor starting...")
    print(f"Port set to {port}.")
    print(f"To change the port number use GM_monitor.exe <Port>")
    
    app = TrayApp(port)
    app.run()