import os
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

async def main():
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    stream = client.aio.models.generate_content_stream(
        model="gemini-2.5-flash",
        contents="Hello, say 3 sentences about cats.",
    )
    
    # Check if stream needs to be awaited or iterated directly
    if hasattr(stream, "__aiter__"):
        print("stream is an async generator")
        async for chunk in stream:
            print("CHUNK:", chunk.text)
    else:
        print("stream is a coroutine")
        actual_stream = await stream
        async for chunk in actual_stream:
            print("CHUNK:", chunk.text)

asyncio.run(main())
