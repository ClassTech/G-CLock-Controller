# File: pendulum_controller.py
# Main controller logic - compile with: mpy-cross -O2 pendulum_controller.py

import _thread
import time
import ntptime
from machine import Pin
import ucollections
import ujson
import gc

# --- Constants ---
# CORRECTED PIN MAPPING (XIAO ESP32C3)
STEPPER_PINS = [Pin(2, Pin.OUT), Pin(3, Pin.OUT), Pin(4, Pin.OUT), Pin(5, Pin.OUT)]

STEPS_PER_INCH = 6400
STEP_DELAY_MS = 3
MAX_TOTAL_TRAVEL_INCH = 0.2
STEP_SEQUENCE = [[1, 0, 0, 1], [0, 1, 0, 1], [0, 1, 1, 0], [1, 0, 1, 0]]

# CORRECTED PIEZO PIN
PIEZO_PIN = 21

# REQUESTED CHANGE: Debounce set to 0.7 seconds
DEBOUNCE_US = 700000 
EXPECTED_BEAT_PERIOD = 0.95
STATE_FILE = "pendulum_state.json"

# Reduced buffer sizes for memory optimization
LOG_BUFFER_SIZE = 50  
BEAT_HISTORY_SIZE = 40  
TICK_TOCK_SIZE = 20     

