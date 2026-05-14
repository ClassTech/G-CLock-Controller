from machine import Pin

STEPPER_PINS = [Pin(3, Pin.OUT), Pin(4, Pin.OUT), Pin(5, Pin.OUT), Pin(6, Pin.OUT)]

print("Stepper holding. Press Ctrl+C to release.")

try:
    for pin, val in zip(STEPPER_PINS, [1, 0, 1, 0]):
        pin.value(val)
    while True:
        pass
except KeyboardInterrupt:
    for pin in STEPPER_PINS:
        pin.value(0)
    print("Stepper released.")
