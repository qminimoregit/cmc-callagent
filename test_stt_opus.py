import asyncio
from google.cloud import speech
from src.stt import get_stt_config

def main():
    print(speech.RecognitionConfig.AudioEncoding.WEBM_OPUS)

main()
