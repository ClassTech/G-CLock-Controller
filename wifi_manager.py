# File: wifi_manager.py (optimized)
# Compile with: mpy-cross -O1 wifi_manager.py
# Using -O1 instead of -O2 for networking code to be safer

import network
import time
import gc

def connectWifi(ssid, password, hostname="pendulum-clock", timeout=15):
    """
    Connect to Wi-Fi network with optimized memory usage
    """
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    # --- CRITICAL FIX: Disable WiFi Power Save Mode ---
    try:
        wlan.config(pm=0xa11140) 
    except:
        pass 

    try:
        wlan.config(dhcp_hostname=hostname)
    except (TypeError, AttributeError):
        pass

    if not wlan.isconnected():
        print(f"Connecting to Wi-Fi SSID: {ssid}...")
        wlan.connect(ssid, password, hostname)

        start_time = time.time()
        dot_count = 0
        
        while not wlan.isconnected():
            if time.time() - start_time > timeout:
                print("\nWi-Fi connection timed out.")
                wlan.active(False)  # Turn off to save power
                return None
                
            time.sleep(1)
            print(".", end="")
            
            dot_count += 1
            if dot_count % 10 == 0:
                gc.collect()
                
        print() 

    if wlan.isconnected():
        ip_addr = wlan.ifconfig()[0]
        print(f"Wi-Fi connected. IP Address: {ip_addr}")
        gc.collect()
    
    return wlan