class PendulumController:
    def __init__(self):
        self.pendulum_state = {}
        self.log_buffer = []
        self.tick_event_queue = ucollections.deque((), 20)
        self.last_state_save_utc = 0
        self.last_correction_hour = -1
        self.last_drift_calc_utc = 0
        # Initialize with current time (likely 2000-01-01), will be reset after NTP
        self.last_valid_beat_time = time.time()
        
    def log_msg(self, msg):
        """Optimized logging with memory management"""
        ts = time.localtime()
        full_msg = f"[{ts[0]:04}-{ts[1]:02}-{ts[2]:02} {ts[3]:02}:{ts[4]:02}:{ts[5]:02}] {msg}"
        print(full_msg)
        
        self.log_buffer.append(full_msg)
        if len(self.log_buffer) > LOG_BUFFER_SIZE:
            self.log_buffer.pop(0)
        
        # Periodic garbage collection
        if len(self.log_buffer) % 10 == 0:
            gc.collect()

    def save_state(self):
        """Save persistent state to JSON file"""
        try:
            with open(STATE_FILE, "w") as f:
                history_list = list(self.pendulum_state["hourlyHistory"])
                persistent_state = {
                    "currPosIn": self.pendulum_state["currPosIn"],
                    "kp": self.pendulum_state["kp"],
                    "ki": self.pendulum_state["ki"],
                    "hourlyHistory": history_list
                }
                ujson.dump(persistent_state, f)
                self.log_msg("STATE: Periodic state save complete.")
        except Exception as e:
            self.log_msg(f"ERROR: Could not save state: {e}")

    def load_state(self):
        """Load initial state from JSON or set defaults"""
        hourly_history_deque = ucollections.deque((), 168)

        self.pendulum_state = {
            "currPosIn": 0.0, "moveRequest": 0.0, "lastSwingTimeUs": 0, "swingCount": 0,
            "last60Beats": [], "last30Ticks": [], "last30Tocks": [], "avgBeatTime": 0.0,
            "totalDriftS": 0.0, "correctionActive": True, "stepperPosition": 0,
            "tickTockDiff": 0.0, "timingStartUtc": 0, "lastCorrectionUtc": 0,
            "elapsedTimeStr": "0d 0h 0m 0s", "driftSph": 0.0, "driftHistory": [0] * 10,
            "lastMinuteSwingCount": 0, "lastHourlySwingCount": 0, "missedBeats": 0,
            "kp": 0.0002, "ki": 0.00002, "hourlyHistory": hourly_history_deque,
            "lastRateError": 0.0,
            "tickCount": 0,          # REQUESTED: Counter for UI light
            "watchdogTriggered": False # REQUESTED: Flag for safety stop
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
                self.log_msg(f"State loaded. Restored position, tuning, and {len(loaded_history)} history points.")
        except (OSError, ValueError):
            self.log_msg("State file not found/corrupt. Initializing with default state.")

    def move_stepper(self, steps):
        """Move stepper motor by specified number of steps"""
        direction = 1 if steps > 0 else -1
        for _ in range(abs(steps)):
            self.pendulum_state["stepperPosition"] = (self.pendulum_state["stepperPosition"] + direction) % 4
            pattern = STEP_SEQUENCE[self.pendulum_state["stepperPosition"]]
            for i in range(4): 
                STEPPER_PINS[i].value(pattern[i])
            time.sleep_ms(STEP_DELAY_MS)
        for i in range(4): 
            STEPPER_PINS[i].value(0)

    def piezo_interrupt_handler(self, pin):
        """Interrupt handler for piezo sensor"""
        try:
            self.tick_event_queue.append(time.ticks_us())
        except:
            # Queue full - oldest event will be lost
            pass

    def handle_tick_processing(self):
        """Process all pending tick events"""
        while len(self.tick_event_queue) > 0:
            current_time = self.tick_event_queue.popleft()
            if self.pendulum_state["lastSwingTimeUs"] == 0:
                self.pendulum_state["lastSwingTimeUs"] = current_time
                continue
                
            interval = time.ticks_diff(current_time, self.pendulum_state["lastSwingTimeUs"])
            if interval < DEBOUNCE_US: 
                continue
            
            # --- Valid Beat Detected ---
            beat_duration = interval / 1_000_000.0
            is_beat_processed = False
            
            if 0.7 < beat_duration < 1.8:
                # Valid beat logic
                beats = self.pendulum_state["last60Beats"]
                beats.append(beat_duration)
                if len(beats) > BEAT_HISTORY_SIZE:
                    beats.pop(0)
                
                if self.pendulum_state["swingCount"] % 2 == 0:
                    ticks = self.pendulum_state["last30Ticks"]
                    ticks.append(beat_duration)
                    if len(ticks) > TICK_TOCK_SIZE:
                        ticks.pop(0)
                else:
                    tocks = self.pendulum_state["last30Tocks"]
                    tocks.append(beat_duration)
                    if len(tocks) > TICK_TOCK_SIZE:
                        tocks.pop(0)

                self.pendulum_state["swingCount"] += 1
                
                # REQUESTED: Update tick counter and reset watchdog
                self.pendulum_state["tickCount"] = self.pendulum_state.get("tickCount", 0) + 1
                self.pendulum_state["watchdogTriggered"] = False
                self.last_valid_beat_time = time.time()
                
                is_beat_processed = True
                
            elif 1.9 <= beat_duration < 10.0:
                num_beats = int(round(beat_duration / EXPECTED_BEAT_PERIOD))
                missed_count = num_beats - 1
                if missed_count > 0:
                    self.pendulum_state["missedBeats"] += missed_count
                    self.pendulum_state["swingCount"] += num_beats
                    self.log_msg(f"WARNING: Detected {missed_count} missed beats.")
                is_beat_processed = True
                
            if is_beat_processed: 
                self.pendulum_state["lastSwingTimeUs"] = current_time

    def format_timespan(self, seconds):
        """Format seconds into readable string"""
        days, r = divmod(seconds, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return f"{int(days)}d {int(h)}h {int(m)}m {int(s)}s"

    def handle_stepper_motor(self):
        """Handle any pending stepper motor moves"""
        if self.pendulum_state["moveRequest"] != 0.0:
            move_in = self.pendulum_state["moveRequest"]
            steps = int(move_in * STEPS_PER_INCH)
            self.log_msg(f"MOVE: {move_in:.6f} in -> {steps} steps.")
            self.move_stepper(steps)
            self.pendulum_state["currPosIn"] += steps / STEPS_PER_INCH
            self.pendulum_state["moveRequest"] = 0.0
            self.log_msg(f"MOVE: Complete. New position: {self.pendulum_state['currPosIn']:.6f} in.")

    def handle_hourly_tasks(self, current_tuple, current_utc):
        """Handle hourly corrections and data logging"""
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
            p_move = rate_error * self.pendulum_state.get('kp', 0.0002)
            i_move = self.pendulum_state["totalDriftS"] * self.pendulum_state.get('ki', 0.00002)
            self.log_msg(f"CORRECTION: Total Error: {self.pendulum_state['totalDriftS']:.2f}s. Rate Error: {rate_error:.2f} swings/hr.")
            self.log_msg(f"  -> P: {p_move:.6f}in, I: {i_move:.6f}in.")
            
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
        self.log_msg(f"HISTORY: Saved hourly snapshot. History now has {len(self.pendulum_state['hourlyHistory'])} points.")
        
        self.save_state()
        self.pendulum_state["lastCorrectionUtc"] = current_utc
        self.pendulum_state["lastHourlySwingCount"] = self.pendulum_state["swingCount"]
        
        gc.collect()

    def handle_rolling_drift_calc(self, current_utc):
        """Calculate rolling drift estimate"""
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
        """Update continuous calculations"""
        # REQUESTED FEATURE: Watchdog (10 beats = approx 12 seconds)
        # Using 12.0 seconds as the threshold
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
        """Main control loop"""
        self.log_msg("Main loop started.")
        self.last_drift_calc_utc = time.time()
        
        while True:
            try:
                self.handle_tick_processing()
                current_utc = time.time()
                current_tuple = time.localtime(current_utc)
                
                self.handle_continuous_updates(current_utc)
                self.handle_rolling_drift_calc(current_utc)
                self.handle_hourly_tasks(current_tuple, current_utc)
                
                if self.pendulum_state["moveRequest"] != 0.0:
                    self.handle_stepper_motor()
                    self.save_state()

                time.sleep_ms(20)
                
            except Exception as e:
                self.log_msg(f"ERROR in main loop: {e}")
                time.sleep_ms(100)

    def run(self, wifi_ssid, wifi_password, hostname="pendulum-clock"):
        """Main entry point - sets up everything and starts the system"""
        self.log_msg("--- Pendulum Regulator Initializing ---")
        self.load_state()
        
        import wifi_manager
        import webserver
        
        while True:
            wlan = wifi_manager.connectWifi(wifi_ssid, wifi_password, hostname=hostname)
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
                
        # CRITICAL FIX: Reset the watchdog timer NOW, because time just jumped 26 years forward
        self.last_valid_beat_time = time.time()

        now = time.time()
        if self.pendulum_state["timingStartUtc"] == 0: 
            self.pendulum_state["timingStartUtc"] = now
        self.pendulum_state["lastCorrectionUtc"] = now
        self.last_correction_hour = time.localtime(now)[3]

        _thread.start_new_thread(webserver.runServer, 
                               (self.pendulum_state, self.log_buffer, self.log_msg, self.save_state))
        
        piezo_pin = Pin(PIEZO_PIN, Pin.IN, Pin.PULL_UP)
        piezo_pin.irq(trigger=Pin.IRQ_FALLING, handler=self.piezo_interrupt_handler)
        self.log_msg(f"Piezo tick detector initialized on Pin {PIEZO_PIN} (with PULL_UP).")
        
        gc.collect()
        self.log_msg(f"Free memory: {gc.mem_free()} bytes")
        
        self.main_loop()