# Pendulum Clock Regulator — Circuit Documentation

## Power Rails

| Rail | Source | Voltage | Consumers |
|------|--------|---------|-----------|
| +5V  | External DC input | 5.0 V | DRV8833 VM, DRV8833 nSLEEP, XIAO 5V pin |
| +3V3 | XIAO onboard regulator | 3.3 V | IR LED current-limit resistor, IR signal pull-up |
| GND  | Common | 0 V | All |

Power input: direct 5 V DC via connector (type TBD — screw terminal or barrel jack).  
The XIAO ESP32C3's onboard regulator supplies the 3.3 V rail from the 5 V input.

---

## U1 — Seeed XIAO ESP32C3

Primary microcontroller. Accepts 5 V on its VBUS/5V pin; internal LDO produces 3.3 V.

| XIAO Pin | GPIO | Direction | Connected To |
|----------|------|-----------|--------------|
| 5V       | —    | Power in  | +5V rail |
| GND      | —    | Power     | GND |
| 3V3      | —    | Power out | +3V3 rail |
| D3       | GPIO3 | Output   | DRV8833 AIN1 |
| D4       | GPIO4 | Output   | DRV8833 AIN2 |
| D5       | GPIO5 | Output   | DRV8833 BIN1 |
| D6       | GPIO6 | Output   | DRV8833 BIN2 |
| D10      | GPIO21 | Input (PULL_UP) | IR sensor signal (phototransistor collector), C1 |

Internal pull-up on GPIO21 is ~45 kΩ to 3.3 V.

---

## U2 — DRV8833 Dual H-Bridge Motor Driver

Drives both coils of the bipolar stepper motor.

| DRV8833 Pin | Connected To | Notes |
|-------------|--------------|-------|
| VM          | +5V rail     | Motor supply. Decouple with C2 + C3 (see below). |
| GND         | GND          | |
| nSLEEP      | +5V rail     | Pulled HIGH — device always enabled. |
| nFAULT      | Not connected | Open-drain output; leave unconnected. |
| AIN1        | GPIO3        | |
| AIN2        | GPIO4        | |
| BIN1        | GPIO5        | |
| BIN2        | GPIO6        | |
| AOUT1       | Motor J2 pin 1 | Coil A+ |
| AOUT2       | Motor J2 pin 2 | Coil A− |
| BOUT1       | Motor J2 pin 3 | Coil B+ |
| BOUT2       | Motor J2 pin 4 | Coil B− |

---

## J1 — IR Sensor Connector (off-board TCRT5000)

The TCRT5000 is a 4-pin package (IR LED + phototransistor in one body) mounted separately and wired back to the PCB.

| Connector Pin | Wire Color | Signal | PCB Net |
|---------------|------------|--------|---------|
| 1 | Purple | IR LED anode | +3V3 via R1 |
| 2 | Blue/Green | IR LED cathode | GND |
| 3 | Yellow | Phototransistor collector | GPIO21 / Signal net |
| 4 | Blue/Green | Phototransistor emitter | GND |

Connector type: TBD (JST-PH 4-pin recommended for mechanical security).

---

## J2 — Stepper Motor Connector (off-board Oukeda BHY2001-10)

4-wire bipolar stepper. Connector type TBD.

| Connector Pin | Signal | DRV8833 Pin |
|---------------|--------|-------------|
| 1 | Coil A+ | AOUT1 |
| 2 | Coil A− | AOUT2 |
| 3 | Coil B+ | BOUT1 |
| 4 | Coil B− | BOUT2 |

---

## Passives

| Ref | Value | Type | Placement | Purpose |
|-----|-------|------|-----------|---------|
| R1  | 220 Ω | 1/4 W resistor | Between +3V3 and J1 pin 1 | IR LED current limiting (~14 mA at 3.3 V) |
| R2  | 10 kΩ | 1/4 W resistor | GPIO21 signal net to +3V3 | External pull-up; reduces RC time constant with C1 from 4.5 ms (internal 45 kΩ alone) to ~0.8 ms for faster edge recovery |
| C1  | 100 nF | Ceramic, 0402 or 0603 | GPIO21 signal net to GND, close to J1 pin 3 | Motor switching noise filter on IR signal |
| C2  | 100 nF | Ceramic, 0402 or 0603 | DRV8833 VM to GND, close to VM pin | HF decoupling for motor driver |
| C3  | 10 µF | Electrolytic or ceramic, ≥10 V | DRV8833 VM to GND | Bulk decoupling for motor current transients |

---

## Net List Summary

```
+5V:    Power input(+) — XIAO 5V — DRV8833 VM — DRV8833 nSLEEP — C2(+) — C3(+)
+3V3:   XIAO 3V3 — R1(one end) — C1(one end via pull-up inside XIAO)
GND:    Power input(−) — XIAO GND — DRV8833 GND — J1 pins 2&4 — C1(−) — C2(−) — C3(−)

GPIO3:  XIAO D3 — DRV8833 AIN1
GPIO4:  XIAO D4 — DRV8833 AIN2
GPIO5:  XIAO D5 — DRV8833 BIN1
GPIO6:  XIAO D6 — DRV8833 BIN2

IR_SIG: J1 pin 3 — C1(+) — R2(one end) — XIAO GPIO21 (internal 45kΩ pull-up to 3V3)
IR_LED: R1(other end) — J1 pin 1
R2:     other end → +3V3
```

---

## PCB Layout Notes

- Place C2 and C3 as close as possible to the DRV8833 VM pin to minimize loop inductance.
- Place C1 as close as possible to the J1 signal pin (pin 3), not at the XIAO.
- Route AOUT1/2 and BOUT1/2 motor traces as wide as practical (≥ 0.5 mm / 20 mil); they carry peak motor current.
- Keep motor output traces away from the IR signal net — motor switching noise can couple into the IR signal (mitigated in firmware via brake-mode stop, but good layout practice regardless).
- The XIAO ESP32C3 module mounts as a sub-board (castellated or header pins); no additional decoupling caps needed for the 3.3 V rail on the PCB.
- nFAULT is open-drain on the DRV8833 — leave the pad unconnected or add a DNP pull-up if fault monitoring is added later.

---

## PI Controller Tuning

The regulator applies a PI correction once per hour based on swing count vs. elapsed NTP time.

| Parameter | Default | Notes |
|-----------|---------|-------|
| Kp | 0.005 | Proportional gain (inches per swing/hr of rate error) |
| Ki | 0.001 | Integral gain (inches per accumulated second of drift) |
| Max travel | ±0.2 in | Hard clamp on total clip position |

**Nonlinearity note:** The pendulum period varies as √L, so equal clip movements produce diminishing rate corrections as the pendulum lengthens. Gains are calibrated for the mid-travel operating point. If the clock settles near either extreme of travel, Kp may need adjustment.
