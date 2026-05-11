Here is the updated `README.md` with the correction regarding the adjustment mechanism.

-----

# ESP32 Pendulum Clock Regulator

This project implements an IoT-based regulation system for mechanical pendulum clocks (e.g., grandfather/longcase clocks). It uses a MicroPython-based ESP32 controller to monitor the clock's beat via an IR sensor, calculate drift against an NTP time server, and physically adjust the effective length of the pendulum by moving a clip up and down the suspension spring using a stepper motor.

## Features

  * **Precision Timing:** Detects pendulum swings using an IR sensor interrupt.
  * **Automatic Regulation:** Uses a Proportional-Integral (PI) control loop to adjust the pendulum length to correct rate errors.
  * **Web Dashboard:** Hosted directly on the ESP32 (no external internet required for UI).
      * Live status (Position, Drift, Beat differential).
      * Historical performance charts (Position vs. Drift).
      * Manual control of the stepper motor.
      * Live system logs.
  * **Drift Tracking:** Calculates drift in Seconds Per Hour (SPH) and total accumulated error.
  * **Persistence:** Saves state (position, history, tuning parameters) to `pendulum_state.json` to survive reboots.
  * **OTA Management:** Supports uploading new Python files and restarting via the web interface.

## Hardware Requirements

Based on the pin configurations in `pendulum_controller.py`:

  * **Microcontroller:** ESP32 (running MicroPython).
  * **Sensor:** TCRT5000 IR sensor (signal wire connected to **Pin 21**). The signal goes **LOW** (active low) when the pendulum passes through the beam; the ESP32C3's internal pull-up is enabled. Wiring:
      * Yellow — signal (Pin 21, active LOW)
      * Purple — Vcc (3.3 V via 220 Ω resistor)
      * Blue / Green — GND
  * **Actuator:** Stepper Motor (likely 28BYJ-48 with ULN2003 driver) to drive the suspension spring clip mechanism.
  * **Stepper Wiring:**
      * IN1: Pin 3
      * IN2: Pin 4
      * IN3: Pin 5
      * IN4: Pin 6

## File Structure

  * `boot.py`: Standard MicroPython bootloader.
  * `main.py`: Entry point. Initializes the controller.
  * `wifi_manager.py`: Handles Wi-Fi connection with memory optimization and garbage collection.
  * `pendulum_controller.py`: Core logic. Handles interrupts, stepper movement, drift calculation, and the main control loop.
  * `webserver.py`: A custom, memory-efficient HTTP server handling API requests and serving the frontend.
  * `index.html`: The frontend dashboard (embedded CSS/JS).
  * `pendulum_state.json`: Stores persistent data (current stepper position, history, PI values) and configuration (Wi-Fi credentials, hostname).

## Installation & Setup

1.  **Flash MicroPython:** Ensure your ESP32 is flashed with a recent version of MicroPython.

2.  **Configure Credentials:**
    Edit `pendulum_state.json` and set your Wi-Fi details:

    ```json
    "wifi_ssid": "YOUR_WIFI_SSID",
    "wifi_password": "YOUR_WIFI_PASSWORD",
    "hostname": "pendulum-clock"
    ```

3.  **Upload Files:** Upload all `.py` files and `index.html` to the root of the ESP32.

4.  **Hardware Calibration:**

      * Ensure the stepper motor is mechanically coupled to the suspension spring clip.
      * The system assumes `6400` steps per inch (configurable in `pendulum_controller.py`).

## Usage

### Web Interface

Once running, access the dashboard via your browser at `http://pendulum-clock` (or the IP address printed in the serial console).

  * **Live Status:** Shows the current error in seconds, the beat rate, and the "Tick-Tock" difference (useful for putting the clock "in beat").
  * **Controls:**
      * **Toggle Auto-Correction:** Enable/Disable the PI loop.
      * **Manual Move:** Manually adjust the stepper up or down.
      * **Set Zero:** Define the current stepper position as the "0" reference point.
      * **Reset Timing:** Clears drift history and restarts the timing session.
  * **Tuning:** Adjust the `Kp` (Proportional) and `Ki` (Integral) gains to tune how aggressively the regulator corrects timing errors.

### Memory Management

The system is optimized for the constrained memory of the ESP32:

  * `webserver.py` uses chunked reading for static files.
  * `wifi_manager.py` utilizes aggressive garbage collection (`gc.collect`) during connection.
  * For best stability, you may compile python files to bytecode (`.mpy`) using `mpy-cross` as noted in the file headers.

## API Endpoints

The web server exposes several JSON endpoints:

  * `GET /status`: Returns current telemetry (position, drift, beat info).
  * `GET /history`: Returns hourly historical data for the chart.
  * `GET /log`: Returns recent system logs.
  * `POST /move`: Accepts JSON `{"inches": 0.001}` to move the motor.
  * `POST /updateTuning`: Accepts JSON `{"kp": 0.0002, "ki": 0.00002}`.
  * `POST /toggleCorrections`: Accepts JSON `{"active": true}`.

## License

MIT License 