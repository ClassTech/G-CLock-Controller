# File: main.py (simplified)
# Keep this as .py for easier debugging/modification

from pendulum_controller import PendulumController

if __name__ == "__main__":
    controller = PendulumController()
    controller.run()