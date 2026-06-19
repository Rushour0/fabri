import json
import sys

data = json.loads(sys.stdin.read())
print(json.dumps({"echoed": data}))
