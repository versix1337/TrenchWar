try:
    import websockets
    print("websockets OK")
except:
    print("websockets MISSING")

try:
    import aiohttp
    print("aiohttp OK")
except:
    print("aiohttp MISSING")
