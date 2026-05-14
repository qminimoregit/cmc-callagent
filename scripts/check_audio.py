import sounddevice as sd
print("Available Audio Devices:")
print(sd.query_devices())
print("\nDefault Input Device:", sd.default.device[0])
