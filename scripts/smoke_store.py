import asyncio
import json
from app import store

async def main():
    test_state = {"smoke_test": True}
    await store.save_storage_state(test_state)
    s = await store.get_storage_state()
    print(json.dumps(s))

if __name__ == "__main__":
    asyncio.run(main())
