# File: pendulum_controller.py
# Main controller logic - compile with: mpy-cross -O2 pendulum_controller.py

import _thread
import time
import ntptime
from machine import Pin, WDT
import ucollections
import ujson
import gc
import esp32

# --- Constants ---
# DRV8833 bipolar driver: [AIN1, AIN2, BIN1, BIN2] → GPIO3-6
STEPPER_PINS = [Pin(3, Pin.OUT), Pin(4, Pin.OUT), Pin(5, Pin.OUT), Pin(6, Pin.OUT)]

STEPS_PER_INCH = 6400
STEP_DELAY_MS = 10
MAX_TOTAL_TRAVEL_INCH = 0.2
# Full-step dual-phase sequence for bipolar stepper via DRV8833 [AIN1, AIN2, BIN1, BIN2]
# Both coils energized every step — maximum torque, matches STEPS_PER_INCH = 6400
STEP_SEQUENCE = [
    [1, 0, 1, 0],
    [0, 1, 1, 0],
    [0, 1, 0, 1],
    [1, 0, 0, 1],
]

IR_SENSOR_PIN = 21

# BEAT FILTERING
# 1. Debounce: Ignore re-triggers within this window after a valid swing (0.4s)
DEBOUNCE_US = 400000
# 2. Min Valid Beat: Intervals shorter than this are ignored
MIN_VALID_BEAT_S = 0.85
# 3. Max Valid Beat: beats longer than this are considered "missed beats"
MAX_VALID_BEAT_S = 1.35

EXPECTED_BEAT_PERIOD = 1.00
STATE_FILE = "pendulum_state.json"

def _is_dst(utc_secs):
    t = time.localtime(utc_secs)
    month, mday, hour = t[1], t[2], t[3]
    if month < 3 or month > 11:
        return False
    if 3 < month < 11:
        return True
    # Second Sunday of March (start) / First Sunday of November (end)
    year = t[0]
    if month == 3:
        dow_mar1 = time.localtime(time.mktime((year, 3, 1, 0, 0, 0, 0, 0)))[6]
        second_sun = 1 + (6 - dow_mar1) % 7 + 7
        return mday > second_sun or (mday == second_sun and hour >= 2)
    # month == 11
    dow_nov1 = time.localtime(time.mktime((year, 11, 1, 0, 0, 0, 0, 0)))[6]
    first_sun = 1 + (6 - dow_nov1) % 7
    return mday < first_sun or (mday == first_sun and hour < 2)

def _central_time():
    utc = time.time()
    offset = 5 if _is_dst(utc) else 6
    return time.localtime(utc - offset * 3600)

# Reduced buffer sizes for memory optimization
LOG_BUFFER_SIZE = 50  
BEAT_HISTORY_SIZE = 40  
TICK_TOCK_SIZE = 20     

