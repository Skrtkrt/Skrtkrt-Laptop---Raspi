import time
import board
import busio

# Multiplexer and ADS libraries
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_tca9548a import TCA9548A

# Initialize I2C bus
i2c = busio.I2C(board.SCL, board.SDA)

# Initialize TCA9548A multiplexer
tca = TCA9548A(i2c)

# Select channel 1 on the TCA9548A (where ADS1115 is connected)
ads_i2c = tca[1]

# Initialize ADS1115 on channel 1 of the multiplexer
ads = ADS.ADS1115(ads_i2c)

# Read from A2 (ADS.P2 corresponds to pin A0)
channel = AnalogIn(ads, ADS.P0)

# Loop to read turbidity sensor voltage
while True:
    voltage = channel.voltage
    print(f"Turbidity Sensor Voltage (A0): {voltage:.3f}V")
    time.sleep(1)
