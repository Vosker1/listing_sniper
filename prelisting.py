import requests

response = requests.get('https://api.bybit.com/v5/market/instruments-info', 
                        params={'category': 'linear', 'limit': 1000}, timeout=10)
data = response.json()

if data.get('retCode') == 0:
    instruments = data.get('result', {}).get('list', [])
    
    prelistings = [i for i in instruments if i.get('isPreListing') == True]
    
    print(f"=== PRELISTINGS: {len(prelistings)} ===\n")
    
    if prelistings:
        for p in prelistings:
            print(f"Symbol: {p.get('symbol')}")
            print(f"  launchTime: {p.get('launchTime')}")
            print(f"  preListingInfo: {p.get('preListingInfo')}")
    else:
        print("Geen pre-listings nu.")