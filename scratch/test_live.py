import asyncio
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

async def main():
    client = genai.Client()
    print("Connecting...")
    async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config={"response_modalities": ["AUDIO"]}) as session:
        print("Connected!")
        await session.send(input="Hello, can you hear me?", end_of_turn=True)
        print("Sent message")
        async for response in session.receive():
            server_content = response.server_content
            if server_content is not None:
                model_turn = server_content.model_turn
                if model_turn:
                    for part in model_turn.parts:
                        print(f"Received part: text={bool(part.text)} audio={bool(part.inline_data)}")
                        if part.text:
                            print(part.text, end="")
            elif response.tool_call is not None:
                print("Tool call!")

if __name__ == "__main__":
    asyncio.run(main())
