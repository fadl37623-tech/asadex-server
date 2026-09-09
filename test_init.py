import requests
r = requests.get("https://asadex-server-production.up.railway.app/init")
print(r.json())