class PendulumController:
    def __init__(self):
        self.pendulum_state = {}
        self.log_buffer = []
        self.wifi_ssid = ""
        self.wifi_password = ""
        self.hostname = "pendulum-clock"
        self.ir_pin = None
        self._motor_stop_utc = 0
        self.tick_event_queue = ucollections.deque((), 20)
        self.last_state_save_utc = 0
        self.last_correction_hour = -1
        self.last_drift_calc_utc = 0
        self.last_valid_beat_time = time.time()
        
    def log_msg(self, msg):
        ts = _central_time()
        full_msg = f"[{ts[1]:02}-{ts[2]:02} {ts[3]:02}:{ts[4]:02}:{ts[5]:02}] {msg}"
        print(full_msg)
        self.log_buffer.append(full_msg)
        if len(self.log_buffer) > LOG_BUFFER_SIZE:
            self.log_buffer.pop(0)
        if len(self.log_buffer) % 10 == 0:
            gc.collect()

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                history_list = list(self.pendulum_state["hourlyHistory"])
                persistent_state = {
                    "currPosIn": self.pendulum_state["currPosIn"],
                    "kp": self.pendulum_state["kp"],
                    "ki": self.pendulum_state["ki"],
                    "hourlyHistory": history_list,
                    "dotHistory": list(self.pendulum_state["dotHistory"]),
                }
                ujson.dump(persistent_state, f)
                self.log_msg("STATE: Periodic state save complete.")
        except Exception as e:
            self.log_msg(f"ERROR: Could not save state: {e}")

    def load_state(self):
        hourly_history_deque = ucollections.deque((), 168)
        self.pendulum_state = {
            "currPosIn": 0.0, "moveRequest": 0.0, "lastSwingTimeUs": 0, "swingCount": 0,
            "last60Beats": [], "last30Ticks": [], "last30Tocks": [], "avgBeatTime": 0.0,
            "totalDriftS": 0.0, "correctionActive": True, "stepperPosition": 0,
            "tickTockDiff": 0.0, "timingStartUtc": 0, "lastCorrectionUtc": 0,
            "elapsedTimeStr": "0d 0h 0m 0s", "driftSph": 0.0, "driftHistory": [0] * 10,
            "lastMinuteSwingCount": 0, "lastHourlySwingCount": 0, "missedBeats": 0,
            "kp": 0.005, "ki": 0.001, "hourlyHistory": hourly_history_deque,
            "lastRateError": 0.0,
            "tickCount": 0,
            "watchdogTriggered": False,
            "stepsRemaining": 0, "stepDir": 1,
            "dotWindowPending": False, "dotWindowStartMs": 0, "dotWindowStartSwing": -1,
            "dotWindowTickSum": 0.0, "dotWindowTickCount": 0,
            "dotWindowTockSum": 0.0, "dotWindowTockCount": 0,
            "dotHistory": ucollections.deque((), 50),
        }
        try:
            with open(STATE_FILE, "r") as f:
                loaded = ujson.load(f)
                self.pendulum_state["currPosIn"] = loaded.get("currPosIn", 0.0)
                self.pendulum_state["kp"] = loaded.get("kp", 0.0002)
                self.pendulum_state["ki"] = loaded.get("ki", 0.00002)
                loaded_history = loaded.get("hourlyHistory", [])
                for entry in loaded_history:
                    self.pendulum_state["hourlyHistory"].append(entry)
                loaded_dot = loaded.get("dotHistory", [])
                for entry in loaded_dot:
                    self.pendulum_state["dotHistory"].append(entry)
                self.log_msg(f"State loaded. Restored {len(loaded_history)} hourly and {len(loaded_dot)} dot history points.")
        except (OSError, ValueError):
            self.log_msg("State file not found/corrupt. Initializing with default state.")

    def _step_once(self):
        remaining = self.pendulum_state["stepsRemaining"]
        if remaining <= 0:
            return False
        p0, p1, p2, p3 = STEPPER_PINS
        pos = (self.pendulum_state["stepperPosition"] + self.pendulum_state["stepDir"]) % 4
        s = STEP_SEQUENCE[pos]
        p0.value(s[0])
        p1.value(s[1])
        p2.value(s[2])
        p3.value(s[3])
        self.pendulum_state["stepperPosition"] = pos
        remaining -= 1
        self.pendulum_state["stepsRemaining"] = remaining
        if remaining <= 0:
            # Brake mode: short both outputs to GND so coil current recirculates
            # through motor resistance instead of spiking VM via flyback diodes.
            p0.value(1)
            p1.value(1)
            p2.value(1)
            p3.value(1)
            time.sleep_ms(10)
            p0.value(0)
            p1.value(0)
            p2.value(0)
            p3.value(0)
            self.log_msg(f"MOVE: Complete. Position: {self.pendulum_state['currPosIn']:.6f} in.")
            # Clip disturbance: reset beat reference so the gap isn't counted as drift
            self.pendulum_state["lastSwingTimeUs"] = 0
            self._motor_stop_utc = time.time()
        return remaining > 0

    def swing_interrupt_handler(self, pin):
        try:
            self.tick_event_queue.append(time.ticks_us())
        except:
            pass

    def handle_tick_processing(self):
        while len(self.tick_event_queue) > 0:
            current_time = self.tick_event_queue.popleft()
            
            # Initialization case
            if self.pendulum_state["lastSwingTimeUs"] == 0:
                self.pendulum_state["lastSwingTimeUs"] = current_time
                continue
                
            interval = time.ticks_diff(current_time, self.pendulum_state["lastSwingTimeUs"])
            
            # 1. HARD DEBOUNCE: Ignore re-triggers within debounce window
            if interval < DEBOUNCE_US:
                continue
            
            beat_duration = interval / 1_000_000.0
            is_valid_beat = False
            
            # 2. WINDOW FILTERING
            # If duration is 0.4s - 0.85s, it falls through here. 
            # We ignore it AND we do NOT update lastSwingTimeUs.
            
            if MIN_VALID_BEAT_S < beat_duration < MAX_VALID_BEAT_S:
                # --- VALID BEAT (0.85s - 1.35s) ---
                beats = self.pendulum_state["last60Beats"]
                beats.append(beat_duration)
                if len(beats) > BEAT_HISTORY_SIZE:
                    beats.pop(0)
                
                # Tick/Tock separation
                if self.pendulum_state["swingCount"] % 2 == 0:
                    ticks = self.pendulum_state["last30Ticks"]
                    ticks.append(beat_duration)
                    if len(ticks) > TICK_TOCK_SIZE: ticks.pop(0)
                else:
                    tocks = self.pendulum_state["last30Tocks"]
                    tocks.append(beat_duration)
                    if len(tocks) > TICK_TOCK_SIZE: tocks.pop(0)

                self.pendulum_state["swingCount"] += 1
                
                # Update Watchdog Counters
                self.pendulum_state["tickCount"] = self.pendulum_state.get("tickCount", 0) + 1
                self.pendulum_state["watchdogTriggered"] = False
                self.last_valid_beat_time = time.time()

                ps = self.pendulum_state
                if ps["dotWindowStartSwing"] >= 0:
                    if ps["swingCount"] % 2 == 1:
                        ps["dotWindowTickSum"] += beat_duration
                        ps["dotWindowTickCount"] += 1
                    else:
                        ps["dotWindowTockSum"] += beat_duration
                        ps["dotWindowTockCount"] += 1
                    if ps["swingCount"] - ps["dotWindowStartSwing"] >= 3550:
                        ps["dotWindowStartSwing"] = -1
                        elapsed_ms = time.ticks_diff(time.ticks_ms(), ps["dotWindowStartMs"])
                        tc = ps["dotWindowTickCount"]
                        tok = ps["dotWindowTockCount"]
                        avg_tt = 0.0
                        if tc > 0 and tok > 0:
                            avg_tt = (ps["dotWindowTickSum"] / tc - ps["dotWindowTockSum"] / tok) * 1000.0
                        try:
                            temp = esp32.mcu_temperature() * 9 / 5 + 32
                        except:
                            temp = None
                        entry = {"ts": time.time(), "elapsed": elapsed_ms / 1000.0, "avgTT": avg_tt, "pos": ps["currPosIn"]}
                        if temp is not None:
                            entry["temp"] = temp
                        ps["dotHistory"].append(entry)
                        self.log_msg(f"DOT: {elapsed_ms/1000.0:.3f}s elapsed, avgTT:{avg_tt:.3f}ms")

                is_valid_beat = True
                
            elif beat_duration >= MAX_VALID_BEAT_S:
                # --- MISSED BEAT LOGIC (> 1.35s) ---
                # Only check up to 10 seconds to avoid massive jumps on reboot
                if beat_duration < 10.0:
                    num_beats = int(round(beat_duration / EXPECTED_BEAT_PERIOD))
                    missed_count = num_beats - 1
                    if missed_count > 0:
                        self.pendulum_state["missedBeats"] += missed_count
                        self.pendulum_state["swingCount"] += num_beats
                        self.log_msg(f"WARNING: Detected {missed_count} missed beats (Duration: {beat_duration:.2f}s).")
                    is_valid_beat = True
            
            # CRITICAL: Only update the reference time if it was a REAL beat.
            # If it was noise (0.4s - 0.85s), we keep the old reference time!
            if is_valid_beat: 
                self.pendulum_state["lastSwingTimeUs"] = current_time

    def format_timespan(self, seconds):
        days, r = divmod(seconds, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return f"{int(days)}d {int(h)}h {int(m)}m {int(s)}s"

    def handle_stepper_motor(self):
        if self.pendulum_state["moveRequest"] != 0.0:
            move_in = self.pendulum_state["moveRequest"]
            steps = int(move_in * STEPS_PER_INCH)
            self.pendulum_state["moveRequest"] = 0.0
            if steps == 0:
                return
            self.log_msg(f"MOVE: {move_in:.6f} in -> {steps} steps.")
            self.pendulum_state["stepDir"] = -1 if steps > 0 else 1
            self.pendulum_state["stepsRemaining"] = abs(steps)
            self.pendulum_state["currPosIn"] += steps / STEPS_PER_INCH

    def handle_hourly_tasks(self, current_tuple, current_utc):
        current_hour = current_tuple[3] 
        if current_hour == self.last_correction_hour:
            return

        self.last_correction_hour = current_hour
        try: 
            ntptime.settime()
            self.log_msg("NTP: Sync successful.")
        except Exception as e: 
            self.log_msg(f"NTP: Sync failed: {e}.")
        current_utc = time.time()
        
        seconds_in_period = current_utc - self.pendulum_state["lastCorrectionUtc"]
        if seconds_in_period < 1800:
            self.log_msg("CORRECTION: Period too short, skipping hourly tasks.")
            return

        rate_error = 0.0
        if self.pendulum_state["swingCount"] > self.pendulum_state["lastHourlySwingCount"]:
            swings_in_period = self.pendulum_state["swingCount"] - self.pendulum_state["lastHourlySwingCount"]
            rate_error = swings_in_period - seconds_in_period
        self.pendulum_state["lastRateError"] = rate_error

        if self.pendulum_state["correctionActive"]:
            p_move = rate_error * self.pendulum_state.get('kp', 0.005)
            i_move = self.pendulum_state["totalDriftS"] * self.pendulum_state.get('ki', 0.001)
            self.log_msg(f"CORRECTION: Total Error: {self.pendulum_state['totalDriftS']:.2f}s. Rate Error: {rate_error:.2f} swings/hr.")
            
            calculated_move = p_move + i_move
            clamped_pos = max(-MAX_TOTAL_TRAVEL_INCH, min(MAX_TOTAL_TRAVEL_INCH, self.pendulum_state["currPosIn"] + calculated_move))
            self.pendulum_state["moveRequest"] = clamped_pos - self.pendulum_state["currPosIn"]

        self.handle_stepper_motor()

        snapshot = {
            "ts": current_utc,
            "pos": self.pendulum_state["currPosIn"],
            "drift": self.pendulum_state["totalDriftS"],
            "rate": self.pendulum_state["lastRateError"]
        }
        self.pendulum_state["hourlyHistory"].append(snapshot)
        self.save_state()
        self.pendulum_state["lastCorrectionUtc"] = current_utc
        self.pendulum_state["lastHourlySwingCount"] = self.pendulum_state["swingCount"]
        gc.collect()
        self.log_msg(f"MEM: {gc.mem_free()} bytes free")
        self.pendulum_state["dotWindowPending"] = True

    def handle_rolling_drift_calc(self, current_utc):
        if current_utc - self.last_drift_calc_utc >= 60:
            index = time.localtime(current_utc)[4] % 10
            swings = self.pendulum_state["swingCount"] - self.pendulum_state["lastMinuteSwingCount"]
            self.pendulum_state["driftHistory"][index] = swings
            self.pendulum_state["lastMinuteSwingCount"] = self.pendulum_state["swingCount"]
            valid = [s for s in self.pendulum_state["driftHistory"] if s > 0]
            
            if len(valid) > 1:
                self.pendulum_state["driftSph"] = ((sum(valid) / len(valid)) - 60.0) * 60
            else:
                self.pendulum_state["driftSph"] = 0.0
            
            self.last_drift_calc_utc = current_utc

    def handle_continuous_updates(self, current_utc):
        # Watchdog: 12 seconds
        if self.pendulum_state["correctionActive"] and not self.pendulum_state.get("watchdogTriggered", False):
            if current_utc - self.last_valid_beat_time > 12.0:
                self.log_msg("EMERGENCY: 10+ missed beats detected. Auto-correction DISABLED.")
                self.pendulum_state["correctionActive"] = False
                self.pendulum_state["watchdogTriggered"] = True

        if self.pendulum_state["timingStartUtc"] > 0:
            self.pendulum_state["totalDriftS"] = self.pendulum_state["swingCount"] - (current_utc - self.pendulum_state["timingStartUtc"])
        
        if self.pendulum_state["last60Beats"]:
            self.pendulum_state["avgBeatTime"] = sum(self.pendulum_state["last60Beats"]) / len(self.pendulum_state["last60Beats"])

        ticks = self.pendulum_state["last30Ticks"]
        tocks = self.pendulum_state["last30Tocks"]
        if len(ticks) > 0 and len(tocks) > 0:
            avg_tick = sum(ticks) / len(ticks)
            avg_tock = sum(tocks) / len(tocks)
            self.pendulum_state["tickTockDiff"] = (avg_tick - avg_tock) * 1000.0

        self.pendulum_state["elapsedTimeStr"] = self.format_timespan(current_utc - self.pendulum_state["timingStartUtc"])

    def main_loop(self):
        self.log_msg("Main loop started.")
        self.last_drift_calc_utc = time.time()
        wdt = WDT(timeout=30000)
        while True:
            try:
                wdt.feed()
                self.handle_tick_processing()
                current_utc = time.time()
                current_tuple = time.localtime(current_utc)
                
                self.handle_continuous_updates(current_utc)
                self.handle_rolling_drift_calc(current_utc)
                self.handle_hourly_tasks(current_tuple, current_utc)
                
                if self.pendulum_state["moveRequest"] != 0.0:
                    self.handle_stepper_motor()

                if self._step_once():
                    time.sleep_ms(STEP_DELAY_MS)
                else:
                    if self.pendulum_state.get("dotWindowPending", False):
                        ps = self.pendulum_state
                        ps["dotWindowPending"] = False
                        ps["dotWindowStartMs"] = time.ticks_ms()
                        ps["dotWindowStartSwing"] = ps["swingCount"]
                        ps["dotWindowTickSum"] = 0.0
                        ps["dotWindowTickCount"] = 0
                        ps["dotWindowTockSum"] = 0.0
                        ps["dotWindowTockCount"] = 0
                        self.log_msg("DOT: Window started.")
                    time.sleep_ms(20)
                
            except Exception as e:
                self.log_msg(f"ERROR in main loop: {e}")
                time.sleep_ms(100)

    def load_wifi_config(self):
        try:
            with open("wifi.config", "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key == "ssid":
                        self.wifi_ssid = value
                    elif key == "password":
                        self.wifi_password = value
                    elif key == "hostname":
                        self.hostname = value
            self.log_msg("WIFI: Config loaded.")
        except OSError:
            self.log_msg("ERROR: wifi.config not found. WiFi will not connect.")

    def run(self):
        self.log_msg("--- Pendulum Regulator Initializing ---")
        self.load_state()
        self.load_wifi_config()

        import wifi_manager
        import webserver

        while True:
            wlan = wifi_manager.connectWifi(self.wifi_ssid, self.wifi_password, hostname=self.hostname)
            if wlan and wlan.isconnected():
                self.log_msg("WIFI: Connection successful.")
                break
            self.log_msg("WIFI: Connection failed. Retrying in 30 seconds...")
            time.sleep(30)

        self.log_msg("TIME: Waiting for NTP time synchronization...")
        while True:
            try:
                ntptime.settime()
                if time.localtime()[0] < 2024:
                    raise ValueError("Time is not yet valid.")
                self.log_msg(f"TIME: NTP sync successful. Current time: {time.localtime()}")
                break
            except Exception as e:
                self.log_msg(f"TIME: NTP sync failed: {e}. Retrying in 30 seconds...")
                time.sleep(30)
                
        self.last_valid_beat_time = time.time()
        now = time.time()
        if self.pendulum_state["timingStartUtc"] == 0:
            self.pendulum_state["timingStartUtc"] = now
        self.pendulum_state["lastCorrectionUtc"] = now
        self.last_correction_hour = time.localtime(now)[3]

        _thread.start_new_thread(webserver.runServer, 
                               (self.pendulum_state, self.log_buffer, self.log_msg, self.save_state))
        
        self.ir_pin = Pin(IR_SENSOR_PIN, Pin.IN, Pin.PULL_UP)
        self.ir_pin.irq(trigger=Pin.IRQ_FALLING, handler=self.swing_interrupt_handler)
        self.log_msg(f"IR swing detector initialized on Pin {IR_SENSOR_PIN} (PULL_UP, active LOW).")
        
        gc.collect()
        self.log_msg(f"Free memory: {gc.mem_free()} bytes")
        
        self.main_loop()