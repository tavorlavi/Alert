import httpx, asyncio
async def test():
    async with httpx.AsyncClient() as c:
        r = await c.get('https://data.gov.il/api/3/action/datastore_search?resource_id=5c78e9fa-c2e2-4771-93ff-7f400a12f7ba&limit=2')
        print(r.json()['result']['records'])
asyncio.run(test())
