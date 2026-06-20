import sys
import time

mode = sys.stdin.read()
if "slow" in mode:
    time.sleep(5)
print("this is not json")
