import os
import asyncio
from google import genai
from dotenv import load_dotenv

load_dotenv()

async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    if hasattr(client, "aio"):
        print("client.aio exists!")
    else:
        print("client.aio DOES NOT exist")

asyncio.run(main())
