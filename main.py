# File: main.py (simplified)
# Keep this as .py for easier debugging/modification

from pendulum_controller import PendulumController

if __name__ == "__main__":
    # Create controller instance and run
    controller = PendulumController()
    controller.run("<YOUR_WIFI_SSID>", "<PASSWORD>", hostname="pendulum-clock")