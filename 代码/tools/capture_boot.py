"""
capture_boot.py — 抓取 Claude HUD 固件的启动日志，用于验证烧录是否真正生效。

用法:
    python capture_boot.py [端口]    默认 COM7

会做的事:
  1. 用 DTR/RTS 时序把 ESP32-C3 复位进下载模式再放开，等价于按一次 RESET
  2. 读 6 秒串口输出
  3. 把全文原样打印 + 写入 boot.log
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
BAUD = 115200

ser = serial.Serial(PORT, BAUD, timeout=0.2)
ser.dtr = False
ser.rts = True
time.sleep(0.1)
ser.rts = False
time.sleep(0.1)
ser.dtr = True
time.sleep(0.05)

print(f"[*] 抓取 {PORT} @ {BAUD}，6 秒...")
chunks = []
deadline = time.time() + 6.0
while time.time() < deadline:
    data = ser.read(4096)
    if data:
        chunks.append(data)
ser.close()

raw = b"".join(chunks)
text = raw.decode("utf-8", errors="replace")
print("─" * 60)
print(text if text.strip() else "(没有任何输出 —— 说明 USB CDC On Boot 没开，或设备没复位)")
print("─" * 60)

with open("boot.log", "w", encoding="utf-8") as f:
    f.write(text)

# ── 自动判读 ──────────────────────────────────────────────
checks = {
    "固件已启动":     "ready" in text,
    "LittleFS 挂载":  "store=1" in text,
    "BLE 已起服务":   "BLE" in text.upper(),
    "无崩溢/异常":    "Guru Meditation" not in text and "rst:0x" not in text.lower(),
}
print("\n自动判读:")
for name, ok in checks.items():
    print(f"  {'[OK]  ' if ok else '[FAIL]'} {name}")

if not checks["固件已启动"]:
    print("\n没有看到 'ready'。最可能的原因：")
    print("  1. tools -> USB CDC On Boot 不是 Enabled（最常见）")
    print("  2. 波特率串口监视器设的不是 115200")
    print("  3. 板卡选的不是 ESP32C3 Dev Module")
